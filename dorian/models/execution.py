"""
dorian/models/execution.py
--------------------------
Pydantic models for tracking the lifecycle state of a pipeline run.
"""
from __future__ import annotations

from enum import Enum
from time import time
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class NodeStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"   # downstream of a failed node
    CANCELLED = "CANCELLED"


class NodeState(BaseModel):
    node_id: str
    status: NodeStatus = NodeStatus.PENDING
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    error: Optional[str] = None
    # Pointer to result stored in ResultStore (None until SUCCESS)
    result_ref: Optional[str] = None

    @property
    def duration(self) -> Optional[float]:
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return None


class PipelineRunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PipelineExecution(BaseModel):
    run_id: str
    session_id: str
    pipeline_id: str
    uid: str
    status: PipelineRunStatus = PipelineRunStatus.PENDING
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    node_states: Dict[str, NodeState] = Field(default_factory=dict)

    @property
    def has_failures(self) -> bool:
        return any(
            n.status in (NodeStatus.FAILED, NodeStatus.SKIPPED)
            for n in self.node_states.values()
        )

    def summary(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "pipeline_id": self.pipeline_id,
            "status": self.status,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "nodes": {
                nid: {
                    "status": ns.status,
                    "duration": ns.duration,
                    "error": ns.error,
                    "result_ref": ns.result_ref,
                }
                for nid, ns in self.node_states.items()
            },
        }
