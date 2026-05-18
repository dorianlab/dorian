from typing import Sequence, Any, Tuple
from dataclasses import asdict
import hashlib
import re
from backend.events import Event, emit

from dorian.dag import DAG, Node, Edge, ID, wildcard, Operator, Snippet, Parameter
from dorian.languages import PYTHON
from dorian.code.parsing.rule import (
    RewriteRule,
    Add,
    Apply,
    Delete,
    Revert,
    ToOperator,
    ToParameter,
    PurgeMode,
)

Rules = Sequence[RewriteRule]


def _update(dag: DAG, key: ID, part: str, value: Any) -> DAG:
    """Replace attribute *part* of the node at *key* with *value*.

    This helper is exposed at module level so that LLM-generated rules
    can reference it via ``_update`` inside their ``Apply`` lambdas.
    """
    if key not in dag.nodes:
        return dag
    node = Node(**dict(asdict(dag.nodes[key]), **{part: value}))
    return DAG(nodes=dict(dag.nodes, **{key: node}), edges=dag.edges)


# Method-shortcut names the dorian executor recognises — when a
# call's attribute text matches ``<var>.<method>`` and ``<method>``
# is in this set, the assignment rule's nested method-shortcut rule
# below collapses the prefix into a chain edge from the variable's
# producer at position 0, mirroring the runtime resolver dispatch.
_METHOD_SHORTCUT_NAMES: tuple[str, ...] = (
    "fit", "predict", "transform", "fit_transform",
    "fit_predict", "predict_proba", "decision_function",
    "score", "score_samples", "inverse_transform",
    "validate", "create",
)


def _edge_output_between(g: DAG, src: ID, dst: ID) -> int:
    """Return the ``output`` index of the first edge from *src* to *dst*.

    Used by the generic identifier-rewire rule so the producer→use
    rewire can preserve the matched parent→identifier edge's
    ``output`` index. Defaults to 0 when no edge is found (the rule's
    pattern guarantees one exists at match time, but a defensive
    default keeps the rewrite safe under reentry).
    """
    for e in g.edges:
        if e.source == src and e.destination == dst:
            try:
                return int(e.output)
            except (TypeError, ValueError):
                return 0
    return 0


def _rewire_identifier_uses_local(producer_id: ID, producer_output: int, *, ident_key: str):
    """Local Apply for the generic identifier-rewire rule.

    Replaces the matched use-identifier (no incoming edges from the
    pattern's perspective; outgoing edges go to consumers) with
    direct ``producer → consumer`` edges that carry
    ``output=producer_output``. Touches only the matched
    identifier's outgoing edges — no graph-wide scan.
    """
    def _f(_g: DAG, _m: dict) -> DAG:
        ident_id = _m[ident_key]
        new_edges: list[Edge] = []
        for e in _g.edges:
            if e.source == ident_id:
                new_edges.append(Edge(
                    source=producer_id,
                    destination=e.destination,
                    position=e.position,
                    output=producer_output,
                ))
            elif e.destination == ident_id:
                continue
            else:
                new_edges.append(e)
        new_nodes = {k: v for k, v in _g.nodes.items() if k != ident_id}
        return DAG(nodes=new_nodes, edges=new_edges)
    return _f


def _rewire_use_to_producer(producer_id: ID, *, use_key: str, consumer_key: str):
    """Build a local-only Apply that replaces the matched use-edge
    with an equivalent edge from the captured producer.

    Used inside the nested variable-resolution rule. The Apply
    operates strictly on the matched mapping — touches only the
    use-identifier node and its single edge to the consumer in the
    matched subgraph. No graph-wide scan.
    """
    def _f(_g: DAG, _m: dict) -> DAG:
        use_id = _m[use_key]
        consumer_id = _m[consumer_key]
        # Method shortcuts return ``(instance, result)`` — the
        # variable bound by ``y_pred = clf.predict(X)`` refers to
        # the *result* (output=1), not the instance (output=0).
        # Inspect the producer at rewire time (not capture time)
        # because the method-shortcut nested rule fires earlier in
        # the queue and may have just renamed it. This local check
        # handles only the matched producer; no graph scan.
        prod = _g.nodes.get(producer_id)
        # Method-shortcut producers expose ``(instance, result)`` —
        # ``y_pred = clf.predict(X)`` binds the result, output=1, not
        # the instance. Detect both already-renamed (name == bare
        # method) and pending-rename (name == ``<var>.<method>``)
        # producers so the right output port wires regardless of
        # whether clf's method-shortcut nested rule has fired yet.
        prod_name = (prod.name or "") if isinstance(prod, Operator) else ""
        is_method = (
            prod_name in _METHOD_SHORTCUT_NAMES
            or (
                "." in prod_name
                and prod_name.rsplit(".", 1)[-1] in _METHOD_SHORTCUT_NAMES
            )
        )
        prod_output: int = 1 if is_method else 0
        new_edges: list[Edge] = []
        for e in _g.edges:
            if e.source == use_id and e.destination == consumer_id:
                new_edges.append(Edge(
                    source=producer_id,
                    destination=consumer_id,
                    position=e.position,
                    output=prod_output,
                ))
            elif e.source == use_id or e.destination == use_id:
                continue
            else:
                new_edges.append(e)
        new_nodes = {k: v for k, v in _g.nodes.items() if k != use_id}
        return DAG(nodes=new_nodes, edges=new_edges)
    return _f


def _kb_method_port_table(method_name: str) -> dict[int, str]:
    """Return ``{position: semantic_name}`` for a method shortcut
    by querying the KB. Cached per-method so the lookup is O(1) on
    repeat hits. KB walking is bounded — one ``get_method_io`` call
    per known interface, all of which are tiny."""
    try:
        cached = _METHOD_PORT_CACHE[method_name]
    except KeyError:
        cached = _build_method_port_table(method_name)
        _METHOD_PORT_CACHE[method_name] = cached
    return cached


_METHOD_PORT_CACHE: dict[str, dict[int, str]] = {}


def _build_method_port_table(method_name: str) -> dict[int, str]:
    table: dict[int, str] = {}
    try:
        from dorian.knowledge.queries import get_method_io
    except Exception:
        return table
    for iface in (
        "Sklearn Estimator",
        "Sklearn Transformer",
        "Sklearn Supervised Transformer",
    ):
        try:
            mio = get_method_io(iface)
        except Exception:
            continue
        ins, _ = (mio or {}).get(method_name, ([], []))
        for inp in ins:
            name = inp.get("name", "")
            try:
                pos = int(inp.get("position", 0))
            except (TypeError, ValueError):
                continue
            if not name or name.isdigit() or name == "self":
                continue
            table.setdefault(pos, name)
    return table


def _rewrite_method_call_local(
    producer_id: ID,
    producer_output,
    method_name: str,
    *,
    op_key: str,
):
    """Build a local-only Apply that renames a matched
    ``Operator(name="<var>.<method>")`` to the bare method shortcut,
    adds a chain edge from the captured producer at ``"self"``, and
    rewrites incident data-arg edge positions to the KB-declared
    semantic names.

    Edge positions are stored as the semantic port name everywhere
    the KB declares one — ``"self"`` for the instance, ``"X"`` /
    ``"y"`` / ``"X_test"`` for the data slots. The runtime resolver
    routes string-positioned edges as kwargs and the method-shortcut
    path pops ``"self"`` before invoking the underlying sklearn
    method (see ``_method_call`` in ``operator_resolver.py``). The
    visual frontend shows the same names, so the UI gives the
    end-user the actual port semantics instead of bare slot
    indices.

    The Apply touches only the matched Operator's incident edges —
    no global scan. Existing positional data-arg edges
    (``_expand_argument_list`` wrote them as ``pos=0, 1, 2, ...``
    using function-call convention) are bumped by +1 to free
    ``"self"`` for the chain edge, then translated to KB names.
    """
    port_table = _kb_method_port_table(method_name)

    def _f(_g: DAG, _m: dict) -> DAG:
        op_id = _m[op_key]
        existing = _g.nodes.get(op_id)
        new_nodes = dict(_g.nodes)
        new_nodes[op_id] = Operator(
            name=method_name,
            language=getattr(existing, "language", PYTHON),
            tasks=list(getattr(existing, "tasks", []) or []),
        )
        new_edges: list[Edge] = []
        for e in _g.edges:
            if e.destination == op_id and isinstance(e.position, int):
                bumped = e.position + 1
                renamed = port_table.get(bumped)
                new_edges.append(Edge(
                    source=e.source,
                    destination=e.destination,
                    position=renamed if renamed else bumped,
                    output=e.output,
                ))
            else:
                new_edges.append(e)
        new_edges.append(Edge(
            source=producer_id,
            destination=op_id,
            position="self",
            output=producer_output,
        ))
        return DAG(nodes=new_nodes, edges=new_edges)
    return _f


def _subscript_to_snippet_local(dag: DAG, mapping: dict) -> DAG:
    """Convert a single matched ``subscript`` Node into a Snippet
    that runs the slice on the root identifier's value.

    Local to one subscript (matched via the pattern node ``"0"``).
    Iterates only edges incident to the subscript subtree — no
    global node scan. Falls back to no-op if the subscript text
    has no leading identifier (e.g. ``[1, 2, 3][0]``).
    """
    sub_id = mapping["0"]
    node = dag.nodes.get(sub_id)
    if not isinstance(node, Node) or node.type != "subscript":
        return dag
    text = node.text or ""
    end = len(text)
    for i, ch in enumerate(text):
        if not (ch.isalnum() or ch == "_"):
            end = i
            break
    root = text[:end]
    if not root or not (root[0].isalpha() or root[0] == "_"):
        return dag

    # Walk descendants — local to this subscript only.
    descendants: set[ID] = set()
    stack: list[ID] = [
        e.destination
        for e in dag.edges
        if e.source == sub_id
    ]
    while stack:
        cid = stack.pop()
        if cid in descendants or cid == sub_id:
            continue
        child = dag.nodes.get(cid)
        if not isinstance(child, Node):
            continue
        descendants.add(cid)
        stack.extend([
            e.destination
            for e in dag.edges
            if e.source == cid
        ])

    # Keep one descendant identifier whose text matches the root —
    # the variable-resolution nested rule (under the assignment
    # rule) will later rewire its outgoing edge to come from the
    # actual producer (e.g. ``pandas.read_csv``). Drop the rest of
    # the subscript subtree.
    keep_id: ID | None = None
    for d in descendants:
        nd = dag.nodes.get(d)
        if isinstance(nd, Node) and nd.type == "identifier" and nd.text == root:
            keep_id = d
            break

    snippet_code = f"def foo({root}):\n    return {text}\n"
    new_nodes = dict(dag.nodes)
    new_nodes[sub_id] = Snippet(
        name="subscript",
        code=snippet_code,
        language="python",
    )
    for d in descendants:
        if d == keep_id:
            continue
        new_nodes.pop(d, None)

    new_edges: list[Edge] = []
    have_root_edge = False
    for e in dag.edges:
        # Subscript-internal edges (sub→descendant or descendant→
        # descendant excluding keep_id) — drop. These were tree
        # plumbing (slice, attribute, …) that the Snippet's text
        # already encodes.
        drop_descendants = descendants - {keep_id} if keep_id else descendants
        if e.source == sub_id and e.destination in drop_descendants:
            continue
        if e.source in drop_descendants and e.destination in drop_descendants:
            continue
        if e.source in drop_descendants and e.destination == sub_id:
            continue
        if e.destination in drop_descendants:
            # Drops external edges into pruned tree plumbing.
            continue
        if e.source in drop_descendants:
            continue
        new_edges.append(e)
    if keep_id is not None and not have_root_edge:
        # Rewire ``root_identifier → subscript`` at the root name
        # (matches the Snippet's ``def foo(<root>)`` signature) so
        # the variable-resolution rewire and the visual layer both
        # display the parameter name instead of a bare slot index.
        new_edges.append(Edge(
            source=keep_id,
            destination=sub_id,
            position=root,
            output=0,
        ))
    return DAG(nodes=new_nodes, edges=new_edges)


def _expand_argument_list(dag: DAG, mapping: dict) -> DAG:
    """Wire a call's argument_list into positional edges.

    Replaces the two-step Revert chain (``argument_list↔arg`` then
    ``call↔argument_list``) that previously collapsed the wrapper.
    The Revert path used ``set(...)`` for edge dedup, which threw
    away tree-sitter's source order, leaving every argument at
    ``position=0``. For ``clf.fit(X, y)`` that meant ``X`` and ``y``
    raced for the first slot; whichever happened to win sat at
    pos=1, the other got renumbered to pos=2 — so half the time the
    runtime call became ``fit(self, y, X)`` instead of ``fit(self,
    X, y)``.

    This Apply walks the dag-edge list (preserves insertion order
    from the parser) and assigns positions 1..N in order, leaving
    pos=0 free for the chain edge that ``_resolve_method_shortcuts``
    later adds. Skips the ``(`` / ``)`` / ``,`` punctuation children
    — they were typed-deleted by the noise rule but the safety
    filter mirrors the punctuation alternation defensively.
    """
    call_id = mapping["0"]
    al_id = mapping["1"]
    children: list[ID] = []
    for e in dag.edges:
        if e.source != al_id:
            continue
        child = dag.nodes.get(e.destination)
        if isinstance(child, Node) and child.type in {"(", ")", ","}:
            continue
        children.append(e.destination)

    new_edges = [
        e for e in dag.edges
        if not (e.source == al_id or e.destination == al_id)
        and not (e.source == call_id and e.destination == al_id)
    ]
    # Start at pos=0 for plain function calls (``pd.read_csv(fpath)``
    # → fpath at pos=0). When the call later resolves to a method
    # shortcut (``clf.fit`` → ``fit``), the nested method-shortcut
    # rule bumps these positions by +1 to make room for the chain
    # edge at pos=0. Parameter children (already promoted from
    # ``keyword_argument`` by an earlier rule) wire as kwargs
    # — position is the parameter's ``name``, not the slot index —
    # so they survive the bump unchanged.
    pos_idx = 0
    for cid in children:
        child = dag.nodes.get(cid)
        if isinstance(child, Parameter) and child.name:
            new_edges.append(Edge(
                source=cid,
                destination=call_id,
                position=child.name,
                output=0,
            ))
        else:
            new_edges.append(Edge(
                source=cid,
                destination=call_id,
                position=pos_idx,
                output=0,
            ))
            pos_idx += 1
    new_nodes = {k: v for k, v in dag.nodes.items() if k != al_id}
    return DAG(nodes=new_nodes, edges=new_edges)


def _unpack_pattern_list(dag: DAG, mapping: dict) -> DAG:
    """Turn a ``parent → pattern_list → [identifier_0, ..., identifier_k]``
    subgraph into direct ``parent → identifier_i`` edges carrying
    ``output=i``. Subsequent identifier-rewire rules propagate each
    identifier to its downstream consumers, preserving the output port.

    Handles tuple unpacking: ``X, y = f()`` and
    ``X_train, X_test, y_train, y_test = train_test_split(...)``.
    """
    parent_id = mapping["0"]
    pl_id = mapping["1"]

    # Collect identifier children of the pattern_list in source order.
    # We need stable ordering so "X, y" maps to outputs 0, 1 in the
    # same order they appear in the tuple. DAG edges are a list so
    # iteration order is insertion order — good enough.
    idents: list[ID] = []
    for e in dag.edges:
        if e.source == pl_id:
            child = dag.nodes.get(e.destination)
            if isinstance(child, Node) and child.type == "identifier":
                idents.append(e.destination)

    # Build the replacement edges: parent → each identifier at output=i.
    # Carry an integer position (default 0); downstream rules reassign
    # it based on the consumer's argument slot.
    new_edges = [
        Edge(source=parent_id, destination=ident_id, position=0, output=i)
        for i, ident_id in enumerate(idents)
    ]

    # Drop the parent → pattern_list edge AND the pattern_list → child
    # edges, keep everything else; the pattern_list node itself is
    # removed. The identifier children stay — they're still present so
    # the generic identifier-rewire rule can reroute to their downstream
    # consumers (preserving the output port via the new edge's ``output``).
    filtered_edges = [
        e for e in dag.edges
        if e.source != pl_id and e.destination != pl_id
    ]
    filtered_edges.extend(new_edges)
    new_nodes = {k: v for k, v in dag.nodes.items() if k != pl_id}
    return DAG(nodes=new_nodes, edges=filtered_edges)


# Mutable list of rules — `get_rules()` returns a snapshot,
# `add_rewrite_rule()` appends to this list at runtime.
_rules: list[RewriteRule] = []


def add_rewrite_rule(rule_str: str) -> Tuple[RewriteRule | None, str]:
    """Parse *rule_str* as a ``RewriteRule`` and append it to the rule list.

    Returns ``(rule, error_message)``.  On success *error_message* is ``""``.
    Used by the LLM rule-generation loop to dynamically add rules.
    """
    error_message = ""
    new_rule = None
    try:
        new_rule = eval(rule_str)  # noqa: S307
    except Exception as e:
        error_message = str(e)

    if new_rule and isinstance(new_rule, RewriteRule):
        _rules.append(new_rule)
        global _rules_version_cache
        _rules_version_cache = None  # invalidate on mutation

    return new_rule, error_message


# ---------------------------------------------------------------------------
# Rule versioning — content-hash-based
# ---------------------------------------------------------------------------

_rules_version_cache: str | None = None


def _compute_rules_hash(rules: Rules) -> str:
    """SHA-256 content hash of the rule set.

    Hashes each rule's description, pattern topology, and transformation
    class names in a deterministic order.  Lambda bytecode is included so
    structural changes to ``Apply`` closures invalidate the hash.
    """
    h = hashlib.sha256()
    for rule in rules:
        h.update(rule.description.encode())
        # Pattern nodes — sorted by key for determinism
        for nid in sorted(rule.pattern.nodes.keys()):
            node = rule.pattern.nodes[nid]
            h.update(f"{nid}:{node.type}:{node.text}:{node.language}".encode())
        # Pattern edges — sorted by (source, destination)
        for edge in sorted(rule.pattern.edges, key=lambda e: (e.source, e.destination)):
            h.update(f"{edge.source}->{edge.destination}".encode())
        # Transformation class names + lambda bytecode
        for tf in rule.transformations:
            h.update(tf.__class__.__name__.encode())
            if hasattr(tf, "f") and callable(tf.f):
                h.update(tf.f.__code__.co_code)
                h.update(str(tf.f.__code__.co_consts).encode())
    return h.hexdigest()[:16]


def get_rules_version() -> str:
    """Return a stable content hash for the current rule set.

    Recomputed lazily; invalidated whenever ``add_rewrite_rule()`` mutates
    the list.
    """
    global _rules_version_cache
    if _rules_version_cache is None:
        _rules_version_cache = _compute_rules_hash(get_rules())
    return _rules_version_cache


def get_rules(custom_list_src: str | None = None) -> Rules:
    """Return the active rule set.

    If *custom_list_src* is provided (the ``return [...]`` list literal saved
    by the user), it is evaluated in a sandboxed context that exposes all
    local helper variables (``single_character``, ``basic``,
    ``types_to_delete``) and every symbol imported at the top of this module.
    The result is validated to be a non-empty list of ``RewriteRule`` objects;
    on any error the default rule set is used and a warning is logged.
    """
    single_character = r"^.$"
    basic = r"string|integer|float|identifier"
    types_to_delete = [
        # "module",
        # "expression_statement",
        "comment",
        "string_start",
        "string_end",
        "string_content",
        "from",
        # "import",
        "as",
    ]

    if custom_list_src:
        _eval_ctx = {
            # local helper variables users can reference in their rules
            "single_character": single_character,
            "basic": basic,
            "types_to_delete": types_to_delete,
            # DAG primitives
            "DAG": DAG,
            "Node": Node,
            "Edge": Edge,
            "ID": ID,
            "wildcard": wildcard,
            "Operator": Operator,
            "Snippet": Snippet,
            "Parameter": Parameter,
            # language constants
            "PYTHON": PYTHON,
            # rule / transformation types
            "RewriteRule": RewriteRule,
            "Add": Add,
            "Apply": Apply,
            "Delete": Delete,
            "Revert": Revert,
            "ToOperator": ToOperator,
            "ToParameter": ToParameter,
            "PurgeMode": PurgeMode,
            # module-level helpers
            "_update": _update,
        }
        try:
            result = eval(custom_list_src, _eval_ctx)  # noqa: S307
            if isinstance(result, tuple):
                result = list(result)
            if (
                isinstance(result, list)
                and len(result) > 0
                and all(isinstance(r, RewriteRule) for r in result)
            ):
                emit(Event("CustomRulesLoaded", {"count": len(result)}))
                return result
            emit(Event("CustomRulesInvalid", {"resultType": str(type(result))}))
        except Exception as exc:
            emit(Event("CustomRulesEvalFailed", {"error": str(exc)}))

    return [
        RewriteRule(
            description=f'Deletes nodes with types {", ".join(types_to_delete)}, in isolation',
            pattern=DAG(
                nodes={
                    "0": Node(
                        # Anchored alternation. Without the end anchor,
                        # ``as`` would match the prefix of ``assignment``
                        # (re.match anchors at start only), and ``from``
                        # would swallow ``from_statement`` etc. That was
                        # wiping Python assignment nodes before later
                        # rules could process them.
                        type=r"^(?:" + "|".join(types_to_delete) + r")$",
                        language=PYTHON,
                    )
                },
                edges=[],
            ),
            transformations=[Delete(nodes=["0"])],
        ),
        RewriteRule(
            description='Removes the children of an attribute, which themselves are attributes.',
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="list", # |attribute
                        language=PYTHON,
                    ),
                    "1": Node(
                        type=basic,
                        language=PYTHON
                    ),
                },
                edges=[Edge(source="0", destination="1")],
            ),
            transformations=[Delete(nodes=["1"])],
        ),
        RewriteRule(
            description="""assigns the type attribute of the 0 Node to the type value of 2 Node and then deletes 1,2 Nodes.
            The point is to delete two nodes and change the type value of the root from 'unary_operator' to 'identifier'""",
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="unary_operator",
                        language=PYTHON,
                    ),
                    "1": Node(language=PYTHON),
                    "2": Node(language=PYTHON),
                },
                edges=[
                    Edge(source="0", destination="1"),
                    Edge(source="0", destination="2"),
                ],
            ),
            transformations=[
                Apply(f=lambda g, m: _update(g, m["0"], "type", g.nodes[m["2"]].type)),
                Delete(nodes=["1", "2"], mode=PurgeMode.recursive),
            ],
        ),
        RewriteRule(
            pattern=DAG(
                nodes={
                    "0": Node(
                        type=single_character,
                        text=single_character,
                        language=PYTHON,
                    )
                },
                edges=[],
            ),
            transformations=[Delete(nodes=["0"])],
        ),
        RewriteRule(
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="dotted_name",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="identifier",
                        language=PYTHON,
                    ), 
                },
                edges=[
                    Edge(source="0", destination="1")
                ]
            ),
            transformations=[
                Delete(nodes=["1"]),
            ],
        ),
        RewriteRule(
            description="""Handling import statements in 3 stages.
            This rule replaces aliased imports with their full path in the code.""",
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="aliased_import",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="dotted_name",
                        language=PYTHON,
                    ),
                    "2": Node(
                        type="identifier",
                        language=PYTHON,
                    ),
                },
                edges=[
                    Edge(source="0", destination="1"),
                    Edge(source="0", destination="2"),
                ],
            ),
            transformations=[
                Apply(lambda g, m: _update(g, m["1"], "type", "identifier")),
                Delete(nodes=["0", "2"]),
            ],
            rules=[
                lambda g, m: RewriteRule(
                    pattern=DAG(
                        nodes={
                            "0": Node(
                                type="attribute",
                                language=PYTHON,
                            ),
                            "1": Node(
                                type="identifier",
                                text=g.nodes[m["2"]].text,
                                language=PYTHON,
                            ),
                        },
                        edges=[
                            Edge(source="0", destination="1")
                        ]
                    ),
                    transformations=[
                        # Replace just the alias prefix in the attribute's
                        # text. ``import pandas as pd`` + use site
                        # ``pd.read_csv`` should become ``pandas.read_csv``,
                        # not bare ``pandas``. The previous version overwrote
                        # the whole text with the module name, throwing away
                        # the method suffix and producing
                        # ``Operator pandas`` for every ``pd.read_csv`` call.
                        Apply(
                            f=lambda _g, _m: _update(
                                _g,
                                _m["0"],
                                "text",
                                g.nodes[m["1"]].text + (
                                    _g.nodes[_m["0"]].text[
                                        len(g.nodes[m["2"]].text):
                                    ]
                                    if _g.nodes[_m["0"]].text.startswith(
                                        g.nodes[m["2"]].text
                                    )
                                    else "." + _g.nodes[_m["0"]].text.split(".", 1)[1]
                                    if "." in _g.nodes[_m["0"]].text
                                    else ""
                                ),
                            )
                        ),
                        Delete(nodes=["1"])
                    ]
                )
            ]
        ),
        # TODO: this rule doesn't handle the combination of "from" and "aliased" imports
        # Also, here the only difference between "1" and "2" node is the order of them. Can lead to errors.
        RewriteRule(
            description='changes the path of imported libraries to their complete path',
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="import_statement|import_from_statement",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="dotted_name",
                        language=PYTHON,
                    ),
                    "2": Node(
                        type="identifier|dotted_name",
                        language=PYTHON,
                    ),
                },
                edges=[
                    Edge(source="0", destination="1"),
                    Edge(source="0", destination="2"),
                ],
            ),
            transformations=[Delete(nodes=["2"])],
            rules=[
                # Sub-rule A: ``module.<name>(...)`` → rewrite the
                # attribute's text to the FQN.
                lambda g, m: RewriteRule(
                    pattern=DAG(
                        nodes={
                            "0": Node(
                                type="call",
                                language=PYTHON,
                            ),
                            "1": Node(
                                type="attribute",
                                language=PYTHON,
                            ),
                            "2": Node(
                                type="identifier",
                                text=g.nodes[m["2"]].text,
                                language=PYTHON,
                            ),
                        },
                        edges=[
                            Edge(source="0", destination="1"),
                            Edge(source="1", destination="2"),
                        ]
                    ),
                    transformations=[
                        Apply(
                            f=lambda _g, _m: _update(
                                _g,
                                _m["1"],
                                "text",
                                f'{g.nodes[m["1"]].text}.{_g.nodes[_m["2"]].text}',
                            )
                        ),
                        Delete(nodes=["2"])
                    ]
                ),
                # Sub-rule B: bare ``<name>(...)`` (no attribute) →
                # rewrite the identifier's text to the FQN. Without
                # this, ``from sklearn.preprocessing import
                # StandardScaler`` followed by ``StandardScaler()``
                # left the call node as ``Operator StandardScaler``
                # (no module prefix), and the executor's dotted-path
                # importer couldn't find the class.
                lambda g, m: RewriteRule(
                    pattern=DAG(
                        nodes={
                            "0": Node(
                                type="call",
                                language=PYTHON,
                            ),
                            "1": Node(
                                type="identifier",
                                text=g.nodes[m["2"]].text,
                                language=PYTHON,
                            ),
                        },
                        edges=[
                            Edge(source="0", destination="1"),
                        ],
                    ),
                    transformations=[
                        Apply(
                            f=lambda _g, _m: _update(
                                _g,
                                _m["1"],
                                "text",
                                f'{g.nodes[m["1"]].text}.{g.nodes[m["2"]].text}',
                            )
                        ),
                    ],
                ),
            ]
        ),
        RewriteRule(
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="import_statement|import_from_statement",
                        language=PYTHON,
                    ),
                },
            ),
            transformations=[
                Delete(nodes=["0"], mode=PurgeMode.recursive)
            ]
        ),
        RewriteRule(
            description=(
                "Convert a ``subscript`` node into a Snippet that "
                "runs its slice on the root identifier (``df.iloc[:, "
                ":-1]`` → ``def foo(df): return df.iloc[:, :-1]``). "
                "Must fire BEFORE the attribute-identifier-children "
                "collapse below — otherwise the subscript's inner "
                "``df`` identifier is gone before this rule can keep "
                "it alive for the variable-resolution rewire. Pattern "
                "matches one subscript at a time; the Apply walks "
                "only that subscript's subtree edges."
            ),
            pattern=DAG(
                nodes={
                    "0": Node(type="subscript", language=PYTHON),
                },
            ),
            transformations=[
                Apply(f=_subscript_to_snippet_local),
            ],
        ),
        RewriteRule(
            description=(
                "Collapse an `attribute` node's identifier children. Tree-sitter "
                "already sets ``attribute.text`` to the full dotted path (e.g. "
                "``clf.fit``), so the historical Apply that appended each "
                "identifier's text produced duplication (``clf.fit.clf.fit`` — "
                "the rule fires once per child). Just delete the identifier; "
                "the attribute's text is correct as-is."
            ),
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="attribute",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="identifier",
                        language=PYTHON,
                    ),
                },
                edges=[
                    Edge(source="0", destination="1")
                ]
            ),
            transformations=[
                Delete(nodes=["1"])
            ]
        ),
        RewriteRule(
            description='handles keyword arguments.',
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="keyword_argument",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="identifier",
                        language=PYTHON,
                    ),
                    "2": Node(language=PYTHON),
                },
                edges=[
                    Edge(source="0", destination="1"),
                    Edge(source="0", destination="2"),
                ],
            ),
            transformations=[
                ToParameter(nid="0", kw="1", value="2"),
                Delete(nodes=["1", "2"])
            ],
        ),
        RewriteRule(
            description=(
                "Wire a call's ``argument_list`` into positional edges "
                "preserving source order. Replaces the prior Revert "
                "chain whose ``set(...)`` dedup discarded order and "
                "left every argument at pos=0."
            ),
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="call",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="argument_list",
                        language=PYTHON,
                    ),
                },
                edges=[
                    Edge(source="0", destination="1"),
                ]
            ),
            transformations=[
                Apply(f=_expand_argument_list),
            ],
        ),
        RewriteRule(
            description=(
                "Promote a call-with-dotted-function (``obj.method(...)``) to "
                "an Operator whose name is the attribute's text."
            ),
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="call",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="attribute",
                        language=PYTHON,
                    ),
                },
                edges=[
                    Edge(source="0", destination="1"),
                ]
            ),
            transformations=[
                ToOperator(nid="0", content="1"),
                Delete(nodes=["1"])
            ]
        ),
        RewriteRule(
            description=(
                "Promote a plain function call (``foo(...)``) to an Operator "
                "named after the function identifier. Must precede the generic "
                "identifier-deletion rule, otherwise the identifier is gone "
                "before we can read its text."
            ),
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="call",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="identifier",
                        language=PYTHON,
                    ),
                },
                edges=[
                    Edge(source="0", destination="1"),
                ]
            ),
            transformations=[
                ToOperator(nid="0", content="1"),
                Delete(nodes=["1"])
            ]
        ),
        RewriteRule(
            description='Transforms slicing/indexing to function calling',
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="subscript",
                        text=wildcard,
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="identifier",
                        text=wildcard,
                        language=PYTHON,
                    ),
                    "2": Node(
                        type=r"identifier|int|string",
                        text=wildcard,
                        language=PYTHON,
                    ),
                },
                edges=[
                    Edge(source="0", destination="1"),
                    Edge(source="0", destination="2"),
                ],
            ),
            transformations=[
                Revert(nodes=["0", "1", "2"])
            ],
        ),
        RewriteRule(
            description=(
                "Collapse a bare-statement wrapper: ``expression_statement`` "
                "whose only child is an Operator (or a plain ``call`` Node "
                "that didn't make it to Operator promotion). Without this "
                "rule, statements like ``clf.fit(X, y)`` and ``print(score)`` "
                "leave two stacked nodes in the DAG — an expression_statement "
                "pointing at the Operator / call. Deleting the wrapper keeps "
                "the semantic node and rewires any incoming edges."
            ),
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="expression_statement",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="Operator|call",
                        language=PYTHON,
                    ),
                },
                edges=[
                    Edge(source="0", destination="1"),
                ],
            ),
            transformations=[
                Delete(nodes=["0"], mode=PurgeMode.isolated),
            ],
        ),
        RewriteRule(
            description=(
                "Collapse a single-target variable assignment and "
                "queue nested local rewires for each later use of "
                "the bound name. Tree-sitter produces ``assignment "
                "→ (identifier_LHS, RHS)`` for ``scaler = "
                "StandardScaler()`` — the LHS identifier does not "
                "survive intact across later uses (each occurrence of "
                "``scaler`` further down the source becomes a fresh "
                "node with the same ``text`` but no producer edge). "
                "The two nested rules below capture the bound name + "
                "its producer at outer match time and apply local "
                "subgraph rewrites at every use site: one rule "
                "rewires data-references (``scaler`` used as an "
                "argument), the other collapses method calls "
                "(``scaler.fit_transform(...)`` becomes a bare "
                "``fit_transform`` shortcut with a chain edge from "
                "the producer at pos=0)."
            ),
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="assignment",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="identifier|pattern_list",
                        language=PYTHON,
                    ),
                    "2": Node(language=PYTHON),
                },
                edges=[
                    Edge(source="0", destination="1"),
                    Edge(source="0", destination="2"),
                ],
            ),
            transformations=[
                Add(edges=[("2", "1")]),
                Delete(nodes=["0"]),
            ],
            rules=[
                # Nested rule A — variable use rewire.
                # Pattern: a use-identifier with the captured ``text``
                # feeds some consumer. Local subgraph: the use plus
                # its single consuming edge. The Apply replaces the
                # edge with one from the producer (captured at outer
                # match time), preserving the original ``position``
                # / ``output`` so kwargs and positional ports survive
                # the rewire. The def-identifier captured at m["1"]
                # has no outgoing edges (the LHS isn't referenced
                # by anything after the assignment is collapsed),
                # so this pattern only matches use sites.
                lambda g, m: RewriteRule(
                    description=(
                        f"Rewire identifier ``{g.nodes[m['1']].text}`` "
                        "use-sites to its assignment producer."
                    ),
                    pattern=DAG(
                        nodes={
                            "use": Node(
                                type="identifier",
                                text=f"^{re.escape(g.nodes[m['1']].text)}$",
                                language=PYTHON,
                            ),
                            "consumer": Node(language=PYTHON),
                        },
                        edges=[Edge(source="use", destination="consumer")],
                    ),
                    transformations=[
                        Apply(f=_rewire_use_to_producer(
                            producer_id=m["2"],
                            use_key="use",
                            consumer_key="consumer",
                        )),
                    ],
                ),
                # Nested rule B — method-shortcut collapse.
                # Pattern: ``Operator(name="<name>.<method>")`` where
                # ``<name>`` is the bound assignment LHS and
                # ``<method>`` is one of the recognised shortcut
                # names. By outer-rule firing time the call-promotion
                # rules have already turned ``clf.fit(...)`` into
                # ``Operator(name="clf.fit")``, so the pattern matches
                # on operator name (Operators expose ``.name`` to the
                # comparator's ``Node.text`` slot — see ``Operator(),
                # Node()`` branch in ``dorian.dag.comparator``). One
                # rule is generated per ``(name, method)`` pair to
                # keep each inner pattern a literal regex rather than
                # a global ``(fit|predict|transform|...)`` alternation.
                *(
                    (lambda g, m, _method=method_name: RewriteRule(
                        description=(
                            f"Collapse ``{g.nodes[m['1']].text}.{_method}`` "
                            "operator into the bare method shortcut with "
                            "a chain edge to the assignment producer."
                        ),
                        pattern=DAG(
                            nodes={
                                "op": Node(
                                    type="Operator",
                                    text=f"^{g.nodes[m['1']].text}\\.{_method}$",
                                    language=PYTHON,
                                ),
                            },
                        ),
                        transformations=[
                            Apply(f=_rewrite_method_call_local(
                                producer_id=m["2"],
                                producer_output=0,
                                method_name=_method,
                                op_key="op",
                            )),
                        ],
                    ))
                    for method_name in _METHOD_SHORTCUT_NAMES
                ),
            ],
        ),
        RewriteRule(
            description=(
                "Tuple unpacking. For ``X, y = f()`` or "
                "``X_train, X_test, y_train, y_test = train_test_split(...)``, "
                "the assignment rule above already feeds the call into a "
                "``pattern_list`` node. This rule then fans the pattern_list "
                "out into direct ``call → identifier_i`` edges with "
                "``output=i`` so that the generic identifier-rewire rule can "
                "carry the correct port all the way to each downstream "
                "consumer."
            ),
            pattern=DAG(
                nodes={
                    "0": Node(language=PYTHON),
                    "1": Node(type="pattern_list", language=PYTHON),
                },
                edges=[
                    Edge(source="0", destination="1"),
                ],
            ),
            transformations=[
                Apply(f=_unpack_pattern_list),
            ],
        ),
        RewriteRule(
            description=(
                "Drop a module-level docstring. Tree-sitter represents a "
                "triple-quoted statement as ``expression_statement → string``; "
                "the ``string`` node's text starts with ``\"\"\"`` (or "
                "``'''``). Both nodes survive prior cleanup because the "
                "top-of-file string-type deletions don't target the full "
                "``string`` node, only its fragments (``string_start`` etc.)."
            ),
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="expression_statement",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="string",
                        text=r'^("""|\'\'\')',
                        language=PYTHON,
                    ),
                },
                edges=[
                    Edge(source="0", destination="1"),
                ],
            ),
            transformations=[
                Delete(nodes=["0", "1"], mode=PurgeMode.recursive),
            ],
        ),
        # !!! R8
        # Description: When method call exists, separates the class identifier from call function
        # Note: this rule must always follow R7!
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type="call",
        #                 text=r"^[^(]+\..+$",
        #                 language=PYTHON,
        #             ),
        #         },
        #         edges=[],
        #     ),
        #     transformations=[
        #         Apply(handle_method_call),
        #     ],
        # ),
        # !!! R9
        # attribute is names separated by dots when not used in import statements, like pandas.x.y.z
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type="dictionary",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "1": Node(
        #                 type="pair", text=wildcard, language=PYTHON
        #             ),
        #             # '2': Node(type=basic, text=wildcard, language=PYTHON),
        #             # '3': Node(type=wildcard, text=wildcard, language=PYTHON)
        #         },
        #         edges=[
        #             Edge(source="0", destination="1"),
        #             # Edge(source='1', destination='2'),
        #             # Edge(source='1', destination='3'),
        #         ],
        #     ),
        #     transformations=[
        #         # ToDictionary(nid='0')
        #         # ToOperator(nid='0', name='dict', language=Where('1', 'language')),
        #         # ToParameter(nid='1', name=Where('2', ''), type=, value=Where('')),
        #         # Add(edges=[('0', '1')]),
        #         Delete(nodes=["1"], edges=[("0", "1")], mode=PurgeMode.recursive)
        #     ],
        # ),
        # !!! R11
        # Description: Deletes print function instances
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type="call",
        #                 text="^print\(.*\)$",
        #                 language=PYTHON,
        #             )
        #         },
        #         edges=[],
        #     ),
        #     transformations=[Delete(nodes=["0"], mode=PurgeMode.recursive)],
        # ),
        # !!! R13 (2 stage)
        # Description: connects the arguments of a function to its name/identifier/attribute
        # Note: takes too long. Better to be positioned after making the graph simpler.
        # It seems, it's better to add connections first and do the cleaning at the end.
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type=r"call|method_call",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "1": Node(
        #                 type=r"attribute|identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "2": Node(
        #                 type="argument_list",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "3": Node(
        #                 type=r"string|keyword_argument|identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #         },
        #         edges=[
        #             Edge(source="0", destination="1"),
        #             Edge(source="0", destination="2"),
        #             Edge(source="2", destination="3"),
        #         ],
        #     ),
        #     transformations=[
        #         Add(edges=[("3", "1")]),
        #         Delete(edges=[("2", "3")]),
        #     ],
        # ),
        # !!! R14
        # Description: Continuation of the last rule. Does the cleanup and renaming.
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type=r"call|method_call",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "1": Node(
        #                 type=r"attribute|identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "2": Node(
        #                 type="argument_list",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #         },
        #         edges=[
        #             Edge(source="0", destination="1"),
        #             Edge(source="0", destination="2"),
        #         ],
        #     ),
        #     transformations=[
        #         # changes the type of attribute|identifier to call.
        #         Apply(
        #             f=lambda g, m: _update(
        #                 g,
        #                 m["1"],
        #                 "type",
        #                 "call",
        #             )
        #         ),
        #         Delete(nodes=["0", "2"]),
        #     ],
        # ),
        # !!! R15 (3 stages)
        # Description: handles list and tuple creation, with just considering one level nested lists or tuples.
        # First, mark the nested lists/tuples
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type=r"list|tuple",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "1": Node(
        #                 type=r"list|tuple",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #         },
        #         edges=[Edge(source="0", destination="1")],
        #     ),
        #     transformations=[
        #         Apply(
        #             lambda g, m: _update(
        #                 g, m["0"], "type", f"nested_{g.nodes[m['0']].type}"
        #             )
        #         )
        #     ],
        # ),
        # !!! R18 (2 stage)
        # Description: Here, when we have a statement like x=12, then whenever we have x in another part of the code,
        # it connects 12 to it. Then at the end, it removes the nodes for assignment expression x=12.
        # Question: if both sides of the assignment are identifiers (like x=y) then the pattern can get mixed.
        # Note: this rule should always follow R13/R14
        RewriteRule(
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="expression_statement",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="identifier|pattern_list",
                        language=PYTHON,
                    ),
                    "2": Node(language=PYTHON),
                },
                edges=[
                    Edge(source="0", destination="1"),
                    Edge(source="0", destination="2"),
                ],
            ),
            transformations=[
                Add(edges=[("2", "1")]),
                Delete(nodes=["0"])
            ]
            # rules=[
            #     lambda g, m: RewriteRule(
            #         pattern=DAG(
            #             nodes={
            #                 "0": Node(
            #                     type="identifier",
            #                     text=g.nodes[m["2"]].text,
            #                     language=PYTHON,
            #                 ),
            #             },
            #             edges=[]
            #         ),
            #         transformations=[
            #             Add(edges=[(m["3"], "0")]),
            #             Delete(nodes=["0"])
            #         ],
            #     )
            # ],
        ),
        RewriteRule(
            description=(
                "Generic identifier rewire — when a parent node points to "
                "an identifier, find every OTHER identifier with the same "
                "text in the DAG and rewire them to come from the parent. "
                "Preserves the original parent→identifier edge's ``output`` "
                "index so tuple unpackings (``X_train, X_test, y_train, "
                "y_test = train_test_split(X, y)``) propagate the right "
                "split-output port to each downstream use site (X_train "
                "→ output=0, X_test → output=1, …). Without this, every "
                "rewire used output=0 and the split was effectively "
                "treated as a single-output node — predict ended up "
                "running on X_train and the metric on y_train, all "
                "producing training-set scores."
            ),
            pattern=DAG(
                nodes={
                    "0": Node(language=PYTHON,),
                    "1": Node(
                        type="identifier",
                        language=PYTHON,
                    ),
                },
                edges=[
                    Edge(source="0", destination="1"),
                ],
            ),
            transformations=[
                Delete(nodes=["1"])
            ],
            rules=[
                lambda g, m: RewriteRule(
                    pattern=DAG(
                        nodes={
                            "0": Node(
                                type="identifier",
                                text=f"^{re.escape(g.nodes[m['1']].text)}$",
                                language=PYTHON,
                            ),
                        },
                        edges=[]
                    ),
                    transformations=[
                        Apply(f=_rewire_identifier_uses_local(
                            producer_id=m["0"],
                            producer_output=_edge_output_between(g, m["0"], m["1"]),
                            ident_key="0",
                        )),
                    ],
                )
            ],
        ),
        # !!! R19
        # Description: continuation of the last rule. Removes the assignment occurrences.
        # Question: if both sides are identifiers then the pattern can get mixed.
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "1": Node(
        #                 type="assignment",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "2": Node(
        #                 type="identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "3": Node(
        #                 type=wildcard,
        #                 text=wildcard,
        #                 language=PYTHON
        #             ),
        #         },
        #         edges=[
        #             Edge(source="1", destination="2"),
        #             Edge(source="1", destination="3"),
        #         ],
        #     ),
        #     transformations=[
        #         Delete(nodes=["1", "2"], mode=PurgeMode.isolated),
        #     ],
        # ),
        # !!! R20 (2 stage)
        # Description: Exactly as the last two rules. But for pattern-list type.
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "1": Node(
        #                 type="assignment",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "2": Node(
        #                 type="pattern_list",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "3": Node(
        #                 type="identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "4": Node(
        #                 type=wildcard,
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #         },
        #         edges=[
        #             Edge(source="1", destination="2"),
        #             Edge(source="2", destination="3"),
        #             Edge(source="1", destination="4"),
        #         ],
        #     ),
        #     rules=[
        #         lambda g, m: RewriteRule(
        #             pattern=DAG(
        #                 nodes={
        #                     "0": Node(
        #                         type="identifier",
        #                         text=g.nodes[m["3"]],
        #                         language=PYTHON,
        #                     ),
        #                 },
        #             ),
        #             transformations=[Add(edges=[(m["4"], "0")]), Delete(nodes=["0"])],
        #         )
        #     ]
        # ),
        # !!! R21
        # Description: continuation of the last rule. Removes the assignment occurrences.
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "1": Node(
        #                 type="assignment",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "2": Node(
        #                 type="pattern_list",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "3": Node(
        #                 type=wildcard, text=wildcard, language=PYTHON
        #             ),
        #         },
        #         edges=[
        #             Edge(source="1", destination="2"),
        #             Edge(source="1", destination="3"),
        #         ],
        #     ),
        #     transformations=[
        #         Delete(nodes=["1"], mode=PurgeMode.isolated),
        #         Delete(nodes=["2"], mode=PurgeMode.recursive),
        #     ],
        # ),
        # # !!! R
        # # Question:
        # # At the end of transformations, we will have a triangle, root, left Wildcard, right Operator, with Wildcard connected to Operator.
        # # I don't find anywhere in the code having attribute node connected to keyword_argument node?
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type="attribute",
        #                 text="sklearn.pipeline.Pipeline",
        #                 language=PYTHON,
        #             ),
        #             "1": Wildcard(type="Operator"),
        #             "2": Node(
        #                 type="keyword_argument",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "3": Node(
        #                 type="identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "4": Node(
        #                 type="attribute",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #         },
        #         edges=[
        #             Edge(source="0", destination="1"),
        #             Edge(source="0", destination="2"),
        #             Edge(source="2", destination="3"),
        #             Edge(source="2", destination="4"),
        #         ],
        #     ),
        #     transformations=[
        #         Delete(nodes=["2", "3"]),
        #         ToOperator(
        #             nid="4", name=Where("4", "text"), language=Where("4", "language")
        #         ),
        #         Add(edges=[("1", "4")]),
        #     ],
        # ),
        # # !!! R
        # # Question:
        # # comments: same as last rule
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type="call", text=wildcard, language=PYTHON
        #             ),
        #             "1": Node(
        #                 type="attribute",
        #                 text="sklearn.pipeline.Pipeline",
        #                 language=PYTHON,
        #             ),
        #             "2": Node(
        #                 type="keyword_argument",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "3": Node(
        #                 type="identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "4": Node(
        #                 type="attribute",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #         },
        #         edges=[
        #             Edge(source="0", destination="1"),
        #             Edge(source="0", destination="2"),
        #             Edge(source="2", destination="3"),
        #             Edge(source="2", destination="4"),
        #         ],
        #     ),
        #     transformations=[
        #         Delete(nodes=["0", "2", "3"]),
        #         ToOperator(
        #             nid="4", name=Where("4", "text"), language=Where("4", "language")
        #         ),
        #         Add(edges=[("1", "4")]),
        #     ],
        # ),
        # # !!! R
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type="attribute",
        #                 text="sklearn.pipeline.Pipeline",
        #                 language=PYTHON,
        #             ),
        #             "1": Wildcard(type="Operator"),
        #         },
        #         edges=[
        #             Edge(source="0", destination="1"),
        #         ],
        #     ),
        #     transformations=[
        #         Delete(nodes=["0"]),
        #     ],
        # ),
        # # !!! R
        # # Description: Changes the call node to an Operator, with information from the 1 Node, which shows which function is being called
        # # Then removes the attribute/identifier node.
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type="call", text=wildcard, language=PYTHON
        #             ),
        #             "1": Node(
        #                 type="attribute|identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #         },
        #         edges=[
        #             Edge(source="0", destination="1"),
        #         ],
        #     ),
        #     transformations=[
        #         ToOperator(
        #             nid="0", name=Where("1", "text"), language=Where("1", "language")
        #         ),
        #         Delete(nodes=["1"]),
        #     ],
        # ),
        # # !!! R
        # # Question:
        # # Vague. Here we are transforming the argument to a parameter, and we are getting it's name, type and value
        # # from the 2 node, which is a Wildcard. The type here specially is Operator, which is not consistent with the types
        # # we expect for a Parameter, like int, string, float.
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type="keyword_argument",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "1": Node(
        #                 type="identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "2": Wildcard(type="Operator"),
        #         },
        #         edges=[
        #             Edge(source="0", destination="1"),
        #             Edge(source="0", destination="2"),
        #         ],
        #     ),
        #     transformations=[
        #         ToParameter(
        #             nid="0",
        #             name=Where("1", "text"),
        #             type=Where("2", "type"),
        #             value=Where("2", "text"),
        #         ),
        #         Delete(nodes=["1"]),
        #     ],
        # ),
        # # !!! R
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type="keyword_argument",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "1": Node(
        #                 type="identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "2": Node(
        #                 type=basic + "|list|attribute",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #         },
        #         edges=[
        #             Edge(source="0", destination="1"),
        #             Edge(source="0", destination="2"),
        #         ],
        #     ),
        #     transformations=[
        #         ToParameter(
        #             nid="0",
        #             name=Where("1", "text"),
        #             type=Where("2", "type"),
        #             value=Where("2", "text"),
        #         ),
        #         Delete(nodes=["1", "2"]),
        #     ],
        # ),
        # # !!! R
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '0': Wildcard(type='Operator'),
        #             '1': Wildcard(type='Parameter')
        #         },
        #         edges=[
        #             Edge(source='0', destination='1'),
        #         ]
        #     ),
        #     transformations=[
        #         Flip(edges=[('0', '1')])
        #     ]
        # ),
        # # !!! R
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '0': Node(type='dictionary', text=wildcard, language=PYTHON),
        #             '1': Node(type='pair', text=wildcard, language=PYTHON),
        #             '2': Node(type=basic, text=wildcard, language=PYTHON),
        #             '3': Node(type=wildcard, text=wildcard, language=PYTHON)
        #         },
        #         edges=[
        #             Edge(source='0', destination='1'),
        #             Edge(source='1', destination='2'),
        #             Edge(source='1', destination='3'),
        #         ]
        #     ),
        #     transformations=[
        #         Delete(nodes=['1'], edges=[('0', '1'), ('1', '2'), ('1', '3')]),
        #         Add(edges=[('2', '3')]),
        #     ]
        # ),
        # # !!! R
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '0': Node(type='call', text=wildcard, language=PYTHON),
        #             '1': Node(type='attribute', text=wildcard, language=PYTHON),
        #             '2': Node(type='argument_list', text=wildcard, language=PYTHON),
        #         },
        #         edges=[
        #             Edge(source='0', destination='1'),
        #             Edge(source='0', destination='2')
        #         ]
        #     ),
        #     transformations=[
        #         Delete(nodes=['0', '2'], edges=[('0', '1'), ('0', '2')]),
        #     ]
        # ),
        # # !!! R
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '0': Node(type='module|expression_statement', text=wildcard, language=PYTHON),
        #         },
        #         edges=[]
        #     ),
        #     transformations=[Delete(nodes=['0'])]
        # ),
    ]