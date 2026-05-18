# Dorian — Development Principles

This document is the single authoritative reference for contributors.
It explains *why* the codebase is structured the way it is, not just *what* is in it.
The `tree` section at the end will be filled in after any structural reorganisation is accepted.

---

## 1. Architectural layers

```
dorian/      ← core library  (pure Python, no server, no web)
backend/     ← makes the library a FastAPI service
frontend/    ← React/Next.js UI that talks to backend via WebSocket + REST
```

**`dorian/`** has no dependency on FastAPI, Redis, or any infrastructure.
It can be imported in a notebook, a script, or a test without a running server.

**`backend/`** wires the library to infrastructure: WebSocket routing, Redis, Dask cluster,
PostgreSQL (Experiment Store + document collections via `pg_collections`), event dispatch.
It imports `dorian`; `dorian` never imports `backend`.

**`frontend/`** is a separate Node process.  It communicates exclusively via the WebSocket
defined in `dorian/api/websocket.py`.  The only contract between frontend and backend is the
`AppEventName` union type mirrored in `frontend/types/index.ts`.

---

## 2. Human-readable import paths

Module names are chosen so that import statements read left-to-right as an English phrase:

```python
from dorian.code.parsing import rule         # dorian  → code  → parsing  → rule
from dorian.knowledge.management import ...  # dorian  → knowledge → management
from dorian.pipeline.execution import ...    # dorian  → pipeline  → execution
from dorian.tabular.data.profiling import profiler
```

Prefer concrete sub-packages over flat modules. A module called `utils.py` or `helpers.py`
at package root is a sign that it belongs somewhere more specific.

---

## 3. Python naming conventions

| Thing | Convention | Example |
|---|---|---|
| Modules / packages | `snake_case` | `data_science_task.py` |
| Classes | `PascalCase` | `RewriteRule`, `Operator` |
| Functions / methods | `snake_case` | `get_operator_interface` |
| Constants | `UPPER_SNAKE` | `TOOLTIPS`, `COMPOUND_OPERATOR_EXPANSION_RULE` |
| Private helpers | `_leading_underscore` | `_fit_arity`, `_make_param` |
| Redis key helpers | `RedisKeys.*` static methods | `RedisKeys.session_meta(session)` |

No PascalCase filenames anywhere.  Any new file must use `snake_case`.

---

## 4. Configuration

Application settings are loaded by **dynaconf** from `config/config.yaml`:

```python
# backend/config.py
from dynaconf import Dynaconf
from pathlib import Path

base = Path(__file__).parents[1]          # project root
config = Dynaconf(settings_files=['config/config.yaml'])
config = config[config.type]              # select env: dev / prod / test
```

Access nested values via attribute syntax: `config.dask.cluster`, `config.redis.host`,
`config.postgresql.host`, `config.cache.max_bytes`.

Always use `backend.config.config` — never read environment variables inline.

---

## 5. Preferred standard library and third-party modules

| Task | Use | Avoid |
|---|---|---|
| File paths | `pathlib.Path` | `os.path`, string concatenation |
| Dates / times | `pendulum` | bare `datetime` (tz-unaware) |
| Async Redis | `aioredis` (via `backend.envs.aioredis`) | direct `redis.asyncio` |
| Sync Redis | `redis` (via `backend.envs.redis`) | |
| HTTP client | `httpx` | `requests` in async code |
| Observability | `emit(Event(...))` / `aemit(Event(...))` from `backend.events` | `logging`, `print` |
| Caching KB queries | `functools.lru_cache` | manual dicts |
| Config | `backend.config.config` (dynaconf) | env vars read inline |
| WS encoding | `msgpack` | JSON over the wire |

---

## 6. Event bus

Every meaningful user action must emit an event.  Events are the primary mechanism for
analytics, session replay, and triggering downstream logic.

### Event class

```python
# backend/events.py
@dataclass
class Event:
    type: str
    data: Dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str):
        if key not in self.data:
            raise KeyError(f"{key!r} not found in {self.type} event")
        return self.data[key]
```

`Event(type="DatasetUploaded", data={"uid": ..., "session": ...})`.
Handlers receive this and access fields via `event["uid"]` or `event.data["uid"]`.

Handler type alias:
```python
Handler: TypeAlias = Callable[[Event], None] | Callable[[Event], Coroutine[None, None, None]]
```
Handlers may be sync or async — the bus detects which and dispatches correctly.

### Anatomy of an event

```
AppEventName  →  WsMessage  →  backend.events.Event
```

Frontend sends:
```typescript
// helpers/ws-events.ts — ALWAYS use emitEvent, never raw sendMessage
emitEvent("DataScienceTaskSelected", { taskId: "classification" });
// ↑ uid, session, ts, requestId are injected automatically
```

Backend receives:
```python
# registry.py
subscribe("DataScienceTaskSelected", handle_data_science_task_selected)

# handler
async def handle_data_science_task_selected(event: Event):
    uid     = event.data["uid"]
    session = event.data["session"]
    task_id = event.data["taskId"]
```

### Naming convention

Event names are `PascalCase` **past-tense verb + noun**:
`DatasetUploaded`, `PipelineSaved`, `EvaluationProcedureSelected`.

"Clicked" suffix is reserved for pure UI interactions that have no backend domain logic
(`PipelineExportClicked`) — these still emit so they are logged for analytics, but the
handler is `persist_interaction_event`, not a domain function.

### Worker pool

Event handlers execute inside a bounded worker pool (not in the request coroutine):

```
POOL_SIZE      = 32    # concurrent handler coroutines
QUEUE_CAPACITY = 256   # bounded asyncio.Queue; aemit blocks when full
```

`start_workers()` / `stop_workers()` are called once during app startup / shutdown
via the FastAPI `lifespan` context manager.  When the pool is inactive (startup,
shutdown, tests) handlers execute directly in the caller's coroutine.

### emit vs aemit

| Context | Function | Redis client |
|---|---|---|
| `async def` handler (FastAPI / asyncio) | `await aemit(Event(...))` | `aioredis` |
| Sync code (Dask worker, background thread) | `emit(Event(...))` | `redis` |

**`emit()` raises `RuntimeError` if called from an async context.**
It bridges to the main event loop via `run_coroutine_threadsafe`.  Never call
`asyncio.run(aemit(...))` inside a running event loop — use `emit()` instead.

### Handler instrumentation

Every handler invocation is instrumented automatically by `_run_handler`:
```
HANDLER fn=module.qualname event=EventName wall=0.042s rss=234.5MB delta=+0.1MB error=False
```
Logger: `dorian.observability`.  This covers wall-clock time, absolute RSS, RSS delta,
and whether the handler raised.  The built-in `verbose` handler is exempt (print-only).

### with_envelope

`with_envelope(handler)` wraps a handler to unpack the standard envelope fields into
keyword arguments:

```python
async def my_handler(event, *, uid, session, payload, request_id, ts):
    ...

subscribe("MyEvent", with_envelope(my_handler))
```

Use it when the handler pushes a state update back to the frontend over the WS stream.
Handlers that only write to Redis or trigger internal events do not need it.

### Session-meta helpers

```python
await _get_session_meta(session)     # → dict | None (from Redis session:{session}:meta)
await _save_session_meta(session, meta)  # set JSON to session:{session}:meta
await _xadd(uid, session, message)   # push to {uid}:{session}:stream + emit notification
await _set_json(key, value)          # aioredis.set(key, json.dumps(value))
```

Defined in `dorian/event/helpers/lifecycle.py`.

---

## 7. WebSocket protocol

### Wire format

All messages between frontend and backend are **msgpack-encoded** binary frames.

```python
# Receiving (backend)
message = await websocket.receive_bytes()
payload = msgpack.unpackb(message, raw=False)

# Sending (backend)
await websocket.send_bytes(msgpack.packb(message, use_bin_type=True))
```

### Dispatch (incoming)

The dispatcher in `dorian/api/websocket.py` uses structural pattern matching:

```python
match payload:
    case {'event': 'init', 'user': uid_, 'session': sess_}:
        → Event("InitSession", ...)
    case {'event': 'feedback', 'user': uid_, 'session': sess_, 'answers': answers}:
        → Event("FeedbackReceived", ...)
    case {'event': str() as ev, 'payload': dict() as inner}:
        → Event(ev, data=inner)              # generic — covers all emitEvent calls
    case _:
        → Event("MalformedEvent", data=payload)
```

Error events: `WebsocketMalformedPayload` (bad msgpack), `MalformedEvent` (unknown shape),
`WebsocketDisconnected`, `WebsocketOnReceiveError`, `WebsocketOnSendError`.

### Response stream (outgoing)

Backend pushes responses to the frontend via a **Redis Stream** per connection:

```
Key:    {uid}:{session}:stream     ← XADD from handlers via _xadd()
Cursor: {uid}:{session}:last       ← tracks the last message ID read
```

The send loop blocks for 50 ms per iteration (`block=50`) and reads up to 20 messages
per wake-up (`count=20`), giving ~20 wake-ups/sec when idle and fast burst draining
under load.

Messages with `type: "list"` have their `value` field split by comma before sending —
this is a presentation-layer convention for multi-value state updates.

Connection lifecycle: `receive_messages` and `send_messages` run as concurrent
`asyncio.Tasks`; when either completes the WebSocket is torn down.

---

## 8. Redis key conventions

All keys are defined as class methods on `RedisKeys` in `dorian/infra/keys.py`.
Never construct Redis keys from raw f-strings inline.

```
session:{session}:meta                 JSON session state (dataset, pipeline, pipelineHistory, rankingObjectives)
dataset:fpath:{did}                    absolute CSV/Parquet path
dataset:{did}:feature_columns          JSON list of feature column names
dataset:{did}:target_columns           JSON list of target column names
feedback:{uid}:{session}:{reqId}       single feedback submission (scoped, never collides)
feedback:{uid}:{session}:history       RPUSH list — full submission history in order
interactions:{uid}:{session}           RPUSH list — canvas interaction events in order
execution:{run_id}                     PipelineExecution JSON
{uid}:{session}:stream                 Redis Stream — outgoing WS messages (XADD/XREAD)
{uid}:{session}:last                   cursor — last stream message ID sent to the client
task_queue                             Sorted set — pending pipeline executions (ZADD/ZPOPMIN)
```

Key structure rule: always scope to `uid` and `session` before any domain key.
A bare domain key (e.g. a question ID from a feedback form) is **never** a Redis key.

---

## 9. DAG data model

The pipeline graph is represented by dataclasses in `dorian/dag.py`:

```python
@dataclass
class Operator:
    name: str                            # e.g. "sklearn.preprocessing.StandardScaler"
    language: str                        # "python"
    tasks: Optional[Sequence[str]] = []  # lifecycle methods ["__init__", "fit", "transform"]

@dataclass
class Snippet:
    name: str          # identifier
    code: str          # Python code defining a `foo(...)` function
    language: str      # "python"
    # __call__ exec's code and invokes foo(*args, **kwargs)

@dataclass
class Parameter:
    name: str          # e.g. "fpath", "n_estimators"
    dtype: SupportedType   # "int" | "float" | "string"
    value: str         # string representation — evaluated at runtime via eval(dtype)(value)

@dataclass
class Edge:
    source: UUID
    destination: UUID
    position: Positional | Keyword = 0   # int = arg slot, str = kwarg name
    output: Positional = 0               # source port index (0 = default single output)

@dataclass
class Node:
    type: str = ".*"       # regex for rewrite-rule pattern matching
    text: str = ".*"       # regex against operator name
    language: str = ".*"

@dataclass
class DAG:
    nodes: Dict[UUID, Operator | Snippet | Parameter] = {}
    edges: List[Edge] = []
```

`Node` is only used in rewrite-rule patterns (section 10).  Pipeline DAGs contain
`Operator`, `Snippet`, and `Parameter` nodes.

`UUID` is just `str` (from `dorian/types.py`).

---

## 10. DAG rewrite rules

`dorian/pipeline/transforms.py` applies pre-execution pipeline rewrites.

```
pipeline DAG  →  expand_dataset_refs  →  expand_compound_operators  →  build_dag_graph
```

All rule primitives live in `dorian/code/parsing/rule.py`:

```python
Transformation = Add | Apply | Replace | Delete | Revert | ToOperator | ToParameter
```

A `RewriteRule` has a `pattern` DAG and a list of `Transformation` steps.
`Apply.f` signature: `f(dag: DAG, mapping: dict, meta: dict) -> DAG`.
`meta` carries runtime context (session id, dataset fpath).

- `sync_apply` — for background threads (Dask submission path)
- `apply` (async) — for event handlers and future mitigation rules

The same `RewriteRule` object works in both contexts.

Adding a new rewrite:
1. Write an `_expand_*` function in `transforms.py`.
2. Create a module-level `*_RULE = RewriteRule(...)` constant.
3. Chain the new rule in `expand_*` entry-point function called from `execution.py`.
4. Add a unit test that mocks any KB queries (`lru_cache` patching).

---

## 11. Dask execution engine

The ML operators run inside the Dask distributed worker pool, **not** in the FastAPI
process.  The execution path is:

```
ExecutePipeline event
  → submit_for_execution (backend/queue.py)
  → expand_dataset_refs + expand_compound_operators
  → build_dag_graph (operator_resolver.py)    # pure dict, no Dask objects yet
  → executor.submit(dask.get, graph, keys)
  → workers execute operators
  → pipeline/node/started|completed|failed events emitted per node
```

### Operator resolver

`dorian/pipeline/operator_resolver.py` converts node objects to callables:

| Input | Resolution |
|---|---|
| **Method shortcut** (`fit`, `predict`, `transform`, …) | First positional arg is the instance; dispatches `instance.method(*rest)` |
| **Built-in** (no dot: `print`, `len`) | `getattr(__builtins__, name)` |
| **Dotted path** (`sklearn.X.Y`) | Import + call; if `inspect.isclass` → instantiate |
| **Parameter** | `eval(dtype)(value)` (e.g. `int("42")`) |
| **Snippet** | `exec(code)` then call `foo(...)` |

Full method shortcut set:
```python
_METHOD_SHORTCUTS = frozenset({
    "fit", "predict", "transform", "fit_transform", "fit_predict",
    "score", "predict_proba", "predict_log_proba", "decision_function",
    "inverse_transform", "partial_fit",
})
```

Library name mapping (import name → pip name):
```python
_LIBRARY_MAP = {"sklearn": "scikit-learn", "keras": "tensorflow[keras]",
                "cv2": "opencv-python", "PIL": "Pillow", "bs4": "beautifulsoup4"}
```

Missing packages are auto-installed via `pip` (emitting `PackageInstalling` /
`PackageInstalled` / `PackageInstallFailed` events).

### Dask graph shape

Each graph entry: `{node_id: (callable, *dep_keys)}`.
Multi-output nodes get slice entries: `{"{src}_{output}": (_slice, src_key, output_index)}`.
Keyword args are handled by a `_wrapper` that unpacks trailing positional slots back
into kwargs at runtime — Dask only natively supports `(callable, *args)`.

### Task queue and backpressure

Pipeline submissions go through a Redis sorted set (`task_queue`) with priority scoring.
A background `bridge_logic` coroutine pops items and submits them to Dask, using an
**elastic limit** based on the current worker thread count to prevent flooding the cluster.

### Limitations

- Worker code is serialised via cloudpickle.  Functions defined inside closures or
  that reference un-importable objects will fail with cryptic pickling errors.
- Imports inside worker functions must be absolute.  `from dorian import ...` works;
  relative imports do not survive pickling.
- Do not start asyncio event loops inside Dask tasks — the worker thread has its own
  event loop managed by Dask.
- `multioutput` nodes (returning tuples/lists) require slice entries in the graph dict;
  use `edge.output` (source port index), not `edge.position` (argument slot).

---

## 12. Knowledge base

The KB stores the operator catalogue, interface hierarchy, and method call sequences.
It ships as an in-tree snapshot (``dorian/knowledge/sources/*.kb``) loaded at process
start; the Rust path reads a JSON snapshot produced by
``scripts/export_kb_snapshot.py``.

Query modules live under `dorian/knowledge/`.  All queries are:
- **Read-only** — the KB is never mutated at runtime.
- **Cached** with `@functools.lru_cache` to avoid repeated round-trips per pipeline run.
- **Synchronous** — KB queries happen in the background thread (DAG expansion), not in
  the asyncio event loop.

The two queries used by the compound-operator expansion:
```python
get_operator_interface(op_name) -> str | None   # e.g. "Sklearn Transformer"
get_method_sequence(interface_name) -> list[str] # e.g. ["__init__", "fit", "transform"]
```

Every executable operator must have an interface in the KB.
A missing interface is a KB completeness error, not a code path — log a warning and skip
expansion rather than raising.

---

## 13. Frontend state and WebSocket conventions

- **All global state** lives in Zustand stores under `frontend/store/`.
  Import from the barrel: `import { usePipelineStore, useUIStore } from "@/store"`.
- **All WS emissions** go through `emitEvent` in `frontend/helpers/ws-events.ts`.
  Never call `sendMessage` directly in components.
- **`emitEvent`** injects `uid`, `session`, `ts`, `requestId` automatically.
  Pass only domain-specific fields in the payload.
- **Store split**: `usePipelineStore` owns design-time graph state.
  `usePipelineRunStore` owns execution-time run state.  Keeping them separate prevents
  pipeline-run re-renders from invalidating the static graph.
- **Incoming WS events** are dispatched via the `eventHandlers` map in
  `usePipelineSocket.ts`.  Adding a new inbound event is one map entry; no switch needed.
- **Store access in callbacks**: Use `usePipelineStore.getState()` (Zustand's static accessor)
  inside `onDrop`, `onConnect`, and other callbacks where React hook rules prohibit calling
  `usePipelineStore()`.

---

## 14. Application lifecycle

```python
# main.py — FastAPI lifespan (simplified)
@asynccontextmanager
async def lifespan(app: FastAPI):
    # STARTUP
    await start_workers()                    # 32 event-handler coroutines
    await start_sampler(interval=5.0)        # CPU/RSS monitor
    bridge = asyncio.create_task(bridge_logic(executor, aioredis))  # pipeline scheduler

    register_event_handlers()                # subscribe all domain handlers

    yield

    # SHUTDOWN
    bridge.cancel()
    await stop_sampler()
    await stop_workers()
    executor.cancel(list(executor.futures))  # cancel pending Dask tasks
    executor.close(); cluster.close()
    redis.close(); await aioredis.close()
```

Routes (`session`, `file`) and the WebSocket endpoint are registered at module level.
CORS allows `localhost:3000` and `localhost:3001`.

---

## 15. Observability

### Process sampler

`dorian/observability/sampler.py` logs CPU usage and RSS every 5 seconds:

```
PROCESS cpu=12.3% rss=456.7MB
```

Logger: `dorian.observability.sampler`.  `start_sampler()` is idempotent (safe to call
multiple times).  The task is named `obs-sampler` for `asyncio.Task` introspection.

### Handler metrics

Every event-handler invocation is logged with wall time, RSS, RSS delta, and error status
(see section 6 "Handler instrumentation").  Logger: `dorian.observability`.

### Dask worker memory

`dask.config.set({"distributed.worker.memory.measure": "managed"})` in `backend/envs.py`
prevents false "unmanaged memory" warnings on Windows.

---

## 16. Testing

```bash
uv run pytest                   # all tests, coverage off
uv run pytest --cov             # with coverage (fail_under = 40)
uv run pytest tests/pipeline/   # subset
```

Configuration in `pyproject.toml` under `[tool.pytest.ini_options]`.

- Unit tests for DAG transforms: `tests/pipeline/test_*.py`
- Mock KB queries by patching `lru_cache`-decorated functions:
  ```python
  from unittest.mock import patch
  with patch("dorian.knowledge.queries.get_operator_interface", return_value="Sklearn Transformer"):
      ...
  ```
- Never hit a real Redis, PostgreSQL, or Dask cluster in unit tests.

---

## 17. Adding a new end-to-end feature (checklist)

1. **Backend event** — add `"MyNewEvent"` to `AppEventName` in `frontend/types/index.ts`
   and a handler in the appropriate `dorian/event/handlers/*.py`, registered in `registry.py`.
2. **Frontend emission** — add `ws.myNewEvent = (info) => emitEvent("MyNewEvent", info)` to
   `frontend/helpers/ws-events.ts`.
3. **Frontend inbound handler** (if the backend sends data back) — add one entry to the
   `eventHandlers` map in `usePipelineSocket.ts`.
4. **Store** — if new state is needed, add it to the relevant Zustand store and export from
   `frontend/store/index.ts`.
5. **Tests** — add a unit test for any non-trivial backend handler or DAG transform.
6. **Tooltip** — if the feature exposes a UI element, add an entry to `dorian/ui/tooltips.py`.

---

## 18. State replay invariant (KPI)

**Every user interaction must be replayable with 100% fidelity.**

When a user navigates away from a view (Back button, canvas switch, tab change) and returns,
the view must render **exactly** as they left it.  This is not optional polish — it is a
correctness requirement.  All state transitions must be deterministic, all view state must
be recoverable, and no navigation action may silently discard information.

### What this means in practice

| Layer | Requirement |
|---|---|
| **Events (frontend → backend)** | Every meaningful user action emits an event with enough context to reconstruct the state change.  Canvas events (`PipelineNodeAdded`, `PipelineEdgeRemoved`, `PipelineNodeConfigured`) log the full diff, not just the node ID. |
| **Redis persistence** | Session-scoped state lives in `session:{session}:meta` or a dedicated Redis key.  Transient state that must survive a page reload belongs in Redis, not in a Zustand store alone. |
| **Zustand stores** | Store state is the frontend's **cache** of backend truth.  On reconnect, `seed_session` re-hydrates all stores from Redis.  If a store field is not re-hydrated on reconnect, it will be lost — and that violates replay. |
| **Run state** | Pipeline execution state (`pipelineRun`, `checkReport`) must be scoped to a pipeline UUID.  When switching canvases, run state for the *previous* pipeline must be preserved (not cleared) so that returning to that pipeline restores the execution overlay.  **Current state**: run state is cleared on canvas switch as a stopgap; a proper pipeline-scoped run store is a tracked follow-up. |
| **AI Debugger** | Suggestions are scoped via the `canvas_operators` Redis SET and replayed on pipeline load (`sync_canvas_operators_from_pipeline`).  The `suggestions/reset` + re-identify flow ensures consistency after any topology change. |
| **Notifications / toasts** | Transient toasts (success, error) are fire-and-forget UI feedback — they do not need replay.  Persistent indicators (failed run badge on a node) must be state-driven and survive navigation. |

### Anti-patterns to avoid

- **`clearX()` on navigation** — Clearing a store on Back/switch destroys state.  Prefer scoping
  (e.g., key by pipeline UUID) so multiple contexts coexist without interference.
- **State in component-local `useState`** — If the value must survive a component unmount/remount
  (e.g., collapsing a sidebar and re-opening it), it belongs in a Zustand store.
- **Backend state that is never re-sent on reconnect** — If `seed_session` doesn't emit it,
  a page reload loses it.  Every new `state/*` event must have a corresponding hydration path.

---

## 19. Common gotchas

| Gotcha | Details |
|---|---|
| **`Edge.position` / `Edge.output` arrive as strings** | JSON deserialization can turn `0` into `"0"`. The resolver handles this, but any new code operating on these fields must coerce with `int()` defensively. |
| **`Parameter.__call__` uses `eval`** | `eval(self.dtype)(self.value)` — `dtype` is `"int"`, `"float"`, or `"string"`. For `dtype="eval"` the value is evaluated directly (used for `None`, `True`, `False`). |
| **`emit()` raises in async context** | Calling `emit()` from inside `async def` raises `RuntimeError`. Always use `await aemit(...)` in async code. |
| **msgpack ↔ JSON boundary** | Frontend→backend is msgpack.  Redis stores JSON.  Never mix the two or assume one when the other is in play. |
| **Redis Stream `type='list'`** | Messages with `type: "list"` have their `value` string split by comma in the WS send loop — this is automatic and callers should not pre-split. |
| **`bool` before `int` in type checks** | `isinstance(True, int)` is `True` in Python.  Always check `isinstance(x, bool)` before `isinstance(x, int)` when branching on type. |
| **Dask slice key format** | Multi-output slice entries are keyed `"{src}_{output}"` with `output` as an `int`.  If `output` is a string the key will be wrong — ensure it's coerced. |

---

## Appendix: folder tree

```
dorian/                              ← core library (no server dependencies)
├── api/                             ← HTTP/WS route definitions
│   ├── eda.py                       ← exploratory data analysis endpoint
│   ├── query.py                     ← KB query execution
│   ├── routes/                      ← REST route modules
│   │   ├── file.py                  ← dataset upload / download
│   │   └── session.py               ← session management
│   └── websocket.py                 ← WebSocket dispatcher
├── code/                            ← code generation / LLM integration
│   ├── data/scripts.py
│   ├── llm/                         ← LLM prompt management
│   ├── parsing/                     ← DAG rewrite rule primitives
│   │   ├── rule.py                  ← RewriteRule, Apply, Add, etc.
│   │   ├── rules.py / rules_i.py   ← built-in rule library
│   │   ├── parser.py / debugging.py
│   └── utils.py
├── dag.py                           ← DAG, Operator, Snippet, Parameter, Edge
├── data/science/                    ← KB query wrappers
│   ├── operators.py
│   ├── pipelines.py
│   └── tasks.py
├── evaluation/                      ← evaluation procedure resolution
│   ├── procedures.py
│   └── resolver.py
├── event/                           ← event bus wiring
│   ├── registry.py                  ← subscribe() all handlers here
│   ├── handlers/                    ← all event handler modules
│   │   ├── custom_nodes.py          ← custom operator lifecycle
│   │   ├── data_science_task.py     ← task selection persistence
│   │   ├── datasets.py              ← dataset events
│   │   ├── encoding.py              ← reactive categorical encoding
│   │   ├── evaluation.py            ← eval procedure persistence
│   │   ├── experiment.py            ← experiment store persistence
│   │   ├── lifecycle.py             ← feedback + canvas interaction sink
│   │   ├── listeners.py             ← progress + notification listeners
│   │   ├── pipeline.py              ← pipeline save handler
│   │   ├── pipeline_events.py       ← pipeline debug / data profiling
│   │   ├── ranking_objective.py     ← ranking objective persistence
│   │   ├── recommendations.py       ← recommendation re-ranking
│   │   ├── risk_events.py           ← AI Debugger risk chain
│   │   └── session.py               ← session seeding
│   └── helpers/                     ← shared utilities for handlers
│       └── lifecycle.py             ← with_envelope, _get_session_meta, etc.
├── experiment/                      ← experiment store (similarity search)
│   ├── bktree.py / kdtree.py       ← spatial indexes
│   ├── schema.py                    ← experiment data models
│   ├── similarity.py                ← pipeline similarity
│   └── store.py                     ← main store interface
├── infra/                           ← infrastructure helpers
│   └── keys.py                      ← RedisKeys.* static methods
├── knowledge/                       ← in-tree knowledge base
│   ├── base.py / management.py      ← KB lifecycle
│   ├── queries.py                   ← get_operator_interface, get_method_sequence
│   ├── collection/                  ← web crawlers for KB population
│   └── sources/                     ← KB seed data (operators, tasks, risks, etc.)
├── mcp/                             ← MCP tool server
│   ├── server.py / router.py        ← FastMCP server + tool routing
│   ├── kb_tools.py                  ← KB query tools
│   ├── dag_tools.py                 ← DAG manipulation tools
│   ├── rule_tools.py                ← rewrite rule tools
│   ├── mitigation_tools.py          ← mitigation lookup tools
│   ├── rule_compiler.py             ← compile rules from LLM output
│   ├── extraction.py / prompts.py   ← LLM extraction helpers
│   └── draft_store.py               ← in-memory draft storage
├── models/                          ← Pydantic / dataclass models
│   └── toggles/                     ← UI toggle state models
├── observability/
│   └── sampler.py                   ← CPU/RSS monitor
├── pipeline/                        ← pipeline processing core
│   ├── execution.py                 ← run_pipeline, _parse_pipeline
│   ├── operator_resolver.py         ← Dask graph builder
│   ├── transforms.py                ← DAG rewrite rules (expand_*)
│   ├── mitigation_rewrites.py       ← KB-driven mitigation rewrites
│   ├── parser.py                    ← match/apply/transform
│   ├── generation/                  ← pipeline generation (param sampling)
│   ├── recommendation/              ← recommendation engine
│   └── utils/                       ← feature helpers, debugger
├── ranking/
│   └── objectives.py                ← ranking objective queries
├── tabular/data/profiling/          ← dataset profiling
│   ├── metafeatures.py              ← 40+ metafeature functions
│   ├── profiler.py                  ← profiling orchestrator
│   ├── ml_operator.py / service.py
├── toolbox/                         ← bias detection + fairness
│   ├── checks.py                    ← data bias checks
│   ├── fairness.py                  ← AIF360 fairness metrics
│   ├── mitigations.py               ← AIF360 bias mitigation
│   └── ranking/objectives.py
├── ui/
│   └── tooltips.py                  ← TOOLTIPS dict (14 onboarding entries)
└── types.py                         ← UUID, SupportedType aliases

backend/                             ← FastAPI service layer
├── config.py                        ← dynaconf settings
├── envs.py                          ← Redis, Dask, PostgreSQL clients
├── events.py                        ← Event dataclass, subscribe/emit/aemit
├── queue.py                         ← pipeline task queue (Redis sorted set)
├── cache.py / utils.py / models.py
├── infra/                           ← infrastructure setup
│   ├── dbs/expdb/                   ← Experiment Store seeders
│   │   └── import_trial_configs.py  ← seed pipelines from JSON
│   ├── init.py / provision.py
│   └── podman/main.py
└── repository/                      ← document store (PostgreSQL pg_collections)
    ├── document.py
    └── engines.py
```
