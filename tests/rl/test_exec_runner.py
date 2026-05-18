"""Tests for the rl.exec Python ⇄ Rust bridge.

These tests exercise the pyo3 bindings through the Python wrappers so
contract drift is caught at `uv run pytest` time rather than at the
first RL rollout. They require the ``dorian_native`` extension to be
built (``uv run maturin develop --release`` from ``engine/native/``).
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip(
    "dorian_native",
    reason="dorian_native extension not built; "
    "run `uv run maturin develop --release` in engine/native/",
)

from rl.exec import (  # noqa: E402
    BatchProjection,
    BatchRunner,
    DemSummary,
    ExperimentGraph,
    cache_affinity,
    dem_summary,
    detect_missing_random_state,
)


HOUSING_FIXTURE = (
    ".data/app/766398ff-8100-4d30-a81c-8aea2b3d0ca7/pipeline.json"
)


def _housing() -> str:
    import pathlib

    p = pathlib.Path(HOUSING_FIXTURE)
    if not p.is_file():
        pytest.skip(f"fixture not found: {HOUSING_FIXTURE}")
    return p.read_text(encoding="utf-8")


def test_dem_summary_counts_housing_nodes():
    s = dem_summary(_housing())
    assert isinstance(s, DemSummary)
    assert s.node_count == 9
    assert s.edge_count == 12
    assert s.sdf_count == 9
    assert s.de_count == 0
    # preprocessing + print are Snippets and are non-deterministic.
    assert set(s.non_deterministic_node_ids) == {"preprocessing", "print"}


def test_dem_summary_cacheable_fraction_matches_deterministic():
    s = dem_summary(_housing())
    assert s.cacheable_fraction == pytest.approx(
        s.deterministic_count / s.node_count
    )


def test_cache_affinity_empty_index_is_zero():
    aff = cache_affinity(_housing())
    assert aff == pytest.approx(0.0)


def test_batch_runner_collapses_identical_pipelines():
    runner = BatchRunner()
    proj = runner.plan([_housing()] * 3)
    assert isinstance(proj, BatchProjection)
    assert proj.pipelines == 3
    # 3 identical pipelines -> unique = per-pipeline deterministic count,
    # collapsed = 2 * that (two duplicates on top of the one surviving).
    assert proj.unique_fire_count > 0
    assert proj.collapsed_firings == 2 * proj.unique_fire_count
    assert proj.implied_speedup == pytest.approx(3.0)


def test_batch_runner_parameter_change_breaks_upstream_only():
    """Flipping a parameter invalidates nodes depending on it AND only
    those — downstream nodes isolated from it by a bypass-class
    (Snippet) keep their constant keys and still collapse across
    pipelines."""
    data = _housing()
    alt = json.loads(_housing())
    alt["nodes"]["fname"]["value"] = "data/other.csv"
    runner = BatchRunner()
    proj = runner.plan([data, json.dumps(alt)])
    # `data_loading` diverges (fname-dependent); everything downstream
    # of the `preprocessing` Snippet stays constant because the
    # Snippet bypass breaks pedigree propagation, so those nodes
    # collapse. The exact split is corpus-specific; what we assert
    # here is that at least one node diverged AND at least one
    # collapsed.
    assert 0 < proj.collapsed_firings < proj.naive_fire_count
    assert 1.0 < proj.implied_speedup < float(proj.pipelines)


def test_experiment_graph_cold_start_zero_affinity():
    eg = ExperimentGraph()
    assert len(eg) == 0
    assert eg.is_empty
    assert eg.affinity(_housing()) == pytest.approx(0.0)


def test_experiment_graph_commit_then_warm_affinity_is_one():
    eg = ExperimentGraph()
    n = eg.commit(_housing(), artifact="feature", compute_secs=0.5)
    assert n > 0
    assert len(eg) == n
    # Same pipeline now fully served from the index.
    assert eg.affinity(_housing()) == pytest.approx(1.0)


def test_experiment_graph_match_reports_hits_misses_bypassed():
    eg = ExperimentGraph()
    m_cold = eg.match(_housing())
    assert len(m_cold.hits) == 0
    assert len(m_cold.misses) > 0
    # Snippets always bypass — never in hits/misses.
    assert set(m_cold.bypassed) >= {"preprocessing", "print"}

    eg.commit(_housing())
    m_warm = eg.match(_housing())
    assert len(m_warm.hits) == len(m_cold.misses)
    assert len(m_warm.misses) == 0
    # Bypassed set is unchanged.
    assert set(m_warm.bypassed) == set(m_cold.bypassed)


def test_experiment_graph_batch_fully_cached_implies_infinite_work_saved():
    eg = ExperimentGraph()
    eg.commit(_housing())
    proj = eg.plan_batch([_housing()] * 3)
    # Every firing in all 3 pipelines now hits the index.
    assert proj.unique_fire_count == 0
    assert proj.index_hits > 0
    # Speedup reports naive count as the "work avoided" proxy.
    assert proj.implied_speedup == float(proj.naive_fire_count)


def test_experiment_graph_commit_rejects_bad_artifact():
    eg = ExperimentGraph()
    with pytest.raises(ValueError):
        eg.commit(_housing(), artifact="bogus")


def test_commit_episode_is_equivalent_to_commit_for_structural_reuse():
    """commit_episode is a semantic wrapper over commit. Both
    materialise the same pedigree so downstream affinity picks up
    identically."""
    eg_a = ExperimentGraph()
    eg_b = ExperimentGraph()
    eg_a.commit(_housing(), artifact="feature", compute_secs=0.5)
    eg_b.commit_episode(
        _housing(), terminal_reward=0.9, artifact="feature", compute_secs=0.5
    )
    assert len(eg_a) == len(eg_b)
    assert eg_a.affinity(_housing()) == pytest.approx(eg_b.affinity(_housing()))


def test_commit_episode_works_on_partial_pipeline_but_caller_shouldnt():
    """Structural test: commit on an intermediate graph is not
    rejected by the optimizer -- the docs state the contract, and
    RL callers honour it. The test pins current behaviour so a
    future tightening (e.g. adding a validity check) is a conscious
    choice, not accidental."""
    partial = {
        "nodes": {
            "fname": {"type": "Parameter", "name": "fpath", "dtype": "str", "value": "a.csv"},
            "load": {"type": "Operator", "name": "pandas.read_csv", "language": "python", "tasks": []},
            # No consumers -- `load` is a dangling leaf.
        },
        "edges": [
            {"source": "fname", "destination": "load", "position": 0, "output": 0},
        ],
    }
    eg = ExperimentGraph()
    n = eg.commit_episode(json.dumps(partial), terminal_reward=0.0)
    # `load` is deterministic; still counts. Contract says this is
    # the caller's responsibility to avoid.
    assert n >= 1


def test_analytical_reads_on_partial_graph_never_mutate():
    """Intermediate reads must not touch shared state."""
    eg = ExperimentGraph()
    before = len(eg)
    partial = {
        "nodes": {
            "load": {"type": "Operator", "name": "pandas.read_csv", "language": "python", "tasks": []},
        },
        "edges": [],
    }
    partial_s = json.dumps(partial)
    _ = eg.affinity(partial_s)
    _ = eg.match(partial_s)
    _ = eg.plan_batch([partial_s, partial_s])
    assert len(eg) == before


def _make_split_rf_pipeline(wire_split_seed: bool = False, wire_clf_seed: bool = False) -> str:
    pipe = {
        "nodes": {
            "fname": {"type": "Parameter", "name": "fpath", "dtype": "str", "value": "a.csv"},
            "load": {"type": "Operator", "name": "pandas.read_csv", "language": "python", "tasks": []},
            "split": {"type": "Operator", "name": "sklearn.model_selection.train_test_split", "language": "python", "tasks": []},
            "clf": {"type": "Operator", "name": "sklearn.ensemble.RandomForestClassifier", "language": "python", "tasks": []},
            "test_size": {"type": "Parameter", "name": "test_size", "dtype": "float", "value": "0.2"},
        },
        "edges": [
            {"source": "fname", "destination": "load", "position": 0, "output": 0},
            {"source": "load", "destination": "split", "position": 0, "output": 0},
            {"source": "test_size", "destination": "split", "position": "test_size", "output": 0},
            {"source": "split", "destination": "clf", "position": 0, "output": 0},
        ],
    }
    if wire_split_seed:
        pipe["nodes"]["rs_split"] = {"type": "Parameter", "name": "random_state", "dtype": "int", "value": "42"}
        pipe["edges"].append({"source": "rs_split", "destination": "split", "position": "random_state", "output": 0})
    if wire_clf_seed:
        pipe["nodes"]["rs_clf"] = {"type": "Parameter", "name": "random_state", "dtype": "int", "value": "42"}
        pipe["edges"].append({"source": "rs_clf", "destination": "clf", "position": "random_state", "output": 0})
    return json.dumps(pipe)


def test_detect_missing_random_state_flags_unwired_seeds():
    # No seeds wired: both split + clf are flagged.
    missing = detect_missing_random_state(_make_split_rf_pipeline())
    assert sorted(missing) == ["clf", "split"]


def test_detect_missing_random_state_honours_partial_wiring():
    missing = detect_missing_random_state(
        _make_split_rf_pipeline(wire_split_seed=True)
    )
    assert missing == ["clf"]

    missing_both = detect_missing_random_state(
        _make_split_rf_pipeline(wire_split_seed=True, wire_clf_seed=True)
    )
    assert missing_both == []


def test_unwired_random_state_forces_cache_bypass():
    # The correctness gate: even with all other params matching,
    # unwired random_state means match_pipeline counts this node as
    # bypassed, not misses.
    eg = ExperimentGraph()
    unwired = _make_split_rf_pipeline()
    m = eg.match(unwired)
    assert "split" in m.bypassed
    assert "clf" in m.bypassed
    # Wire both seeds -> both become cacheable (misses against a cold index).
    fully = _make_split_rf_pipeline(wire_split_seed=True, wire_clf_seed=True)
    m_fully = eg.match(fully)
    assert "split" in m_fully.misses
    assert "clf" in m_fully.misses
    assert "split" not in m_fully.bypassed
    assert "clf" not in m_fully.bypassed


def test_op_tasks_change_produces_different_cache_keys():
    """Two otherwise-identical operator nodes with different `tasks`
    lists MUST produce different pedigrees so they never collide."""
    base = json.loads(_housing())
    alt = json.loads(_housing())
    # Alter the tasks on `data_loading` only in the `alt` variant.
    alt["nodes"]["data_loading"]["tasks"] = ["read_csv", "info"]

    eg = ExperimentGraph()
    m_base = eg.match(json.dumps(base))
    m_alt = eg.match(json.dumps(alt))
    k_base = m_base.node_keys["data_loading"]
    k_alt = m_alt.node_keys["data_loading"]
    assert k_base != k_alt, "op.tasks must be reflected in the cache key"


def test_dem_summary_accepts_dict_or_string():
    # Smoke test: the wrapper accepts both a raw JSON string and a
    # pre-parsed dict (json.dumps internally).
    s1 = dem_summary(_housing())
    s2 = dem_summary(json.loads(_housing()))
    assert s1 == s2
