"""dorian.rl — next-gen RL pipeline generator.

Built on top of the new Rust DEM engine (`engine/`) and the event-bus
overhaul. See (internal design note; not in public repo) for design rationale and the
ablation matrix that motivates the module layout.

The thesis baseline (Faizan, 2026-01-01) lives in
``rl/bench/baseline.py`` (vendored, untouched) for ablation A0; the v2
implementation lives in the sibling subpackages.
"""
