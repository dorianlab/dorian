# Contributing to Dorian

Thank you for your interest in Dorian!
We are still in active development, but welcome bug reports and carefully scoped contributions.

---

## Developer Certificate of Origin

All external contributions must be signed-off to indicate you have read and agreed to the
[Developer Certificate of Origin](https://developercertificate.org/) (DCO).

Sign off every commit with `-s`:

```bash
git commit -s -m "fix: describe your change"

# Or create a git alias for convenience:
git config alias.cos "commit -s"
git cos -m "fix: describe your change"
```

**DCO violations** (fake name, unauthorized submission) may result in a ban from the organisation.

<details>
<summary><b>Full DCO text</b></summary>

```
Developer Certificate of Origin — Version 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I have the right
    to submit it under the open source license indicated in the file; or

(b) The contribution is based upon previous work that, to the best of my knowledge,
    is covered under an appropriate open source license and I have the right under
    that license to submit that work with modifications, whether created in whole or
    in part by me, under the same open source license; or

(c) The contribution was provided directly to me by some other person who certified
    (a), (b) or (c) and I have not modified it.

(d) I understand and agree that this project and the contribution are public and
    that a record of the contribution (including all personal information I submit
    with it, including my sign-off) is maintained indefinitely.
```

</details>

---

## Setting up the development environment

```bash
# Backend
pyenv install 3.13
pyenv local 3.13
uv sync

# Frontend
cd frontend
pnpm install
```

Run the full test suite before opening a merge request:

```bash
uv run pytest --cov=dorian --cov=backend --cov-report=term-missing
```

---

## Code conventions

### Python (backend)

- **Redis keys** — always use `RedisKeys.*` helpers from `dorian/infra/keys.py`.
  Never write raw f-strings like `f"session:{sid}:meta"` outside that file.
- **Observability** — use `emit(Event(...))` (sync) or `await aemit(Event(...))` (async)
  from `backend.events` instead of `logging`. The event bus is the single observability sink.
  Never use a bare `except Exception: pass` — emit an error event or let the bus log it.
- **DAG rewrite rules** — add new `RewriteRule` + `Apply` pairs in `dorian/pipeline/transforms.py`.
  The same rule object must work in both `sync_apply` (background thread) and `await apply(…)` (async).
- **TypedDicts for meta dicts** — define a `TypedDict` for every `meta` dict passed through
  rewrite rules. Use `DatasetMeta` / `SessionMeta` from `transforms.py` as a model.
- **Tests** — every new Python module should have a corresponding test file in `tests/`.
  Stub `backend.*` dependencies using `sys.modules` insertion (see `tests/test_execution_engine.py`).

### TypeScript (frontend)

- **Store imports** — always import from the barrel (`@/store`) not from individual store files.
- **New stores** — add a new `use<Name>Store` in `frontend/store/<name>.ts` and export it from
  `frontend/store/index.ts`.
- **New node components** — add the component to `frontend/components/pipeline/composition/Nodes/`
  and re-export it from the barrel `index.ts` in that directory.
- **WebSocket events** — add a single entry to the `eventHandlers` map in
  `frontend/hooks/usePipelineSocket.ts`. Do not expand the `switch` — the map pattern is intentional.
- **Handle logic** — use `useNodeHandles` from `frontend/hooks/useNodeHandles.ts` instead of
  duplicating the deduplication logic in each node component.
- **Toggles vs. enableX** — use `setToggle(key, value)` from `useUIStore` for permission flags.
  Do not add new `enableX` booleans to `UIState`.

---

## How to add a new WebSocket server → client event

1. **Backend**: In the relevant event handler (e.g. `dorian/event/handlers/pipeline.py`),
   emit to the Redis stream:

   ```python
   await aioredis.xadd(
       RedisKeys.stream(uid, session),
       {"event": "my/new/event", "value": json.dumps(payload), "type": "json"},
   )
   ```

2. **Frontend**: In `frontend/hooks/usePipelineSocket.ts`, add a handler:

   ```typescript
   "my/new/event": ({ value }) => {
     // value is whatever you put in the "value" field above
     myStore.setFoo(value);
   },
   ```

That's all — no switch statement to extend.

---

## How to add a new DAG rewrite rule

1. Write the transformation function `_my_transform(dag: DAG, mapping: dict, meta: dict) -> DAG`.
2. Define a `MY_RULE = RewriteRule(pattern=..., description=..., transformations=[Apply(f=_my_transform)])`.
3. Add a public entry point `my_transform(pipeline: DAG, session: str) -> DAG` that calls
   `sync_apply(MY_RULE, pipeline, meta)`.
4. Call `my_transform(pipeline, session)` in `run_pipeline()` inside `dorian/pipeline/execution.py`.

Both `sync_apply` (background thread) and `await apply(rule, dag, meta)` (async event handler)
will work with the same rule — no duplication needed.

---

## Merge request checklist

- [ ] `uv run pytest` passes with no new failures
- [ ] New Python modules have tests in `tests/`
- [ ] No raw Redis key f-strings outside `dorian/infra/keys.py`
- [ ] No bare `except Exception: pass` blocks
- [ ] New stores / node components are added to their barrel exports
- [ ] Commit signed off with `-s`
