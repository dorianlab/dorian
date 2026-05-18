"""
dorian/pipeline/mitigation_rewrites.py
---------------------------------------
KB-driven mitigation rewrite system.

Rewrite rules are first-class citizens:
  - **Docstore** ``doc_rewrites`` collection stores serialised ``RewriteRule`` bodies
    (pattern + transformations list — same structure as ``dorian/code/parsing/rule``).
  - **Neo4j KB** stores ontological metadata: ``applies_to`` (interface targeting),
    ``has_input`` (IO port anchoring), ``has_parameter`` (parameter signature).

The factory ``build_mitigation_rewrite()`` fetches the docstore doc, compiles it
into a ``RewriteRule``, and returns a ``(dag) -> DAG`` closure.

Named Apply functions
~~~~~~~~~~~~~~~~~~~~~
Complex edge manipulation (rerouting) that cannot be expressed as atomic
``Add``/``Delete`` operations is encoded as named ``Apply`` references in the
docstore doc.  The compiler resolves these to Python closures at compile time:

  - ``reroute_outgoing(from, through)``  — insert_after
  - ``reroute_incoming(to, through, anchor?)``  — insert_before
  - ``replace_node(target, new_node)``  — replace_operator
  - ``duplicate_data_kwarg(target, source_position, kwarg_name)``  — add_data_kwarg

See also
--------
dorian/pipeline/transforms.py   – DATASET_EXPANSION_RULE, COMPOUND_OPERATOR_EXPANSION_RULE
dorian/code/parsing/rule.py     – Apply, RewriteRule, Add (canonical primitives)
dorian/dag.py                   – DAG, Operator, Parameter, Edge
backend/infra/dbs/expdb/seed_rewrites.py  – seed data
"""
from __future__ import annotations

import json
import os
import re as _re
from uuid import uuid4

from dorian.code.parsing.rule import Add, Apply, RewriteRule
from dorian.dag import DAG, Edge, Group, Node, Operator, Parameter, Snippet
from dorian.pipeline.transforms import sync_apply
from backend.events import Event, aemit


# Default ON: route primitive-op transformations through the Rust
# evaluator (``dorian_native.apply_primitives`` / ``Pipeline.sync_apply_rule``)
# instead of the Python ``_make_*_primitive`` factories. Both paths
# produce identical post-DAGs (parity-tested against all 23 KB
# rewrites); the rust path wins on the steady-state architecture
# (shared ``Pipeline`` handle across rules: 3.8× faster on the
# 5-rule sweep bench). Set ``DORIAN_USE_RUST_REWRITES=0`` to force
# the legacy python evaluator — investigation only.
_USE_RUST_REWRITES = (
    os.environ.get("DORIAN_USE_RUST_REWRITES", "1").lower()
    not in ("0", "false", "no", "off")
)


# ═══════════════════════════════════════════════════════════════════════════
# Named Apply function factories
# ═══════════════════════════════════════════════════════════════════════════

def _make_reroute_outgoing(source_key: str, through_key: str):
    """Reroute all outgoing edges from ``mapping[source_key]`` through
    ``mapping[through_key]``.

    ``n → X``  becomes  ``n → through → X``
    """
    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        if source_key not in mapping:
            raise KeyError(
                f"reroute_outgoing: '{source_key}' not in mapping "
                f"(available keys: {list(mapping.keys())}). "
                f"Ensure a preceding Add transformation defines node '{source_key}'."
            )
        if through_key not in mapping:
            raise KeyError(
                f"reroute_outgoing: '{through_key}' not in mapping "
                f"(available keys: {list(mapping.keys())}). "
                f"Ensure a preceding Add transformation defines node '{through_key}'."
            )
        src = mapping[source_key]
        through = mapping[through_key]
        outgoing = [e for e in dag.edges if e.source == src and e.destination != through]
        kept = [e for e in dag.edges if e not in outgoing]
        new_edges = [
            Edge(through, e.destination, position=e.position, output=0)
            for e in outgoing
        ]
        return DAG(nodes=dag.nodes, edges=kept + new_edges)
    return f


def _make_reroute_incoming(target_key: str, through_key: str, anchor: str | None = None):
    """Reroute incoming edges to ``mapping[target_key]`` through
    ``mapping[through_key]``.

    ``X → target``  becomes  ``X → through → target``

    When *anchor* is set, only edges whose ``position`` matches the anchor
    port name are intercepted (e.g. ``"messages"`` for LLM prompt input).
    """
    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        if target_key not in mapping:
            raise KeyError(
                f"reroute_incoming: '{target_key}' not in mapping "
                f"(available keys: {list(mapping.keys())}). "
                f"Ensure a preceding Add transformation defines node '{target_key}'."
            )
        if through_key not in mapping:
            raise KeyError(
                f"reroute_incoming: '{through_key}' not in mapping "
                f"(available keys: {list(mapping.keys())}). "
                f"Ensure a preceding Add transformation defines node '{through_key}'."
            )
        tgt = mapping[target_key]
        through = mapping[through_key]

        # Determine which edges to intercept
        incoming = [
            e for e in dag.edges
            if e.destination == tgt and e.source != through
        ]

        if anchor:
            # Only intercept non-Parameter edges matching the anchor port
            incoming = [
                e for e in incoming
                if str(e.position) == anchor
                and not isinstance(dag.nodes.get(e.source), Parameter)
            ]
        else:
            # Intercept non-Parameter data edges (traditional sklearn behaviour).
            # We exclude Parameter sources because they are kwargs/config, not
            # data flow that should be rerouted through the new operator.
            incoming = [
                e for e in incoming
                if not isinstance(dag.nodes.get(e.source), Parameter)
            ]

            # Narrow further to the FEATURE-FLOW ports of the target. A
            # transformer inserted upstream (OrdinalEncoder, StandardScaler,
            # …) produces X_transformed and only replaces feature inputs —
            # it has no relationship to the label flow (y) or to test-side
            # inputs (X_test) which the same transformer cannot fit-then-
            # transform in-chain today (see notes in
            # ``dorian/knowledge/sources/interfaces.py``). Without this
            # filter the rewrite greedily grabs y_train and X_test too,
            # breaking fit.y and predict.X downstream.
            #
            # KB I/O names are strings ("X", "y") but edge.position is
            # typically int (0, 1) for positional args. Build an allow-set
            # that maps BOTH the string name and its numeric position so
            # the filter works regardless of which coordinate system the
            # edges use.
            target_node = dag.nodes.get(tgt)
            if isinstance(target_node, (Operator, Group)):
                op_name = target_node.name
                io_specs = _get_io_input_specs(op_name)
                if io_specs:
                    allowed = set()
                    for spec in io_specs:
                        name = spec.get("name") or ""
                        # Feature-flow heuristic: input name starts with
                        # "X" (X, X_train, X_test, X_transformed, …).
                        # Everything else — "y", "y_train", "messages",
                        # "model" — stays connected to the target.
                        if not name.upper().startswith("X"):
                            continue
                        allowed.add(name)
                        pos = spec.get("position")
                        if pos is not None:
                            allowed.add(pos)           # int
                            allowed.add(str(pos))      # str("0")
                    incoming = [e for e in incoming if e.position in allowed]

        kept = [e for e in dag.edges if e not in incoming]
        new_edges = [
            Edge(e.source, through, position=e.position, output=e.output)
            for e in incoming
        ]
        return DAG(nodes=dag.nodes, edges=kept + new_edges)
    return f


def _make_replace_node(target_key: str, new_node_spec: dict):
    """Replace ``mapping[target_key]`` with a new Operator node (in-place rename)."""
    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        nid = mapping[target_key]
        old = dag.nodes.get(nid)
        if not isinstance(old, Operator):
            return dag
        name = new_node_spec.get("name", old.name)
        new_nodes = dict(dag.nodes)
        new_nodes[nid] = Operator(name=name, language=old.language, tasks=old.tasks)
        return DAG(nodes=new_nodes, edges=list(dag.edges))
    return f


def _make_set_param_value(through_key: str, param_name: str, new_value: str, new_dtype: str = ""):
    """Rewrite the ``value`` (and optionally ``dtype``) of the
    Parameter satellite named *param_name* feeding
    ``mapping[through_key]``. Local — touches one node, no edge
    changes.

    Use case: auto-sklearn / FLAML config defaults sometimes ship
    a hyperparameter value that the auto-* validator accepts but
    sklearn's runtime parameter-constraint check rejects (e.g.
    ``FastICA(whiten=True)`` — sklearn ≥1.3 requires
    ``'unit-variance'`` or ``False``; ``SGDClassifier(penalty=1)``
    — must be a string). Pattern matches the Operator (per
    ``compile_rewrite_rule``'s fixed contract); the Apply walks
    the operator's incoming edges to find the Parameter satellite
    by ``name`` and rewrites its value.
    """
    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        op_id = mapping.get(through_key)
        if op_id is None:
            return dag
        param_id = None
        for e in dag.edges:
            if e.destination != op_id:
                continue
            src = dag.nodes.get(e.source)
            if isinstance(src, Parameter) and src.name == param_name:
                param_id = e.source
                break
        if param_id is None:
            return dag
        old = dag.nodes.get(param_id)
        if not isinstance(old, Parameter):
            return dag
        new_nodes = dict(dag.nodes)
        new_nodes[param_id] = Parameter(
            name=old.name,
            dtype=new_dtype or old.dtype,
            value=new_value,
        )
        return DAG(nodes=new_nodes, edges=list(dag.edges))
    return f


def _make_force_random_state(through_key: str = "n", seed_param: str = "random_state"):
    """Inject a deterministic seed Parameter on the Operator at
    ``mapping[through_key]`` if no Parameter is wired to its
    ``seed_param`` handle.

    Two callers:

    * **AI Debugger / canvas** — surfaces this rewrite as a
      `MissingRandomState` mitigation suggestion. The user accepts
      or rejects per the standard mitigation flow. When accepted,
      the seed value comes from `meta.get("random_state_seed")`
      (frontend may inject a user-chosen seed) or defaults to a
      hash of the node id (stable across re-renders).

    * **RL / AutoML / cross-product trial loop** — auto-applies
      this rewrite without UI before every trial. The seed is
      derived from `meta["trial_id"]` so identical
      `(template, config, trial_id)` produces identical pipelines
      and the intermediates cache stays consistent.

    The Apply is idempotent: if a Parameter is already wired to
    `seed_param`, it does nothing — the user's explicit choice (or
    an earlier binder pass) is preserved.

    Cache-correctness coupling: the rust-side cache crate's
    `eligibility_with_incoming` returns Bypass for any Operator
    where the seed handle is unwired, so an unforced trial never
    hits the cache. The whole point of this rewrite is to flip
    those Bypass results to Cacheable.
    """
    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        op_id = mapping.get(through_key)
        if op_id is None:
            return dag
        op = dag.nodes.get(op_id)
        if not isinstance(op, Operator):
            return dag
        # Idempotency check: an existing Parameter wired to the seed
        # handle means we leave the DAG alone.
        for e in dag.edges:
            if e.destination != op_id:
                continue
            if e.position != seed_param:
                continue
            src = dag.nodes.get(e.source)
            if isinstance(src, Parameter):
                return dag
        # Derive a 32-bit seed.
        #
        # Trial-loop callers don't pass `trial_id` — and shouldn't.
        # Two trials that propose the same template + config must
        # produce identical operator firings so the intermediates
        # cache hits across them. A timestamp-based trial_id would
        # vary the seed and break that invariant. The sane default
        # is a content-stable fingerprint: position of the operator
        # in the pipeline + its FQN + the canonicalised parameters
        # already wired to it. Same template+config → same fingerprint
        # → same seed → cache key collides → hit.
        #
        # End users on the canvas can override by setting their own
        # Parameter on the seed handle (the idempotency check above
        # respects it). The AI Debugger surfaces this rewrite as a
        # suggestion they accept/reject.
        trial_id = meta.get("trial_id")
        if trial_id is not None:
            seed_value = _derive_seed(str(trial_id), str(op_id), op.name)
        else:
            seed_value = _derive_stable_seed(dag, op_id, op.name, seed_param)
        param_id = str(uuid4())
        new_nodes = dict(dag.nodes)
        new_nodes[param_id] = Parameter(
            name=seed_param,
            dtype="int",
            value=str(seed_value),
        )
        new_edges = list(dag.edges)
        new_edges.append(Edge(
            source=param_id,
            destination=op_id,
            position=seed_param,
            output=0,
        ))
        return DAG(nodes=new_nodes, edges=new_edges)
    return f


def _canonicalise_param_value(dtype: str, value: str) -> str:
    """Float Parameters are normalised to N significant digits so
    arithmetic-noise in their construction doesn't shift the seed
    (and therefore the cache key) for two trials that nominally use
    the same value. The precision is read from
    ``DORIAN_PARAM_PRECISION`` (default 12) — same env var the
    intermediates cache reads, so the seed derived here always
    matches the precision the cache will hash against. Coarse-to-fine
    exploration (lower precision early, refine over time) is opted
    into by setting that env var per trial.

    Mirrored from
    ``dorian.exec.intermediates_cache.canonicalise_param_string``;
    duplicated here to keep this file's imports lean."""
    if dtype in ("float", "Float") and value:
        try:
            digits = _resolve_precision_digits()
            return f"{float(value):.{digits}g}"
        except (ValueError, TypeError):
            return value
    return value


def _resolve_precision_digits() -> int:
    raw = os.environ.get("DORIAN_PARAM_PRECISION")
    if raw:
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    return 12


def _derive_stable_seed(dag: DAG, op_id: str, op_fqn: str, seed_param: str) -> int:
    """Content-stable seed derivation. The seed depends on:
      * the operator FQN
      * the canonicalised parameter bindings already wired to this
        operator (sorted by handle, dtype + value)
      * the seed_param being wired (so RandomForest with two seed
        params, e.g. random_state + np_random_state, gets distinct
        values — no two seeds in one operator collide)

    Crucially it does NOT depend on:
      * the node UUID (varies per template instantiation)
      * a trial-instance timestamp (varies per trial)
      * any upstream content hash (varies per dataset)

    That's intentional. The intermediates cache memoises by
    `(op_fqn, params, upstream_keys)`. The seed is a parameter, so
    it enters the cache key. We want identical-FQN-with-identical-
    other-params operators to share their seed value across
    different pipelines that happen to use the same operator with
    the same hyperparameters — which is the cache-hit case.
    """
    import hashlib
    h = hashlib.sha256()
    h.update(b"force_seed_v1\x00")
    h.update(op_fqn.encode("utf-8"))
    h.update(b"\x00")
    h.update(seed_param.encode("utf-8"))
    h.update(b"\x00")
    # Canonicalise existing parameter bindings on this op.
    bindings: list[tuple[str, str, str]] = []
    for e in dag.edges:
        if e.destination != op_id:
            continue
        src = dag.nodes.get(e.source)
        if not isinstance(src, Parameter):
            continue
        handle = str(e.position)
        if handle == seed_param:
            continue  # we're about to inject this
        bindings.append(
            (handle, src.dtype, _canonicalise_param_value(src.dtype, src.value)),
        )
    bindings.sort()
    for handle, dtype, value in bindings:
        h.update(handle.encode("utf-8"))
        h.update(b"=")
        h.update(dtype.encode("utf-8"))
        h.update(b":")
        h.update(value.encode("utf-8"))
        h.update(b"\x00")
    return int.from_bytes(h.digest()[:4], "big") & 0xFFFFFFFF


def _derive_seed(trial_id: str, node_id: str, op_fqn: str) -> int:
    """SHA-256-based 32-bit seed derivation. Same scheme the rust
    side uses for cache key components — keep them aligned."""
    import hashlib
    h = hashlib.sha256()
    h.update(b"trial:")
    h.update(trial_id.encode("utf-8"))
    h.update(b"\x00node:")
    h.update(node_id.encode("utf-8"))
    h.update(b"\x00op:")
    h.update(op_fqn.encode("utf-8"))
    return int.from_bytes(h.digest()[:4], "big") & 0xFFFFFFFF


def _make_param_to_snippet(through_key: str, param_name: str, fqn_map: dict, default_fqn: str):
    """Replace the Parameter satellite named *param_name* feeding
    ``mapping[through_key]`` with a Snippet that imports + returns a
    callable.

    Use case: sklearn parameters typed as *callable*
    (``SelectKBest.score_func``, ``FeatureAgglomeration.pooling_func``)
    can't be carried by a ``Parameter`` — the resolver evaluates
    ``eval(dtype)(value)`` and produces an int / string, not a
    function reference. auto-sklearn enumerates the choice as an
    integer index or a short alias (``'mean'`` / ``'median'``);
    sklearn's ``InvalidParameterError`` rejects both at construction
    time with "must be a callable".

    Pattern matches the Operator (per ``compile_rewrite_rule``'s
    fixed contract — the LHS is always a single Operator at key
    ``"n"``). The Apply walks the matched Operator's incoming
    edges to find the Parameter satellite whose ``name`` equals
    *param_name*, then in-place swaps it for a Snippet whose body
    is::

        def foo():
            from <module> import <fn>
            return <fn>

    with the ``<fn>`` looked up in ``fqn_map`` keyed on the
    Parameter's existing ``value`` (``score_func=2`` →
    ``mutual_info_classif`` per auto-sklearn's enumeration).
    Unknown values fall back to ``default_fqn``. The outgoing edge
    keeps the parameter ``name`` as kwarg position, so the
    Snippet is invoked the same way the Parameter was — the
    resolver dispatches Snippets by calling ``foo()`` and the
    consumer receives the function reference as its kwarg value.
    """
    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        op_id = mapping.get(through_key)
        if op_id is None:
            return dag
        # Find the Parameter satellite matching ``param_name`` among
        # the matched Operator's incoming edges.
        param_id = None
        for e in dag.edges:
            if e.destination != op_id:
                continue
            src = dag.nodes.get(e.source)
            if isinstance(src, Parameter) and src.name == param_name:
                param_id = e.source
                break
        if param_id is None:
            return dag
        old = dag.nodes.get(param_id)
        if not isinstance(old, Parameter):
            return dag
        key = (old.value or "").strip()
        fqn = fqn_map.get(key, default_fqn)
        if not fqn or "." not in fqn:
            return dag
        mod, _, fn = fqn.rpartition(".")
        code = (
            f"def foo():\n"
            f"    from {mod} import {fn}\n"
            f"    return {fn}\n"
        )
        new_nodes = dict(dag.nodes)
        new_nodes[param_id] = Snippet(
            name=old.name or "callable",
            code=code,
            language="python",
        )
        return DAG(nodes=new_nodes, edges=list(dag.edges))
    return f


def _make_insert_dense_converter_before(through_key: str):
    """Insert a Snippet that calls ``.toarray()`` on its input
    upstream of ``mapping[through_key]``. Mirrors
    :func:`_make_insert_x_preprocessor` but emits a Snippet (no
    Operator/sklearn class to wrap) and is generic over the
    splitter / compound-port shape — works on any consumer with
    a single positional X input.

    Use case: ``OneHotEncoder``, ``CountVectorizer``,
    ``RBFSampler`` produce sparse matrices that downstream
    classifiers (``MLPClassifier``, ``GaussianNB``) reject with
    ``TypeError: Sparse data was passed for X, but dense data
    is required``. Splicing the converter immediately upstream
    of the failing node fixes the contract without changing the
    pipeline shape.
    """
    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        target_id = mapping.get(through_key)
        if target_id is None or target_id not in dag.nodes:
            return dag
        # Find the X edge into the target — convention: the first
        # incoming non-Parameter edge with the smallest int-castable
        # position (the resolver routes ``self`` at pos=0, ``X`` at
        # pos=1; for non-method-shortcut Operators ``X`` lands at
        # pos=0). We splice on the X edge regardless.
        incoming_x = None
        for e in dag.edges:
            if e.destination != target_id:
                continue
            src = dag.nodes.get(e.source)
            if isinstance(src, Parameter):
                continue
            incoming_x = e
            break
        if incoming_x is None:
            return dag
        from uuid import uuid4
        conv_id = f"{target_id}_dense_{uuid4().hex[:4]}"
        new_nodes = dict(dag.nodes)
        new_nodes[conv_id] = Snippet(
            name="to_dense",
            code=(
                "def foo(x):\n"
                "    return x.toarray() if hasattr(x, 'toarray') else x\n"
            ),
            language="python",
        )
        new_edges: list[Edge] = []
        for e in dag.edges:
            if e is incoming_x:
                # Original: src → target. After: src → converter → target.
                new_edges.append(Edge(
                    source=e.source,
                    destination=conv_id,
                    position=0,
                    output=e.output,
                ))
                new_edges.append(Edge(
                    source=conv_id,
                    destination=e.destination,
                    position=e.position,
                    output=0,
                ))
            else:
                new_edges.append(e)
        return DAG(nodes=new_nodes, edges=new_edges)
    return f


def _make_insert_label_encoder_before(through_key: str):
    """Splice a label-encoder Snippet between the y-source and ALL
    its consumers, upstream of ``mapping[through_key]``.

    Use case: classifiers (LightGBM, RandomForest, ExtraTrees) reject
    non-zero-indexed / string ``y``. FLAML's TabularPredictor encodes
    y internally; the imported pipeline doesn't. We need to apply the
    SAME encoding to every consumer of y so train (fit) and test
    (accuracy_score) end up in the same integer space.

    Strategy: find the matched operator's y input edge (the
    non-Parameter incoming whose source isn't an X-feature feeder),
    walk back to its source node, then re-route every edge whose
    source is that y-source through a freshly-spawned Snippet that
    runs ``pd.Categorical(y).codes`` (alphabetical-by-default →
    deterministic across any two slices of the same column).

    The Snippet's first incoming edge is the y-source; its single
    outgoing forking pattern matches every original consumer of
    y_source. Snippets in Dorian have one logical output (the return
    value of ``foo``) so consumers see a single port — exactly what
    they were getting from the y-source pre-rewrite.
    """
    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        target_id = mapping.get(through_key)
        if target_id is None or target_id not in dag.nodes:
            return dag
        # The y-source is the non-Parameter incoming whose ``position``
        # matches the classifier's y slot — for compound-expanded
        # Sklearn Estimator that's the fit method's position 2 (y),
        # but pre-expansion (when the operator is still the matched
        # "n") the y edge sits at the original Operator-level position
        # 1 (Sklearn Estimator interface input ``y@1``). Try both.
        # Ignore the X edge (position 0 / "X" / "X_train") to avoid
        # mis-encoding features.
        y_edge = None
        for e in dag.edges:
            if e.destination != target_id:
                continue
            src = dag.nodes.get(e.source)
            if isinstance(src, Parameter):
                continue
            pos = e.position
            try:
                pos_i = int(pos)
            except (TypeError, ValueError):
                pos_i = -1
            if pos_i in (1, 2) or pos in ("y", "y_train"):
                y_edge = e
                break
        if y_edge is None:
            return dag
        y_source = y_edge.source
        from uuid import uuid4
        enc_id = f"{target_id}_label_encoder_{uuid4().hex[:4]}"
        new_nodes = dict(dag.nodes)
        new_nodes[enc_id] = Snippet(
            name="label_encoder",
            code=(
                "def foo(y):\n"
                "    import pandas as pd\n"
                "    return pd.Categorical(y).codes\n"
            ),
            language="python",
        )
        new_edges: list[Edge] = []
        wired_input = False
        for e in dag.edges:
            if e.source == y_source:
                # Re-route every consumer of the y-source through the
                # encoder. The encoder reads the y-source ONCE
                # (idempotent below).
                if not wired_input:
                    new_edges.append(Edge(
                        source=y_source,
                        destination=enc_id,
                        position=0,
                        output=e.output,
                    ))
                    wired_input = True
                new_edges.append(Edge(
                    source=enc_id,
                    destination=e.destination,
                    position=e.position,
                    output=0,
                ))
            else:
                new_edges.append(e)
        return DAG(nodes=new_nodes, edges=new_edges)
    return f


def _make_replace_node_refresh(target_key: str, new_op_key: str):
    """Replace ``mapping[target_key]`` with a fresh Operator node and
    rebuild its Parameter satellite sub-DAG from the catalog's
    declared defaults for ``new_op_key``. Unlike :func:`_make_replace_node`
    (pure rename), this:

      * Drops incoming edges whose port name doesn't exist on the new
        op's inputs (variadic fallback kept).
      * Drops outgoing edges whose output slot index is out of range
        for the new op.
      * Strips the existing Parameter satellites targeting the node
        and materialises fresh ones from ``new_op.parameters``.

    Used by the RL env's in-place operator swap primitive
    (``ReplaceNodeSpec``) where both the hyperparameter signature and
    the port shape may differ between old and new op. Also available
    to DB-authored rewrite rules via the Apply registry.
    """
    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        from rl.catalog.loader import catalog_by_key as _catalog_by_key
        from rl.catalog.loader import seed_catalog_with_guards
        import uuid as _uuid
        catalog = meta.get("rl_catalog")
        if catalog is None:
            catalog = seed_catalog_with_guards()
        by_key = _catalog_by_key(catalog)
        new_op = by_key.get(new_op_key)
        if new_op is None:
            return dag
        nid = mapping.get(target_key)
        old = dag.nodes.get(nid) if nid else None
        if not isinstance(old, Operator):
            return dag

        # Catalog of old param satellites feeding this node — collected
        # BEFORE filtering so the port-name drop doesn't hide them.
        old_param_ids = {
            e.source for e in dag.edges
            if e.destination == nid
            and isinstance(dag.nodes.get(e.source), Parameter)
        }

        new_input_names = {p.name for p in new_op.inputs}
        variadic_inputs = {p.name for p in new_op.inputs if getattr(p, "variadic", False)}
        n_outputs = len(new_op.outputs)

        def _keep_incoming(e) -> bool:
            if e.destination != nid:
                return True
            pos = str(e.position)
            if pos in new_input_names:
                return True
            return bool(variadic_inputs) and pos.isdigit()

        def _keep_outgoing(e) -> bool:
            if e.source != nid:
                return True
            try:
                return 0 <= int(e.output) < n_outputs
            except (TypeError, ValueError):
                return False

        new_edges = [
            e for e in dag.edges
            if e.source not in old_param_ids
            and _keep_incoming(e)
            and _keep_outgoing(e)
        ]

        new_nodes = dict(dag.nodes)
        for pid in old_param_ids:
            new_nodes.pop(pid, None)
        new_nodes[nid] = Operator(
            name=new_op.op_key, language=old.language, tasks=list(old.tasks or ()),
        )
        for p in new_op.parameters:
            if p.default is None:
                continue
            pid = f"{nid}_p_{p.name}_{_uuid.uuid4().hex[:3]}"
            new_nodes[pid] = Parameter(
                name=p.name, dtype=p.dtype, value=str(p.default),
            )
            new_edges.append(Edge(
                source=pid, destination=nid,
                position=p.name, output=0,
            ))

        return DAG(nodes=new_nodes, edges=new_edges)
    return f


def _splice_preprocessor_on_x_edge(dag: DAG, pre_id: str) -> DAG:
    """No-splitter fallback for ``insert_x_preprocessor``.

    The encoder/preprocessor was added as a node by a preceding
    ``Add`` transformation, with one edge ``encoder → n`` connecting
    it to the matched downstream consumer. The contract:

      1. Find the consumer ``n`` (destination of the encoder→n edge
         the Add introduced).
      2. Identify the X-source — whichever upstream node feeds X
         into ``n`` (smallest int position is the X / fit slot).
      3. Reroute EVERY edge from that X-source into ``n`` (including
         X_test at position 2 in Sklearn Estimator wiring) through
         the encoder's outgoing port. Otherwise predict's X_test
         edge stays raw and the same string-column failure recurs
         on the unencoded test data.
      4. Wire the X-source through the encoder's X input at position
         0 once.

    Used for FLAML / canvas-without-split pipelines where X_train
    and X_test ultimately come from the same data source (the
    subscript-of-df pair, or the dorian.io.dataset's X output).
    """
    consumer_id = next(
        (e.destination for e in dag.edges if e.source == pre_id),
        None,
    )
    if consumer_id is None:
        return dag

    # Pick the X-edge: smallest int position, non-Parameter source,
    # source not the encoder.
    x_edges = []
    for e in dag.edges:
        if e.destination != consumer_id:
            continue
        if e.source == pre_id:
            continue
        if isinstance(dag.nodes.get(e.source), Parameter):
            continue
        try:
            pos_i = int(e.position)
        except (TypeError, ValueError):
            continue
        x_edges.append((pos_i, e))
    if not x_edges:
        return dag
    x_edges.sort()
    x_source = x_edges[0][1].source
    # Every consumer-incoming edge whose source IS the X-source gets
    # rewired through the encoder. Edges from other sources (y,
    # secondary features) stay direct.
    rewire_set = {e for _, e in x_edges if e.source == x_source}

    new_edges: list[Edge] = []
    rewired_x_in = False
    rewired_pre_out = False
    for e in dag.edges:
        if e in rewire_set:
            # Replace this X-source → consumer edge with
            # encoder → consumer at the same position.
            new_edges.append(Edge(
                source=pre_id, destination=consumer_id,
                position=e.position, output=0,
            ))
            rewired_pre_out = True
            continue
        if e.source == pre_id and e.destination == consumer_id:
            # Drop the placeholder edge added by the preceding
            # ``Add`` transformation — we've replaced it with a
            # properly-positioned edge above.
            continue
        new_edges.append(e)
    # Wire X-source into the encoder once (position 0 = encoder's X).
    new_edges.append(Edge(
        source=x_source, destination=pre_id,
        position=0, output=x_edges[0][1].output,
    ))
    rewired_x_in = True
    if not rewired_x_in or not rewired_pre_out:
        return dag
    return DAG(nodes=dag.nodes, edges=new_edges)


def _make_insert_x_preprocessor(through_key: str):
    """Splice ``mapping[through_key]`` between the splitter and its
    X-path consumers, wiring the preprocessor's own compound inputs
    (X_train, y_train, X_test) from the splitter in one pass.

    Specific to the RL env's compound-shape preprocessor contract:
    OrdinalEncoder / SimpleImputer / StandardScaler all take
    ``(X_train, y_train, X_test)`` and emit
    ``(X_train_encoded, X_test_encoded)``. y_train flows through
    for the encoder's own fit, but consumers downstream of the
    splitter's y_train output read y_train directly — it doesn't
    need transformation. This Apply encodes that asymmetry so
    DB-stored rewrites can express "wrap the X paths" in one line.

    Looks for a ``sklearn.model_selection.train_test_split`` Operator
    in the DAG as the splitter anchor. Refuses to rewrite when no
    splitter is present (prevents the preprocessor from dangling).
    """
    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        pre_id = mapping.get(through_key)
        if pre_id is None:
            return dag
        pre_node = dag.nodes.get(pre_id)
        if not isinstance(pre_node, Operator):
            return dag

        splitter_id = next(
            (nid for nid, n in dag.nodes.items()
             if isinstance(n, Operator)
             and n.name == "sklearn.model_selection.train_test_split"),
            None,
        )
        if splitter_id is None:
            # No splitter (FLAML / canvas-without-split shape) —
            # splice the preprocessor on the X edge feeding the
            # downstream consumer of `pre_id` (the matched op `n`).
            # The matched op is identified via the encoder→n edge
            # added by the preceding ``Add`` transformation in the
            # rewrite doc.
            return _splice_preprocessor_on_x_edge(dag, pre_id)

        # Cycle guard: the splitter-based splice below adds edges
        # ``splitter → encoder`` (X_train/y_train/X_test). If the
        # matched op ``n`` is UPSTREAM of the splitter (the rewrite
        # was applied to e.g. a pre-split scaler whose forward path
        # is ``proj_X → n → ... → splitter``), then the existing
        # ``encoder → n`` edge added by the preceding ``Add`` plus
        # the new ``splitter → encoder`` edges close a cycle:
        # ``splitter → encoder → n → ... → splitter``. Detect that
        # by walking forward from pre_id; if the splitter is
        # reachable, fall through to the no-splitter splice (which
        # inserts the encoder between the X-source and n directly,
        # without involving the splitter at all).
        outgoing: dict[str, list[str]] = {}
        for _e in dag.edges:
            outgoing.setdefault(_e.source, []).append(_e.destination)
        seen, frontier = {pre_id}, [pre_id]
        while frontier:
            cur = frontier.pop()
            if cur == splitter_id:
                return _splice_preprocessor_on_x_edge(dag, pre_id)
            for nxt in outgoing.get(cur, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)

        catalog = meta.get("rl_catalog") or _default_rl_catalog()
        pre_meta = _meta_by_key(catalog, pre_node.name)
        split_meta = _meta_by_key(catalog, "sklearn.model_selection.train_test_split")
        if pre_meta is None or split_meta is None:
            return dag

        def _output_index(op, name: str) -> int:
            for i, p in enumerate(op.outputs):
                if p.name == name:
                    return i
            return -1

        x_train_out = _output_index(split_meta, "X_train")
        x_test_out = _output_index(split_meta, "X_test")
        y_train_out = _output_index(split_meta, "y_train")
        pre_x_train_out = _output_index(pre_meta, "X_train")
        pre_x_test_out = _output_index(pre_meta, "X_test")

        # Rewire X consumers of the splitter through the preprocessor's
        # transformed outputs. Leave y_train / y_test consumers alone.
        new_edges = []
        for e in dag.edges:
            if (e.source == splitter_id
                    and e.destination != pre_id
                    and e.output == x_train_out):
                new_edges.append(Edge(
                    source=pre_id, destination=e.destination,
                    position=e.position, output=pre_x_train_out,
                ))
            elif (e.source == splitter_id
                    and e.destination != pre_id
                    and e.output == x_test_out):
                new_edges.append(Edge(
                    source=pre_id, destination=e.destination,
                    position=e.position, output=pre_x_test_out,
                ))
            else:
                new_edges.append(e)

        # Ensure the preprocessor itself reads its three inputs from
        # the splitter (idempotent — skip if already wired).
        preset = {
            (e.source, e.destination, str(e.position))
            for e in new_edges
        }

        def _needs_edge(dst_port: str) -> bool:
            return (splitter_id, pre_id, dst_port) not in preset

        if _needs_edge("X_train"):
            new_edges.append(Edge(
                source=splitter_id, destination=pre_id,
                position="X_train", output=x_train_out,
            ))
        if _needs_edge("y_train") and y_train_out >= 0:
            new_edges.append(Edge(
                source=splitter_id, destination=pre_id,
                position="y_train", output=y_train_out,
            ))
        if _needs_edge("X_test"):
            new_edges.append(Edge(
                source=splitter_id, destination=pre_id,
                position="X_test", output=x_test_out,
            ))

        return DAG(nodes=dag.nodes, edges=new_edges)

    return f


def _default_rl_catalog():
    """Lazy import so this module doesn't take a hard rl-package dep."""
    try:
        from rl.catalog.loader import seed_catalog_with_guards
        return seed_catalog_with_guards()
    except Exception:
        return ()


def _meta_by_key(catalog, op_key: str):
    for op in catalog:
        if op.op_key == op_key:
            return op
    return None


def _make_duplicate_data_kwarg(target_key: str, source_position: int, kwarg_name: str):
    """Duplicate a positional data edge as a keyword argument on the target node."""
    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        nid = mapping[target_key]
        # Already has this kwarg?
        if any(e.destination == nid and e.position == kwarg_name for e in dag.edges):
            return dag
        # Find the data edge at source_position
        src_edge = next(
            (e for e in dag.edges
             if e.destination == nid
             and e.position == source_position
             and not isinstance(dag.nodes.get(e.source), Parameter)),
            None,
        )
        if src_edge is None:
            return dag
        new_edges = list(dag.edges)
        new_edges.append(Edge(src_edge.source, nid, position=kwarg_name, output=src_edge.output))
        return DAG(nodes=dag.nodes, edges=new_edges)
    return f


# Registry of named Apply functions (compiler looks them up by name)
_APPLY_REGISTRY: dict[str, callable] = {
    "reroute_outgoing": lambda args: _make_reroute_outgoing(args["from"], args["through"]),
    "reroute_incoming": lambda args: _make_reroute_incoming(
        args["to"], args["through"], args.get("anchor"),
    ),
    "replace_node": lambda args: _make_replace_node(args["target"], args["new_node"]),
    "set_param_value": lambda args: _make_set_param_value(
        args.get("through", "n"),
        args["param_name"],
        args["value"],
        args.get("dtype", ""),
    ),
    "param_to_snippet": lambda args: _make_param_to_snippet(
        args.get("through", "n"),
        args["param_name"],
        args.get("fqn_map") or {},
        args.get("default_fqn", ""),
    ),
    "insert_dense_converter_before": lambda args: _make_insert_dense_converter_before(
        args["through"],
    ),
    "insert_label_encoder_before": lambda args: _make_insert_label_encoder_before(
        args["through"],
    ),
    "replace_node_refresh": lambda args: _make_replace_node_refresh(
        args["target"], args["new_op_key"],
    ),
    "insert_x_preprocessor": lambda args: _make_insert_x_preprocessor(
        args["through"],
    ),
    "duplicate_data_kwarg": lambda args: _make_duplicate_data_kwarg(
        args["target"], int(args["source_position"]), args["kwarg_name"],
    ),
    "force_random_state": lambda args: _make_force_random_state(
        args.get("through", "n"),
        args.get("seed_param", "random_state"),
    ),
}


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _get_io_input_specs(operator_fqn: str) -> list[dict]:
    """Return the interface I/O input port specs for an operator from the KB.

    Each spec is a dict with at least ``name`` and ``position`` keys.
    Returns an empty list when the KB has no info for this operator.
    """
    try:
        from dorian.knowledge.queries import get_operator_interface, get_interface_io
        iface = get_operator_interface(operator_fqn)
        if not iface:
            return []
        inputs, _ = get_interface_io(iface)
        return inputs or []
    except Exception:
        return []


def _deserialize_node(spec: dict):
    """Deserialize a node spec dict into an Operator, Parameter, or Snippet."""
    node_type = spec.get("node_type", "Operator")
    if node_type == "Operator":
        return Operator(name=spec["name"], language=spec.get("language", "python"))
    elif node_type == "Parameter":
        return Parameter(
            name=spec["name"],
            dtype=spec.get("dtype", "str"),
            value=str(spec.get("value", "")),
        )
    else:
        raise ValueError(f"Unknown node_type {node_type!r}")


# ═══════════════════════════════════════════════════════════════════════════
# Rule compiler
# ═══════════════════════════════════════════════════════════════════════════

def _primitive_op_to_apply_fn(prim: dict):
    """Build an ``Apply.f`` from a primitive-op dict.

    Mirrors the semantics of ``engine/graph/src/primitive.rs``:
    closed vocabulary, role-aware EdgeSelector, NodeSelector with
    Id / FromMapping / PayloadKind / All / Any / Not. The Python
    compiler stays the source of truth at apply time until the
    Rust evaluator is wired into the pipeline runner; this Python
    mirror is the bridge so the migrated KB docs work
    immediately, not after the runner refactor.

    When ``DORIAN_USE_RUST_REWRITES`` is set, the body delegates to
    ``dorian_native.apply_primitives`` instead — the Python evaluator
    becomes the parity reference and the Rust path becomes the source
    of truth for the apply pass.
    """
    if _USE_RUST_REWRITES:
        return _make_rust_primitive_apply_fn(prim)
    op_kind = prim.get("op")
    if op_kind == "reroute_edges":
        return _make_reroute_edges_primitive(prim)
    if op_kind == "set_node_payload":
        return _make_set_node_payload_primitive(prim)
    if op_kind == "delete_edges":
        return _make_delete_edges_primitive(prim)
    if op_kind == "delete_node":
        return _make_delete_node_primitive(prim)
    if op_kind == "add_edge":
        return _make_add_edge_primitive(prim)
    if op_kind == "add_node":
        return _make_add_node_primitive(prim)
    if op_kind == "lower_task":
        return _make_lower_task_primitive(prim)
    raise ValueError(f"unknown primitive op {op_kind!r}")


def _dag_to_pg_json(dag: DAG) -> str:
    """Serialise a Dorian DAG to the ``ProcessGraph`` JSON shape that
    the Rust ``apply_primitives`` entry expects: nodes carry a
    ``class_type`` discriminator; edges keep
    source/destination/position/output where ``position`` is a raw
    int (positional) or string (kwarg) and ``output`` is a raw int
    (the rust ``Position`` enum is ``untagged``)."""
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
        elif isinstance(n, Group):
            nodes_out[nid] = {"class_type": "Group", "name": getattr(n, "name", "")}
    edges_out: list[dict] = []
    for e in dag.edges:
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


def _pg_json_to_dag(graph: dict) -> DAG:
    """Inverse of :func:`_dag_to_pg_json` — accept the Rust-side
    ``ProcessGraph`` JSON dict and rebuild a Dorian ``DAG``. Used to
    splice the primitive-op evaluator's output back into the
    Python-driven rewrite pipeline."""
    nodes: dict = {}
    for nid, spec in graph.get("nodes", {}).items():
        ct = spec.get("class_type")
        if ct == "Operator":
            nodes[nid] = Operator(
                name=spec.get("name", ""),
                language=spec.get("language", "python"),
                tasks=list(spec.get("tasks", []) or []),
            )
        elif ct == "Parameter":
            nodes[nid] = Parameter(
                name=spec.get("name", ""),
                dtype=spec.get("dtype", "string"),
                value=str(spec.get("value", "")),
            )
        elif ct == "Snippet":
            nodes[nid] = Snippet(
                name=spec.get("name", ""),
                code=spec.get("code", ""),
                language=spec.get("language", "python"),
            )
    edges = []
    for e in graph.get("edges", []) or []:
        pos = e.get("position")
        out = e.get("output", 0)
        if isinstance(out, dict):
            out = next(iter(out.values()), 0)
        edges.append(Edge(
            source=e["source"],
            destination=e["destination"],
            position=pos,
            output=int(out),
        ))
    return DAG(nodes=nodes, edges=edges)


def _make_rust_primitive_apply_fn(prim: dict):
    """Apply a single primitive-op via the Rust evaluator. Used as the
    fallback when ``compile_rewrite_rule`` can't batch consecutive
    primitives — see :func:`_make_rust_primitive_batch_apply_fn` for the
    batched path that's the default for an all-primitive rule.

    The Python ``_make_add_edge_primitive`` silently no-ops when the
    source / destination selector resolves to 0 or >1 nodes (a
    pattern carried over from migrated ``__needs_primitive_extension__``
    rules whose source binding doesn't exist in the mapping). The
    Rust evaluator returns ``EvalError::AmbiguousSource`` /
    ``AmbiguousDestination`` for the same condition. Catch those at
    the wrapper boundary and fall back to the unchanged DAG so the
    two paths produce identical post-DAGs across all 23 KB rules.
    """
    return _make_rust_primitive_batch_apply_fn([prim])


def _make_rust_primitive_batch_apply_fn(prims: list[dict]):
    """Apply *prims* in one Rust round-trip.

    ``compile_rewrite_rule`` collapses consecutive primitive-op
    transformations into a single ``Apply.f`` that calls
    ``dorian_native.apply_primitives`` with the whole list. The
    rust evaluator runs them in order against the same in-process
    ``ProcessGraph``; the JSON marshalling cost is paid once per
    batch instead of once per primitive, which is the bottleneck on
    the cross-PyO3 path (the boundary itself is microseconds; the
    repeated ``json.dumps``/``serde_json::from_str`` round-trip is
    not).
    """
    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        if not prims:
            return dag
        import dorian_native
        pg_json = _dag_to_pg_json(dag)
        ops_json = json.dumps(list(prims))
        mapping_json = json.dumps(dict(mapping))
        try:
            out = json.loads(
                dorian_native.apply_primitives(pg_json, ops_json, mapping_json)
            )
        except ValueError as exc:
            msg = str(exc)
            if "Ambiguous" in msg:
                # Match the python evaluator's silent-no-op contract
                # for unsatisfiable selectors. Fall back to per-op so
                # the rest of the batch still applies cleanly.
                if len(prims) == 1:
                    return dag
                # Re-run primitives one-by-one in python so the bad
                # one no-ops without dropping the whole batch.
                d = dag
                for p in prims:
                    d = _primitive_op_to_apply_fn_python(p)(d, mapping, meta)
                return d
            raise
        mapping.update(out.get("mapping", {}))
        return _pg_json_to_dag(out.get("graph", {"nodes": {}, "edges": []}))
    return f


def _primitive_op_to_apply_fn_python(prim: dict):
    """Force the python evaluator regardless of the rust opt-in flag.
    Used by the batch fallback above when a single primitive in a
    rust batch fails — the rest of the batch is rerun on the python
    path so a bad selector doesn't drop a multi-op rule on the floor.
    """
    op_kind = prim.get("op")
    if op_kind == "reroute_edges":
        return _make_reroute_edges_primitive(prim)
    if op_kind == "set_node_payload":
        return _make_set_node_payload_primitive(prim)
    if op_kind == "delete_edges":
        return _make_delete_edges_primitive(prim)
    if op_kind == "delete_node":
        return _make_delete_node_primitive(prim)
    if op_kind == "add_edge":
        return _make_add_edge_primitive(prim)
    if op_kind == "add_node":
        return _make_add_node_primitive(prim)
    if op_kind == "lower_task":
        return _make_lower_task_primitive(prim)
    raise ValueError(f"unknown primitive op {op_kind!r}")


# ───────────────────────────────────────────────────────────────────
# Add / Delete → primitive ops conversion (compile-time).
#
# Pre-migration rewrites mix legacy ``Add``/``Delete`` types with
# primitive-op entries. Converting the legacy types into the
# equivalent primitive vocabulary at compile time lets the entire
# rule run as a flat primitive list against the rust ``Pipeline``,
# which is the path that pays zero per-step marshalling.
# ───────────────────────────────────────────────────────────────────

def _node_payload_to_primitive_payload(node) -> dict:
    if isinstance(node, Operator):
        return {
            "payload": "operator",
            "name": node.name,
            "language": node.language,
        }
    if isinstance(node, Parameter):
        return {
            "payload": "parameter",
            "name": node.name,
            "dtype": node.dtype,
            "value": str(node.value),
        }
    if isinstance(node, Snippet):
        return {
            "payload": "snippet",
            "name": node.name,
            "code": node.code,
            "language": node.language,
        }
    raise ValueError(f"cannot convert node payload to primitive: {type(node).__name__}")


def _add_to_primitives(tf: dict) -> list[dict]:
    """Convert an ``Add`` transformation dict to ``add_node`` +
    ``add_edge`` primitives.

    Each named node gets a fresh UUID; the local-id → uuid binding
    flows through the mapping via the ``bind`` field on AddNode so
    subsequent edges can reference both ends by their local-id key.
    Edges resolve their endpoints via ``FromMapping`` so they pick up
    the freshly-bound UUIDs. Edge endpoints that match an
    already-mapped pattern var (e.g. ``n``) flow through the same
    ``FromMapping`` selector — works because the pattern matcher
    seeds those bindings at match time.
    """
    out: list[dict] = []
    nodes = tf.get("nodes") or {}
    if isinstance(nodes, dict):
        for local_id, spec in nodes.items():
            payload = (
                spec
                if isinstance(spec, dict)
                else _node_payload_to_primitive_payload(spec)
            )
            # Normalise legacy ``node_type`` discriminator → primitive ``payload``.
            if isinstance(payload, dict) and "node_type" in payload and "payload" not in payload:
                ct = payload.get("node_type")
                payload = dict(payload)
                payload.pop("node_type", None)
                payload["payload"] = (
                    "operator" if ct == "Operator"
                    else "parameter" if ct == "Parameter"
                    else "snippet" if ct == "Snippet"
                    else ""
                )
            out.append({
                "op": "add_node",
                "id": str(uuid4()),
                "bind": local_id,
                "payload": payload,
            })
    edges = tf.get("edges") or []
    for e in edges:
        if isinstance(e, dict):
            src = e.get("source")
            dst = e.get("destination")
            pos = e.get("position", 0)
            outp = e.get("output", 0)
        else:
            src = getattr(e, "source")
            dst = getattr(e, "destination")
            pos = getattr(e, "position", 0)
            outp = getattr(e, "output", 0)
        out.append({
            "op": "add_edge",
            "source": {"sel": "from_mapping", "key": src},
            "destination": {"sel": "from_mapping", "key": dst},
            "position": pos,
            "output": outp,
        })
    return out


def _delete_to_primitives(tf: dict) -> list[dict]:
    """Convert ``Delete`` to ``delete_node`` + ``delete_edges``
    primitives. Node deletes go first so the edge filter doesn't
    have to also account for nodes about to disappear (the rust
    ``delete_node`` primitive cleans up incident edges in the same
    pass)."""
    out: list[dict] = []
    for n in tf.get("nodes") or []:
        out.append({
            "op": "delete_node",
            "selector": {"sel": "from_mapping", "key": n},
        })
    for e in tf.get("edges") or []:
        src, dst = (e[0], e[1]) if isinstance(e, (list, tuple)) else (e.get("source"), e.get("destination"))
        out.append({
            "op": "delete_edges",
            "selector": {
                "source": {"sel": "from_mapping", "key": src},
                "destination": {"sel": "from_mapping", "key": dst},
            },
        })
    return out


def _build_rust_rule_index_entry(doc: dict, operator_fqn: str, rule_id: str | None = None) -> dict | None:
    """Build the dict shape ``RuleIndex`` consumes: ``{"id", "target_fqn",
    "pattern", "transformations"}``. Same conversion as
    :func:`_build_rust_rule_json` but emits the dict so the caller
    can assemble a list and serialise once.

    Returns ``None`` when the doc has a legacy ``Apply(function=...)``
    transformation that can't run on the rust path — caller falls
    back to the per-rule python compile path.
    """
    primitives: list[dict] = []
    for tf in doc.get("transformations", []) or []:
        tf_type = tf.get("type")
        if tf_type == "Add":
            primitives.extend(_add_to_primitives(tf))
        elif tf_type == "Delete":
            primitives.extend(_delete_to_primitives(tf))
        elif tf_type == "Apply":
            return None
        elif "op" in tf and tf_type is None:
            primitives.append(tf)
        else:
            return None

    return {
        "id": rule_id or doc.get("_id") or doc.get("name") or operator_fqn,
        "target_fqn": operator_fqn,
        "pattern": {
            "nodes": {
                "n": {
                    "class_type": "Node",
                    "type": "Operator",
                    "text": _re.escape(operator_fqn),
                    "language": "python",
                }
            },
            "edges": [],
        },
        "transformations": primitives,
    }


def _build_rust_rule_json(doc: dict, operator_fqn: str) -> str | None:
    """Serialise a rewrite doc as the JSON shape ``Pipeline.sync_apply_rule``
    expects: ``{"pattern": <ProcessGraph>, "transformations": [<prim>, ...]}``.

    Returns ``None`` when the doc has any ``Apply(function=...)`` entry
    with a legacy named function — those run python closures
    (``reroute_outgoing``, ``replace_node_refresh``, …) that the rust
    evaluator can't execute, so the rule has to take the python path.
    """
    primitives: list[dict] = []
    for tf in doc.get("transformations", []) or []:
        tf_type = tf.get("type")
        if tf_type == "Add":
            primitives.extend(_add_to_primitives(tf))
        elif tf_type == "Delete":
            primitives.extend(_delete_to_primitives(tf))
        elif tf_type == "Apply":
            return None  # legacy named-function Apply — python only
        elif "op" in tf and tf_type is None:
            primitives.append(tf)
        else:
            return None

    pattern_json = {
        "nodes": {
            "n": {
                "class_type": "Node",
                "type": "Operator",
                "text": _re.escape(operator_fqn),
                "language": "python",
            }
        },
        "edges": [],
    }
    return json.dumps({"pattern": pattern_json, "transformations": primitives})


# ───────────────────────────────────────────────────────────────────
# Cached process-wide KB rewrite index (RuleIndex pyclass).
#
# Production callers that need "given this pipeline, which KB
# rewrites could apply" go through ``get_kb_rule_index()`` →
# ``RuleIndex.match_pipeline(pipeline)``. The index is built once
# from ``expdb.rewrites`` and reused for the lifetime of the
# process; ``invalidate_kb_rule_index_cache()`` is the explicit
# escape hatch when the seeder writes new rules.
# ───────────────────────────────────────────────────────────────────

_kb_rule_index_cache: object | None = None
_kb_rule_index_lock = __import__("threading").Lock()


def invalidate_kb_rule_index_cache() -> None:
    """Drop the cached KB rule index. Call after the seeder rewrites
    ``expdb.rewrites`` so the next ``get_kb_rule_index()`` rebuilds."""
    global _kb_rule_index_cache
    with _kb_rule_index_lock:
        _kb_rule_index_cache = None


async def get_kb_rule_index():
    """Return the cached ``dorian_native.RuleIndex`` of all KB
    rewrites, building it on first access. Returns ``None`` when
    the rust extension isn't loadable or no rules are storable.

    Each rewrite doc is compiled per-target; we use the doc's
    ``applies_to`` interface (resolved via the KB) to figure out
    which operator FQNs the rule should be indexed under. For docs
    that target a single FQN literally, that's the index key. For
    interface-targeting docs (``applies_to`` of e.g. ``Sklearn
    Estimator``), we fan out into one index entry per operator
    implementing that interface — same compile-time work the AI
    Debugger does today, but cached process-wide.
    """
    global _kb_rule_index_cache
    if _kb_rule_index_cache is not None:
        return _kb_rule_index_cache
    try:
        import dorian_native
    except Exception:
        return None
    from backend.envs import expdb
    from dorian.knowledge.queries import get_operators_by_interface

    docs: list[dict] = []
    try:
        cursor = expdb.rewrites.find({})
        async for doc in cursor:
            docs.append(doc)
    except Exception:
        return None

    entries: list[dict] = []
    for doc in docs:
        applies_to = doc.get("applies_to")
        targets: list[str] = []
        if isinstance(applies_to, str) and applies_to:
            # Interface name → fan out to all operators implementing it.
            try:
                targets = list(get_operators_by_interface(applies_to)) or [applies_to]
            except Exception:
                targets = [applies_to]
        elif isinstance(applies_to, list):
            targets = [str(a) for a in applies_to if a]
        if not targets:
            # Fall back to the doc's slug as the target — many
            # KB rewrites don't carry a separate ``applies_to``
            # because the matching is handled at compile time.
            slug = doc.get("_id") or doc.get("name") or ""
            if slug:
                targets = [slug]
        rid_base = doc.get("_id") or doc.get("name") or "rewrite"
        for target_fqn in targets:
            rid = (
                f"{rid_base}::{target_fqn}"
                if len(targets) > 1
                else str(rid_base)
            )
            entry = _build_rust_rule_index_entry(doc, target_fqn, rule_id=rid)
            if entry is not None:
                entries.append(entry)

    if not entries:
        return None

    with _kb_rule_index_lock:
        if _kb_rule_index_cache is not None:
            return _kb_rule_index_cache
        _kb_rule_index_cache = dorian_native.RuleIndex(json.dumps(entries))
    return _kb_rule_index_cache


async def applicable_rewrites_for(dag: DAG) -> list[tuple[str, dict]]:
    """Return ``(rule_id, mapping)`` for every KB rewrite whose
    pattern matches *dag*. One rust call against the cached index.

    Used by the AI Debugger / MCP to enumerate eligible mitigations
    without per-rule python compile + match. Returns an empty list
    when the rust path isn't available — callers should fall back
    to the slow per-rule scan if they need to.
    """
    idx = await get_kb_rule_index()
    if idx is None:
        return []
    import dorian_native
    pg_json = _dag_to_pg_json(dag)
    pipeline = dorian_native.Pipeline(pg_json)
    raw = json.loads(idx.match_pipeline(pipeline))
    return [(rid, mapping) for rid, mapping in raw]


def _make_pipeline_apply_fn(rule_json: str):
    """Build an ``Apply.f`` that runs the entire rule against a
    rust ``Pipeline`` handle. Marshals the dag once on the way in,
    once on the way out — the match → apply → re-match loop is
    rust-side. Replaces the per-primitive ``Apply.f`` chain when
    the doc compiles cleanly into all-primitive form.
    """
    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        import dorian_native
        pg_json = _dag_to_pg_json(dag)
        pipeline = dorian_native.Pipeline(pg_json)
        try:
            pipeline.sync_apply_rule(rule_json)
        except ValueError as exc:
            msg = str(exc)
            if "Ambiguous" in msg:
                return dag
            raise
        return _pg_json_to_dag(json.loads(pipeline.to_json()))
    return f


def _resolve_node_selector(sel: dict, dag: DAG, mapping: dict) -> list[str]:
    """Return every node id in *dag* matching *sel*.

    Selector dict shape mirrors NodeSelector serialisation:
      {sel: id, id: <node_id>}
      {sel: from_mapping, key: <pattern_var>}
      {sel: payload_kind, payload: operator|parameter|snippet|group}
      {sel: all|any, of: [...]}
      {sel: not, inner: <selector>}
    """
    kind = sel.get("sel")
    if kind == "id":
        target = sel.get("id")
        return [target] if target in dag.nodes else []
    if kind == "from_mapping":
        key = sel.get("key")
        nid = mapping.get(key)
        return [nid] if nid in dag.nodes else []
    if kind == "payload_kind":
        want = (sel.get("payload") or "").lower()
        return [
            nid for nid, n in dag.nodes.items()
            if _node_payload_kind(n) == want
        ]
    if kind == "all":
        of = sel.get("of") or []
        if not of:
            return list(dag.nodes.keys())
        sets = [set(_resolve_node_selector(s, dag, mapping)) for s in of]
        return list(set.intersection(*sets))
    if kind == "any":
        out: set[str] = set()
        for s in sel.get("of") or []:
            out.update(_resolve_node_selector(s, dag, mapping))
        return list(out)
    if kind == "not":
        inner = _resolve_node_selector(sel.get("inner") or {}, dag, mapping)
        all_ids = set(dag.nodes.keys())
        return list(all_ids.difference(inner))
    raise ValueError(f"unknown node selector kind {kind!r}")


def _node_payload_kind(node) -> str:
    if isinstance(node, Operator):
        return "operator"
    if isinstance(node, Parameter):
        return "parameter"
    if isinstance(node, Snippet):
        return "snippet"
    if isinstance(node, Group):
        return "group"
    return ""


def _edge_matches_selector(edge: Edge, sel: dict, dag: DAG, mapping: dict) -> bool:
    """Match an edge against an EdgeSelector dict. Each field is
    optional — empty selector matches everything.

    Role checks (``destination_role`` / ``source_role``) are best-
    effort: when KB lookup is unavailable we fall back to
    name-prefix heuristics matching what the Rust RoleResolver +
    KbTaskTopology answer for the common case (X-prefix → feature_flow,
    y-prefix → label_flow). Acceptable for the rewrite-application
    path; precision improves when the Rust evaluator takes over.
    """
    src = sel.get("source")
    if src is not None:
        srcs = _resolve_node_selector(src, dag, mapping)
        if edge.source not in srcs:
            return False
    dst = sel.get("destination")
    if dst is not None:
        dsts = _resolve_node_selector(dst, dag, mapping)
        if edge.destination not in dsts:
            return False
    pos_pred = sel.get("position")
    if pos_pred is not None and not _position_matches(pos_pred, edge.position):
        return False
    expected_dst_role = sel.get("destination_role")
    if expected_dst_role is not None:
        if _port_role_for_position(edge.position) != expected_dst_role:
            return False
    expected_src_role = sel.get("source_role")
    if expected_src_role is not None:
        if _port_role_for_position(edge.position) != expected_src_role:
            return False
    src_out = sel.get("source_output")
    if src_out is not None:
        try:
            edge_out = int(edge.output)
        except (TypeError, ValueError):
            edge_out = -1
        if edge_out != int(src_out):
            return False
    return True


def _position_matches(pred: dict, pos) -> bool:
    p = (pred or {}).get("pred")
    if p == "index_eq":
        try:
            return int(pos) == int(pred.get("i", -1))
        except (TypeError, ValueError):
            return False
    if p == "keyword_eq":
        return isinstance(pos, str) and pos == pred.get("k")
    if p == "any_index":
        try:
            int(pos)
            return True
        except (TypeError, ValueError):
            return False
    if p == "any_keyword":
        return isinstance(pos, str) and not pos.lstrip("-").isdigit()
    if p == "one_of":
        positions = pred.get("positions") or []
        return any(_position_eq(p_, pos) for p_ in positions)
    return False


def _position_eq(a, b) -> bool:
    if isinstance(a, dict):
        # serialised Position::Index / Keyword
        if "Index" in a:
            return _position_matches({"pred": "index_eq", "i": a["Index"]}, b)
        if "Keyword" in a:
            return _position_matches({"pred": "keyword_eq", "k": a["Keyword"]}, b)
    return a == b


def _port_role_for_position(pos) -> str:
    """Best-effort port-role inference. Mirrors the convention
    KbTaskTopology + RoleResolver carry for the common feature /
    label / model port names. The Rust path consults the KB; this
    Python fallback uses the well-known prefixes that have been
    Dorian's de-facto contract since the catalog seeding.
    """
    if isinstance(pos, str):
        s = pos.lower()
        if s.startswith("x") or s in ("features",):
            return "feature_flow"
        if s.startswith("y") or s in ("labels", "target"):
            return "label_flow"
        if s in ("model", "estimator"):
            return "model_flow"
    return "unknown"


def _make_reroute_edges_primitive(prim: dict):
    selector = prim.get("selector") or {}
    through_sel = prim.get("through") or {}

    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        through_ids = _resolve_node_selector(through_sel, dag, mapping)
        if len(through_ids) != 1:
            raise ValueError(
                f"reroute_edges: 'through' must resolve to exactly one node, got {through_ids}"
            )
        through_id = through_ids[0]
        intercepted = []
        kept = []
        for e in dag.edges:
            if (
                _edge_matches_selector(e, selector, dag, mapping)
                and e.source != through_id
                and e.destination != through_id
            ):
                intercepted.append(e)
            else:
                kept.append(e)
        new_edges = list(kept)

        # Idempotency: the surrounding Add transformation typically
        # wires one half of the bridge (src→through or through→dst)
        # with the through-operator's port name. The pre-migration
        # ``_make_reroute_outgoing`` / ``_make_reroute_incoming``
        # emitted only the *missing* half so that a single edge
        # carried each data flow at the through-operator's intended
        # port. Re-emit only the half that isn't already wired, and
        # treat Add's bridge as the source of truth for its own
        # position regardless of the rerouted edge's port name.
        def _has_path(es, src, dst) -> bool:
            return any(ex.source == src and ex.destination == dst for ex in es)

        for e in intercepted:
            if not _has_path(new_edges, e.source, through_id):
                new_edges.append(Edge(
                    source=e.source,
                    destination=through_id,
                    position=e.position,
                    output=e.output,
                ))
            if not _has_path(new_edges, through_id, e.destination):
                new_edges.append(Edge(
                    source=through_id,
                    destination=e.destination,
                    position=e.position,
                    output=0,
                ))
        return DAG(nodes=dag.nodes, edges=new_edges)
    return f


def _make_delete_edges_primitive(prim: dict):
    selector = prim.get("selector") or {}

    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        kept = [
            e for e in dag.edges
            if not _edge_matches_selector(e, selector, dag, mapping)
        ]
        return DAG(nodes=dag.nodes, edges=kept)
    return f


def _make_delete_node_primitive(prim: dict):
    selector = prim.get("selector") or {}

    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        ids = set(_resolve_node_selector(selector, dag, mapping))
        new_nodes = {nid: n for nid, n in dag.nodes.items() if nid not in ids}
        new_edges = [
            e for e in dag.edges
            if e.source not in ids and e.destination not in ids
        ]
        return DAG(nodes=new_nodes, edges=new_edges)
    return f


def _make_set_node_payload_primitive(prim: dict):
    selector = prim.get("selector") or {}
    payload_spec = prim.get("payload") or {}

    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        ids = _resolve_node_selector(selector, dag, mapping)
        new_node = _payload_spec_to_node(payload_spec)
        new_nodes = dict(dag.nodes)
        for nid in ids:
            new_nodes[nid] = new_node
        return DAG(nodes=new_nodes, edges=dag.edges)
    return f


def _make_add_edge_primitive(prim: dict):
    src_sel = prim.get("source") or {}
    dst_sel = prim.get("destination") or {}
    pos = prim.get("position", 0)
    out = prim.get("output", 0)

    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        srcs = _resolve_node_selector(src_sel, dag, mapping)
        dsts = _resolve_node_selector(dst_sel, dag, mapping)
        if len(srcs) != 1 or len(dsts) != 1:
            return dag
        new_edges = list(dag.edges)
        new_edges.append(Edge(
            source=srcs[0],
            destination=dsts[0],
            position=pos,
            output=out,
        ))
        return DAG(nodes=dag.nodes, edges=new_edges)
    return f


def _make_add_node_primitive(prim: dict):
    nid = prim.get("id")
    bind = prim.get("bind")
    payload_spec = prim.get("payload") or {}

    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        node = _payload_spec_to_node(payload_spec)
        new_nodes = dict(dag.nodes)
        new_nodes[nid] = node
        if bind:
            mapping[bind] = nid
        return DAG(nodes=new_nodes, edges=dag.edges)
    return f


def _make_lower_task_primitive(prim: dict):
    selector = prim.get("selector") or {}
    realisations = prim.get("realisations") or []

    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        if not realisations:
            return dag
        chosen = realisations[0]
        new_op = Operator(
            name=chosen.get("fqn", ""),
            language=chosen.get("language", "python"),
            tasks=[],
        )
        ids = _resolve_node_selector(selector, dag, mapping)
        new_nodes = dict(dag.nodes)
        for nid in ids:
            new_nodes[nid] = new_op
        return DAG(nodes=new_nodes, edges=dag.edges)
    return f


def _payload_spec_to_node(spec: dict):
    kind = (spec or {}).get("payload")
    if kind == "operator":
        return Operator(
            name=spec.get("name", ""),
            language=spec.get("language", "python"),
            tasks=[],
        )
    if kind == "parameter":
        return Parameter(
            name=spec.get("name", ""),
            dtype=spec.get("dtype", "string"),
            value=str(spec.get("value", "")),
        )
    if kind == "snippet":
        return Snippet(
            name=spec.get("name", ""),
            code=spec.get("code", ""),
            language=spec.get("language", "python"),
        )
    raise ValueError(f"unknown payload kind {kind!r}")


def compile_rewrite_rule(doc: dict, operator_fqn: str) -> RewriteRule:
    """Compile a docstore rewrite document into a ``RewriteRule``.

    The LHS pattern always targets the specific *operator_fqn* (the operator
    the user clicked "apply" on).  The KB ``applies_to`` relationship resolves
    WHICH operators qualify; the compiled rule targets ONE specific operator.

    The RHS is a list of ``Transformation`` objects deserialized from the
    document's ``transformations`` array.
    """
    # ── LHS pattern ──────────────────────────────────────────────────────
    pattern = DAG(
        nodes={"n": Node(type="Operator", text=_re.escape(operator_fqn), language="python")},
        edges=[],
    )

    # ── Rust-fast-path: whole rule runs against an in-process Pipeline
    # handle, zero per-primitive marshalling, zero per-match marshalling.
    # Eligible when every transformation reduces to the primitive-op
    # vocabulary (Add → add_node + add_edge, Delete → delete_node +
    # delete_edges, Apply with primitive ``op``). Legacy named ``Apply``
    # functions (``reroute_outgoing`` etc.) bail us out — those need
    # python closures, so the rule takes the python path.
    if _USE_RUST_REWRITES:
        rule_json = _build_rust_rule_json(doc, operator_fqn)
        if rule_json is not None:
            return RewriteRule(
                pattern=pattern,
                description=doc.get("description", doc.get("name", "")),
                transformations=[Apply(f=_make_pipeline_apply_fn(rule_json))],
            )

    # ── RHS transformations ──────────────────────────────────────────────
    # When the rust path is enabled, consecutive primitive-op entries
    # are batched into a single ``Apply(f=...)`` that calls
    # ``dorian_native.apply_primitives`` once with the whole list.
    # The boundary cost (JSON encode/decode of the DAG) is the
    # bottleneck on the cross-PyO3 path; paying it once per rule
    # instead of once per primitive collapses the slowdown.
    transformations = []
    pending_primitives: list[dict] = []

    def _flush_primitives():
        if not pending_primitives:
            return
        if _USE_RUST_REWRITES:
            transformations.append(
                Apply(f=_make_rust_primitive_batch_apply_fn(list(pending_primitives)))
            )
        else:
            for p in pending_primitives:
                transformations.append(Apply(f=_primitive_op_to_apply_fn_python(p)))
        pending_primitives.clear()

    for tf in doc.get("transformations", []):
        tf_type = tf.get("type")

        if "op" in tf and tf_type is None:
            # Primitive-op entry — defer until we know whether more
            # primitives follow so we can batch the run.
            pending_primitives.append(tf)
            continue

        # Non-primitive transformation: flush any pending primitive
        # batch first, then handle this one normally.
        _flush_primitives()

        if tf_type == "Add":
            # Named nodes: local_id → deserialized node object
            nodes = None
            if tf.get("nodes"):
                nodes = {
                    local_id: _deserialize_node(spec)
                    for local_id, spec in tf["nodes"].items()
                }

            # Rich edges with position/output
            edges = None
            if tf.get("edges"):
                edges = [
                    Edge(
                        source=e["source"],
                        destination=e["destination"],
                        position=e.get("position", 0),
                        output=e.get("output", 0),
                    )
                    for e in tf["edges"]
                ]

            transformations.append(Add(nodes=nodes, edges=edges))

        elif tf_type == "Delete":
            from dorian.code.parsing.rule import Delete
            transformations.append(Delete(
                nodes=tf.get("nodes", []),
                edges=[tuple(e) for e in tf.get("edges", [])],
            ))

        elif tf_type == "Apply":
            fn_name = tf.get("function")
            factory = _APPLY_REGISTRY.get(fn_name)
            if not factory:
                raise ValueError(f"Unknown Apply function {fn_name!r} in rewrite doc {doc.get('_id')!r}")
            # Pass the full tf dict as args (factory extracts what it needs)
            transformations.append(Apply(f=factory(tf)))

        else:
            raise ValueError(f"Unknown transformation type {tf_type!r} in rewrite doc {doc.get('_id')!r}")

    _flush_primitives()

    return RewriteRule(
        pattern=pattern,
        description=doc.get("description", doc.get("name", "")),
        transformations=transformations,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Mitigation rule factory
# ═══════════════════════════════════════════════════════════════════════════

async def build_mitigation_rewrite(
    mitigation_name: str,
    operator_fqn: str,
    suggestion: dict | None = None,
) -> "callable[[DAG], DAG] | None":
    """Build a DAG rewrite function for a mitigation.

    Fetches the serialised rule from ``doc_rewrites``,
    compiles it into a ``RewriteRule``, and returns a ``(dag) -> DAG``
    closure that applies the rule via ``sync_apply``.

    Returns ``None`` when no rule exists or the target package is not installed.
    """
    from backend.envs import expdb

    # 1. Fetch rule body from the docstore
    slug = mitigation_name.lower().replace(" ", "-")
    doc = await expdb.rewrites.find_one({"_id": slug})
    if not doc:
        doc = await expdb.rewrites.find_one({"name": mitigation_name})
    if not doc:
        await aemit(Event("RewriteSkipped", {
            "source": "mitigation_rewrites.build_mitigation_rewrite",
            "reason": f"no rewrite rule in the docstore for {mitigation_name!r}",
        }))
        return None

    # 2. Direct Alternative — patch the dynamic target from suggestion.alternatives
    if mitigation_name == "Direct Alternative" and suggestion:
        alts_raw = suggestion.get("alternatives", "[]")
        alts = json.loads(alts_raw) if isinstance(alts_raw, str) else (alts_raw or [])
        if alts:
            _patch_dynamic_target(doc, alts[0])
        else:
            await aemit(Event("RewriteSkipped", {
                "source": "mitigation_rewrites.build_mitigation_rewrite",
                "reason": "Direct Alternative has no alternatives",
            }))
            return None

    # 3. Compile to RewriteRule
    rule = compile_rewrite_rule(doc, operator_fqn)

    # 4. Return closure
    return lambda dag: sync_apply(rule, dag, {})


def _patch_dynamic_target(doc: dict, alternative_fqn: str) -> None:
    """Patch ``__DYNAMIC__`` placeholders in a Direct Alternative rule."""
    for tf in doc.get("transformations", []):
        if tf.get("type") == "Apply" and tf.get("function") == "replace_node":
            new_node = tf.get("new_node", {})
            if new_node.get("name") == "__DYNAMIC__":
                new_node["name"] = alternative_fqn
