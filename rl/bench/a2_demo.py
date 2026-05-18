"""A2 ablation demo -- engine swap + cache batch-planning.

Runs the Rust cache's batch planner over real pipelines from the
document-store corpus and prints the collapse statistics. Mirrors what the
v2 RL training loop will do at rollout time: generate N candidates,
project their shared compute via `BatchRunner.plan`, feed the
`implied_speedup` into the wall-clock cost estimate.

Usage:
    uv run python -m rl.bench.a2_demo [--limit N] [--corpus PATH]

The demo is independent of any RL policy -- it isolates the
engine-level win that A2 measures. Pair with A0 (thesis baseline,
unchanged) to attribute wall-clock gains to the new engine vs the
policy updates.

See internal design note § Ablation matrix rows A0/A2.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from statistics import mean, median

from rl.exec import BatchRunner, DemSummary, dem_summary


DEFAULT_CORPUS = (
    "backups/20260419_180008/docstore/pipelines.json"
)


def load_pipelines(path: Path, limit: int | None = None) -> list[str]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = [data]
    pipes: list[str] = []
    for doc in data:
        if "nodes" in doc:
            pipes.append(json.dumps(doc))
        elif isinstance(doc.get("pipeline"), dict):
            pipes.append(json.dumps(doc["pipeline"]))
        if limit is not None and len(pipes) >= limit:
            break
    return pipes


def summarise_per_pipeline(pipes: list[str]) -> list[DemSummary]:
    return [dem_summary(p) for p in pipes]


def print_dem_histogram(summaries: list[DemSummary]) -> None:
    node_counts = [s.node_count for s in summaries]
    cacheable = [s.cacheable_fraction for s in summaries]
    nondet = [s.non_deterministic_count for s in summaries]
    de = [s.de_count for s in summaries]
    print("Per-pipeline DEM distribution:")
    print(f"  nodes         min={min(node_counts)} "
          f"med={median(node_counts):.0f} mean={mean(node_counts):.1f} "
          f"max={max(node_counts)}")
    print(f"  cacheable%    min={min(cacheable):.1%} "
          f"med={median(cacheable):.1%} mean={mean(cacheable):.1%}")
    print(f"  non-det count min={min(nondet)} "
          f"med={median(nondet):.0f} mean={mean(nondet):.2f} "
          f"max={max(nondet)}")
    print(f"  DE count      min={min(de)} max={max(de)}")


def run(corpus: Path, limit: int | None) -> None:
    t0 = time.time()
    pipes = load_pipelines(corpus, limit=limit)
    dt_load = time.time() - t0
    print(f"Loaded {len(pipes)} pipelines from {corpus} "
          f"({dt_load*1000:.0f} ms)")

    t0 = time.time()
    summaries = summarise_per_pipeline(pipes)
    dt_sum = time.time() - t0
    print(f"Parsed + classified {len(summaries)} pipelines "
          f"({dt_sum*1000:.0f} ms, {dt_sum*1e6/max(1,len(summaries)):.0f} us/pipeline)")
    print_dem_histogram(summaries)

    print()
    print("=== Batch projection ===")
    t0 = time.time()
    runner = BatchRunner()
    proj = runner.plan(pipes)
    dt_plan = time.time() - t0
    print(f"BatchRunner.plan({len(pipes)})  -> {dt_plan*1000:.0f} ms")
    print(f"  naive_fire_count:   {proj.naive_fire_count:>8,}")
    print(f"  unique_fire_count:  {proj.unique_fire_count:>8,}")
    print(f"  collapsed_firings:  {proj.collapsed_firings:>8,}")
    print(f"  collapse_ratio:     {proj.collapse_ratio:>8.3f}")
    print(f"  implied_speedup:    {proj.implied_speedup:>8.2f}x")

    print()
    print("A2 reading:")
    print(f"  With zero prior cache state, collapsing shared firings")
    print(f"  across a batch of {len(pipes):,} AutoSklearn-shaped pipelines")
    print(f"  saves {proj.collapsed_firings:,} of {proj.naive_fire_count:,} firings -- a {proj.implied_speedup:.2f}x")
    print(f"  net compute reduction before any policy changes.")
    print(f"  RL-generated batches with tighter structural overlap")
    print(f"  (shared loader + scaler + train/test split) should push this")
    print(f"  substantially higher; A2 full measurement does the policy run.")


def main() -> None:
    p = argparse.ArgumentParser(description="A2 ablation demo -- batch planner over real corpus")
    p.add_argument("--corpus", default=DEFAULT_CORPUS, help="Path to pipelines.json dump")
    p.add_argument("--limit", type=int, default=None, help="Max pipelines to load")
    args = p.parse_args()
    corpus = Path(args.corpus)
    if not corpus.is_file():
        raise SystemExit(f"corpus not found: {corpus}")
    run(corpus, args.limit)


if __name__ == "__main__":
    main()
