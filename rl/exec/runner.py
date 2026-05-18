"""Bridge from the Python RL trainer to the Rust DEM engine.

This module is the load-bearing Python ⇄ Rust seam. The RL agent
proposes a candidate DAG; the utilities here inspect it through the
Rust parser + cache primitives.

What v1 of this module provides (all via the ``dorian_native`` pyo3
extension built from ``engine/native``):

  * ``dem_summary(dag_json) -> DemSummary`` — domain / determinism
    classification, used by the RL env to validate candidates before
    dispatch and to decide cache eligibility per node.

  * ``cache_affinity(dag_json) -> float`` — scalar in [0, 1] used as
    a logit-nudge input in action priors. v1 scores against an empty
    experiment graph (always 0.0 cold-start); a follow-up lands the
    live-index variant once the pyo3 bridge for ``ExperimentGraphIndex``
    handles is added.

  * ``BatchProjection.from_candidates(dags) -> BatchProjection`` —
    collapse-statistics over a batch of candidate pipelines; the RL
    rollout uses this to estimate effective wall-clock cost when
    running N candidates together.

What v1 does NOT yet provide:

  * ``run_pipeline(dag_json, dataset_id)`` actually runs — the Rust
    scheduler is there (``sdf::SdfScheduler``) but the dispatch layer
    still needs to ship real Python payloads back. See
    (internal design note; not in public repo) § Open Questions for the sequencing.

  * Live Experiment Graph handles across process boundaries — requires
    a ``PyExperimentGraphIndex`` wrapper in the native crate, landing
    in the next round.

The RL training loop should treat v1 as a read-only introspection of
the Rust engine's view of candidate DAGs. Running the pipeline
end-to-end still goes through the legacy Python executor for now;
the A2 ablation (engine swap) flips this once ``run_pipeline`` lands.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Sequence

# ``dorian_native`` is OPTIONAL at import time. The RL rollout path
# works without it (legacy Python executor handles everything); only
# the cache-affinity + batch-projection + live-ExperimentGraph bridges
# need the pyo3 extension. Functions that do need it raise a clear
# message at call-time so the demo can run end-to-end even without
# the Rust toolchain / maturin build on the image.
try:
    import dorian_native  # type: ignore[import-not-found]
    _DORIAN_NATIVE_AVAILABLE = True
except ImportError:  # pragma: no cover -- optional dep
    dorian_native = None  # type: ignore[assignment]
    _DORIAN_NATIVE_AVAILABLE = False


def _require_native(feature: str) -> None:
    if not _DORIAN_NATIVE_AVAILABLE:
        raise ImportError(
            f"{feature} requires the dorian_native pyo3 extension. "
            f"Run `uv run maturin develop --release` from engine/native/ "
            f"to build it, OR disable this feature "
            f"(e.g. DORIAN_RL_COMMIT=false skips ExperimentGraph)."
        )


# ---------------------------------------------------------------------------
# DEM map summary
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DemSummary:
    """Rust-side view of a candidate pipeline."""

    node_count: int
    edge_count: int
    sdf_count: int
    de_count: int
    deterministic_count: int
    non_deterministic_count: int
    unknown_count: int
    de_node_ids: tuple[str, ...]
    non_deterministic_node_ids: tuple[str, ...]

    @classmethod
    def from_rust(cls, raw: dict) -> "DemSummary":
        return cls(
            node_count=raw["node_count"],
            edge_count=raw["edge_count"],
            sdf_count=raw["sdf_count"],
            de_count=raw["de_count"],
            deterministic_count=raw["deterministic_count"],
            non_deterministic_count=raw["non_deterministic_count"],
            unknown_count=raw["unknown_count"],
            de_node_ids=tuple(raw.get("de_node_ids", [])),
            non_deterministic_node_ids=tuple(
                raw.get("non_deterministic_node_ids", [])
            ),
        )

    @property
    def cacheable_fraction(self) -> float:
        """Fraction of nodes eligible for content-addressable caching."""
        if self.node_count == 0:
            return 0.0
        return self.deterministic_count / self.node_count


def dem_summary(dag_json: str | dict) -> DemSummary:
    """Parse a DAG through the Rust parser and return its DEM summary."""
    _require_native("dem_summary")
    s = dag_json if isinstance(dag_json, str) else json.dumps(dag_json)
    raw = json.loads(dorian_native.dem_map_summary(s))
    return DemSummary.from_rust(raw)


# ---------------------------------------------------------------------------
# Cache affinity
# ---------------------------------------------------------------------------

def detect_missing_random_state(dag_json: str | dict) -> list[str]:
    """Node IDs in the pipeline that declare a random_state parameter
    (e.g. train_test_split, RandomForestClassifier, MLPClassifier)
    but do NOT have a Parameter node wired to that handle. These
    firings will Bypass the cache by design -- an unseeded stochastic
    op must never share results across runs.

    Mitigation: a rewrite rule adds ``Parameter(random_state, int,
    42)`` nodes to each flagged operator so subsequent passes find
    the handle wired and admit the node to the cache. See
    (internal design note; not in public repo) and the user directive of
    2026-04-20 on explicit seed-pinning as a critical correctness
    requirement.
    """
    _require_native("detect_missing_random_state")
    s = dag_json if isinstance(dag_json, str) else json.dumps(dag_json)
    return list(dorian_native.detect_missing_random_state(s))


def cache_affinity(dag_json: str | dict) -> float:
    """Cache-affinity scalar for the RL logit-nudge term.

    v1: scored against an empty experiment graph, so the cold-start
    value is always 0.0. The scalar starts carrying signal once the
    live-index variant lands; for now it is here so callers can wire
    the ε · cache_affinity(a) nudge in advance without code churn.
    """
    _require_native("cache_affinity")
    s = dag_json if isinstance(dag_json, str) else json.dumps(dag_json)
    return float(dorian_native.cache_affinity_empty(s))


# ---------------------------------------------------------------------------
# Batch projection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BatchProjection:
    """Collapse-statistics over N candidate pipelines."""

    pipelines: int
    naive_fire_count: int
    unique_fire_count: int
    collapsed_firings: int
    collapse_ratio: float
    implied_speedup: float
    index_hits: int = 0  # 0 for empty-index variant, populated by live index

    @classmethod
    def from_raw(cls, raw: dict) -> "BatchProjection":
        return cls(
            pipelines=raw["pipelines"],
            naive_fire_count=raw["naive_fire_count"],
            unique_fire_count=raw["unique_fire_count"],
            collapsed_firings=raw["collapsed_firings"],
            collapse_ratio=float(raw["collapse_ratio"]),
            implied_speedup=float(raw["implied_speedup"]),
            index_hits=int(raw.get("index_hits", 0)),
        )

    @classmethod
    def from_candidates(cls, dags: Sequence[str | dict]) -> "BatchProjection":
        """Empty-index variant; wire through ExperimentGraph for the
        live-index path."""
        _require_native("BatchProjection.from_candidates")
        payloads = [
            d if isinstance(d, str) else json.dumps(d) for d in dags
        ]
        raw = json.loads(dorian_native.plan_batch_empty_index(payloads))
        return cls.from_raw(raw)


# ---------------------------------------------------------------------------
# Live Experiment Graph — shared cache state across RL episodes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReuseMatch:
    """Per-pipeline reuse snapshot against a live index."""

    hits: tuple[str, ...]
    misses: tuple[str, ...]
    bypassed: tuple[str, ...]
    hit_ratio: float
    node_keys: dict[str, str]  # node_id -> hex(CacheKey)


class ExperimentGraph:
    """Python wrapper over ``dorian_native.ExperimentGraphIndex``.

    Shared across RL episodes so ``cache_affinity`` carries real
    signal after the first rollout. ``commit(dag, ...)`` materialises
    every deterministic node in a completed pipeline under its
    computed cache key — future calls to ``affinity`` or
    ``plan_batch`` see those nodes as hits.
    """

    def __init__(self) -> None:
        _require_native("ExperimentGraph")
        self._native = dorian_native.ExperimentGraphIndex()

    def __len__(self) -> int:
        return len(self._native)

    @property
    def is_empty(self) -> bool:
        return bool(self._native.is_empty())

    def affinity(self, dag_json: str | dict) -> float:
        s = dag_json if isinstance(dag_json, str) else json.dumps(dag_json)
        return float(self._native.cache_affinity(s))

    def match(self, dag_json: str | dict) -> ReuseMatch:
        s = dag_json if isinstance(dag_json, str) else json.dumps(dag_json)
        raw = json.loads(self._native.match_pipeline(s))
        return ReuseMatch(
            hits=tuple(raw.get("hits", [])),
            misses=tuple(raw.get("misses", [])),
            bypassed=tuple(raw.get("bypassed", [])),
            hit_ratio=float(raw.get("hit_ratio", 0.0)),
            node_keys=dict(raw.get("node_keys", {})),
        )

    def plan_batch(self, dags: Sequence[str | dict]) -> BatchProjection:
        payloads = [
            d if isinstance(d, str) else json.dumps(d) for d in dags
        ]
        raw = json.loads(self._native.plan_batch(payloads))
        return BatchProjection.from_raw(raw)

    def commit(
        self,
        dag_json: str | dict,
        *,
        artifact: str = "feature",
        payload: dict | None = None,
        compute_secs: float = 0.0,
    ) -> int:
        """Low-level commit. Materialises every deterministic node
        of the given pipeline as a cache entry under its computed
        key.

        **Contract**: the dag must be a TERMINAL, validated pipeline.
        Committing an intermediate (mid-construction) pipeline
        corrupts downstream lookups because the resulting keys
        carry a partial pedigree. RL callers should prefer
        ``commit_episode`` which names the contract in its API.

        See (internal design note; not in public repo) for the full
        read-vs-write boundary.
        """
        if artifact not in {"feature", "statistics", "model", "opaque"}:
            raise ValueError(f"unknown artifact: {artifact}")
        s = dag_json if isinstance(dag_json, str) else json.dumps(dag_json)
        p = json.dumps(payload or {"placeholder": True})
        return int(
            self._native.insert_from_pipeline(s, artifact, p, compute_secs)
        )

    def commit_episode(
        self,
        dag_json: str | dict,
        *,
        terminal_reward: float,
        artifact: str = "feature",
        compute_secs: float = 0.0,
        payload: dict | None = None,
    ) -> int:
        """End-of-episode commit for the RL trainer.

        Call once per completed trajectory, AFTER the env has
        reported ``done=True`` and the terminal reward has been
        observed. Writes a placeholder envelope per deterministic
        node, tagged with ``terminal_reward`` so future benefit-
        scoring and episode-level analyses can correlate cache
        entries with the episode that produced them.

        Do NOT call from inside the step loop. Intermediate commits
        corrupt the shared index -- see
        (internal design note; not in public repo).

        Returns the number of entries inserted (equal to the
        number of deterministic, cache-eligible nodes in the
        pipeline). A return of 0 indicates either the pipeline has
        no deterministic nodes OR all deterministic nodes were
        bypassed (e.g. unwired ``random_state``); neither is a
        crash, but a 0 means the episode added nothing to the
        shared index.
        """
        enriched = dict(payload or {})
        enriched.update(
            {
                "placeholder": True,
                "terminal_reward": float(terminal_reward),
                "episode_compute_secs": float(compute_secs),
            }
        )
        return self.commit(
            dag_json,
            artifact=artifact,
            payload=enriched,
            compute_secs=compute_secs,
        )


# ---------------------------------------------------------------------------
# RunResult / BatchRunner — contract, live impl pending
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunResult:
    """Outcome of one pipeline evaluation. Populated by the live runner
    once the dispatch layer ships real payloads; see
    (internal design note; not in public repo) § "Execution Engine"."""

    reward: float
    wall_clock_secs: float
    cache_hits: int
    cache_misses: int
    bypassed_nodes: int
    failed_nodes: list[str] = field(default_factory=list)
    structural_hash: str = ""


def run_pipeline(
    dag_json: str | dict,
    dataset_csv_path: str,
    *,
    seed: int | None = None,
    canonical_registry=None,
) -> RunResult:
    """Evaluate a single DAG.

    Tier C path: routes through the existing Python+Dask executor
    in ``rl.env.executor`` (reuses ``dorian.pipeline.operator_
    resolver`` + ``dask.threaded.get``). Zero round-trip through
    ``dorian.pipeline.execution.run_pipeline`` -- we skip the
    session-meta + Redis + event-bus plumbing for the inner RL
    loop.

    Tier D add-on (optional): canonical-form substitution. If a
    ``canonical_registry`` is supplied and the dag's class hash
    matches a promoted source, the registry's canonical pipeline
    is executed instead and the substitution is recorded in the
    returned ``RunResult.structural_hash`` for UI observability.

    Future Tier D: swap the executor for the Rust SDF scheduler
    once the dispatch layer is wired. Signature stays.
    """
    # Convert to a Dorian DAG object natively -- no intermediate
    # node-list shape. The env's _dag IS already a DAG; callers
    # that pass raw JSON deserialise once.
    from dorian.dag import DAG
    from dorian.pipeline.canonical import canonical_class_hash, substitute
    from rl.env.executor import execute_pipeline

    if isinstance(dag_json, str):
        dag_payload = json.loads(dag_json)
    else:
        dag_payload = dag_json
    dag = DAG.from_json_dict(dag_payload)
    class_hash = canonical_class_hash(dag)

    if canonical_registry is not None:
        sub_result = substitute(dag, canonical_registry)
        if sub_result.substituted:
            dag = sub_result.output_dag
            class_hash = canonical_class_hash(dag)

    result = execute_pipeline(dag, dataset_csv_path=dataset_csv_path)
    return RunResult(
        reward=float(result.metric_value) if result.metric_value is not None else 0.0,
        wall_clock_secs=result.wall_clock_secs,
        cache_hits=0,  # Tier C: Python+Dask executor; cache-hit accounting lands with the Rust scheduler.
        cache_misses=len(dag.nodes),
        bypassed_nodes=0,
        failed_nodes=[result.failed_node] if result.failed_node else [],
        structural_hash=class_hash,
    )


class BatchRunner:
    """Batch of candidate DAGs sharing one cache + one batch plan.

    v1 projects collapse statistics (``plan()`` → ``projection``) via
    the Rust ``plan_batch_empty_index`` path; ``execute()`` awaits the
    dispatch-layer runner (same note as ``run_pipeline`` above)."""

    def __init__(self, cache_handle: object | None = None) -> None:
        self._cache = cache_handle
        self._planned: list[str] = []
        self._projection: BatchProjection | None = None

    def plan(self, dags: Sequence[str | dict]) -> BatchProjection:
        self._planned = [
            d if isinstance(d, str) else json.dumps(d) for d in dags
        ]
        self._projection = BatchProjection.from_candidates(self._planned)
        return self._projection

    @property
    def projection(self) -> BatchProjection | None:
        return self._projection

    def execute(self) -> list[RunResult]:
        raise NotImplementedError(
            "execute() needs the live cache + Firer dispatch wiring; "
            "see internal design notes § Execution Engine."
        )
