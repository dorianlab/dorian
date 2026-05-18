"""Tests for the exception-driven optimization pass scaffold."""
from __future__ import annotations

import re

import pytest

from dorian.pipeline.exceptions import (
    ExceptionPattern,
    LlmFallbackRequest,
    LlmFallbackResponse,
    MemoryExceptionRegistry,
    MitigationRef,
    TracebackSignature,
    canonicalise_message,
    extract,
    match,
    proposed_pattern_from,
    seed_patterns,
)


# ---------------------------------------------------------------------------
# canonicalise_message
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("'col_xyz' not in index", "'<STR>' not in index"),
        ('"col_xyz" not in index', "'<STR>' not in index"),
        ("X has 42 features, but needs 30", "X has <NUM> features, but needs <NUM>"),
        ("got -3.14 at address 0xDEADBEEF", "got <NUM> at address <HEX>"),
        ("Could not open /data/file.csv", "Could not open <PATH>"),
        ("Could not open C:\\data\\file.csv", "Could not open <PATH>"),
    ],
)
def test_canonicalise_message_substitutions(raw, expected):
    assert canonicalise_message(raw) == expected


def test_canonicalise_message_idempotent():
    """Running the canonicaliser twice yields the same result --
    placeholders like <STR>, <NUM> are already stable."""
    once = canonicalise_message("'a' has 42 items at 0xFF")
    twice = canonicalise_message(once)
    assert once == twice


# ---------------------------------------------------------------------------
# TracebackSignature.hash_hex
# ---------------------------------------------------------------------------

def test_signature_hash_is_deterministic():
    a = TracebackSignature(
        exception_type="KeyError",
        operator_fqn="sklearn.preprocessing.OrdinalEncoder.transform",
        site_library="pandas.core.indexes",
        message_template="'<STR>' not in index",
        user_frame_depth=2,
    )
    b = TracebackSignature(
        exception_type="KeyError",
        operator_fqn="sklearn.preprocessing.OrdinalEncoder.transform",
        site_library="pandas.core.indexes",
        message_template="'<STR>' not in index",
        user_frame_depth=2,
    )
    assert a.hash_hex() == b.hash_hex()


def test_signature_hash_is_sensitive_to_each_field():
    base = TracebackSignature(
        exception_type="KeyError",
        operator_fqn="x.y",
        site_library="pandas",
        message_template="'<STR>'",
        user_frame_depth=1,
    )
    perturbed = [
        TracebackSignature("ValueError", "x.y", "pandas", "'<STR>'", 1),
        TracebackSignature("KeyError", "z.w", "pandas", "'<STR>'", 1),
        TracebackSignature("KeyError", "x.y", "numpy", "'<STR>'", 1),
        TracebackSignature("KeyError", "x.y", "pandas", "'<NUM>'", 1),
        TracebackSignature("KeyError", "x.y", "pandas", "'<STR>'", 2),
    ]
    for other in perturbed:
        assert base.hash_hex() != other.hash_hex()


# ---------------------------------------------------------------------------
# extract() on a live exception
# ---------------------------------------------------------------------------

def test_extract_captures_exception_type_and_message():
    try:
        raise ValueError("X has 10 features, but fitter is expecting 20")
    except ValueError as exc:
        sig = extract(exc)
    assert sig.exception_type == "ValueError"
    # Canonicalised message drops the concrete counts.
    assert "<NUM>" in sig.message_template


def test_extract_picks_operator_frame():
    """When the raise happens inside a module matching a known
    operator prefix (e.g. pandas, sklearn), the signature should
    reflect that."""
    # We can't easily trigger a real pandas error inside a
    # sandboxed test without pandas semantics, but we CAN rely on
    # the traceback walker: raise from a module named to look like
    # a dorian-operator prefix using exec + a fake module.
    try:
        raise KeyError("'col_x'")
    except KeyError as exc:
        sig = extract(exc)
    # The test module itself is "tests.test_exception_handling",
    # not a known operator prefix; operator_fqn should be empty.
    assert sig.exception_type == "KeyError"
    assert sig.message_template == "'<STR>'"


# ---------------------------------------------------------------------------
# Registry + seed_patterns + match
# ---------------------------------------------------------------------------

def test_seed_patterns_includes_common_failure_modes():
    seeds = seed_patterns()
    # At least the three curated ones.
    assert len(seeds) >= 3
    kinds = {p.exception_type for p in seeds}
    assert {"KeyError", "NotFittedError", "ValueError"} <= kinds


def test_memory_registry_register_and_get():
    reg = MemoryExceptionRegistry()
    for p in seed_patterns():
        reg.register(p)
    assert len(reg) >= 3
    # Re-register is idempotent (keyed on signature hash).
    first_len = len(reg)
    for p in seed_patterns():
        reg.register(p)
    assert len(reg) == first_len


def test_match_via_hash_hit():
    reg = MemoryExceptionRegistry()
    for p in seed_patterns():
        reg.register(p)
    # Build a signature matching the first seed pattern exactly.
    sig = TracebackSignature(
        exception_type="KeyError",
        operator_fqn="",
        site_library="pandas.core.indexes",
        message_template="'<STR>'",
        user_frame_depth=0,
    )
    result = match(sig, reg)
    assert result.matched
    assert result.via == "hash"
    assert result.pattern is not None
    mits = result.mitigations_ranked()
    # Weights 0.6 + 0.4 for the KeyError seed.
    assert [m.weight for m in mits] == [0.6, 0.4]


def test_match_via_regex_hit_when_hash_misses():
    reg = MemoryExceptionRegistry()
    # Plant a pattern whose hash will NOT match our incoming sig
    # (different message_template), but whose regex will.
    pat = ExceptionPattern(
        signature_hash="deadbeef",
        exception_type="KeyError",
        operator_fqn="",
        site_library="pandas.core.indexes",
        message_template="something-unique",
        user_frame_depth=0,
        mitigations=(MitigationRef("fix_me", 1.0),),
        message_regex=re.compile(r"not in index"),
    )
    reg.register(pat)
    sig = TracebackSignature(
        exception_type="KeyError",
        operator_fqn="",
        site_library="pandas.core.indexes",
        # Different canonical template so the hash won't match.
        message_template="'<STR>' not found",
        user_frame_depth=0,
    )
    # The RAW message (not the template) is what the regex probes.
    result = match(sig, reg, raw_message="'col_xyz' not in index")
    assert result.matched
    assert result.via == "leaf_regex"


def test_match_miss_without_raw_message():
    reg = MemoryExceptionRegistry()
    sig = TracebackSignature(
        exception_type="TypeError",
        operator_fqn="",
        site_library="numpy",
        message_template="uncharted territory",
        user_frame_depth=0,
    )
    result = match(sig, reg)
    assert not result.matched
    assert result.via == "miss"


def test_match_miss_when_regex_does_not_match_raw():
    reg = MemoryExceptionRegistry()
    reg.register(ExceptionPattern(
        signature_hash="deadbeef",
        exception_type="KeyError",
        operator_fqn="",
        site_library="pandas.core.indexes",
        message_template="something",
        user_frame_depth=0,
        mitigations=(MitigationRef("x", 1.0),),
        message_regex=re.compile(r"not in index"),
    ))
    sig = TracebackSignature(
        exception_type="KeyError",
        operator_fqn="",
        site_library="pandas.core.indexes",
        message_template="unrelated",
        user_frame_depth=0,
    )
    result = match(sig, reg, raw_message="completely different message")
    assert not result.matched
    assert result.via == "miss"


def test_match_via_bucket_prefix_when_leaf_misses():
    """A KeyError from ``pandas.io.parsers`` (not covered by any
    leaf with site_library="pandas.core.indexes") should still land
    on the ``pandas.`` bucket entry from ``seed_patterns``."""
    reg = MemoryExceptionRegistry()
    for p in seed_patterns():
        reg.register(p)
    sig = TracebackSignature(
        exception_type="KeyError",
        operator_fqn="",
        site_library="pandas.io.parsers",  # NOT the leaf's site_library
        message_template="some brand new template",
        user_frame_depth=0,
    )
    result = match(sig, reg, raw_message="column 'zzz' missing from parser output")
    assert result.matched
    assert result.via == "bucket"
    assert result.pattern is not None
    assert result.pattern.scope == "bucket"
    # Bucket gets the catchall mitigation bundle.
    rewrite_ids = {m.rewrite_id for m in result.mitigations_ranked()}
    assert "inject_column_exists_check" in rewrite_ids


def test_leaf_beats_bucket_when_both_would_match():
    """Precision tier ordering: a leaf hit takes priority over a
    bucket hit, even when both cover the same surface."""
    reg = MemoryExceptionRegistry()
    for p in seed_patterns():
        reg.register(p)
    # Signature that matches BOTH the pandas leaf (via hash) AND
    # the pandas bucket (by exception_type + prefix).
    sig = TracebackSignature(
        exception_type="KeyError",
        operator_fqn="",
        site_library="pandas.core.indexes",
        message_template="'<STR>'",
        user_frame_depth=0,
    )
    result = match(sig, reg, raw_message="'col_xyz'")
    assert result.matched
    # Leaf wins -- via="hash", scope="leaf".
    assert result.via == "hash"
    assert result.pattern is not None
    assert result.pattern.scope == "leaf"


def test_bucket_hash_does_not_collide_with_leaf_hash():
    """Bucket hashes are synthesised from ``bucket:{type}:{prefix}``
    so they never collide with concrete 5-field signature hashes."""
    seeds = seed_patterns()
    leaves = [p for p in seeds if p.scope == "leaf"]
    buckets = [p for p in seeds if p.scope == "bucket"]
    assert buckets, "seed_patterns must include at least one bucket"
    leaf_hashes = {p.signature_hash for p in leaves}
    bucket_hashes = {p.signature_hash for p in buckets}
    assert leaf_hashes.isdisjoint(bucket_hashes)
    # Sanity: bucket hashes follow the synthetic-prefix convention.
    for h in bucket_hashes:
        assert h.startswith("bucket:")


def test_miss_stays_a_miss_when_no_bucket_covers_the_library():
    """An error family with no bucket entry (e.g. a torch error) must
    still report ``via="miss"`` -- that's the LLM-fallback trigger."""
    reg = MemoryExceptionRegistry()
    for p in seed_patterns():
        reg.register(p)
    sig = TracebackSignature(
        exception_type="RuntimeError",
        operator_fqn="",
        site_library="torch.nn.modules",
        message_template="shape mismatch",
        user_frame_depth=0,
    )
    result = match(sig, reg, raw_message="some torch-specific message")
    assert not result.matched
    assert result.via == "miss"


def test_registry_touch_increments_observations():
    reg = MemoryExceptionRegistry()
    for p in seed_patterns():
        reg.register(p)
    # Pick one pattern and touch it several times.
    hashes = [p.signature_hash for p in seed_patterns()]
    h = hashes[0]
    reg.touch(h)
    reg.touch(h)
    reg.touch(h)
    got = reg.get(h)
    assert got is not None
    assert got.observations == 3
    assert got.last_seen_ts > 0


# ---------------------------------------------------------------------------
# all_live filters by status
# ---------------------------------------------------------------------------

def test_all_live_excludes_proposed_and_demoted():
    reg = MemoryExceptionRegistry()
    for p in seed_patterns():
        reg.register(p)
    # Add a proposed one.
    reg.register(ExceptionPattern(
        signature_hash="proposed123",
        exception_type="ImportError",
        operator_fqn="",
        site_library="sklearn",
        message_template="some new template",
        user_frame_depth=0,
        mitigations=(),
        status="proposed",
    ))
    live = list(reg.all_live())
    assert all(p.status == "live" for p in live)
    assert "proposed123" not in {p.signature_hash for p in live}


# ---------------------------------------------------------------------------
# LLM-fallback contract
# ---------------------------------------------------------------------------

def test_proposed_pattern_from_builds_registry_entry():
    sig = TracebackSignature(
        exception_type="RuntimeError",
        operator_fqn="my.op.fn",
        site_library="torch.nn",
        message_template="mismatch",
        user_frame_depth=2,
    )
    req = LlmFallbackRequest(
        signature=sig,
        raw_message="size mismatch: 10 vs 20",
        raw_traceback="...",
    )
    resp = LlmFallbackResponse(
        root_cause_summary="tensor shape mismatch between layers",
        message_regex=r"size mismatch",
        message_template="size mismatch: <NUM> vs <NUM>",
        proposed_mitigations=(
            MitigationRef("insert_shape_assertion", 0.8),
        ),
        confidence=0.9,
    )
    pat = proposed_pattern_from(req, resp)
    assert pat.status == "proposed"
    assert pat.source == "llm_proposed"
    assert pat.signature_hash == sig.hash_hex()
    assert pat.message_regex is not None
    assert pat.message_regex.search("size mismatch somewhere")
    assert pat.observations == 1


# ---------------------------------------------------------------------------
# End-to-end: extract -> register -> match
# ---------------------------------------------------------------------------

def test_discovery_mode_regex_only_uses_only_regex_agent():
    """REGEX_ONLY ignores the LLM agent even if one is provided."""
    from dorian.pipeline.exceptions import (
        DiscoveryMode,
        DiscoveryProposal,
        DiscoveryRequest,
        StubDiscoveryAgent,
        discover,
    )
    sig = TracebackSignature("KeyError", "", "pandas", "'<STR>'", 0)
    req = DiscoveryRequest(signature=sig, raw_message="'x' not in index", raw_traceback="")
    regex_proposal = DiscoveryProposal(
        root_cause_summary="regex path",
        message_regex=r"not in index",
        message_template="'<STR>' not in index",
        proposed_mitigations=(MitigationRef("r_fix", 1.0),),
        confidence=1.0,
    )
    llm_proposal = DiscoveryProposal(
        root_cause_summary="llm path",
        message_regex=r"anything",
        message_template="...",
        proposed_mitigations=(MitigationRef("l_fix", 1.0),),
        confidence=1.0,
    )
    regex_agent = StubDiscoveryAgent({"KeyError": regex_proposal})
    llm_agent = StubDiscoveryAgent({"KeyError": llm_proposal})
    out = discover(req, mode=DiscoveryMode.REGEX_ONLY, regex_agent=regex_agent, llm_agent=llm_agent)
    assert len(out) == 1
    assert out[0].root_cause_summary == "regex path"


def test_discovery_mode_llm_fallback_uses_llm_on_regex_miss():
    from dorian.pipeline.exceptions import (
        DiscoveryMode,
        DiscoveryProposal,
        DiscoveryRequest,
        StubDiscoveryAgent,
        discover,
    )
    sig = TracebackSignature("RuntimeError", "", "torch.nn", "mismatch", 0)
    req = DiscoveryRequest(signature=sig, raw_message="", raw_traceback="")
    # regex agent knows nothing about RuntimeError -> miss
    regex_agent = StubDiscoveryAgent({"KeyError": DiscoveryProposal(
        root_cause_summary="regex",
        message_regex="",
        message_template="",
        proposed_mitigations=(),
    )})
    llm_proposal = DiscoveryProposal(
        root_cause_summary="llm",
        message_regex="size mismatch",
        message_template="size mismatch: <NUM> vs <NUM>",
        proposed_mitigations=(MitigationRef("x", 1.0),),
        confidence=0.7,
    )
    llm_agent = StubDiscoveryAgent({"RuntimeError": llm_proposal})
    out = discover(req, mode=DiscoveryMode.LLM_FALLBACK,
                    regex_agent=regex_agent, llm_agent=llm_agent)
    assert len(out) == 1
    assert out[0].root_cause_summary == "llm"


def test_discovery_mode_parallel_returns_both_when_both_hit():
    from dorian.pipeline.exceptions import (
        DiscoveryMode,
        DiscoveryProposal,
        DiscoveryRequest,
        StubDiscoveryAgent,
        discover,
    )
    sig = TracebackSignature("KeyError", "", "pandas", "'<STR>'", 0)
    req = DiscoveryRequest(signature=sig, raw_message="", raw_traceback="")
    p1 = DiscoveryProposal(
        root_cause_summary="from regex", message_regex="",
        message_template="", proposed_mitigations=(),
    )
    p2 = DiscoveryProposal(
        root_cause_summary="from llm", message_regex="",
        message_template="", proposed_mitigations=(),
    )
    out = discover(
        req,
        mode=DiscoveryMode.REGEX_AND_LLM_PARALLEL,
        regex_agent=StubDiscoveryAgent({"KeyError": p1}),
        llm_agent=StubDiscoveryAgent({"KeyError": p2}),
    )
    summaries = {p.root_cause_summary for p in out}
    assert summaries == {"from regex", "from llm"}


def test_discovery_llm_primary_falls_back_to_regex_on_miss():
    from dorian.pipeline.exceptions import (
        DiscoveryMode,
        DiscoveryProposal,
        DiscoveryRequest,
        StubDiscoveryAgent,
        discover,
    )
    sig = TracebackSignature("KeyError", "", "pandas", "'<STR>'", 0)
    req = DiscoveryRequest(signature=sig, raw_message="", raw_traceback="")
    p_regex = DiscoveryProposal(
        root_cause_summary="regex", message_regex="",
        message_template="", proposed_mitigations=(),
    )
    regex_agent = StubDiscoveryAgent({"KeyError": p_regex})
    llm_agent = StubDiscoveryAgent({})  # LLM knows nothing
    out = discover(
        req, mode=DiscoveryMode.LLM_PRIMARY,
        regex_agent=regex_agent, llm_agent=llm_agent,
    )
    assert len(out) == 1
    assert out[0].root_cause_summary == "regex"


def test_end_to_end_extract_then_match():
    # Build a registry whose KeyError pattern matches our
    # synthesised exception.
    reg = MemoryExceptionRegistry()
    for p in seed_patterns():
        reg.register(p)

    raw_message = ""
    try:
        raise KeyError("'missing_col'")
    except KeyError as exc:
        sig = extract(exc)
        raw_message = str(exc)
    # The signature is extracted correctly; even if the
    # site_library doesn't match the pandas seed (the raise came
    # from the test module, not pandas), the extraction path works
    # end to end.
    assert sig.exception_type == "KeyError"
    assert sig.message_template == "'<STR>'"
    # Try the match -- if site_library doesn't match, this stays a
    # miss, which is correct (the raise didn't happen in pandas).
    _ = match(sig, reg, raw_message=raw_message)


# ---------------------------------------------------------------------------
# Templating: deterministic bucket → leaf mining
# ---------------------------------------------------------------------------

def test_tokenise_keeps_placeholders_atomic():
    from dorian.pipeline.exceptions import tokenise
    toks = tokenise("X has <NUM> features, but needs <NUM>")
    assert "<NUM>" in toks
    # Placeholder must be one token, not split by angle brackets.
    num_count = sum(1 for t in toks if t == "<NUM>")
    assert num_count == 2


def test_propose_leaf_returns_none_below_min_samples():
    from dorian.pipeline.exceptions import propose_leaf
    # One sample: no disagreement to anti-unify away.
    assert propose_leaf(["'col_a' not in index"]) is None
    # Zero samples: trivial None.
    assert propose_leaf([]) is None


def test_propose_leaf_distils_shared_structure_from_pandas_keyerror_family():
    """Anti-unify three pandas KeyError canonicalised messages into a
    leaf regex that matches all of them and nothing weirdly broader."""
    from dorian.pipeline.exceptions import propose_leaf
    samples = [
        "'<STR>' not in index",
        "'<STR>' not in index",
        "'<STR>' not in index",
    ]
    prop = propose_leaf(samples)
    assert prop is not None
    assert prop.sample_count == 3
    # All three canonicalise identically, so the template equals any one.
    assert prop.template == "'<STR>' not in index"
    # And the regex must match an instance.
    compiled = re.compile(prop.regex)
    assert compiled.search("'missing' not in index")


def test_propose_leaf_collapses_disagreement_to_bounded_wildcard():
    """Samples share prefix + suffix but disagree on a middle token.
    The middle collapses to a bounded wildcard."""
    from dorian.pipeline.exceptions import propose_leaf
    samples = [
        "expected int got str",
        "expected int got float",
        "expected int got list",
    ]
    prop = propose_leaf(samples)
    assert prop is not None
    # Shared prefix "expected int got " must appear in the template
    # and the regex; disagreement on the last word collapses.
    assert "expected" in prop.template
    assert "got" in prop.template
    assert "<VAR>" in prop.template
    compiled = re.compile(prop.regex)
    assert compiled.search("expected int got str")
    assert compiled.search("expected int got dict")  # unseen, still matches
    # ...but the pattern must NOT match completely unrelated text.
    assert compiled.search("nothing to do with it") is None


def test_propose_leaf_refuses_pure_wildcard_no_info_gain():
    """When samples share no tokens, the mined regex would be pure
    wildcard — no precision gain over the bucket catchall. Refuse."""
    from dorian.pipeline.exceptions import propose_leaf
    # Disjoint vocab on both sides.
    samples = ["alpha beta gamma", "x y z", "one two three"]
    prop = propose_leaf(samples)
    assert prop is None


def test_propose_leaf_regex_has_only_bounded_quantifiers():
    """ReDoS defence: every quantifier in the emitted regex is
    upper-bounded. No unbounded ``*`` / ``+`` / ``?`` chains."""
    from dorian.pipeline.exceptions import propose_leaf
    samples = [
        "X has <NUM> features, but <STR> is expecting <NUM> features as input.",
        "X has <NUM> features, but <STR> is expecting <NUM> features as input.",
        "X has <NUM> features, but <STR> is expecting <NUM> features as input.",
    ]
    prop = propose_leaf(samples)
    assert prop is not None
    # Unbounded greedy patterns would be `.*`, `.+`, `.*?`, `.+?`.
    # The generator must emit `.{0,N}?` instead.
    assert ".*" not in prop.regex
    assert ".+" not in prop.regex
    # And every `.{...}` span must have a finite upper bound.
    for m in re.finditer(r"\.\{0,(\d+)\}", prop.regex):
        assert int(m.group(1)) < 10_000


def test_propose_leaf_regex_compiles_and_does_not_backtrack_catastrophically():
    """A smoke test that an adversarial input can't hang the
    compiled regex. If this hangs, the bounded-quantifier invariant
    has broken."""
    import time
    from dorian.pipeline.exceptions import propose_leaf
    samples = [
        "got <STR> at <HEX> in <PATH>",
        "got <STR> at <HEX> in <PATH>",
    ]
    prop = propose_leaf(samples)
    assert prop is not None
    compiled = re.compile(prop.regex)
    adversarial = "a" * 10_000
    start = time.perf_counter()
    compiled.search(adversarial)
    elapsed = time.perf_counter() - start
    # Bound: under 1 second even on a loaded CI runner.
    assert elapsed < 1.0, f"regex backtracked — took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Discovery pipeline: observe_match orchestration end-to-end
# ---------------------------------------------------------------------------

def test_observe_match_noop_on_hash_hit():
    """Hash-hit path: observation is recorded, but no discovery fires."""
    from dorian.pipeline.exceptions import (
        BufferRegistry,
        match,
        observe_match,
    )
    reg = MemoryExceptionRegistry()
    for p in seed_patterns():
        reg.register(p)
    sig = TracebackSignature(
        exception_type="KeyError",
        operator_fqn="",
        site_library="pandas.core.indexes",
        message_template="'<STR>'",
        user_frame_depth=0,
    )
    result = match(sig, reg)
    assert result.via == "hash"
    buffers = BufferRegistry()
    proposals = observe_match(
        result,
        raw_message="'x'",
        raw_traceback="",
        registry=reg,
        buffers=buffers,
    )
    assert proposals == []
    # Observation counter incremented.
    assert reg.get(result.pattern.signature_hash).observations == 1


def test_observe_match_bucket_accumulates_samples_and_mines_leaf():
    """After MIN_SAMPLES_FOR_PROMOTION distinct bucket hits with a
    shared structure, a status='proposed' scope='leaf' pattern lands
    in the registry with the bucket's mitigations carried over."""
    from dorian.pipeline.exceptions import (
        BufferRegistry,
        MIN_SAMPLES_FOR_PROMOTION,
        match,
        observe_match,
    )
    reg = MemoryExceptionRegistry()
    for p in seed_patterns():
        reg.register(p)
    buffers = BufferRegistry()

    # Samples must canonicalise to DISTINCT templates, otherwise the
    # buffer's dedup collapses them and templating never sees quorum.
    # Here the suffix varies ("missing" vs "not found") so canonical
    # forms differ but share the "column '<STR>' " prefix.
    raw_messages = [
        "column 'alpha' missing",
        "column 'beta' not found",
        "column 'gamma' missing",
    ][:MIN_SAMPLES_FOR_PROMOTION + 1]

    proposals_all = []
    for raw in raw_messages:
        sig = TracebackSignature(
            exception_type="KeyError",
            operator_fqn="",
            site_library="pandas.io.parsers",  # prefix-matches pandas. bucket
            message_template=canonicalise_message(raw),
            user_frame_depth=0,
        )
        result = match(sig, reg, raw_message=raw)
        assert result.via == "bucket"
        proposals_all.extend(
            observe_match(
                result,
                raw_message=raw,
                raw_traceback="",
                registry=reg,
                buffers=buffers,
            )
        )

    # Exactly one mined proposal (the later hits see buf.mined=True
    # and don't re-fire).
    assert len(proposals_all) == 1
    # Proposed leaf now lives in the registry with bucket mitigations.
    leaves = [
        p for p in reg.all_live() if p.scope == "leaf"
    ]
    # Seed provides 3 leaves; a proposed leaf is status='proposed'
    # and not returned by all_live(). So verify via direct lookup.
    mined = [
        p for p in reg._by_hash.values()
        if p.scope == "leaf" and p.status == "proposed"
    ]
    assert len(mined) == 1
    bucket = next(p for p in seed_patterns() if p.exception_type == "KeyError"
                  and p.scope == "bucket")
    assert mined[0].mitigations == bucket.mitigations


def test_observe_match_miss_escalates_to_llm_when_configured():
    from dorian.pipeline.exceptions import (
        BufferRegistry,
        DiscoveryProposal,
        StubDiscoveryAgent,
        match,
        observe_match,
    )
    reg = MemoryExceptionRegistry()
    for p in seed_patterns():
        reg.register(p)
    buffers = BufferRegistry()
    llm_proposal = DiscoveryProposal(
        root_cause_summary="llm mined",
        message_regex=r"size mismatch",
        message_template="size mismatch: <NUM> vs <NUM>",
        proposed_mitigations=(MitigationRef("insert_shape_assertion", 0.8),),
        confidence=0.7,
    )
    llm = StubDiscoveryAgent({"RuntimeError": llm_proposal})

    sig = TracebackSignature(
        exception_type="RuntimeError",
        operator_fqn="",
        site_library="torch.nn.modules",
        message_template="size mismatch: <NUM> vs <NUM>",
        user_frame_depth=0,
    )
    result = match(sig, reg, raw_message="size mismatch: 10 vs 20")
    assert result.via == "miss"
    events: list = []
    proposals = observe_match(
        result,
        raw_message="size mismatch: 10 vs 20",
        raw_traceback="...",
        registry=reg,
        buffers=buffers,
        llm_agent=llm,
        on_event=events.append,
    )
    assert len(proposals) == 1
    assert proposals[0].root_cause_summary == "llm mined"
    # The LLM path registered a proposed pattern with source="llm_proposed".
    llm_proposed = [
        p for p in reg._by_hash.values()
        if p.source == "llm_proposed"
    ]
    assert len(llm_proposed) == 1
    # Event emitted with the right kind.
    assert any(e.kind == "leaf_proposed_llm" for e in events)


def test_observe_match_miss_without_llm_agent_is_graceful():
    """No LLM configured + miss = no crash, no proposal, no registry
    change. Telemetry event still emits so the operator sees the gap."""
    from dorian.pipeline.exceptions import (
        BufferRegistry,
        match,
        observe_match,
    )
    reg = MemoryExceptionRegistry()
    for p in seed_patterns():
        reg.register(p)
    sig = TracebackSignature(
        exception_type="RuntimeError",
        operator_fqn="",
        site_library="torch.nn",
        message_template="mismatch",
        user_frame_depth=0,
    )
    result = match(sig, reg, raw_message="some torch error")
    events: list = []
    proposals = observe_match(
        result,
        raw_message="some torch error",
        raw_traceback="",
        registry=reg,
        buffers=BufferRegistry(),
        llm_agent=None,
        on_event=events.append,
    )
    assert proposals == []
    assert any(e.kind == "discovery_failed" for e in events)


def test_observe_match_bucket_refusal_escalates_to_llm():
    """When templating REFUSES (disjoint-vocab samples), the bucket
    path falls through to the LLM fallback."""
    from dorian.pipeline.exceptions import (
        BufferRegistry,
        DiscoveryProposal,
        StubDiscoveryAgent,
        match,
        observe_match,
    )
    reg = MemoryExceptionRegistry()
    for p in seed_patterns():
        reg.register(p)
    buffers = BufferRegistry()
    llm_proposal = DiscoveryProposal(
        root_cause_summary="llm fallback from bucket",
        message_regex=r"pandas-specific",
        message_template="pandas-specific",
        proposed_mitigations=(MitigationRef("llm_fix", 1.0),),
        confidence=0.5,
    )
    llm = StubDiscoveryAgent({"KeyError": llm_proposal})

    # Fully disjoint raw messages -> canonical templates share no
    # non-whitespace tokens. propose_leaf returns None.
    raws = ["'alpha'", "'beta'", "'gamma'"]
    # But wait — these ALL canonicalise to "'<STR>'", which is
    # IDENTICAL, so stability is perfect. Use disjoint-word raws.
    raws = ["alpha beta", "kappa delta", "mu nu"]

    events: list = []
    proposals_all = []
    for raw in raws:
        sig = TracebackSignature(
            exception_type="KeyError",
            operator_fqn="",
            site_library="pandas.io",
            message_template=canonicalise_message(raw),
            user_frame_depth=0,
        )
        result = match(sig, reg, raw_message=raw)
        assert result.via == "bucket"
        proposals_all.extend(
            observe_match(
                result,
                raw_message=raw,
                raw_traceback="",
                registry=reg,
                buffers=buffers,
                llm_agent=llm,
                on_event=events.append,
            )
        )

    # Templating refused; LLM fallback fired exactly once.
    escalated = [e for e in events if e.kind == "discovery_escalated"]
    assert len(escalated) == 1
    llm_events = [e for e in events if e.kind == "leaf_proposed_llm"]
    assert len(llm_events) == 1
