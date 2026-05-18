"""Constraint-based action masking for pipeline generation.

All constraints are derived from KB metadata (interface, task, family) — no
hardcoded operator lists.  When the KB adds a new operator with proper
annotations, the masking engine handles it automatically.

Constraint rules
----------------
1. **I/O compatibility**: operator input port dtypes must match available
   free output port dtypes (features → features, labels → labels, etc.).
2. **Task compatibility**: only operators whose ``performs`` set includes the
   session's selected task (or a generic preprocessing task) are allowed.
3. **Interface ordering**: transformers must appear before the first estimator;
   at most one estimator per pipeline (it's always the terminal model).
4. **Family cap**: at most one operator from each algorithmic family in
   estimator families (Ensemble, Naive Bayes, etc.) per pipeline.
5. **Self-exclusion**: an operator that's already in the DAG cannot be
   added again.
6. **Arity satisfaction**: all required input ports of the candidate must
   be satisfiable from the current set of free output ports.
"""
from __future__ import annotations

from typing import Sequence

from dorian.dag import DAG, Operator
from dorian.pipeline.generation.types import OperatorSpec, PortSpec

# Tasks that are universally applicable (preprocessing / utility operators
# can appear in any pipeline regardless of the selected DS task).
_UNIVERSAL_TASKS = frozenset({
    "Data Preprocessing",
    "Data Normalization",
    "Data Encoding",
    "Missing Data Imputation",
    "Dimensionality Reduction",
    "Feature Engineering",
    "Feature Selection",
    "Model Selection",
    "Model Evaluation",
    "Data Loading",
    "Data Cleaning",
    "Data Transformation",
    "Feature Extraction",
})


class MaskingEngine:
    """Computes valid actions for each DAG construction step.

    Parameters
    ----------
    catalog : sequence of OperatorSpec
        Full or task-filtered operator catalog.
    task : str or None
        The session's selected data-science task (e.g. "Classification").
        When set, only operators that perform this task or a universal
        preprocessing task are allowed.
    max_estimators : int
        Maximum number of estimators (terminal models) per pipeline.
    max_transformers : int
        Maximum number of transformer steps per pipeline.
    """

    def __init__(
        self,
        catalog: Sequence[OperatorSpec],
        task: str | None = None,
        max_estimators: int = 1,
        max_transformers: int = 6,
    ):
        self.catalog = tuple(catalog)
        self.task = task
        self.max_estimators = max_estimators
        self.max_transformers = max_transformers

        # Pre-partition catalog for fast lookup
        self._estimators = [op for op in self.catalog if op.is_estimator]
        self._transformers = [op for op in self.catalog if op.is_transformer]
        self._functions = [op for op in self.catalog if op.is_function]

    def valid_operators(
        self,
        dag: DAG,
        free_ports: list[tuple[str, PortSpec]],
    ) -> list[OperatorSpec]:
        """Return the subset of catalog operators that can legally be added.

        Parameters
        ----------
        dag : DAG
            Current partially-built pipeline.
        free_ports : list of (node_id, PortSpec)
            Output ports that haven't been consumed by any downstream node yet.
        """
        # Analyse current DAG state
        current_operators = self._extract_operators(dag)
        current_names = {op.name for op in current_operators}
        current_families = {op.family for op in current_operators if op.family}

        n_estimators = sum(1 for op in current_operators if op.is_estimator)
        n_transformers = sum(1 for op in current_operators if op.is_transformer)
        has_estimator = n_estimators > 0

        # Available output port types for binding
        available_dtypes = {port.dtype for _, port in free_ports}

        valid: list[OperatorSpec] = []
        for op in self.catalog:
            # Rule 5: self-exclusion — no duplicate operators
            if op.name in current_names:
                continue

            # Rule 2: task compatibility
            if not self._task_compatible(op):
                continue

            # Rule 3: interface ordering — no transformer after estimator
            if op.is_transformer and has_estimator:
                continue

            # Rule 3: estimator cap
            if op.is_estimator and n_estimators >= self.max_estimators:
                continue

            # Transformer cap
            if op.is_transformer and n_transformers >= self.max_transformers:
                continue

            # Rule 4: family cap (for estimator families only)
            if op.is_estimator and op.family and op.family in current_families:
                continue

            # Rule 6: arity satisfaction — all required inputs satisfiable
            if not self._inputs_satisfiable(op, available_dtypes):
                continue

            # Rule 1: I/O compatibility — at least one input matches available ports
            if not self._io_compatible(op, free_ports):
                continue

            valid.append(op)

        return valid

    def can_terminate(self, dag: DAG) -> bool:
        """Whether the current DAG is a valid complete pipeline.

        A pipeline is considered complete if it contains at least one estimator.
        """
        operators = self._extract_operators(dag)
        return any(op.is_estimator for op in operators)

    def must_terminate(self, dag: DAG) -> bool:
        """Whether no more operators can be added (hard limit reached)."""
        operators = self._extract_operators(dag)
        n_est = sum(1 for op in operators if op.is_estimator)
        return n_est >= self.max_estimators

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _task_compatible(self, op: OperatorSpec) -> bool:
        """Check if operator's tasks overlap with the session task or universals."""
        if not self.task:
            return True  # no task filter active
        op_tasks = set(op.tasks)
        if not op_tasks:
            return True  # no task annotation → treat as generic
        # Operator is valid if it performs the session task or any universal task
        return bool(op_tasks & ({self.task} | _UNIVERSAL_TASKS))

    @staticmethod
    def _inputs_satisfiable(
        op: OperatorSpec,
        available_dtypes: set[str],
    ) -> bool:
        """Check if all required input ports can be served."""
        for port in op.inputs:
            if port.dtype == "any":
                continue
            # "any" available dtype can serve any required port
            if "any" in available_dtypes:
                continue
            if port.dtype not in available_dtypes:
                return False
        return True

    @staticmethod
    def _io_compatible(
        op: OperatorSpec,
        free_ports: list[tuple[str, PortSpec]],
    ) -> bool:
        """Check that at least one input port can bind to a free output port."""
        if not op.inputs:
            return True
        if not free_ports:
            return False

        for inp in op.inputs:
            for _, out_port in free_ports:
                if inp.dtype == "any" or out_port.dtype == "any" or inp.dtype == out_port.dtype:
                    return True
        return False

    def _extract_operators(self, dag: DAG) -> list[OperatorSpec]:
        """Extract OperatorSpec instances for operators already in the DAG."""
        specs = []
        catalog_by_name = {op.name: op for op in self.catalog}
        for node in dag.nodes.values():
            if isinstance(node, Operator) and node.name in catalog_by_name:
                specs.append(catalog_by_name[node.name])
        return specs
