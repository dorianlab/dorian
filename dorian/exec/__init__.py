"""
dorian/exec
-----------
Execution engine: a pool of Python workers that consume data/compute
jobs from a Redis stream and emit completion events. Event-bus
handlers (Go) submit jobs here and then react to completion events.

The intent is a strict split:

  * Coordination (event routing, validation, decision application) =
    Go, in the event-bus subscriber. Latency is ms, no GIL, no jitter.
  * Compute (pandas / numpy / sklearn / KB queries / any pipeline
    work) = Python, here. Workers scale independently.

Jobs are durable (XADD → XREADGROUP consumer group), so submitter
crashes do not lose work and worker crashes redeliver via XPENDING.
Results are written to a per-job Redis key with a configurable TTL
and announced via a ``{Kind}Completed`` event on the regular event bus.

Logical vs. physical operators — the long-term shape
----------------------------------------------------
A job's ``kind`` today ("dq_check:missing_values") is a LOGICAL
identifier. There is exactly one physical implementation (Python,
this package), but the engine is designed to host multiple
implementations of the same logical kind, each registered in the KB
with capability annotations (input-size class, runtime, expected
throughput, serialization cost).

Future: ``kind`` stays logical. The submitter (Go event-bus handler
or the Rust pipeline engine) does NOT bind a specific implementation
— it queries the KB for the set of workers that can service the kind
for THIS input's characteristics, and picks one based on recent
execution stats. A Rust worker for ``dq_check:missing_values`` could
register under the same kind with a "small-dataset-preferred"
capability; the Python implementation stays for cases where serde
cost exceeds compute time.

That optimizer belongs in the pipeline engine (Rust phase 1+). The
exec package's contract here (stream + consumer group + completion
event) supports it without any naming change — logical kinds are
already what the stream carries.
"""

from dorian.exec.registry import register, get_registry
from dorian.exec.worker import Worker, run_forever

__all__ = ["register", "get_registry", "Worker", "run_forever"]
