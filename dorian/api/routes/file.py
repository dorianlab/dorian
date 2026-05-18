from fastapi import APIRouter, Form, File, UploadFile, HTTPException, Query

import asyncio
import inspect
import traceback
from uuid import uuid4
from datetime import datetime, timezone
import pendulum
import json
from pathlib import Path
import aiofiles

import ast

from fastapi.responses import JSONResponse

from backend.config import config
from backend.events import Event, aemit, aemit_bg, emit
from backend.envs import aioredis, expdb
from backend.cache import cached_read_csv
from backend.rate_limit import http_rate_limit
from dorian.code.parsing.parser import parse as parse_code
from dorian.dag import DAG, Node, Operator, Parameter, Snippet
from dorian.infra.keys import RedisKeys, STREAM_MAXLEN

router = APIRouter()


# ---------------------------------------------------------------------------
# Pipeline extraction from Python scripts
# ---------------------------------------------------------------------------

def _dag_to_frontend_format(dag: DAG) -> dict:
    """Convert a backend DAG to the frontend PipelineDraft shape.

    Frontend expects ``{ uuid, nodes: {id: {type, name, ...}}, edges: [...] }``.

    Output/input ports carry a ``label`` field resolved from the KB
    (``dorian.knowledge.port_names.kb_port_maps``) when available, so
    the canvas renders human-readable names (``X_train`` / ``y_true``)
    on handles that would otherwise be bare numbers.
    """
    from dorian.knowledge.port_names import kb_port_maps

    # Pre-aggregate edge endpoints per node so the KB-name lookup runs
    # once per operator regardless of fan-in / fan-out.
    outputs_by_node: dict[str, set] = {}
    inputs_by_node: dict[str, dict] = {}
    for edge in dag.edges:
        outputs_by_node.setdefault(str(edge.source), set()).add(edge.output)
        inputs_by_node.setdefault(str(edge.destination), {})[edge.position] = True

    port_labels_by_node: dict[str, tuple[dict, dict]] = {}
    for nid, node in dag.nodes.items():
        if isinstance(node, Operator):
            try:
                port_labels_by_node[str(nid)] = kb_port_maps(node.name)
            except Exception:
                port_labels_by_node[str(nid)] = ({}, {})

    nodes: dict[str, dict] = {}
    for nid, node in dag.nodes.items():
        nid_str = str(nid)
        cls = node.__class__.__name__  # Operator | Parameter | Snippet | Node
        entry: dict = {
            "type": cls,
            "name": getattr(node, "name", getattr(node, "text", nid)),
        }
        if isinstance(node, Parameter):
            entry["value"] = node.value
            entry["dtype"] = node.dtype
        elif isinstance(node, Snippet):
            entry["code"] = node.code
            entry["language"] = node.language
        elif isinstance(node, Node):
            # Unreduced AST nodes → promote to Operator so canvas renders them
            entry["type"] = "Operator"
            entry["name"] = node.text or node.type

        in_map, out_map = port_labels_by_node.get(nid_str, ({}, {}))

        out_ports = sorted(outputs_by_node.get(nid_str, set()))
        entry["outputs"] = [
            {"name": str(p), **({"label": out_map[p]} if p in out_map else {})}
            for p in out_ports
        ]

        in_positions = sorted(
            inputs_by_node.get(nid_str, {}).keys(),
            key=lambda x: (isinstance(x, str), x),
        )
        entry["inputs"] = [
            {"name": str(p), **({"label": in_map[p]} if p in in_map else {})}
            for p in in_positions
        ]

        nodes[nid] = entry

    edges = [e.to_dict() for e in dag.edges]
    return {"uuid": str(uuid4()), "nodes": nodes, "edges": edges}


@router.post("/extract")
async def extract_pipeline(
    code: str = Form(...),
    language: str = Form("python"),
    session_id: str = Form(None),
    user_id: str = Form(None),
    filename: str = Form(None),
):
    """Parse Python source code into a pipeline DAG and return it for preview.

    Uses the tree-sitter AST parser + rewrite rules to convert raw Python
    into Operator / Parameter / Snippet nodes with dataflow edges.

    The extraction is persisted to the docstore (full document blob) and Postgres
    (relational index) for regression testing and rule improvement.  The
    response includes ``extractionId`` and ``rulesVersion`` so the frontend
    can link corrections back to this specific extraction.
    """
    from dorian.code.parsing.rules import get_rules, get_rules_version, _compute_rules_hash

    extraction_id = str(uuid4())

    # ── Resolve rule set: prefer user's custom rules from the docstore ────────────
    custom_list_src: str | None = None
    if user_id:
        doc = await expdb.user_extraction_rules.find_one({"uid": user_id})
        if doc and doc.get("content"):
            content = doc["content"]
            # Guard against stale full-file format docs
            if "def get_rules" not in content and "from dorian" not in content:
                custom_list_src = content

    rules = get_rules(custom_list_src=custom_list_src)
    rules_version = _compute_rules_hash(rules) if custom_list_src else get_rules_version()

    try:
        initial_dag, final_dag = await asyncio.to_thread(parse_code, code, language, rules)
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={
                "error": f"Parse error: {e}",
                "trace": traceback.format_exc(),
            },
        )

    # Persist extraction record (fire-and-forget — non-blocking)
    asyncio.create_task(_persist_extraction(
        extraction_id, code, language, rules_version,
        initial_dag, final_dag, session_id, user_id, filename,
    ))

    # Track active extraction in Redis for session context
    if session_id:
        await aioredis.set(RedisKeys.active_extraction(session_id), extraction_id)

    result = _dag_to_frontend_format(final_dag)
    result["extractionId"] = extraction_id
    result["rulesVersion"] = rules_version
    return result


async def _persist_extraction(
    eid: str, code: str, lang: str, rv: str,
    initial: DAG, final: DAG,
    session: str | None, uid: str | None, fname: str | None,
) -> None:
    """Fire-and-forget helper — write extraction to the docstore + Postgres."""
    try:
        from dorian.code.extraction_store import persist_extraction
        await persist_extraction(
            eid, code, lang, rv, initial, final, session, uid, fname,
        )
    except Exception as exc:
        await aemit(Event("ExtractionPersistenceFailed", {"error": repr(exc)}))


@router.post("/extract/propose-rule")
async def propose_rule_endpoint(extraction_id: str = Form(...)):
    """Load a corrected extraction and propose a new rewrite rule.

    Requires the extraction to have a ``correctedDag``.  Returns the
    proposed rule string (or null if the stub cannot propose one yet).
    """
    from dorian.code.extraction_store import get_extraction
    from dorian.code.rule_learning import propose_rule

    record = await get_extraction(extraction_id)
    if not record:
        return JSONResponse(status_code=404, content={"error": "Extraction not found"})

    if not record.get("correctedDag"):
        return JSONResponse(
            status_code=400,
            content={"error": "Extraction has not been corrected yet"},
        )

    rule_str = await propose_rule(
        code=record["code"],
        rules_version=record.get("rulesVersion", "unknown"),
        auto_dag_json=record["autoDag"],
        corrected_dag_json=record["correctedDag"],
    )

    return {
        "extractionId": extraction_id,
        "proposedRule": rule_str,
        "status": "proposed" if rule_str else "no_proposal",
    }


@router.post("/extract/regression-test")
async def regression_test_rules():
    """Run the full extraction regression set against the current rules.

    Returns a list of per-extraction results with pass/fail status and
    diff summaries for failures.  This endpoint is used after rule changes
    to verify backward compatibility.
    """
    from dorian.code.regression import run_regression_test

    try:
        results = await run_regression_test()
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Regression test failed: {e}", "trace": traceback.format_exc()},
        )

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "results": results,
    }


def _extract_rules_list(source: str) -> str:
    """Return only the list literal from get_rules() — the part after 'return'.

    Parses the source with the ``ast`` module, locates the ``Return`` statement
    inside ``get_rules``, then slices the raw source text to that exact span so
    comments and formatting are preserved.  Falls back to the full source if the
    function cannot be found.
    """
    try:
        tree = ast.parse(source)
        lines = source.splitlines()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "get_rules":
                for stmt in node.body:
                    if isinstance(stmt, ast.Return) and stmt.value is not None:
                        start_line = stmt.value.lineno - 1   # 0-indexed
                        start_col  = stmt.value.col_offset
                        end_line   = stmt.value.end_lineno   # 0-indexed inclusive
                        extracted  = lines[start_line:end_line]
                        extracted[0] = extracted[0][start_col:]  # strip "return "
                        return "\n".join(extracted)
    except Exception:
        pass
    return source  # fallback: return full source unchanged


@router.get("/rules")
async def get_extraction_rules(user_id: str = Query(None)):
    """Return the user's saved extraction rules from the docstore.

    Only the ``return [...]`` list literal from ``get_rules()`` is exposed —
    that is the only part the user can meaningfully edit.

    Old-format documents (saved when the full file was stored) are detected by
    the presence of ``def get_rules`` in the content and silently wiped so the
    user sees the clean list-only default on next load.

    Falls back to the default ``rules.py`` for users who have never saved
    custom rules.
    """
    if user_id:
        doc = await expdb.user_extraction_rules.find_one({"uid": user_id})
        if doc:
            content = doc["content"]
            # Detect stale full-file format → wipe and fall through to default
            if "def get_rules" in content or "from dorian" in content:
                await expdb.user_extraction_rules.delete_one({"uid": user_id})
            else:
                return {"content": content, "source": "user"}

    # Default: extract only the return-list from rules.py
    import dorian.code.parsing.rules as rules_module
    source_path = Path(inspect.getfile(rules_module))
    try:
        full_source = source_path.read_text(encoding="utf-8")
        rules_list  = _extract_rules_list(full_source)
        return {"content": rules_list, "source": "default"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not read default rules file: {e}")


@router.post("/rules")
async def update_extraction_rules(
    content: str = Form(...),
    user_id: str = Form(...),
):
    """Persist the user's extraction rules to the docstore.

    Rules are stored per-user and not yet used for extraction — they are
    saved for future use when the rule-application logic is wired up.
    """
    await expdb.user_extraction_rules.update_one(
        {"uid": user_id},
        {"$set": {"content": content, "updatedAt": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return {"status": "ok"}


def _as_text(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8")
    return str(value)


async def _bind_dataset_to_session(
    *,
    did: str,
    session_id: str,
    user_id: str,
    fpath: str,
    doc: dict | None = None,
    fallback_meta: dict | None = None,
) -> bool:
    """Bind an existing dataset id/path to the active session.

    Returns True when the bound dataset already has a profile and can
    immediately emit DataProfiled instead of re-running profiling.
    """
    dataset_meta = {
        "did": did,
        "fpath": fpath,
        "uid": user_id,
        "session": session_id,
        "mime": "text/csv",
    }
    if fallback_meta:
        dataset_meta.update({k: v for k, v in fallback_meta.items() if v is not None})

    profile = doc.get("profile") if isinstance(doc, dict) else None
    if profile:
        dataset_meta["profile"] = profile

    raw = await aioredis.get(RedisKeys.session_meta(session_id))
    if raw:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        meta = json.loads(raw)
    else:
        meta = {
            "uid": user_id,
            "session": session_id,
            "created_at": str(pendulum.now()),
        }
    meta["dataset"] = dataset_meta

    await aioredis.set(RedisKeys.session_meta(session_id), json.dumps(meta))
    await aioredis.set(RedisKeys.dataset_fpath(did), fpath)

    if isinstance(doc, dict):
        columns = doc.get("columns") or {}
        features = doc.get("features") or columns.get("features")
        targets = doc.get("targets") or columns.get("targets")
        if features:
            await aioredis.set(RedisKeys.dataset_feature_columns(did), json.dumps(features))
        if targets:
            await aioredis.set(RedisKeys.dataset_target_columns(did), json.dumps(targets))

    await aioredis.xadd(
        RedisKeys.stream(user_id, session_id),
        {
            "event": "state/dataset",
            "value": json.dumps(dataset_meta),
            "type": "json",
        },
        maxlen=STREAM_MAXLEN,
        approximate=True,
    )
    return bool(profile)


@router.post("/upload")
async def upload_data(
    file: UploadFile = File(...),
    session_id: str = Form(...),
    user_id: str = Form(...),
    description: str | None = Form(None),
    _rl=http_rate_limit("upload"),
):
    print(f"Uploading file: {file.filename} for user: {user_id} in session: {session_id}")

    fpath = Path(config.fs.data) / f"{user_id}/{file.filename}"

    if fpath.exists():
        cursor, _fpath = 0, None
        found_key = None

        while True:
            cursor, keys = await aioredis.scan(cursor=cursor, match="dataset:fpath:*")
            for key in keys:
                stored_path = await aioredis.get(key)
                if stored_path and fpath.absolute().as_posix() == _as_text(stored_path):
                    _fpath = _as_text(stored_path)
                    found_key = key
                    break
            if cursor == 0 or _fpath:
                break

        if _fpath and found_key:
            did = _as_text(found_key).split(":")[-1]
            meta = {
                "did": did,
                "uid": user_id,
                "fpath": fpath.absolute().as_posix(),
                "session": session_id,
            }
            doc = await expdb.datasets.find_one({"_id": did})
            has_profile = await _bind_dataset_to_session(
                did=did,
                session_id=session_id,
                user_id=user_id,
                fpath=fpath.absolute().as_posix(),
                doc=doc,
                fallback_meta=meta,
            )
            await aemit_bg(Event(
                type="DataProfiled" if has_profile else "DataExists",
                data=meta,
            ))
            return {"status": "OK", "did": did, "deduped": True}

    # Read the upload into memory + compute a content hash so we can
    # dedupe against existing datasets before persisting a new one.
    # Small CSVs (<50MB) fit comfortably; large uploads stream to disk.
    # The hash check catches two common cases:
    #   1. The user re-uploads a CSV that matches a public dataset
    #      (e.g. saving credit-g locally and dragging it onto the canvas)
    #      -- we bind their session to the existing public ``did``
    #      instead of creating a parallel private entry.
    #   2. The user re-uploads the same file from a different path
    #      (different filename) -- same did, no duplicate.
    import hashlib as _hashlib
    payload = await file.read()
    content_hash = _hashlib.blake2b(payload, digest_size=16).hexdigest()

    existing = await expdb.datasets.find_one({"contentHash": content_hash})
    if existing is not None:
        existing_owner = existing.get("ownerId")
        existing_public = existing.get("isPublic") is True
        if existing_owner not in (None, user_id) and not existing_public:
            existing = None

    if existing is not None:
        existing_did = str(existing.get("_id"))
        existing_fpath = (
            (existing.get("storage") or {}).get("location", {}).get("path", "")
        )
        if existing_fpath:
            # Resolve relative paths the same way import_existing_dataset does.
            p = Path(existing_fpath)
            if not p.is_absolute():
                try:
                    data_dir = Path(config.fs.data)
                except Exception:
                    data_dir = Path("data")
                p = (data_dir / p).resolve()
            if p.exists():
                has_profile = await _bind_dataset_to_session(
                    did=existing_did,
                    session_id=session_id,
                    user_id=user_id,
                    fpath=p.as_posix(),
                    doc=existing,
                    fallback_meta={
                        "created_at": str(pendulum.now()),
                        "description": (description or "").strip() or None,
                        "content_hash": content_hash,
                    },
                )
                await aemit_bg(Event(type="DataProfiled" if has_profile else "DataExists", data={
                    "did": existing_did,
                    "uid": user_id,
                    "fpath": p.as_posix(),
                    "session": session_id,
                }))
                return {
                    "status": "OK",
                    "did": existing_did,
                    "deduped": True,
                    "matched_name": existing.get("name"),
                }

    did = str(uuid4())
    meta = {
        "did": did,
        "created_at": str(pendulum.now()),
        "uid": user_id,
        "fpath": fpath.absolute().as_posix(),
        "session": session_id,
        "description": (description or "").strip() or None,
        "content_hash": content_hash,
    }

    fpath.parent.mkdir(exist_ok=True, parents=True)
    await aemit(Event(type="WritingData", data=meta))

    async with aiofiles.open(fpath, "wb") as out:
        # payload is already in memory; write it out directly.
        await out.write(payload)

    raw = await aioredis.get(f"session:{session_id}:meta")

    if not raw:
        await aemit(Event("SessionNotFound", data={"uid": user_id, "session": session_id}))
        session = {"uid": user_id, "session": session_id, "created_at": str(pendulum.now())}
    else:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        session = json.loads(raw)

    await aioredis.set(f"session:{session_id}:meta", json.dumps({**session, "dataset": meta}))
    await aioredis.set(f"dataset:fpath:{did}", fpath.absolute().as_posix())
    asyncio.create_task(asyncio.to_thread(cached_read_csv, fpath.absolute().as_posix()))
    await aemit(Event(type="DataWritten", data=meta))
    return {"status": "OK", "did": did}


@router.post("/import")
async def import_pipeline(
    file: UploadFile = File(...),
    session_id: str = Form(...),
    user_id: str = Form(...),
    _rl=http_rate_limit("upload"),
):
    fpath = Path(config.fs.data) / f'{session_id}/{file.filename}'
    meta = {'uid': user_id, 'session': session_id, 'fpath': fpath.absolute().as_posix()}

    # Always overwrite — re-importing the same filename must pick up the
    # new file content, not silently serve a stale cached version.
    existed = fpath.exists()
    fpath.parent.mkdir(exist_ok=True, parents=True)
    async with aiofiles.open(fpath, 'wb') as out:
        while content := await file.read(1024):
            await out.write(content)
    event_type = "PipelineExists" if existed else "PipelineImported"
    await aemit(Event(type=event_type, data=meta))
    return {'status': 'OK'}


# ---------------------------------------------------------------------------
# Dataset discovery & import endpoints
# ---------------------------------------------------------------------------

@router.get("/datasets")
async def list_datasets(
    uid: str = Query(...),
    _rl=http_rate_limit("datasets"),
):
    """List datasets available to this user (own + public).

    Read-time dedup: a user's private upload that shadows a public
    dataset (same ``contentHash``, or a string ``_id`` whose hex
    matches an ObjectId ``_id`` — the pre-``8c8f1fc`` collision
    class) is hidden from the listing. The public entry wins because
    it's the one the rest of the system treats as canonical — the
    private copy is a ghost left behind by the old upload path and
    cleaned up implicitly on the next backfill pass.
    """
    col = expdb.datasets
    cursor = col.find(
        {"$or": [{"ownerId": uid}, {"isPublic": True}]},
        projection={
            "_id": 1, "name": 1, "description": 1, "isPublic": 1,
            "ownerId": 1, "itemCount": 1, "dataType": 1,
            "source.type": 1, "storage.location.path": 1,
            "profile": 1, "features": 1, "targets": 1,
            "createdAt": 1, "contentHash": 1,
        },
    ).sort("updatedAt", -1).limit(100)

    raw: list[dict] = []
    async for doc in cursor:
        doc["id"] = str(doc.pop("_id"))
        raw.append(doc)

    # Pass 1: group by dedup key. Prefer public > private; within a
    # tie, prefer older createdAt (the canonical entry the rest of
    # the system cites). Keys combine contentHash + id-as-hex so we
    # catch both "same file, different doc" and "string/ObjectId id
    # collision" variants of the same bug.
    def _keys_for(d: dict) -> list[str]:
        keys: list[str] = []
        ch = d.get("contentHash")
        if isinstance(ch, str) and ch:
            keys.append(f"hash:{ch}")
        # 24-hex-char id string shadows ObjectId-typed id with the
        # same hex. Normalising both sides to the hex lets the group
        # collapse across the type boundary. ``doc["id"]`` is already
        # ``str(_id)`` — for ObjectId that's the hex, for an errant
        # string _id that's the string itself. Both ends meet here.
        idv = d.get("id") or ""
        if (
            len(idv) == 24
            and all(c in "0123456789abcdef" for c in idv.lower())
        ):
            keys.append(f"idhex:{idv.lower()}")
        return keys

    best_by_key: dict[str, dict] = {}
    for d in raw:
        for k in _keys_for(d):
            cur = best_by_key.get(k)
            if cur is None:
                best_by_key[k] = d
                continue
            # Public wins over private; then older createdAt.
            cur_pub = bool(cur.get("isPublic"))
            new_pub = bool(d.get("isPublic"))
            if new_pub and not cur_pub:
                best_by_key[k] = d
            elif new_pub == cur_pub:
                if str(d.get("createdAt", "")) < str(cur.get("createdAt", "")):
                    best_by_key[k] = d

    # Pass 2: emit each dataset at most once, skipping shadows.
    seen: set[int] = set()
    results: list[dict] = []
    for d in raw:
        shadowed = False
        for k in _keys_for(d):
            winner = best_by_key.get(k)
            if winner is not None and winner is not d:
                shadowed = True
                break
        if shadowed:
            continue
        if id(d) in seen:
            continue
        seen.add(id(d))
        results.append(d)

    # Pass 3: hide private uploads that look like clones of a public
    # dataset by *normalised name*. Catches the "user uploaded
    # creditg_31.csv before the upload-time content-hash dedup
    # existed, openml-loader later inserted a public credit-g
    # entry, both have different contentHashes so passes 1+2 don't
    # group them" case. Normalisation:
    #
    #   * lowercase
    #   * strip trailing dataset extension (.csv, .arff, .tsv, .xlsx)
    #   * strip a trailing ``_NN`` openml-id-style suffix
    #   * keep only [a-z0-9]
    #
    # ``credit-g`` → ``creditg``;
    # ``creditg_31.csv`` → ``creditg_31`` → ``creditg``.
    # The ``_NN`` strip risks false positives on names like
    # ``fashion_2017`` collapsing to ``fashion``; rename such
    # datasets if they overlap with a public canonical (cheaper than
    # a perfect heuristic).
    import re
    def _normalize(name: str) -> str:
        n = (name or "").lower()
        n = re.sub(r"\.(csv|arff|tsv|xlsx?)$", "", n)
        n = re.sub(r"_\d{1,5}$", "", n)
        return re.sub(r"[^a-z0-9]", "", n)

    public_norms = {
        _normalize(d.get("name", ""))
        for d in results
        if d.get("isPublic") and _normalize(d.get("name", ""))
    }
    if public_norms:
        results = [
            d for d in results
            if d.get("isPublic")
            or _normalize(d.get("name", "")) not in public_norms
        ]
    return results


@router.post("/datasets/{did}/import")
async def import_existing_dataset(
    did: str,
    session_id: str = Query(...),
    user_id: str = Query(...),
    _rl=http_rate_limit("datasets"),
):
    """Bind a previously-profiled dataset to the current session."""
    doc = await _find_dataset(did)
    if not doc:
        raise HTTPException(404, "Dataset not found")

    if doc.get("ownerId") != user_id and not doc.get("isPublic"):
        raise HTTPException(403, "Access denied")

    storage = doc.get("storage", {})
    location = storage.get("location", {})
    raw_path = location.get("path", "")
    if not raw_path:
        raise HTTPException(410, "Dataset file no longer available")

    # Resolve relative storage paths against the data directory.
    # The OpenML crawler stores paths like "datasets/<id>/file.csv"
    # relative to its storage root, which in prod is /app/data/.
    #
    # Two previous bugs lived here:
    #   1) ``config.get("data_dir", "data")`` — the top-level key
    #      doesn't exist in the Dynaconf tree, so it silently fell
    #      back to the literal string "data" and produced a relative
    #      path like "data/datasets/<did>/file.csv".
    #   2) We stored ``str(fpath_obj)`` without resolving to absolute,
    #      so the path stored in Redis followed whatever CWD the
    #      backend happened to be running from. When a Dask worker
    #      later tried to read the CSV under a different CWD, it got
    #      ``[Errno 2] No such file or directory``.
    # Fix: read the absolute data root from ``config.fs.data`` and
    # always resolve before storing.
    fpath_obj = Path(raw_path)
    if not fpath_obj.is_absolute():
        try:
            data_dir = Path(config.fs.data)
        except Exception:
            data_dir = Path("data")
        fpath_obj = data_dir / fpath_obj
    fpath_obj = fpath_obj.resolve()
    if not fpath_obj.exists():
        raise HTTPException(410, f"Dataset file no longer available: {fpath_obj}")
    fpath = fpath_obj.as_posix()

    await aioredis.set(RedisKeys.dataset_fpath(did), fpath)

    dataset_meta = {
        "did": did,
        "fpath": fpath,
        "created_at": str(doc.get("createdAt", "")),
        "uid": user_id,
        "mime": "text/csv",
    }

    raw = await aioredis.get(RedisKeys.session_meta(session_id))
    meta = json.loads(raw) if raw else {"uid": user_id, "session": session_id}
    meta["dataset"] = dataset_meta

    profile = doc.get("profile")
    # Features/targets may be top-level or nested under "columns"
    columns = doc.get("columns") or {}
    features = doc.get("features") or columns.get("features")
    targets = doc.get("targets") or columns.get("targets")

    if profile:
        dataset_meta["profile"] = profile
        meta["dataset"] = dataset_meta

    await aioredis.set(RedisKeys.session_meta(session_id), json.dumps(meta))

    if features:
        await aioredis.set(RedisKeys.dataset_feature_columns(did), json.dumps(features))
    if targets:
        await aioredis.set(RedisKeys.dataset_target_columns(did), json.dumps(targets))

    # Push the dataset state to the SPA's event stream FIRST so the
    # toast + sidebar update fire on the import response, not on
    # handler completion.
    stream = RedisKeys.stream(user_id, session_id)
    await aioredis.xadd(stream, {
        "event": "state/dataset",
        "value": json.dumps(dataset_meta),
        "type": "json",
    }, maxlen=STREAM_MAXLEN, approximate=True)

    # Background-emit ``DataProfiled``/``DataExists`` so the import
    # response returns ASAP. ``aemit`` awaits every subscribed handler;
    # the ``DataProfiled`` chain (auto_task.rs CSV inference +
    # attempt_recommendations scoring all 500+ trial pipelines) is
    # easily 5–10 s on a populated catalogue, which the user sees as a
    # hang between clicking "Import" and the toast appearing. The
    # handlers fire-and-forget here — task auto-detection and
    # recommendations land asynchronously over the WS stream as they
    # complete, so the SPA still gets the picker badges, just slightly
    # delayed instead of blocking the click→toast latency.
    event_name = "DataProfiled" if profile else "DataExists"
    await aemit_bg(Event(type=event_name, data={
        "uid": user_id, "session": session_id, "did": did, "fpath": fpath,
    }))

    return {"did": did, "name": doc.get("name"), "fpath": fpath}


@router.get("/extraction/rules")
async def list_extraction_rules(
    uid: str = Query(None),
    limit: int = Query(20, le=100),
    _rl=http_rate_limit("extraction_rules"),
):
    """Return saved extraction rule versions, newest first. Optionally filter by uid."""
    query = {"uid": uid} if uid else {}
    cursor = expdb.extraction_rule_versions.find(
        query,
        projection={"content": 0},
    ).sort("createdAt", -1).limit(limit)

    results = []
    async for doc in cursor:
        doc["id"] = str(doc.pop("_id"))
        if "createdAt" in doc:
            doc["createdAt"] = _iso(doc["createdAt"])
        results.append(doc)
    return results


@router.get("/extraction/rules/{rule_id}")
async def get_extraction_rule(
    rule_id: str,
    _rl=http_rate_limit("extraction_rules"),
):
    """Return a single rule version including its full source content."""
    if not rule_id:
        raise HTTPException(400, "Invalid rule id")

    doc = await expdb.extraction_rule_versions.find_one({"_id": rule_id})
    if not doc:
        raise HTTPException(404, "Rule version not found")

    doc["id"] = str(doc.pop("_id"))
    if "createdAt" in doc:
        doc["createdAt"] = _iso(doc["createdAt"])
    return doc


def _iso(value) -> str | None:
    """Normalise a timestamp field to ISO 8601 text for JSON responses.

    The Postgres document-store facade stores datetimes as ISO strings
    (``json.dumps(default=str)``) so reads return strings; legacy callers
    that stored bare ``datetime`` objects would fall into the
    ``isoformat()`` branch. Handles both transparently.
    """
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


async def _find_dataset(did: str):
    """Find a dataset document by id (TEXT in the Postgres document store)."""
    return await expdb.datasets.find_one({"_id": did})


@router.get("/datasets/{did}")
async def get_dataset_detail(
    did: str,
    _rl=http_rate_limit("datasets"),
):
    """Return full dataset document including profile and column metadata."""
    doc = await _find_dataset(did)
    if not doc:
        raise HTTPException(404, "Dataset not found")

    doc["id"] = str(doc.pop("_id"))
    if "createdAt" in doc:
        doc["createdAt"] = _iso(doc["createdAt"])
    if "updatedAt" in doc:
        doc["updatedAt"] = _iso(doc["updatedAt"])
    return doc


@router.get("/datasets/{did}/leaderboard")
async def get_dataset_leaderboard(
    did: str,
    metric: str = Query("accuracy"),
    limit: int = Query(50, le=200),
    _rl=http_rate_limit("datasets"),
):
    """Return ranked pipelines evaluated on this dataset, sorted by metric.

    Queries the Postgres evaluations table for the given dataset and metric,
    then enriches each entry with pipeline operator names from the docstore.
    """
    try:
        from backend.envs import get_pg_pool

        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            # Single query: deduplicate by pipeline (keep latest eval),
            # rank by metric descending.  Go port: same SQL, no Python needed.
            rows = await conn.fetch(
                """
                SELECT ranked.* FROM (
                    SELECT DISTINCT ON (e.pipeline_id)
                        e.pipeline_id,
                        e.metric_value,
                        e.run_id,
                        e.created_at,
                        p.operators,
                        p.task,
                        p.provenance
                    FROM evaluations e
                    JOIN pipelines p ON p.id = e.pipeline_id
                    WHERE e.dataset_id = $1 AND e.metric_name = $2
                    ORDER BY e.pipeline_id, e.created_at DESC
                ) ranked
                ORDER BY ranked.metric_value DESC
                LIMIT $3
                """,
                did,
                metric,
                limit,
            )

        entries = []
        for i, row in enumerate(rows, 1):
            entry = dict(row)
            entry["rank"] = i
            if entry.get("created_at"):
                entry["created_at"] = entry["created_at"].isoformat()
            entries.append(entry)

        return {"dataset_id": did, "metric": metric, "entries": entries}

    except Exception:
        import traceback
        await aemit(Event("LeaderboardQueryFailed", {
            "did": did, "error": traceback.format_exc(),
        }))
        return {"dataset_id": did, "metric": metric, "entries": []}


@router.get("/datasets/{did}/metrics")
async def get_dataset_available_metrics(
    did: str,
    _rl=http_rate_limit("datasets"),
):
    """Return the list of distinct metric names evaluated for this dataset."""
    try:
        from backend.envs import get_pg_pool

        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT metric_name, COUNT(*) as eval_count
                FROM evaluations
                WHERE dataset_id = $1
                GROUP BY metric_name
                ORDER BY eval_count DESC
                """,
                did,
            )

        return [{"name": row["metric_name"], "count": row["eval_count"]} for row in rows]

    except Exception:
        return []


@router.patch("/datasets/{did}/visibility")
async def toggle_dataset_visibility(
    did: str,
    user_id: str = Query(...),
    is_public: bool = Query(...),
    _rl=http_rate_limit("datasets"),
):
    """Toggle the public/private flag on a dataset (owner only)."""
    for key in _dataset_id_variants(did):
        result = await expdb.datasets.update_one(
            {"_id": key, "ownerId": user_id},
            {"$set": {"isPublic": is_public, "updatedAt": datetime.now(timezone.utc)}},
        )
        if result.matched_count > 0:
            return {"did": did, "isPublic": is_public}
    raise HTTPException(404, "Dataset not found or not owned by user")


@router.patch("/datasets/{did}/description")
async def update_dataset_description(
    did: str,
    user_id: str = Form(...),
    description: str = Form(""),
    _rl=http_rate_limit("datasets"),
):
    """Update the human-authored description on a dataset (owner only).

    An empty string clears the description.
    """
    cleaned = description.strip() or None
    for key in _dataset_id_variants(did):
        result = await expdb.datasets.update_one(
            {"_id": key, "ownerId": user_id},
            {"$set": {"description": cleaned, "updatedAt": datetime.now(timezone.utc)}},
        )
        if result.matched_count > 0:
            return {"did": did, "description": cleaned}
    raise HTTPException(404, "Dataset not found or not owned by user")
