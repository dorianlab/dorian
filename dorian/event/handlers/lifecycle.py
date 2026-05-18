"""Lightweight event handlers that don't belong to a dedicated domain module.

Handlers registered here:
  - handle_feedback   — persists user feedback with full session context
  - persist_interaction_event — generic sink for canvas / UI interaction events
"""
from backend.events import Event, aemit
from backend.envs import aioredis, expdb
import json


async def handle_feedback(event: Event) -> None:
    """Persist user feedback to Redis with full context for session replay.

    Redis keys
    ----------
    feedback:{uid}:{session}:{requestId}
        JSON blob with all answers + context. One entry per form submission.
        Scoped so submissions from different users/sessions never collide.

    feedback:{uid}:{session}:history
        Append-only list of full feedback blobs for this session.
        Use LRANGE to retrieve the full history in order.
    """
    uid        = event.data.get("uid")
    session    = event.data.get("session")
    request_id = event.data.get("requestId", "")

    answers = dict(event.data.get("answers", {}) or {})

    # Treat omitted optional dataset quality expectations as explicit skips.
    # The UI leaves blank optional fields out of the payload entirely, which
    # would otherwise keep rule-driven quality metrics stuck in "pending".
    dataset_ids = set()
    for key in answers.keys():
        parts = key.split(":")
        if len(parts) >= 3 and parts[0] == "dataset":
            dataset_ids.add(parts[1])
    for did in dataset_ids:
        optional_defaults = {
            f"dataset:{did}:value_occurrence_expectations": [],
            f"dataset:{did}:syntactic_allowed_values": {},
            f"dataset:{did}:semantic_accuracy_rules": [],
            f"dataset:{did}:inaccuracy_columns": [],
            f"dataset:{did}:range_rules": {},
            f"dataset:{did}:sensitive_columns": [],
        }
        for optional_key, default_value in optional_defaults.items():
            if optional_key not in answers:
                answers[optional_key] = default_value

    full_entry = {
        "uid":        uid,
        "session":    session,
        "requestId":  request_id,
        "answers":    answers,
        "pipelineId": event.data.get("pipelineId"),
        "view":       event.data.get("view"),
        "ts":         event.data.get("ts"),
    }

    # Primary key: one entry per submission, never overwritten by another user.
    await aioredis.set(
        f"feedback:{uid}:{session}:{request_id}",
        json.dumps(full_entry),
    )

    # Append to session history list so submissions can be replayed in order.
    await aioredis.rpush(
        f"feedback:{uid}:{session}:history",
        json.dumps(full_entry),
    )

    # Durable backup — feedback is valuable user data.
    try:
        await expdb.feedback.update_one(
            {"uid": uid, "session": session, "requestId": request_id},
            {"$set": full_entry},
            upsert=True,
        )
    except Exception:
        pass  # best-effort; Redis is authoritative at runtime

    # Route each answer to its per-question callback Redis key so that
    # blocking polls in the profiling pipeline (get_features, get_targets,
    # are_fairness_checks_required, etc.) can resume.  The answer keys are
    # the question IDs emitted by the backend via state/queries — e.g.
    # "dataset:{did}:feature_columns", "callback:fairness_checks:{did}".
    for key, value in answers.items():
        await aioredis.set(
            key,
            json.dumps(value) if isinstance(value, (list, dict)) else str(value),
        )

    # Clear answered query IDs from the pending list in session meta so
    # they can be re-emitted if needed (e.g. after resetting a selection).
    from dorian.event.helpers.lifecycle import session_meta_tx
    async with session_meta_tx(session) as meta:
        task_key = f"session:{session}:task_selection"
        if task_key in answers:
            task_value = answers.get(task_key)
            if task_value in ("", "__skip__", None):
                meta["_taskPromptDismissed"] = True
            else:
                meta.pop("_taskPromptDismissed", None)

        eval_key = f"session:{session}:eval_selection"
        if eval_key in answers:
            eval_value = answers.get(eval_key)
            if eval_value in ("", "__skip__", None):
                meta["_evalPromptDismissed"] = True
            else:
                meta.pop("_evalPromptDismissed", None)

        pending = meta.get("_pendingQueryIds")
        if pending:
            answered_ids = set(answers.keys())
            meta["_pendingQueryIds"] = [
                qid for qid in pending if qid not in answered_ids
            ]

        # Sync target column changes to session meta so seed_session can
        # replay state/target on reconnect and dorian.io.state reads it.
        from dorian.infra.keys import RedisKeys
        dataset = meta.get("dataset") or {}
        did = dataset.get("did", "")
        target_key = RedisKeys.dataset_target_columns(did) if did else ""
        if target_key and target_key in answers:
            dataset["target"] = answers[target_key]
            meta["dataset"] = dataset
            # Push updated target to frontend
            from dorian.infra.keys import STREAM_MAXLEN
            stream = RedisKeys.stream(uid, session)
            await aioredis.xadd(stream, {
                "event": "state/target",
                "value": json.dumps(answers[target_key]),
                "type": "json",
            }, maxlen=STREAM_MAXLEN, approximate=True)

    # ── HITL: DQ review answers trigger re-profiling ──────────────────
    dq_review_keys = [
        key for key in answers.keys()
        if key.startswith("dq:") and ":review:" in key
    ]
    if dq_review_keys:
        from dorian.infra.keys import RedisKeys
        raw_meta = await aioredis.get(RedisKeys.session_meta(session))
        _meta = json.loads(raw_meta) if raw_meta else {}
        dataset = _meta.get("dataset") if isinstance(_meta, dict) else None
        did = dataset.get("did") if isinstance(dataset, dict) else None
        fpath = dataset.get("fpath") if isinstance(dataset, dict) else None
        if did and fpath and uid and session:
            await aemit(Event("DataExists", data={
                "uid": uid,
                "session": session,
                "did": did,
                "fpath": fpath,
            }))

    await aemit(Event("FeedbackStored", data=full_entry))

    # If quality expectations were provided after an initial profiling run,
    # trigger a re-profile so pending quality metrics compute immediately.
    quality_input_suffixes = (
        ":value_occurrence_expectations",
        ":syntactic_allowed_values",
        ":semantic_accuracy_rules",
        ":inaccuracy_columns",
        ":range_rules",
        ":quality_threshold_mode",
        ":quality_threshold_override",
        ":feature_columns",
        ":target_columns",
        ":category_column",
        ":sensitive_columns",
        ":balance_target_labels",
        ":compliance_rules",
        ":consistency_label_threshold",
        ":format_schema",
        ":semantic_consistency_rules",
        ":feature_effectiveness_rules",
        ":category_size_threshold",
        ":label_effectiveness_rules",
        ":target_size",
        ":precision_requirements",
        ":relevant_features",
        ":record_relevance_condition",
        ":required_attributes",
    )
    quality_input_keys = [
        key for key in answers.keys()
        if key.startswith("dataset:") and key.endswith(quality_input_suffixes)
    ]
    if quality_input_keys:
        from dorian.infra.keys import RedisKeys
        _raw = await aioredis.get(RedisKeys.session_meta(session))
        _qmeta = json.loads(_raw) if _raw else {}
        dataset = _qmeta.get("dataset") if isinstance(_qmeta, dict) else None
        did = dataset.get("did") if isinstance(dataset, dict) else None
        fpath = dataset.get("fpath") if isinstance(dataset, dict) else None
        if did and fpath and uid and session:
            await aemit(Event("DataExists", data={
                "uid": uid,
                "session": session,
                "did": did,
                "fpath": fpath,
            }))


async def handle_feedback_edit_requested(event: Event) -> None:
    """Re-emit the feature/target selection queries so the user can change answers.

    Reads the dataset profile from session meta to reconstruct the column
    options, then reads current answers from Redis so the frontend can
    pre-populate the form.  Emits ``state/queries`` with a ``defaultValue``
    field on each question.
    """
    uid     = event.data.get("uid")
    session = event.data.get("session")

    from dorian.infra.keys import RedisKeys

    raw = await aioredis.get(RedisKeys.session_meta(session))
    if not raw:
        return

    meta = json.loads(raw)
    dataset = meta.get("dataset") or {}
    did = dataset.get("did", "")
    columns = dataset.get("columns") or []

    if not did or not columns:
        await aemit(Event("FeedbackEditNoDataset", data={
            "uid": uid, "session": session,
        }))
        return

    # Read current answers from Redis
    feat_key = RedisKeys.dataset_feature_columns(did)
    target_key = RedisKeys.dataset_target_columns(did)

    feat_raw = await aioredis.get(feat_key)
    target_raw = await aioredis.get(target_key)

    current_features = json.loads(feat_raw) if feat_raw else []
    current_targets  = json.loads(target_raw) if target_raw else []

    queries = [
        {
            "id": feat_key,
            "type": "multi-select",
            "question": "Select the feature columns of the dataset.",
            "options": columns,
            "defaultValue": current_features,
        },
        {
            "id": target_key,
            "type": "multi-select",
            "question": "Select the target column of the dataset.",
            "options": columns,
            "defaultValue": current_targets,
        },
    ]

    # Delete the callback keys so handle_feedback can overwrite them
    await aioredis.delete(feat_key, target_key)

    from dorian.infra.keys import STREAM_MAXLEN
    stream = RedisKeys.stream(uid, session)
    await aioredis.xadd(stream, {
        "event": "state/queries",
        "value": json.dumps(queries),
        "type": "json",
    }, maxlen=STREAM_MAXLEN, approximate=True)

    await aemit(Event("FeedbackEditEmitted", data={
        "uid": uid, "session": session,
        "questionCount": len(queries),
    }))


async def persist_interaction_event(event: Event) -> None:
    """Append a canvas / UI interaction event to the session's interaction log.

    This is a generic sink — it handles every event type registered under
    "Canvas interaction events" in registry.py.  Stored per-session as an
    append-only list so the full sequence of user actions can be replayed.

    Redis key
    ---------
    interactions:{uid}:{session}
        RPUSH list; each element is a JSON-encoded event dict (event name +
        full payload). Retention is bounded by two mechanisms together:
        * LTRIM to the most recent ``_INTERACTIONS_CAP`` entries after
          every push (default 50_000 — enough for a day of heavy use).
        * EXPIRE set to ``_INTERACTIONS_TTL_S`` seconds (default 7 days)
          on every push, so a stale session's log eventually drops off
          even if the user never returns.
        Both defaults tuneable via the module-level constants below
        (no env vars yet — tune if real-world traffic pushes past
        these).
    """
    uid     = event.data.get("uid")
    session = event.data.get("session")

    entry = {"event": event.type, **event.data}

    key = f"interactions:{uid}:{session}"
    await aioredis.rpush(key, json.dumps(entry))
    # LTRIM [-N:-1] keeps only the most recent N entries; O(N) worst
    # case, amortised O(1) because Redis trims at the front pointer.
    await aioredis.ltrim(key, -_INTERACTIONS_CAP, -1)
    # Refresh TTL on every push so an active session's log stays alive.
    await aioredis.expire(key, _INTERACTIONS_TTL_S)


# Retention bounds for the interaction log — keep them in sync with
# the Go handler's literals in handlers/interactions.go.
_INTERACTIONS_CAP = 50_000          # entries
_INTERACTIONS_TTL_S = 7 * 24 * 3600  # 7 days
