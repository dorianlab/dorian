from dataclasses import dataclass
from collections import namedtuple
from typing import Dict, Sequence, Any, Literal
from pathlib import Path
import re


EdgeType = Literal['IsA', 'IsSubclassOf', 'Implements', 'IsEquivalentTo', 'HasParameter']

def to_camel(expr: str) -> str:
    # return re.sub(r'\s(\w)', lambda x: x[1].upper(), expr.title())
    return expr.replace(' ', '')


Statement = namedtuple('Statement', 'subject predicate object')
camel_case = re.compile(r'(?<!^)(?=[A-Z])')
knowledge = """
Data Science Task is a Concept
Data Preprocessing is subclass of Data Science Task
Supervised Learning is subclass of Data Science Task
Unsupervised Learning is subclass of Data Science Task
Binary Classification is subclass of Supervised Learning
Regression is subclass of Data Science Task
Clustering is subclass of Unsupervised Learning
Data Collection is subclass of Data Science Task
Data Cleaning is subclass of Data Preprocessing
Data Augmentation  is subclass of Data Preprocessing
Data Integration is subclass of Data Preprocessing
Data Exploration is subclass of Data Science Task
Data Visualization is subclass of Data Science Task
Statistical Analysis is subclass of Data Science Task
Natural Language Processing is subclass of Data Science Task
Image Analysis is subclass of Data Science Task
Video Analysis is subclass of Data Science Task
Time Series Analysis is subclass of Data Science Task
Anomaly Detection is subclass of Data Science Task
Fraud Detection is subclass of Anomaly Detection
Network Intrusion Detection is subclass of Anomaly Detection
"""


class UnknownRelation(Exception):
    pass


def parse(statement: str) -> Statement:
    edges = [camel_case.sub(r' ', edge.name).lower() for edge in EdgeType]
    for edge in edges:
        if edge in statement:
            s, o = statement.split(edge)
            return Statement(subject=s.strip(), predicate=edge, object=o.strip())
    raise UnknownRelation(statement)


@dataclass
class Node:
    type: str
    attr: Dict[str, Any] | None = None


@dataclass
class Edge:
    source: str
    destination: str
    type: EdgeType
    attr: Dict[str, Any] | None = None


@dataclass
class Ontology:
    nodes: Sequence[Node]
    edges: Sequence[Edge]


def get_ontology() -> Ontology:
    stmts = [parse(line) for line in knowledge.strip().split('\n')]
    return Ontology(
        nodes=list(map(lambda el: Node(type=el.subject), stmts)),
        edges=list(map(lambda el: Edge(source=el.subject,
                                       destination=el.object,
                                       type=EdgeType(to_camel(el.predicate))
                                       ), stmts))
    )


if __name__ == "__main__":
    print(get_ontology())
