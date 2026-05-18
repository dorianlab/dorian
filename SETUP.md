# Dorian — Setup & Launch Guide

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| **Podman** + `podman-compose` | latest | Container runtime invoked by `scripts/up.sh`. Plain `docker compose` works for per-service launches but `scripts/up.sh` is hard-wired to `podman-compose`. |
| **Python** | ≥ 3.13.7 (`!= 3.14.1`) | Backend runtime; constraint encoded in `pyproject.toml`. |
| **[uv](https://docs.astral.sh/uv/)** | latest | Python package manager (`pip install uv` or see docs). |
| **Rust toolchain** | stable, ≥ 1.75 | Builds the gateway (`engine/gateway`), the engines (`engine/`), and the `dorian_native` PyO3 bindings (`native/python/dorian_native`) consumed by the backend. |
| **Node.js** | ≥ 22 | Required by Next.js 16 + React 19. |
| **pnpm** | ≥ 10 | Frontend package manager (`packageManager` pinned in `frontend/package.json`). |

> PostgreSQL and Redis are not host requirements — `scripts/up.sh` brings
> them up as containers from `docker-compose.yml`. Install them on the
> host only if you intend to run the backend on the host while the
> databases stay containerised (see *Option B* below).

---

## Configuration (do this once before either option)

All runtime settings live in **`config/config.yaml`** — the single source of
truth. Copy the documented template and edit it in place:

```bash
cp config/config.yaml.example config/config.yaml
```

Every required field has a comment describing what to set. If a required
value is missing or empty, the relevant component refuses to start (the
backend raises on import, compose `:?required` checks abort the stack,
NextAuth refuses to sign cookies). Generate fresh secrets with
`openssl rand -hex 32`.

When running through `scripts/up.sh` (the full container stack), the
database hostnames in `config.yaml` should be the compose service names
(`redis`, `postgres`). When running the backend directly on the host
against containerised databases (Option B), set them to `localhost`. Both
presets are documented inline in the template.

---

## Option A — Full container stack via `scripts/up.sh` (recommended)

Everything runs inside containers; the only host dependencies are Podman
and `podman-compose`.

```bash
# Start every service in the foreground
./scripts/up.sh

# Start detached
./scripts/up.sh -d

# Start a subset (useful for partial restarts)
./scripts/up.sh backend frontend
```

`scripts/up.sh` reads `config/config.yaml`, translates it into the env-var
vocabulary that compose substitutes (via `scripts/_config_export.py`), and
then `exec`s `podman-compose up`. The stack includes:

| Service | Role |
|---|---|
| `redis` | event bus, session state, bridge queue |
| `postgres` | Experiment Store, document collections (`pg_collections`) |
| `bootstrap` | one-shot provisioning (Postgres schema, KB-snapshot generation) |
| `engines` | Rust execution / shadow engine |
| `gateway` | Rust HTTP + WebSocket gateway in front of the Python backend |
| `backend` | Python FastAPI app (`main.py` at repo root) |
| `exec-worker` | one or more containers that consume the bridge queue and run pipelines on Dask |
| `frontend` | Next.js 16 application |
| `rl-trainer` *(optional)* | background reinforcement-learning pipeline generator |
| `flaml-seeder` *(optional)* | one-shot FLAML warm-start seeder |

Reachable URLs once the stack reports healthy:

| Surface | URL |
|---|---|
| Frontend (Next.js) | `http://localhost:3000` |
| Rust gateway | `http://localhost:8080` |
| Python backend (FastAPI) | `http://localhost:8000` |
| Backend WebSocket | `ws://localhost:8000/ws` |
| Backend health probe | `http://localhost:8000/healthz` |
| Dask dashboard | printed in backend startup logs |

To stop the stack (preserving data):

```bash
podman-compose down
```

To stop and wipe all data volumes:

```bash
podman-compose down -v
```

---

## Option B — Databases in containers, backend & frontend on the host

This is the workflow during active code development: databases run in
containers (fast restart, consistent schema), the backend and frontend run
on the host with reload.

```bash
# 1. Bring up just the long-lived services
podman-compose up -d redis postgres

# 2. Install Python deps once
uv sync

# 3. Install frontend deps once
cd frontend && pnpm install && cd ..

# 4. Run the FastAPI backend on the host (uvicorn picks up `main:app` from
#    main.py at the repo root --- NOT backend/main.py, which does not exist)
uv run uvicorn main:app --reload

# 5. Run the frontend in a separate terminal
cd frontend && pnpm dev
```

The host backend reads the same `config/config.yaml`. Make sure the
database hostnames in that file are set to `localhost` for this workflow.

---

## Optional services

| Service | Effect | How to enable |
|---|---|---|
| `rl-trainer` | Background RL agent that generates pipeline candidates into the Experiment Store. | `config.yaml`: `generation.enabled: true`. Env override: `RL_GENERATION_ENABLED=0` disables without editing the config. |
| `flaml-seeder` | One-shot warm-start prior seeder; consumed by the RL agent. | Install the `flaml` optional dep group (`uv sync --extra flaml`) and start `flaml-seeder` via compose. |
| `engines` | Rust execution backend used in shadow / dual-mode trials. | `config.yaml`: `execution.engine = python \| rust \| shadow`. |

---

## Verifying the setup

| Check | How |
|-------|-----|
| Backend responds | `curl http://localhost:8000/healthz` |
| Frontend loads | Open `http://localhost:3000` in a browser |
| Gateway alive | `curl http://localhost:8080/` |
| Dask cluster running | Dashboard URL printed in backend startup logs |
| WebSocket connected | Green connection indicator in the frontend UI |
| KB snapshot built | Backend startup logs include a `KBSnapshotGenerated` event (or no `KBSnapshotFailed` warning) |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `ExperimentStore initialization failed` | PostgreSQL may not be running or not yet provisioned — check `podman-compose ps` and `podman-compose logs postgres`. |
| Frontend can't connect to backend | Verify backend is on `:8000`, the gateway is on `:8080`, and the browser isn't blocking WebSocket upgrades. The frontend bundle bakes `NEXT_PUBLIC_*` URLs at build time — re-run `scripts/up.sh` after editing `config.yaml`. |
| Dask workers won't start | Check `dask.cluster.memory_limit` in `config/config.yaml` (default 4 GB). |
| Port already in use | Kill the conflicting process or change the port in `config.yaml` (`urls.*` for host-side ports). |
| `config/config.yaml` is missing | `cp config/config.yaml.example config/config.yaml` and populate every required field. |
| Compose aborts with `… required` | A required field is empty in `config.yaml`. The error message names the field. |
| Sign-in fails | Ensure `oauth.github.client_id` / `client_secret` are set in `config.yaml`, or accept the disabled sign-in screen (the GitHub button hides itself when those fields are empty). |
| `backend.main: No module named 'backend.main'` | The entry point is `main:app` (with `main.py` at the repo root). Use `uv run uvicorn main:app`, not `python -m backend.main`. |
