"""Ranking objectives — the main extension point for recommendation scoring.

Each objective scores pipeline candidates on a single axis.  The recommendation
engine combines multiple objectives via non-dominated sorting (Pareto fronts).

Built-in objectives are registered in ``OBJECTIVE_REGISTRY`` by name.
User-defined objectives are compiled from code stored in the docstore and wrapped
in ``UserDefinedObjective``.

Every objective declares a ``requires`` frozenset of ``RecommendationContext``
field names it depends on.  When a required field is ``None``, the objective
returns 0.0 (graceful degradation).  The ``check_dependencies`` helper produces
a status report so the frontend can show which objectives are active vs degraded.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, FrozenSet, List, Optional, Protocol, TYPE_CHECKING

from backend.events import Event, emit

if TYPE_CHECKING:
    from dorian.pipeline.recommendation import RecommendationContext


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------
class Objective(Protocol):
    name: str
    requires: FrozenSet[str]

    def score(self, candidate: Dict[str, Any], ctx: RecommendationContext) -> float: ...


# ---------------------------------------------------------------------------
# Concrete objectives
# ---------------------------------------------------------------------------
class GeneralPerformance:
    """Ranks candidates by their stored evaluation performance (mean score)."""

    name = "Good General Performance"
    requires: FrozenSet[str] = frozenset()

    def score(self, candidate: Dict[str, Any], ctx: RecommendationContext) -> float:
        evaluations = candidate.get("evaluations") or []
        if not evaluations:
            return 0.0
        scores = [e.get("score", 0.0) for e in evaluations if isinstance(e.get("score"), (int, float))]
        return sum(scores) / len(scores) if scores else 0.0


class SimilarDataPerformance:
    """Ranks candidates by performance on datasets similar to the user's.

    Uses the KD-Tree in the Experiment Store to find datasets with similar
    metafeature profiles, then weights the candidate's evaluation scores
    by dataset similarity (1 / (1 + distance)).

    Works with **partial profiles** — when some metafeatures haven't been
    computed yet (e.g. slow PCA/landmark features), the KD-Tree query still
    works using available features.  Recommendations are jump-started from
    the partial profile and refined as profiling completes.

    Falls back to mean-score logic if the Experiment Store is not available.
    """

    name = "Good Performance On Similar Data"
    requires: FrozenSet[str] = frozenset({"dataset_profile"})

    def score(self, candidate: Dict[str, Any], ctx: RecommendationContext) -> float:
        if ctx.dataset_profile is None:
            return 0.0

        try:
            from dorian.experiment.store import get_experiment_store_sync
            store = get_experiment_store_sync()
            if store is not None and store.is_initialized:
                return store.score_by_similar_datasets(candidate, ctx.dataset_profile)
        except Exception:
            pass

        # Fallback: mean score (same as GeneralPerformance)
        return self._fallback_score(candidate)

    @staticmethod
    def _fallback_score(candidate: Dict[str, Any]) -> float:
        evaluations = candidate.get("evaluations") or []
        if not evaluations:
            return 0.0
        scores = [e.get("score", 0.0) for e in evaluations if isinstance(e.get("score"), (int, float))]
        return sum(scores) / len(scores) if scores else 0.0


class PreviouslyUnseen:
    """Penalises candidates that were already suggested but not selected."""

    name = "Previously Unseen"
    requires: FrozenSet[str] = frozenset()

    def score(self, candidate: Dict[str, Any], ctx: RecommendationContext) -> float:
        cid = str(candidate.get("_id", ""))
        if cid in ctx.selected:
            return 0.0
        return -ctx.suggested.count(cid)


class PipelinePreferenceRatio:
    """Ranks candidates by their pairwise win rate from user interactions.

    Uses the Experiment Store's ``get_win_rate()`` to compute:
        PPR = (times preferred) / (times compared)

    When a ``dataset_profile`` is available, the win rate is scoped to the
    current dataset for more contextual ranking.  Falls back to the global
    win rate when no dataset info is present.

    Returns 0.0 for candidates with no interaction history (graceful
    degradation for cold-start pipelines).
    """

    name = "Pipeline Preference Ratio"
    requires: FrozenSet[str] = frozenset()

    def score(self, candidate: Dict[str, Any], ctx: RecommendationContext) -> float:
        cid = str(candidate.get("_id", ""))
        if not cid:
            return 0.0

        try:
            from dorian.experiment.store import get_experiment_store_sync
            store = get_experiment_store_sync()
            if store is None or not store.is_initialized:
                return 0.0
            # Scoped win rate if dataset info is available
            dataset_id = None
            if ctx.dataset_profile is not None and isinstance(ctx.dataset_profile, dict):
                dataset_id = ctx.dataset_profile.get("dataset_id")
            return store.get_win_rate_sync(cid, dataset_id)
        except Exception:
            return 0.0


class FasterExecution:
    """Prefers candidates that are cheaper to execute.

    No reliable per-pipeline wall-clock is stored on candidates, so this
    objective uses **operator count** as a proxy: fewer operators means a
    smaller DAG, fewer materialisations, and (in aggregate) faster runs.

    Score is ``1 / (1 + n_operators)`` so the value is bounded to (0, 1],
    always active, and monotone in pipeline size. If the candidate also
    carries an ``evaluations`` entry with a ``duration`` or ``runtime_ms``
    metric, that overrides the proxy with a normalised inverse-time score.
    """

    name = "Faster Execution"
    requires: FrozenSet[str] = frozenset()

    def score(self, candidate: Dict[str, Any], ctx: RecommendationContext) -> float:
        # Prefer a stored duration metric when available (future-proof).
        evaluations = candidate.get("evaluations") or []
        durations = [
            float(e.get("score", 0.0))
            for e in evaluations
            if isinstance(e, dict)
            and str(e.get("metric", "")).lower() in {"duration", "runtime_ms", "runtime", "elapsed_ms"}
            and isinstance(e.get("score"), (int, float))
            and not isinstance(e.get("score"), bool)
            and float(e.get("score", 0.0)) > 0.0
        ]
        if durations:
            mean = sum(durations) / len(durations)
            return 1.0 / (1.0 + mean)

        # Proxy: inverse of operator count.
        names = _extract_operator_names(candidate)
        n = len(names)
        if n == 0:
            # Fall back to raw node count if operator extraction yielded nothing.
            raw_nodes = candidate.get("nodes") or candidate.get("operators") or []
            if isinstance(raw_nodes, dict):
                n = len(raw_nodes)
            elif isinstance(raw_nodes, list):
                n = len(raw_nodes)
        return 1.0 / (1.0 + float(n))


class AtomicChanges:
    """Prefers candidates whose operators overlap with the current pipeline (Jaccard)."""

    name = "Atomic Changes"
    requires: FrozenSet[str] = frozenset({"current_pipeline"})

    def score(self, candidate: Dict[str, Any], ctx: RecommendationContext) -> float:
        if ctx.current_pipeline is None:
            return 0.0

        current_nodes = set(_extract_operator_names(ctx.current_pipeline))
        candidate_nodes = set(_extract_operator_names(candidate))

        union = current_nodes | candidate_nodes
        if not union:
            return 0.0

        return len(current_nodes & candidate_nodes) / len(union)


# ---------------------------------------------------------------------------
# User-defined objectives — compiled from user-submitted code
# ---------------------------------------------------------------------------
class UserDefinedObjective:
    """Wraps user-submitted Python code as a scoring function.

    The user's code must define a function ``score(candidate, ctx) -> float``.
    The code is compiled once at instantiation and called per candidate.

    Security:
    - ``__import__`` is removed from builtins (``import X`` fails with NameError)
    - ``open``, ``exec``, ``eval``, ``compile`` are blocked
    - Pre-injected safe modules: ``math``, ``statistics``
    - All runtime exceptions are caught and return 0.0
    - Non-numeric returns are coerced to 0.0
    """

    requires: FrozenSet[str] = frozenset()

    def __init__(self, name: str, code: str, language: str = "python"):
        self.name = name
        self._code = code
        self._language = language
        self._fn: Optional[Callable] = None
        self._compile_error: Optional[str] = None
        self._compile()

    def _compile(self) -> None:
        if self._language != "python":
            self._compile_error = f"Unsupported language: {self._language}"
            return

        try:
            compiled = compile(self._code, f"<objective:{self.name}>", "exec")
            namespace: Dict[str, Any] = {}

            # Restricted builtins — no file I/O, imports, exec/eval
            _blocked = {
                "open", "exec", "eval", "compile", "__import__",
                "input", "breakpoint", "exit", "quit",
                "globals", "locals", "vars", "dir",
                "getattr", "setattr", "delattr",
            }
            raw_builtins = __builtins__ if isinstance(__builtins__, dict) else __builtins__.__dict__
            namespace["__builtins__"] = {k: v for k, v in raw_builtins.items() if k not in _blocked}

            # Allow math + statistics — safe and useful for scoring
            import math
            import statistics
            namespace["math"] = math
            namespace["statistics"] = statistics

            exec(compiled, namespace)  # noqa: S102 — intentional restricted exec

            if "score" not in namespace or not callable(namespace["score"]):
                self._compile_error = "Code must define: def score(candidate, ctx) -> float"
                return

            self._fn = namespace["score"]

        except SyntaxError as e:
            self._compile_error = f"Syntax error: {e}"
        except Exception as e:
            self._compile_error = f"Compilation error: {e}"

    @property
    def is_valid(self) -> bool:
        return self._fn is not None

    @property
    def compile_error(self) -> Optional[str]:
        return self._compile_error

    def score(self, candidate: Dict[str, Any], ctx: RecommendationContext) -> float:
        if self._fn is None:
            return 0.0

        try:
            result = self._fn(candidate, ctx)
            if isinstance(result, (int, float)) and not isinstance(result, bool):
                return float(result)
            return 0.0
        except Exception:
            return 0.0  # never crash the scoring loop


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
OBJECTIVE_REGISTRY: Dict[str, type] = {
    cls.name: cls
    for cls in [
        GeneralPerformance,
        SimilarDataPerformance,
        PreviouslyUnseen,
        PipelinePreferenceRatio,
        AtomicChanges,
        FasterExecution,
    ]
}


def resolve_objectives(
    objective_names: List[str],
    custom_objectives: Optional[List[Dict[str, Any]]] = None,
) -> List[Objective]:
    """Instantiate objectives by name, including user-defined ones.

    Built-in objectives are looked up from ``OBJECTIVE_REGISTRY``.
    Custom objectives are compiled from ``(name, language, code)`` dicts
    loaded from the docstore.  Falls back to ``GeneralPerformance`` if nothing resolves.
    """
    if not objective_names:
        return [GeneralPerformance()]

    custom_by_name: Dict[str, Dict[str, Any]] = {}
    if custom_objectives:
        for co in custom_objectives:
            if isinstance(co, dict) and co.get("name") and co.get("code"):
                custom_by_name[co["name"]] = co

    resolved: List[Objective] = []
    for name in objective_names:
        if name in OBJECTIVE_REGISTRY:
            resolved.append(OBJECTIVE_REGISTRY[name]())
        elif name in custom_by_name:
            co = custom_by_name[name]
            obj = UserDefinedObjective(
                name=co["name"],
                code=co["code"],
                language=co.get("language", "python"),
            )
            if obj.is_valid:
                resolved.append(obj)
            else:
                emit(Event("CustomObjectiveCompilationFailed", {
                    "name": co["name"],
                    "error": obj.compile_error,
                }))

    return resolved if resolved else [GeneralPerformance()]


# ---------------------------------------------------------------------------
# Dependency checking — status metadata for the frontend
# ---------------------------------------------------------------------------
def check_dependencies(
    objectives: List[Objective],
    ctx: RecommendationContext,
) -> List[Dict[str, Any]]:
    """Return status metadata for each objective.

    Returns::

        [{"name": str, "status": "active"|"degraded", "missing": [str]}, ...]

    An objective is *degraded* when one or more of its required context fields
    are ``None``.  Scoring behaviour does not change (the objective still returns
    0.0 for missing deps).  This is purely informational for the UI.
    """
    result = []
    for obj in objectives:
        reqs = getattr(obj, "requires", frozenset())
        missing = [dep for dep in reqs if getattr(ctx, dep, None) is None]
        result.append({
            "name": obj.name,
            "status": "active" if not missing else "degraded",
            "missing": missing,
        })
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _extract_operator_names(pipeline: Dict[str, Any]) -> List[str]:
    """Pull operator names from a pipeline document (various shapes)."""
    nodes = pipeline.get("nodes") or pipeline.get("operators") or []
    if isinstance(nodes, dict):
        nodes = list(nodes.values())
    return [
        str(n.get("name") or n.get("operator"))
        for n in nodes
        if isinstance(n, dict) and (n.get("name") or n.get("operator"))
    ]
