"""Exception pattern registry.

Maps traceback signatures to mitigation rewrites. Two kinds of
entries:

  * ``ExceptionPattern``  -- a known pattern with one or more
                             candidate mitigations. Status is
                             ``live`` (matched into UI suggestions)
                             or ``proposed`` (LLM-suggested, not
                             yet promoted).
  * ``MitigationRef``     -- a reference to a rewrite rule id in
                             the existing ``doc_rewrites``
                             collection, with a weight for ranking.

See (internal design note; not in public repo) § "Registry".
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Iterable, Protocol, runtime_checkable

from .traceback_signature import TracebackSignature


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MitigationRef:
    """Pointer to a rewrite rule in the existing ``rewrites``
    collection, with a suggestion-ranking weight."""

    rewrite_id: str
    weight: float = 1.0


PatternStatus = str  # "live" | "proposed" | "demoted"
PatternScope = str   # "leaf" | "bucket"


@dataclass
class ExceptionPattern:
    """One registry entry. Matches either by exact signature fields
    OR by a precompiled ``message_regex`` covering a family of
    instance-specific messages. The regex path is how the same
    pattern catches e.g. every "X not in index" KeyError
    regardless of which column X is.

    Scope
    -----
    ``leaf`` (default) patterns are the high-precision tier: the
    canonicalised template is authoritative and ``site_library`` is
    matched for strict equality. These are the entries seeded from
    curated templates or promoted from LLM proposals.

    ``bucket`` patterns are the lower-precision catch-all tier,
    consulted only when no leaf matches. A bucket's ``site_library``
    is interpreted as a PREFIX (so ``"pandas."`` catches any
    ``pandas.core.*`` / ``pandas.io.*`` surface), and its
    ``message_regex`` can be deliberately broad -- the idea is "we
    don't know this exact error, but a KeyError from pandas core
    almost certainly wants the column-missing mitigation bundle as
    a best-guess." Bucket patterns carry the same mitigation schema
    as leaves; the distinction is only in how they're matched.
    """

    signature_hash: str
    exception_type: str
    operator_fqn: str
    site_library: str
    message_template: str
    user_frame_depth: int
    mitigations: tuple[MitigationRef, ...]
    source: str = "regex"           # "regex" | "llm_proposed" | "operator_curated" | "bucket_catchall"
    status: PatternStatus = "live"
    scope: PatternScope = "leaf"    # "leaf" (exact) | "bucket" (catchall)
    observations: int = 0
    last_seen_ts: float = 0.0
    # Optional compiled regex for instance-specific message matching.
    # Compiled lazily so serialised patterns don't carry regex objects.
    message_regex: re.Pattern[str] | None = None


# ---------------------------------------------------------------------------
# Registry protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class ExceptionRegistry(Protocol):
    """Storage interface for exception patterns. docstore-backed
    impl is a follow-up; tests use ``MemoryExceptionRegistry``."""

    def register(self, pattern: ExceptionPattern) -> None:
        ...

    def get(self, signature_hash: str) -> ExceptionPattern | None:
        ...

    def all_live(self) -> Iterable[ExceptionPattern]:
        ...

    def touch(self, signature_hash: str) -> None:
        """Record an observation of the pattern."""
        ...


class MemoryExceptionRegistry:
    """In-memory exception-pattern registry for tests + local
    experimentation."""

    def __init__(self) -> None:
        self._by_hash: dict[str, ExceptionPattern] = {}

    def register(self, pattern: ExceptionPattern) -> None:
        self._by_hash[pattern.signature_hash] = pattern

    def get(self, signature_hash: str) -> ExceptionPattern | None:
        return self._by_hash.get(signature_hash)

    def all_live(self) -> Iterable[ExceptionPattern]:
        for p in self._by_hash.values():
            if p.status == "live":
                yield p

    def touch(self, signature_hash: str) -> None:
        p = self._by_hash.get(signature_hash)
        if p is None:
            return
        p.observations += 1
        p.last_seen_ts = time.time()

    def __len__(self) -> int:
        return len(self._by_hash)


# ---------------------------------------------------------------------------
# Seed library -- universal patterns for well-known library errors
# ---------------------------------------------------------------------------

def seed_patterns() -> list[ExceptionPattern]:
    """Seed library of global, well-known exception patterns. These
    are tenant-independent facts about pandas / sklearn / numpy;
    per-tenant + LLM-proposed patterns layer on top via ``register``.
    """
    out: list[ExceptionPattern] = []

    # --- pandas: column missing from index ---
    sig = TracebackSignature(
        exception_type="KeyError",
        operator_fqn="",
        site_library="pandas.core.indexes",
        message_template="'<STR>'",
        user_frame_depth=0,
    )
    out.append(
        ExceptionPattern(
            signature_hash=sig.hash_hex(),
            exception_type="KeyError",
            operator_fqn="",
            site_library="pandas.core.indexes",
            message_template="'<STR>'",
            user_frame_depth=0,
            mitigations=(
                MitigationRef("insert-simple-imputer-before", 0.6),
                MitigationRef("insert-ordinal-encoder-before", 0.4),
            ),
            source="operator_curated",
            message_regex=re.compile(r"\"[^\"]+\"|'[^']+'"),
        )
    )

    # --- sklearn: not fitted / called predict before fit ---
    sig = TracebackSignature(
        exception_type="NotFittedError",
        operator_fqn="",
        site_library="sklearn.utils",
        message_template=(
            "This <STR> instance is not fitted yet. Call 'fit' "
            "with appropriate arguments before using this estimator."
        ),
        user_frame_depth=0,
    )
    out.append(
        ExceptionPattern(
            signature_hash=sig.hash_hex(),
            exception_type="NotFittedError",
            operator_fqn="",
            site_library="sklearn.utils",
            message_template=sig.message_template,
            user_frame_depth=0,
            mitigations=(
                MitigationRef("insert_fit_before_predict", 1.0),
            ),
            source="operator_curated",
        )
    )

    # --- sklearn / numpy: categorical string in numeric feature
    #     matrix. Most common failure mode for classifier-on-categorical
    #     datasets (credit-g etc.) where the agent wired split →
    #     classifier without an OrdinalEncoder in between. Leaf pattern
    #     uses no site_library so it matches regardless of which
    #     sklearn subpackage surfaced the ValueError — ``check_array``,
    #     ``check_X_y``, the RF forest itself, etc.
    sig = TracebackSignature(
        exception_type="ValueError",
        operator_fqn="",
        site_library="",
        message_template="could not convert <STR> to <STR>: '<STR>'",
        user_frame_depth=0,
    )
    out.append(
        ExceptionPattern(
            signature_hash=sig.hash_hex(),
            exception_type="ValueError",
            operator_fqn="",
            site_library="",
            message_template=sig.message_template,
            user_frame_depth=0,
            mitigations=(
                MitigationRef("insert-ordinal-encoder-before", 0.8),
            ),
            source="operator_curated",
            message_regex=re.compile(
                r"could not convert .*? to (float|numeric)"
            ),
        )
    )

    # --- sklearn: NaN / Inf in feature matrix. Missing-value
    #     error that OrdinalEncoder won't fix but SimpleImputer will.
    sig = TracebackSignature(
        exception_type="ValueError",
        operator_fqn="",
        site_library="",
        message_template="Input <STR> contains NaN",
        user_frame_depth=0,
    )
    out.append(
        ExceptionPattern(
            signature_hash=sig.hash_hex(),
            exception_type="ValueError",
            operator_fqn="",
            site_library="",
            message_template=sig.message_template,
            user_frame_depth=0,
            mitigations=(
                MitigationRef("insert-simple-imputer-before", 0.8),
            ),
            source="operator_curated",
            message_regex=re.compile(
                r"Input .*? contains (NaN|infinity)"
            ),
        )
    )

    # --- sklearn: shape mismatch (common fit/transform after
    #     upstream schema change) ---
    sig = TracebackSignature(
        exception_type="ValueError",
        operator_fqn="",
        site_library="sklearn.utils",
        message_template=(
            "X has <NUM> features, but <STR> is expecting <NUM> "
            "features as input."
        ),
        user_frame_depth=0,
    )
    out.append(
        ExceptionPattern(
            signature_hash=sig.hash_hex(),
            exception_type="ValueError",
            operator_fqn="",
            site_library="sklearn.utils",
            message_template=sig.message_template,
            user_frame_depth=0,
            mitigations=(
                MitigationRef("insert-standard-scaler-before", 0.7),
                MitigationRef("refit_on_current_schema", 0.3),
            ),
            source="operator_curated",
        )
    )

    # ========================================================================
    # Bucket catch-alls (scope="bucket"). Consulted only when no leaf
    # pattern matches. Intentionally broad: carry a best-guess mitigation
    # bundle keyed off (exception_type, site_library-prefix) so novel
    # errors still get a sensible suggestion while the LLM-propose path
    # mines a proper leaf pattern in the background.
    # ========================================================================

    # pandas -- any KeyError surfacing inside pandas core / io. Catches
    # every column-missing / index-missing variant that didn't match a
    # specific leaf above.
    out.append(
        _bucket(
            exception_type="KeyError",
            site_library_prefix="pandas.",
            mitigations=(
                MitigationRef("insert-simple-imputer-before", 0.4),
                MitigationRef("insert-ordinal-encoder-before", 0.3),
            ),
            rationale_template="KeyError in pandas (bucket catchall)",
        )
    )

    # sklearn -- any ValueError from utils / preprocessing / model
    # selection. Covers the long tail of "bad input shape / wrong dtype
    # / NaN in X" variants that haven't been canonicalised yet.
    out.append(
        _bucket(
            exception_type="ValueError",
            site_library_prefix="sklearn.",
            mitigations=(
                MitigationRef("insert-simple-imputer-before", 0.4),
                MitigationRef("insert-standard-scaler-before", 0.3),
            ),
            rationale_template="ValueError in sklearn (bucket catchall)",
        )
    )

    # --- sklearn ≥1.3: FastICA whiten=True rejected by InvalidParameterError
    sig = TracebackSignature(
        exception_type="InvalidParameterError",
        operator_fqn="",
        site_library="",
        message_template=(
            "The 'whiten' parameter of FastICA must be a str among "
            "{<STR>} or a bool among {False}. Got True instead."
        ),
        user_frame_depth=0,
    )
    out.append(
        ExceptionPattern(
            signature_hash=sig.hash_hex(),
            exception_type="InvalidParameterError",
            operator_fqn="",
            site_library="",
            message_template=sig.message_template,
            user_frame_depth=0,
            mitigations=(MitigationRef("fix-fastica-whiten-true", 1.0),),
            source="operator_curated",
            message_regex=re.compile(
                r"The 'whiten' parameter of FastICA must be.*Got True"
            ),
        )
    )

    # --- sklearn: SGDClassifier penalty=int → must be string
    sig = TracebackSignature(
        exception_type="InvalidParameterError",
        operator_fqn="",
        site_library="",
        message_template=(
            "The 'penalty' parameter of SGDClassifier must be a str "
            "among {<STR>} or None. Got <STR> instead."
        ),
        user_frame_depth=0,
    )
    out.append(
        ExceptionPattern(
            signature_hash=sig.hash_hex(),
            exception_type="InvalidParameterError",
            operator_fqn="",
            site_library="",
            message_template=sig.message_template,
            user_frame_depth=0,
            mitigations=(MitigationRef("fix-sgd-penalty-int", 1.0),),
            source="operator_curated",
            message_regex=re.compile(
                r"The 'penalty' parameter of SGDClassifier must be"
            ),
        )
    )

    # --- sklearn: PCA n_components > min(n_samples, n_features)
    sig = TracebackSignature(
        exception_type="ValueError",
        operator_fqn="",
        site_library="",
        message_template=(
            "n_components=<STR> must be between 0 and "
            "min(n_samples, n_features)=<STR>"
        ),
        user_frame_depth=0,
    )
    out.append(
        ExceptionPattern(
            signature_hash=sig.hash_hex(),
            exception_type="ValueError",
            operator_fqn="",
            site_library="",
            message_template=sig.message_template,
            user_frame_depth=0,
            mitigations=(MitigationRef("fix-pca-too-many-components", 1.0),),
            source="operator_curated",
            message_regex=re.compile(
                r"n_components=\d+ must be between 0 and "
                r"min\(n_samples, n_features\)"
            ),
        )
    )

    # --- sklearn: SelectKBest/SelectPercentile score_func not callable.
    #     auto-sklearn enumerates the option as an int index — the
    #     ``param_to_snippet`` mitigation swaps the Parameter for a
    #     Snippet that imports + returns the underlying scoring fn.
    sig = TracebackSignature(
        exception_type="InvalidParameterError",
        operator_fqn="",
        site_library="",
        message_template=(
            "The 'score_func' parameter of <STR> must be a callable. "
            "Got <STR> instead."
        ),
        user_frame_depth=0,
    )
    out.append(
        ExceptionPattern(
            signature_hash=sig.hash_hex(),
            exception_type="InvalidParameterError",
            operator_fqn="",
            site_library="",
            message_template=sig.message_template,
            user_frame_depth=0,
            mitigations=(MitigationRef("fix-score-func-callable", 1.0),),
            source="operator_curated",
            message_regex=re.compile(
                r"The 'score_func' parameter of \w+ must be a callable"
            ),
        )
    )

    # --- sklearn: FeatureAgglomeration pooling_func not callable.
    sig = TracebackSignature(
        exception_type="InvalidParameterError",
        operator_fqn="",
        site_library="",
        message_template=(
            "The 'pooling_func' parameter of FeatureAgglomeration "
            "must be a callable. Got <STR> instead."
        ),
        user_frame_depth=0,
    )
    out.append(
        ExceptionPattern(
            signature_hash=sig.hash_hex(),
            exception_type="InvalidParameterError",
            operator_fqn="",
            site_library="",
            message_template=sig.message_template,
            user_frame_depth=0,
            mitigations=(MitigationRef("fix-pooling-func-callable", 1.0),),
            source="operator_curated",
            message_regex=re.compile(
                r"The 'pooling_func' parameter of FeatureAgglomeration "
                r"must be a callable"
            ),
        )
    )

    # --- sklearn: dense data required (input was sparse). Triggered
    #     by classifiers that don't accept CSR matrices (``MLPClassifier``,
    #     ``GaussianNB``, ``QuadraticDiscriminantAnalysis``) when an
    #     upstream encoder/sampler emitted sparse output. The
    #     ``insert_dense_converter_before`` mitigation splices a
    #     ``.toarray()`` Snippet on the X edge.
    sig = TracebackSignature(
        exception_type="TypeError",
        operator_fqn="",
        site_library="sklearn.utils",
        message_template=(
            "Sparse data was passed for X, but dense data is required. "
            "Use '.toarray()' to convert to a dense numpy array."
        ),
        user_frame_depth=0,
    )
    out.append(
        ExceptionPattern(
            signature_hash=sig.hash_hex(),
            exception_type="TypeError",
            operator_fqn="",
            site_library="sklearn.utils",
            message_template=sig.message_template,
            user_frame_depth=0,
            mitigations=(MitigationRef("insert-dense-converter-before", 1.0),),
            source="operator_curated",
            message_regex=re.compile(
                r"Sparse data was passed for X, but dense data is required"
            ),
        )
    )

    return out


def _bucket(
    *,
    exception_type: str,
    site_library_prefix: str,
    mitigations: tuple[MitigationRef, ...],
    rationale_template: str,
) -> ExceptionPattern:
    """Build a bucket catch-all ``ExceptionPattern``.

    The ``signature_hash`` is synthesised from the
    ``(exception_type, site_library_prefix)`` tuple so bucket entries
    never collide with hash-tier lookups on concrete signatures (which
    hash over all 5 fields including ``message_template``).
    """
    # Deliberate: make the hash derive from scope tag so it never
    # collides with a leaf pattern that happens to have the same prefix.
    synthetic_hash = f"bucket:{exception_type}:{site_library_prefix}"
    return ExceptionPattern(
        signature_hash=synthetic_hash,
        exception_type=exception_type,
        operator_fqn="",
        site_library=site_library_prefix,
        message_template=rationale_template,
        user_frame_depth=0,
        mitigations=mitigations,
        source="bucket_catchall",
        status="live",
        scope="bucket",
        message_regex=None,
    )


__all__ = [
    "ExceptionPattern",
    "ExceptionRegistry",
    "MemoryExceptionRegistry",
    "MitigationRef",
    "seed_patterns",
]
