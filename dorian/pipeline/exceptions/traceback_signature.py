"""Traceback signature extractor.

Turns a raised Python exception + its traceback into a stable,
content-addressable ``TracebackSignature`` that different
occurrences of the same fundamental error produce identically.

Five fields feed the signature (see
(internal design note; not in public repo) § "Traceback signature"):

  * ``exception_type``     -- class name of the raised exception
  * ``operator_fqn``       -- most specific Dorian-operator frame
  * ``site_library``       -- deepest library module (e.g.
                              ``pandas.core.indexes``)
  * ``message_template``   -- exception message with variable
                              substitutions replaced (``'col_xyz'``
                              -> ``'<STR>'``, ``42`` -> ``<NUM>``,
                              paths -> ``<PATH>``)
  * ``user_frame_depth``   -- steps from the user-operator frame
                              to the raise site

The signature hash is SHA-256 over the field-sorted
canonicalised form; two signatures are equal iff every field
matches.
"""
from __future__ import annotations

import hashlib
import re
import traceback
from dataclasses import dataclass
from types import TracebackType
from typing import Any


# ---------------------------------------------------------------------------
# Message canonicalisation
# ---------------------------------------------------------------------------

# Order matters: match longer structures first so e.g. a path
# containing digits isn't split by the number regex.
_MESSAGE_SUBSTITUTIONS: list[tuple[re.Pattern[str], str]] = [
    # Absolute Windows paths ("C:\...\file.py").
    (re.compile(r"[a-zA-Z]:\\\\?(?:[^\s'\"]+\\\\?)+[^\s'\"]+"), "<PATH>"),
    # Absolute POSIX paths ("/foo/bar/baz.csv").
    (re.compile(r"(?<![\w'])/(?:[^\s'\"/]+/)+[^\s'\"]+"), "<PATH>"),
    # Double-quoted strings.
    (re.compile(r'"[^"]*"'), "'<STR>'"),
    # Single-quoted strings.
    (re.compile(r"'[^']*'"), "'<STR>'"),
    # Hexadecimal object addresses / ids. Must precede the numeric
    # patterns so "0xDEADBEEF" isn't first chewed by the int regex
    # on the leading "0".
    (re.compile(r"0x[0-9a-fA-F]+"), "<HEX>"),
    # Floats (incl. negatives). Use lookbehind/lookahead so the
    # leading `-` is consumed as part of the match (\b won't work
    # because "-" is non-word and `\b-` never binds).
    (re.compile(r"(?<![\w.])-?\d+\.\d+(?![\w.])"), "<NUM>"),
    # Integers (incl. negatives).
    (re.compile(r"(?<![\w.])-?\d+(?![\w.])"), "<NUM>"),
]


def canonicalise_message(msg: str) -> str:
    """Replace variable-looking substrings with stable placeholders
    so the same error shape always produces the same template."""
    for pat, repl in _MESSAGE_SUBSTITUTIONS:
        msg = pat.sub(repl, msg)
    # Collapse whitespace runs for visual stability.
    msg = re.sub(r"\s+", " ", msg).strip()
    return msg


# ---------------------------------------------------------------------------
# Frame inspection
# ---------------------------------------------------------------------------

_DORIAN_OPERATOR_PREFIXES: tuple[str, ...] = (
    "dorian.",
    "sklearn.",
    "pandas.",
    "numpy.",
    "scipy.",
    "torch.",
    "openrouter.",
    "trust_guardrails.",
)


@dataclass(frozen=True)
class FrameSummary:
    """One Python traceback frame reduced to the bits we key on."""

    module: str
    function: str
    line: int
    is_dorian_operator: bool


def _summarise_frames(tb: TracebackType | None) -> list[FrameSummary]:
    out: list[FrameSummary] = []
    for frame, lineno in traceback.walk_tb(tb):
        mod = frame.f_globals.get("__name__", "")
        out.append(
            FrameSummary(
                module=mod,
                function=frame.f_code.co_name,
                line=lineno,
                is_dorian_operator=any(
                    mod.startswith(p) for p in _DORIAN_OPERATOR_PREFIXES
                ),
            )
        )
    return out


def _pick_operator_frame(frames: list[FrameSummary]) -> FrameSummary | None:
    """Most specific Dorian-operator-space frame. Rationale: we want
    the Dorian node most likely to be the culprit -- which is the
    LAST operator-prefixed frame before the raise site, since by
    then we've descended from user code into the library that blew
    up."""
    last: FrameSummary | None = None
    for f in frames:
        if f.is_dorian_operator:
            last = f
    return last


def _site_library(frames: list[FrameSummary]) -> str:
    """Top-level package of the deepest frame in the traceback
    (typically the library that raised)."""
    if not frames:
        return ""
    deepest = frames[-1]
    mod = deepest.module
    parts = mod.split(".")
    if len(parts) >= 3:
        return ".".join(parts[:3])
    return mod


def _user_frame_depth(frames: list[FrameSummary]) -> int:
    """Number of frames after the last Dorian-operator frame
    (inclusive of the raise site). Zero if no operator frame is
    present."""
    if not frames:
        return 0
    last_op = None
    for i, f in enumerate(frames):
        if f.is_dorian_operator:
            last_op = i
    if last_op is None:
        return 0
    return len(frames) - last_op - 1


# ---------------------------------------------------------------------------
# TracebackSignature
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TracebackSignature:
    """Stable, content-addressable identifier for an exception.

    Two signatures are equal iff all fields match. The hash is
    SHA-256 over the canonicalised form (field-sorted dict,
    JSON-free serialisation) for compact storage + Redis keys.
    """

    exception_type: str
    operator_fqn: str
    site_library: str
    message_template: str
    user_frame_depth: int

    def hash_hex(self) -> str:
        h = hashlib.sha256()
        parts = [
            ("exception_type", self.exception_type),
            ("message_template", self.message_template),
            ("operator_fqn", self.operator_fqn),
            ("site_library", self.site_library),
            ("user_frame_depth", str(self.user_frame_depth)),
        ]
        # Keys sorted alphabetically for determinism regardless of
        # dataclass field order.
        for k, v in sorted(parts):
            h.update(k.encode("utf-8"))
            h.update(b"\x00")
            h.update(v.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "exception_type": self.exception_type,
            "operator_fqn": self.operator_fqn,
            "site_library": self.site_library,
            "message_template": self.message_template,
            "user_frame_depth": self.user_frame_depth,
            "hash": self.hash_hex(),
        }


def extract(exc: BaseException) -> TracebackSignature:
    """Extract a signature from a live exception.

    The caller has already captured the exception (typically inside
    an ``except`` block); this function inspects ``exc`` +
    ``exc.__traceback__`` and produces the signature.
    """
    frames = _summarise_frames(exc.__traceback__)
    op_frame = _pick_operator_frame(frames)
    operator_fqn = ""
    if op_frame is not None:
        operator_fqn = f"{op_frame.module}.{op_frame.function}"
    return TracebackSignature(
        exception_type=type(exc).__name__,
        operator_fqn=operator_fqn,
        site_library=_site_library(frames),
        message_template=canonicalise_message(str(exc)),
        user_frame_depth=_user_frame_depth(frames),
    )


__all__ = [
    "FrameSummary",
    "TracebackSignature",
    "canonicalise_message",
    "extract",
]
