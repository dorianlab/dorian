"""Pipeline versioning + canonical-form substitution.

Submodules:

  * ``class_hash``    -- structural class-hash function over a DAG
  * ``ledger``        -- rewrite-observation recording
  * ``promotion``     -- hit-rate + min-observation policy over
                         the ledger
  * ``substitution``  -- recommendation-engine substitution against
                         a canonical-form registry

See (internal design note; not in public repo) for design rationale.
"""

from .class_hash import canonical_class_hash, describe
from .instance_hash import canonical_instance_hash
from .ledger import (
    MemoryLedger,
    RewriteLedger,
    RewriteObservation,
    SourceStats,
)
from .promotion import (
    PROMOTION_HIT_RATE_THRESHOLD,
    PROMOTION_MIN_OBSERVATIONS,
    PromotionDecision,
    demotions,
    evaluate,
    promotions,
)
from .substitution import (
    CanonicalEntry,
    CanonicalRegistry,
    DictCanonicalRegistry,
    SubstitutionResult,
    substitute,
    substitute_many,
)

__all__ = [
    "CanonicalEntry",
    "CanonicalRegistry",
    "DictCanonicalRegistry",
    "MemoryLedger",
    "PROMOTION_HIT_RATE_THRESHOLD",
    "PROMOTION_MIN_OBSERVATIONS",
    "PromotionDecision",
    "RewriteLedger",
    "RewriteObservation",
    "SourceStats",
    "SubstitutionResult",
    "canonical_class_hash",
    "canonical_instance_hash",
    "demotions",
    "describe",
    "evaluate",
    "promotions",
    "substitute",
    "substitute_many",
]
