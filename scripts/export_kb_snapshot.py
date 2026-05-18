#!/usr/bin/env python3
"""
scripts/export_kb_snapshot.py
-----------------------------
Build the JSON KB snapshot the rust runtime consumes.

Inputs:
- ``dorian/knowledge/sources/*.kb`` — curated DSL text.
- ``volumes/io_crawler_extras.kb`` (optional) — synthetic ports
  emitted by the offline io-crawler (sklearn / pandas / numpy
  signatures the curated KB doesn't declare).
- ``expdb.kb_overlay`` (optional) — runtime-curated, validated
  statements waiting to be promoted into a curated source.

Output:
- ``volumes/kb_snapshot.json`` (or ``--out`` override).
- Parser errors collected and printed; a non-zero exit when any
  curated source contains an unparsable statement.

Implementation notes:
- Parsing + builder are rust-side
  (``dorian_native.kb_build_snapshot``). This file is glue: collect
  inputs, hand them off, write JSON.
- ``--no-overlay`` skips the postgres merge for deterministic CI
  builds.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _curated_sources() -> List[Tuple[str, str]]:
    """Read every ``.kb`` file under ``dorian/knowledge/sources/``."""
    sources_dir = _PROJECT_ROOT / "dorian" / "knowledge" / "sources"
    out: List[Tuple[str, str]] = []
    for path in sorted(sources_dir.glob("*.kb")):
        out.append((str(path), path.read_text()))
    return out


def _io_crawler_extras() -> List[Tuple[str, str]]:
    """Return the io-crawler extras file if it exists, else []."""
    extras_path = _PROJECT_ROOT / "volumes" / "io_crawler_extras.kb"
    if extras_path.is_file() and extras_path.stat().st_size > 0:
        return [(str(extras_path), extras_path.read_text())]
    return []


def _validated_overlay_statements() -> List[Tuple[str, str]]:
    """Pull validated/promoted DSL statements from ``expdb.kb_overlay``.

    Returns one virtual source pair ``("<overlay>", text)`` so the
    rust parser surfaces overlay-origin parse errors with that label.
    Failure is non-fatal: empty list keeps the build deterministic
    when postgres is unreachable.
    """
    try:
        import asyncio
        from dorian.knowledge import overlay
        statements = asyncio.run(overlay.list_validated_statements())
    except Exception as exc:  # noqa: BLE001
        print(f"overlay merge skipped: {exc}", file=sys.stderr)
        return []
    if not statements:
        return []
    return [("<overlay>", "\n".join(statements))]


def _emit_snapshot(*, with_overlay: bool = True) -> dict:
    """Build the KB snapshot dict in-memory (no file I/O).

    Used by ``main.py``'s lifespan when ``DORIAN_KB_SNAPSHOT`` points at
    a missing file — we generate one on the fly so the gateway has
    something to read at boot. The CLI ``main()`` below wraps this and
    writes the result to disk.

    Raises on curated-source parse errors so the caller can choose
    whether to abort startup or continue with the previous snapshot.
    Overlay errors are surfaced as warnings only — the same policy
    the CLI uses (a single bad community statement can't block boot).
    """
    import dorian_native  # type: ignore  -- lazy import so help still works

    sources: List[Tuple[str, str]] = []
    sources.extend(_curated_sources())
    sources.extend(_io_crawler_extras())
    if with_overlay:
        sources.extend(_validated_overlay_statements())

    payload = json.loads(dorian_native.kb_build_snapshot(sources))
    snap = payload["snapshot"]
    errors = payload.get("errors", [])
    curated_errors = [e for e in errors if e.get("source") != "<overlay>"]
    if curated_errors:
        first = curated_errors[0]
        raise RuntimeError(
            f"KB snapshot build failed: {len(curated_errors)} curated parse "
            f"error(s); first: {Path(first['source']).name}:{first['line_no']} "
            f"-> {first['message']}"
        )
    return snap


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        type=str,
        default="volumes/kb_snapshot.json",
        help="Output path. Default: volumes/kb_snapshot.json (shared volume).",
    )
    ap.add_argument(
        "--no-overlay",
        action="store_true",
        help="Skip the postgres ``kb_overlay`` merge.",
    )
    args = ap.parse_args()

    import dorian_native  # type: ignore  -- lazy import so help still works

    sources: List[Tuple[str, str]] = []
    sources.extend(_curated_sources())
    sources.extend(_io_crawler_extras())
    if not args.no_overlay:
        sources.extend(_validated_overlay_statements())

    payload = json.loads(dorian_native.kb_build_snapshot(sources))
    snap = payload["snapshot"]
    errors = payload.get("errors", [])

    # Surface every parse error. Non-zero exit if any line in a
    # *curated* source fails — the overlay path tolerates failures
    # so a single bad community submission can't block the build.
    if errors:
        print(f"-- {len(errors)} parse error(s):", file=sys.stderr)
        curated_failures = 0
        for e in errors:
            label = e["source"]
            short = Path(label).name if label != "<overlay>" else label
            is_overlay = label == "<overlay>"
            tag = "(overlay)" if is_overlay else "(curated)"
            print(
                f"   {tag} {short}:{e['line_no']}  {e['line']}  -> {e['message']}",
                file=sys.stderr,
            )
            if not is_overlay:
                curated_failures += 1
        if curated_failures:
            return 2

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snap, indent=2, sort_keys=True))
    print(
        f"wrote KB snapshot: {out_path} "
        f"({len(snap['operators'])} operators, "
        f"{len(snap['interfaces'])} interfaces, "
        f"{len(snap['mitigations'])} mitigations, "
        f"{len(snap['pathways'])} pathways)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
