import json
import time
import traceback

from backend.envs import aioredis
from backend.events import Event, aemit

from dorian.infra.keys import RedisKeys, STREAM_MAXLEN
from dorian.pipeline.recommendation import suggest, suggest_with_status, record_interaction
from dorian.data.science.tasks import Tasks
from dorian.evaluation.procedures import EvaluationProcedures
from dorian.event.helpers.lifecycle import set_pipeline_default_ranking


# Per-session debounce window for ``attempt_recommendations``. The
# session-init burst typically fires DataProfiled (or DataExists)
# immediately followed by DataScienceTaskSelected (from the rust
# auto-task handler 100–200 ms later) and EvaluationProcedureCommitted
# once the user picks an eval. Each event triggers a full re-rank
# against unchanged context. Coalesce them so the user sees one
# recommendations push instead of three serialised ones.
#
# Window covers the typical handler-chain spread without swallowing
# legitimate user-driven re-rank requests (which arrive seconds apart
# at the soonest — the user has to click).
_DEBOUNCE_WINDOW_S = 1.0
_LAST_RUN: dict[str, float] = {}


def _serialize_suggestions(suggestions: list) -> str:
    """Serialise pipeline docs from the docstore, converting ObjectId to plain strings."""
    clean = []
    for doc in suggestions:
        d = dict(doc)
        if "_id" in d:
            d["_id"] = str(d["_id"])
        clean.append(d)
    # default=str handles any remaining non-serializable types (e.g. nested ObjectId)
    return json.dumps(clean, default=str)


# ---------------------------------------------------------------------------
# Shared logic — record interaction, re-suggest, push to stream
# ---------------------------------------------------------------------------
async def _handle_interaction(event: Event, kind: str) -> None:
    """Re-rank recommendations after a user interaction.

    The redis-I/O slice (``record_interaction``, ``selected``→meta save
    + ``RecommendationPipelineSaved`` emit) moved to rust
    (``engine/backend/src/handlers/recommendation.rs``). This handler
    now owns only the heavy ``suggest_with_status`` re-rank — keeps
    running in python until the recommendation engine itself ports.
    """
    uid = event.data.get("uid")
    session = event.data.get("session")
    if not uid or not session:
        return

    # re-rank and push fresh recommendations + objective status to the frontend
    suggestions, status = await suggest_with_status(uid, session)
    stream = RedisKeys.stream(uid, session)
    await aioredis.xadd(
        stream,
        {"event": "state/pipelines/recommendation", "value": _serialize_suggestions(suggestions), "type": "json"},
        maxlen=STREAM_MAXLEN, approximate=True,
    )
    await aioredis.xadd(
        stream,
        {"event": "state/objectives/status", "value": json.dumps(status), "type": "json"},
        maxlen=STREAM_MAXLEN, approximate=True,
    )

    # Fire-and-forget: debug recommended pipelines in background
    await aemit(Event("RecommendationsFetched", data={
        "uid": uid, "session": session,
        "suggestions": suggestions,
    }))


# ---------------------------------------------------------------------------
# One handler per event type (thin wrappers)
# ---------------------------------------------------------------------------
async def handle_recommendation_selected(event: Event):
    await _handle_interaction(event, "selected")


async def handle_recommendation_upvoted(event: Event):
    await _handle_interaction(event, "upvoted")


async def handle_recommendation_downvoted(event: Event):
    await _handle_interaction(event, "downvoted")


# handle_pipeline_objectives_switch ported to rust
# (engine/backend/src/handlers/recommendation.rs::handle_pipeline_objectives_switch).
# Pure I/O — meta read + objective-list rewrite + state/objectives/selected
# xadd. The python implementation lives below as a no-op shim only because
# registry.py still imports the symbol; remove once the import is dropped.
async def handle_pipeline_objectives_switch(event: Event):
    return


# ---------------------------------------------------------------------------
# Readiness-driven recommendations — triggered by data / task / eval changes
# ---------------------------------------------------------------------------
async def attempt_recommendations(event: Event):
    """Check context readiness, prompt user for missing pieces, trigger suggest().

    Subscribed to: DataExists, DataProfiled, DataScienceTaskSelected,
    EvaluationProcedureCommitted.

    Readiness model:
    - dataset present (any kind): enough to PROMPT for task/eval
    - dataset.profile: required only for ``suggest()`` itself — the
      prompts fire even without a profile so the user can still pick
      a task and eval procedure. Gating prompts on profile silently
      breaks the whole onboarding flow when a public dataset doc was
      seeded with ``profile = null`` (e.g. crawler ran with
      ``--no-profile``).
    - selectedDataScienceTask / selectedEvaluationProcedureId: prompt
      if missing.
    - rankingObjectives: auto-defaulted by ensure_ranking_objectives at session init.
    """
    uid = event.data.get("uid")
    session = event.data.get("session")
    if not uid or not session:
        return

    # Debounce: skip if this session ran us within the window. Always
    # let DataScienceTaskSelected through — that event carries new
    # context (the auto-detected task) that the previous run from
    # DataProfiled couldn't have seen, and a debounced skip here
    # would silently drop the recommendations refresh after task
    # detection.
    now = time.monotonic()
    last = _LAST_RUN.get(session, 0.0)
    if (
        now - last < _DEBOUNCE_WINDOW_S
        and event.type != "DataScienceTaskSelected"
    ):
        await aemit(Event("RecommendationsDebounced", {
            "session": session,
            "trigger": event.type,
            "since_last_s": round(now - last, 3),
        }))
        return
    _LAST_RUN[session] = now

    raw = await aioredis.get(RedisKeys.session_meta(session))
    if not raw:
        return
    meta = json.loads(raw)

    dataset = meta.get("dataset")
    if not isinstance(dataset, dict):
        return  # No dataset bound — nothing to prompt for yet
    has_profile = dataset.get("profile") is not None

    # -- Prompt for missing optional context --
    # Guard: track which query IDs have already been emitted to the stream so
    # we don't spam the frontend when multiple events trigger this function in
    # rapid succession (e.g. DataProfiled + RankingObjectivesChanged on reconnect).
    pending_query_ids = set(meta.get("_pendingQueryIds") or [])
    queries: list[dict] = []
    has_task = meta.get("selectedDataScienceTask") is not None
    has_eval = (
        meta.get("selectedEvaluationProcedureId") is not None
        or meta.get("selectedEvaluationProcedureName") is not None
    )

    task_qid = f"session:{session}:task_selection"
    eval_qid = f"session:{session}:eval_selection"

    # Skip the task question when the dataset has targets — the auto-detection
    # handler (handle_auto_task_selection) will infer the task from the target
    # column and emit DataScienceTaskSelected, which re-enters this function
    # with has_task=True.  Asking now would race with auto-detection and show
    # a redundant question that the user doesn't need to answer.
    did = dataset.get("did", "") if isinstance(dataset, dict) else ""
    targets_raw = await aioredis.get(RedisKeys.dataset_target_columns(did)) if did else None
    auto_detect_likely = targets_raw is not None and targets_raw != b"[]"

    if not has_task and not auto_detect_likely and task_qid not in pending_query_ids:
        try:
            tasks = await Tasks.get()
            queries.append({
                "id": task_qid,
                "type": "select",
                "question": "What data science task would you like to perform?",
                "options": [t.name for t in tasks],
            })
        except Exception:
            await aemit(Event("TaskFetchFailed", {"session": session}))

    if not has_eval and eval_qid not in pending_query_ids:
        try:
            evals = await EvaluationProcedures.get()
            queries.append({
                "id": eval_qid,
                "type": "select",
                "question": "Which evaluation procedure should be used?",
                "options": [e.name for e in evals],
            })
        except Exception:
            await aemit(Event("EvalProcedureFetchFailed", {"session": session}))

    if queries:
        # Mark these query IDs as pending in session meta so subsequent
        # attempt_recommendations calls (from other events) won't re-emit them.
        new_pending = pending_query_ids | {q["id"] for q in queries}
        meta["_pendingQueryIds"] = list(new_pending)
        await aioredis.set(RedisKeys.session_meta(session), json.dumps(meta))

        await aioredis.xadd(
            RedisKeys.stream(uid, session),
            {"event": "state/queries", "value": json.dumps(queries), "type": "json"},
            maxlen=STREAM_MAXLEN, approximate=True,
        )

    # -- Trigger recommendations with available context --
    # ``suggest_with_status`` reads dataset.profile metafeatures to
    # rank candidate pipelines; without profile it can't score and
    # would either error or return an unranked dump. Skip the call
    # when profile is missing — the user still gets the prompts
    # above, and a re-entry on ``DataProfiled`` will pick up here.
    if not has_profile:
        return
    try:
        suggestions, status = await suggest_with_status(uid, session)
        stream = RedisKeys.stream(uid, session)
        await aioredis.xadd(
            stream,
            {"event": "state/pipelines/recommendation", "value": _serialize_suggestions(suggestions), "type": "json"},
            maxlen=STREAM_MAXLEN, approximate=True,
        )
        await aioredis.xadd(
            stream,
            {"event": "state/objectives/status", "value": json.dumps(status), "type": "json"},
            maxlen=STREAM_MAXLEN, approximate=True,
        )

        # Fire-and-forget: debug recommended pipelines in background
        await aemit(Event("RecommendationsFetched", data={
            "uid": uid, "session": session,
            "suggestions": suggestions,
        }))
    except Exception as e:
        await aemit(Event("RecommendationEngineFailed", data={
            "source": "handlers.recommendations.attempt_recommendations",
            "uid": uid,
            "session": session,
            "error": str(e),
            "trace": traceback.format_exc(),
        }))
