"""
dorian/state/execution.py
--------------------------
Redis-backed state tracker for pipeline executions.

Keys:
    execution:{run_id}                  → PipelineExecution JSON (run-level only)
    execution:{run_id}:node:{node_id}   → NodeState JSON (per-node, zero-contention)

Node states are stored in dedicated per-node keys to eliminate WATCH/MULTI
contention when many Dask worker threads update different nodes concurrently.
The monolithic execution key holds only run-level fields; at finalization the
sync engine gathers per-node keys and embeds them for the summary.
"""
from __future__ import annotations

import json
from time import time
from typing import Dict, List, Optional

from backend.envs import aioredis
from dorian.infra.keys import RedisKeys
from dorian.models.execution import (
    NodeState,
    NodeStatus,
    PipelineExecution,
    PipelineRunStatus,
)

_TTL = 60 * 60 * 24  # 24 hours


class StateTracker:
    """
    All methods are async-safe and idempotent.
    Node state is stored per-node in dedicated Redis keys (zero contention).
    Run-level state is stored in a single key.
    """

    # ------------------------------------------------------------------ run-level

    @staticmethod
    async def create_run(execution: PipelineExecution) -> None:
        """Persist a brand-new PipelineExecution to Redis."""
        key = RedisKeys.execution(execution.run_id)
        await aioredis.set(key, execution.model_dump_json(), ex=_TTL)

    @staticmethod
    async def get_run(run_id: str) -> Optional[PipelineExecution]:
        raw = await aioredis.get(RedisKeys.execution(run_id))
        if not raw:
            return None
        return PipelineExecution.model_validate_json(raw)

    @staticmethod
    async def save_run(execution: PipelineExecution) -> None:
        key = RedisKeys.execution(execution.run_id)
        await aioredis.set(key, execution.model_dump_json(), ex=_TTL)

    @staticmethod
    async def mark_run_started(run_id: str) -> Optional[PipelineExecution]:
        execution = await StateTracker.get_run(run_id)
        if execution is None:
            return None
        execution.status = PipelineRunStatus.RUNNING
        execution.start_time = time()
        await StateTracker.save_run(execution)
        return execution

    @staticmethod
    async def mark_run_finished(run_id: str, failed: bool) -> Optional[PipelineExecution]:
        execution = await StateTracker.get_run(run_id)
        if execution is None:
            return None
        execution.status = PipelineRunStatus.FAILED if failed else PipelineRunStatus.SUCCESS
        execution.end_time = time()
        await StateTracker.save_run(execution)
        return execution

    # ------------------------------------------------------------------ node-level

    @staticmethod
    async def patch_node(run_id: str, node_id: str, **fields) -> None:
        """Update a single NodeState in its own per-node key (async version)."""
        key = RedisKeys.node_state(run_id, node_id)
        raw = await aioredis.get(key)
        ns = NodeState.model_validate_json(raw) if raw else NodeState(node_id=node_id)
        for k, v in fields.items():
            setattr(ns, k, v)
        await aioredis.set(key, ns.model_dump_json(), ex=_TTL)

    @staticmethod
    async def gather_node_states(
        run_id: str, node_ids: List[str]
    ) -> Dict[str, NodeState]:
        """Read all per-node keys for a run (async, pipelined GETs).

        Uses a pipeline of individual GETs rather than MGET to stay
        compatible with Redis ACLs that only whitelist ``+get``.
        """
        if not node_ids:
            return {}
        pipe = aioredis.pipeline(transaction=False)
        for nid in node_ids:
            pipe.get(RedisKeys.node_state(run_id, nid))
        raw_values = await pipe.execute()
        states: Dict[str, NodeState] = {}
        for nid, raw in zip(node_ids, raw_values):
            if raw:
                states[nid] = NodeState.model_validate_json(raw)
        return states

    @staticmethod
    async def mark_node_running(run_id: str, node_id: str) -> None:
        await StateTracker.patch_node(
            run_id, node_id, status=NodeStatus.RUNNING, start_time=time()
        )

    @staticmethod
    async def mark_node_success(
        run_id: str, node_id: str, result_ref: Optional[str] = None
    ) -> None:
        await StateTracker.patch_node(
            run_id, node_id, status=NodeStatus.SUCCESS, end_time=time(), result_ref=result_ref
        )

    @staticmethod
    async def mark_node_failed(run_id: str, node_id: str, error: str) -> None:
        await StateTracker.patch_node(
            run_id, node_id, status=NodeStatus.FAILED, end_time=time(), error=error
        )
