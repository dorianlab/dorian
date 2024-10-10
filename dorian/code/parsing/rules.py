from dataclasses import dataclass
from typing import Literal, Sequence, Optional, Tuple, Callable
from aenum import Enum

from dorian.languages import SupportedLanguage
from dorian.dag import DAG, ID


class Priority(Enum):
    Low = 0
    Medium = 50
    High = 100

    def __repr__(self):
        return self.name


PurgeMode = Literal['recursive', 'isolated']
Pattern = DAG


@dataclass
class Add:
    nodes: Sequence[ID] | None = None
    edges: Sequence[Tuple[ID, ID]] | None = None


@dataclass
class Apply:
    f: Callable[[DAG], DAG]


@dataclass
class Replace:
    pass


@dataclass
class Delete:
    nodes: Sequence[ID] | None = None
    edges: Sequence[Tuple[ID, ID]] | None = None
    mode: PurgeMode = 'isolated'


Transformation = Apply | Replace | Delete


@dataclass
class RewriteRule:
    pattern: Pattern
    transformations: Sequence[Transformation] | None = None
    priority: Priority = Priority.Low
