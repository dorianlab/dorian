"""
dorian/knowledge/io_crawler.py
------------------------------
Offline crawler that emits ``has_input`` declarations for operators
the curated `.kb` files don't already cover. Output is plain DSL
text — same format as ``dorian/knowledge/sources/*.kb`` — so the
rust snapshot builder ingests it unchanged.

Output path defaults to ``volumes/io_crawler_extras.kb``. Each line
is a single statement:

    sklearn.svm.SVC has_input C; is_of_type any; has_position C
    sklearn.svm.SVC has_input kernel; is_of_type any; has_position kernel

Method:

  1. Read every ``.kb`` source file, parse it (rust-side) and
     collect operator names that already have a ``has_input``
     declaration. Hand-curated entries always win.
  2. For every remaining sklearn / pandas / numpy operator, import
     the dotted path, ``inspect.signature`` it, and emit one
     ``has_input`` declaration per non-self / non-variadic param.
  3. Outputs are NOT auto-crawled — sklearn return types aren't
     accessible via ``inspect``. They stay hand-curated.

Rerun whenever a curated source adds new operators that we want
inputs for. Idempotent — overwriting the extras file is fine.
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import logging
import sys
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUT = _PROJECT_ROOT / "volumes" / "io_crawler_extras.kb"
_SOURCES_DIR = _PROJECT_ROOT / "dorian" / "knowledge" / "sources"


_CRAWLABLE_PREFIXES: tuple[str, ...] = (
    "sklearn.",
    "pandas.",
    "numpy.",
    "matplotlib.",
    "seaborn.",
    "plotly.",
    "scipy.",
)


def _is_crawlable(op_name: str) -> bool:
    if not op_name or "." not in op_name:
        return False
    return any(op_name.startswith(p) for p in _CRAWLABLE_PREFIXES)


def _resolve(op_name: str) -> Any | None:
    parts = op_name.split(".")
    for split in range(len(parts) - 1, 0, -1):
        module_path = ".".join(parts[:split])
        try:
            module = importlib.import_module(module_path)
        except Exception:
            continue
        obj: Any = module
        try:
            for a in parts[split:]:
                obj = getattr(obj, a)
            return obj
        except Exception:
            continue
    return None


def _signature_of(obj: Any) -> inspect.Signature | None:
    try:
        if inspect.isclass(obj):
            return inspect.signature(obj.__init__)
        if callable(obj):
            return inspect.signature(obj)
    except (TypeError, ValueError):
        return None
    return None


def _ports_from_signature(sig: inspect.Signature) -> list[tuple[str, str]]:
    """Project a callable's signature into ``(name, position)`` tuples.

    Position is **always the parameter name** — never a numeric index.
    Numeric positions (``0``, ``1``, …) leak into the SPA's handle
    labels and force end users to remember which slot is which; param
    names carry the same wiring semantics (Python accepts every
    positional arg by name) without that loss.

    Variadic ``*args`` / ``**kwargs`` are skipped — they have no
    fixed position to wire from a canvas. ``self`` / ``cls`` are
    method-implicit, also skipped.
    """
    ports: list[tuple[str, str]] = []
    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        ports.append((name, name))
    return ports


def _curated_snapshot() -> dict:
    """Load the curated ``.kb`` sources via the rust builder.

    We need the snapshot to know which operators (a) exist and (b)
    already declare ``has_input``. Both come from one parse pass.
    """
    import dorian_native  # type: ignore

    sources: list[tuple[str, str]] = []
    for path in sorted(_SOURCES_DIR.glob("*.kb")):
        sources.append((str(path), path.read_text()))
    return json.loads(dorian_native.kb_build_snapshot(sources))["snapshot"]


def crawl(*, out_path: Path = _DEFAULT_OUT, dry_run: bool = False) -> dict[str, int]:
    """Emit synthetic ``has_input`` declarations to ``out_path``.

    Returns a stats dict (``scanned`` / ``already_declared`` /
    ``ports_added`` / ``import_failed`` / ``signature_failed``).
    """
    stats = {
        "scanned": 0,
        "already_declared": 0,
        "ports_added": 0,
        "import_failed": 0,
        "signature_failed": 0,
    }

    snap = _curated_snapshot()
    operators = snap.get("operators", {})

    candidates = [n for n in operators if _is_crawlable(n)]
    stats["scanned"] = len(candidates)
    # Skip operators that already have a curated ``has_input`` AND
    # operators whose data ports come from an interface (Sklearn
    # Estimator / Transformer / ...). For class operators with an
    # interface, ``__init__`` parameters are *configuration* — already
    # captured via ``has_parameter`` — not data inputs. Crawling them
    # here used to surface 19 hyperparameters as fake input handles
    # on the canvas (``n_estimators``, ``criterion``, ...) instead of
    # the interface-declared X / y.
    targets = [
        n for n in candidates
        if not operators[n].get("inputs") and not operators[n].get("interface")
    ]
    stats["already_declared"] = len(candidates) - len(targets)

    lines: list[str] = []
    for name in targets:
        obj = _resolve(name)
        if obj is None:
            stats["import_failed"] += 1
            continue
        sig = _signature_of(obj)
        if sig is None:
            stats["signature_failed"] += 1
            continue
        ports = _ports_from_signature(sig)
        for port_name, position in ports:
            # DSL uses space-separated predicate names — ``has input``,
            # ``is of type``, ``has position``. Underscored forms are
            # only the parser's internal canonicalised label.
            lines.append(
                f"{name} has input {port_name}; is of type any; has position {position}"
            )
            stats["ports_added"] += 1

    if dry_run:
        _log.info("[io-crawler] dry-run: %d ports", stats["ports_added"])
        return stats

    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Auto-generated by dorian.knowledge.io_crawler.\n"
        "# Curated ``.kb`` sources take precedence — only operators\n"
        "# without an explicit ``has_input`` are filled in here.\n"
        "# Re-run: ``python -m dorian.knowledge.io_crawler``.\n\n"
    )
    out_path.write_text(header + "\n".join(lines) + "\n")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        type=str,
        default=str(_DEFAULT_OUT),
        help=f"Output path. Default: {_DEFAULT_OUT}",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write the file; just print stats.",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    stats = crawl(out_path=Path(args.out), dry_run=args.dry_run)
    print(
        f"[io-crawl] scanned={stats['scanned']} "
        f"already_declared={stats['already_declared']} "
        f"ports_added={stats['ports_added']} "
        f"import_failed={stats['import_failed']} "
        f"signature_failed={stats['signature_failed']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
