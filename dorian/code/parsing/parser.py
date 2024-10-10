import tree_sitter_python as tspython
from tree_sitter import Language, Parser, Tree
from pathlib import Path
from typing import Sequence, Dict, Any, List, Tuple, Optional
from dataclasses import dataclass
from functools import reduce
from classes import typeclass
from itertools import product
from collections import defaultdict
from dataclasses import asdict
import asyncio
import re

from dorian.dag import to_dag, DAG, Node, Edge, ID, Variable, Parameter
from dorian.operator import Operator
from dorian.languages import SupportedLanguage

from dorian.code.parsing.rules import RewriteRule, Transformation, Add, Apply, Replace, Delete, PurgeMode
from dorian.dag import ID
from backend.config import base


# # TODO: handle parsers via configuration
# Language.build_library(
#     # Store the library in the `build` directory
#     str(base / 'build/my-languages.so'),
#     # Include one or more languages
#     [
#         str(base / 'third_party/parsers/tree-sitter-python'),
#         # str(base / 'third_party/parsers/tree-sitter-r'),
#         # str(base / 'third_party/parsers/tree-sitter-snakemake-pure')
#     ]
# )


Rules = Sequence[RewriteRule]
wildcard = r'.*'
s = lambda x: x.decode('utf-8') if isinstance(x, bytes) else x


def create_parser(language: SupportedLanguage) -> Parser:
    # language = Language((base / 'build/my-languages.so').resolve().as_posix(), language)
    language = Language(tspython.language(), "python")
    parser = Parser()
    parser.set_language(language)
    return parser


@dataclass
class Where:
    nid: ID
    attr: str


@dataclass
class ToParameter:
    nid: ID
    name: Where | str
    type: Where | str
    value: Where | str


@dataclass
class ToOperator:
    nid: ID
    name: Where | str
    language: Where | str


@dataclass
class ToDictionary:
    nid: ID


@dataclass
class Flip:
    edges: List[Tuple[ID, ID]]


@dataclass
class Wildcard:
    language: SupportedLanguage
    type: str = wildcard
    text: str = wildcard


def _handle_deleted_nodes(dag: DAG, nodes: List[ID]) -> DAG:
    to_remove, to_add = [], []
    for nid in nodes:
        sources = [e.source for e in dag.edges if e.destination == nid]
        destinations = [e.destination for e in dag.edges if e.source == nid]
        to_remove.extend([(s, nid) for s in sources])
        to_remove.extend([(nid, d) for d in destinations])
        to_add.extend([Edge(source=s, destination=d, position=0) for s, d in product(sources, destinations)])
    return DAG(
        nodes=dict((k, v) for k, v in dag.nodes.items() if k not in nodes),
        edges=[e for e in dag.edges + to_add if (e.source, e.destination) not in to_remove]
    )


def _rewrite(dag: DAG, mapping: Dict[ID, ID], transformation: Transformation) -> DAG:
    print(mapping, { k: v for k, v in dag.nodes.items() if k in mapping.values() }, transformation)
    match transformation:
        case Add():
            _nodes = dict((mapping[k], v) for k, v in transformation.nodes.items()) if transformation.nodes else {}
            _edges = [Edge(source=mapping[e[0]], destination=mapping[e[1]])
                      for e in transformation.edges] if transformation.edges else []
            for nid in _nodes:
                if nid in dag.nodes:
                    raise KeyError(nid)
            return DAG(nodes=dict(dag.nodes, **_nodes), edges=dag.edges + _edges)
        case Apply():
            return transformation.f(dag, mapping)
        case Replace():
            return DAG(nodes={}, edges=[])
        case Delete():
            mapped_nodes = list(map(lambda x: mapping[x], transformation.nodes))
            match transformation.mode:
                case 'recursive':
                    queue = mapped_nodes[:]
                    while queue:
                        nid = queue.pop()
                        added = [e.destination for e in dag.edges if e.source == nid]
                        mapped_nodes.extend(added)
                        queue.extend(added)
                case 'isolated':
                    dag = _handle_deleted_nodes(dag, mapped_nodes)
            _nodes = dict((k, v) for k, v in dag.nodes.items() if k not in mapped_nodes)
            foo = lambda x: (mapping[x[0]], mapping[x[1]])
            to_remove = list(map(foo, transformation.edges)) if transformation.edges else []
            _edges = [e for e in dag.edges if ((e.source, e.destination) not in to_remove)
                      and (e.source not in mapped_nodes) and (e.destination not in mapped_nodes)]
            return DAG(nodes=_nodes, edges=_edges)
        case ToOperator():
            op = {}
            for k in ['name', 'language']:
                val = getattr(transformation, k)
                if _class_of(val) == 'Where':
                    _id = mapping[val.nid]
                    op[k] = getattr(dag.nodes[_id], val.attr)
                else:
                    op[k] = val
            nid = mapping[transformation.nid]
            return DAG(nodes=dict(dag.nodes, **{nid: Operator(**op)}), edges=dag.edges)
        case ToParameter():
            p = {}
            nid = mapping[transformation.nid]
            for k in ['name', 'type', 'value']:
                val = getattr(transformation, k)
                if _class_of(val) == 'Where':
                    _id = mapping[val.nid]
                    match _class_of(dag.nodes[_id]):
                        case 'Node':
                            p[k] = getattr(dag.nodes[_id], getattr(transformation, k).attr)
                        case 'Operator':
                            p[k] = None
                        case unknown:
                            raise NotImplemented(f'ToParameter {unknown}')
                else:
                    p[k] = val
            return DAG(nodes=dict(dag.nodes, **{nid: Parameter(**p)}), edges=dag.edges)
        case Flip():
            to_remove = [Edge(source=mapping[e[0]], destination=mapping[e[1]]) for e in transformation.edges]
            to_add = [Edge(source=mapping[e[1]], destination=mapping[e[0]]) for e in transformation.edges]
            return DAG(nodes=dag.nodes, edges=[e for e in dag.edges if e not in to_remove] + to_add)
        case ToDictionary():
            nid = mapping[transformation.nid]
            return DAG(nodes=dict(dag.nodes, **{nid: Dictionary(dag.nodes[nid].language)}), edges=dag.edges)
        case unknown:
            raise NotImplemented(f'Unknown transformation "{unknown}" in f: rewrite')


def rewrite(dag: DAG, mapping: Dict[ID, ID], transformations: Sequence[Transformation]) -> DAG:
    return reduce(lambda g, tf: _rewrite(g, mapping, tf), transformations, dag)


def _class_of(obj):
    return obj.__class__.__name__


def comparator(one, another) -> bool:
    """Compares a DAG node against another node or pattern"""
    if _class_of(another) == 'Wildcard': return bool(re.match(another.type, _class_of(one)))
    if (_class_of(another) != 'Wildcard') and (_class_of(one) != _class_of(another)): return False
    match _class_of(one):
        case "Node":
            return (one.language == another.language) \
                & (True if not another.type or isinstance(another.type, Variable) else bool(re.match(another.type, s(one.type))))\
                & (True if not another.text or isinstance(another.text, Variable) else bool(re.match(another.text, s(one.text))))
        case "Operator":
            return (one.name == another.name) & (one.language == another.language)
        case "Parameter":
            return (one.name == another.name) & (one.type == another.type) & (one.value == another.value)


# TODO typing hint for comparable items
def has_single_value(_list: Sequence[Any]) -> bool:
    return len(set(_list)) == 1


def apply(rule: RewriteRule, dag: DAG) -> DAG:
    # element: T, elements: Sequence[Tuple[ID, T]], comparator: [T, T] -> bool
    def _iter(elements, element, _comparator):
        for idx, el in elements:
            if _comparator(el, element):
                yield idx

    # check if pattern has variables
    def is_variable(value):
        match value:
            case Variable():
                return True
            case _:
                return False

    has_variables = any([is_variable(n.type) or is_variable(n.text) for n in rule.pattern.nodes.values() if _class_of(n) != 'Wildcard'])

    print('\n', rule)

    for values in product(*map(lambda x: _iter(dag.nodes.items(), x, comparator), rule.pattern.nodes.values())):
        # candidate nodes should have unique IDs
        if len(values) != len(set(values)):
            continue

        candidate = dict(zip(rule.pattern.nodes.keys(), values))

        if has_variables:
            variables = defaultdict(list)
            for left, right in candidate.items():
                node = rule.pattern.nodes[left]
                match node.type:
                    case Variable():
                        variables[node.type.name].append(dag.nodes[right].type)
                match node.text:
                    case Variable():
                        variables[node.text.name].append(dag.nodes[right].text)

            if variables and not all(map(lambda x: has_single_value(x), variables.values())):
                continue

        if all(map(lambda e: Edge(candidate[e.source], candidate[e.destination], position=0) in dag.edges, rule.pattern.edges)):
            return apply(rule, rewrite(dag, candidate, rule.transformations))

    def prune(dag: DAG) -> DAG:
        _nn = dag.nodes
        return DAG(nodes=_nn, edges=list(set([e for e in dag.edges if (e.source in _nn) and (e.destination in _nn) and (e.source != e.destination)])))

    return prune(dag)


def transform(dag: DAG, rules: Rules | None = None) -> DAG:
    return reduce(lambda o, func: apply(func, o), rules, dag) if rules else dag


def get_rules() -> Rules:
    single_character = r'^.$'
    basic = r'string|integer|float|identifier'

    def _update(dag: DAG, key: ID, part: str, value: Any) -> DAG:
        node = Node(**dict(asdict(dag.nodes[key]), **{part: value}))
        return DAG(nodes=dict(dag.nodes, **{key: node}), edges=dag.edges)

    return [
        RewriteRule(
            pattern=DAG(
                nodes={'0': Node(type=single_character, text=single_character, language='python')},
                edges=[]
            ),
            transformations=[Delete(nodes=['0'])]),
        RewriteRule(
            pattern=DAG(
                nodes={'0': Node(type='module|expression_statement|argument_list|string', text=wildcard, language='python')},
                edges=[]
            ),
            transformations=[Delete(nodes=['0'])]),
        RewriteRule(
            pattern=DAG(
                nodes={
                    '0': Node(type='dotted_name', text=wildcard, language='python'),
                    '1': Node(type=wildcard, text=wildcard, language='python')},
                edges=[Edge(source='0', destination='1', position=0)]
            ),
            transformations=[Delete(nodes=['1'], edges=[('0', '1')])]),
        RewriteRule(
            pattern=DAG(
                nodes={
                    '0': Node(type='unary_operator', text=wildcard, language='python'),
                    '1': Node(type=wildcard, text=wildcard, language='python'),
                    '2': Node(type=wildcard, text=wildcard, language='python'),
                },
                edges=[Edge(source='0', destination='1', position=0), Edge(source='0', destination='2', position=0)]
            ),
            transformations=[
                Apply(f=lambda g, m: _update(g, m['0'], 'type', g.nodes[m["2"]].type)),
                Delete(nodes=['1', '2'], mode='recursive')
            ]),
        RewriteRule(
            pattern=DAG(
                nodes={
                    '0': Node(type='attribute', text=wildcard, language='python'),
                    '1': Node(type=wildcard, text=wildcard, language='python')
                },
                edges=[Edge(source='0', destination='1', position=0)]
            ),
            transformations=[Delete(nodes=['1'], mode='recursive')]),
        RewriteRule(
            pattern=DAG(
                nodes={
                    '0': Node(type='aliased_import', text=wildcard, language='python'),
                    '1': Node(type='dotted_name', text=wildcard, language='python'),
                    # '2': Node(type='as', text='as', language='python'),
                    '3': Node(type='dotted_name', text=Variable('X'), language='python'),
                    # '4': Node(type='attribute', text=wildcard, language='python'),
                    '5': Node(type='identifier', text=Variable('X'), language='python')
                },
                edges=[
                    Edge(source='0', destination='1', position=0),
                    # Edge(source='0', destination='2', position=0),
                    Edge(source='0', destination='3', position=0),
                    Edge(source='4', destination='5', position=0)
                ]
            ),
            transformations=[
                Apply(f=lambda g, m: _update(g, m['4'], 'text', g.nodes[m["4"]].text.replace(f'{g.nodes[m["3"]].text}.', f'{g.nodes[m["1"]].text}.'))),
                Delete(nodes=['3', '5'])
            ]
        ),
        RewriteRule(
            pattern=DAG(
                nodes={
                    '0': Node(type='import_statement|import_from_statement', text=wildcard, language='python'),
                    # '1': Node(type='from', text='from', language='python'),
                    '2': Node(type='dotted_name', text=wildcard, language='python'),
                    # '3': Node(type='import', text='import', language='python'),
                    '4': Node(type='dotted_name', text=Variable('X'), language='python'),
                    '5': Node(type='call', text=wildcard, language='python'),
                    '6': Node(type='identifier', text=Variable('X'), language='python')
                },
                edges=[
                    # Edge(source='0', destination='1', position=0),
                    Edge(source='0', destination='2', position=0),
                    # Edge(source='0', destination='3', position=0),
                    Edge(source='0', destination='4', position=0),
                    Edge(source='5', destination='6', position=0),
                ]
            ),
            transformations=[
                Apply(f=lambda g, m: _update(g, m['6'], 'text', f'{s(g.nodes[m["2"]].text)}.{s(g.nodes[m["6"]].text)}')),
                Delete(nodes=['4'])
            ]
        ),
        RewriteRule(
            pattern=DAG(
                nodes={
                    '0': Node(type='import_statement|import_from_statement', text=wildcard, language='python'),
                    '1': Node(type=wildcard, text=wildcard, language='python')
                },
                edges=[
                    Edge(source='0', destination='1', position=0)
                ]
            ),
            transformations=[
                Delete(nodes=['0', '1'], edges=[('0', '1')], mode='recursive')
            ]
        ),
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '0': Node(type='dictionary', text=wildcard, language='python'),
        #             '1': Node(type='pair', text=wildcard, language='python'),
        #             # '2': Node(type=basic, text=wildcard, language='python'),
        #             # '3': Node(type=wildcard, text=wildcard, language='python')
        #         },
        #         edges=[
        #             Edge(source='0', destination='1', position=0),
        #             # Edge(source='1', destination='2', position=0),
        #             # Edge(source='1', destination='3', position=0),
        #         ]
        #     ),
        #     transformations=[
        #         # ToDictionary(nid='0')
        #         # ToOperator(nid='0', name='dict', language=Where('1', 'language')),
        #         # ToParameter(nid='1', name=Where('2', ''), type=, value=Where('')),
        #         # Add(edges=[('0', '1')]),
        #         Delete(nodes=['1'], edges=[('0', '1')], mode='recursive')
        #     ]
        # ),
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '0': Node(type='list', text=wildcard, language='python'),
        #             '1': Wildcard(language='python'),
        #         },
        #         edges=[
        #             Edge(source='0', destination='1', position=0),
        #         ]
        #     ),
        #     transformations=[
        #         Delete(nodes=['1'], edges=[('0', '1')], mode='recursive')
        #     ]
        # ),
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '1': Node(type='assignment', text=wildcard, language='python'),
        #             '2': Node(type='identifier', text=Variable('X'), language='python'),
        #             '3': Node(type=wildcard, text=wildcard, language='python'),
        #             '4': Node(type='identifier', text=Variable('X'), language='python'),
        #         },
        #         edges=[
        #             Edge(source='1', destination='2', position=0),
        #             Edge(source='1', destination='3', position=0),
        #         ]
        #     ),
        #     transformations=[
        #         Add(edges=[('4', '3')]),
        #         Delete(nodes=['1', '2'])
        #     ]
        # ),
        RewriteRule(
            pattern=DAG(
                nodes={
                    '0': Node(type='identifierdidn', text='sklearn.pipeline.Pipeline',
                                  language='python'),
                    '1': Wildcard(type='Operator', language='python'),
                    '2': Node(type='keyword_argument', text=wildcard, language='python'),
                    '3': Node(type='identifier', text=wildcard, language='python'),
                    '4': Node(type='attribute', text=wildcard, language='python'),
                },
                edges=[
                    Edge(source='0', destination='1', position=0),
                    Edge(source='0', destination='2', position=0),
                    Edge(source='2', destination='3', position=0),
                    Edge(source='2', destination='4', position=0),
                ]
            ),
            transformations=[
                Delete(nodes=['2', '3']),
                ToOperator(nid='4', name=Where('4', 'text'), language=Where('4', 'language')),
                Add(edges=[('1', '4')])
            ]
        ),
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '0': Node(type='call', text=wildcard, language='python'),
        #             '1': Node(type='attribute', text='sklearn.pipeline.Pipeline', language='python'),
        #             '2': Node(type='keyword_argument', text=wildcard, language='python'),
        #             '3': Node(type='identifier', text=wildcard, language='python'),
        #             '4': Node(type='attribute', text=wildcard, language='python'),
        #         },
        #         edges=[
        #             Edge(source='0', destination='1', position=0),
        #             Edge(source='0', destination='2', position=0),
        #             Edge(source='2', destination='3', position=0),
        #             Edge(source='2', destination='4', position=0),
        #         ]
        #     ),
        #     transformations=[
        #         Delete(nodes=['0', '2', '3']),
        #         ToOperator(nid='4', name=Where('4', 'text'), language=Where('4', 'language')),
        #         Add(edges=[('1', '4')])
        #     ]
        # ),
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '0': Node(type='attribute', text='sklearn.pipeline.Pipeline',
        #                           language='python'),
        #             '1': Wildcard(type='Operator', language='python'),
        #         },
        #         edges=[
        #             Edge(source='0', destination='1', position=0),
        #         ]
        #     ),
        #     transformations=[
        #         Delete(nodes=['0']),
        #     ]
        # ),
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '0': Node(type='call', text=wildcard, language='python'),
        #             '1': Node(type='attribute|identifier', text=wildcard, language='python'),
        #         },
        #         edges=[
        #             Edge(source='0', destination='1', position=0),
        #         ]
        #     ),
        #     transformations=[
        #         ToOperator(nid='0', name=Where('1', 'text'), language=Where('1', 'language')),
        #         Delete(nodes=['1'])
        #     ]
        # ),
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '0': Node(type='keyword_argument', text=wildcard, language='python'),
        #             '1': Node(type='identifier', text=wildcard, language='python'),
        #             '2': Wildcard(type='Operator', language='python'),
        #         },
        #         edges=[
        #             Edge(source='0', destination='1', position=0),
        #             Edge(source='0', destination='2', position=0)
        #         ]
        #     ),
        #     transformations=[
        #         ToParameter(nid='0', name=Where('1', 'text'), type=Where('2', 'type'), value=Where('2', 'text')),
        #         Delete(nodes=['1'])
        #     ]
        # ),
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '0': Node(type='keyword_argument', text=wildcard, language='python'),
        #             '1': Node(type='identifier', text=wildcard, language='python'),
        #             '2': Node(type=basic+'|list|attribute', text=wildcard, language='python'),
        #         },
        #         edges=[
        #             Edge(source='0', destination='1', position=0),
        #             Edge(source='0', destination='2', position=0)
        #         ]
        #     ),
        #     transformations=[
        #         ToParameter(nid='0', name=Where('1', 'text'), type=Where('2', 'type'), value=Where('2', 'text')),
        #         Delete(nodes=['1', '2'])
        #     ]
        # ),
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '0': Wildcard(type='Operator', language='python'),
        #             '1': Wildcard(type='Parameter', language='python')
        #         },
        #         edges=[
        #             Edge(source='0', destination='1', position=0),
        #         ]
        #     ),
        #     transformations=[
        #         Flip(edges=[('0', '1')])
        #     ]
        # ),
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '0': Node(type='dictionary', text=wildcard, language='python'),
        #             '1': Node(type='pair', text=wildcard, language='python'),
        #             '2': Node(type=basic, text=wildcard, language='python'),
        #             '3': Node(type=wildcard, text=wildcard, language='python')
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
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '0': Node(type='call', text=wildcard, language='python'),
        #             '1': Node(type='attribute', text=wildcard, language='python'),
        #             '2': Node(type='argument_list', text=wildcard, language='python'),
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
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '0': Node(type='module|expression_statement', text=wildcard, language='python'),
        #         },
        #         edges=[]
        #     ),
        #     transformations=[Delete(nodes=['0'])]
        # ),
    ]


@typeclass
def pprint(instance) -> None:
    """This is a typeclass definition to print tree-like objects"""


@pprint.instance(Tree)
def _pprint_tree(instance: Tree):
    def _foo(n, depth=0):
        print('  '*depth, n.type, n.text)
        for child in n.children:
            _foo(child, depth+1)
    _foo(instance.root_node)


@pprint.instance(Node)
def _pprint_node(instance: Node):
    if instance.type in ['module', 'expression_statement', 'assignment', 'argument_list', 'keyword_argument', 'pattern_list', 'call', 'list']:
        return instance.type
    else:
        return f'{instance.type} {instance.text}'


@pprint.instance(Operator)
def _pprint_operator(instance: Operator):
    return repr(instance)


@pprint.instance(Parameter)
def _pprint_parameter(instance: Parameter):
    return repr(instance)


@pprint.instance(DAG)
def _pprint_dag(instance):
    def _foo(nid, depth=0):
        if nid in instance.nodes:
            print('  '*depth, f'({nid})', pprint(instance.nodes[nid]))
        for edge in instance.edges:
            if edge.source != nid: continue
            _foo(edge.destination, depth+1)

    # print(instance)
    if instance.edges:
        _foo(sorted(instance.nodes, key=int)[0])
    else:
        # In case of bugs, to-be-removed
        print(instance)


def parse(code: str, language: SupportedLanguage):
    parser = create_parser(language)
    b = bytes(code, "utf8")
    tree = parser.parse(b)
    pprint(tree)
    dag = to_dag(tree, language)
    pprint(dag)
    print(dag.nodes, '\n\n', dag.edges)
    rules = get_rules()
    final = transform(dag, rules)
    return final


if __name__ == "__main__":
    from dorian.scripts import scripts
    from time import time

    start = time()
    script = scripts[0]
    print(script)
    final = parse(script, 'python')
    pprint(final)
    print(len(final.nodes), len(final.edges), final.nodes, '\n\n', final.edges)
    print(len(script.split('\n')), time() - start)
