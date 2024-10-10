from tree_sitter import Tree
from dataclasses import dataclass
from classes import typeclass
from typing import Dict, Sequence, Literal, List

from dorian.languages import SupportedLanguage


ID = str
Positional = int | list
Keyword = str

SupportedType = Literal['int', 'float', 'string']


@dataclass
class Variable:
    name: str
    

@dataclass
class Snippet:
    code: str
    language: SupportedLanguage

    def __call__(self, *args, **kwargs):
        res = {}
        exec(self.code, globals(), res)
        return res['foo'](*args, **kwargs)


@dataclass
class Parameter:
    name: str
    type: SupportedType
    value: str

    def __hash__(self) -> int:
        return hash(f'{self.name}:{self.type}:{self.value}')

    def __call__(self, *args, **kwargs):
        return eval(self.type)(self.value)
    

@dataclass
class Node:
    type: str
    text: str
    language: SupportedLanguage


@dataclass
class Edge:
    source: ID
    destination: ID
    position: Positional | Keyword
    output: Positional = 0

    def __hash__(self) -> int:
        return hash(f'{self.source}:{self.output}+{self.destination}:{self.position}')


@dataclass
class DAG:
    nodes: Dict[ID, Node]
    edges: Sequence[Edge]

    def __iter__(self):
        yield from self.nodes.items()

    def __len__(self):
        return len(self.nodes.keys())


@typeclass
def to_dag(instance, language: SupportedLanguage) -> DAG:
    """This is a typeclass definition to convert objects to DAGs"""


@to_dag.instance(Tree)
def __to_dag(instance: Tree, language: SupportedLanguage) -> DAG:
    _nodes, _edges = {}, []
    # _id = lambda n: "-".join(map(str, n.start_point))
    def _foo(n, parent=None):
        _id = len(_nodes)
        if parent is not None:
            _edges.append(Edge(str(parent), str(_id), position=0))
        _nodes[str(_id)] = Node(type=n.type, text=n.text, language=language)
        for child in n.children:
            _foo(child, _id)
    _foo(instance.root_node)
    return DAG(nodes=_nodes, edges=_edges)