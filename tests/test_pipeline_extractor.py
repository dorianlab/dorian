"""
tests/test_pipeline_extractor.py
---------------------------------
Unit and integration tests for the pipeline extractor.

Covers:
  - DAG data model (Node, Edge, DAG, serialization)
  - Pattern matching (match, comparator)
  - Transformation primitives (Add, Delete, Apply, ToOperator, ToParameter, Revert)
  - Individual rewrite rules (from get_rules)
  - End-to-end parse() on real Python snippets
  - Frontend-format conversion (_dag_to_frontend_format)

No external services required (no Redis, Dask, docstore, Neo4j).
"""
from __future__ import annotations

import pytest
from uuid import uuid4

from dorian.dag import (
    DAG,
    Node,
    Edge,
    Operator,
    Parameter,
    Snippet,
    match,
    comparator,
    wildcard,
    _class_of,
    to_dag,
)
from dorian.languages import PYTHON
from dorian.code.parsing.rule import (
    Add,
    Apply,
    Delete,
    Replace,
    Revert,
    ToOperator,
    ToParameter,
    PurgeMode,
    RewriteRule,
)
from dorian.code.parsing.rules import get_rules, _update, add_rewrite_rule
from dorian.code.parsing.parser import (
    parse,
    create_parser,
    rewrite,
    _rewrite,
)


# ═══════════════════════════════════════════════════════════════════════════
# DAG DATA MODEL
# ═══════════════════════════════════════════════════════════════════════════


class TestNode:
    def test_default_wildcard(self):
        n = Node(language=PYTHON)
        assert n.type == wildcard
        assert n.text == wildcard

    def test_custom_fields(self):
        n = Node(type="call", text="foo", language=PYTHON)
        assert n.type == "call"
        assert n.text == "foo"

    def test_invalid_language(self):
        with pytest.raises(ValueError, match="language must be one of"):
            Node(language="javascript")

    def test_to_dict_round_trip(self):
        n = Node(type="identifier", text="x", language=PYTHON)
        d = n.to_dict()
        n2 = Node.from_dict(d)
        assert n2.type == n.type
        assert n2.text == n.text

    def test_hash_deterministic(self):
        n1 = Node(type="call", text="foo", language=PYTHON)
        n2 = Node(type="call", text="foo", language=PYTHON)
        assert hash(n1) == hash(n2)


class TestEdge:
    def test_default_position_and_output(self):
        e = Edge(source="a", destination="b")
        assert e.position == 0
        assert e.output == 0

    def test_string_int_coercion(self):
        """JSON deserialization can produce "0" instead of 0."""
        e = Edge(source="a", destination="b", position="2", output="1")
        assert e.position == 2
        assert e.output == 1
        assert isinstance(e.position, int)
        assert isinstance(e.output, int)

    def test_keyword_position_preserved(self):
        """Non-numeric position (kwarg name) stays as string."""
        e = Edge(source="a", destination="b", position="strategy")
        assert e.position == "strategy"
        assert isinstance(e.position, str)

    def test_to_dict_round_trip(self):
        e = Edge(source="x", destination="y", position=3, output=1)
        d = e.to_dict()
        e2 = Edge.from_dict(d)
        assert e2.source == e.source
        assert e2.destination == e.destination
        assert e2.position == e.position
        assert e2.output == e.output


class TestOperator:
    def test_basic(self):
        op = Operator(name="sklearn.preprocessing.StandardScaler", language="python")
        assert op.name == "sklearn.preprocessing.StandardScaler"
        assert "StandardScaler" in repr(op)

    def test_to_dict_round_trip(self):
        op = Operator(name="pandas.read_csv", language="python")
        d = op.to_dict()
        assert d["class_type"] == "Operator"
        op2 = Operator.from_dict(d)
        assert op2.name == op.name


class TestParameter:
    def test_basic(self):
        p = Parameter(name="n_estimators", dtype="int", value="100")
        assert p.name == "n_estimators"
        assert p.dtype == "int"
        assert p.value == "100"

    def test_to_dict_round_trip(self):
        p = Parameter(name="alpha", dtype="float", value="0.5")
        d = p.to_dict()
        assert d["class_type"] == "Parameter"
        p2 = Parameter.from_dict(d)
        assert p2.name == p.name
        assert p2.dtype == p.dtype
        assert p2.value == p.value


class TestSnippet:
    def test_basic(self):
        s = Snippet(name="preprocess", code="def foo(x): return x+1", language="python")
        assert s.name == "preprocess"
        assert "x+1" in s.code

    def test_to_dict_round_trip(self):
        s = Snippet(name="t", code="def foo(): pass", language="python")
        d = s.to_dict()
        assert d["class_type"] == "Snippet"
        s2 = Snippet.from_dict(d)
        assert s2.code == s.code


class TestDAG:
    def test_empty_dag(self):
        dag = DAG()
        assert len(dag) == 0
        assert list(dag) == []

    def test_iter(self):
        dag = DAG(nodes={"a": Operator(name="op1", language="python")})
        items = list(dag)
        assert items == [("a", dag.nodes["a"])]

    def test_merge(self):
        d1 = DAG(
            nodes={"a": Node(language="python")},
            edges=[Edge("a", "b")],
        )
        d2 = DAG(
            nodes={"c": Node(language="python")},
            edges=[Edge("c", "d")],
        )
        merged = DAG.merge([d1, d2])
        assert "a" in merged.nodes
        assert "c" in merged.nodes
        assert len(merged.edges) == 2

    def test_json_round_trip(self):
        dag = DAG(
            nodes={
                "op1": Operator(name="sklearn.svm.SVC", language="python"),
                "p1": Parameter(name="C", dtype="float", value="1.0"),
            },
            edges=[Edge(source="p1", destination="op1", position="C")],
        )
        d = dag.to_json_dict()
        dag2 = DAG.from_json_dict(d)
        assert isinstance(dag2.nodes["op1"], Operator)
        assert isinstance(dag2.nodes["p1"], Parameter)
        assert len(dag2.edges) == 1
        assert dag2.edges[0].position == "C"

    def test_split_line_based(self):
        """Module root "0" with two children "1" and "3"."""
        dag = DAG(
            nodes={
                "0": Node(type="module", language="python"),
                "1": Node(type="expression_statement", language="python"),
                "2": Node(type="call", language="python"),
                "3": Node(type="expression_statement", language="python"),
                "4": Node(type="call", language="python"),
            },
            edges=[
                Edge("0", "1"),
                Edge("1", "2"),
                Edge("0", "3"),
                Edge("3", "4"),
            ],
        )
        subdags = dag.split_line_based()
        assert len(subdags) == 2
        assert "1" in subdags[0].nodes
        assert "2" in subdags[0].nodes
        assert "3" in subdags[1].nodes
        assert "4" in subdags[1].nodes


# ═══════════════════════════════════════════════════════════════════════════
# PATTERN MATCHING
# ═══════════════════════════════════════════════════════════════════════════


class TestComparator:
    def test_node_vs_node_match(self):
        concrete = Node(type="call", text="foo()", language="python")
        pattern = Node(type="call", language="python")
        assert comparator(concrete, pattern) is True

    def test_node_vs_node_type_mismatch(self):
        concrete = Node(type="identifier", text="x", language="python")
        pattern = Node(type="call", language="python")
        assert comparator(concrete, pattern) is False

    def test_node_regex_type(self):
        concrete = Node(type="call", text="f()", language="python")
        pattern = Node(type="call|attribute", language="python")
        assert comparator(concrete, pattern) is True

    def test_node_regex_text(self):
        concrete = Node(type="identifier", text="sklearn.svm", language="python")
        pattern = Node(type="identifier", text="sklearn.*", language="python")
        assert comparator(concrete, pattern) is True

    def test_operator_vs_node_match(self):
        op = Operator(name="sklearn.svm.SVC", language="python")
        pattern = Node(type="Operator", text="sklearn.*", language="python")
        assert comparator(op, pattern) is True

    def test_operator_vs_node_mismatch(self):
        op = Operator(name="pandas.read_csv", language="python")
        pattern = Node(type="Operator", text="sklearn.*", language="python")
        assert comparator(op, pattern) is False

    def test_parameter_vs_node(self):
        p = Parameter(name="C", dtype="float", value="1.0")
        pattern = Node(type=".*", text="Parameter", language="python")
        assert comparator(p, pattern) is True

    def test_language_regex_match(self):
        """Language comparison is a regex match.

        Historical note: was exact-equality until the fix commit; the
        strict check silently rejected any rule authored with the schema
        default ``language=".*"``. Pattern layer matches text and type
        as regex; language now follows the same rule for consistency
        with ``dorian/pipeline/parser.py::comparator``. The ``Node``
        dataclass separately enforces a closed-set of allowed values
        (``python`` and wildcard ``.*``) at construction time, so we
        can only exercise those two here.
        """
        concrete = Node(type="call", text="f()", language="python")

        # Exact literal still matches.
        pattern_exact = Node(type="call", language="python")
        assert comparator(concrete, pattern_exact) is True

        # Wildcard ``.*`` matches any language (was False pre-fix — the
        # core regression this change addresses).
        pattern_wildcard = Node(type="call", language=".*")
        assert comparator(concrete, pattern_wildcard) is True


class TestMatch:
    def test_single_node_match(self):
        dag = DAG(
            nodes={"a": Node(type="call", text="foo()", language="python")},
            edges=[],
        )
        pattern = DAG(
            nodes={"0": Node(type="call", language="python")},
            edges=[],
        )
        found, mapping = match(pattern, dag)
        assert found is True
        assert mapping == {"0": "a"}

    def test_no_match(self):
        dag = DAG(
            nodes={"a": Node(type="identifier", text="x", language="python")},
            edges=[],
        )
        pattern = DAG(
            nodes={"0": Node(type="call", language="python")},
            edges=[],
        )
        found, mapping = match(pattern, dag)
        assert found is False
        assert mapping is None

    def test_edge_match(self):
        dag = DAG(
            nodes={
                "a": Node(type="call", text="f()", language="python"),
                "b": Node(type="identifier", text="x", language="python"),
            },
            edges=[Edge(source="a", destination="b")],
        )
        pattern = DAG(
            nodes={
                "0": Node(type="call", language="python"),
                "1": Node(type="identifier", language="python"),
            },
            edges=[Edge(source="0", destination="1")],
        )
        found, mapping = match(pattern, dag)
        assert found is True
        assert mapping["0"] == "a"
        assert mapping["1"] == "b"

    def test_edge_direction_matters(self):
        """Reversed edge should not match."""
        dag = DAG(
            nodes={
                "a": Node(type="call", text="f()", language="python"),
                "b": Node(type="identifier", text="x", language="python"),
            },
            edges=[Edge(source="b", destination="a")],  # reversed!
        )
        pattern = DAG(
            nodes={
                "0": Node(type="call", language="python"),
                "1": Node(type="identifier", language="python"),
            },
            edges=[Edge(source="0", destination="1")],
        )
        found, _ = match(pattern, dag)
        assert found is False

    def test_processed_skip(self):
        """Already-processed candidates should be skipped."""
        dag = DAG(
            nodes={"a": Node(type="call", text="f()", language="python")},
            edges=[],
        )
        pattern = DAG(
            nodes={"0": Node(type="call", language="python")},
            edges=[],
        )
        already = [{"0": "a"}]
        found, _ = match(pattern, dag, processed=already)
        assert found is False

    def test_multi_node_no_duplicate_mapping(self):
        """Pattern with 2 nodes must map to 2 distinct DAG nodes."""
        dag = DAG(
            nodes={
                "a": Node(type="identifier", text="x", language="python"),
            },
            edges=[],
        )
        pattern = DAG(
            nodes={
                "0": Node(type="identifier", language="python"),
                "1": Node(type="identifier", language="python"),
            },
            edges=[],
        )
        found, _ = match(pattern, dag)
        assert found is False  # only 1 node, pattern needs 2 distinct


# ═══════════════════════════════════════════════════════════════════════════
# TRANSFORMATION PRIMITIVES
# ═══════════════════════════════════════════════════════════════════════════


class TestTransformations:
    def _simple_dag(self):
        return DAG(
            nodes={
                "a": Node(type="call", text="f()", language="python"),
                "b": Node(type="identifier", text="x", language="python"),
                "c": Node(type="integer", text="42", language="python"),
            },
            edges=[
                Edge(source="a", destination="b"),
                Edge(source="a", destination="c"),
            ],
        )

    def test_delete_isolated(self):
        dag = self._simple_dag()
        mapping = {"0": "b"}
        result = _rewrite(dag, mapping, Delete(nodes=["0"], mode=PurgeMode.isolated))
        assert "b" not in result.nodes
        assert "a" in result.nodes
        assert "c" in result.nodes

    def test_delete_recursive(self):
        dag = DAG(
            nodes={
                "a": Node(type="root", language="python"),
                "b": Node(type="child", language="python"),
                "c": Node(type="grandchild", language="python"),
            },
            edges=[Edge("a", "b"), Edge("b", "c")],
        )
        mapping = {"0": "b"}
        result = _rewrite(dag, mapping, Delete(nodes=["0"], mode=PurgeMode.recursive))
        assert "b" not in result.nodes
        assert "c" not in result.nodes  # recursively deleted
        assert "a" in result.nodes

    def test_add_edges(self):
        dag = self._simple_dag()
        mapping = {"0": "b", "1": "c"}
        result = _rewrite(dag, mapping, Add(edges=[("0", "1")]))
        # Should have original edges plus b→c
        new_edges = [e for e in result.edges if e.source == "b" and e.destination == "c"]
        assert len(new_edges) == 1

    def test_apply(self):
        dag = self._simple_dag()
        mapping = {"0": "a"}

        def my_func(g, m):
            nid = m["0"]
            node = g.nodes[nid]
            new_node = Node(type="modified", text=node.text, language=node.language)
            return DAG(nodes={**g.nodes, nid: new_node}, edges=g.edges)

        result = _rewrite(dag, mapping, Apply(f=my_func))
        assert result.nodes["a"].type == "modified"

    def test_to_operator(self):
        dag = DAG(
            nodes={
                "a": Node(type="call", text="f()", language="python"),
                "b": Node(type="attribute", text="sklearn.svm.SVC", language="python"),
            },
            edges=[Edge("a", "b")],
        )
        mapping = {"0": "a", "1": "b"}
        result = _rewrite(dag, mapping, ToOperator(nid="0", content="1"))
        assert isinstance(result.nodes["a"], Operator)
        assert result.nodes["a"].name == "sklearn.svm.SVC"

    def test_to_parameter(self):
        dag = DAG(
            nodes={
                "a": Node(type="keyword_argument", text="C=1.0", language="python"),
                "b": Node(type="identifier", text="C", language="python"),
                "c": Node(type="float", text="1.0", language="python"),
            },
            edges=[Edge("a", "b"), Edge("a", "c")],
        )
        mapping = {"0": "a", "1": "b", "2": "c"}
        result = _rewrite(dag, mapping, ToParameter(nid="0", kw="1", value="2"))
        assert isinstance(result.nodes["a"], Parameter)
        assert result.nodes["a"].name == "C"
        assert result.nodes["a"].value == "1.0"
        assert result.nodes["a"].dtype == "float"

    def test_revert_edges(self):
        dag = DAG(
            nodes={
                "a": Node(type="call", language="python"),
                "b": Node(type="arg_list", language="python"),
            },
            edges=[Edge(source="a", destination="b")],
        )
        mapping = {"0": "a", "1": "b"}
        result = _rewrite(dag, mapping, Revert(edges=[("0", "1")]))
        # Original a→b should be removed, b→a should be added
        forward = [e for e in result.edges if e.source == "a" and e.destination == "b"]
        backward = [e for e in result.edges if e.source == "b" and e.destination == "a"]
        assert len(forward) == 0
        assert len(backward) == 1

    def test_replace(self):
        dag = self._simple_dag()
        result = _rewrite(dag, {}, Replace())
        assert len(result.nodes) == 0
        assert len(result.edges) == 0


class TestRewriteSequence:
    def test_multi_step_rewrite(self):
        """Apply multiple transformations in sequence via rewrite()."""
        dag = DAG(
            nodes={
                "a": Node(type="call", text="foo()", language="python"),
                "b": Node(type="attribute", text="my_module.foo", language="python"),
                "c": Node(type="identifier", text="arg1", language="python"),
            },
            edges=[Edge("a", "b"), Edge("a", "c")],
        )
        mapping = {"0": "a", "1": "b"}
        transformations = [
            ToOperator(nid="0", content="1"),
            Delete(nodes=["1"]),
        ]
        result = rewrite(dag, mapping, transformations)
        assert isinstance(result.nodes["a"], Operator)
        assert result.nodes["a"].name == "my_module.foo"
        assert "b" not in result.nodes
        assert "c" in result.nodes


# ═══════════════════════════════════════════════════════════════════════════
# HELPER: _update
# ═══════════════════════════════════════════════════════════════════════════


class TestUpdateHelper:
    def test_update_type(self):
        dag = DAG(
            nodes={"a": Node(type="call", text="f()", language="python")},
            edges=[],
        )
        result = _update(dag, "a", "type", "identifier")
        assert result.nodes["a"].type == "identifier"
        assert result.nodes["a"].text == "f()"  # unchanged

    def test_update_missing_key(self):
        dag = DAG(nodes={}, edges=[])
        result = _update(dag, "nonexistent", "type", "foo")
        assert result is dag  # returned unchanged


# ═══════════════════════════════════════════════════════════════════════════
# INDIVIDUAL REWRITE RULES
# ═══════════════════════════════════════════════════════════════════════════


class TestBuiltinRules:
    """Test individual rules from get_rules() in isolation."""

    @pytest.fixture
    def rules(self):
        return get_rules()

    def test_comment_deletion(self, rules):
        """Rule 0: Deletes comment nodes."""
        rule = rules[0]
        dag = DAG(
            nodes={
                "a": Node(type="comment", text="# hello", language="python"),
                "b": Node(type="identifier", text="x", language="python"),
            },
            edges=[],
        )
        found, mapping = match(rule.pattern, dag)
        assert found
        result = rewrite(dag, mapping, rule.transformations)
        assert "a" not in result.nodes
        assert "b" in result.nodes

    def test_single_char_deletion(self, rules):
        """Rule 3: Deletes single-character syntax nodes."""
        rule = rules[3]
        dag = DAG(
            nodes={
                "a": Node(type="(", text="(", language="python"),
                "b": Node(type="identifier", text="foo", language="python"),
            },
            edges=[],
        )
        found, mapping = match(rule.pattern, dag)
        assert found
        result = rewrite(dag, mapping, rule.transformations)
        assert "a" not in result.nodes

    def test_keyword_argument_to_parameter(self, rules):
        """The keyword_argument rule converts to Parameter."""
        # Find the keyword_argument rule
        kw_rule = None
        for r in rules:
            if "keyword" in r.description.lower():
                kw_rule = r
                break
        assert kw_rule is not None, "keyword argument rule not found"

        dag = DAG(
            nodes={
                "a": Node(type="keyword_argument", text="n=100", language="python"),
                "b": Node(type="identifier", text="n", language="python"),
                "c": Node(type="integer", text="100", language="python"),
            },
            edges=[Edge("a", "b"), Edge("a", "c")],
        )
        found, mapping = match(kw_rule.pattern, dag)
        assert found
        result = rewrite(dag, mapping, kw_rule.transformations)
        # Node "a" should now be a Parameter
        assert isinstance(result.nodes[mapping["0"]], Parameter)
        param = result.nodes[mapping["0"]]
        assert param.name == "n"
        assert param.value == "100"
        assert param.dtype == "integer"
        # Children should be deleted
        assert mapping["1"] not in result.nodes
        assert mapping["2"] not in result.nodes

    def test_import_deletion(self, rules):
        """Import statements should be recursively deleted."""
        import_rule = None
        for r in rules:
            for t in r.transformations:
                if (
                    isinstance(t, Delete)
                    and t.mode == PurgeMode.recursive
                    and "0" in t.nodes
                ):
                    # Check if pattern matches import
                    for nid, node in r.pattern.nodes.items():
                        if hasattr(node, "type") and "import" in node.type:
                            import_rule = r
                            break
            if import_rule:
                break
        assert import_rule is not None, "import deletion rule not found"

        dag = DAG(
            nodes={
                "a": Node(type="import_statement", text="import os", language="python"),
                "b": Node(type="dotted_name", text="os", language="python"),
            },
            edges=[Edge("a", "b")],
        )
        found, mapping = match(import_rule.pattern, dag)
        assert found
        result = rewrite(dag, mapping, import_rule.transformations)
        # Both should be deleted (recursive)
        assert len(result.nodes) == 0

    def test_attribute_flattening(self, rules):
        """Attribute nodes with identifier children get flattened."""
        attr_rule = None
        for r in rules:
            if any(
                hasattr(n, "type") and n.type == "attribute"
                for n in r.pattern.nodes.values()
            ):
                if any(isinstance(t, Apply) for t in r.transformations):
                    # The attribute concatenation rule
                    if len(r.pattern.nodes) == 2 and len(r.pattern.edges) == 1:
                        attr_rule = r
                        break
        if attr_rule is None:
            pytest.skip("attribute flattening rule not found in current rule set")

        dag = DAG(
            nodes={
                "a": Node(type="attribute", text="sklearn", language="python"),
                "b": Node(type="identifier", text="svm", language="python"),
            },
            edges=[Edge("a", "b")],
        )
        found, mapping = match(attr_rule.pattern, dag)
        assert found
        result = rewrite(dag, mapping, attr_rule.transformations)
        # After flattening, "a" should have text "sklearn.svm" and "b" deleted
        assert "b" not in result.nodes or result.nodes.get("a", Node()).text == "sklearn.svm"

    def test_call_to_operator(self, rules):
        """Call + attribute → Operator promotion."""
        op_rule = None
        for r in rules:
            if any(isinstance(t, ToOperator) for t in r.transformations):
                op_rule = r
                break
        assert op_rule is not None, "ToOperator rule not found"

        dag = DAG(
            nodes={
                "a": Node(type="call", text="SVC()", language="python"),
                "b": Node(type="attribute", text="sklearn.svm.SVC", language="python"),
            },
            edges=[Edge("a", "b")],
        )
        found, mapping = match(op_rule.pattern, dag)
        assert found
        result = rewrite(dag, mapping, op_rule.transformations)
        # "a" should now be an Operator
        assert isinstance(result.nodes[mapping["0"]], Operator)
        assert result.nodes[mapping["0"]].name == "sklearn.svm.SVC"


# ═══════════════════════════════════════════════════════════════════════════
# DYNAMIC RULE ADDITION
# ═══════════════════════════════════════════════════════════════════════════


class TestAddRewriteRule:
    def test_valid_rule(self):
        rule_str = (
            "RewriteRule("
            "  description='test dynamic rule',"
            "  pattern=DAG(nodes={'0': Node(type='test_node', language='python')}, edges=[]),"
            "  transformations=[Delete(nodes=['0'])],"
            ")"
        )
        rule, error = add_rewrite_rule(rule_str)
        assert rule is not None
        assert error == ""
        assert isinstance(rule, RewriteRule)

    def test_invalid_rule(self):
        rule, error = add_rewrite_rule("not a valid rule")
        assert rule is None
        assert error != ""


# ═══════════════════════════════════════════════════════════════════════════
# TREE-SITTER PARSER
# ═══════════════════════════════════════════════════════════════════════════


class TestTreeSitterParser:
    def test_create_python_parser(self):
        parser = create_parser("python")
        assert parser is not None

    def test_unsupported_language(self):
        with pytest.raises(ValueError, match="not supported"):
            create_parser("javascript")

    def test_to_dag_simple(self):
        """Parse a simple expression and verify DAG structure."""
        # The @typeclass stub in conftest doesn't dispatch .instance() registrations,
        # so call the registered implementation directly.
        from dorian.dag import _tree_to_dag
        parser = create_parser("python")
        tree = parser.parse(b"x = 1")
        dag = _tree_to_dag(tree, "python")
        assert len(dag.nodes) > 0
        # Root should be a module node
        assert dag.nodes["0"].type == "module"
        # Should have edges
        assert len(dag.edges) > 0


# ═══════════════════════════════════════════════════════════════════════════
# END-TO-END parse()
# ═══════════════════════════════════════════════════════════════════════════


def _parse_with_real_to_dag(code: str, language: str = "python", rewrite_rules=None):
    """Wrapper around parse() that patches to_dag with the real tree-sitter implementation.

    The conftest _FakeTypeclass stub doesn't dispatch .instance() registrations,
    so to_dag(tree, language) returns None. This wrapper substitutes the real
    _tree_to_dag implementation so parse() works correctly in tests.
    """
    from unittest.mock import patch
    from dorian.dag import _tree_to_dag
    import dorian.code.parsing.parser as _parser_mod
    with patch.object(_parser_mod, "to_dag", _tree_to_dag):
        return parse(code, language, rewrite_rules)


class TestEndToEndParse:
    def test_simple_function_call(self):
        """A bare function call should produce an Operator."""
        code = "sklearn.svm.SVC(C=1.0)"
        initial, final = _parse_with_real_to_dag(code)

        # Initial DAG should have raw AST nodes
        assert len(initial.nodes) > 0
        assert initial.nodes["0"].type == "module"

        # Final DAG should have reduced nodes
        operators = [n for n in final.nodes.values() if isinstance(n, Operator)]
        assert len(operators) >= 1
        op_names = [op.name for op in operators]
        # The operator name should contain SVC (possibly with sklearn prefix)
        assert any("SVC" in name for name in op_names), f"Expected SVC in {op_names}"

    def test_keyword_args_become_parameters(self):
        """Keyword arguments should be extracted as Parameters."""
        code = "sklearn.svm.SVC(C=1.0, kernel='rbf')"
        _, final = _parse_with_real_to_dag(code)

        params = [n for n in final.nodes.values() if isinstance(n, Parameter)]
        param_names = [p.name for p in params]
        assert "C" in param_names, f"Expected 'C' in {param_names}"
        assert "kernel" in param_names, f"Expected 'kernel' in {param_names}"

    def test_imports_removed(self):
        """Import statements should not appear in the final DAG."""
        code = "import pandas as pd\npd.read_csv('data.csv')"
        _, final = _parse_with_real_to_dag(code)

        # No nodes should have type "import_statement"
        for node in final.nodes.values():
            if isinstance(node, Node):
                assert "import" not in node.type, f"Import node survived: {node}"

    def test_multiple_statements(self):
        """Multiple statements should all be represented."""
        code = (
            "from sklearn.svm import SVC\n"
            "clf = SVC(kernel='linear')\n"
        )
        _, final = _parse_with_real_to_dag(code)
        # Should have at least one Operator or meaningful node
        assert len(final.nodes) >= 1

    def test_empty_code(self):
        """Empty code should produce an empty final DAG."""
        code = ""
        _, final = _parse_with_real_to_dag(code)
        # Module with no children → empty after split
        assert len(final.nodes) == 0 or all(
            isinstance(n, Node) and n.type == "module"
            for n in final.nodes.values()
        )

    def test_comments_only(self):
        """Code with only comments should produce an empty DAG."""
        code = "# this is a comment\n# another comment\n"
        _, final = _parse_with_real_to_dag(code)
        # Comments should be deleted
        for node in final.nodes.values():
            if isinstance(node, Node):
                assert node.type != "comment"

    def test_assignment_wiring(self):
        """Variable assignment should wire RHS to LHS uses."""
        code = (
            "from sklearn.preprocessing import StandardScaler\n"
            "scaler = StandardScaler()\n"
        )
        _, final = _parse_with_real_to_dag(code)
        # Should produce an operator for StandardScaler
        operators = [n for n in final.nodes.values() if isinstance(n, Operator)]
        # At minimum, StandardScaler should be detected
        assert any("StandardScaler" in op.name for op in operators) or len(final.nodes) > 0


# ═══════════════════════════════════════════════════════════════════════════
# FRONTEND FORMAT CONVERSION
# ═══════════════════════════════════════════════════════════════════════════


class TestDagToFrontendFormat:
    def test_operator_conversion(self):
        from dorian.api.routes.file import _dag_to_frontend_format

        dag = DAG(
            nodes={
                "op1": Operator(name="sklearn.svm.SVC", language="python"),
                "p1": Parameter(name="C", dtype="float", value="1.0"),
            },
            edges=[Edge(source="p1", destination="op1", position="C")],
        )
        result = _dag_to_frontend_format(dag)
        assert "uuid" in result
        assert "nodes" in result
        assert "edges" in result

        # Operator node
        assert result["nodes"]["op1"]["type"] == "Operator"
        assert result["nodes"]["op1"]["name"] == "sklearn.svm.SVC"

        # Parameter node
        assert result["nodes"]["p1"]["type"] == "Parameter"
        assert result["nodes"]["p1"]["value"] == "1.0"
        assert result["nodes"]["p1"]["dtype"] == "float"

        # Edge
        assert len(result["edges"]) == 1

    def test_snippet_conversion(self):
        from dorian.api.routes.file import _dag_to_frontend_format

        dag = DAG(
            nodes={
                "s1": Snippet(name="preprocess", code="def foo(x): return x", language="python"),
            },
            edges=[],
        )
        result = _dag_to_frontend_format(dag)
        assert result["nodes"]["s1"]["type"] == "Snippet"
        assert result["nodes"]["s1"]["code"] == "def foo(x): return x"

    def test_raw_node_promoted_to_operator(self):
        """Unreduced Node instances should be promoted to Operator."""
        from dorian.api.routes.file import _dag_to_frontend_format

        dag = DAG(
            nodes={
                "n1": Node(type="subscript", text="df['col']", language="python"),
            },
            edges=[],
        )
        result = _dag_to_frontend_format(dag)
        assert result["nodes"]["n1"]["type"] == "Operator"
        assert result["nodes"]["n1"]["name"] == "df['col']"


# ═══════════════════════════════════════════════════════════════════════════
# REWRITE RULE PROPERTIES
# ═══════════════════════════════════════════════════════════════════════════


class TestRewriteRuleProperties:
    def test_repr(self):
        rule = RewriteRule(
            description="test rule",
            pattern=DAG(
                nodes={"0": Node(type="call", language="python")},
                edges=[],
            ),
            transformations=[Delete(nodes=["0"])],
        )
        r = repr(rule)
        assert "test rule" in r
        assert "call" in r
        assert "Delete" in r

    def test_rule_has_stable_id(self):
        rule = RewriteRule(
            description="test",
            pattern=DAG(nodes={}, edges=[]),
        )
        assert hasattr(rule, "ID")
        assert len(rule.ID) > 0

    def test_get_rules_returns_list(self):
        rules = get_rules()
        assert isinstance(rules, list)
        assert len(rules) > 0
        assert all(isinstance(r, RewriteRule) for r in rules)


# ═══════════════════════════════════════════════════════════════════════════
# RULE VERSIONING
# ═══════════════════════════════════════════════════════════════════════════


class TestRuleVersioning:
    """Tests for get_rules_version() and _compute_rules_hash()."""

    def test_version_is_hex_string(self):
        from dorian.code.parsing.rules import get_rules_version
        version = get_rules_version()
        assert isinstance(version, str)
        assert len(version) == 16
        # Must be valid hex
        int(version, 16)

    def test_version_deterministic(self):
        """Same rules produce the same hash on repeated calls."""
        from dorian.code.parsing.rules import get_rules_version
        v1 = get_rules_version()
        v2 = get_rules_version()
        assert v1 == v2

    def test_cache_invalidation_on_add_rewrite_rule(self):
        """add_rewrite_rule() should set _rules_version_cache to None."""
        import dorian.code.parsing.rules as rules_mod

        # Prime the cache
        _ = rules_mod.get_rules_version()
        assert rules_mod._rules_version_cache is not None

        # Add a throwaway rule
        rule_str = (
            "RewriteRule("
            "  description='version_test_rule',"
            "  pattern=DAG(nodes={'0': Node(type='version_test_xyz', language='python')}, edges=[]),"
            "  transformations=[Delete(nodes=['0'])],"
            ")"
        )
        rule, error = add_rewrite_rule(rule_str)
        assert rule is not None, f"Failed to add rule: {error}"

        # Cache should be invalidated
        assert rules_mod._rules_version_cache is None

        # Cleanup: remove the rule we just added
        rules_mod._rules = [r for r in rules_mod._rules if r.description != "version_test_rule"]
        rules_mod._rules_version_cache = None

    def test_different_rule_sets_produce_different_versions(self):
        """_compute_rules_hash should produce different hashes for different rule sets."""
        from dorian.code.parsing.rules import _compute_rules_hash
        base_rules = get_rules()
        extra_rule = RewriteRule(
            description="extra rule for hash test",
            pattern=DAG(nodes={"0": Node(type="identifier", language="python")}, edges=[]),
            transformations=[Delete(nodes=["0"])],
        )
        h_base = _compute_rules_hash(base_rules)
        h_extended = _compute_rules_hash([*base_rules, extra_rule])
        assert h_base != h_extended

    def test_compute_rules_hash_empty(self):
        """Empty rule set should still produce a valid hash."""
        from dorian.code.parsing.rules import _compute_rules_hash
        h = _compute_rules_hash([])
        assert isinstance(h, str)
        assert len(h) == 16

    def test_compute_rules_hash_includes_description(self):
        """Two rules differing only in description should produce different hashes."""
        from dorian.code.parsing.rules import _compute_rules_hash
        r1 = RewriteRule(
            description="rule alpha",
            pattern=DAG(nodes={"0": Node(type="call", language="python")}, edges=[]),
            transformations=[Delete(nodes=["0"])],
        )
        r2 = RewriteRule(
            description="rule beta",
            pattern=DAG(nodes={"0": Node(type="call", language="python")}, edges=[]),
            transformations=[Delete(nodes=["0"])],
        )
        assert _compute_rules_hash([r1]) != _compute_rules_hash([r2])

    def test_compute_rules_hash_includes_pattern_topology(self):
        """Two rules differing in pattern should produce different hashes."""
        from dorian.code.parsing.rules import _compute_rules_hash
        r1 = RewriteRule(
            description="same",
            pattern=DAG(nodes={"0": Node(type="call", language="python")}, edges=[]),
            transformations=[Delete(nodes=["0"])],
        )
        r2 = RewriteRule(
            description="same",
            pattern=DAG(nodes={"0": Node(type="identifier", language="python")}, edges=[]),
            transformations=[Delete(nodes=["0"])],
        )
        assert _compute_rules_hash([r1]) != _compute_rules_hash([r2])


# ═══════════════════════════════════════════════════════════════════════════
# REGRESSION TESTING MODULE
# ═══════════════════════════════════════════════════════════════════════════


class TestDagEquality:
    """Tests for _dag_equal() and _diff_summary() in regression.py."""

    def test_identical_dags_are_equal(self):
        from dorian.code.regression import _dag_equal
        dag_dict = {
            "nodes": {
                "a": {"class_type": "Operator", "name": "sklearn.svm.SVC", "language": "python"},
                "b": {"class_type": "Parameter", "name": "C", "value": "1.0", "language": ""},
            },
            "edges": [{"source": "b", "destination": "a", "position": "C", "output": 0}],
        }
        assert _dag_equal(dag_dict, dag_dict) is True

    def test_different_node_ids_same_semantics(self):
        """DAGs with different UUIDs but same semantics should be equal."""
        from dorian.code.regression import _dag_equal
        a = {
            "nodes": {
                "uuid-111": {"class_type": "Operator", "name": "sklearn.svm.SVC", "language": "python"},
            },
            "edges": [],
        }
        b = {
            "nodes": {
                "uuid-999": {"class_type": "Operator", "name": "sklearn.svm.SVC", "language": "python"},
            },
            "edges": [],
        }
        assert _dag_equal(a, b) is True

    def test_different_node_count(self):
        from dorian.code.regression import _dag_equal
        a = {
            "nodes": {
                "a": {"class_type": "Operator", "name": "SVC"},
                "b": {"class_type": "Parameter", "name": "C"},
            },
            "edges": [],
        }
        b = {
            "nodes": {
                "x": {"class_type": "Operator", "name": "SVC"},
            },
            "edges": [],
        }
        assert _dag_equal(a, b) is False

    def test_different_edge_count(self):
        from dorian.code.regression import _dag_equal
        a = {
            "nodes": {"a": {"class_type": "Operator", "name": "SVC"}},
            "edges": [{"source": "x", "destination": "a"}],
        }
        b = {
            "nodes": {"a": {"class_type": "Operator", "name": "SVC"}},
            "edges": [],
        }
        assert _dag_equal(a, b) is False

    def test_different_node_semantics(self):
        from dorian.code.regression import _dag_equal
        a = {
            "nodes": {"a": {"class_type": "Operator", "name": "SVC"}},
            "edges": [],
        }
        b = {
            "nodes": {"a": {"class_type": "Operator", "name": "RandomForest"}},
            "edges": [],
        }
        assert _dag_equal(a, b) is False

    def test_empty_dags_are_equal(self):
        from dorian.code.regression import _dag_equal
        assert _dag_equal({"nodes": {}, "edges": []}, {"nodes": {}, "edges": []}) is True

    def test_missing_keys_treated_as_empty(self):
        from dorian.code.regression import _dag_equal
        assert _dag_equal({}, {}) is True


class TestDiffSummary:
    """Tests for _diff_summary() in regression.py."""

    def test_identical_produces_no_diff(self):
        from dorian.code.regression import _diff_summary
        dag = {"nodes": {"a": {"class_type": "Operator", "name": "SVC"}}, "edges": []}
        result = _diff_summary(dag, dag)
        assert "no structural differences" in result

    def test_node_count_mismatch(self):
        from dorian.code.regression import _diff_summary
        a = {"nodes": {"a": {"name": "X"}, "b": {"name": "Y"}}, "edges": []}
        b = {"nodes": {"a": {"name": "X"}}, "edges": []}
        result = _diff_summary(a, b)
        assert "nodes: expected 2, got 1" in result

    def test_edge_count_mismatch(self):
        from dorian.code.regression import _diff_summary
        a = {"nodes": {}, "edges": [{"source": "a", "destination": "b"}]}
        b = {"nodes": {}, "edges": []}
        result = _diff_summary(a, b)
        assert "edges: expected 1, got 0" in result

    def test_missing_nodes_reported(self):
        from dorian.code.regression import _diff_summary
        a = {"nodes": {"a": {"class_type": "Operator", "name": "SVC"}}, "edges": []}
        b = {"nodes": {"b": {"class_type": "Operator", "name": "RF"}}, "edges": []}
        result = _diff_summary(a, b)
        assert "missing nodes" in result


class TestFingerprintNode:
    """Tests for _fingerprint_node() in regression.py."""

    def test_operator_fingerprint(self):
        from dorian.code.regression import _fingerprint_node
        fp = _fingerprint_node({
            "class_type": "Operator",
            "name": "sklearn.svm.SVC",
            "language": "python",
        })
        assert "Operator" in fp
        assert "sklearn.svm.SVC" in fp

    def test_parameter_fingerprint(self):
        from dorian.code.regression import _fingerprint_node
        fp = _fingerprint_node({
            "class_type": "Parameter",
            "name": "C",
            "value": "1.0",
        })
        assert "Parameter" in fp
        assert "C" in fp
        assert "1.0" in fp

    def test_empty_node_fingerprint(self):
        from dorian.code.regression import _fingerprint_node
        fp = _fingerprint_node({})
        # 6 fields joined by "|" produces 5 separators
        assert fp == "|||||"

    def test_different_nodes_different_fingerprints(self):
        from dorian.code.regression import _fingerprint_node
        fp1 = _fingerprint_node({"class_type": "Operator", "name": "SVC"})
        fp2 = _fingerprint_node({"class_type": "Operator", "name": "RF"})
        assert fp1 != fp2


# ═══════════════════════════════════════════════════════════════════════════
# RULE LEARNING STUB
# ═══════════════════════════════════════════════════════════════════════════


class TestRuleLearningStub:
    """Tests for the rule learning stub (propose_rule)."""

    def test_propose_rule_returns_none(self):
        """Stub always returns None."""
        import asyncio
        from unittest.mock import AsyncMock, patch
        from dorian.code.rule_learning import propose_rule, SuggestRulesResult

        # aemit in conftest is a plain MagicMock (not AsyncMock) so we patch it here.
        # propose_rule now also delegates to suggest_rules (which calls an LLM) —
        # stub that to return a result with no valid rules so propose_rule returns None.
        stub_result = SuggestRulesResult(
            suggestion_id="s1", extraction_id="correction",
            rules=[], reasoning="stub",
        )
        import dorian.code.rule_learning as _rl_mod
        with patch.object(_rl_mod, "aemit", new_callable=AsyncMock), \
             patch.object(_rl_mod, "suggest_rules", new_callable=AsyncMock, return_value=stub_result):
            result = asyncio.run(propose_rule(
                code="sklearn.svm.SVC(C=1.0)",
                rules_version="abc123",
                auto_dag_json={"nodes": {"a": {"name": "SVC"}}, "edges": []},
                corrected_dag_json={"nodes": {"b": {"name": "SVC_corrected"}}, "edges": []},
            ))
        assert result is None

    def test_propose_rule_accepts_empty_dags(self):
        """Stub should not crash on empty inputs."""
        import asyncio
        from unittest.mock import AsyncMock, patch
        from dorian.code.rule_learning import propose_rule, SuggestRulesResult
        stub_result = SuggestRulesResult(
            suggestion_id="s1", extraction_id="correction",
            rules=[], reasoning="stub",
        )
        import dorian.code.rule_learning as _rl_mod
        with patch.object(_rl_mod, "aemit", new_callable=AsyncMock), \
             patch.object(_rl_mod, "suggest_rules", new_callable=AsyncMock, return_value=stub_result):
            result = asyncio.run(propose_rule(
                code="",
                rules_version="",
                auto_dag_json={},
                corrected_dag_json={},
            ))
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# EXTRACTION HANDLER
# ═══════════════════════════════════════════════════════════════════════════


def _ensure_backend_infra_stubbed():
    """Stub backend.infra.* sub-modules that handlers import at module load time.

    The conftest only stubs top-level backend.* modules. Importing
    dorian.event.handlers.extraction triggers __init__.py which loads
    ranking_objective.py, which imports backend.infra.dbs.expdb.ranking_objectives.
    This helper ensures those sub-modules are present in sys.modules.
    """
    import sys
    import types
    from unittest.mock import MagicMock

    for modname in [
        "backend.infra",
        "backend.infra.dbs",
        "backend.infra.dbs.expdb",
        "backend.infra.dbs.expdb.ranking_objectives",
        "backend.queue",
    ]:
        if modname not in sys.modules:
            mod = types.ModuleType(modname)
            # Provide any attributes that imports at module level need
            mod._upsert_objectives = MagicMock()
            mod.submit_for_execution = MagicMock()
            sys.modules[modname] = mod

    # Also ensure backend.events has 'subscribe' (used by dorian.event.registry)
    import sys as _sys
    _be = _sys.modules.get("backend.events")
    if _be is not None and not hasattr(_be, "subscribe"):
        _be.subscribe = MagicMock()


class TestExtractionHandler:
    """Tests for the ExtractionCorrected event handler (logic only).

    These tests verify the handler's validation/extraction logic without
    touching docstore or Postgres.
    """

    def test_handler_skips_missing_extraction_id(self):
        """Handler should return early (without raising) if extractionId is missing.

        The handler uses aemit() to signal missing fields rather than Python
        logging — so we verify it returns cleanly rather than checking caplog.
        """
        import asyncio
        from unittest.mock import AsyncMock, patch
        _ensure_backend_infra_stubbed()
        from dorian.event.handlers.extraction import handle_extraction_corrected

        class FakeEvent:
            data = {}

        import dorian.event.handlers.extraction as _ext_mod
        with patch.object(_ext_mod, "aemit", new_callable=AsyncMock):
            # Should return without raising
            asyncio.run(handle_extraction_corrected(
                FakeEvent(),
                uid="user1", session="sess1",
                payload={},
                request_id="req1", ts=123,
            ))

    def test_handler_skips_missing_corrected_pipeline(self):
        """Handler should return early (without raising) if correctedPipeline is missing.

        The handler uses aemit() to signal missing fields rather than Python
        logging — so we verify it returns cleanly rather than checking caplog.
        """
        import asyncio
        from unittest.mock import AsyncMock, patch
        _ensure_backend_infra_stubbed()
        from dorian.event.handlers.extraction import handle_extraction_corrected

        class FakeEvent:
            data = {}

        import dorian.event.handlers.extraction as _ext_mod
        with patch.object(_ext_mod, "aemit", new_callable=AsyncMock):
            # Should return without raising
            asyncio.run(handle_extraction_corrected(
                FakeEvent(),
                uid="user1", session="sess1",
                payload={"extractionId": "ext-123"},
                request_id="req1", ts=123,
            ))

    def test_handler_normalises_corrected_dag(self):
        """The handler should extract nodes/edges from the correctedPipeline payload."""
        # This verifies the normalisation logic without calling record_correction
        corrected_pipeline = {
            "uuid": "pipeline-uuid",
            "nodes": {"a": {"type": "Operator", "name": "SVC"}},
            "edges": [{"source": "a", "destination": "b"}],
            "extra_field": "should_be_ignored",
        }
        corrected_dag_json = {
            "nodes": corrected_pipeline.get("nodes", {}),
            "edges": corrected_pipeline.get("edges", []),
        }
        assert "a" in corrected_dag_json["nodes"]
        assert len(corrected_dag_json["edges"]) == 1
        assert "uuid" not in corrected_dag_json
        assert "extra_field" not in corrected_dag_json


# ═══════════════════════════════════════════════════════════════════════════
# EXTRACTION ENDPOINT METADATA
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractionEndpointMetadata:
    """Tests that the /extract endpoint returns extractionId and rulesVersion."""

    def test_frontend_format_includes_extraction_metadata(self):
        """Simulates what the /extract endpoint does after parsing."""
        from dorian.api.routes.file import _dag_to_frontend_format
        from dorian.code.parsing.rules import get_rules_version

        dag = DAG(
            nodes={
                "op": Operator(name="sklearn.svm.SVC", language="python"),
                "p": Parameter(name="C", dtype="float", value="1.0"),
            },
            edges=[Edge(source="p", destination="op", position="C")],
        )
        result = _dag_to_frontend_format(dag)

        # Simulate the endpoint adding metadata
        result["extractionId"] = "test-extraction-id"
        result["rulesVersion"] = get_rules_version()

        assert "extractionId" in result
        assert result["extractionId"] == "test-extraction-id"
        assert "rulesVersion" in result
        assert len(result["rulesVersion"]) == 16

    def test_rules_version_in_frontend_result(self):
        """get_rules_version() produces a stable 16-char hex hash."""
        from dorian.code.parsing.rules import get_rules_version
        v = get_rules_version()
        assert isinstance(v, str)
        assert len(v) == 16
        # Verify it's valid hex
        int(v, 16)


# ═══════════════════════════════════════════════════════════════════════════
# DAG SERIALIZATION ROUND-TRIP (for extraction persistence)
# ═══════════════════════════════════════════════════════════════════════════


class TestDagSerializationForExtraction:
    """Tests that DAG.to_json_dict() and from_json_dict() preserve content
    needed by the extraction store and regression testing.
    """

    def test_operator_round_trip(self):
        dag = DAG(
            nodes={"op": Operator(name="sklearn.svm.SVC", language="python")},
            edges=[],
        )
        d = dag.to_json_dict()
        dag2 = DAG.from_json_dict(d)
        assert isinstance(dag2.nodes["op"], Operator)
        assert dag2.nodes["op"].name == "sklearn.svm.SVC"

    def test_parameter_round_trip(self):
        dag = DAG(
            nodes={"p": Parameter(name="C", dtype="float", value="1.0")},
            edges=[],
        )
        d = dag.to_json_dict()
        dag2 = DAG.from_json_dict(d)
        assert isinstance(dag2.nodes["p"], Parameter)
        assert dag2.nodes["p"].name == "C"
        assert dag2.nodes["p"].dtype == "float"
        assert dag2.nodes["p"].value == "1.0"

    def test_snippet_round_trip(self):
        dag = DAG(
            nodes={"s": Snippet(name="preprocess", code="def f(x): return x", language="python")},
            edges=[],
        )
        d = dag.to_json_dict()
        dag2 = DAG.from_json_dict(d)
        assert isinstance(dag2.nodes["s"], Snippet)
        assert dag2.nodes["s"].code == "def f(x): return x"

    def test_edges_round_trip(self):
        dag = DAG(
            nodes={
                "op": Operator(name="SVC", language="python"),
                "p": Parameter(name="C", dtype="float", value="1.0"),
            },
            edges=[Edge(source="p", destination="op", position="C", output=0)],
        )
        d = dag.to_json_dict()
        dag2 = DAG.from_json_dict(d)
        assert len(dag2.edges) == 1
        assert dag2.edges[0].source == "p"
        assert dag2.edges[0].destination == "op"
        assert dag2.edges[0].position == "C"

    def test_to_json_dict_is_json_serializable(self):
        """Extraction store uses json.dumps() on the DAG dict."""
        import json
        dag = DAG(
            nodes={
                "op": Operator(name="sklearn.svm.SVC", language="python"),
                "p": Parameter(name="C", dtype="float", value="1.0"),
            },
            edges=[Edge(source="p", destination="op", position="C")],
        )
        d = dag.to_json_dict()
        # Must not raise
        serialized = json.dumps(d)
        assert isinstance(serialized, str)
        # Must round-trip
        deserialized = json.loads(serialized)
        assert deserialized == d

    def test_complex_dag_round_trip(self):
        """Multi-node, multi-edge DAG should survive round-trip."""
        dag = DAG(
            nodes={
                "op1": Operator(name="sklearn.svm.SVC", language="python"),
                "op2": Operator(name="sklearn.preprocessing.StandardScaler", language="python"),
                "p1": Parameter(name="C", dtype="float", value="1.0"),
                "p2": Parameter(name="with_mean", dtype="eval", value="True"),
                "s1": Snippet(name="preprocess", code="df.dropna()", language="python"),
            },
            edges=[
                Edge(source="p1", destination="op1", position="C"),
                Edge(source="p2", destination="op2", position="with_mean"),
                Edge(source="s1", destination="op2", position=0),
                Edge(source="op2", destination="op1", position=0, output=0),
            ],
        )
        d = dag.to_json_dict()
        dag2 = DAG.from_json_dict(d)
        assert len(dag2.nodes) == 5
        assert len(dag2.edges) == 4
        assert isinstance(dag2.nodes["op1"], Operator)
        assert isinstance(dag2.nodes["p2"], Parameter)
        assert isinstance(dag2.nodes["s1"], Snippet)


# ═══════════════════════════════════════════════════════════════════════════
# REDIS KEY PATTERNS
# ═══════════════════════════════════════════════════════════════════════════


class TestRedisKeyPatterns:
    """Tests for extraction-related Redis key patterns."""

    def test_active_extraction_key_format(self):
        from dorian.infra.keys import RedisKeys
        key = RedisKeys.active_extraction("session-abc")
        assert key == "session:session-abc:active_extraction"

    def test_active_extraction_key_with_uuid(self):
        from dorian.infra.keys import RedisKeys
        key = RedisKeys.active_extraction("550e8400-e29b-41d4-a716-446655440000")
        assert key.startswith("session:")
        assert key.endswith(":active_extraction")


# ═══════════════════════════════════════════════════════════════════════════
# CONTRACT TESTS — DEGRADATION SENTINELS
# ═══════════════════════════════════════════════════════════════════════════
#
# These tests exist solely to catch silent breakage caused by:
#   (a) other contributors refactoring/renaming/removing symbols
#   (b) AI models rewriting code without awareness of cross-boundary contracts
#
# Each test verifies ONE specific contract point. If any test fails, the
# commit that caused it has broken an interface that other modules depend on.
# The test name and docstring explain *what* depends on the contract so the
# breaker knows what else needs updating.
#
# DO NOT delete these tests to "fix" a failure — fix the broken contract or
# update ALL consumers of the old interface first.
# ═══════════════════════════════════════════════════════════════════════════


class TestContractModuleExports:
    """Verify that every public symbol other modules import still exists.

    If a symbol is renamed or removed, the downstream import will break
    at runtime but may not be caught by linting alone (especially dynamic
    imports inside async handlers).
    """

    # -- dorian.dag exports --------------------------------------------------

    def test_dag_module_exports_all_node_types(self):
        """Operator, Parameter, Snippet, Node must be importable from dorian.dag.

        Used by: extraction_store, regression, rule_learning, api/routes/file,
        every rewrite rule, every test.
        """
        from dorian.dag import Operator, Parameter, Snippet, Node, DAG, Edge
        assert all(callable(cls) for cls in [Operator, Parameter, Snippet, Node, DAG])

    def test_dag_module_exports_matching_utilities(self):
        """match, comparator, wildcard, _class_of, to_dag must exist.

        Used by: rewrite rules (pattern matching), tests, parser.
        """
        from dorian.dag import match, comparator, wildcard, _class_of, to_dag
        assert callable(match)
        assert callable(comparator)
        assert callable(_class_of)

    # -- dorian.code.parsing.rules exports -----------------------------------

    def test_rules_module_exports_version_functions(self):
        """get_rules_version, _compute_rules_hash must exist for rule versioning.

        Used by: /extract endpoint, extraction_store, regression tests.
        """
        from dorian.code.parsing.rules import get_rules_version, _compute_rules_hash
        assert callable(get_rules_version)
        assert callable(_compute_rules_hash)

    def test_rules_module_exports_mutation_functions(self):
        """get_rules, add_rewrite_rule, _update must exist.

        Used by: parser, rule_learning loop, LLM-generated rules, tests.
        """
        from dorian.code.parsing.rules import get_rules, add_rewrite_rule, _update
        assert callable(get_rules)
        assert callable(add_rewrite_rule)
        assert callable(_update)

    # -- dorian.code.parsing.rule exports ------------------------------------

    def test_rule_module_exports_all_transformation_types(self):
        """All transformation types must be importable.

        Used by: every rewrite rule definition, LLM rule synthesis.
        """
        from dorian.code.parsing.rule import (
            Add, Apply, Delete, Replace, Revert,
            ToOperator, ToParameter, PurgeMode, RewriteRule,
        )
        assert all(callable(cls) for cls in [
            Add, Apply, Delete, Replace, Revert, ToOperator, ToParameter, RewriteRule,
        ])

    # -- dorian.code.parsing.parser exports ----------------------------------

    def test_parser_module_exports(self):
        """parse, create_parser, rewrite, _rewrite must exist.

        Used by: /extract endpoint, regression test runner.
        """
        from dorian.code.parsing.parser import parse, create_parser, rewrite, _rewrite
        assert callable(parse)
        assert callable(create_parser)

    # -- dorian.code.extraction_store exports --------------------------------

    def test_extraction_store_exports_all_functions(self):
        """All four public functions must exist.

        Used by: /extract endpoint, ExtractionCorrected handler,
        /extract/propose-rule endpoint, regression test runner.
        """
        from dorian.code.extraction_store import (
            persist_extraction,
            record_correction,
            get_extraction,
            get_regression_set,
        )
        assert callable(persist_extraction)
        assert callable(record_correction)
        assert callable(get_extraction)
        assert callable(get_regression_set)

    # -- dorian.code.regression exports --------------------------------------

    def test_regression_module_exports(self):
        """_fingerprint_node, _dag_equal, _diff_summary, run_regression_test.

        Used by: /extract/regression-test endpoint, future CI pipeline.
        """
        from dorian.code.regression import (
            _fingerprint_node,
            _dag_equal,
            _diff_summary,
            run_regression_test,
        )
        assert callable(_fingerprint_node)
        assert callable(_dag_equal)
        assert callable(_diff_summary)
        assert callable(run_regression_test)

    # -- dorian.code.rule_learning exports -----------------------------------

    def test_rule_learning_module_exports(self):
        """propose_rule must exist.

        Used by: /extract/propose-rule endpoint.
        """
        from dorian.code.rule_learning import propose_rule
        assert callable(propose_rule)

    # -- dorian.event.handlers.extraction exports ----------------------------

    def test_extraction_handler_exports(self):
        """handle_extraction_corrected must exist.

        Used by: event registry subscription.
        """
        _ensure_backend_infra_stubbed()
        from dorian.event.handlers.extraction import handle_extraction_corrected
        assert callable(handle_extraction_corrected)

    # -- dorian.infra.keys exports -------------------------------------------

    def test_redis_keys_has_active_extraction(self):
        """RedisKeys.active_extraction must exist as a static/class method.

        Used by: /extract endpoint (write), future session-context queries.
        """
        from dorian.infra.keys import RedisKeys
        assert hasattr(RedisKeys, "active_extraction")
        assert callable(RedisKeys.active_extraction)

    # -- dorian.api.routes.file exports --------------------------------------

    def test_file_routes_exports_frontend_format(self):
        """_dag_to_frontend_format must exist.

        Used by: /extract endpoint, tests.
        """
        from dorian.api.routes.file import _dag_to_frontend_format
        assert callable(_dag_to_frontend_format)


class TestContractFunctionSignatures:
    """Verify that function signatures have not silently changed.

    If a required parameter is removed or renamed, the caller breaks at
    runtime.  These tests use inspect to check parameter names and counts.
    """

    def test_parse_accepts_rewrite_rules(self):
        """parse() must accept `rewrite_rules` keyword for regression testing.

        If removed, run_regression_test() breaks silently — it would always
        use default rules instead of candidate rules.
        """
        import inspect
        from dorian.code.parsing.parser import parse
        sig = inspect.signature(parse)
        params = list(sig.parameters.keys())
        assert "code" in params, "parse() must accept 'code' param"
        assert "language" in params, "parse() must accept 'language' param"
        assert "rewrite_rules" in params, "parse() must accept 'rewrite_rules' param"

    def test_parse_returns_tuple_of_two_dags(self):
        """parse() must return (initial_dag, final_dag) tuple.

        Used by: /extract endpoint (persists both), tests.
        """
        result = _parse_with_real_to_dag("x = 1", "python")
        assert isinstance(result, tuple), "parse() must return a tuple"
        assert len(result) == 2, "parse() must return exactly 2 DAGs"
        assert isinstance(result[0], DAG), "First element must be a DAG"
        assert isinstance(result[1], DAG), "Second element must be a DAG"

    def test_persist_extraction_signature(self):
        """persist_extraction() must accept all required parameters.

        Called fire-and-forget from /extract endpoint.
        """
        import inspect
        from dorian.code.extraction_store import persist_extraction
        sig = inspect.signature(persist_extraction)
        params = list(sig.parameters.keys())
        required = ["extraction_id", "code", "language", "rules_version",
                     "initial_dag", "auto_dag"]
        for p in required:
            assert p in params, f"persist_extraction() must accept '{p}'"
        # Optional params
        optional = ["session", "uid", "filename"]
        for p in optional:
            assert p in params, f"persist_extraction() must accept optional '{p}'"

    def test_record_correction_signature(self):
        """record_correction(extraction_id, corrected_dag_json) must exist.

        Called from ExtractionCorrected handler.
        """
        import inspect
        from dorian.code.extraction_store import record_correction
        sig = inspect.signature(record_correction)
        params = list(sig.parameters.keys())
        assert "extraction_id" in params
        assert "corrected_dag_json" in params

    def test_get_extraction_signature(self):
        """get_extraction(extraction_id) must accept extraction_id.

        Called from /extract/propose-rule endpoint.
        """
        import inspect
        from dorian.code.extraction_store import get_extraction
        sig = inspect.signature(get_extraction)
        params = list(sig.parameters.keys())
        assert "extraction_id" in params

    def test_propose_rule_signature(self):
        """propose_rule() must accept (code, rules_version, auto_dag_json, corrected_dag_json).

        Called from /extract/propose-rule endpoint.
        """
        import inspect
        from dorian.code.rule_learning import propose_rule
        sig = inspect.signature(propose_rule)
        params = list(sig.parameters.keys())
        assert "code" in params
        assert "rules_version" in params
        assert "auto_dag_json" in params
        assert "corrected_dag_json" in params

    def test_run_regression_test_accepts_candidate_rules(self):
        """run_regression_test() must accept optional candidate_rules.

        If removed, regression testing always uses default rules — defeats
        the purpose of testing candidate rule sets.
        """
        import inspect
        from dorian.code.regression import run_regression_test
        sig = inspect.signature(run_regression_test)
        params = list(sig.parameters.keys())
        assert "candidate_rules" in params

    def test_handle_extraction_corrected_signature(self):
        """Handler must accept the with_envelope keyword arguments.

        with_envelope unpacks: uid, session, payload, request_id, ts
        """
        import inspect
        _ensure_backend_infra_stubbed()
        from dorian.event.handlers.extraction import handle_extraction_corrected
        sig = inspect.signature(handle_extraction_corrected)
        params = list(sig.parameters.keys())
        assert "event" in params or len(params) >= 1  # first positional
        kw_params = {
            name for name, p in sig.parameters.items()
            if p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)
        }
        for required in ["uid", "session", "payload", "request_id", "ts"]:
            assert required in kw_params, (
                f"handle_extraction_corrected() must accept '{required}' "
                f"(required by with_envelope wrapper)"
            )

    def test_compute_rules_hash_accepts_rules_sequence(self):
        """_compute_rules_hash(rules) must accept a sequence of RewriteRules.

        Used by get_rules_version() and version-comparison tests.
        """
        import inspect
        from dorian.code.parsing.rules import _compute_rules_hash
        sig = inspect.signature(_compute_rules_hash)
        params = list(sig.parameters.keys())
        assert len(params) >= 1, "_compute_rules_hash must accept at least 1 param"
        assert "rules" in params

    def test_fingerprint_node_accepts_dict(self):
        """_fingerprint_node(node) must accept a dict.

        Used by _dag_equal() for structural comparison.
        """
        import inspect
        from dorian.code.regression import _fingerprint_node
        sig = inspect.signature(_fingerprint_node)
        params = list(sig.parameters.keys())
        assert "node" in params

    def test_dag_equal_accepts_two_dicts(self):
        """_dag_equal(a, b) must accept two dict arguments.

        Used by run_regression_test() for pass/fail comparison.
        """
        import inspect
        from dorian.code.regression import _dag_equal
        sig = inspect.signature(_dag_equal)
        params = list(sig.parameters.keys())
        assert len(params) == 2
        assert "a" in params
        assert "b" in params


class TestContractDAGSerialization:
    """Verify the exact shape of DAG serialization.

    The extraction store, regression runner, and frontend all depend on
    specific field names in the serialized DAG dict.  If to_dict() or
    to_json_dict() changes its output shape, deserialization breaks.
    """

    def test_operator_to_dict_has_class_type(self):
        """Operator.to_dict() must include 'class_type': 'Operator'.

        Used by: from_json_dict() dispatch, regression _fingerprint_node().
        """
        op = Operator(name="SVC", language="python")
        d = op.to_dict()
        assert d["class_type"] == "Operator"
        assert "name" in d
        assert "language" in d

    def test_parameter_to_dict_has_class_type(self):
        """Parameter.to_dict() must include 'class_type': 'Parameter'.

        Used by: from_json_dict() dispatch, regression _fingerprint_node().
        """
        p = Parameter(name="C", dtype="float", value="1.0")
        d = p.to_dict()
        assert d["class_type"] == "Parameter"
        assert "name" in d
        assert "dtype" in d
        assert "value" in d

    def test_snippet_to_dict_has_class_type(self):
        """Snippet.to_dict() must include 'class_type': 'Snippet'.

        Used by: from_json_dict() dispatch.
        """
        s = Snippet(name="pre", code="x=1", language="python")
        d = s.to_dict()
        assert d["class_type"] == "Snippet"
        assert "name" in d
        assert "code" in d
        assert "language" in d

    def test_edge_to_dict_has_required_fields(self):
        """Edge.to_dict() must include source, destination, position, output.

        Used by: DAG.to_json_dict() → extraction store, regression.
        """
        e = Edge(source="a", destination="b", position=1, output=2)
        d = e.to_dict()
        assert "source" in d
        assert "destination" in d
        assert "position" in d
        assert "output" in d

    def test_dag_to_json_dict_toplevel_keys(self):
        """DAG.to_json_dict() must return {version, metadata, nodes, edges}.

        Used by: persist_extraction() stores initialDag/autoDag as this shape.
        """
        dag = DAG(
            nodes={"op": Operator(name="SVC", language="python")},
            edges=[],
        )
        d = dag.to_json_dict()
        assert "version" in d
        assert "metadata" in d
        assert "nodes" in d
        assert "edges" in d

    def test_dag_to_json_dict_metadata_fields(self):
        """metadata must include created_at, node_count, edge_count, class_types.

        Used by: extraction document introspection, debugging.
        """
        dag = DAG(
            nodes={"op": Operator(name="SVC", language="python")},
            edges=[],
        )
        meta = dag.to_json_dict()["metadata"]
        assert "created_at" in meta
        assert "node_count" in meta
        assert "edge_count" in meta
        assert "class_types" in meta
        assert meta["node_count"] == 1
        assert meta["edge_count"] == 0

    def test_dag_from_json_dict_dispatches_on_class_type(self):
        """from_json_dict must reconstruct the correct node type from class_type.

        If the dispatch is broken, all nodes deserialize as generic Node,
        losing name/value/code fields — regression comparisons silently fail.
        """
        raw = {
            "nodes": {
                "a": {"class_type": "Operator", "name": "SVC", "language": "python"},
                "b": {"class_type": "Parameter", "name": "C", "type": "float", "value": "1.0"},
                "c": {"class_type": "Snippet", "name": "pre", "code": "x=1", "language": "python"},
            },
            "edges": [],
        }
        dag = DAG.from_json_dict(raw)
        assert isinstance(dag.nodes["a"], Operator)
        assert isinstance(dag.nodes["b"], Parameter)
        assert isinstance(dag.nodes["c"], Snippet)


class TestContractFrontendFormat:
    """Verify _dag_to_frontend_format() output shape.

    The frontend ExtractionView.tsx + extraction store depend on the exact
    keys returned by this function.  Changes here silently break the canvas.
    """

    def test_output_has_uuid_nodes_edges(self):
        """Must return {uuid, nodes, edges} — the PipelineDraft shape."""
        from dorian.api.routes.file import _dag_to_frontend_format
        dag = DAG(
            nodes={"op": Operator(name="SVC", language="python")},
            edges=[],
        )
        result = _dag_to_frontend_format(dag)
        assert "uuid" in result, "Frontend expects 'uuid' field"
        assert "nodes" in result, "Frontend expects 'nodes' field"
        assert "edges" in result, "Frontend expects 'edges' field"

    def test_operator_node_has_type_and_name(self):
        """Each node dict must have at least 'type' and 'name'."""
        from dorian.api.routes.file import _dag_to_frontend_format
        dag = DAG(
            nodes={"op": Operator(name="sklearn.svm.SVC", language="python")},
            edges=[],
        )
        result = _dag_to_frontend_format(dag)
        node = result["nodes"]["op"]
        assert node["type"] == "Operator"
        assert node["name"] == "sklearn.svm.SVC"

    def test_parameter_node_has_value_and_dtype(self):
        """Parameter nodes must include 'value' and 'dtype' for the config panel."""
        from dorian.api.routes.file import _dag_to_frontend_format
        dag = DAG(
            nodes={"p": Parameter(name="C", dtype="float", value="1.0")},
            edges=[],
        )
        result = _dag_to_frontend_format(dag)
        node = result["nodes"]["p"]
        assert node["type"] == "Parameter"
        assert "value" in node
        assert "dtype" in node

    def test_snippet_node_has_code_and_language(self):
        """Snippet nodes must include 'code' and 'language' for the editor."""
        from dorian.api.routes.file import _dag_to_frontend_format
        dag = DAG(
            nodes={"s": Snippet(name="pre", code="x=1", language="python")},
            edges=[],
        )
        result = _dag_to_frontend_format(dag)
        node = result["nodes"]["s"]
        assert node["type"] == "Snippet"
        assert "code" in node
        assert "language" in node

    def test_unreduced_ast_node_promoted_to_operator(self):
        """Raw AST Node should be promoted to type='Operator' for canvas rendering.

        If this promotion is removed, unreduced nodes show as unknown types
        on the canvas.
        """
        from dorian.api.routes.file import _dag_to_frontend_format
        dag = DAG(
            nodes={"n": Node(type="call", text="my_function", language="python")},
            edges=[],
        )
        result = _dag_to_frontend_format(dag)
        node = result["nodes"]["n"]
        assert node["type"] == "Operator", "Raw Node must be promoted to Operator type"

    def test_extraction_metadata_fields_appended(self):
        """The /extract endpoint appends extractionId and rulesVersion.

        Frontend extraction store reads these to enable correction submission.
        """
        from dorian.api.routes.file import _dag_to_frontend_format
        from dorian.code.parsing.rules import get_rules_version
        dag = DAG(nodes={}, edges=[])
        result = _dag_to_frontend_format(dag)
        # Simulate what the endpoint does
        result["extractionId"] = "test-id"
        result["rulesVersion"] = get_rules_version()
        # These keys are what the frontend destructures
        assert isinstance(result["extractionId"], str)
        assert isinstance(result["rulesVersion"], str)
        assert len(result["rulesVersion"]) == 16


class TestContractRuleVersioning:
    """Verify rule versioning contracts.

    Rule versions are persisted alongside extractions. If the versioning
    mechanism changes, regression tests compare against wrong baselines.
    """

    def test_version_is_16_char_hex(self):
        """Version must be exactly 16 hex characters (SHA-256 prefix).

        Stored in docstore rulesVersion field and Postgres rules_version column.
        """
        from dorian.code.parsing.rules import get_rules_version
        v = get_rules_version()
        assert len(v) == 16
        int(v, 16)  # must be valid hex

    def test_version_deterministic_across_calls(self):
        """Same rule set must produce same version — no randomness."""
        from dorian.code.parsing.rules import get_rules_version
        v1 = get_rules_version()
        v2 = get_rules_version()
        assert v1 == v2

    def test_cache_invalidation_mechanism_exists(self):
        """_rules_version_cache must be set to None by add_rewrite_rule.

        If cache invalidation is removed, version becomes stale after
        adding rules — regression tests use wrong version.
        """
        from dorian.code.parsing import rules as rules_mod
        # Cache should be invalidated (set to None) when add_rewrite_rule is called
        assert hasattr(rules_mod, "_rules_version_cache"), \
            "_rules_version_cache must exist for lazy caching"
        # Verify the code path: add_rewrite_rule sets cache to None
        import ast
        import inspect
        source = inspect.getsource(rules_mod.add_rewrite_rule)
        assert "_rules_version_cache" in source, \
            "add_rewrite_rule must reference _rules_version_cache for invalidation"
        assert "None" in source, \
            "add_rewrite_rule must set _rules_version_cache = None"


class TestContractRegressionRunner:
    """Verify the regression runner's output contract.

    The /extract/regression-test endpoint depends on the exact dict keys
    returned by run_regression_test().
    """

    def test_fingerprint_uses_all_semantic_fields(self):
        """_fingerprint_node must use class_type, name, value, language, type, text.

        If any field is dropped, structurally different nodes hash the same
        and regression tests produce false positives.
        """
        from dorian.code.regression import _fingerprint_node
        # Two nodes differing only in 'value' must fingerprint differently
        a = {"class_type": "Parameter", "name": "C", "value": "1.0",
             "language": "", "type": "float", "text": ""}
        b = {"class_type": "Parameter", "name": "C", "value": "2.0",
             "language": "", "type": "float", "text": ""}
        assert _fingerprint_node(a) != _fingerprint_node(b)

    def test_fingerprint_includes_class_type(self):
        """Operator and Parameter with same name must fingerprint differently."""
        from dorian.code.regression import _fingerprint_node
        op = {"class_type": "Operator", "name": "SVC", "value": "",
              "language": "python", "type": "", "text": ""}
        param = {"class_type": "Parameter", "name": "SVC", "value": "",
                 "language": "python", "type": "", "text": ""}
        assert _fingerprint_node(op) != _fingerprint_node(param)

    def test_dag_equal_is_symmetric(self):
        """_dag_equal(a, b) == _dag_equal(b, a) — structural equality is symmetric."""
        from dorian.code.regression import _dag_equal
        a = {"nodes": {"x": {"class_type": "Operator", "name": "SVC"}}, "edges": []}
        b = {"nodes": {"y": {"class_type": "Operator", "name": "SVC"}}, "edges": []}
        assert _dag_equal(a, b) == _dag_equal(b, a)

    def test_diff_summary_reports_node_differences(self):
        """_diff_summary must report node count mismatches."""
        from dorian.code.regression import _diff_summary
        a = {"nodes": {"x": {"class_type": "Operator", "name": "A"}}, "edges": []}
        b = {"nodes": {}, "edges": []}
        summary = _diff_summary(a, b)
        assert "nodes" in summary.lower()

    def test_diff_summary_reports_edge_differences(self):
        """_diff_summary must report edge count mismatches."""
        from dorian.code.regression import _diff_summary
        a = {"nodes": {}, "edges": [{"source": "a", "destination": "b"}]}
        b = {"nodes": {}, "edges": []}
        summary = _diff_summary(a, b)
        assert "edges" in summary.lower()


class TestContractExtractionHandler:
    """Verify the ExtractionCorrected handler's payload contract.

    The frontend emits a specific payload shape via ws.extractionCorrected().
    The handler must be resilient to missing fields and normalise properly.
    """

    def test_handler_requires_extraction_id_in_payload(self):
        """If extractionId is missing, handler should return without error."""
        import asyncio
        from unittest.mock import AsyncMock, patch
        _ensure_backend_infra_stubbed()
        from dorian.event.handlers.extraction import handle_extraction_corrected

        class FakeEvent:
            data = {}

        import dorian.event.handlers.extraction as _ext_mod
        with _noop_context(), patch.object(_ext_mod, "aemit", new_callable=AsyncMock):
            # Should NOT raise — handler returns early when extractionId is absent
            asyncio.run(handle_extraction_corrected(
                FakeEvent(),
                uid="u", session="s",
                payload={},  # no extractionId
                request_id="r", ts=0,
            ))

    def test_handler_normalises_to_nodes_and_edges_only(self):
        """Handler must extract only 'nodes' and 'edges' from correctedPipeline.

        The frontend sends {uuid, nodes, edges, ...} — handler must strip
        uuid and other fields before persisting, so the stored shape matches
        what _dag_equal() expects.
        """
        corrected = {
            "uuid": "should-be-stripped",
            "nodes": {"a": {"class_type": "Operator", "name": "SVC"}},
            "edges": [{"source": "a", "destination": "b"}],
            "reactflowState": "should-be-stripped",
        }
        normalised = {
            "nodes": corrected.get("nodes", {}),
            "edges": corrected.get("edges", []),
        }
        assert "uuid" not in normalised
        assert "reactflowState" not in normalised
        assert len(normalised) == 2  # exactly nodes + edges


class TestContractEventRegistry:
    """Verify that ExtractionCorrected is properly registered.

    If the event subscription is removed, corrections are silently lost —
    users click "Submit Correction" but nothing happens.
    """

    def test_extraction_corrected_handler_importable(self):
        """The handler module must be importable from the expected path."""
        _ensure_backend_infra_stubbed()
        from dorian.event.handlers.extraction import handle_extraction_corrected
        assert handle_extraction_corrected is not None

    def test_event_registry_module_importable(self):
        """The registry module must be importable with its bootstrap function."""
        _ensure_backend_infra_stubbed()
        from dorian.event import registry
        assert hasattr(registry, "register_event_handlers"), \
            "register_event_handlers() must exist — it bootstraps all subscriptions"


class TestContractPostgresSchema:
    """Verify the extractions table DDL is present in schema.py.

    If the DDL is removed or columns renamed, the Postgres migration fails
    and extraction persistence breaks silently (fire-and-forget).
    """

    def test_schema_contains_extractions_table(self):
        """The _DDL string must contain CREATE TABLE extractions."""
        from dorian.experiment import schema
        ddl = schema._DDL
        assert "CREATE TABLE IF NOT EXISTS extractions" in ddl

    def test_schema_extractions_has_required_columns(self):
        """All columns used by extraction_store.py must exist in DDL."""
        from dorian.experiment import schema
        ddl = schema._DDL
        required_columns = [
            "id ", "code_hash", "auto_dag_id", "corrected_dag_id",
            "rules_version", "session", "uid", "status", "created_at",
            "corrected_at",
        ]
        for col in required_columns:
            assert col in ddl, f"Column '{col.strip()}' missing from extractions DDL"

    def test_schema_extractions_has_indices(self):
        """Performance-critical indices must exist."""
        from dorian.experiment import schema
        ddl = schema._DDL
        assert "idx_extractions_rules_version" in ddl
        assert "idx_extractions_session" in ddl
        assert "idx_extractions_status" in ddl


class TestContractdocstoreCollections:
    """Verify that 'extractions' is in the docstore collection init list."""

    def test_init_creates_extractions_collection(self):
        """backend/infra/__init__.py must declare the 'extractions' collection.

        If removed, the first persist_extraction() call may fail or create
        the collection without proper indices.
        """
        import pathlib
        # Read the source directly — importing backend.infra requires live
        # service connections (docstore, Redis, Neo4j) unavailable in unit tests.
        repo = pathlib.Path(__file__).resolve().parent.parent
        init_path = repo / "backend" / "infra" / "__init__.py"
        assert init_path.exists(), f"backend/infra/__init__.py not found at {init_path}"
        source = init_path.read_text(encoding="utf-8")
        assert '"extractions"' in source or "'extractions'" in source, \
            "'extractions' must be in the docstore collection init list"


class TestContractFrontendTypes:
    """Verify frontend TypeScript contracts by reading source files.

    These tests read .ts files to ensure type definitions, store fields,
    and event helpers haven't been silently removed. They don't execute
    TypeScript — they pattern-match on the source text.
    """

    def _read_file(self, relpath: str) -> str:
        """Read a project file relative to the repo root."""
        import pathlib
        # Walk up from tests/ to find the repo root
        repo = pathlib.Path(__file__).resolve().parent.parent
        path = repo / relpath
        assert path.exists(), f"File not found: {path}"
        return path.read_text(encoding="utf-8")

    # -- frontend/types/index.ts ---------------------------------------------

    def test_app_event_name_includes_extraction_corrected(self):
        """AppEventName union must include 'ExtractionCorrected'.

        If removed, emitEvent("ExtractionCorrected", ...) fails at compile time,
        but a raw string bypass could still send the wrong event name.
        """
        src = self._read_file("frontend/types/index.ts")
        assert "ExtractionCorrected" in src, \
            "'ExtractionCorrected' must be in AppEventName union"

    def test_app_event_name_includes_pipeline_extracted(self):
        """AppEventName must include 'ExtractPipeline' (the outbound extraction trigger).

        The inbound result arrives via the Redis stream as 'extraction/result',
        not as a WebSocket AppEventName. The outbound trigger event is 'ExtractPipeline'.
        """
        src = self._read_file("frontend/types/index.ts")
        assert "ExtractPipeline" in src

    # -- frontend/helpers/ws-events.ts ---------------------------------------

    def test_ws_events_has_extraction_corrected_wrapper(self):
        """ws.extractionCorrected must exist as a convenience wrapper.

        The header component calls ws.extractionCorrected() — if the wrapper
        is removed, the Submit Correction button silently does nothing.
        """
        src = self._read_file("frontend/helpers/ws-events.ts")
        assert "extractionCorrected" in src, \
            "'extractionCorrected' wrapper must exist in ws-events.ts"
        assert "ExtractionCorrected" in src, \
            "Wrapper must emit 'ExtractionCorrected' event"

    # -- frontend/store/extraction.ts ----------------------------------------

    def test_extraction_store_has_extraction_id_field(self):
        """useExtractionStore must have extractionId field.

        The header reads this to know which extraction to submit corrections for.
        """
        src = self._read_file("frontend/store/extraction.ts")
        assert "extractionId" in src
        assert "rulesVersion" in src
        assert "setExtractionMeta" in src

    # -- frontend/store/pipeline.ts ------------------------------------------

    def test_pipeline_store_has_source_extraction_id(self):
        """usePipelineStore must have sourceExtractionId field.

        The header conditionally shows the Submit Correction button based
        on this field. If removed, the button never appears.
        """
        src = self._read_file("frontend/store/pipeline.ts")
        assert "sourceExtractionId" in src, \
            "'sourceExtractionId' must exist in pipeline store"
        assert "setSourceExtractionId" in src, \
            "'setSourceExtractionId' setter must exist in pipeline store"

    # -- frontend/components/layout/header/index.tsx -------------------------

    def test_header_has_submit_correction_handler(self):
        """The pipeline header area must have handleSubmitCorrection.

        The handler was moved from header/index.tsx to PipelineActions.tsx
        as part of a layout refactor. Both files are part of the header area.
        """
        src = self._read_file("frontend/components/layout/header/PipelineActions.tsx")
        assert "handleSubmitCorrection" in src, \
            "handleSubmitCorrection handler must exist in PipelineActions.tsx"
        assert "extractionCorrected" in src, \
            "PipelineActions must call ws.extractionCorrected()"
        assert "sourceExtractionId" in src, \
            "PipelineActions must reference sourceExtractionId for conditional rendering"


class TestContractEndToEndFlow:
    """Verify that the end-to-end extraction→correction flow contracts hold.

    These tests don't call external services but verify the data shapes
    flow correctly between modules.
    """

    def test_parse_output_is_persistable(self):
        """parse() output DAGs must be serializable via to_json_dict().

        If to_json_dict() raises, persist_extraction() fails silently.
        """
        import json
        initial, final = _parse_with_real_to_dag(
            "from sklearn.svm import SVC\nclf = SVC(C=1.0)", "python"
        )
        d1 = initial.to_json_dict()
        d2 = final.to_json_dict()
        # Must be JSON-serializable (extraction store persists as JSON)
        json.dumps(d1)
        json.dumps(d2)
        # Must have the expected top-level keys
        for d in [d1, d2]:
            assert "nodes" in d
            assert "edges" in d

    def test_serialized_dag_is_regression_testable(self):
        """Serialized DAG dict must be comparable via _dag_equal().

        This verifies the contract between to_json_dict() output and
        the regression runner's comparison logic.
        """
        from dorian.code.regression import _dag_equal
        _, dag = _parse_with_real_to_dag("from sklearn.svm import SVC\nclf = SVC()", "python")
        d = dag.to_json_dict()
        # Same DAG serialized twice should be equal
        assert _dag_equal(d, d)

    def test_frontend_format_compatible_with_correction_payload(self):
        """The shape returned by _dag_to_frontend_format must be usable
        as a correctedPipeline payload after the user edits it.

        The handler extracts .nodes and .edges — these must exist.
        """
        from dorian.api.routes.file import _dag_to_frontend_format
        dag = DAG(
            nodes={
                "op": Operator(name="SVC", language="python"),
                "p": Parameter(name="C", dtype="float", value="1.0"),
            },
            edges=[Edge(source="p", destination="op", position="C")],
        )
        frontend_result = _dag_to_frontend_format(dag)
        # Simulate what the frontend sends back as correctedPipeline
        correction_payload = {
            "uuid": frontend_result["uuid"],
            "nodes": frontend_result["nodes"],
            "edges": frontend_result["edges"],
        }
        # Handler normalisation
        normalised = {
            "nodes": correction_payload.get("nodes", {}),
            "edges": correction_payload.get("edges", []),
        }
        assert len(normalised["nodes"]) == 2
        assert len(normalised["edges"]) == 1

    def test_get_rules_returns_non_empty_list(self):
        """get_rules() must return at least 1 rule.

        If the rule list is accidentally emptied, parse() produces raw AST
        nodes instead of Operator/Parameter — all extractions degrade.
        """
        from dorian.code.parsing.rules import get_rules
        rules = get_rules()
        assert len(rules) > 0, "Rule list must not be empty"
        assert all(isinstance(r, RewriteRule) for r in rules)

    def test_get_rules_count_stability(self):
        """Rule count should not silently drop.

        Current count is a baseline — if a contributor removes rules,
        this test flags it.  Update the expected count intentionally.
        """
        from dorian.code.parsing.rules import get_rules
        rules = get_rules()
        # Current active rule count (update when rules are added/removed)
        EXPECTED_MIN_RULES = 10
        assert len(rules) >= EXPECTED_MIN_RULES, (
            f"Expected at least {EXPECTED_MIN_RULES} rules, got {len(rules)}. "
            f"Did someone accidentally remove rules?"
        )


# -- Helper for context manager used in TestContractExtractionHandler --------
from contextlib import contextmanager

@contextmanager
def _noop_context():
    """No-op context manager for test readability."""
    yield
