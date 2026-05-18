"""BK-Tree index for pipeline similarity search.

A Burkhard-Keller tree using graph edit distance as the metric function.
Provides O(log n) approximate nearest-neighbor search over pipeline DAGs.

For performance, each pipeline is stored with its sorted operator-name list.
The symmetric difference of operator sets serves as a **fast lower bound**
on the true graph edit distance, enabling aggressive pruning during search
before computing the expensive exact GED.

Reference: VLDB 2024 paper §6.2 "BK-Tree for Pipeline Similarity".
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict

from backend.events import Event, aemit

_log = logging.getLogger(__name__)
from dorian.experiment.similarity import (
    extract_operator_names,
    graph_edit_distance,
)


# ---------------------------------------------------------------------------
# BK-Tree node
# ---------------------------------------------------------------------------

class _BKNode:
    """A node in the BK-Tree."""

    __slots__ = ("pipeline_id", "operators", "dag_json", "children")

    def __init__(self, pipeline_id: str, operators: list[str], dag_json: dict):
        self.pipeline_id = pipeline_id
        self.operators = operators       # sorted operator names
        self.dag_json = dag_json
        self.children: dict[int, _BKNode] = {}  # distance → child

    def add(self, pipeline_id: str, operators: list[str], dag_json: dict,
            distance_func) -> None:
        """Insert a new pipeline into the subtree rooted at this node."""
        d = distance_func(self.dag_json, dag_json)
        if d in self.children:
            self.children[d].add(pipeline_id, operators, dag_json, distance_func)
        else:
            self.children[d] = _BKNode(pipeline_id, operators, dag_json)

    def query(self, target_ops: list[str], target_json: dict, max_distance: int,
              distance_func, results: list) -> None:
        """Search the subtree for pipelines within max_distance.

        Uses the operator-set symmetric difference as a fast lower bound
        to prune branches that cannot possibly contain matches.
        """
        # Fast lower bound: symmetric difference of operator multisets
        lower_bound = _operator_set_distance(self.operators, target_ops)

        if lower_bound <= max_distance:
            # Compute exact distance only if the lower bound passes
            d = distance_func(self.dag_json, target_json)
            if d <= max_distance:
                results.append((self.pipeline_id, d))
        else:
            # Even the lower bound exceeds threshold — use it for pruning range
            d = lower_bound

        # BK-Tree triangle inequality: only visit children whose distance
        # is in [d - max_distance, d + max_distance]
        for child_dist in range(d - max_distance, d + max_distance + 1):
            if child_dist in self.children:
                self.children[child_dist].query(
                    target_ops, target_json, max_distance, distance_func, results
                )


def _operator_set_distance(ops1: list[str], ops2: list[str]) -> int:
    """Fast lower bound on GED: symmetric difference of operator name multisets.

    This counts how many operator additions + removals are needed at minimum,
    ignoring edge topology.  Always ≤ the true GED.
    """
    s1 = set(ops1)
    s2 = set(ops2)
    return len(s1.symmetric_difference(s2))


# ---------------------------------------------------------------------------
# PipelineBKTree
# ---------------------------------------------------------------------------

class PipelineBKTree:
    """In-memory BK-Tree over pipeline DAGs using graph edit distance.

    The tree is rebuilt from Postgres on startup and incrementally updated
    when new pipelines are saved.

    Parameters
    ----------
    use_exact_ged : bool
        If True (default), use exact graph edit distance for queries.
        If False, use the fast approximate distance (operator set diff +
        edge count diff).  The approximate mode is ~100x faster but less
        accurate for structurally different pipelines with the same operators.
    """

    def __init__(self, use_exact_ged: bool = True):
        self._root: _BKNode | None = None
        self._use_exact_ged = use_exact_ged
        self._size = 0
        self._pipeline_ids: set[str] = set()  # for dedup
        # Serialises mutations across threads. ``add()`` and the initial
        # ``_build()`` both run in worker threads (never on the event loop)
        # so an OS-level lock is the right primitive — an asyncio.Lock would
        # be held across the long sync work and block the loop.
        self._mutation_lock = threading.Lock()

    @property
    def size(self) -> int:
        """Number of pipelines in the index."""
        return self._size

    # ------------------------------------------------------------------
    # Distance function
    # ------------------------------------------------------------------

    def _distance(self, dag1: dict, dag2: dict) -> int:
        """Compute the distance metric between two pipeline DAGs."""
        if self._use_exact_ged:
            return graph_edit_distance(dag1, dag2)
        else:
            # Fast approximate: operator set diff + edge count diff
            from dorian.experiment.similarity import _fast_distance, dag_json_to_nxgraph
            G1 = dag_json_to_nxgraph(dag1)
            G2 = dag_json_to_nxgraph(dag2)
            return _fast_distance(G1, G2)

    # ------------------------------------------------------------------
    # Load from Postgres
    # ------------------------------------------------------------------

    async def load_from_db(self, pool) -> None:
        """Load all pipeline DAGs from Postgres and rebuild the tree.

        The DB fetch runs on the asyncio loop, but tree construction is
        offloaded to a worker thread. ``_BKNode.add`` calls
        ``graph_edit_distance`` which is NP-hard, pure-Python and holds
        the GIL — running it inline on the loop starves every other
        coroutine (uvicorn lifespan, healthcheck, WS handlers).
        """
        import asyncio
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, operators, dag FROM pipelines"
            )

        def _build():
            import json as _json
            for row in rows:
                pid = row["id"]
                operators = list(row["operators"]) if row["operators"] else []
                dag_json = row["dag"]  # JSONB → dict (asyncpg may return str)
                if isinstance(dag_json, str):
                    dag_json = _json.loads(dag_json)

                with self._mutation_lock:
                    if pid in self._pipeline_ids:
                        continue

                    if self._root is None:
                        self._root = _BKNode(pid, operators, dag_json)
                    else:
                        self._root.add(pid, operators, dag_json, self._distance)

                    self._pipeline_ids.add(pid)
                    self._size += 1

        await asyncio.to_thread(_build)
        await aemit(Event("BKTreeLoaded", {"pipelines": self._size}))

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def add(self, pipeline_id: str, dag_json: Dict[str, Any]) -> None:
        """Add a pipeline to the tree.

        This method is GED-heavy and must be called from a worker thread —
        never from the asyncio event loop. Mutations across threads are
        serialised by ``self._mutation_lock``.

        Parameters
        ----------
        pipeline_id : str
            Unique pipeline identifier.
        dag_json : dict
            Full DAG JSON (nodes + edges).
        """
        operators = extract_operator_names(dag_json)

        with self._mutation_lock:
            if pipeline_id in self._pipeline_ids:
                _log.debug("BKTree duplicate skipped: %s", pipeline_id)
                return

            if self._root is None:
                self._root = _BKNode(pipeline_id, operators, dag_json)
            else:
                self._root.add(pipeline_id, operators, dag_json, self._distance)

            self._pipeline_ids.add(pipeline_id)
            self._size += 1

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(self, dag_json: Dict[str, Any], max_distance: int = 5) -> list[tuple[str, int]]:
        """Find all pipelines within ``max_distance`` edits of the query DAG.

        Parameters
        ----------
        dag_json : dict
            Query pipeline DAG JSON.
        max_distance : int
            Maximum allowed graph edit distance.

        Returns
        -------
        list of (pipeline_id, distance) tuples, sorted by ascending distance.
        """
        if self._root is None:
            return []

        target_ops = extract_operator_names(dag_json)
        results: list[tuple[str, int]] = []
        self._root.query(target_ops, dag_json, max_distance, self._distance, results)

        # Sort by distance ascending
        results.sort(key=lambda x: x[1])
        return results

    def find_nearest(self, dag_json: Dict[str, Any], k: int = 5,
                     max_distance: int = 10) -> list[tuple[str, int]]:
        """Find the k nearest pipelines (up to max_distance).

        Unlike ``query()`` which returns all matches within a threshold,
        this returns at most k results.
        """
        results = self.query(dag_json, max_distance)
        return results[:k]

    def contains(self, pipeline_id: str) -> bool:
        """Check if a pipeline is already in the tree."""
        return pipeline_id in self._pipeline_ids
