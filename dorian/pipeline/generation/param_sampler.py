"""KB-informed parameter sampling for pipeline generation.

Combines parameter domains from the knowledge base with learned patterns
(from ``patterns.PatternStore``) to sample hyperparameter values for each
operator placed in a pipeline.

Sampling strategy
-----------------
1. Start from the KB defaults and ranges in ``ParameterSpec``.
2. If learned patterns exist for this operator + current metafeature context,
   narrow the bounds accordingly.
3. Sample within the (possibly adjusted) bounds:
   - ``int`` / ``float``: uniform or log-uniform within [low, high]
   - ``categorical``: uniform random choice
   - ``bool``: weighted coin flip (60% default, 40% alternative)
"""
from __future__ import annotations

import math
import random
from typing import Any, Protocol

from dorian.pipeline.generation.types import OperatorSpec, ParameterSpec


# ---------------------------------------------------------------------------
# Pattern protocol (avoid circular import with patterns.py)
# ---------------------------------------------------------------------------

class PatternAdjuster(Protocol):
    """Interface for applying learned patterns to parameter bounds."""

    def adjust_bounds(
        self,
        op_name: str,
        param: ParameterSpec,
        metafeatures: dict[str, float] | None,
    ) -> ParameterSpec:
        """Return an adjusted copy of *param* based on learned patterns."""
        ...


class _NoOpAdjuster:
    """Default adjuster when no patterns are available."""

    def adjust_bounds(
        self,
        op_name: str,
        param: ParameterSpec,
        metafeatures: dict[str, float] | None,
    ) -> ParameterSpec:
        return param


_NOOP = _NoOpAdjuster()


# ---------------------------------------------------------------------------
# Sampling functions
# ---------------------------------------------------------------------------

def _sample_int(spec: ParameterSpec, rng: random.Random) -> int | None:
    if spec.low is None or spec.high is None:
        return spec.default
    low, high = int(spec.low), int(spec.high)
    if spec.log_scale and low > 0:
        log_low, log_high = math.log(low), math.log(high)
        return int(round(math.exp(rng.uniform(log_low, log_high))))
    return rng.randint(low, high)


def _sample_float(spec: ParameterSpec, rng: random.Random) -> float | None:
    if spec.low is None or spec.high is None:
        return spec.default
    if spec.log_scale and spec.low > 0:
        log_low, log_high = math.log(spec.low), math.log(spec.high)
        return math.exp(rng.uniform(log_low, log_high))
    return rng.uniform(spec.low, spec.high)


def _sample_categorical(spec: ParameterSpec, rng: random.Random) -> Any:
    if not spec.choices:
        return spec.default
    return rng.choice(spec.choices)


def _sample_bool(spec: ParameterSpec, rng: random.Random) -> bool:
    # 60% chance of the default value, 40% chance of the alternative
    default = spec.default if isinstance(spec.default, bool) else True
    return default if rng.random() < 0.6 else (not default)


_SAMPLERS = {
    "int": _sample_int,
    "float": _sample_float,
    "categorical": _sample_categorical,
    "bool": _sample_bool,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sample_parameters(
    op: OperatorSpec,
    *,
    metafeatures: dict[str, float] | None = None,
    adjuster: PatternAdjuster | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Sample a complete hyperparameter configuration for an operator.

    Parameters
    ----------
    op : OperatorSpec
        Operator to sample parameters for.
    metafeatures : dict or None
        Current dataset metafeature vector (used by learned patterns).
    adjuster : PatternAdjuster or None
        Optional adjuster that narrows bounds based on learned patterns.
    rng : random.Random or None
        Random number generator for reproducibility.

    Returns
    -------
    dict[str, Any]
        Mapping of parameter name → sampled value.  Only includes parameters
        that are defined in the operator's ``ParameterSpec`` list.
    """
    if rng is None:
        rng = random.Random()
    if adjuster is None:
        adjuster = _NOOP

    params: dict[str, Any] = {}
    for spec in op.parameters:
        # Apply learned pattern adjustments
        adjusted = adjuster.adjust_bounds(op.name, spec, metafeatures)

        sampler = _SAMPLERS.get(adjusted.dtype)
        if sampler is None:
            params[spec.name] = spec.default
            continue

        value = sampler(adjusted, rng)
        params[spec.name] = value

    return params


def default_parameters(op: OperatorSpec) -> dict[str, Any]:
    """Return the default parameter configuration for an operator."""
    return {spec.name: spec.default for spec in op.parameters}
