"""Gymnasium MDP environment for RL pipeline generation.

This environment constructs ``dorian.dag.DAG`` instances step-by-step,
placing operators from the KB catalog and wiring them via free-port tracking.
It integrates with the masking engine for action validity and the WL-Nyström
encoder for state representation.

The environment starts from a **frozen evaluation template** — the RL agent
only places operators in the "RL zone" between the train/test split and the
evaluation metric.  The template handles data loading, splitting, and scoring.

MDP formulation
---------------
- **State**: ``concat(wl_nystrom_embedding, metafeature_vector)``
- **Actions**: select an operator from the RL-scoped catalog to place next
- **Masking**: invalid actions are masked out via ``action_masks()``
- **Reward**: 0 during construction; terminal reward is the evaluation
  metric of the completed pipeline (set externally after execution).
- **Termination**: agent selects the special ``__END__`` action or no valid
  actions remain.

On termination, ``info["dag"]`` contains the full DAG (frozen template +
RL-placed operators) ready for compound-operator expansion and execution.

Reference: thesis §4.2 "MDP Formulation".
"""
from __future__ import annotations

import logging
from typing import Any, Sequence
from uuid import uuid4

import numpy as np

from dorian.dag import DAG, Edge, Operator, Parameter
from dorian.pipeline.generation.types import OperatorSpec, PortSpec
from dorian.pipeline.generation.catalog import load_catalog
from dorian.pipeline.generation.masking import MaskingEngine
from dorian.pipeline.generation.encoder import WLNystromEncoder
from dorian.pipeline.generation.eval_template import EvalTemplate, build_eval_template
from dorian.pipeline.generation.param_sampler import sample_parameters

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
END_ACTION = -1  # special action index = terminate pipeline construction


class PipelineGenEnv:
    """Pipeline generation environment (Gymnasium-compatible interface).

    This does **not** inherit from ``gymnasium.Env`` to avoid a hard dependency
    on Gymnasium (which is only needed for PPO training in Phase 4).  The
    interface mirrors Gymnasium's API (``reset``, ``step``, ``action_masks``)
    so it can be trivially wrapped when training is introduced.

    The environment uses an **RL-only catalog** (sklearn transformers and
    estimators only) and starts each episode with a frozen evaluation template
    DAG.  The agent places operators in the RL zone — between ``train_test_split``
    outputs and the evaluation metric input.

    Parameters
    ----------
    task : str or None
        Session data-science task (e.g. "Classification").
    metafeatures : np.ndarray or None
        Dataset metafeature vector (from profiling).
    encoder : WLNystromEncoder or None
        Pre-fitted state encoder.  If None, state is metafeatures only.
    max_steps : int
        Maximum operators per pipeline.
    seed : int or None
        Random seed for reproducibility.
    """

    def __init__(
        self,
        task: str | None = None,
        metafeatures: np.ndarray | None = None,
        encoder: WLNystromEncoder | None = None,
        max_steps: int = 15,
        seed: int | None = None,
    ):
        self.task = task
        self.metafeatures = metafeatures if metafeatures is not None else np.zeros(48, dtype=np.float32)
        self.encoder = encoder
        self.max_steps = max_steps

        # Load RL-scoped catalog (sklearn transformers & estimators only)
        self._catalog: tuple[OperatorSpec, ...] = load_catalog(task, rl_only=True)
        self._masking = MaskingEngine(self._catalog, task=task)

        # Catalog index → OperatorSpec mapping
        # Action i maps to self._catalog[i]; action END_ACTION = terminate
        self.n_actions = len(self._catalog) + 1  # +1 for __END__

        # Build the frozen eval template for this task
        self._template: EvalTemplate = build_eval_template(task=task)

        # Mutable state (reset per episode)
        self._dag: DAG = DAG()
        self._frozen_nodes: frozenset[str] = frozenset()
        self._frozen_edges: frozenset[tuple[str, str]] = frozenset()
        self._free_ports: list[tuple[str, PortSpec]] = []
        self._step_count: int = 0
        self._done: bool = False
        self._errors: list[dict[str, Any]] = []

        # Error-pattern mask populated at reset() time from the failure
        # corpus. Operators with enough repeated failures on the current
        # dataset are removed from the action space so the agent doesn't
        # re-propose them in the same pipeline shape. See
        # dorian/pipeline/generation/error_learning.py.
        self._error_masked_ops: set[str] = set()
        self._error_mask_stats: dict[str, Any] = {}

        # RNG
        import random
        self._rng = random.Random(seed)
        self._np_rng = np.random.RandomState(seed)

    # ------------------------------------------------------------------
    # Gymnasium-compatible API
    # ------------------------------------------------------------------

    def reset(
        self,
        *,
        metafeatures: np.ndarray | None = None,
        task: str | None = None,
        seed: int | None = None,
        error_masked_ops: set[str] | None = None,
        error_mask_stats: dict[str, Any] | None = None,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        """Reset environment for a new episode.

        Initialises the DAG from the frozen evaluation template.  The RL
        agent starts with X_train, X_test, y_train free ports from
        ``train_test_split`` and must wire operators through to the metric.

        ``error_masked_ops`` is the set of operator FQNs the caller has
        decided to remove from the action space for this episode, based
        on recent failure patterns on the target dataset. The caller
        computes this via ``error_learning.invalid_ops_for_dataset``
        and passes the result down; the env just honours it.

        Returns ``(observation, info)``.
        """
        if metafeatures is not None:
            self.metafeatures = metafeatures
        if task is not None and task != self.task:
            self.task = task
            self._catalog = load_catalog(task, rl_only=True)
            self._masking = MaskingEngine(self._catalog, task=task)
            self.n_actions = len(self._catalog) + 1
            self._template = build_eval_template(task=task)
        if seed is not None:
            self._rng = __import__("random").Random(seed)
            self._np_rng = np.random.RandomState(seed)
        self._error_masked_ops = set(error_masked_ops or ())
        self._error_mask_stats = dict(error_mask_stats or {})

        # Deep-copy the template DAG so each episode gets a fresh copy
        import copy
        self._dag = copy.deepcopy(self._template.dag)
        self._frozen_nodes = self._template.frozen_nodes
        self._frozen_edges = self._template.frozen_edges

        # Initialise free ports from the template's RL entry points
        # These are the outputs of train_test_split that the agent wires to
        self._free_ports = [
            (node_id, PortSpec(name=pname, position=ppos, dtype=pdtype))
            for node_id, (pname, ppos, pdtype) in self._template.rl_entry_ports
        ]

        self._step_count = 0
        self._done = False
        self._errors = []

        obs = self._observe()
        info = {
            "dag": self._dag,
            "valid_actions": self._get_valid_action_indices(),
            "frozen_nodes": self._frozen_nodes,
            "template": self._template,
        }
        return obs, info

    def step(self, action: int) -> tuple[np.ndarray, float, bool, bool, dict[str, Any]]:
        """Execute one action (place an operator or terminate).

        Returns ``(observation, reward, terminated, truncated, info)``.

        Reward is always 0.0 during construction.  The terminal reward is
        set externally after execution (stored in episodic memory).
        """
        if self._done:
            raise RuntimeError("Episode is done — call reset() first.")

        terminated = False
        truncated = False
        reward = 0.0

        if action == self.n_actions - 1:
            # __END__ action — terminate if pipeline is valid
            if self._masking.can_terminate(self._dag):
                self._wire_metric_input()
                terminated = True
                self._done = True
            else:
                # Can't terminate yet — treat as no-op with penalty
                reward = -0.1
        elif 0 <= action < len(self._catalog):
            op_spec = self._catalog[action]
            # Verify action is valid
            valid_ops = self._masking.valid_operators(self._dag, self._free_ports)
            if op_spec in valid_ops:
                try:
                    self._place_operator(op_spec)
                    self._step_count += 1
                except Exception as exc:
                    self._errors.append({
                        "step": self._step_count,
                        "action": action,
                        "operator": op_spec.name,
                        "error": str(exc),
                        "type": "placement_error",
                    })
                    _log.warning(
                        "Operator placement failed for %s at step %d: %s",
                        op_spec.name, self._step_count, exc,
                    )
                    reward = -0.1
            else:
                # Invalid action — penalty
                reward = -0.1
        else:
            # Out-of-range action
            reward = -0.1

        # Check truncation (max steps reached)
        if self._step_count >= self.max_steps and not self._done:
            truncated = True
            self._done = True
            self._errors.append({
                "step": self._step_count,
                "type": "truncated",
                "detail": f"Max steps ({self.max_steps}) reached without termination.",
            })

        # Check if must terminate (no valid actions left)
        if not self._done:
            valid = self._get_valid_action_indices()
            if not valid or (len(valid) == 1 and valid[0] == self.n_actions - 1
                             and self._masking.must_terminate(self._dag)):
                self._wire_metric_input()
                terminated = True
                self._done = True

        obs = self._observe()
        info = {
            "dag": self._dag,
            "step": self._step_count,
            "valid_actions": self._get_valid_action_indices() if not self._done else [],
            "frozen_nodes": self._frozen_nodes,
            "errors": list(self._errors),
        }

        return obs, reward, terminated, truncated, info

    def _wire_metric_input(self) -> None:
        """Wire the last estimator's predictions to the metric node.

        On termination, finds the free port carrying "predictions" and wires
        it to the metric's y_pred input.  If no predictions port exists (e.g.
        only transformers placed), records an error.
        """
        target = self._template.rl_exit_target
        metric_input = target["input_port"]
        # Multi-metric templates expose every metric in node_ids;
        # legacy single-metric callers still set node_id. Fan out
        # to every metric so the agent's predictions land in all
        # parallel scoring nodes.
        metric_ids = target.get("node_ids") or [target.get("node_id")]

        # Find a free port with predictions dtype
        pred_port = None
        for src_id, port in self._free_ports:
            if port.dtype == "predictions":
                pred_port = (src_id, port)
                break

        if pred_port is None:
            # Fall back: use the last free port with features dtype
            # (some pipelines only have transformers → pass features to metric)
            for src_id, port in reversed(self._free_ports):
                if port.dtype in ("features", "any"):
                    pred_port = (src_id, port)
                    break

        if pred_port is not None:
            src_id, port = pred_port
            for metric_id in metric_ids:
                if not metric_id:
                    continue
                self._dag.edges.append(Edge(
                    source=src_id,
                    destination=metric_id,
                    position=metric_input["position"],
                    output=port.position,
                ))
        else:
            self._errors.append({
                "step": self._step_count,
                "type": "wiring_error",
                "detail": "No suitable output port found to wire to metric input.",
            })

    def action_masks(self) -> np.ndarray:
        """Boolean mask over the action space.  True = valid action.

        Shape: ``(n_actions,)`` where the last element is the __END__ action.

        Two validity layers:
          1. **Structural** (``_masking.valid_operators``) — the operator's
             I/O is wireable against the current free-port pool.
          2. **Experience** (``_error_masked_ops``) — the operator has
             repeatedly failed on this dataset in the recent past.
             Populated at ``reset()`` time by the caller from the
             execution-error corpus (see ``error_learning.py``).
        """
        mask = np.zeros(self.n_actions, dtype=bool)

        if self._done:
            return mask

        valid_ops = self._masking.valid_operators(self._dag, self._free_ports)
        valid_names = {op.name for op in valid_ops}

        for i, op in enumerate(self._catalog):
            if op.name in valid_names and op.name not in self._error_masked_ops:
                mask[i] = True

        # __END__ is valid only if pipeline can terminate
        if self._masking.can_terminate(self._dag):
            mask[-1] = True

        return mask

    # ------------------------------------------------------------------
    # Internal: operator placement
    # ------------------------------------------------------------------

    def _place_operator(self, op_spec: OperatorSpec) -> None:
        """Add an operator to the DAG with sampled parameters and edge wiring."""
        node_id = uuid4().hex[:12]

        # Create the Operator node
        operator = Operator(name=op_spec.name, language="python")
        self._dag.nodes[node_id] = operator

        # Sample and attach parameters
        param_values = sample_parameters(op_spec, rng=self._rng)
        for pname, pvalue in param_values.items():
            pid = uuid4().hex[:12]
            dtype = self._infer_dtype(pvalue)
            self._dag.nodes[pid] = Parameter(name=pname, dtype=dtype, value=str(pvalue))
            self._dag.edges.append(Edge(
                source=pid,
                destination=node_id,
                position=pname,  # keyword argument
                output=0,
            ))

        # Wire input edges from free ports. Standard consume-and-replace
        # semantics: each wired port is removed from the free pool;
        # downstream consumers use the operator's output ports instead.
        #
        # This means any operator that consumes a data flow (features or
        # labels) AND has downstream consumers of the SAME flow must
        # declare a matching output port — otherwise the flow is lost.
        # Feature selectors like SelectKBest consume y but don't
        # transform it; their catalog entry must declare y as a
        # passthrough output so the downstream classifier can still
        # wire fit.y. See dorian/pipeline/generation/catalog.py.
        consumed_ports: list[int] = []
        for inp in op_spec.inputs:
            # Find a compatible free port
            for idx, (src_id, src_port) in enumerate(self._free_ports):
                if idx in consumed_ports:
                    continue
                if self._ports_compatible(src_port, inp):
                    self._dag.edges.append(Edge(
                        source=src_id,
                        destination=node_id,
                        position=inp.position,
                        output=src_port.position,
                    ))
                    consumed_ports.append(idx)
                    break

        # Remove consumed free ports (reverse order to preserve indices)
        for idx in sorted(consumed_ports, reverse=True):
            self._free_ports.pop(idx)

        # Register new free output ports
        for out_port in op_spec.outputs:
            self._free_ports.append((node_id, out_port))

    @staticmethod
    def _ports_compatible(src: PortSpec, dst: PortSpec) -> bool:
        """Check if an output port can connect to an input port."""
        if src.dtype == "any" or dst.dtype == "any":
            return True
        return src.dtype == dst.dtype

    @staticmethod
    def _infer_dtype(value: Any) -> str:
        """Infer Dorian's SupportedType from a Python value."""
        if isinstance(value, bool):
            return "eval"
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "float"
        if value is None:
            return "eval"
        return "string"

    # ------------------------------------------------------------------
    # Internal: observation
    # ------------------------------------------------------------------

    def _observe(self) -> np.ndarray:
        """Compute the current state vector.

        State = concat(dag_embedding, metafeatures).
        """
        if self.encoder is not None and self.encoder.is_fitted:
            dag_emb = self.encoder.encode(self._dag)
        else:
            # Zero vector when encoder not available
            n_dag = self.encoder.n_components if self.encoder else 64
            dag_emb = np.zeros(n_dag, dtype=np.float32)

        return np.concatenate([dag_emb, self.metafeatures]).astype(np.float32)

    def _get_valid_action_indices(self) -> list[int]:
        """Return indices of valid actions in the current state."""
        mask = self.action_masks()
        return [i for i in range(self.n_actions) if mask[i]]

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @property
    def observation_dim(self) -> int:
        """Dimensionality of the observation vector."""
        n_dag = self.encoder.output_dim if self.encoder and self.encoder.is_fitted else 64
        return n_dag + len(self.metafeatures)

    @property
    def current_dag(self) -> DAG:
        """Current (partial or complete) pipeline DAG."""
        return self._dag

    @property
    def is_done(self) -> bool:
        return self._done

    @property
    def errors(self) -> list[dict[str, Any]]:
        """Errors accumulated during the current episode."""
        return list(self._errors)
