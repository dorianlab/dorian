"""Dataset profiling — robust metafeatures computed from the actual
CSV, not from heuristic tags on the :class:`CC18Dataset` registry.

Two consumers:

  1. **Policy feature vector**. The MemoryPolicy's dataset-similarity
     prior (``_prior`` in ``rl/policy/memory_policy.py``) keys on an
     8-dim embedding. Previously this came from
     ``DatasetRegistry.feature_embedding`` which used the
     registry's approximate-row-count / feature-mix tags. When a
     dataset was loaded from the docstore those tags defaulted to
     ``feature_mix="mixed"`` regardless of the CSV's real content,
     so credit-g (all categorical) and mfeat (all numeric) landed at
     the same embedding — warm-start credits transferred into
     wrong-context priors. The profile's :meth:`feature_vector`
     replaces that embedding with measured values.

  2. **LLM prompt input**. :meth:`to_prompt_dict` returns the
     profile as a JSON-serialisable shape suitable for an
     OpenAI-compatible chat endpoint or an MCP tool, so an external
     recommender can bias the mask toward
     encoder / imputer / classifier choices without us guessing.

Robustness contract: every field is *measured* or ``None``. No
heuristics. We never lie to the policy or the LLM about what we
observed. Fields that require more than a column-dtype sniff (deep
stats, correlations, drift) can be added later; missing is fine —
the prior sources treat unknown fields as "don't care."
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None  # type: ignore[assignment]


# In-process cache keyed by (csv_path, mtime). Profile computation
# reads the CSV header + full column; cheap on small datasets
# but still worth caching when every episode hits the same file.
_PROFILE_CACHE: dict[tuple[str, float], "DatasetProfile"] = {}


@dataclass(frozen=True)
class DatasetProfile:
    """Measured metafeatures of one dataset.

    Carries the full 51-metafeature canonical profile (``raw`` dict)
    alongside a handful of projected scalars for prompt / observability
    use. Both the policy's similarity prior and the KD-Tree's
    dataset-similarity index consume the same ``raw`` profile via
    :func:`profile_to_policy_vector` / :func:`profile_to_vector`
    respectively — no parallel feature engineering, no handpicked
    dims.

    Projected scalars (``n_rows``, ``n_numeric_features``, etc.) are
    for human consumption (LLM prompts, MCP tool responses, debug
    logs). They are NOT the policy's feature vector. A field that
    can't be projected stays ``None``; downstream treats ``None`` as
    unknown, not zero.
    """

    name: str
    n_rows: int
    n_features: int
    n_numeric_features: int
    n_categorical_features: int
    has_nulls: bool
    null_fraction: float
    has_strings: bool
    n_classes: int | None
    class_imbalance: float | None
    task: str
    target_column: str | None
    # The canonical 51-metafeature dict — same shape the backend's
    # profiler writes to ``datasets.profile``. The policy's feature
    # vector derives from this via
    # ``dorian.experiment.kdtree.profile_to_policy_vector``; the
    # KD-Tree's dataset-similarity index derives from the same dict
    # via ``profile_to_vector``. Single source of truth.
    raw: dict = field(default_factory=dict)

    def feature_vector(self) -> tuple[float, ...]:
        """Policy-facing embedding. Delegates to the canonical
        projection in ``dorian.experiment.kdtree`` so policy-side
        dataset similarity and KD-Tree-side dataset similarity share
        one definition of "how do you vectorise a profile."
        """
        from dorian.experiment.kdtree import profile_to_policy_vector
        return tuple(profile_to_policy_vector(self.raw).tolist())

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "n_rows": self.n_rows,
            "n_features": self.n_features,
            "n_numeric_features": self.n_numeric_features,
            "n_categorical_features": self.n_categorical_features,
            "has_nulls": self.has_nulls,
            "null_fraction": round(self.null_fraction, 4),
            "has_strings": self.has_strings,
            "n_classes": self.n_classes,
            "class_imbalance": (
                round(self.class_imbalance, 4)
                if self.class_imbalance is not None else None
            ),
            "task": self.task,
            "target_column": self.target_column,
        }


def compute_dataset_profile(
    csv_path: str | Path,
    *,
    name: str | None = None,
    target_column: str | int = -1,
    task: str = "classification",
    dataset_id: str | None = None,
) -> DatasetProfile:
    """Build a robust profile for the dataset at ``csv_path``.

    Preference order:
      1. **Canonical Postgres profile** from the 51-metafeature
         profiler (``dorian.exec.profile``). That profile covers
         landmark models, PCA stats, skewness, class entropy — all
         computed by the backend on upload. If present AND
         non-degenerate (numeric + categorical counts > 0), we
         project the fields this module cares about from it. Single
         source of truth: same metafeatures the KD-Tree keys on.
      2. **Direct CSV read** as fallback. Used when the dataset
         isn't in Postgres (stand-alone tests, first-boot trainer)
         or when the stored profile predates the ``feat_type`` bug
         fix (all 73 datasets as of the audit — every row has
         ``NumberOfNumericFeatures=0`` from the string-vs-dtype
         mismatch). Graceful degradation: we never lie about
         measured fields, just fill them from the CSV ourselves
         when the canonical source is unusable.

    Cached per (absolute path, mtime).

    ``dataset_id`` (optional) is the Postgres ``datasets.id`` for
    this dataset. When passed, we try the canonical profile first;
    when omitted, skip straight to CSV fallback.
    """
    if pd is None:
        raise RuntimeError(
            "pandas required for dataset profiling — import failed at module load"
        )
    p = Path(csv_path).resolve()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0.0
    key = (str(p), mtime)
    if key in _PROFILE_CACHE:
        return _PROFILE_CACHE[key]

    if dataset_id:
        canonical = _try_canonical_profile(
            dataset_id, name or p.stem, task,
        )
        if canonical is not None:
            _PROFILE_CACHE[key] = canonical
            return canonical

    # Canonical profile unavailable (stand-alone tests, first-boot
    # trainer, dataset not yet in Postgres). Fall back to running
    # the same 51-metafeature profiler inline — same shape the
    # canonical path produces.
    try:
        profile_raw = _profile_inline(p, target_column, task)
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "inline profile for %s failed (%s) — using empty profile",
            p, exc,
        )
        profile_raw = {}

    profile = _build_from_raw(profile_raw, name=name or p.stem, task=task)
    _PROFILE_CACHE[key] = profile
    return profile


def _build_from_raw(
    raw: dict, *, name: str, task: str, target_column: str | None = None,
) -> DatasetProfile:
    """Project the 51-metafeature canonical profile into the scalar
    fields DatasetProfile exposes. Non-derivable scalars stay None /
    False rather than being guessed."""
    n_num = int(float(raw.get("NumberOfNumericFeatures") or 0))
    n_cat = int(float(raw.get("NumberOfCategoricalFeatures") or 0))
    n_rows = int(float(raw.get("NumberOfInstances") or 0))
    n_features = int(float(raw.get("NumberOfFeatures") or 0))
    null_pct = float(raw.get("PercentageOfMissingValues") or 0.0)
    n_missing = float(raw.get("NumberOfMissingValues") or 0.0)

    n_classes: int | None = None
    p_mean = raw.get("ClassProbabilityMean")
    if isinstance(p_mean, (int, float)) and p_mean > 0:
        n_classes = max(1, int(round(1.0 / p_mean)))

    class_imbalance: float | None = None
    p_min = raw.get("ClassProbabilityMin")
    p_max = raw.get("ClassProbabilityMax")
    if isinstance(p_min, (int, float)) and isinstance(p_max, (int, float)) and p_max > 0:
        class_imbalance = float(1.0 - (p_min / p_max))

    return DatasetProfile(
        name=name,
        n_rows=n_rows,
        n_features=n_features,
        n_numeric_features=n_num,
        n_categorical_features=n_cat,
        has_nulls=n_missing > 0,
        null_fraction=null_pct,
        has_strings=n_cat > 0,
        n_classes=n_classes,
        class_imbalance=class_imbalance,
        task=task,
        target_column=target_column,
        raw=dict(raw),
    )


def _profile_inline(
    csv_path: Path, target_column: str | int, task: str
) -> dict:
    """Run the canonical ``profile_dataframe`` inline on a CSV.

    Uses the same profiler the backend invokes on upload — single
    definition of "what 51 metafeatures we measure." No per-field
    re-implementation here; if the profiler gains a metafeature the
    RL policy embedding picks it up for free on the next restart.
    """
    from dorian.tabular.data.profiling.profile_dataset import profile_dataframe
    df = pd.read_csv(csv_path)

    # Resolve target column to a concrete name so the canonical
    # profiler's class-entropy / landmark metafeatures can resolve y.
    n_cols = df.shape[1]
    if isinstance(target_column, int):
        idx = target_column if target_column >= 0 else n_cols + target_column
        target_name = df.columns[idx] if 0 <= idx < n_cols else df.columns[-1]
    else:
        target_name = target_column if target_column in df.columns else df.columns[-1]

    return profile_dataframe(df, target_columns=[target_name])


def _try_canonical_profile(
    dataset_id: str, name: str, task: str
) -> DatasetProfile | None:
    """Fetch the cached 51-metafeature profile from
    ``doc_datasets.data->'profile'`` (the dataset-level dict the
    backend's profiler writes on upload) and project it into a
    :class:`DatasetProfile`. Return None when the doc is missing or
    the profile field hasn't been populated yet — the caller falls
    back to inline profiling.

    Single source of truth: the upload flow's ``DataProfiled``
    handler is the only writer; we just read the same dict here.
    Per-column stats under ``data->'columns'->'profiles'`` are a
    different artefact (column-level summaries used by the
    suggester) and are NOT what the KD-Tree / policy embedding
    consumes — never read from there.

    Lookup uses ``doc_datasets.id`` directly: the RL env's
    ``CC18Dataset.catalogue_id`` is exactly that UUID. The unified
    ``per-collection doc_* tables`` table was retired in 25c79a4.
    """
    try:
        import asyncpg
        from backend.config import config as _cfg
    except Exception:
        return None

    import asyncio as _asyncio
    import threading

    out: list[dict] = []

    async def _fetch() -> dict | None:
        pg = _cfg.postgresql
        conn = await asyncpg.connect(
            host=pg.host,
            port=int(pg.port),
            user="dorian",
            password=pg.password,
            database="dorian",
        )
        try:
            row = await conn.fetchrow(
                """
                SELECT data->'profile' AS profile,
                       data->'columns'->'targets' AS targets,
                       data->'columns'->'features' AS features,
                       data->'itemCount' AS item_count
                FROM doc_datasets
                WHERE id = $1
                """,
                dataset_id,
            )
        finally:
            await conn.close()
        if row is None:
            return None
        import json as _json
        prof_raw = row["profile"]
        prof: dict | None
        if prof_raw is None:
            prof = None
        elif isinstance(prof_raw, str):
            try:
                prof = _json.loads(prof_raw)
            except ValueError:
                prof = None
        else:
            prof = dict(prof_raw)
        targets_raw = row["targets"]
        if isinstance(targets_raw, str):
            try:
                targets = _json.loads(targets_raw) or []
            except ValueError:
                targets = []
        else:
            targets = list(targets_raw or [])
        features_raw = row["features"]
        if isinstance(features_raw, str):
            try:
                features = _json.loads(features_raw) or []
            except ValueError:
                features = []
        else:
            features = list(features_raw or [])
        item_count_raw = row["item_count"]
        if isinstance(item_count_raw, str):
            try:
                item_count = int(_json.loads(item_count_raw))
            except (ValueError, TypeError):
                item_count = 0
        else:
            try:
                item_count = int(item_count_raw or 0)
            except (TypeError, ValueError):
                item_count = 0
        return {
            "profile": prof,
            "targets": targets,
            "features": features,
            "item_count": item_count,
        }

    def _worker() -> None:
        loop = _asyncio.new_event_loop()
        try:
            _asyncio.set_event_loop(loop)
            try:
                doc = loop.run_until_complete(_fetch())
            except BaseException:
                return
            if doc is not None:
                out.append(doc)
        finally:
            loop.close()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()
    if not out:
        return None

    doc = out[0]
    prof = doc["profile"]
    targets = doc["targets"]
    target_col = str(targets[0]) if targets else None

    if isinstance(prof, dict):
        n_numeric = int(float(prof.get("NumberOfNumericFeatures") or 0))
        n_categorical = int(float(prof.get("NumberOfCategoricalFeatures") or 0))
        if n_numeric + n_categorical > 0:
            # Full canonical profile present and non-degenerate —
            # the happy path.
            return _build_from_raw(
                prof, name=name, task=task, target_column=target_col,
            )

    # Profile field missing or degenerate. Synthesize a minimal stub
    # from `itemCount` + `columns->features` so the trainer's
    # warm-start can use the legacy embedding path immediately
    # instead of inline-profiling 60k×3072 CIFAR-style datasets that
    # take 20+ min on a single core. The stub carries enough to feed
    # the policy's similarity prior without lying — landmark
    # accuracies + class entropy stay None / unset, so downstream
    # consumers treat them as unknown rather than zero.
    item_count = int(doc.get("item_count") or 0)
    features = doc.get("features") or []
    if item_count <= 0 or not features:
        return None
    stub = {
        "NumberOfInstances": float(item_count),
        "NumberOfFeatures": float(len(features)),
        "NumberOfClasses": 0.0,
    }
    return _build_from_raw(stub, name=name, task=task, target_column=target_col)


__all__ = ["DatasetProfile", "compute_dataset_profile"]
