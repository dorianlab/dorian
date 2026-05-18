"""
dorian/code/parsing/rule.py
----------------------------
Canonical rewrite-rule primitives used by both the code-parsing layer
(AST → DAG) and the pipeline execution layer (DAG → DAG pre-execution).

Import from here for all rewrite rules:

    from dorian.code.parsing.rule import Apply, RewriteRule, PurgeMode, Add

History
-------
Previously a near-identical copy lived at ``dorian/pipeline/rule.py``.
That file has been removed; all pipeline code now imports from this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Sequence, Tuple, Any
from aenum import StrEnum, Enum
from uuid import uuid4, UUID

from dorian.dag import DAG, ID, Node, Edge
from dorian.languages import SupportedLanguage


# ---------------------------------------------------------------------------
# Priority (informational; not yet enforced by the scheduler)
# ---------------------------------------------------------------------------

class Priority(Enum):
    Low    = 0
    Medium = 50
    High   = 100

    def __repr__(self) -> str:
        return self.name


# ---------------------------------------------------------------------------
# PurgeMode
# ---------------------------------------------------------------------------

PurgeMode = StrEnum("PurgeMode", "recursive isolated")

Pattern = DAG


# ---------------------------------------------------------------------------
# Transformation primitives
# ---------------------------------------------------------------------------

@dataclass
class Add:
    """Insert new nodes and/or edges into the DAG.

    ``nodes`` accepts either:
    - ``Sequence[Node]`` — anonymous nodes (legacy, auto-assigned UUIDs)
    - ``Dict[str, Any]``  — **named** nodes: local-ID → node object.
      Named nodes can be referenced by their local ID in ``edges``
      and in subsequent transformations via the extended mapping.

    ``edges`` accepts either:
    - ``Tuple[ID, ID]``  — (source, dest) resolved via mapping
    - ``Edge`` objects   — rich edges with ``position`` / ``output``
    """
    nodes: Dict[str, Any] | Sequence[Node] | None = None
    edges: Sequence[Tuple[ID, ID] | Edge] | None = None


@dataclass
class Apply:
    """Apply an arbitrary function to the DAG.

    Calling convention (3 positional arguments)::

        f(dag: DAG, mapping: dict, meta: dict) -> DAG

    ``mapping`` maps pattern node keys to matched DAG node IDs.
    ``meta`` carries session context (e.g. ``meta["session"]``).

    Note: the type annotation uses ``Callable[..., DAG]`` to avoid coupling
    the core library to specific meta-dict shapes or event types.
    """
    f: Callable[..., DAG]


@dataclass
class Replace:
    pass


@dataclass
class Delete:
    """Remove nodes and/or edges, with optional recursive purging."""
    nodes: Sequence[ID] = field(default_factory=list)
    edges: Sequence[Tuple[ID, ID]] = field(default_factory=list)
    mode: str = PurgeMode.isolated


@dataclass
class Revert:
    """Revert a set of nodes/edges to an earlier DAG state."""
    nodes: Sequence[ID] = field(default_factory=list)
    edges: Sequence[Tuple[ID, ID]] = field(default_factory=list)


@dataclass
class ToOperator:
    """Promote a node to an Operator node (code-parsing use case)."""
    nid: UUID
    content: UUID


@dataclass
class ToParameter:
    """Promote a node to a Parameter node (code-parsing use case)."""
    nid: UUID
    kw: UUID
    value: UUID


Transformation = Add | Apply | Replace | Delete | Revert | ToOperator | ToParameter


# ---------------------------------------------------------------------------
# RewriteRule
# ---------------------------------------------------------------------------

@dataclass
class RewriteRule:
    """A rewrite rule: match a sub-DAG pattern, then apply transformations.

    Attributes
    ----------
    pattern:
        A DAG whose nodes carry ``type``/``text`` constraints used by
        ``match()`` (from ``dorian.pipeline.parser``) to find candidates.
    description:
        Human-readable summary, used in ``__repr__`` and log messages.
    emit:
        Optional callable called after each successful match to produce
        side-effect events.  Signature: ``(dag, mapping) -> Sequence[Any]``.
        Defaults to a no-op that returns ``[]``.
    transformations:
        Ordered sequence of transformation steps applied to the matched DAG.
    rules:
        Dynamic sub-rules generated at match time.
    handle_in_parallel:
        Hint to the scheduler: whether multiple matches may be processed
        concurrently (default ``True``).
    ID:
        Stable rule identity string, auto-generated from ``__repr__`` + UUID.
    """
    pattern: Pattern
    description: str = field(default_factory=str)
    emit: Callable[..., Sequence[Any]] = field(
        default_factory=lambda: (lambda g, m: [])
    )
    transformations: Sequence[Transformation] = field(default_factory=list)
    rules: Sequence[Callable[[DAG, Dict[str, str]], "RewriteRule"]] = field(
        default_factory=list
    )
    handle_in_parallel: bool = True

    def __post_init__(self) -> None:
        self.ID = self.__repr__() + "\n" + str(uuid4())

    def __repr__(self) -> str:
        types = ", ".join(
            str(getattr(v, "type", ""))
            for v in self.pattern.nodes.values()
            if hasattr(v, "type") and getattr(v, "type", None)
        )
        texts = ", ".join(
            str(getattr(v, "text", ""))
            for v in self.pattern.nodes.values()
            if hasattr(v, "text") and getattr(v, "text", None)
        )
        transformations = ", ".join(tr.__class__.__name__ for tr in self.transformations)
        rules = ", ".join(r.__class__.__name__ for r in self.rules)
        return (
            f"Rule that {self.description.lower()}\n"
            f"types: [{types}]\ntexts: [{texts}]\n"
            f"transformations: [{transformations}]\nrules: [{rules}]"
        )

    def __str__(self) -> str:
        return repr(self)
