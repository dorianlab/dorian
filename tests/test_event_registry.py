"""Tests for event handler registry completeness and wiring.

Covers:
  - All expected events are subscribed
  - Handler imports resolve without error
  - Critical data pathway events have handlers
"""
import pytest


# Capture all subscribe() calls once at module level — the registry is
# designed to run exactly once, so we capture during that single run.
_CALLS: list[tuple[str, object]] = []


def _capture_calls():
    if _CALLS:
        return  # already captured

    import backend.events as events_mod
    original = events_mod.subscribe

    def _capture(name, handler):
        _CALLS.append((name, handler))

    events_mod.subscribe = _capture

    import dorian.event.registry as reg
    reg._registered = False
    reg.register_event_handlers()

    events_mod.subscribe = original


_capture_calls()

_EVENT_NAMES = {name for name, _ in _CALLS}


def _handlers_for(event_name: str) -> list:
    return [h for name, h in _CALLS if name == event_name]


class TestRegistryCompleteness:

    # --- Session lifecycle ---

    def test_init_session_wired(self):
        assert "InitSession" in _EVENT_NAMES

    def test_feedback_received_wired(self):
        assert "FeedbackReceived" in _EVENT_NAMES

    # --- Pipeline lifecycle ---

    def test_pipeline_exists_wired(self):
        assert "PipelineExists" in _EVENT_NAMES

    def test_pipeline_saved_has_multiple_handlers(self):
        handlers = _handlers_for("PipelineSaved")
        assert len(handlers) >= 2

    def test_execute_pipeline_wired(self):
        assert "ExecutePipeline" in _EVENT_NAMES

    def test_cancel_pipeline_wired(self):
        assert "CancelPipeline" in _EVENT_NAMES

    # --- Recommendation events ---

    def test_recommendation_user_events_wired(self):
        for event in (
            "PipelineRecommendationSelected",
            "PipelineRecommendationUpvoted",
            "PipelineRecommendationDownvoted",
        ):
            assert event in _EVENT_NAMES, f"{event} not subscribed"

    def test_readiness_triggers_attempt_recommendations(self):
        for event in ("DataProfiled", "DataScienceTaskSelected", "EvaluationProcedureSelected"):
            handlers = _handlers_for(event)
            names = [getattr(h, "__name__", "") for h in handlers]
            assert any("attempt_recommendations" in n for n in names), \
                f"{event} missing attempt_recommendations"

    # --- Risk / AI Debugger chain ---

    def test_risk_chain_wired(self):
        for event in ("TaskIdentified", "PotentialRiskIdentified", "RiskIdentified",
                       "MitigationActionsIdentified", "PipelineNodeAdded"):
            assert event in _EVENT_NAMES, f"{event} not subscribed"

    def test_suggestion_events_wired(self):
        assert "SuggestionInteraction" in _EVENT_NAMES
        assert "SuggestionAccepted" in _EVENT_NAMES

    # --- Canvas events → persist ---

    def test_canvas_events_all_persisted(self):
        canvas_events = [
            "PipelineNodeAdded", "PipelineNodeRemoved",
            "PipelineEdgeAdded", "PipelineEdgeRemoved",
            "PipelineNodeConfigured", "PipelineComposed",
            "PipelineExportClicked", "PipelineShareClicked",
        ]
        for event in canvas_events:
            names = [getattr(h, "__name__", "") for h in _handlers_for(event)]
            assert any("persist_interaction_event" in n for n in names), \
                f"{event} missing persist_interaction_event"

    # --- Dataset lifecycle ---

    def test_dataset_events_wired(self):
        for event in ("DatasetUploaded", "DatasetImported", "DatasetRemoved"):
            assert event in _EVENT_NAMES, f"{event} not subscribed"

    # --- Pipeline CRUD ---

    def test_pipeline_crud_wired(self):
        for event in ("PipelineCreated", "PipelineUpdated", "PipelineRemoved"):
            assert event in _EVENT_NAMES, f"{event} not subscribed"

    # --- Custom nodes ---

    def test_custom_node_events_wired(self):
        for event in ("CustomOperatorAdded", "CustomSnippetAdded", "CustomParameterAdded"):
            assert event in _EVENT_NAMES, f"{event} not subscribed"

    # --- Ranking objectives ---

    def test_ranking_objectives_wired(self):
        assert "RankingObjectivesChanged" in _EVENT_NAMES
        assert "RankingObjectiveAdded" in _EVENT_NAMES

    # --- Experiment store persistence ---

    def test_experiment_store_handlers_wired(self):
        for event in ("DataProfiled", "PipelineSaved", "PipelineRunCompleted", "PipelineRunFailed"):
            assert event in _EVENT_NAMES, f"{event} not subscribed"

    # --- Worker metrics ---

    def test_worker_metrics_wired(self):
        assert "WorkerMetricsCollected" in _EVENT_NAMES

    # --- Notifications ---

    def test_notification_events_wired(self):
        for event in ("PipelineRunCompleted", "PipelineRunFailed",
                       "RecommendationsFetched", "DataProfiled"):
            names = [getattr(h, "__name__", "") for h in _handlers_for(event)]
            assert any("notify" in n for n in names), \
                f"{event} missing notify handler"

    # --- Vault analytics ---

    def test_vault_events_persisted(self):
        for event in ("VaultEnvVarStored", "VaultEnvVarDeleted"):
            names = [getattr(h, "__name__", "") for h in _handlers_for(event)]
            assert any("persist_interaction_event" in n for n in names), \
                f"{event} missing persist_interaction_event"

    # --- Extraction ---

    def test_extraction_events_wired(self):
        for event in ("ExtractPipeline", "SaveExtractionRules", "LoadExtractionRules",
                       "SuggestExtractionRules", "AcceptExtractionRule", "RejectExtractionRule"):
            assert event in _EVENT_NAMES, f"{event} not subscribed"

    # --- Integrity checks ---

    def test_no_none_handlers(self):
        for name, handler in _CALLS:
            assert handler is not None, f"Handler for {name} is None"
            assert callable(handler), f"Handler for {name} is not callable"

    def test_minimum_subscription_count(self):
        assert len(_CALLS) >= 50, \
            f"Only {len(_CALLS)} subscriptions — expected 50+"
