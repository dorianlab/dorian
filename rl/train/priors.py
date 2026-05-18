"""Warm-start priors for the RL trainer.

MemoryPolicy (and Hedge) starts every episode with zero prior weight on
every action. With a ~5-action critical path through a ~30-candidate
mask per step, random exploration takes ~500-5000 episodes to land one
valid pipeline — and the policy can't even form preferences until it
sees its first success. A warm-start closes that bootstrap gap.

Two prior sources are supported:

  * **Curated / LLM-generated action sequences** loaded from
    ``rl/train/llm_priors.json``. Each entry is a short list of
    structural edits (add_node / add_edge / commit) that collectively
    describe a pipeline known to score well on the target task. An
    LLM (or a human) authors these. No pipeline execution required —
    the trainer just credits the action_ids as successes.
  * **BK-Tree seeded pipelines** from the Postgres ``pipelines`` table
    (the 500 trial-config DAGs plus any RL-v2 winners committed since
    last reset). Each DAG is decomposed into the structural edits that
    would have built it. This gives the policy access to the curated
    hyperparameter-sweep winners without needing them re-executed.

Both sources converge on the same abstraction — a list of
``abstract_key`` tuples (the same projection ``ActionSpace.abstract_key``
computes for live rollouts). The trainer maps each to an integer
``action_id`` via the process-wide ``ActionSpace`` and credits it on
MemoryPolicy (success-rate bump) and HedgePolicy (log-weight bump).

No LLM call is made at trainer startup — the JSON is the baked-in
prior. Live LLM queries can be added later by writing new entries
into the JSON file from a separate tool. This keeps the hot path
deterministic and offline-safe.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriorAction:
    """One abstract structural edit in a prior pipeline recipe.

    Matches the three abstract-key variants the policy stores
    statistics for. Port names align with the catalog's
    ``PortSpec.name`` field (integer positional ports are stringified).
    """

    kind: str                      # "add_node" | "add_edge" | "commit"
    op_key: str | None = None      # add_node only
    src_op: str | None = None      # add_edge
    src_port: str | None = None    # add_edge
    dst_op: str | None = None      # add_edge
    dst_port: str | None = None    # add_edge

    def abstract_key(self) -> tuple:
        if self.kind == "add_node":
            return ("add_node", self.op_key or "")
        if self.kind == "add_edge":
            return (
                "add_edge",
                self.src_op or "",
                self.src_port or "",
                self.dst_op or "",
                self.dst_port or "",
            )
        if self.kind == "commit":
            return ("commit_episode",)
        raise ValueError(f"unknown prior action kind: {self.kind}")


@dataclass(frozen=True)
class PriorPipeline:
    name: str
    description: str
    datasets: tuple[str, ...]       # dataset names this prior targets
    actions: tuple[PriorAction, ...]


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_llm_priors(path: Path) -> list[PriorPipeline]:
    """Parse the curated prior file. Missing file → empty list (no crash)."""
    if not path.exists():
        _log.info("llm_priors: no file at %s, skipping", path)
        return []
    try:
        raw = json.loads(path.read_text())
    except Exception as exc:
        _log.warning("llm_priors: parse failed (%s) — skipping", exc)
        return []

    out: list[PriorPipeline] = []
    for entry in raw:
        try:
            actions = tuple(
                PriorAction(
                    kind=a["kind"],
                    op_key=a.get("op_key"),
                    src_op=a.get("src_op"),
                    src_port=a.get("src_port"),
                    dst_op=a.get("dst_op"),
                    dst_port=a.get("dst_port"),
                )
                for a in entry["actions"]
            )
            out.append(PriorPipeline(
                name=entry["name"],
                description=entry.get("description", ""),
                datasets=tuple(entry.get("datasets", ())),
                actions=actions,
            ))
        except Exception as exc:
            _log.warning("llm_priors: malformed entry %r (%s) — skipping",
                         entry.get("name", "<unnamed>"), exc)
    return out


def decompose_dag_to_actions(dag_json: dict, catalog_by_op: dict) -> list[PriorAction]:
    """Decompose a full-pipeline DAG into the structural edits that
    would build it from the frozen harness. Used for BK-Tree seeds.

    Skips nodes/edges that already live in the harness (loader, split,
    metric, and their harness-wired Parameters) so the prior only
    covers the agent-side construction.

    Returns an action list in node-then-edges order, followed by a
    commit. Node order is topological over the filtered graph.
    """
    nodes = dag_json.get("nodes", {})
    edges = dag_json.get("edges", [])

    # Which op_keys belong to the frozen harness? The env hardwires:
    harness_op_keys = {
        "dorian.io.dataset",
        "sklearn.model_selection.train_test_split",
        "sklearn.metrics.accuracy_score",
    }

    # Strip harness nodes so the prior only covers the agent-added middle.
    agent_node_ids: set[str] = set()
    node_op_key: dict[str, str] = {}
    for nid, nd in nodes.items():
        if not isinstance(nd, dict):
            continue
        kind = nd.get("class_type", nd.get("type", ""))
        if kind != "Operator":
            continue
        op_key = nd.get("name", "")
        if op_key in harness_op_keys:
            continue
        agent_node_ids.add(nid)
        node_op_key[nid] = op_key

    # AddNode actions — one per agent-added Operator.
    actions: list[PriorAction] = []
    for nid, op_key in node_op_key.items():
        actions.append(PriorAction(kind="add_node", op_key=op_key))

    # AddEdge actions — only edges whose destination is agent-added AND
    # whose source is in the DAG. Skip Parameter→agent_node (covered by
    # AddNode's Parameter-satellite auto-wiring) and harness→harness.
    op_name_for: dict[str, str] = {}
    for nid, nd in nodes.items():
        if not isinstance(nd, dict):
            continue
        kind = nd.get("class_type", nd.get("type", ""))
        if kind == "Operator":
            op_name_for[nid] = nd.get("name", "")
        elif kind == "Parameter":
            op_name_for[nid] = "<Parameter>"
        elif kind == "Snippet":
            op_name_for[nid] = "<Snippet>"

    for e in edges:
        if not isinstance(e, dict):
            continue
        src = e.get("source", "")
        dst = e.get("destination", "")
        if dst not in agent_node_ids and (
            op_name_for.get(dst) not in harness_op_keys
            or op_name_for.get(src) == "<Parameter>"
        ):
            # Purely harness-internal or parameter-satellite wire.
            continue
        src_op = op_name_for.get(src, "")
        dst_op = op_name_for.get(dst, "")
        if src_op == "<Parameter>":
            # Parameter satellites are materialised by AddNode's default
            # wiring, not by a separate AddEdge action.
            continue
        # Resolve port names.
        src_meta = catalog_by_op.get(src_op)
        src_output_index = int(e.get("output", 0) or 0)
        src_port = ""
        if src_meta is not None and src_meta.outputs and 0 <= src_output_index < len(src_meta.outputs):
            src_port = src_meta.outputs[src_output_index].name
        dst_port = str(e.get("position", ""))
        if not src_port or not dst_port:
            continue
        actions.append(PriorAction(
            kind="add_edge",
            src_op=src_op,
            src_port=src_port,
            dst_op=dst_op,
            dst_port=dst_port,
        ))

    if actions:
        actions.append(PriorAction(kind="commit"))
    return actions


def load_bktree_priors(
    catalog_by_op: dict,
    *,
    limit: int = 200,
) -> list[PriorPipeline]:
    """Pull N pipelines from the Postgres ``pipelines`` table and
    decompose each into a prior. Returns an empty list when the
    table is unreachable — first-boot installs without a seeded
    store still train, just without this source of prior."""
    import asyncio

    async def _fetch() -> list[tuple[str, dict]]:
        from backend.envs import get_pg_pool
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, dag FROM pipelines "
                "WHERE provenance IN ('trial-config', 'rl-v2') "
                "ORDER BY created_at DESC LIMIT $1",
                limit,
            )
        out: list[tuple[str, dict]] = []
        for row in rows:
            dag = row["dag"]
            if isinstance(dag, str):
                dag = json.loads(dag)
            out.append((row["id"], dag))
        return out

    try:
        fetched = asyncio.run(_fetch())
    except Exception as exc:
        _log.warning("bktree_priors: fetch failed (%s) — skipping", exc)
        return []

    priors: list[PriorPipeline] = []
    for pid, dag in fetched:
        actions = decompose_dag_to_actions(dag, catalog_by_op)
        if not actions:
            continue
        priors.append(PriorPipeline(
            name=f"bktree:{pid[:8]}",
            description="decomposed from trial-config / rl-v2 seed pipeline",
            datasets=(),
            actions=tuple(actions),
        ))
    return priors


# ---------------------------------------------------------------------------
# Warm-start injection
# ---------------------------------------------------------------------------


def warm_start_policy(
    policy,  # MemoryPolicy, HedgePolicy, or HybridPolicy
    priors: Iterable[PriorPipeline],
    action_space,
    dataset_embeddings: dict[str, tuple[float, ...]],
    *,
    strength: float = 1.0,
) -> dict[str, int]:
    """Credit every prior as a synthetic successful trajectory.

    Walks each prior's action list, resolves abstract keys to
    ``action_id`` via the shared ``ActionSpace`` (inserting new entries
    as needed), and calls the policy's warm-start method (for memory
    policies) or ``credit_synthetic_trajectory`` (for hedge).

    ``dataset_embeddings`` maps dataset name → embedding. Priors with
    ``datasets=()`` are credited against a neutral zero-vector embedding
    so they apply to every dataset weakly; priors targeted at specific
    datasets are credited against each target embedding so the
    cosine-similarity term in the prior computation lights up.
    """
    counts = {"priors": 0, "actions_credited": 0, "datasets_hit": 0}

    # Collect the inner policy handles we need to warm-start.
    memory = getattr(policy, "memory", None)
    hedge = getattr(policy, "hedge", None)
    mem = memory if memory is not None else _as_memory(policy)
    hp = hedge if hedge is not None else _as_hedge(policy)

    for prior in priors:
        # Resolve action_ids in ActionSpace — register each abstract
        # key and grab its persistent id. We don't need a real DAG:
        # abstract_key only uses op_name lookups, and for prior entries
        # we already have those as literal strings in the key shape.
        #
        # Skip the trailing ``commit`` action: every prior ends with
        # it, so crediting it N-times-per-prior makes Commit the
        # highest-prior action for every episode and the agent learns
        # to commit instantly on the bare harness. The structural
        # actions (add_node / add_edge) are the ones worth biasing;
        # Commit is cheap to learn organically from rewards.
        action_ids: list[int] = []
        for pa in prior.actions:
            if pa.kind == "commit":
                continue
            key = pa.abstract_key()
            if key in action_space._abs_to_id:
                action_ids.append(action_space._abs_to_id[key])
            else:
                new_id = len(action_space._abs_to_id)
                action_space._abs_to_id[key] = new_id
                action_space._id_to_abs[new_id] = key
                action_ids.append(new_id)

        # Pick the embeddings to credit against. MemoryPolicy's
        # ``_prior`` scales by cosine similarity to the observed
        # dataset, so a zero-vector would contribute nothing. Credit
        # against every real embedding instead — dataset-agnostic
        # priors fire on every dataset; dataset-targeted priors fire
        # only on their targets.
        targets = prior.datasets or ()
        if targets:
            embs_to_credit = [
                dataset_embeddings[n] for n in targets if n in dataset_embeddings
            ]
        else:
            embs_to_credit = list(dataset_embeddings.values())

        if mem is not None:
            for emb in embs_to_credit:
                mem.credit_synthetic_trajectory(action_ids, emb)
                counts["datasets_hit"] += 1

        if hp is not None:
            hp.credit_synthetic_trajectory(action_ids, strength=strength)

        counts["priors"] += 1
        counts["actions_credited"] += len(action_ids)

    return counts


def _as_memory(policy):
    # MemoryPolicy has credit_synthetic_trajectory; HedgePolicy doesn't
    if hasattr(policy, "credit_synthetic_trajectory") and hasattr(policy, "_stats"):
        return policy
    return None


def _as_hedge(policy):
    if hasattr(policy, "credit_synthetic_trajectory") and hasattr(policy, "_log_weights"):
        return policy
    return None


__all__ = [
    "PriorAction",
    "PriorPipeline",
    "decompose_dag_to_actions",
    "load_bktree_priors",
    "load_llm_priors",
    "warm_start_policy",
]
