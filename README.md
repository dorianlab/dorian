# Dorian

**Dorian** is a human-in-the-loop framework for the *assisted design of
end-to-end data science pipelines*. Given a user's dataset and a data
science task (e.g., supervised classification, dimensionality reduction,
graph embedding), Dorian retrieves relevant previously-executed
pipelines from its Experiment Store, ranks them through a
multi-objective sort tailored to user-defined ranking objectives, and
surfaces a short list of candidates that the user can execute, modify,
inspect, or compare. Unlike fully-automated AutoML, Dorian supports
arbitrary evaluation processes (including those with no quantifiable
loss function) and extends to new operators by ingesting executed
pipelines rather than by changing system code; unlike rule-based
Intelligent Discovery Assistants, it replaces hand-curated expert
knowledge with a continuously growing repository of recorded
experiments.

The framework is described in the VLDB Journal article *"Assisted
design of data science pipelines"* (Redyuk, Kaoudi, Schelter, Markl,
2024) and is the reference implementation cited in the PhD thesis
*"Assisted Design of End-to-End Data Science Pipelines"* (Redyuk, TU
Berlin, 2026).

---

## Citing this work

```bibtex
@article{Redyuk2024,
  title   = {Assisted design of data science pipelines},
  author  = {Redyuk, Sergey and Kaoudi, Zoi and Schelter, Sebastian and Markl, Volker},
  journal = {The VLDB Journal},
  year    = 2024,
  doi     = {10.1007/s00778-024-00835-2},
  url     = {https://link.springer.com/article/10.1007/s00778-024-00835-2}
}
```

A frozen snapshot of this codebase is archived on Zenodo; see
[`CITATION.cff`](./CITATION.cff) for the version and concept DOIs.

---

## What Dorian does and does not do

- **Does:** retrieves, ranks, and recommends end-to-end DS pipelines
  for a user-defined task on user-supplied data; executes user-selected
  pipelines on a Dask cluster; tracks experiment history in a queryable
  store; supports arbitrary user-defined ranking objectives and
  evaluation procedures via a small Python interface; extracts pipeline
  graphs from third-party source code via a tree-sitter–based parser;
  applies KB-driven mitigation rewrites to flagged pipelines before
  execution.
- **Does not:** generate pipelines from scratch through exhaustive
  search (AutoML's design point); replace the user's judgement on
  domain-specific design decisions; require a quantifiable loss
  function; require its operator catalogue to be closed-world.

The three named components — *Recommendation Engine*, *Experiment Store*,
and *DS Pipeline Extractor* — correspond to the components introduced in
the VLDB Journal article.

---

## Architecture at a glance

```
┌──────────────────────────────────────────────────────────┐
│  Next.js frontend (TypeScript / React Flow / Zustand)    │
│  - usePipelineStore    — design-time graph state         │
│  - usePipelineRunStore — execution-time run state        │
│  - usePipelineSocket   — msgpack WebSocket dispatcher    │
└───────────────────────┬──────────────────────────────────┘
                        │  WebSocket (msgpack binary)
┌───────────────────────▼──────────────────────────────────┐
│  Rust gateway        (engine/gateway: catalog, session,  │
│                       KB-snapshot reads, WS proxy)       │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│  Python backend (FastAPI; main.py at repo root)          │
│  - Recommendation Engine (multi-objective sort over      │
│                           retrieved candidates)          │
│  - Experiment Store      (PostgreSQL: pipelines,         │
│                           datasets, evaluations, runs)   │
│  - DS Pipeline Extractor (tree-sitter AST + KB-driven    │
│                           rewrite rules; native Rust     │
│                           handler via `dorian_native`)   │
│  - Event bus             (emit / aemit; Redis streams)   │
│  - exec-workers          (separate containers; consume   │
│                           the Redis bridge queue and run │
│                           pipelines on Dask)             │
└──────────────────────────────────────────────────────────┘
        │                                       │
        ▼                                       ▼
   PostgreSQL                                Redis
   (Experiment Store +                       (event bus, session
    document collections via                  state, bridge queue,
    `pg_collections`)                         pub/sub streams)
```

The Rust gateway, the exec-workers, the optional RL pipeline generator
and the FLAML warm-start seeder are post-paper additions; the thesis's
*Cold-Start Problem and the three-lateral background generation engine*
section documents them.

Key layers:

| Layer | Path | Purpose |
|---|---|---|
| DAG model | `dorian/dag.py` | Node, Edge, Operator, Parameter, Snippet dataclasses |
| Rewrite rules | `dorian/pipeline/transforms.py` | Pre-execution DAG rewrites (dataset expansion, compound operator expansion, KB-driven mitigations) |
| Execution engine | `dorian/pipeline/execution.py` | Dask graph building, node instrumentation, run lifecycle |
| Operator resolver | `dorian/pipeline/operator_resolver.py` | Maps Operator names to Python callables |
| Knowledge base | `dorian/knowledge/` | Operator interfaces, method sequences, rewrite rules (loaded from in-tree KB snapshot) |
| Redis key namespace | `dorian/infra/keys.py` | Central factory for all Redis key strings |
| Event handlers | `dorian/event/handlers/` | One file per domain (pipeline, recommendations, …) |
| WebSocket hook | `frontend/hooks/usePipelineSocket.ts` | Client-side msgpack dispatcher |
| Store layer | `frontend/store/` | Zustand stores (pipeline graph, run state, UI, session, …) |

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | ≥ 3.13.7 (and `!= 3.14.1`) | Constraint encoded in `pyproject.toml`; managed with `pyenv` |
| `uv` | latest | Python package manager — replaces `poetry` / `pip` for every command |
| Rust toolchain | stable, ≥ 1.75 | Builds the gateway (`engine/gateway`), the engines (`engine/`), and the `dorian_native` PyO3 bindings (`native/python/dorian_native`) consumed by the backend |
| Node.js | ≥ 22 | Required by Next.js 16 + React 19 |
| pnpm | ≥ 10 | Frontend package manager (`packageManager` pinned in `frontend/package.json`) |
| Podman + `podman-compose` | latest | Container runtime invoked by `scripts/up.sh`. Plain Docker compose is interchangeable for the per-service `docker compose up <svc>` workflow but `scripts/up.sh` is hard-wired to `podman-compose` |
| PostgreSQL | ≥ 15 | Experiment Store (pipelines, datasets, evaluations) + document collections via `pg_collections`. Provisioned automatically when launching via compose |
| Redis | ≥ 7 | Session state, event bus, bridge queue, pub/sub streams. Provisioned automatically when launching via compose |

---

## Installation

```bash
# 1. Python (one-time)
pyenv install 3.13          # any 3.13.x except 3.14.1
pyenv local 3.13            # or: pyenv global 3.13

# 2. Python dependencies
uv sync                     # installs every required wheel, including the
                            # Rust-backed `dorian_native` from native/

# 3. Frontend dependencies
cd frontend && pnpm install && cd ..

# 4. Configuration --- single source of truth
cp config/config.yaml.example config/config.yaml
# then edit config/config.yaml in place; every required field is documented inline
```

---

## Running the application

There is one entry-point script for the full stack and one well-defined
local-development workflow. Each is described below; pick whichever
matches what you need.

### Full stack via `scripts/up.sh` (recommended)

```bash
./scripts/up.sh                       # all services, foreground
./scripts/up.sh -d                    # detached
./scripts/up.sh backend frontend      # selected services only
```

`scripts/up.sh` reads `config/config.yaml`, translates it into the
flat env-var vocabulary that compose substitutes (via
`scripts/_config_export.py`), and then `exec`s `podman-compose up`. The
full stack brings up Redis, PostgreSQL, a one-shot `bootstrap` step,
the Rust `engines`, the Rust `gateway`, the Python `backend`
(FastAPI), one or more `exec-worker` containers for pipeline
execution, the optional `rl-trainer` and `flaml-seeder` background
services, and the Next.js `frontend`. Reachable URLs on first
successful start:

| Surface | URL |
|---|---|
| Frontend (Next.js) | `http://localhost:3000` |
| Rust gateway | `http://localhost:8080` |
| Python backend (FastAPI) | `http://localhost:8000` |
| Backend WebSocket | `ws://localhost:8000/ws` |
| Backend health probe | `http://localhost:8000/healthz` |

The `bootstrap` container performs PostgreSQL provisioning and any
required one-shot seeders (auto-sklearn trial configs, KB snapshot
generation) the first time it runs; subsequent runs are no-ops thanks
to the seeders' presence checks.

### Local-development workflow (databases in containers, backend + frontend on the host)

For fast-reload editing:

```bash
# 1. Bring up just the long-lived services in containers
podman-compose up -d redis postgres

# 2. Run the FastAPI backend on the host with reload
uv run uvicorn main:app --reload
#                  └────┘
#                  the FastAPI app lives in main.py at the repo root
#                  (NOT backend/main.py --- that path does not exist)

# 3. Run the frontend in a separate terminal
cd frontend && pnpm dev
```

The host-side backend reads the same `config/config.yaml`; set the
database hostnames in that file to `localhost` for this workflow (the
template documents the two presets).

### Optional services

| Service | Effect | Configuration |
|---|---|---|
| `rl-trainer` | Background RL agent generating pipeline candidates into the Experiment Store | `config.yaml`: `generation.enabled: true`. Also honoured via env: `RL_GENERATION_ENABLED=0` disables at start without editing config. |
| `flaml-seeder` | One-shot warm-start prior seeder | Enabled when the `flaml` optional dependency group is installed (`uv sync --extra flaml`) |
| `engines` | Rust execution backend used in shadow / dual-mode trials | Toggle via `config.yaml`: `execution.engine = python \| rust \| shadow` |

---

## Running tests

### Backend

```bash
# Run all tests
uv run pytest

# Run with coverage report
uv run pytest --cov=dorian --cov=backend --cov-report=term-missing

# Run a specific test file
uv run pytest tests/test_execution_engine.py -v
```

Test files live in `tests/`.
Each test file stubs out `backend.*` modules so the suite runs without live Redis or Dask.

**Current coverage areas**

| Module | Test file |
|---|---|
| Pipeline execution + operator resolver | `tests/test_execution_engine.py` |
| Data pathway hardening (vault, feedback, datasets) | `tests/test_data_pathways.py` |
| Atomic session meta transactions | `tests/test_session_meta_tx.py` |
| Event handler registry completeness | `tests/test_event_registry.py` |
| Observability collector + result reaper | `tests/test_observability.py` |
| Data quality metrics + mitigations | `tests/test_tabular_quality_*.py` |
| Quality decision functions | `tests/test_quality_decision_functions.py` |
| Backend cache | `tests/test_cache.py` |

### Frontend

The frontend does not yet have a dedicated test runner.
To add Vitest:

```bash
cd frontend
pnpm add -D vitest @vitejs/plugin-react jsdom @testing-library/react
```

Then create `vitest.config.ts` and add `"test": "vitest"` to `frontend/package.json`.

---

## Configuration & secrets

Dorian reads runtime configuration from a single file:
`config/config.yaml`, the single source of truth. The backend (via
dynaconf), the frontend bundle, and `scripts/up.sh` all derive their
values from this one file. There are no parallel `.env` files, secret
overlays, or per-environment shadow templates.

```bash
# Create your local config from the template
cp config/config.yaml.example config/config.yaml
# Edit config/config.yaml --- every required field is documented inline
```

`config/config.yaml.example` is the schema documentation: it lists every
configurable value with comments explaining what to set.
`config/config.yaml` itself is gitignored, so your secrets stay local.

If a required value is missing or empty, the relevant component refuses
to start --- the backend raises on import, the compose file's
`:?required` checks abort the stack, NextAuth refuses to sign cookies.
There are no fallback defaults for secret-bearing fields. Generate
fresh secrets with `openssl rand -hex 32`; the same HMAC value must
appear in `hmac.secret` so the backend and the frontend bundle sign and
verify against a common key.

---

## Project structure (highlights)

```
main.py                             # FastAPI app entry-point (uvicorn main:app)

dorian/                             # Core Python package (the framework itself)
├── dag.py                          # Core DAG node types
├── infra/
│   └── keys.py                     # Central Redis key namespace (RedisKeys)
├── pipeline/
│   ├── execution.py                # run_pipeline(), node instrumentation
│   ├── operator_resolver.py        # Operator name → Python callable
│   ├── transforms.py               # DAG rewrite rules (dataset, compound operators, mitigations)
│   ├── rule.py                     # RewriteRule / Apply types
│   └── parser.py                   # async match() / apply() / transform()
├── knowledge/
│   └── base.py                     # KB lookups (operator interface, method sequence, rewrite rules)
├── event/
│   └── handlers/                   # Domain event handlers (pipeline, recommendations, …)
└── models/
    └── execution.py                # PipelineExecution, NodeState, status enums

backend/                            # FastAPI app glue (config, envs, middleware, routes)
├── config.py                       # Dynaconf single-source loader (config/config.yaml)
├── envs.py                         # Shared Redis/Postgres/Dask singletons
├── hmac_auth.py                    # HMAC-SHA256 request-signing middleware
└── infra/dbs/expdb/
    └── import_trial_configs.py     # Seed Experiment Store with auto-sklearn trial configs

engine/                             # Rust services
├── gateway/                        # WS proxy + catalog/session HTTP routes (KB-snapshot reads)
├── extractor/                      # Native pipeline-extractor implementation
├── automl/                         # Rust AutoML wrappers
└── backend/                        # Rust execution backend (shadow / dual-mode)

native/                             # PyO3 bindings exposing the Rust extractor as `dorian_native`
└── python/dorian_native/

rl/                                 # Reinforcement-learning pipeline generator
├── env/                            # Pipeline-design environment
├── policy/                         # Policies trained on the Experiment Store
├── train/                          # Training loops
└── priors/                         # FLAML warm-start seeder

frontend/
├── store/
│   ├── index.ts                    # Barrel export — import all stores from here
│   ├── pipeline.ts                 # Design-time graph state
│   ├── pipeline-run.ts             # Execution-time run state (separate for perf)
│   ├── ui.ts                       # UI toggles, selected task/eval/objectives
│   └── session.ts                  # User session management
├── hooks/
│   ├── usePipelineSocket.ts        # WebSocket dispatcher (event handler map)
│   └── useNodeHandles.ts           # Shared handle deduplication for node components
└── components/pipeline/composition/
    ├── Nodes/
    │   ├── index.ts                # Barrel export for all node components
    │   ├── operator.tsx
    │   ├── parameter.tsx
    │   ├── snippet.tsx
    │   └── visualizer.tsx
    └── canvas/
        └── index.tsx               # React Flow canvas
```

---

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the full guide.

Quick checklist:
- All contributions require a Developer Certificate of Origin sign-off (`git commit -s`)
- Python: follow the existing style; add tests for new modules in `tests/`
- TypeScript: follow existing Zustand/React patterns; update barrel exports when adding files
- Do not add Redis key strings as raw f-strings — use `RedisKeys.*` helpers
- New WebSocket event types: add a single entry to `eventHandlers` in `usePipelineSocket.ts`

---

## License

Apache License, Version 2.0. See [LICENSE](./LICENSE).
