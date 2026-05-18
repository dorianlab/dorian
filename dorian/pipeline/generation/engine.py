"""GenerationEngine — runs RL episodes to produce pipeline candidates.

Three inference modes (from thesis §4.3):

  - ``model_free``    — uniform random valid-action sampling (cold start)
  - ``model_guided``  — action probabilities from a trained policy (future)
  - ``blended``       — epsilon-greedy between random and policy (future)

The engine produces complete DAGs by stepping through PipelineGenEnv until
termination, then hands them to the executor for persistence and submission.

Usage::

    engine = GenerationEngine(task="Classification")
    dag = engine.generate_one()               # single pipeline
    dags = engine.generate_batch(n=10)         # batch

    # With live execution:
    await engine.generate_and_submit(
        dataset_id="abc123",
        n=10,
        session="rl:batch-1",
    )
"""
from __future__ import annotations

import logging
import random
from typing import Any

import numpy as np

from dorian.dag import DAG
from dorian.pipeline.generation.environment import PipelineGenEnv
from dorian.pipeline.generation.encoder import WLNystromEncoder

_log = logging.getLogger(__name__)


class GenerationEngine:
    """Template-free pipeline generator using RL environment.

    Parameters
    ----------
    task : str or None
        Data science task to constrain the operator catalog.
    metafeatures : np.ndarray or None
        Dataset metafeature vector (from profiling).
    encoder : WLNystromEncoder or None
        Pre-fitted WL-Nyström encoder for state embeddings.
    max_steps : int
        Maximum operators per generated pipeline.
    seed : int or None
        Base seed for reproducibility.  Each episode uses ``seed + episode_idx``.
    mode : str
        Inference mode: "model_free" (random), "model_guided", "blended".
    """

    def __init__(
        self,
        task: str | None = None,
        metafeatures: np.ndarray | None = None,
        encoder: WLNystromEncoder | None = None,
        max_steps: int = 15,
        seed: int | None = None,
        mode: str = "model_free",
    ):
        self.task = task
        self.metafeatures = metafeatures
        self.max_steps = max_steps
        self.base_seed = seed
        self.mode = mode

        self._env = PipelineGenEnv(
            task=task,
            metafeatures=metafeatures,
            encoder=encoder,
            max_steps=max_steps,
            seed=seed,
        )
        self._episode_count = 0
        self._rng = random.Random(seed)

    # ------------------------------------------------------------------
    # Single episode
    # ------------------------------------------------------------------

    def generate_one(
        self,
        *,
        metafeatures: np.ndarray | None = None,
        max_attempts: int = 5,
        error_masked_ops: set[str] | None = None,
        error_mask_stats: dict | None = None,
    ) -> tuple[DAG | None, list[dict]]:
        """Generate a single valid pipeline DAG.

        Retries up to ``max_attempts`` times if the episode fails to produce
        a valid pipeline (e.g. truncated with no estimator).

        ``error_masked_ops`` is the dataset-specific hard mask derived
        from the failure corpus (see
        ``dorian/pipeline/generation/error_learning.py``). Pre-computed
        by ``generate_and_submit`` for the whole batch so we don't run
        the docstore query once per attempt.

        Returns ``(dag, errors)`` — dag is None if all attempts fail.
        Errors are accumulated across all attempts as first-class data for
        future analysis and mitigation.
        """
        all_errors: list[dict] = []

        for attempt in range(max_attempts):
            episode_seed = (
                self.base_seed + self._episode_count
                if self.base_seed is not None
                else None
            )
            self._episode_count += 1

            obs, info = self._env.reset(
                metafeatures=metafeatures,
                seed=episode_seed,
                error_masked_ops=error_masked_ops,
                error_mask_stats=error_mask_stats,
            )

            terminated = False
            truncated = False
            while not self._env.is_done:
                action = self._select_action(info)
                if action is None:
                    break
                obs, reward, terminated, truncated, info = self._env.step(action)

            episode_errors = info.get("errors", [])
            if episode_errors:
                for err in episode_errors:
                    err["episode"] = self._episode_count
                    err["attempt"] = attempt + 1
                all_errors.extend(episode_errors)

            if terminated:
                dag = info.get("dag")
                if dag and len(dag.nodes) > 0:
                    return dag, all_errors

            _log.debug(
                "Episode %d attempt %d: no valid pipeline produced (truncated=%s).",
                self._episode_count, attempt + 1, truncated,
            )
            all_errors.append({
                "episode": self._episode_count,
                "attempt": attempt + 1,
                "type": "generation_failed",
                "truncated": truncated,
                "detail": "No valid pipeline produced.",
            })

        return None, all_errors

    # ------------------------------------------------------------------
    # Batch generation
    # ------------------------------------------------------------------

    def generate_batch(
        self,
        n: int = 10,
        *,
        metafeatures: np.ndarray | None = None,
        max_attempts_per: int = 5,
        error_masked_ops: set[str] | None = None,
        error_mask_stats: dict | None = None,
    ) -> tuple[list[DAG], list[dict]]:
        """Generate up to ``n`` valid pipelines.

        Returns ``(dags, all_errors)`` — dags may be shorter than ``n`` if
        some episodes fail.  Errors are accumulated for persistence.
        """
        dags: list[DAG] = []
        batch_errors: list[dict] = []
        for i in range(n):
            dag, errors = self.generate_one(
                metafeatures=metafeatures,
                max_attempts=max_attempts_per,
                error_masked_ops=error_masked_ops,
                error_mask_stats=error_mask_stats,
            )
            if errors:
                for err in errors:
                    err["batch_index"] = i
                batch_errors.extend(errors)
            if dag is not None:
                dags.append(dag)
        return dags, batch_errors

    # ------------------------------------------------------------------
    # Generate + submit (async)
    # ------------------------------------------------------------------

    async def generate_and_submit(
        self,
        dataset_id: str,
        n: int = 10,
        *,
        metafeatures: np.ndarray | None = None,
        session: str = "",
        source: str = "rl_generator",
    ) -> list[str]:
        """Generate ``n`` pipelines and submit each for background execution.

        Returns list of pipeline_ids that were successfully submitted.
        Generation errors are persist via the docstore for analysis.
        """
        from dorian.pipeline.generation.executor import persist_and_submit, persist_generation_errors
        from dorian.pipeline.generation.error_learning import invalid_ops_for_dataset
        from backend.events import Event, aemit_bg

        # Pre-compute the dataset-specific error mask ONCE for this batch.
        # Reads execution_error_instances and returns the set of operator
        # FQNs that have failed repeatedly on this dataset. The env then
        # removes them from action_masks for every episode in the batch.
        catalog_ops = [spec.name for spec in self._env._catalog]
        try:
            masked_ops, mask_stats = await invalid_ops_for_dataset(
                dataset_id, catalog_ops,
            )
        except Exception:
            masked_ops, mask_stats = set(), {}

        if masked_ops:
            # Fire-and-forget so observability reflects what the agent
            # has learned from history without blocking the episode.
            await aemit_bg(Event("RLActionMaskFromErrors", {
                "dataset_id": dataset_id,
                "session": session,
                "masked_operators": sorted(masked_ops),
                "stats": {
                    op: {
                        "failures": s.failures,
                        "distinct_signatures": s.distinct_signatures,
                        "preview": s.last_error_preview,
                    }
                    for op, s in mask_stats.items()
                    if op in masked_ops
                },
            }))

        dags, errors = self.generate_batch(
            n,
            metafeatures=metafeatures,
            error_masked_ops=masked_ops,
            error_mask_stats={
                op: s.__dict__ for op, s in mask_stats.items()
            },
        )
        submitted: list[str] = []

        for dag in dags:
            pid = await persist_and_submit(
                dag,
                dataset_id=dataset_id,
                task=self.task,
                session=session,
                source=source,
            )
            if pid:
                submitted.append(pid)

        # Persist generation errors as first-class data
        if errors:
            await persist_generation_errors(
                errors=errors,
                dataset_id=dataset_id,
                task=self.task,
                session=session,
                source=source,
            )

        _log.info(
            "Generated %d pipelines, submitted %d for execution on dataset %s (%d errors).",
            len(dags), len(submitted), dataset_id, len(errors),
        )
        return submitted

    # ------------------------------------------------------------------
    # Action selection (mode-dependent)
    # ------------------------------------------------------------------

    def _select_action(self, info: dict[str, Any]) -> int | None:
        """Select an action based on the current inference mode.

        Returns None if no valid actions are available.
        """
        valid = info.get("valid_actions", [])
        if not valid:
            return None

        if self.mode == "model_free":
            return self._rng.choice(valid)
        elif self.mode == "model_guided":
            # Future: query trained policy network
            # For now, fall back to random
            return self._rng.choice(valid)
        elif self.mode == "blended":
            # Future: epsilon-greedy between random and policy
            return self._rng.choice(valid)
        else:
            return self._rng.choice(valid)
