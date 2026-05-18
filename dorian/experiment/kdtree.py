"""KD-Tree index for dataset profile similarity search.

Provides O(log n) nearest-neighbor search over Min-Max normalized metafeature
vectors.  The feature order is **dynamic** — built from the sorted scalar
metafeature names discovered at runtime, with version tracking so the vector
shape can evolve as new metafeatures are added.

Supports **partial profiles**: when some metafeatures haven't been computed yet
(e.g. slow PCA/landmark features), the vector is still queryable — missing
dimensions are filled with 0.0 (the center of the [0,1] normalized range).
This enables jump-starting recommendations from a partially computed profile
and updating as profiling completes.

Reference: VLDB 2024 paper §6.1 "KD-Tree for Dataset Similarity".
"""
from __future__ import annotations

from typing import Sequence

import numpy as np

from backend.events import Event, aemit, emit

# sklearn.neighbors.KDTree for the actual index
try:
    from sklearn.neighbors import KDTree as _SklearnKDTree
except ImportError:
    _SklearnKDTree = None

# ---------------------------------------------------------------------------
# Feature discovery — dynamic, sorted, versioned
# ---------------------------------------------------------------------------

# These intermediate metafeatures produce non-scalar values (lists, dicts,
# PCA objects) and must be excluded from the vector.
_NON_SCALAR_NAMES = frozenset({
    "MissingValues",
    "ClassOccurences",
    "NumSymbols",
    "Kurtosisses",
    "Skewnesses",
    "PCA",
})


def discover_feature_order() -> tuple[str, ...]:
    """Build the feature order dynamically from the metafeatures module.

    Returns a sorted tuple of scalar metafeature names.  The sort ensures
    deterministic vector layout across restarts.
    """
    try:
        from dorian.tabular.data.profiling.metafeatures import mf
    except ImportError:
        import logging as _logging
        _logging.getLogger(__name__).warning("Could not import metafeatures module — using empty feature order")
        return ()

    scalar_names = sorted(
        name for name in mf.keys()
        if name not in _NON_SCALAR_NAMES
    )
    return tuple(scalar_names)


# Module-level singleton — computed once, cached
_FEATURE_ORDER: tuple[str, ...] | None = None


def get_feature_order() -> tuple[str, ...]:
    """Return the current feature order (lazy-init on first call)."""
    global _FEATURE_ORDER
    if _FEATURE_ORDER is None:
        _FEATURE_ORDER = discover_feature_order()
    return _FEATURE_ORDER


def get_feature_version() -> int:
    """Return a version number based on the feature order hash.

    When the set of metafeatures changes (features added/removed), the
    version changes.  Vectors stored with a different version need
    re-computation from the raw profile JSON.
    """
    order = get_feature_order()
    # Simple hash of the sorted names — deterministic across restarts
    return hash(order) & 0x7FFFFFFF  # positive 31-bit int


# ---------------------------------------------------------------------------
# Vectorisation
# ---------------------------------------------------------------------------

def profile_to_vector(profile: dict) -> np.ndarray:
    """Convert a metafeature dict to an ordered float vector.

    Missing or non-numeric values are replaced with ``NaN`` (which the
    normalization step handles by mapping to 0.0).

    Parameters
    ----------
    profile : dict
        Metafeature name → value mapping (as stored in session meta and Postgres).

    Returns
    -------
    np.ndarray
        1-D array of shape ``(len(feature_order),)``.
    """
    # Defensive: if profile arrives as a JSON string, parse it first.
    if isinstance(profile, str):
        import json as _json
        profile = _json.loads(profile)

    order = get_feature_order()
    vec = np.empty(len(order), dtype=np.float64)
    for i, name in enumerate(order):
        val = profile.get(name)
        if val is None or isinstance(val, (list, dict)):
            vec[i] = np.nan
        else:
            try:
                fval = float(val)
                vec[i] = fval if np.isfinite(fval) else np.nan
            except (TypeError, ValueError):
                vec[i] = np.nan
    return vec


# Metafeature names whose raw values live on a count scale
# (instances, feature counts, missing-value tallies). Applying
# ``log1p`` brings them onto the same order of magnitude as the
# landmark-accuracy / fraction metafeatures (all bounded in [0, 1])
# so downstream cosine similarity isn't dominated by row counts.
_LOG_SCALE_SUBSTRINGS = (
    "Number", "Count", "Instances", "Features", "Dimensionality",
)


def _is_log_scale(name: str) -> bool:
    if name.startswith("Log"):
        return False  # already log-transformed in profiling
    return any(s in name for s in _LOG_SCALE_SUBSTRINGS)


def profile_to_policy_vector(profile: dict) -> np.ndarray:
    """Profile → bounded vector suitable for cosine-similarity comparison.

    Stateless projection of the same feature order ``profile_to_vector``
    emits, with two deterministic adjustments:

      * NaN → 0.0 so missing metafeatures don't poison the dot product.
      * ``log1p`` for count-scale metafeatures (row / feature tallies)
        so their magnitude doesn't dominate the cosine denominator.

    No per-dataset state. Any caller with a profile dict gets the same
    vector regardless of what other datasets are in the fleet. Used by
    the RL policy's dataset-similarity prior to share a feature
    projection with :class:`DatasetKDTree` without the KD-Tree's
    fitted Min-Max normalizer being required upstream.
    """
    order = get_feature_order()
    raw = profile_to_vector(profile)
    out = np.empty_like(raw)
    for i, name in enumerate(order):
        v = raw[i]
        if not np.isfinite(v):
            out[i] = 0.0
            continue
        out[i] = float(np.log1p(v)) if _is_log_scale(name) else v
    return out


def is_partial_profile(profile: dict) -> bool:
    """Check whether a profile is missing any scalar metafeatures."""
    order = get_feature_order()
    for name in order:
        val = profile.get(name)
        if val is None:
            return True
    return False


# ---------------------------------------------------------------------------
# DatasetKDTree
# ---------------------------------------------------------------------------

class DatasetKDTree:
    """In-memory KD-Tree over dataset metafeature vectors.

    The tree is rebuilt from Postgres on startup and incrementally updated
    when new datasets are profiled.  Metafeature vectors are Min-Max
    normalized using running min/max bounds.

    Parameters
    ----------
    leaf_size : int
        sklearn KDTree leaf size (default 30).
    """

    def __init__(self, leaf_size: int = 30):
        self._leaf_size = leaf_size

        # Parallel arrays (same length, same order)
        self._ids: list[str] = []           # dataset IDs
        self._raw_vecs: list[np.ndarray] = []  # unnormalized vectors

        # Normalization bounds (expanded as new datasets arrive)
        self._min: np.ndarray | None = None
        self._max: np.ndarray | None = None

        # The sklearn index (rebuilt after every mutation)
        self._tree: _SklearnKDTree | None = None  # type: ignore[assignment]

    @property
    def size(self) -> int:
        """Number of datasets in the index."""
        return len(self._ids)

    @property
    def feature_dim(self) -> int:
        """Dimensionality of the feature vectors."""
        return len(get_feature_order())

    # ------------------------------------------------------------------
    # Load from Postgres
    # ------------------------------------------------------------------

    async def load_from_db(self, pool) -> None:
        """Load all dataset profile vectors from Postgres and rebuild the tree.

        Only loads vectors whose ``vec_version`` matches the current feature
        version.  Stale vectors are re-computed from the raw profile JSON.
        """
        current_version = get_feature_version()

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, profile, profile_vec, vec_version FROM datasets"
            )

        for row in rows:
            did = row["id"]
            profile = row["profile"]  # JSONB → dict (may be None)
            stored_vec = row["profile_vec"]
            vec_ver = row["vec_version"]

            # Guard: skip rows whose profile is NULL or otherwise not a dict
            if not isinstance(profile, dict):
                if isinstance(profile, str):
                    import json as _json
                    try:
                        profile = _json.loads(profile)
                    except (ValueError, TypeError):
                        await aemit(Event("DatasetSkipped", {"did": did, "reason": "unparseable profile"}))
                        continue
                else:
                    await aemit(Event("DatasetSkipped", {"did": did, "reason": f"profile is {type(profile).__name__}"}))
                    continue

            if stored_vec is not None and vec_ver == current_version:
                vec = np.array(stored_vec, dtype=np.float64)
            else:
                # Re-compute from raw profile
                vec = profile_to_vector(profile)
                # Schedule async update of the stored vector (fire-and-forget)
                # We don't await here to keep startup fast
                await aemit(Event("DatasetRevectorized", {"did": did, "old_version": vec_ver, "new_version": current_version}))

            self._ids.append(did)
            self._raw_vecs.append(vec)

        self._rebuild()
        await aemit(Event("KDTreeLoaded", {"datasets": self.size, "features": self.feature_dim}))

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def add(self, did: str, profile: dict) -> np.ndarray:
        """Add a single dataset and rebuild the tree.

        Parameters
        ----------
        did : str
            Dataset ID.
        profile : dict
            Full or partial metafeature dict.

        Returns
        -------
        np.ndarray
            The raw (unnormalized) vector stored for this dataset.
        """
        vec = profile_to_vector(profile)

        # Upsert: if this did already exists, replace its vector
        if did in self._ids:
            idx = self._ids.index(did)
            self._raw_vecs[idx] = vec
        else:
            self._ids.append(did)
            self._raw_vecs.append(vec)

        self._rebuild()
        return vec

    def update(self, did: str, profile: dict) -> np.ndarray | None:
        """Update an existing dataset's vector (e.g. partial → full profile).

        Returns the new vector, or None if the dataset wasn't in the index.
        """
        if did not in self._ids:
            return None
        return self.add(did, profile)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(self, profile: dict, k: int = 5) -> list[tuple[str, float]]:
        """Find the k nearest datasets to a query profile.

        Parameters
        ----------
        profile : dict
            Metafeature dict (full or partial — missing values → 0.0 after norm).
        k : int
            Number of neighbors.

        Returns
        -------
        list of (dataset_id, distance) tuples, sorted by ascending distance.
        """
        if self._tree is None or self.size == 0:
            return []

        k = min(k, self.size)
        vec = profile_to_vector(profile)
        norm_vec = self._normalize(vec)

        distances, indices = self._tree.query(norm_vec.reshape(1, -1), k=k)

        results = []
        for dist, idx in zip(distances[0], indices[0]):
            if idx < len(self._ids):
                results.append((self._ids[idx], float(dist)))
        return results

    def query_vector(self, vec: np.ndarray, k: int = 5) -> list[tuple[str, float]]:
        """Find nearest neighbors from a pre-computed raw vector."""
        if self._tree is None or self.size == 0:
            return []

        k = min(k, self.size)
        norm_vec = self._normalize(vec)
        distances, indices = self._tree.query(norm_vec.reshape(1, -1), k=k)

        results = []
        for dist, idx in zip(distances[0], indices[0]):
            if idx < len(self._ids):
                results.append((self._ids[idx], float(dist)))
        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _rebuild(self) -> None:
        """Recompute normalization bounds and rebuild the sklearn KDTree."""
        if not self._raw_vecs or _SklearnKDTree is None:
            self._tree = None
            return

        mat = np.vstack(self._raw_vecs)  # (n, d)

        # Replace NaN with 0 before computing bounds
        mat_clean = np.nan_to_num(mat, nan=0.0)

        # Update running min/max
        self._min = np.nanmin(mat_clean, axis=0)
        self._max = np.nanmax(mat_clean, axis=0)

        # Normalize
        normalized = self._normalize_batch(mat)

        self._tree = _SklearnKDTree(normalized, leaf_size=self._leaf_size)

    def _normalize(self, vec: np.ndarray) -> np.ndarray:
        """Min-Max normalize a single vector using current bounds.

        NaN values → 0.0 (the midpoint of [0, 1]).
        """
        vec = np.nan_to_num(vec, nan=0.0)

        if self._min is None or self._max is None:
            return vec

        denom = self._max - self._min
        # Avoid division by zero for constant features
        denom = np.where(denom == 0, 1.0, denom)
        return (vec - self._min) / denom

    def _normalize_batch(self, mat: np.ndarray) -> np.ndarray:
        """Min-Max normalize a matrix of vectors."""
        mat = np.nan_to_num(mat, nan=0.0)

        if self._min is None or self._max is None:
            return mat

        denom = self._max - self._min
        denom = np.where(denom == 0, 1.0, denom)
        return (mat - self._min) / denom
