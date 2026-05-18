import json
import os
import re
from typing import Callable, Sequence, Dict, Any, List, Tuple, Iterator
from functools import reduce, wraps
from classes import typeclass
from itertools import product, combinations
from collections import defaultdict, deque
import inspect
import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from dorian.dag import (
    DAG,
    Node,
    Edge,
    Operator,
    Parameter,
    Snippet,
)
from dorian.dag import ID

from dorian.code.parsing.rule import (
    RewriteRule,
    Transformation,
    Add,
    Apply,
    Replace,
    Delete,
    PurgeMode,
)
from .rules import Rules

from backend.events import Event, verbose, aemit

Candidate = Dict[ID, ID]

def _handle_deleted_nodes(dag: DAG, nodes: List[ID]) -> DAG:
    # Deletes nodes and the edges connected to then.
    # Note: when two nodes are connected via another node, after deleting the middle node,
    # the other two nodes are made connected directly.
    to_remove, to_add = [], []
    for nid in nodes:
        sources = [e.source for e in dag.edges if e.destination == nid]
        destinations = [e.destination for e in dag.edges if e.source == nid]
        if sources:
            to_remove.extend([(s, nid) for s in sources])
        if destinations:
            to_remove.extend([(nid, d) for d in destinations])
        if sources and destinations:
            to_add.extend(
                [
                    Edge(source=s, destination=d)
                    for s, d in product(sources, destinations)
                ]
            )
    return DAG(
        nodes=dict((k, v) for k, v in dag.nodes.items() if k not in nodes),
        # Is it possible to add repetitive edges here? when connecting two nodes together after removing the connection.
        edges=[
            e for e in dag.edges + to_add if (e.source, e.destination) not in to_remove
        ],
    )


async def _rewrite(
    dag: DAG, mapping: Dict[ID, ID], transformation: Transformation, meta: Dict[str, Any]
) -> tuple[DAG, Dict[ID, ID]]:
    """Apply a single transformation, returning (new_dag, updated_mapping).

    The mapping is extended when ``Add`` creates named nodes so that
    subsequent transformations can reference those nodes by local ID.
    """
    match transformation:
        case Add():
            _nodes = {}
            # Extend mapping so edges can reference both pattern nodes
            # AND newly-added nodes by their local ID.
            _local = dict(mapping)

            if isinstance(transformation.nodes, dict):
                # Named nodes: local_id → node object (can be referenced in edges)
                for local_id, node in transformation.nodes.items():
                    uid = str(uuid4())
                    _nodes[uid] = node
                    _local[local_id] = uid
            elif transformation.nodes:
                # Legacy anonymous nodes (auto-assigned UUIDs)
                for node in transformation.nodes:
                    _nodes[str(uuid4())] = node

            _edges = []
            if transformation.edges:
                for e in transformation.edges:
                    if isinstance(e, Edge):
                        _edges.append(Edge(
                            source=_local.get(e.source, e.source),
                            destination=_local.get(e.destination, e.destination),
                            position=e.position,
                            output=e.output,
                        ))
                    else:
                        _edges.append(Edge(
                            source=_local.get(e[0], e[0]),
                            destination=_local.get(e[1], e[1]),
                        ))

            return DAG(nodes=dict(dag.nodes, **_nodes), edges=dag.edges + _edges), _local
        case Apply():
            if inspect.iscoroutinefunction(transformation.f):
                result = await transformation.f(dag, mapping, meta)
            else:
                # If transformation.f is not a coroutine, run it in a thread pool to keep async interface
                loop = asyncio.get_running_loop()
                with ThreadPoolExecutor() as pool:
                    result = await loop.run_in_executor(pool, transformation.f, dag, mapping, meta)
            return result, mapping
        case Replace():
            # Deletes the whole dag
            return DAG(nodes={}, edges=[]), mapping
        case Delete():
            # Delete nodes separately or recursively. Delete edges one by one
            mapped_nodes = (
                list(map(lambda x: mapping[x], transformation.nodes))
                if transformation.nodes
                else []
            )
            if mapped_nodes:
                match transformation.mode:
                    case PurgeMode.recursive:
                        # Copies the mapped_nodes
                        queue = mapped_nodes[:]
                        while queue:
                            nid = queue.pop()
                            added = [
                                e.destination for e in dag.edges if e.source == nid
                            ]
                            mapped_nodes.extend(added)
                            queue.extend(added)
                    case PurgeMode.isolated:
                        dag = _handle_deleted_nodes(dag, mapped_nodes)
            _nodes = dict((k, v) for k, v in dag.nodes.items() if k not in mapped_nodes)
            foo = lambda x: (mapping[x[0]], mapping[x[1]])
            to_remove = (
                list(map(foo, transformation.edges)) if transformation.edges else []
            )
            _edges = [
                e
                for e in dag.edges
                if ((e.source, e.destination) not in to_remove)
                and (e.source not in mapped_nodes)
                and (e.destination not in mapped_nodes)
            ]
            return DAG(nodes=_nodes, edges=_edges), mapping
        # case Revert():
        #     edges = list(filter(lambda x: Edge(source=mapping[x[0]], destination=mapping[x[1]]) in dag.edges, combinations(transformation.nodes, 2)))
        #     to_remove = [
        #         Edge(source=mapping[e[0]], destination=mapping[e[1]])
        #         for e in transformation.edges + edges
        #     ]
        #     to_add = [
        #         Edge(source=mapping[e[1]], destination=mapping[e[0]])
        #         for e in transformation.edges + edges
        #     ]
        #     return DAG(
        #         nodes=dag.nodes,
        #         edges=list(set([e for e in dag.edges if e not in to_remove] + to_add)),
        #     )
        # case ToOperator():
        #     # Populates the Operator instance's attributes with the string or another node's attribute, which is located by "Where" class.
        #     # Then, transforms the node with ID transformation.nid to the just created Operator.
        #     op = dict( (k, getattr(dag.nodes[mapping[transformation.content]], v)) for k, v in {"name": "text", "language": "language"}.items() )
        #     nid = mapping[transformation.nid]
        #     return DAG(nodes=dict(dag.nodes, **{nid: Operator(**op)}), edges=dag.edges)
        # case ToParameter():
        #     nid = mapping[transformation.nid]
        #     language = dag.nodes[nid].language
        #     kw = dag.nodes[mapping[transformation.kw]].text
        #     value = dag.nodes[mapping[transformation.value]]
        #     return DAG(nodes=dict(dag.nodes, **{nid: Parameter(name=kw, language=language, type=value.type, value=value.text)}), edges=dag.edges)
        case unknown:
            raise NotImplementedError(f'Unknown transformation "{unknown}" in f: rewrite')


# @summarize(origin='rewrite.py#L144', types=["dict"])
async def rewrite(
    dag: DAG,
    mapping: Dict[ID, ID],
    transformations: Sequence[Transformation],
    meta: Dict[str, Any]
) -> DAG:
    result = dag
    current_mapping = mapping
    for tf in transformations:
        result, current_mapping = await _rewrite(result, current_mapping, tf, meta)
    return result


Nodes = Node | Operator | Parameter | Snippet

# Pre-compiled regex cache — avoids re-compiling the same pattern string on
# every call to ``comparator()`` (hot path during sync_apply loops).
_re_cache: dict[str, re.Pattern] = {}


def _re_match(pattern_str: str, text: str) -> bool:
    """Cached ``re.match``."""
    pat = _re_cache.get(pattern_str)
    if pat is None:
        pat = re.compile(pattern_str)
        _re_cache[pattern_str] = pat
    return pat.match(text) is not None


def comparator(one: Nodes, another: Nodes) -> bool:
    """Compares a DAG node against another node or pattern"""
    # Another: rule's nodes
    # One: Graph's nodes
    match one, another:
        # Here partial string matching is performed too, because of match func.
        # re.match() checks for a match only at the beginning of the string
        # We try to find the text of rule's node's text at the beginning of graph's node's text
        case Node(), Node():
            is_type_matched = (
                True
                if not another.type
                else _re_match(another.type, one.type)
            )
            is_text_matched = (
                True
                if not another.text
                else _re_match(another.text, one.text)
            )

            return (
                (one.language == another.language)
                & is_type_matched & is_text_matched
            )
        case Operator(), Node():
            return _re_match(another.type, 'Operator') & _re_match(another.text, one.name) & _re_match(another.language, one.language)
        case Parameter(), Node():
            return _re_match(another.type, 'Parameter')
        case Snippet(), _:
            return False
        case first, second:
            verbose(Event("UnknownComparison", {'error': f'Cannot compare {first} and {second}'}))
            return False


_USE_RUST_MATCHER = (
    os.environ.get("DORIAN_USE_RUST_MATCHER", "").lower()
    in ("1", "true", "yes", "on")
)


def _pattern_dag_to_pg_json(pattern: DAG) -> str:
    """Serialise a pattern DAG (whose nodes are ``Node`` regex
    placeholders) to the Rust ``ProcessGraph`` JSON shape. Pattern
    nodes go on the wire as ``{"class_type": "Node", "type": "...",
    "text": "...", "language": "..."}``; concrete pattern nodes
    (rare — Operator/Parameter/Snippet patterns) round-trip through
    their normal class_type."""
    nodes_out: dict[str, dict] = {}
    for nid, n in pattern.nodes.items():
        if isinstance(n, Node):
            nodes_out[nid] = {
                "class_type": "Node",
                "type": n.type,
                "text": n.text,
                "language": n.language,
            }
        elif isinstance(n, Operator):
            nodes_out[nid] = {
                "class_type": "Operator",
                "name": n.name,
                "language": n.language,
                "tasks": list(n.tasks or []),
            }
        elif isinstance(n, Parameter):
            nodes_out[nid] = {
                "class_type": "Parameter",
                "name": n.name,
                "dtype": n.dtype,
                "value": str(n.value),
            }
        elif isinstance(n, Snippet):
            nodes_out[nid] = {
                "class_type": "Snippet",
                "name": n.name,
                "code": n.code,
                "language": n.language,
            }
    edges_out: list[dict] = []
    for e in pattern.edges:
        pos = e.position
        if isinstance(pos, bool):
            pos_json: int | str = int(pos)
        elif isinstance(pos, int):
            pos_json = pos
        elif isinstance(pos, str) and pos.lstrip("-").isdigit():
            pos_json = int(pos)
        else:
            pos_json = str(pos)
        edges_out.append({
            "source": e.source,
            "destination": e.destination,
            "position": pos_json,
            "output": int(e.output),
        })
    return json.dumps({"nodes": nodes_out, "edges": edges_out})


def _dag_to_pg_json(dag: DAG) -> str:
    """Serialise a concrete DAG to ``ProcessGraph`` JSON. Same shape
    as :func:`_pattern_dag_to_pg_json` for the concrete-payload
    branches; never emits the ``Node`` regex placeholder shape."""
    nodes_out: dict[str, dict] = {}
    for nid, n in dag.nodes.items():
        if isinstance(n, Operator):
            nodes_out[nid] = {
                "class_type": "Operator",
                "name": n.name,
                "language": n.language,
                "tasks": list(n.tasks or []),
            }
        elif isinstance(n, Parameter):
            nodes_out[nid] = {
                "class_type": "Parameter",
                "name": n.name,
                "dtype": n.dtype,
                "value": str(n.value),
            }
        elif isinstance(n, Snippet):
            nodes_out[nid] = {
                "class_type": "Snippet",
                "name": n.name,
                "code": n.code,
                "language": n.language,
            }
    edges_out: list[dict] = []
    for e in dag.edges:
        pos = e.position
        if isinstance(pos, bool):
            pos_json2: int | str = int(pos)
        elif isinstance(pos, int):
            pos_json2 = pos
        elif isinstance(pos, str) and pos.lstrip("-").isdigit():
            pos_json2 = int(pos)
        else:
            pos_json2 = str(pos)
        edges_out.append({
            "source": e.source,
            "destination": e.destination,
            "position": pos_json2,
            "output": int(e.output),
        })
    return json.dumps({"nodes": nodes_out, "edges": edges_out})


def _pattern_pipeline(pattern: DAG):
    """Return a ``dorian_native.Pipeline`` of *pattern*, cached on
    the pattern instance itself. Patterns are immutable through a
    rule's lifetime, so the JSON encode + Pipeline construction
    cost is paid once per rule and reused across every match call
    against any DAG. Falls through silently when ``dorian_native``
    isn't loadable — the caller picks up the python path.
    """
    cached = getattr(pattern, "_native_pipeline", None)
    if cached is not None:
        return cached
    import dorian_native
    pat = dorian_native.Pipeline(_pattern_dag_to_pg_json(pattern))
    try:
        pattern._native_pipeline = pat  # type: ignore[attr-defined]
    except Exception:
        # Some DAG instances are frozen / dataclass-locked; the cache
        # miss just means we re-encode the small pattern next time,
        # which is cheap (~5 µs).
        pass
    return pat


def _match_via_rust(pattern: DAG, dag: DAG, processed: Sequence[Candidate]) -> Tuple[bool, Dict[ID, ID]]:
    """Rust matcher entry. Pattern is wrapped in a cached
    ``Pipeline`` (per-rule, reused across every match call). The DAG
    pipeline is built per-call here because the DAG mutates between
    matches in ``sync_apply``; callers that hold a stable DAG should
    instead use ``Pipeline.match_pattern`` directly.
    """
    import dorian_native
    try:
        pat_pipe = _pattern_pipeline(pattern)
        dag_pipe = dorian_native.Pipeline(_dag_to_pg_json(dag))
    except Exception:
        return _match_via_python(pattern, dag, processed)
    proc_json = json.dumps([dict(p) for p in processed]) if processed else None
    try:
        result = dag_pipe.match_pattern(pat_pipe, proc_json)
    except ValueError:
        # Serialisation mismatch (e.g. a ``Group`` node the JSON
        # schema doesn't know yet) — fall back to the Python path so
        # the match still completes.
        return _match_via_python(pattern, dag, processed)
    if result is None:
        return False, {}
    return True, json.loads(result)


def _match_via_python(pattern: DAG, dag: DAG, processed: Sequence[Candidate], comparator_fn: Callable[[Nodes, Nodes], bool] = comparator) -> Tuple[bool, Dict[ID, ID]]:
    return _match_python_impl(pattern, dag, processed, comparator_fn)


def match(pattern: DAG, dag: DAG, processed: Sequence[Candidate] = [], comparator: Callable[[Nodes, Nodes], bool] = comparator) -> Tuple[bool, Dict[ID, ID]]:
    """
    Match a rule pattern against a DAG and find valid candidate mappings.

    Args:
        rule_pattern: Pattern from the rule containing nodes and edges
        dag: The DAG to match against
        comparator: Function to compare nodes

    Returns:
        tuple: (bool indicating if match found, candidate mapping dict if found, else None)
    """
    if _USE_RUST_MATCHER and comparator is globals().get("comparator"):
        # Rust path is only used for the default ``comparator`` —
        # custom Python comparators (e.g. callsite-specific overrides)
        # always run on the Python side because there's no equivalent
        # KB-side hook to plumb them through.
        return _match_via_rust(pattern, dag, processed)
    return _match_python_impl(pattern, dag, processed, comparator)


def _match_python_impl(pattern: DAG, dag: DAG, processed: Sequence[Candidate], comparator: Callable[[Nodes, Nodes], bool]) -> Tuple[bool, Dict[ID, ID]]:
    # ── Type-indexed pre-filter ──────────────────────────────────────
    # Build per-type buckets once so comparator doesn't scan Snippets
    # when looking for Operators, etc.  Falls back to full list for
    # Node() patterns that could match multiple types.
    _type_to_name = {Operator: "Operator", Parameter: "Parameter", Snippet: "Snippet"}
    _by_type: dict[str, list[tuple[ID, Nodes]]] = defaultdict(list)
    _all_items: list[tuple[ID, Nodes]] = list(dag.nodes.items())
    for _id, _node in _all_items:
        _by_type[_type_to_name.get(type(_node), "Node")].append((_id, _node))

    def _candidates_for(pat_node: Nodes) -> list[tuple[ID, Nodes]]:
        """Return only DAG nodes that could possibly match *pat_node*."""
        if isinstance(pat_node, Node) and pat_node.type:
            # Pattern specifies a type constraint — use the bucket
            bucket = _by_type.get(pat_node.type, [])
            if bucket:
                return bucket
        # Fall through: Node() without type, or unknown type → scan all
        return _all_items

    # Gets one list and one item. returns the indices of the elements in the list, which were comparable/equivalent with the specified item.
    def _iter(elements: List[Tuple[ID, Nodes]], element: Nodes, _comparator: Callable[[Nodes, Nodes], bool]) -> Iterator[ID]:
        for idx, el in elements:
            if _comparator(el, element):
                yield idx

    # Graph matching:
    # for each node in the rule, returns a list of potential matches, then iterates over all the possible mappings for different nodes.
    # If we cannot find for even a single node any matches, then we have failed to find the rule (product func returns empty)
    # Each element in product (values) is a list, long as the number of pattern nodes
    for values in product(
        *map(
            lambda x: _iter(_candidates_for(x), x, comparator),
            pattern.nodes.values(),
        )
    ):
        # candidate nodes should have unique IDs
        # cannot match two nodes with a node
        if len(values) != len(set(values)):
            continue

        candidate = dict(zip(pattern.nodes.keys(), values))
        if candidate in processed: continue

        # Count matched edges
        matched = 0
        for edge in pattern.edges:
            s, d = candidate[edge.source], candidate[edge.destination]
            for _edge in dag.edges:
                if (_edge.source == s) & (_edge.destination == d):
                    matched += 1
                    break

        if matched == len(pattern.edges):
            # Meaning all edges are matched and we have a complete match.
            return True, candidate

    return False, {}


async def apply(rule: RewriteRule, dag: DAG, meta: Dict[str, Any], processed: List[Candidate] = []) -> DAG:
    is_matched, candidate = match(rule.pattern, dag, processed)
    if is_matched and candidate not in processed:
        await aemit(*rule.emit(dag, candidate))
        return await apply(rule, await rewrite(dag, candidate, rule.transformations, meta), meta, processed + [candidate])
    return dag


async def transform(dag: DAG, rules: Rules, meta: Dict[str, Any]) -> DAG:
    # Sequentially applies rules from left to right.
    result = dag
    for rule in rules:
        result = await apply(rule, result, meta=meta)
    return result

def cleanup(obj, key):
    return [[el['name'] if isinstance(el, dict) else el for el in path[key]] for path in obj]
