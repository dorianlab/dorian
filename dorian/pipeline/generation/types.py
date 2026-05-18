"""Generation engine type definitions.

Thin frozen dataclasses that carry operator metadata resolved from the KB
at runtime.  No hardcoded catalogs here — all operator data flows through
``catalog.load_catalog()``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Port specification (input / output slots)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PortSpec:
    """One input or output port of an operator."""
    name: str                # e.g. "X", "y", "messages"
    position: int | str      # positional index (0-based) or kwarg name (e.g. "messages")
    dtype: str = "any"       # semantic type hint — "features", "labels", "list[dict]", "any"


# ---------------------------------------------------------------------------
# Parameter specification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParameterSpec:
    """Describes a tunable hyperparameter with its domain."""
    name: str
    dtype: str              # "int" | "float" | "categorical" | "bool"
    default: Any = None
    low: float | None = None    # inclusive lower bound (int / float)
    high: float | None = None   # inclusive upper bound (int / float)
    log_scale: bool = False     # sample on log scale
    choices: tuple | None = None  # frozen iterable for categorical params


# ---------------------------------------------------------------------------
# Operator specification (resolved from KB)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OperatorSpec:
    """Full operator metadata assembled from the KB.

    The generation engine works exclusively through ``OperatorSpec`` instances
    returned by ``catalog.load_catalog()``.  When the KB gains a new operator
    (with proper ``performs``, ``implements``, and interface annotations), the
    catalog picks it up automatically.
    """
    name: str                                       # FQN, e.g. "sklearn.svm.SVC"
    interface: str                                  # "Sklearn Transformer" | "Sklearn Estimator" | "Function"
    tasks: tuple[str, ...] = ()                     # from KB ``performs`` relationships
    family: str | None = None                       # from KB ``implements`` relationship
    inputs: tuple[PortSpec, ...] = ()               # interface-level I/O
    outputs: tuple[PortSpec, ...] = ()
    parameters: tuple[ParameterSpec, ...] = ()      # from KB chain annotations
    visibility: str = "default"                     # "default" | "secondary" | "hidden"

    @property
    def is_estimator(self) -> bool:
        return self.interface == "Sklearn Estimator"

    @property
    def is_transformer(self) -> bool:
        return self.interface == "Sklearn Transformer"

    @property
    def is_function(self) -> bool:
        return self.interface == "Function"

    @property
    def is_llm(self) -> bool:
        return self.interface == "LLM Chat Completion"


# ---------------------------------------------------------------------------
# Action (operator placement in DAG construction)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Action:
    """An action in the MDP: place an operator and bind its input ports."""
    operator: OperatorSpec
    # Mapping from input port position → (source_node_id, source_output_port)
    bindings: tuple[tuple[str, int], ...] = ()
