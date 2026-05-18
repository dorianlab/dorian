"""A4 ablation demo -- cache_affinity signal across rollouts.

Simulates the RL trainer's inner loop to show how the affinity
scalar used for action priors evolves as episodes commit their
pedigrees to the shared ExperimentGraph:

  1. Pull N candidate pipelines from the document-store corpus (stand-in
     for RL-sampled candidates on the same dataset).
  2. Episode 1: commit one random candidate to the index, measure
     mean affinity across the remaining N-1 candidates.
  3. Episodes 2..K: commit one more each time, re-measure.
  4. Plot (text-mode) affinity vs committed-episode count.

The curve rising from ~0 toward 1 is the signal the RL logit-nudge
would ride -- actions whose downstream pipelines have higher
affinity get a small positive bias, so the agent is gently steered
toward reuse without collapsing diversity (epsilon ~= 0.1).

Usage:
    uv run python -m rl.bench.a4_demo [--candidates 50] [--episodes 10]

See internal design note section "4. RL Agent" -> Cache-affinity
logit nudge.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from statistics import mean

from rl.exec import ExperimentGraph

DEFAULT_CORPUS = "backups/20260419_180008/docstore/pipelines.json"


def load_n_random_pipelines(path: Path, n: int, *, seed: int = 0) -> list[str]:
    rng = random.Random(seed)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = [data]
    pool: list[str] = []
    for doc in data:
        if "nodes" in doc:
            pool.append(json.dumps(doc))
        elif isinstance(doc.get("pipeline"), dict):
            pool.append(json.dumps(doc["pipeline"]))
    rng.shuffle(pool)
    return pool[:n]


def run(corpus: Path, n_candidates: int, n_episodes: int, seed: int) -> None:
    pipes = load_n_random_pipelines(corpus, n_candidates, seed=seed)
    if not pipes:
        raise SystemExit(f"no pipelines parsed from {corpus}")
    print(f"Loaded {len(pipes)} random candidates from {corpus}")

    eg = ExperimentGraph()

    def mean_affinity(sample: list[str]) -> float:
        if not sample:
            return 0.0
        return mean(eg.affinity(p) for p in sample)

    # Initial affinity before any commits.
    baseline = mean_affinity(pipes)
    print(f"\nBefore any commit, mean affinity = {baseline:.3f}")

    print("\nEpisode | committed | entries | mean_affinity_remaining")
    print("--------|-----------|---------|------------------------")
    committed = 0
    for ep in range(1, n_episodes + 1):
        # Commit one candidate per episode; use the rest as the pool
        # whose mean affinity we measure.
        dag = pipes[ep - 1]
        eg.commit(dag, artifact="feature", compute_secs=0.25)
        committed += 1
        remaining = pipes[ep:] if ep < len(pipes) else []
        aff = mean_affinity(remaining)
        print(
            f"  {ep:>4} | {committed:>9} | {len(eg):>7} |"
            f" {aff:.3f}"
        )

    # Batch projection over the full candidate set.
    proj = eg.plan_batch(pipes)
    print(
        "\nBatch plan over full candidate set against the populated index:"
    )
    print(f"  pipelines:       {proj.pipelines}")
    print(f"  naive firings:   {proj.naive_fire_count}")
    print(f"  unique firings:  {proj.unique_fire_count}")
    print(f"  index hits:      {proj.index_hits}")
    print(f"  collapse ratio:  {proj.collapse_ratio:.3f}")
    print(f"  implied speedup: {proj.implied_speedup:.2f}x")


def main() -> None:
    p = argparse.ArgumentParser(description="A4 cache_affinity signal demo")
    p.add_argument("--corpus", default=DEFAULT_CORPUS)
    p.add_argument("--candidates", type=int, default=50)
    p.add_argument("--episodes", type=int, default=10)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()
    corpus = Path(args.corpus)
    if not corpus.is_file():
        raise SystemExit(f"corpus not found: {corpus}")
    run(corpus, args.candidates, args.episodes, args.seed)


if __name__ == "__main__":
    main()
