"""
dorian/event/handlers/encoding.py
----------------------------------
Reactive encoding handler: when ``MetafeatureError`` fires due to categorical
data issues, apply ``OrdinalEncoder`` to the session's current pipeline.

Subscribes to ``MetafeatureError`` in the event registry.  Only acts on errors
whose message indicates the root cause is non-numeric (categorical) data.  When
triggered it:

1. Loads the current pipeline from session meta.
2. Applies ``CATEGORICAL_ENCODING_RULE`` with ``force_encoding=True``.
3. Saves the rewritten pipeline to session meta.
4. Pushes ``state/pipeline`` and ``pipeline/rewritten`` stream events so the
   frontend reflects the change immediately.
"""
from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

from backend.events import Event, aemit
from backend.envs import aioredis
from dorian.infra.keys import RedisKeys, STREAM_MAXLEN
from dorian.pipeline.transforms import (
    CATEGORICAL_ENCODING_RULE,
    sync_apply,
)

# Error message fragments that signal a categorical-data root cause.
_CATEGORICAL_PATTERNS = (
    "could not convert string",
    "could not convert",
    "non-numeric",
    "invalid literal for",
    "unable to coerce to series",
)

# Track which sessions have already been rewritten to avoid duplicate rewrites
# when multiple metafeature errors fire from the same profiling run.
_rewritten_sessions: set[str] = set()


def _is_categorical_error(error_msg: str) -> bool:
    """Heuristic: does this metafeature error stem from categorical data?"""
    lower = error_msg.lower()
    return any(pat in lower for pat in _CATEGORICAL_PATTERNS)


async def handle_encoding_on_metafeature_error(event: Event):
    """React to ``MetafeatureError`` by inserting encoding into the pipeline.

    Only fires for errors that appear to be caused by categorical features.
    A per-session guard prevents duplicate rewrites from multiple errors
    in the same profiling run.
    """
    uid = event.data.get("uid", "")
    session = event.data.get("session", "")
    error = event.data.get("error", "")
    metafeature = event.data.get("metafeature", "")

    if not _is_categorical_error(error):
        return

    # Per-session dedup — only rewrite once per profiling run.
    if session in _rewritten_sessions:
        return
    _rewritten_sessions.add(session)

    await aemit(Event("CategoricalEncodingTriggered", {"metafeature": metafeature, "error": error[:80], "session": session}))

    # ── Load current pipeline from session meta ───────────────────────────
    raw = await aioredis.get(RedisKeys.session_meta(session))
    if not raw:
        return

    meta = json.loads(raw)
    pipeline_data = meta.get("pipeline", {})
    if not pipeline_data:
        return
    if isinstance(pipeline_data, str):
        pipeline_data = json.loads(pipeline_data)

    # ── Parse the pipeline ────────────────────────────────────────────────
    from dorian.pipeline.execution import _parse_pipeline

    try:
        dag = _parse_pipeline(pipeline_data)
    except Exception as exc:
        await aemit(Event("EncodingRewriteParseFailed", {"error": str(exc), "session": session}))
        return

    # ── Apply the encoding rule ───────────────────────────────────────────
    rewritten = sync_apply(
        CATEGORICAL_ENCODING_RULE,
        dag,
        {"session": session, "force_encoding": True},
    )

    if rewritten is dag:
        await aemit(Event("EncodingRuleNoChange", {"session": session}))
        return

    # ── Save rewritten pipeline to session meta ───────────────────────────
    rewritten_json = rewritten.to_frontend_dict()
    stream = RedisKeys.stream(uid, session)

    pipeline_history = meta.get("pipelineHistory")
    if isinstance(pipeline_history, str):
        pipeline_history = json.loads(pipeline_history)

    if pipeline_history and isinstance(pipeline_history, dict):
        version_id = str(uuid4())
        new_version = {
            "id": version_id,
            "parentPipelineId": pipeline_history.get("uuid", ""),
            "createdAt": datetime.now().isoformat(),
            "message": "Auto-mitigation: OrdinalEncoder added for categorical features",
            "pipeline": rewritten_json,
            "nodes": rewritten_json.get("nodes", {}),
            "edges": rewritten_json.get("edges", []),
        }
        pipeline_history.setdefault("pipelines", []).append(new_version)
        pipeline_history["headId"] = version_id
        meta["pipeline"] = new_version
        meta["pipelineHistory"] = pipeline_history
    else:
        meta["pipeline"] = rewritten_json

    await aioredis.set(RedisKeys.session_meta(session), json.dumps(meta))

    # ── Push events to frontend ───────────────────────────────────────────
    if pipeline_history and isinstance(pipeline_history, dict):
        await aioredis.xadd(stream, {
            "event": "state/pipeline",
            "value": json.dumps(pipeline_history),
            "type": "json",
        }, maxlen=STREAM_MAXLEN, approximate=True)

    await aioredis.xadd(stream, {
        "event": "pipeline/rewritten",
        "mitigation": "Categorical Encoding",
        "operator": "auto_select",
        "risk": "Categorical Data Error",
        "summary": "OrdinalEncoder added to handle categorical features",
        "pipeline": json.dumps(rewritten_json),
    }, maxlen=STREAM_MAXLEN, approximate=True)

    await aemit(Event("CategoricalEncodingRewriteApplied", {"session": session}))
