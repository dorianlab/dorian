"""Operator-catalog schema for the v2 RL module.

The catalog is the single source of truth every RL component reads:
the action-masking system filters by type/arity/task tags, the state
encoder hashes node metadata into WL base labels, the execution
bridge consults ``is_deterministic`` + ``operator_version`` to drive
cache eligibility, and the drift-aware fine-tuner reads ``is_new``
to decide whether to bias the policy logits.

Dorian already has a richer KB in Neo4j (``dorian/knowledge/``); the
catalog here is a Python-side view that augments KB data with the
v2-specific fields listed in (internal design note; not in public repo) section
"2. Operator Catalog".

Loaders that hydrate this schema live next to it
(``rl/catalog/loader.py``); this module defines only the dataclasses
and enums so schema drift is caught early by static analysis.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


TaskTag = Literal["classification", "regression", "clustering"]
OperatorFamily = Literal[
    "loader", "scaler", "encoder", "imputer", "selector",
    "transformer", "estimator", "metric", "snippet",
    "splitter", "llm", "guardrail",
]


class DeterminismClass(str, Enum):
    """Mirrors ``graph::dem::DeterminismClass``. Gates cache eligibility
    end-to-end: the Rust scheduler reads the annotation in the
    ``DemAnnotations`` sidecar, the RL trainer reads it here."""

    DETERMINISTIC = "deterministic"
    NON_DETERMINISTIC = "non_deterministic"
    UNKNOWN = "unknown"


class DomainKind(str, Enum):
    """Mirrors ``graph::dem::DomainKind``. v2 ships SDF + DE. Today
    every sklearn/pandas op is SDF; DE is reserved for async triggers
    (cancel, mitigation_rewrite, etc.)."""

    SDF = "sdf"
    DE = "de"


@dataclass(frozen=True)
class PortSpec:
    """Typed port in an operator's schema.

    ``variadic=True`` marks an input port that accepts an
    unbounded list of upstream outputs. The mask emits one
    AddEdge candidate per fresh positional slot (``"0"``, ``"1"``,
    ``"2"``, ...) up to ``variadic_cap``; the executor's operator
    substitution collects those positional args into a Python list
    and hands it to the underlying class (e.g. sklearn's
    ``VotingClassifier(estimators=[...])``). This is how
    multi-voter-composed models are expressed without hardcoding
    a specific arity on the operator.
    """

    name: str
    type_hint: str  # "DataFrame", "Array", "Model", "any", ...
    required: bool = True
    variadic: bool = False
    variadic_cap: int = 5
    # Optional semantic identity used by the suggestion layer.
    # ``name`` is how the env's mask / executor address the port
    # (often a positional alias like ``"0"`` / ``"1"``); ``semantic_name``
    # is the "what this port actually is" label (``"y_pred"``,
    # ``"y_true"``). Edge-matching ranks higher when src and dst
    # share the same semantic_name. Defaults to ``name`` when not
    # set, so ports whose name IS already semantic (``"X_train"``,
    # ``"y_pred"``) get matching for free.
    semantic_name: str | None = None


@dataclass(frozen=True)
class ParameterSpec:
    """Hyperparameter binding declared on the operator."""

    name: str
    dtype: Literal["int", "float", "string", "bool", "eval", "env"]
    default: str | None = None
    low: float | None = None
    high: float | None = None
    choices: tuple[str, ...] = ()
    log_scale: bool = False


@dataclass(frozen=True)
class OperatorMeta:
    """Unified catalog entry. Atomic + composite operators share the
    schema so masking, state encoding, and execution all see the same
    shape.

    See internal design note section "2. Operator Catalog" for the
    v2 additions beyond the thesis catalog.
    """

    # Stable identifier — matches dorian operator FQN when atomic,
    # synthesized "composite::<hash>" when mined.
    op_key: str
    family: OperatorFamily
    task_tags: tuple[TaskTag, ...]

    inputs: tuple[PortSpec, ...] = ()
    outputs: tuple[PortSpec, ...] = ()
    parameters: tuple[ParameterSpec, ...] = ()

    # --- DEM / cache integration ---
    domain: DomainKind = DomainKind.SDF
    determinism: DeterminismClass = DeterminismClass.UNKNOWN
    operator_version: str | None = None
    random_state_param_name: str | None = None
    is_warmstartable: bool = False

    # --- Drift-aware fine-tuning (thesis section 4.10) ---
    is_new: bool = False
    is_composite: bool = False

    # --- Exclusivity / arity global constraints ---
    exclusivity_group: str | None = None
    max_occurrence: int | None = None

    # --- Metadata echoed into WL base labels ---
    extra_tags: frozenset[str] = field(default_factory=frozenset)

    # -----------------------------------------------------------------
    # Convenience predicates
    # -----------------------------------------------------------------
    @property
    def is_deterministic(self) -> bool:
        return self.determinism == DeterminismClass.DETERMINISTIC

    @property
    def is_cacheable(self) -> bool:
        return self.is_deterministic and self.operator_version is not None

    def supports_task(self, task: TaskTag) -> bool:
        return task in self.task_tags or not self.task_tags

    def requires_input(self, type_hint: str) -> bool:
        return any(
            p.type_hint == type_hint and p.required for p in self.inputs
        )


# Convenience: a tiny default catalog covering the operators used by
# the sample pipelines. A full KB-backed loader replaces these with
# the Neo4j-derived versions; this seed lets the RL env bring-up
# proceed before the loader lands.
DEFAULT_CATALOG_SEED: tuple[OperatorMeta, ...] = (
    OperatorMeta(
        op_key="pandas.read_csv",
        family="loader",
        task_tags=("classification", "regression", "clustering"),
        inputs=(PortSpec("fpath", "string"),),
        outputs=(PortSpec("X", "DataFrame"),),
        parameters=(),
        domain=DomainKind.SDF,
        determinism=DeterminismClass.DETERMINISTIC,
        operator_version="2.3.3",
        max_occurrence=1,
    ),
    OperatorMeta(
        op_key="sklearn.preprocessing.StandardScaler",
        family="scaler",
        task_tags=("classification", "regression"),
        inputs=(PortSpec("X", "DataFrame"),),
        outputs=(PortSpec("X", "DataFrame"),),
        parameters=(
            ParameterSpec("with_mean", "bool", default="True"),
            ParameterSpec("with_std", "bool", default="True"),
        ),
        domain=DomainKind.SDF,
        determinism=DeterminismClass.DETERMINISTIC,
        operator_version="1.7.2",
        exclusivity_group="scaler",
    ),
    OperatorMeta(
        op_key="sklearn.model_selection.train_test_split",
        family="splitter",
        task_tags=("classification", "regression"),
        inputs=(PortSpec("X", "DataFrame"), PortSpec("y", "Array")),
        outputs=(
            PortSpec("X_train", "DataFrame"),
            PortSpec("X_test", "DataFrame"),
            PortSpec("y_train", "Array"),
            PortSpec("y_test", "Array"),
        ),
        parameters=(
            ParameterSpec("random_state", "int", default="42"),
            ParameterSpec(
                "test_size", "float", default="0.2", low=0.1, high=0.5
            ),
        ),
        domain=DomainKind.SDF,
        determinism=DeterminismClass.DETERMINISTIC,
        operator_version="1.7.2",
        random_state_param_name="random_state",
        max_occurrence=1,
    ),
    OperatorMeta(
        op_key="openrouter.chat.completion",
        family="llm",
        task_tags=("classification", "regression", "clustering"),
        inputs=(PortSpec("messages", "any"),),
        outputs=(PortSpec("response", "any"),),
        parameters=(
            ParameterSpec("model", "string", default="openai/gpt-4"),
            ParameterSpec(
                "temperature", "float", default="1.0", low=0.0, high=2.0
            ),
        ),
        domain=DomainKind.SDF,
        determinism=DeterminismClass.NON_DETERMINISTIC,
        operator_version=None,  # LLMs drift continuously — no op_version
    ),
)
