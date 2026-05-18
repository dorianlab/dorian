"""
dorian/event/handlers/risk_kb.py
---------------------------------
KB fetch helpers and shared utilities for the AI Debugger risk chain.

Pure async KB-query wrappers with no event coupling — consumed by
``risk_debugger``, ``risk_pathways``, and ``risk_checks``.
"""

import importlib
import json
from dataclasses import dataclass

from functools import wraps

from backend.events import Event, emit
from backend.envs import aioredis, expdb
from dorian.knowledge.ontology_kb import load_kb
from dorian.infra.keys import RedisKeys, STREAM_MAXLEN


def async_lru_cache(maxsize: int = 256):
    """Tiny in-process cache for coroutine results.

    Calls are now adjacency lookups against the rust KB snapshot, so
    they're cheap; the cache mostly avoids re-parsing the snapshot
    triples on every fan-out. ``maxsize`` is honoured loosely (FIFO
    eviction on overflow).
    """
    def deco(fn):
        cache: dict = {}

        @wraps(fn)
        async def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            if key in cache:
                return cache[key]
            value = await fn(*args, **kwargs)
            if len(cache) >= maxsize:
                cache.pop(next(iter(cache)))
            cache[key] = value
            return value
        return wrapper
    return deco


# ── Check result type ───────────────────────────────────────────────────────

@dataclass
class CheckResult:
    """Rich result from a data check invocation."""
    confirmed: bool       # True = risk is present
    message: str = ""     # human-readable summary for the frontend


# ── helpers ──────────────────────────────────────────────────────────────────

def _short_name(fqn: str) -> str:
    """``sklearn.preprocessing.StandardScaler`` → ``StandardScaler``."""
    return fqn.rsplit(".", 1)[-1] if "." in fqn else fqn


_rewrite_rule_cache: dict[str, bool] = {}

async def _has_rewrite_rule(mitigation_name: str) -> bool:
    """Check whether a rewrite rule exists in the docstore for *mitigation_name*.

    Results are cached in-process — the rewrites collection is static at
    runtime (only changes on re-seed which restarts the process).
    """
    if mitigation_name in _rewrite_rule_cache:
        return _rewrite_rule_cache[mitigation_name]
    slug = mitigation_name.lower().replace(" ", "-")
    doc = await expdb.rewrites.find_one({"_id": slug}, {"_id": 1})
    if doc:
        _rewrite_rule_cache[mitigation_name] = True
        return True
    doc = await expdb.rewrites.find_one({"name": mitigation_name}, {"_id": 1})
    result = doc is not None
    _rewrite_rule_cache[mitigation_name] = result
    return result


def _resolve_check_fn(check_name: str):
    """Dynamically resolve a toolbox check function by its KB node name.

    The KB stores check nodes like ``class_imbalance``.  We resolve them via
    ``dorian.toolbox.checks.<name>``.
    """
    module_path = f"dorian.toolbox.checks"
    try:
        mod = importlib.import_module(module_path)
        return getattr(mod, check_name, None)
    except Exception as exc:
        emit(Event("CheckModuleImportFailed", {"module": module_path, "error": str(exc)}))
        return None


def _record_completeness_actions() -> list[dict]:
    return [
        {
            "action": "remove_records_with_missing_values",
            "title": "Remove incomplete rows",
            "description": "Drop rows that contain at least one missing value.",
        },
        {
            "action": "impute_missing_values",
            "title": "Impute missing values",
            "description": "Fill missing values with the most frequent value per column.",
        },
    ]


_DATASET_CONFIG_SUFFIXES_TO_COPY = (
    "quality_threshold_mode",
    "quality_threshold_override",
    "syntactic_allowed_values",
    "sensitive_columns",
    "semantic_accuracy_rules",
    "inaccuracy_columns",
    "range_rules",
    "value_occurrence_expectations",
    "category_column",
    "balance_target_labels",
    "compliance_rules",
    "consistency_label_threshold",
    "format_schema",
    "semantic_consistency_rules",
    "feature_effectiveness_rules",
    "category_size_threshold",
    "label_effectiveness_rules",
    "target_size",
    "precision_requirements",
    "relevant_features",
    "record_relevance_condition",
    "required_attributes",
)


async def _emit_mitigation_session_state(uid: str, session: str, dataset: dict) -> None:
    stream = RedisKeys.stream(uid, session)
    did = dataset.get("did", "")
    mitigation_session = dataset.get("mitigation_session")
    await aioredis.xadd(stream, {
        "event": "state/data-mitigation-session",
        "did": did,
        "value": json.dumps(mitigation_session),
        "type": "json",
    }, maxlen=STREAM_MAXLEN, approximate=True)
    await aioredis.xadd(stream, {
        "event": "state/dataset",
        "value": json.dumps(dataset),
        "type": "json",
    }, maxlen=STREAM_MAXLEN, approximate=True)


async def _load_active_dataset_meta(session: str) -> tuple[dict | None, dict | None]:
    raw = await aioredis.get(RedisKeys.session_meta(session))
    if not raw:
        return None, None
    meta = json.loads(raw)
    dataset = meta.get("dataset")
    if not isinstance(dataset, dict):
        return meta, None
    return meta, dataset


# ── KB query helpers ─────────────────────────────────────────────────────────

@async_lru_cache(maxsize=256)
async def _kb_risks_for_operator(operator: str) -> list[dict]:
    """``(op)-[:might_introduce]->(risk)`` → list of ``{risk_name}``."""
    kb = load_kb()
    return [{"risk_name": kb.display(r)} for r in kb.out(operator, "might_introduce")]


@async_lru_cache(maxsize=256)
async def _kb_mitigations_for_risk(risk: str) -> list[dict]:
    """``(m)-[:might_mitigate]->(risk)`` → list of ``{name}``."""
    kb = load_kb()
    return [{"name": kb.display(m)} for m in kb.incoming(risk, "might_mitigate")]


@async_lru_cache(maxsize=256)
async def _kb_principles_for_risk(risk: str) -> list[str]:
    """``(risk)-[:is_threat_to]->(principle)``."""
    kb = load_kb()
    return [kb.display(p) for p in kb.out(risk, "is_threat_to")]


@async_lru_cache(maxsize=256)
async def _kb_checks_for_risk(risk: str) -> list[str]:
    """``(check)-[:checks_for]->(risk)`` → check node names."""
    kb = load_kb()
    return [kb.display(c) for c in kb.incoming(risk, "checks_for")]


async def _kb_descriptions_for_mitigation(mitigation: str) -> tuple[str, str]:
    """Fetch short + long description templates from the KB.

    ``(m)-[:with_description]->(short)``
    ``(m)-[:with_long_description]->(long)``

    Returns ``(short_template, long_template)``.  Empty strings if missing.
    """
    kb = load_kb()
    short_dst = kb.out(mitigation, "with_description")
    long_dst = kb.out(mitigation, "with_long_description")
    short = kb.display(short_dst[0]) if short_dst else ""
    long_ = kb.display(long_dst[0]) if long_dst else ""
    return (short, long_)


# ── Batched mitigation lookup with async cache ─────────────────────────────
#
# When the AI Debugger identifies risks for N operators, the same risk name
# often appears for multiple operators (e.g. "Overfitting" for every sklearn
# estimator).  Each call to identify_mitigations previously made 1 query for
# mitigations + N queries for descriptions = O(M+1) round-trips per risk.
#
# _kb_mitigations_with_descriptions combines mitigations + descriptions into
# a single Cypher query (1 round-trip), and _mitigation_cache ensures that
# repeated risks across operators are served from memory.

_mitigation_cache: dict[str, list[dict]] = {}


async def _kb_mitigations_with_descriptions(risk: str) -> list[dict]:
    """Fetch mitigations and their description templates for *risk* in a single
    Neo4j round-trip.

    Returns ``[{name, short, long}, ...]``.  Results are cached in-process by
    risk name so that multiple operators sharing the same risk (common for
    structural KB risks like Overfitting) avoid redundant queries.

    The cache lives for the process lifetime — safe because the KB is read-only
    at runtime and only changes on re-seed (which restarts the process).
    """
    if risk in _mitigation_cache:
        return _mitigation_cache[risk]

    kb = load_kb()
    result: list[dict] = []
    for m_node in kb.incoming(risk, "might_mitigate"):
        name = kb.display(m_node)
        short_dst = kb.out(m_node, "with_description")
        long_dst = kb.out(m_node, "with_long_description")
        result.append({
            "name": name,
            "short": kb.display(short_dst[0]) if short_dst else "",
            "long": kb.display(long_dst[0]) if long_dst else "",
        })

    _mitigation_cache[risk] = result
    return result


@async_lru_cache(maxsize=256)
async def _kb_direct_alternatives(operator: str, risk: str) -> tuple[str, list[str]]:
    """Find operators that ``performs`` the same task but do NOT ``might_introduce``
    the given risk.

    Returns ``(task_name, [alt_fqn, ...])``."""
    kb = load_kb()
    tasks = kb.out(operator, "performs")
    if not tasks:
        return ("", [])
    task = kb.display(tasks[0])

    seen: set[str] = set()
    alts: list[str] = []
    for task_node in tasks:
        for alt in kb.incoming(task_node, "performs"):
            alt_name = kb.display(alt)
            if alt_name == operator or alt_name in seen:
                continue
            # Skip alternatives that introduce the same risk.
            risks = {kb.display(r) for r in kb.out(alt, "might_introduce")}
            if risk in risks:
                continue
            seen.add(alt_name)
            alts.append(alt_name)
    alts.sort()
    return (task, alts)
