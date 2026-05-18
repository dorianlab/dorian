"""
dorian/dag.py
-------------
Pure-data DAG primitives: Operator, Snippet, Parameter, Edge, Node, DAG.

These classes describe *what* a pipeline is, not *how* to execute it.
Resolution to callables is handled by ``dorian.pipeline.operator_resolver``.
This module has **no dependency** on ``backend`` (no events, no Redis, no config).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from itertools import product
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Sequence,
    Tuple,
    get_args,
)

from classes import typeclass

try:
    from tree_sitter import Tree as _Tree

    _HAS_TREE_SITTER = True
except ImportError:  # pragma: no cover – tree-sitter is optional
    _Tree = None  # type: ignore[assignment,misc]
    _HAS_TREE_SITTER = False

from dorian.languages import SupportedLanguage
from dorian.types import UUID

Keyword = str
Positional = int | list
wildcard = r".*"

SupportedType = Literal["int", "float", "string", "str", "bool", "eval", "env", "state"]
ID = str


# ---------------------------------------------------------------------------
# Pipeline node types (pure data — no __call__)
# ---------------------------------------------------------------------------

@dataclass
class Operator:
    """A library operator (e.g. ``sklearn.preprocessing.StandardScaler``).

    Resolution to a callable is handled by
    ``dorian.pipeline.operator_resolver.resolve()``.
    """
    name: str
    language: str
    tasks: Optional[Sequence[str]] = field(default_factory=list)

    def __repr__(self):
        return f'{self.language}:{self.name}{":" if self.tasks else ""}{";".join(self.tasks)}'

    def __str__(self):
        return repr(self)

    def __hash__(self) -> int:
        return hash(self.__repr__())

    def to_dict(self) -> dict:
        return {
            "class_type": "Operator",
            "name": self.name,
            "language": self.language,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Operator:
        return cls(name=data["name"], language=data["language"])


@dataclass
class Snippet:
    """User-defined inline code block (must define a ``foo(...)`` function).

    Execution is handled by ``dorian.pipeline.operator_resolver.resolve()``.
    """
    name: str
    code: str
    language: str

    def __repr__(self):
        return f"{self.language}:{self.name}"

    def to_dict(self) -> dict:
        return {
            "class_type": "Snippet",
            "name": self.name,
            "code": self.code,
            "language": self.language,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Snippet:
        return cls(name=data["name"], code=data["code"], language=data["language"])


@dataclass
class Parameter:
    """A typed constant value injected into the pipeline graph.

    Evaluation is handled by ``dorian.pipeline.operator_resolver.resolve()``.
    """
    name: str
    dtype: SupportedType
    value: str

    def __repr__(self):
        return f"{self.name}:{self.dtype}:{self.value}"

    def __str__(self):
        return repr(self)

    def __hash__(self) -> int:
        return hash(repr(self))

    def to_dict(self) -> dict:
        return {
            "class_type": "Parameter",
            "name": self.name,
            "dtype": self.dtype,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Parameter:
        return cls(
            name=data["name"],
            dtype=data.get("dtype", data.get("type", "string")),
            value=data["value"],
        )


# ---------------------------------------------------------------------------
# Edge (with type normalisation)
# ---------------------------------------------------------------------------

@dataclass
class Edge:
    source: UUID
    destination: UUID
    position: Positional | Keyword = 0
    output: Positional = 0

    def __post_init__(self):
        """Normalize position and output at construction time.

        JSON deserialization can produce ``"0"`` instead of ``0``.  Coercing
        once here eliminates scattered defensive ``try: int(x)`` blocks.
        Non-numeric strings (keyword arg names like ``"strategy"``) are
        preserved as-is.
        """
        self.position = self._coerce(self.position)
        self.output = self._coerce(self.output, default=0)

    @staticmethod
    def _coerce(val, default=None):
        if val is None:
            return default if default is not None else val
        if isinstance(val, int):
            return val
        try:
            return int(val)
        except (ValueError, TypeError):
            return val  # keyword arg name — keep as str

    def __hash__(self) -> int:
        return hash(f"{self.source}:{self.output}+{self.destination}:{self.position}")

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "destination": self.destination,
            "position": self.position,
            "output": self.output,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Edge:
        return cls(
            source=data["source"],
            destination=data["destination"],
            position=data.get("position", 0),
            output=data.get("output", 0),
        )


# ---------------------------------------------------------------------------
# Node (pattern matching for rewrite rules only)
# ---------------------------------------------------------------------------

@dataclass
class Node:
    type: str = wildcard
    text: str = wildcard
    language: str = field(default=wildcard)

    def __post_init__(self):
        if self.language != wildcard and self.language not in get_args(SupportedLanguage):
            raise ValueError(f"language must be one of {get_args(SupportedLanguage)} or wildcard ('{wildcard}'), got '{self.language}'")

    def __hash__(self) -> int:
        return hash(f"{self.language}:{self.type}:{self.text}")

    def to_dict(self) -> dict:
        return {
            "class_type": "Node",
            "type": self.type,
            "text": self.text.replace("\n", ""),
            "language": self.language,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Node:
        return cls(
            type=data.get("type", wildcard),
            text=data.get("text", wildcard).replace("\n", ""),
            language=data.get("language", "python"),
        )


@dataclass
class IOMapping:
    """Maps an external handle on a Group to an internal node's port.

    When a Group is collapsed, external edges connect to the Group node.
    At execution time the Group is flattened and these mappings rewire
    external edges to the correct internal child node and handle.
    """
    direction: Literal["input", "output"]
    internal_node_id: str
    internal_handle: str | int

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "internalNodeId": self.internal_node_id,
            "internalHandle": self.internal_handle,
        }

    @classmethod
    def from_dict(cls, data: dict) -> IOMapping:
        return cls(
            direction=data["direction"],
            internal_node_id=data.get("internalNodeId", data.get("internal_node_id", "")),
            internal_handle=Edge._coerce(
                data.get("internalHandle", data.get("internal_handle", 0))
            ),
        )


@dataclass
class Group:
    """Collapsible container around a compound operator's sub-DAG.

    A Group is created by the backend when a compound operator (e.g.
    ``sklearn.preprocessing.StandardScaler``, ``openrouter.chat.completion``)
    is dropped onto the canvas.  The frontend renders it as a single node
    (collapsed) or a bounding box around its children (expanded).

    At execution time, ``_parse_pipeline`` flattens Groups: children are
    promoted to top-level DAG nodes, internal edges are added, and external
    edges are rewired via ``io_map``.
    """
    name: str
    children: Dict[str, dict] = field(default_factory=dict)
    internal_edges: List[Edge] = field(default_factory=list)
    io_map: Dict[str, IOMapping] = field(default_factory=dict)
    collapsed: bool = True
    source_interface: str = ""
    source_pipeline_id: str = ""

    def __repr__(self):
        n_children = len(self.children)
        return f"Group({self.name}, {n_children} children)"

    def __hash__(self) -> int:
        return hash(f"Group:{self.name}:{id(self)}")

    def to_dict(self) -> dict:
        return {
            "class_type": "Group",
            "name": self.name,
            "children": self.children,
            "internalEdges": [e.to_dict() for e in self.internal_edges],
            "ioMap": {k: v.to_dict() for k, v in self.io_map.items()},
            "collapsed": self.collapsed,
            "sourceInterface": self.source_interface,
            "sourcePipelineId": self.source_pipeline_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Group:
        internal_edges = [
            Edge.from_dict(e)
            for e in data.get("internalEdges", data.get("internal_edges", []))
        ]
        raw_io = data.get("ioMap", data.get("io_map", {}))
        io_map = {k: IOMapping.from_dict(v) for k, v in raw_io.items()}
        return cls(
            name=data["name"],
            children=data.get("children", {}),
            internal_edges=internal_edges,
            io_map=io_map,
            collapsed=data.get("collapsed", True),
            source_interface=data.get("sourceInterface", data.get("source_interface", "")),
            source_pipeline_id=data.get("sourcePipelineId", data.get("source_pipeline_id", "")),
        )


@dataclass
class LogicalTask:
    """Placeholder for a not-yet-bound operator slot.

    Templates are regular DAGs where some operator positions are
    occupied by ``LogicalTask`` nodes instead of concrete
    ``Operator`` nodes. ``path`` is the canonical hierarchical
    task name from the KB taxonomy — e.g.
    ``("Preprocessing", "Imputation", "Mean")`` for a specific
    method, or ``("Preprocessing", "Imputation")`` for the broader
    category that matches every imputer the KB catalogues.

    A template with LogicalTask nodes is NOT executable. It must
    be bound first via the AutoML/RL binding step which queries the
    KB (`operators_for_task(canonical_path)`), picks a concrete
    operator per slot, and replaces each LogicalTask with the
    chosen Operator + its hyperparameter Parameters. The runner
    rejects any DAG that still contains LogicalTask nodes.

    Storage: same `pipelines` table as concrete pipelines, with
    the resolver/operator-resolver path special-casing LogicalTask
    by raising rather than dispatching.
    """
    path: tuple[str, ...]
    """Canonical hierarchical task name. Top of the tree first.
    Empty path means "any operator" (matches anything in the KB)."""

    name: str = ""
    """Human-readable label for the slot. Defaults to the
    dot-joined path. Used in canvas rendering and BO log lines."""

    def __post_init__(self) -> None:
        # Coerce path into a tuple — JSON deserialisation produces
        # a list, and we need it hashable for cache keys + dict use.
        if isinstance(self.path, list):
            self.path = tuple(self.path)
        if not self.name:
            self.name = ".".join(self.path) if self.path else "<any>"

    def __repr__(self) -> str:
        return f"LogicalTask({'.'.join(self.path) or '<any>'})"

    def __hash__(self) -> int:
        return hash(("LogicalTask", self.path))

    @property
    def depth(self) -> int:
        return len(self.path)

    def matches(self, other_path: tuple[str, ...] | list[str]) -> bool:
        """True when ``other_path`` is a refinement of (or equal to)
        ``self.path``. ``("Preprocessing",)`` matches
        ``("Preprocessing", "Imputation", "Mean")`` because the
        latter is a more specific descendant. Empty self-path
        matches everything.
        """
        other = tuple(other_path)
        if not self.path:
            return True
        if len(other) < len(self.path):
            return False
        return other[: len(self.path)] == self.path

    def to_dict(self) -> dict:
        return {
            "class_type": "LogicalTask",
            "path": list(self.path),
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, data: dict) -> LogicalTask:
        return cls(
            path=tuple(data.get("path") or ()),
            name=data.get("name", ""),
        )


Nodes = Node | Snippet | Operator | Parameter | Group | LogicalTask


# ---------------------------------------------------------------------------
# DAG
# ---------------------------------------------------------------------------

@dataclass
class DAG:
    nodes: Dict[UUID, Nodes] = field(default_factory=dict)
    edges: List[Edge] = field(default_factory=list)

    def __iter__(self):
        yield from self.nodes.items()

    def __len__(self):
        return len(self.nodes.keys())

    # -- template-vs-concrete distinction ----------------------------------

    @property
    def is_template(self) -> bool:
        """True when at least one node is a `LogicalTask` placeholder.
        Templates are not directly executable — they must be bound to
        concrete operators via the AutoML/RL binding step first.
        Concrete pipelines (`is_template == False`) flow through the
        runner unchanged."""
        return any(isinstance(n, LogicalTask) for n in self.nodes.values())

    def logical_task_nodes(self) -> List[tuple[UUID, "LogicalTask"]]:
        """Iterate `(node_id, LogicalTask)` pairs. Order is the
        DAG's nodes-dict insertion order. Used by binders to walk
        every slot needing assignment."""
        return [
            (nid, node)
            for nid, node in self.nodes.items()
            if isinstance(node, LogicalTask)
        ]

    # -- serialization -----------------------------------------------------

    def to_json_dict(self) -> dict:
        """Convert DAG to a JSON-serializable dictionary."""
        return {
            "version": "1.0",
            "metadata": {
                "created_at": datetime.now().isoformat(),
                "node_count": len(self.nodes),
                "edge_count": len(self.edges),
                "class_types": list(
                    set(_class_of(node) for node in self.nodes.values())
                ),
            },
            "nodes": {
                node_id: node.to_dict() for node_id, node in self.nodes.items()
            },
            "edges": [edge.to_dict() for edge in self.edges],
        }

    # Mapping from DAG class_type → ReactFlow node type.
    _CT_MAP = {"Operator": "operator", "Parameter": "parameter", "Snippet": "snippet", "Group": "group"}

    def to_frontend_dict(self) -> dict:
        """Convert DAG to the format expected by the React frontend.

        Same as :meth:`to_json_dict` but remaps ``class_type`` to the
        ReactFlow ``type`` key (lowercase: operator / parameter / snippet).
        Use this instead of ``to_json_dict()`` whenever the output is sent
        to the frontend via WebSocket or REST.
        """
        d = self.to_json_dict()
        for _nd in d.get("nodes", {}).values():
            ct = _nd.pop("class_type", "Operator")
            _nd["type"] = self._CT_MAP.get(ct, ct.lower())
        return d

    def save(self, filename: str) -> None:
        """Save DAG to a JSON file."""
        dag_data = self.to_json_dict()
        if not filename.endswith(".json"):
            filename += ".json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(dag_data, f, indent=2, ensure_ascii=False)

    @staticmethod
    def load(filename: str = "", data: dict | None = None) -> DAG:
        """Load DAG from a JSON file or dict."""
        if filename and data:
            raise ValueError("Provide either filename or data, not both.")
        if not filename and not data:
            raise ValueError("Either filename or data must be provided.")
        if not data:
            if not filename.endswith(".json"):
                filename += ".json"
            with open(filename, "r", encoding="utf-8") as f:
                data = json.load(f)
        return DAG.from_json_dict(data)  # type: ignore[arg-type]

    @staticmethod
    def from_json_dict(data: dict) -> DAG:
        """Create DAG from a JSON dictionary."""

        def _deserialize_node(node_data: dict) -> Nodes:
            ct = node_data.get("class_type", "Node")
            if ct == "Node":
                return Node.from_dict(node_data)
            if ct == "Operator":
                return Operator.from_dict(node_data)
            if ct == "Parameter":
                return Parameter.from_dict(node_data)
            if ct == "Snippet":
                return Snippet.from_dict(node_data)
            if ct == "Group":
                return Group.from_dict(node_data)
            if ct == "LogicalTask":
                return LogicalTask.from_dict(node_data)
            raise ValueError(f"Unknown node class_type: {ct}")

        nodes = {
            nid: _deserialize_node(nd)
            for nid, nd in data.get("nodes", {}).items()
        }
        edges = [Edge.from_dict(ed) for ed in data.get("edges", [])]
        return DAG(nodes=nodes, edges=edges)

    # -- graph operations --------------------------------------------------

    def _get_subgraph(self, root: UUID) -> DAG:
        """Extract the sub-DAG reachable from *root* via forward edges."""
        nodes: Dict[UUID, Nodes] = {}
        edges: List[Edge] = []

        def _traverse(nid: UUID) -> None:
            nodes[nid] = self.nodes[nid]
            for edge in self.edges:
                if edge.source != nid:
                    continue
                edges.append(edge)
                _traverse(edge.destination)

        _traverse(root)
        return DAG(nodes=nodes, edges=edges)

    def split_line_based(self) -> List[DAG]:
        """Split a top-level DAG into per-statement sub-DAGs.

        Assumes that the root node (id ``"0"``) is a ``module`` node whose
        children are the top-level statements.  Each child becomes the root
        of an independent sub-DAG.
        """
        root_ids = [
            str(i)
            for i in sorted(
                int(e.destination) for e in self.edges if e.source == "0"
            )
        ]
        return [self._get_subgraph(rid) for rid in root_ids]

    @staticmethod
    def merge(subdags: Sequence[DAG]) -> DAG:
        """Merge multiple sub-DAGs into one."""
        nodes: Dict[UUID, Nodes] = {}
        edges: List[Edge] = []
        for dag in subdags:
            nodes.update(dag.nodes)
            edges.extend(dag.edges)
        return DAG(nodes=nodes, edges=edges)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _class_of(obj: Any) -> str:
    """Return the class name of *obj* (e.g. ``'Operator'``, ``'Node'``)."""
    return obj.__class__.__name__


def has_single_value(_list: Sequence[Any]) -> bool:
    """Return True if every element of *_list* is identical."""
    return len(set(_list)) == 1


# ---------------------------------------------------------------------------
# Pattern matching (code-parsing layer)
# ---------------------------------------------------------------------------

def comparator(one: Nodes, another: Node) -> bool:
    """Compare a DAG node (*one*) against a pattern node (*another*).

    Used by :func:`match` to decide whether a concrete node satisfies a
    pattern constraint.  Supports ``Node``, ``Operator``, and ``Parameter``
    as the concrete node type; the pattern is always a ``Node``.
    """
    # ``another`` is the pattern node, ``one`` is the concrete DAG node.
    # All three attribute comparisons are regex — pattern values are
    # treated as anchored-at-start regex patterns matched against the
    # concrete node's attribute. An empty/missing pattern attribute is
    # treated as a wildcard (always matches). The pipeline-layer twin
    # at ``dorian/pipeline/parser.py::comparator`` has the same
    # semantics; keep them in sync when touching either.
    def _lang_match(pat: str, concrete: str) -> bool:
        return True if not pat else bool(re.match(pat, concrete or ""))

    match one, another:
        case Node(), Node():
            is_type_matched = (
                True if not another.type else bool(re.match(another.type, one.type))
            )
            is_text_matched = (
                True if not another.text else bool(re.match(another.text, one.text))
            )
            return (
                _lang_match(another.language, one.language)
                and is_type_matched
                and is_text_matched
            )
        case Operator(), Node():
            return (
                bool(re.match(another.type, "Operator"))
                and bool(re.match(another.text, one.name))
                and _lang_match(another.language, one.language)
            )
        case Parameter(), Node():
            # Parity with the Operator branch above: pattern.type must
            # regex-match the concrete class name "Parameter" (so a
            # pattern typed as e.g. ``expression_statement`` doesn't
            # accidentally match Parameters through the permissive
            # empty-text default). Prior behaviour checked only text,
            # which made the Parameter branch the silent "wildcard"
            # case of the comparator — any Node-typed pattern would
            # match every Parameter.
            return bool(re.match(another.type, "Parameter")) and bool(re.match(another.text, "Parameter"))
        case Snippet(), Node():
            # Same shape as Operator/Parameter: pattern.type must
            # regex-match "Snippet". Without this branch, any rule
            # whose pattern fires after a Snippet has been emitted
            # (subscript→Snippet conversion, guardrail passthrough,
            # composer wrappers) crashes pattern matching with
            # "Cannot compare Snippet and Node".
            return (
                bool(re.match(another.type, "Snippet"))
                and bool(re.match(another.text, one.name or "Snippet"))
                and _lang_match(another.language, one.language)
            )
        case first, second:
            raise ValueError(
                f"Cannot compare {type(first).__name__} and {type(second).__name__}"
            )


def match(
    pattern: DAG,
    dag: DAG,
    comp: Callable[[Nodes, Node], bool] = comparator,
    processed: list | None = None,
) -> Tuple[bool, Dict[UUID, UUID] | None]:
    """Match a rule *pattern* against *dag* and return a candidate mapping.

    Returns ``(True, mapping)`` on the first valid match, or
    ``(False, None)`` if no match exists.

    *processed* is an optional list of previously returned candidates
    (used by ``sync_apply`` to avoid re-matching the same node).
    """

    def _iter(elements, element, _comparator):
        for idx, el in elements:
            if _comparator(el, element):
                yield idx

    for values in product(
        *map(
            lambda x: _iter(dag.nodes.items(), x, comp),
            pattern.nodes.values(),
        )
    ):
        if len(values) != len(set(values)):
            continue

        candidate = dict(zip(pattern.nodes.keys(), values))

        # Skip candidates we've already processed.
        if processed is not None and candidate in processed:
            continue

        matched = 0
        for edge in pattern.edges:
            s, d = candidate[edge.source], candidate[edge.destination]
            for _edge in dag.edges:
                if (_edge.source == s) & (_edge.destination == d):
                    matched += 1
                    break

        if matched == len(pattern.edges):
            return True, candidate

    return False, None


# ---------------------------------------------------------------------------
# Tree-sitter AST → DAG conversion (typeclass)
# ---------------------------------------------------------------------------

@typeclass
def to_dag(instance, language: str) -> DAG:  # type: ignore[empty-body]
    """Convert a tree-sitter ``Tree`` (or similar) to a :class:`DAG`."""


if _HAS_TREE_SITTER:

    @to_dag.instance(_Tree)
    def _tree_to_dag(instance: _Tree, language: str) -> DAG:
        """DFS traversal of a tree-sitter Tree, producing incrementing int IDs."""
        _nodes: Dict[str, Node] = {}
        _edges: List[Edge] = []

        def _traverse(n, parent: int | None = None) -> None:
            _id = len(_nodes)
            if parent is not None:
                _edges.append(Edge(str(parent), str(_id), position=0))
            _nodes[str(_id)] = Node(
                type=n.type,
                text=n.text.decode("utf-8"),
                language=language,
            )
            for child in n.children:
                _traverse(child, _id)

        _traverse(instance.root_node)
        return DAG(nodes=_nodes, edges=_edges)
