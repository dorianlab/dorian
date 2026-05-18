"""Training loop + compose entrypoint for the Tier-C/D RL demo."""

from .config import TrainerConfig
from .loop import rollout_episode, train

__all__ = ["TrainerConfig", "rollout_episode", "train"]
