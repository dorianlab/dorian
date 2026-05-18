"""Pipeline generation engine — template-free RL-driven pipeline construction.

This package builds pipelines from scratch using reinforcement learning,
learns from execution outcomes, and mines reusable patterns.  Unlike AutoML
(template-based, fixed search space), the generation engine is template-free:
it constructs DAGs using operators from the KB, learns what works from
execution outcomes and failure patterns, and mines reusable substructures.

Public API
----------
- ``GenerationEngine``    — three-mode inference (model_free / model_guided / blended)
- ``GenerationScheduler`` — background idle-aware continuous experimentation
- ``persist_and_submit``  — save a generated DAG and submit for execution
"""

from dorian.pipeline.generation.engine import GenerationEngine
from dorian.pipeline.generation.scheduler import GenerationScheduler
from dorian.pipeline.generation.executor import persist_and_submit, persist_generation_errors, set_standalone_mode
from dorian.pipeline.generation.eval_template import EvalTemplate, build_eval_template

__all__ = [
    "GenerationEngine",
    "GenerationScheduler",
    "persist_and_submit",
    "persist_generation_errors",
    "set_standalone_mode",
    "EvalTemplate",
    "build_eval_template",
]
