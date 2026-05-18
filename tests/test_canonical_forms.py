"""Tests for the pipeline versioning + canonical-form substitution
scaffold under ``dorian.pipeline.canonical``."""
from __future__ import annotations

import pytest

from dorian.dag import DAG, Edge, Operator, Parameter, Snippet
from dorian.pipeline.canonical import (
    CanonicalEntry,
    DictCanonicalRegistry,
    MemoryLedger,
    RewriteObservation,
    canonical_class_hash,
    describe,
    evaluate,
    promotions,
    substitute,
)
from dorian.pipeline.seed_rewrite import inject_default_seeds


def _split_rf_dag(*, seed_wired: bool = False) -> DAG:
    """train_test_split + RandomForestClassifier with optional
    random_state wiring on both."""
    nodes: dict = {
        "fname": Parameter(name="fpath", dtype="str", value="a.csv"),
        "load": Operator(name="pandas.read_csv", language="python", tasks=[]),
        "split": Operator(
            name="sklearn.model_selection.train_test_split",
            language="python",
            tasks=[],
        ),
        "clf": Operator(
            name="sklearn.ensemble.RandomForestClassifier",
            language="python",
            tasks=[],
        ),
    }
    edges = [
        Edge(source="fname", destination="load", position=0, output=0),
        Edge(source="load", destination="split", position=0, output=0),
        Edge(source="split", destination="clf", position=0, output=0),
    ]
    if seed_wired:
        nodes["rs_split"] = Parameter(name="random_state", dtype="int", value="42")
        nodes["rs_clf"] = Parameter(name="random_state", dtype="int", value="42")
        edges.append(
            Edge(source="rs_split", destination="split",
                 position="random_state", output=0)
        )
        edges.append(
            Edge(source="rs_clf", destination="clf",
                 position="random_state", output=0)
        )
    return DAG(nodes=nodes, edges=edges)


# ---------------------------------------------------------------------------
# class_hash
# ---------------------------------------------------------------------------

def test_class_hash_is_deterministic():
    a = _split_rf_dag()
    b = _split_rf_dag()
    assert canonical_class_hash(a) == canonical_class_hash(b)


def test_class_hash_ignores_node_ids():
    a = _split_rf_dag()
    # Rename every node but preserve structure.
    rename = {old: f"X_{old}" for old in list(a.nodes)}
    renamed_nodes = {rename[k]: v for k, v in a.nodes.items()}
    renamed_edges = [
        Edge(
            source=rename[e.source],
            destination=rename[e.destination],
            position=e.position,
            output=e.output,
        )
        for e in a.edges
    ]
    b = DAG(nodes=renamed_nodes, edges=renamed_edges)
    assert canonical_class_hash(a) == canonical_class_hash(b)


def test_class_hash_ignores_parameter_values():
    a = _split_rf_dag()
    b = _split_rf_dag()
    # Flip fname value; same class.
    b.nodes["fname"] = Parameter(name="fpath", dtype="str", value="other.csv")
    assert canonical_class_hash(a) == canonical_class_hash(b)


def test_class_hash_distinguishes_wired_random_state():
    unwired = _split_rf_dag(seed_wired=False)
    wired = _split_rf_dag(seed_wired=True)
    assert canonical_class_hash(unwired) != canonical_class_hash(wired)


def test_class_hash_distinguishes_different_operators():
    a = _split_rf_dag()
    b = _split_rf_dag()
    b.nodes["clf"] = Operator(
        name="sklearn.ensemble.GradientBoostingClassifier",
        language="python",
        tasks=[],
    )
    assert canonical_class_hash(a) != canonical_class_hash(b)


def test_class_hash_seed_rewrite_changes_class():
    """The whole point: applying the auto-seed rewrite to an unwired
    pipeline moves it to a different class."""
    unwired = _split_rf_dag()
    pre = canonical_class_hash(unwired)
    inject_default_seeds(unwired)
    post = canonical_class_hash(unwired)
    assert pre != post


def test_describe_returns_useful_diagnostic():
    d = describe(_split_rf_dag())
    assert "class_hash" in d
    assert any("pandas.read_csv" in op for op in d["operators"])
    assert "sklearn.ensemble.RandomForestClassifier" in " ".join(d["operators"])


def test_class_hash_snippet_by_name():
    """Snippets are identified by name in v1 -- different code bodies
    with the same name collide. Documented behaviour; code-level
    canonicalisation is future work."""
    a = DAG(
        nodes={"s": Snippet(name="foo", code="def foo(x): return x", language="python")},
        edges=[],
    )
    b = DAG(
        nodes={"s": Snippet(name="foo", code="def foo(x): return x + 1", language="python")},
        edges=[],
    )
    # Same name -> same class today.
    assert canonical_class_hash(a) == canonical_class_hash(b)

    c = DAG(
        nodes={"s": Snippet(name="bar", code="def foo(x): return x", language="python")},
        edges=[],
    )
    assert canonical_class_hash(a) != canonical_class_hash(c)


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

def test_memory_ledger_records_observations():
    ledger = MemoryLedger()
    ledger.record(RewriteObservation(
        rule_id="auto_seed",
        source_class_hash="src_abc",
        target_class_hash="tgt_xyz",
    ))
    ledger.record(RewriteObservation(
        rule_id="auto_seed",
        source_class_hash="src_abc",
        target_class_hash="tgt_xyz",
    ))
    stats = ledger.stats_for("src_abc", "auto_seed")
    assert stats.observations == 2
    assert stats.hit_rate_for("tgt_xyz") == 1.0
    assert stats.dominant_target() == ("tgt_xyz", 2)


def test_memory_ledger_tracks_multiple_targets():
    ledger = MemoryLedger()
    for _ in range(8):
        ledger.record(RewriteObservation(
            "rule1", "src", "tgt_A"
        ))
    for _ in range(2):
        ledger.record(RewriteObservation(
            "rule1", "src", "tgt_B"
        ))
    stats = ledger.stats_for("src", "rule1")
    assert stats.observations == 10
    dom, count = stats.dominant_target()
    assert dom == "tgt_A"
    assert count == 8
    assert stats.hit_rate_for("tgt_A") == pytest.approx(0.8)
    assert stats.hit_rate_for("tgt_B") == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# promotion policy
# ---------------------------------------------------------------------------

def test_promotion_defers_below_min_observations():
    ledger = MemoryLedger()
    for _ in range(5):
        ledger.record(RewriteObservation("rule1", "src", "tgt"))
    decisions = evaluate(ledger, min_observations=20)
    assert all(d.action == "defer" for d in decisions)


def test_promotion_promotes_when_hit_rate_and_obs_meet_thresholds():
    ledger = MemoryLedger()
    for _ in range(100):
        ledger.record(RewriteObservation("rule1", "src", "tgt"))
    decisions = evaluate(ledger, hit_rate_threshold=0.95, min_observations=20)
    proms = promotions(decisions)
    assert len(proms) == 1
    assert proms[0].source_class_hash == "src"
    assert proms[0].target_class_hash == "tgt"
    assert proms[0].hit_rate == pytest.approx(1.0)


def test_promotion_demotes_below_hit_rate_threshold():
    ledger = MemoryLedger()
    # 60 "tgt_A" + 40 "tgt_B" -> hit rate on dominant is 0.6
    for _ in range(60):
        ledger.record(RewriteObservation("rule1", "src", "tgt_A"))
    for _ in range(40):
        ledger.record(RewriteObservation("rule1", "src", "tgt_B"))
    decisions = evaluate(ledger, hit_rate_threshold=0.95, min_observations=20)
    actions = {d.action for d in decisions}
    assert "demote" in actions


def test_promotion_defers_when_target_is_unstable():
    """If the dominant target class is itself a high-hit-rate source
    for another rule, defer -- collapse transitively is future work."""
    ledger = MemoryLedger()
    for _ in range(50):
        ledger.record(RewriteObservation("rule1", "src", "mid"))
    # mid is itself a source for another rule at 100% hit rate.
    for _ in range(50):
        ledger.record(RewriteObservation("rule2", "mid", "final"))
    decisions = evaluate(
        ledger,
        hit_rate_threshold=0.95,
        min_observations=20,
        target_instability_threshold=0.05,
    )
    # rule1 on `src -> mid` should be deferred because `mid` is
    # also a high-rate source.
    by_key = {(d.source_class_hash, d.rule_id): d for d in decisions}
    assert by_key[("src", "rule1")].action == "defer"
    # rule2 on `mid -> final` promotes normally (final is stable).
    assert by_key[("mid", "rule2")].action == "promote"


# ---------------------------------------------------------------------------
# substitution
# ---------------------------------------------------------------------------

def test_substitute_returns_original_when_no_registry_entry():
    candidate = _split_rf_dag()
    reg = DictCanonicalRegistry()
    result = substitute(candidate, reg)
    assert not result.substituted
    assert result.output_dag is candidate


def test_substitute_replaces_when_registry_has_promoted_mapping():
    unwired = _split_rf_dag(seed_wired=False)
    wired = _split_rf_dag(seed_wired=True)
    src = canonical_class_hash(unwired)
    tgt = canonical_class_hash(wired)

    reg = DictCanonicalRegistry()
    reg.register(CanonicalEntry(
        source_class_hash=src,
        target_class_hash=tgt,
        rule_id="auto_seed",
        canonical_pipeline=wired,
        hit_rate=1.0,
        observations=50,
    ))
    result = substitute(unwired, reg)
    assert result.substituted
    assert result.output_dag is wired
    assert result.rule_id == "auto_seed"
    assert canonical_class_hash(result.output_dag) == tgt


def test_substitute_many_preserves_order_and_per_item_result():
    unwired = _split_rf_dag(seed_wired=False)
    wired = _split_rf_dag(seed_wired=True)
    reg = DictCanonicalRegistry()
    reg.register(CanonicalEntry(
        source_class_hash=canonical_class_hash(unwired),
        target_class_hash=canonical_class_hash(wired),
        rule_id="auto_seed",
        canonical_pipeline=wired,
        hit_rate=1.0,
        observations=50,
    ))
    other = _split_rf_dag(seed_wired=True)  # already canonical; no sub
    from dorian.pipeline.canonical.substitution import substitute_many
    results = substitute_many([unwired, other], reg)
    assert results[0].substituted
    assert not results[1].substituted


def test_end_to_end_ledger_to_substitution():
    """Integration: observe the auto-seed rewrite firing reliably,
    run promotion policy, plant the result in the registry,
    recommend a fresh unwired pipeline, see it substituted."""
    unwired = _split_rf_dag(seed_wired=False)
    wired = _split_rf_dag(seed_wired=True)
    src = canonical_class_hash(unwired)
    tgt = canonical_class_hash(wired)

    # Simulate 30 observed rewrite firings on this class.
    ledger = MemoryLedger()
    for _ in range(30):
        ledger.record(RewriteObservation(
            rule_id="auto_seed_random_state",
            source_class_hash=src,
            target_class_hash=tgt,
        ))
    proms = promotions(evaluate(ledger))
    assert len(proms) == 1
    prom = proms[0]
    assert prom.source_class_hash == src
    assert prom.target_class_hash == tgt

    # Plant the canonical entry.
    reg = DictCanonicalRegistry()
    reg.register(CanonicalEntry(
        source_class_hash=prom.source_class_hash,
        target_class_hash=prom.target_class_hash,
        rule_id=prom.rule_id,
        canonical_pipeline=wired,
        hit_rate=prom.hit_rate,
        observations=prom.observations,
    ))

    # New, distinct recommendation in the unwired class -> gets
    # replaced with the wired canonical.
    fresh_unwired = _split_rf_dag(seed_wired=False)
    result = substitute(fresh_unwired, reg)
    assert result.substituted
    assert canonical_class_hash(result.output_dag) == tgt
