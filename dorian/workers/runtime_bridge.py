"""
Runtime Bridge — Redis-based task consumer for the Rust engine.

This module bridges the Rust engine's dispatch layer to Python execution.
The Rust dispatcher publishes NodeTask messages to a Redis stream; this
bridge reads them, executes the operator/snippet/parameter, and publishes
NodeResult back via Redis.

Architecture:
  Rust Engine (gRPC) → Redis Stream "runtime:python:tasks"
                      ← Redis Stream "runtime:python:results"
  This bridge        → reads tasks, executes, writes results

The bridge runs as a standalone process (or container) managed by the
Rust scaling controller. It does NOT import FastAPI or any web server.

Usage:
  python -m dorian.workers.runtime_bridge
  # or via Docker: uv run python -m dorian.workers.runtime_bridge
"""

import asyncio
import ast
import importlib
import inspect
import json
import logging
import os
import signal
import sys
import time
import traceback
from typing import Any
from urllib.parse import urlparse, urlunparse

import redis.asyncio as aioredis

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REDIS_URL = os.environ["DORIAN_REDIS_URL"]  # required — port lives in DORIAN_REDIS_PORT, baked into the URL by compose
TASK_STREAM = "runtime:python:tasks"
RESULT_STREAM = "runtime:python:results"
STREAM_MAXLEN = 10_000  # approximate cap, mirrors dorian.infra.keys.STREAM_MAXLEN
CONSUMER_GROUP = "python-workers"
CONSUMER_NAME = f"worker-{os.getpid()}"
BLOCK_MS = 5000  # Poll interval (5s)
BATCH_SIZE = 1   # Process one task at a time (sequential for safety)
DEFAULT_TASK_TIMEOUT = 300  # 5 minute hard cap per task
MAX_OUTPUT_SIZE = 10 * 1024 * 1024  # 10 MB max result payload

# Allowlist of module prefixes that execute_operator may import.
# Everything else is rejected to prevent arbitrary module loading.
_ALLOWED_MODULE_PREFIXES = (
    "sklearn.",
    "pandas.",
    "pandas",
    "numpy.",
    "numpy",
    "scipy.",
    "openrouter.",
    "trust_guardrails.",
    "aif360.",
    "dorian.",
)

# Allowlist of env var prefixes for the `env` dtype (vault references).
_ALLOWED_ENV_PREFIXES = ("DORIAN_VAULT_", "VAULT_")

logger = logging.getLogger("dorian.workers.runtime_bridge")


def _redact_url(url: str) -> str:
    """Mask password in a Redis URL for safe logging."""
    try:
        parsed = urlparse(url)
        if parsed.password:
            replaced = parsed._replace(
                netloc=f"{parsed.username or ''}:***@{parsed.hostname}:{parsed.port or ''}"
            )
            return urlunparse(replaced)
    except Exception:
        pass
    return url

# ---------------------------------------------------------------------------
# Task execution
# ---------------------------------------------------------------------------


def execute_operator(name: str, language: str, inputs: dict[str, Any]) -> Any:
    """Execute a Python operator by dotted import path.

    This mirrors the resolution logic in dorian/pipeline/operator_resolver.py.
    The operator is imported, and if it's a class, it's instantiated.
    Only modules matching _ALLOWED_MODULE_PREFIXES are permitted.
    """
    parts = name.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid operator name: {name}")

    module_path, attr_name = parts

    if not any(module_path.startswith(p) for p in _ALLOWED_MODULE_PREFIXES):
        raise PermissionError(
            f"Module '{module_path}' is not in the allowed import list"
        )

    module = importlib.import_module(module_path)
    obj = getattr(module, attr_name)

    if inspect.isclass(obj):
        # Class: instantiate with keyword args from inputs.
        kwargs = {k: v for k, v in inputs.items() if not k.startswith("_")}
        return obj(**kwargs)
    elif callable(obj):
        # Function: call with inputs as arguments.
        return obj(**inputs)
    else:
        return obj


def execute_snippet(code: str, inputs: dict[str, Any]) -> Any:
    """Execute a user-defined snippet.

    Mirrors the snippet execution in operator_resolver.py:
    exec(code) then call foo() with inputs.
    """
    # Restricted builtins — no __import__ (prevents sandbox escape),
    # no type() (prevents MRO traversal to access os/subprocess).
    # Pre-inject commonly needed libraries instead.
    safe_builtins = {
        "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
        "enumerate": enumerate, "filter": filter, "float": float,
        "int": int, "isinstance": isinstance, "len": len, "list": list,
        "map": map, "max": max, "min": min, "print": print, "range": range,
        "round": round, "set": set, "sorted": sorted, "str": str,
        "sum": sum, "tuple": tuple, "zip": zip,
        "None": None, "True": True, "False": False,
    }

    # Pre-inject safe libraries so snippets don't need __import__.
    import numpy as _np
    import pandas as _pd
    namespace: dict[str, Any] = {
        "__builtins__": safe_builtins,
        "np": _np,
        "pd": _pd,
        "inspect": inspect,
    }
    exec(code, namespace)  # noqa: S102 — sandboxed execution

    foo = namespace.get("foo")
    if foo is None:
        raise ValueError("Snippet must define a foo() function")

    return foo(**inputs)


def evaluate_parameter(dtype: str, value: str) -> Any:
    """Evaluate a parameter value. Mirrors Parameter.__call__ in dag.py."""
    if dtype == "string" or dtype == "str":
        return value
    elif dtype == "int":
        return int(value)
    elif dtype == "float":
        return float(value)
    elif dtype == "bool":
        return value.lower() in ("true", "1", "yes")
    elif dtype == "eval":
        return ast.literal_eval(value)
    elif dtype == "env":
        # Vault reference — resolve from environment, scoped to allowed prefixes.
        var_name = value.strip("${}")
        if not any(var_name.startswith(p) for p in _ALLOWED_ENV_PREFIXES):
            raise PermissionError(
                f"Environment variable '{var_name}' is not in the allowed prefix list"
            )
        return os.getenv(var_name, "")
    else:
        return value


def _execute_sync(task_data: dict[str, str], inputs: dict[str, Any]) -> Any:
    """Execute the task synchronously (called in a thread for timeout support)."""
    node_type = task_data.get("node_type", "operator")
    if node_type == "parameter":
        return evaluate_parameter(
            task_data.get("dtype", "string"),
            task_data.get("value", ""),
        )
    elif node_type == "snippet":
        return execute_snippet(task_data.get("code", ""), inputs)
    else:
        return execute_operator(
            task_data.get("name", ""),
            task_data.get("language", "python"),
            inputs,
        )


async def process_task(task_data: dict[str, str]) -> dict[str, Any]:
    """Process a single task from the Redis stream.

    Task format (from Rust dispatcher, serialized as JSON in the stream):
      task_id: str
      run_id: str
      node_id: str
      node_type: "operator" | "snippet" | "parameter"
      name: str           (operator FQN or snippet name)
      language: str       (always "python" for this bridge)
      code: str           (snippet code, empty for operators)
      dtype: str          (parameter dtype, empty for operators)
      value: str          (parameter value, empty for operators)
      inputs: JSON str    (input data references as JSON)
      context: JSON str   (execution context as JSON)
      timeout: float      (seconds, 0 = no timeout)

    Returns a result dict matching NodeResult:
      task_id, node_id, status, outputs, error_message, duration_seconds
    """
    task_id = task_data.get("task_id", "unknown")
    node_id = task_data.get("node_id", "unknown")

    # Parse timeout from task, cap to DEFAULT_TASK_TIMEOUT.
    try:
        timeout = float(task_data.get("timeout", "0"))
    except (ValueError, TypeError):
        timeout = 0
    if timeout <= 0 or timeout > DEFAULT_TASK_TIMEOUT:
        timeout = DEFAULT_TASK_TIMEOUT

    start = time.monotonic()

    try:
        inputs = json.loads(task_data.get("inputs", "{}"))

        # Run in a thread with asyncio timeout enforcement.
        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _execute_sync, task_data, inputs),
            timeout=timeout,
        )

        duration = time.monotonic() - start

        # Truncate output to prevent Redis memory exhaustion.
        output_str = str(result)
        if len(output_str) > MAX_OUTPUT_SIZE:
            output_str = output_str[:MAX_OUTPUT_SIZE] + "... [truncated]"

        return {
            "task_id": task_id,
            "node_id": node_id,
            "status": "success",
            "outputs": json.dumps({"0": output_str}),
            "error_message": "",
            "error_traceback": "",
            "duration_seconds": str(duration),
        }

    except asyncio.TimeoutError:
        duration = time.monotonic() - start
        logger.error("task %s timed out after %.1fs", task_id, timeout)
        return {
            "task_id": task_id,
            "node_id": node_id,
            "status": "failed",
            "outputs": "{}",
            "error_message": f"Task timed out after {timeout:.0f}s",
            "error_traceback": "",
            "duration_seconds": str(duration),
        }

    except Exception as e:
        duration = time.monotonic() - start
        # Log full traceback server-side, return only error type + message to caller.
        logger.error("task %s failed: %s\n%s", task_id, e, traceback.format_exc())
        return {
            "task_id": task_id,
            "node_id": node_id,
            "status": "failed",
            "outputs": "{}",
            "error_message": f"{type(e).__name__}: {e}",
            "error_traceback": "",
            "duration_seconds": str(duration),
        }


# ---------------------------------------------------------------------------
# Main event loop
# ---------------------------------------------------------------------------


async def run_bridge() -> None:
    """Main loop: read tasks from Redis, execute, write results."""
    rdb = aioredis.from_url(REDIS_URL, decode_responses=True)

    # Ensure consumer group exists (MKSTREAM creates the stream if needed).
    try:
        await rdb.xgroup_create(TASK_STREAM, CONSUMER_GROUP, id="0", mkstream=True)
        logger.info("created consumer group %s on %s", CONSUMER_GROUP, TASK_STREAM)
    except aioredis.ResponseError as e:
        if "BUSYGROUP" in str(e):
            logger.info("consumer group %s already exists", CONSUMER_GROUP)
        else:
            raise

    logger.info(
        "runtime bridge started (consumer=%s, redis=%s)",
        CONSUMER_NAME,
        _redact_url(REDIS_URL),
    )

    shutdown = asyncio.Event()

    def handle_signal(sig: int, frame: Any) -> None:
        logger.info("received signal %s, shutting down", sig)
        shutdown.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    tasks_processed = 0

    while not shutdown.is_set():
        try:
            # Read from the consumer group.
            messages = await rdb.xreadgroup(
                CONSUMER_GROUP,
                CONSUMER_NAME,
                {TASK_STREAM: ">"},
                count=BATCH_SIZE,
                block=BLOCK_MS,
            )

            if not messages:
                continue

            for stream_name, stream_messages in messages:
                for msg_id, task_data in stream_messages:
                    logger.debug("processing task %s (msg=%s)", task_data.get("task_id"), msg_id)

                    # Execute the task.
                    result = await process_task(task_data)

                    # Write result to the results stream.
                    await rdb.xadd(RESULT_STREAM, result, maxlen=STREAM_MAXLEN, approximate=True)

                    # Acknowledge the task.
                    await rdb.xack(TASK_STREAM, CONSUMER_GROUP, msg_id)

                    tasks_processed += 1

                    if tasks_processed % 100 == 0:
                        logger.info("processed %d tasks", tasks_processed)

        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("error in bridge loop, retrying in 1s")
            await asyncio.sleep(1)

    logger.info("runtime bridge stopped after %d tasks", tasks_processed)
    await rdb.aclose()


def main() -> None:
    """Entry point for `python -m dorian.workers.runtime_bridge`."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    asyncio.run(run_bridge())


if __name__ == "__main__":
    main()
