"""Deterministic template mining for bucket → leaf promotion.

When the bucket tier accumulates N distinct raw messages that it
served (directly, or as a fallback for misses), their canonicalised
templates form a training set we can anti-unify into a narrower
regex. The narrower regex gets promoted to a ``scope="leaf"``
pattern with the same mitigation bundle carried over from the
bucket. This is the deterministic path that gradually drains the
bucket tier into leaves without LLM involvement — user asked for
cheap deterministic options before reaching for an LLM.

Two ingredients:

  * **Tokenisation** — split each canonicalised message on
    word-ish boundaries while keeping Dorian's placeholder tokens
    (``<STR>``, ``<NUM>``, ``<HEX>``, ``<PATH>``) atomic.
  * **Star-refinement LCS** — pick one message as the centroid,
    LCS-align every other message against it, and keep only the
    positions that are stable across ALL alignments. Equivalent
    to progressively intersecting the shared-token set; cheaper
    than generalised pairwise anti-unification and sufficient
    because the canonicalisation already stripped most of the
    variation.

The emitted regex:

  * Literal tokens for stable positions (escaped for regex
    meta-characters).
  * ``.{0,<BOUND>}?`` for variable spans, where ``<BOUND>`` is
    capped so the compiled regex can't ReDoS (no nested quantifiers,
    every quantifier has an upper bound).

Test contract: for a set of inputs that differ only in placeholder
contents, the mined template should equal the canonicalised form
of any one input. For a set that differs in literal tokens, those
positions collapse to bounded wildcards.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# Maximum number of characters a single wildcard span may consume.
# Keeps every quantifier bounded → impossible to trigger catastrophic
# backtracking no matter how many wildcards get chained. 256 chars is
# much larger than any realistic variable substring but short enough
# that a pathological regex can't hang the matcher.
_WILDCARD_BOUND = 256

# Minimum number of sample messages needed for a non-degenerate
# template. One message has no disagreement to anti-unify away;
# the caller should enforce this bound.
MIN_SAMPLES_FOR_PROMOTION = 2

# Token regex: alphanumeric runs, standalone punctuation, OR one of
# the placeholder tokens as an atomic match. Order matters — the
# placeholder alternatives precede the generic fallback so they bind
# as single tokens.
_PLACEHOLDERS = ("<STR>", "<NUM>", "<HEX>", "<PATH>")
_TOKEN_RE = re.compile(
    r"<STR>|<NUM>|<HEX>|<PATH>"       # placeholder tokens (atomic)
    r"|[A-Za-z_][A-Za-z0-9_]*"        # identifier-shaped runs
    r"|\d+"                           # bare digit runs
    r"|\s+"                           # whitespace (kept as its own token)
    r"|[^\s\w]"                       # single punctuation char
)

# Reverse-expansion of canonicaliser placeholders into bounded regex
# fragments. The mined template matches the CANONICALISED shape of a
# message, but registry callers probe the RAW message; each
# placeholder therefore needs to be reversed into a regex that
# matches the original variable substring the canonicaliser would
# have eaten. Every alternative is bounded to stay ReDoS-free.
#
# NOTE on <STR>. The canonicaliser writes quoted strings as
# ``'<STR>'`` — the quote chars are LITERAL context, not part of the
# placeholder. Tokenisation splits ``'<STR>'`` into three tokens
# (``'`` / ``<STR>`` / ``'``); stable-quote tokens handle the
# delimiters, and <STR>'s expansion covers ONLY the inner body.
# Keeping the expansion body-only means adjacent quote literals
# aren't double-counted in the emitted regex.
_PLACEHOLDER_EXPANSION: dict[str, str] = {
    # String BODY (no surrounding quotes), 0..256 chars, no quote
    # chars inside. Adjacent tokens carry the delimiters.
    "<STR>": r"[^'\"]{0,256}",
    # Ints or floats, 1..32 digits each side (wider than any real
    # message carries while keeping the alternation short).
    "<NUM>": r"-?\d{1,32}(?:\.\d{1,32})?",
    # Hex literals, 1..32 hex chars after the 0x.
    "<HEX>": r"0x[0-9a-fA-F]{1,32}",
    # Paths — unix or windows style, non-whitespace up to 512 chars.
    "<PATH>": r"\S{1,512}",
}


@dataclass(frozen=True)
class LeafProposal:
    """A mined regex + template suitable for promoting to a
    ``scope="leaf"`` ``ExceptionPattern``.

    ``regex`` and ``template`` are strings (not compiled): the
    caller compiles once at registration time, per registry policy.
    ``sample_count`` carries through as the minimum-observation
    floor so the promotion worker can apply its threshold.
    """

    regex: str
    template: str
    sample_count: int


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------

def tokenise(message: str) -> list[str]:
    """Split a canonicalised message into atomic tokens.

    Whitespace IS preserved as its own token so round-tripping
    (tokens → join) reconstructs the original string exactly, and
    so regex assembly can re-insert whitespace faithfully without
    "squashing" it via `\\s+`.
    """
    return _TOKEN_RE.findall(message)


# ---------------------------------------------------------------------------
# LCS (standard DP, token-level)
# ---------------------------------------------------------------------------

def _lcs_indices(a: list[str], b: list[str]) -> list[tuple[int, int]]:
    """Return aligned index pairs (i, j) where ``a[i] == b[j]`` and
    the pairs form a longest common subsequence of ``a`` vs ``b``.

    Standard O(|a|·|b|) DP. Returned pairs are ordered so both
    index sequences are strictly increasing.
    """
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return []
    # dp[i][j] = LCS length of a[:i] vs b[:j]
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n):
        for j in range(m):
            if a[i] == b[j]:
                dp[i + 1][j + 1] = dp[i][j] + 1
            else:
                dp[i + 1][j + 1] = max(dp[i][j + 1], dp[i + 1][j])
    # Backtrack.
    pairs: list[tuple[int, int]] = []
    i, j = n, m
    while i > 0 and j > 0:
        if a[i - 1] == b[j - 1]:
            pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif dp[i - 1][j] >= dp[i][j - 1]:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return pairs


# ---------------------------------------------------------------------------
# Star-refinement over N sample messages
# ---------------------------------------------------------------------------

def _stable_mask_against_centroid(
    centroid: list[str], other: list[str]
) -> list[bool]:
    """Return a bool list of length ``len(centroid)`` — True at
    position ``i`` iff ``centroid[i]`` appears in an LCS alignment
    with ``other``."""
    pairs = _lcs_indices(centroid, other)
    mask = [False] * len(centroid)
    for i, _ in pairs:
        mask[i] = True
    return mask


def _intersect_stability(
    centroid: list[str], others: list[list[str]]
) -> list[bool]:
    """For each position in ``centroid``, True iff it is part of an
    LCS alignment with EVERY message in ``others`` — i.e. a stable
    token shared across the entire sample set."""
    if not others:
        return [True] * len(centroid)
    mask = [True] * len(centroid)
    for o in others:
        local = _stable_mask_against_centroid(centroid, o)
        mask = [a and b for a, b in zip(mask, local)]
    return mask


def _pick_centroid(token_lists: list[list[str]]) -> int:
    """Pick the index of the sample whose length is the median — it
    maximises expected LCS overlap with the other samples (shortest
    biases toward trivial matches, longest drags in noise)."""
    lengths = sorted(range(len(token_lists)), key=lambda i: len(token_lists[i]))
    return lengths[len(lengths) // 2]


# ---------------------------------------------------------------------------
# Regex assembly
# ---------------------------------------------------------------------------

def _escape_literal(tok: str) -> str:
    """Escape a literal token for regex embedding.

    Canonicaliser placeholders are reversed into their generative
    regex so the mined pattern matches RAW messages (what the match
    tier actually probes), not canonicalised ones. Every
    expansion is bounded → no ReDoS surface.
    """
    if tok in _PLACEHOLDER_EXPANSION:
        return _PLACEHOLDER_EXPANSION[tok]
    return re.escape(tok)


def _build_regex(centroid: list[str], stable: list[bool]) -> str:
    """Assemble a regex from the centroid tokens + stability mask.

    Stable runs become literal escaped text; unstable runs become a
    bounded ``.{0,N}?`` wildcard. Adjacent unstable positions
    coalesce into a single wildcard to keep the regex compact.
    """
    parts: list[str] = []
    i = 0
    n = len(centroid)
    while i < n:
        if stable[i]:
            # Extend the literal run as long as tokens stay stable.
            j = i
            while j < n and stable[j]:
                parts.append(_escape_literal(centroid[j]))
                j += 1
            i = j
        else:
            # Collapse an unstable run into one bounded wildcard.
            parts.append(f".{{0,{_WILDCARD_BOUND}}}?")
            j = i
            while j < n and not stable[j]:
                j += 1
            i = j
    # Anchor nowhere — the pattern is meant for re.search, so the
    # regex represents the SHAPE of the message, not the whole
    # string. Callers who need anchoring wrap externally.
    return "".join(parts)


def _build_template(centroid: list[str], stable: list[bool]) -> str:
    """Assemble a human-readable template: stable tokens literal,
    unstable runs collapsed to ``<VAR>``."""
    parts: list[str] = []
    i = 0
    n = len(centroid)
    while i < n:
        if stable[i]:
            parts.append(centroid[i])
            i += 1
        else:
            parts.append("<VAR>")
            while i < n and not stable[i]:
                i += 1
    return "".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def propose_leaf(samples: list[str]) -> LeafProposal | None:
    """Mine a narrower regex + template from a set of canonicalised
    messages the bucket tier already served.

    Returns ``None`` if there are too few samples to generalise
    meaningfully, or if the samples share no tokens at all (the
    mined regex would be only wildcards, which adds no precision
    over the bucket catchall itself).

    Callers are expected to:
      1. Collect N ≥ ``MIN_SAMPLES_FOR_PROMOTION`` samples from a
         specific bucket (keyed on ``bucket.signature_hash``).
      2. Call this function with those canonicalised messages.
      3. Register the returned proposal as
         ``scope="leaf"``, ``status="proposed"`` with the bucket's
         mitigations carried over. A promotion policy (not here)
         elevates to ``status="live"`` after further observations.
    """
    if len(samples) < MIN_SAMPLES_FOR_PROMOTION:
        return None

    token_lists = [tokenise(s) for s in samples]
    # Drop empty tokenisations — they can't contribute stability.
    token_lists = [t for t in token_lists if t]
    if len(token_lists) < MIN_SAMPLES_FOR_PROMOTION:
        return None

    centroid_idx = _pick_centroid(token_lists)
    centroid = token_lists[centroid_idx]
    others = [t for i, t in enumerate(token_lists) if i != centroid_idx]
    stable = _intersect_stability(centroid, others)

    # Refuse to emit a pure-wildcard regex — no information gain
    # over the bucket catchall that fed us these samples. Whitespace
    # tokens agreeing across inputs don't count: "a b c" / "x y z"
    # share " " at the same positions but that's structurally
    # meaningless. Require at least one stable NON-whitespace token.
    has_meaningful_stable = any(
        s and not centroid[i].isspace()
        for i, s in enumerate(stable)
    )
    if not has_meaningful_stable:
        return None

    regex = _build_regex(centroid, stable)
    template = _build_template(centroid, stable)
    return LeafProposal(
        regex=regex,
        template=template,
        sample_count=len(samples),
    )


__all__ = [
    "LeafProposal",
    "MIN_SAMPLES_FOR_PROMOTION",
    "propose_leaf",
    "tokenise",
]
