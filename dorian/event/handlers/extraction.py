"""
dorian/event/handlers/extraction.py
-----------------------------------
WebSocket event handlers for pipeline extraction.

Handles:
- ExtractionCorrected: user fixes a wrongly-extracted pipeline
- ExtractPipeline: run AST extraction on Python source code
- SaveExtractionRules: persist custom rewrite rules for a user
- LoadExtractionRules: fetch the user's latest saved rules (or defaults)
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import traceback
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from backend.events import Event, aemit
from backend.envs import aioredis, expdb
from dorian.code.parsing.parser import parse as _parse_code_py
from dorian.code.parsing.rules import (
    _compute_rules_hash,
    get_rules,
    get_rules_version,
)
from dorian.code.parsing.rule import RewriteRule
from dorian.dag import DAG, Node, Operator, Parameter, Snippet
from dorian.infra.keys import RedisKeys

from dorian.event.helpers.lifecycle import _xadd


def parse_code(code, language, rules):
    """Run the python extractor on ``code``.

    Pipeline extraction is rust-native via
    ``engine/backend/src/handlers/extraction.rs``; the python path
    that this function backs survives only as a fallback for
    legacy callers (the regression-replay tool, the rule-suggestion
    flow's compat checks, the HTTP file-upload route). New code
    should not call this — emit ``ExtractPipeline`` on the event
    bus and let the rust handler own it.
    """
    return _parse_code_py(code, language, rules)


def _dag_to_frontend_format(dag: DAG) -> dict:
    # Pre-compute port specs from edges so the frontend can derive handles
    # without relying on a reactive edge subscription (timing-safe).
    outputs_by_node: dict[str, set] = {}
    inputs_by_node: dict[str, dict] = {}
    for edge in dag.edges:
        src = str(edge.source)
        dst = str(edge.destination)
        outputs_by_node.setdefault(src, set()).add(edge.output)
        inputs_by_node.setdefault(dst, {})[edge.position] = True

    nodes: dict[str, dict] = {}
    for nid, node in dag.nodes.items():
        nid_str = str(nid)
        cls = node.__class__.__name__
        entry: dict = {
            "type": cls,
            "name": getattr(node, "name", getattr(node, "text", nid)),
        }
        if isinstance(node, Parameter):
            entry["value"] = node.value
            entry["dtype"] = node.dtype
        elif isinstance(node, Snippet):
            entry["code"] = node.code
            entry["language"] = node.language
        elif isinstance(node, Node):
            entry["type"] = "Operator"
            entry["name"] = node.text or node.type

        # Include port specs so useNodeHandles uses IO-spec-driven handles
        # (avoids the edge-subscription timing race on first render).
        out_ports = sorted(outputs_by_node.get(nid_str, set()))
        entry["outputs"] = [{"name": str(p)} for p in out_ports]

        in_positions = sorted(
            inputs_by_node.get(nid_str, {}).keys(),
            key=lambda x: (isinstance(x, str), x),  # ints first, then kwarg strs
        )
        entry["inputs"] = [{"name": str(p)} for p in in_positions]

        nodes[nid_str] = entry

    edges = [e.to_dict() for e in dag.edges]
    return {"uuid": str(uuid4()), "nodes": nodes, "edges": edges}


async def _persist_extraction(
    eid: str,
    code: str,
    lang: str,
    rv: str,
    initial: DAG,
    final: DAG,
    session: str | None,
    uid: str | None,
    fname: str | None,
) -> None:
    try:
        from dorian.code.extraction_store import persist_extraction

        await persist_extraction(
            eid, code, lang, rv, initial, final, session, uid, fname,
        )
    except Exception as exc:
        await aemit(Event("ExtractionPersistenceFailed", {"error": repr(exc)}))


def _extract_rules_list(source: str) -> str:
    import ast as _ast
    try:
        tree = _ast.parse(source)
        lines = source.splitlines()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef) and node.name == "get_rules":
                for stmt in node.body:
                    if isinstance(stmt, _ast.Return) and stmt.value is not None:
                        start_line = stmt.value.lineno - 1
                        start_col = stmt.value.col_offset
                        end_line = stmt.value.end_lineno
                        extracted = lines[start_line:end_line]
                        extracted[0] = extracted[0][start_col:]
                        return "\n".join(extracted)
    except Exception:
        pass
    return source


def _safe_json(value: dict) -> str:
    return json.dumps(value, default=str)


def _validate_custom_rules(content: str) -> tuple[bool, str, str | None]:
    try:
        rules = get_rules(custom_list_src=content)
        is_valid = isinstance(rules, list) and len(rules) > 0 and all(
            isinstance(r, RewriteRule) for r in rules
        )
        if not is_valid:
            return False, "", "Custom rules are invalid"
        return True, _compute_rules_hash(rules), None
    except Exception as exc:
        return False, "", str(exc)


async def handle_extraction_corrected(
    event, *, uid, session, payload, request_id, ts,
):
    """Persist a user-corrected pipeline back to the extraction record."""
    extraction_id = payload.get("extractionId")
    corrected_pipeline = payload.get("correctedPipeline")

    if not extraction_id:
        await aemit(Event("ExtractionCorrectedMissingId", {"uid": uid, "session": session}))
        return
    if not corrected_pipeline:
        await aemit(Event("ExtractionCorrectedMissingPipeline", {"extraction_id": extraction_id, "uid": uid}))
        return

    corrected_dag_json = {
        "nodes": corrected_pipeline.get("nodes", {}),
        "edges": corrected_pipeline.get("edges", []),
    }

    try:
        from dorian.code.extraction_store import record_correction
        await record_correction(extraction_id, corrected_dag_json)
        await aemit(Event("ExtractionCorrectionPersisted", {"extraction_id": extraction_id, "uid": uid}))
    except Exception:
        await aemit(Event("ExtractionCorrectionFailed", {"extraction_id": extraction_id, "uid": uid}))


async def handle_extract_pipeline(
    event, *, uid, session, payload, request_id, ts,
):
    d = {**event.data, **payload}
    code = d.get("code")
    language = d.get("language", "python")
    filename = d.get("filename")

    if not uid or not session:
        await aemit(Event("ExtractionEnvelopeInvalid", data={"reason": "missing uid/session"}))
        return
    if not code:
        await _xadd(uid, session, {
            "event": "extraction/error",
            "value": _safe_json({"error": "Missing code"}),
        })
        return

    extraction_id = str(uuid4())
    await aemit(Event("ExtractionStarted", {
        "uid": uid, "session": session, "filename": filename, "code_len": len(code),
    }))

    # Format-aware resolver: fetch Python and JSON rules independently
    py_doc = await expdb.extraction_rule_versions.find_one(
        {"uid": uid, "isValid": True, "$or": [{"format": "python_rules"}, {"format": {"$exists": False}}]},
        sort=[("createdAt", -1)],
    )
    json_doc = await expdb.extraction_rule_versions.find_one(
        {"uid": uid, "isValid": True, "format": "json_specs"},
        sort=[("createdAt", -1)],
    )

    custom_rules: list = []
    py_src = py_doc.get("content") if py_doc else None
    rules = await asyncio.to_thread(get_rules, py_src)

    if json_doc and json_doc.get("content"):
        try:
            from dorian.mcp.rule_compiler import compile_rule as _compile
            specs = json.loads(json_doc["content"])
            custom_rules = [r for spec in specs
                            for r in [_compile(spec)[0]] if r]
            rules = rules + custom_rules
        except (json.JSONDecodeError, Exception) as exc:
            await aemit(Event("ExtractionJsonRulesLoadFailed", {"error": str(exc)}))

    has_custom = bool(py_src) or bool(custom_rules)
    rules_version = _compute_rules_hash(rules) if has_custom else get_rules_version()
    await aemit(Event("ExtractionRulesLoaded", {
        "count": len(rules), "version": rules_version,
        "python_custom": bool(py_src), "json_custom_count": len(custom_rules),
    }))

    try:
        initial_dag, final_dag = await asyncio.wait_for(
            asyncio.to_thread(parse_code, code, language, rules),
            timeout=120,
        )
        # Log extraction result for rule suggestion debugging
        from dorian.code.rule_debug_log import log_reextract
        log_reextract(
            f"extract_{extraction_id[:8]}",
            uid, len(rules), len(custom_rules),
            len(initial_dag.edges), len(final_dag.edges),
            dag_after=final_dag.to_json_dict(),
        )
    except asyncio.TimeoutError:
        await aemit(Event("ExtractionParseTimeout", {}))
        await _xadd(uid, session, {
            "event": "extraction/error",
            "value": _safe_json({"error": "Extraction timed out (>120s). Try simplifying the code."}),
        })
        return
    except Exception as exc:
        await aemit(Event("ExtractionParseError", {"error": str(exc)}))
        await _xadd(uid, session, {
            "event": "extraction/error",
            "value": _safe_json({
                "error": f"Parse error: {exc}",
                "trace": traceback.format_exc(),
            }),
        })
        return

    asyncio.create_task(_persist_extraction(
        extraction_id, code, language, rules_version,
        initial_dag, final_dag, session, uid, filename,
    ))

    if session:
        await aioredis.set(RedisKeys.active_extraction(session), extraction_id)

    result = _dag_to_frontend_format(final_dag)
    result["extractionId"] = extraction_id
    result["rulesVersion"] = rules_version

    await _xadd(uid, session, {
        "event": "extraction/result",
        "value": _safe_json(result),
    })

    # Piggyback the current rules onto the extraction response so the
    # frontend's ExtractionView gets rules + pipeline in one round-trip
    # instead of firing a separate LoadExtractionRules. The docs are
    # already in memory from the load above — no extra DB hit.
    has_legacy = bool(py_src)
    await _xadd(uid, session, {
        "event": "extraction/rules",
        "value": _safe_json(
            {
                "content": json_doc["content"],
                "source": "user",
                "format": "json_specs",
            }
            if json_doc and json_doc.get("content")
            else {
                "content": "[]",
                "source": "user" if has_legacy else "default",
                "format": "json_specs",
                "hasLegacyPythonRules": has_legacy,
            }
        ),
    })

    await aemit(Event("ExtractionDone", {
        "extraction_id": extraction_id, "nodes": len(final_dag.nodes), "edges": len(final_dag.edges),
    }))


async def handle_save_rules(
    event, *, uid, session, payload, request_id, ts,
):
    d = {**event.data, **payload}
    content = d.get("content", "")
    filename = d.get("filename")
    skip_compat = bool(d.get("skipCompatCheck", False))

    if not uid or not session:
        await aemit(Event("ExtractionEnvelopeInvalid", data={"reason": "missing uid/session"}))
        return
    if not isinstance(content, str) or not content.strip():
        await _xadd(uid, session, {
            "event": "extraction/rules-saved",
            "value": _safe_json({"status": "error", "error": "Missing rules content"}),
        })
        return

    is_valid, rules_hash, error_message = _validate_custom_rules(content)

    # Backward-compat gate: replay the extraction corpus against the
    # candidate rules list. If any past extraction regresses, block the
    # save and surface the regressions to the frontend so the user can
    # decide: abandon the change, edit it, or override explicitly via
    # skipCompatCheck=true (the audit trail captures the override).
    #
    # Skipped when: the syntax is already invalid (can't run at all),
    # the user explicitly overrode, or the corpus is empty (first run).
    if is_valid and not skip_compat:
        try:
            from dorian.mcp.agent_tools import rules_validate_backward_compat
            report = await rules_validate_backward_compat(content)
        except Exception as exc:
            report = {
                "ok": True,
                "corpus_size": 0, "checked": 0, "elapsed_ms": 0.0,
                "regressions": [],
                "errors": [f"compat check crashed: {exc}"],
            }
        if not report.get("ok") and report.get("regressions"):
            await aemit(Event("ExtractionRulesCompatRegressed", {
                "uid": uid, "session": session, "filename": filename,
                "rulesHash": rules_hash,
                "regression_count": len(report["regressions"]),
                "corpus_size": report.get("corpus_size", 0),
                "checked": report.get("checked", 0),
                "elapsed_ms": report.get("elapsed_ms", 0.0),
            }))
            await _xadd(uid, session, {
                "event": "extraction/rules-compat-regressions",
                "value": _safe_json({
                    "status": "blocked",
                    "reason": "backward_compat",
                    "rulesHash": rules_hash,
                    "regressions": report["regressions"],
                    "corpus_size": report.get("corpus_size", 0),
                    "checked": report.get("checked", 0),
                    "elapsed_ms": report.get("elapsed_ms", 0.0),
                    "capped": report.get("capped", False),
                }),
            })
            return

    doc = {
        "uid": uid,
        "session": session,
        "filename": filename,
        "content": content,
        "format": "python_rules",
        "rulesHash": rules_hash,
        "createdAt": datetime.now(timezone.utc),
        "isValid": is_valid,
        "compatOverride": bool(skip_compat and is_valid),
    }
    await aemit(Event("ExtractionRulesSaving", {
        "uid": uid, "session": session, "filename": filename,
        "rulesHash": rules_hash, "isValid": is_valid, "content_len": len(content),
        "compatOverride": doc["compatOverride"],
    }))
    await expdb.extraction_rule_versions.insert_one(doc)

    await _xadd(uid, session, {
        "event": "extraction/rules-saved",
        "value": _safe_json({
            "status": "ok" if is_valid else "invalid",
            "isValid": is_valid,
            "rulesHash": rules_hash,
            "error": error_message,
            "compatOverride": doc["compatOverride"],
        }),
    })


async def handle_save_rule_specs(
    event, *, uid, session, payload, request_id, ts,
):
    """Persist the user's full JSON-spec rules list.

    Unlike ``handle_save_rules`` (which takes Python source that was
    hand-edited in the Monaco pane), this handler takes a list of
    validated JSON rule specs from the card UI. It's the canonical
    path now — new manual authoring + LLM-accept both funnel here.
    """
    d = {**event.data, **payload}
    specs = d.get("specs")
    filename = d.get("filename")
    skip_compat = bool(d.get("skipCompatCheck", False))

    if not uid or not session:
        await aemit(Event("ExtractionEnvelopeInvalid", data={"reason": "missing uid/session"}))
        return
    if not isinstance(specs, list):
        await _xadd(uid, session, {
            "event": "extraction/rules-saved",
            "value": _safe_json({"status": "error", "error": "Missing specs list"}),
        })
        return

    # Validate each spec individually (ReDoS + depth + size guards)
    from dorian.mcp.rule_schema import validate_rule_spec as _validate_spec
    validated: list[dict] = []
    errors: list[dict] = []
    for idx, spec in enumerate(specs):
        v, errs = _validate_spec(spec)
        if errs:
            errors.append({"index": idx, "errors": errs})
        else:
            validated.append(v)
    if errors:
        await _xadd(uid, session, {
            "event": "extraction/rules-saved",
            "value": _safe_json({
                "status": "invalid",
                "error": "Schema validation failed",
                "regressions_per_spec": errors,
            }),
        })
        return

    # Backward-compat: compile each spec then simulate a replay.
    # For JSON-specs we can't easily round-trip through get_rules()
    # since that expects Python source. Inline the replay against
    # the corpus using the compiled RewriteRule objects directly.
    if not skip_compat:
        try:
            from dorian.code.extraction_store import get_regression_set
            from dorian.code.parsing.parser import parse as parse_code
            from dorian.code.parsing.rules import get_rules as _default_rules
            from dorian.mcp.rule_compiler import compile_rule as _compile
            from dorian.mcp.dag_tools import semantic_dag_diff

            compiled = []
            for s in validated:
                rule, errs, _warn = _compile(s)
                if rule is not None and not errs:
                    compiled.append(rule)
            base = _default_rules()
            effective = list(base) + compiled

            records = (await get_regression_set())[-500:]
            regressions: list[dict] = []
            for rec in records:
                accepted = rec.get("correctedDag") or rec.get("autoDag")
                if not accepted:
                    continue
                try:
                    dag = await parse_code(rec["code"], rec.get("language", "python"), effective)
                except Exception:
                    regressions.append({
                        "extraction_id": rec["_id"],
                        "diff_summary": "parse failure under candidate rules",
                        "missing_ops": [],
                        "extra_ops": [],
                    })
                    continue
                replayed = dag.to_json_dict()
                diff = semantic_dag_diff(replayed, accepted)
                if diff.get("summary") and "0 differences" not in diff.get("summary", ""):
                    regressions.append({
                        "extraction_id": rec["_id"],
                        "diff_summary": diff.get("summary"),
                        "missing_ops": diff.get("missing_operators", []),
                        "extra_ops": diff.get("extra_operators", []),
                    })

            if regressions:
                await _xadd(uid, session, {
                    "event": "extraction/rules-compat-regressions",
                    "value": _safe_json({
                        "status": "blocked",
                        "reason": "backward_compat",
                        "regressions": regressions,
                        "corpus_size": len(records),
                        "checked": len(records),
                    }),
                })
                return
        except Exception as exc:
            await aemit(Event("RuleSpecsCompatCheckFailed", {"error": str(exc)}))

    content = json.dumps(validated)
    rules_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
    doc = {
        "uid": uid,
        "session": session,
        "filename": filename,
        "content": content,
        "format": "json_specs",
        "rulesHash": rules_hash,
        "createdAt": datetime.now(timezone.utc),
        "isValid": True,
        "source": "manual_edit",
        "compatOverride": bool(skip_compat),
    }
    await expdb.extraction_rule_versions.insert_one(doc)

    await _xadd(uid, session, {
        "event": "extraction/rules-saved",
        "value": _safe_json({
            "status": "ok",
            "isValid": True,
            "rulesHash": rules_hash,
            "compatOverride": doc["compatOverride"],
            "count": len(validated),
        }),
    })


async def handle_load_rules(
    event, *, uid, session, payload, request_id, ts,
):
    if not uid or not session:
        await aemit(Event("ExtractionEnvelopeInvalid", data={"reason": "missing uid/session"}))
        return

    # Prefer the canonical json_specs format (from card UI or accepted
    # LLM suggestions). Fall back to the legacy python_rules format and
    # finally to the repo default rules source.
    json_doc = await expdb.extraction_rule_versions.find_one(
        {"uid": uid, "isValid": True, "format": "json_specs"},
        sort=[("createdAt", -1)],
    )
    if json_doc and json_doc.get("content"):
        await _xadd(uid, session, {
            "event": "extraction/rules",
            "value": _safe_json({
                "content": json_doc["content"],
                "source": "user",
                "format": "json_specs",
            }),
        })
        return

    # No json_specs saved → emit empty json_specs list. The card UI
    # renders the "add your first rule" empty state.
    #
    # Note: a user may still have a legacy ``python_rules`` doc saved
    # from before the card UI shipped. It's intentionally NOT returned
    # here because rendering Python rules in Monaco is the retired
    # surface; however it IS still honoured at extraction time
    # (``handle_extract_pipeline`` loads both formats and merges them).
    # A migration button — compile python_rules into json_specs — is a
    # follow-up; the legacy doc remains in the docstore so nothing is lost.
    py_doc = await expdb.extraction_rule_versions.find_one(
        {"uid": uid, "isValid": True, "$or": [{"format": "python_rules"}, {"format": {"$exists": False}}]},
        sort=[("createdAt", -1)],
    )
    has_legacy = bool(py_doc and py_doc.get("content"))

    await _xadd(uid, session, {
        "event": "extraction/rules",
        "value": _safe_json({
            "content": "[]",
            "source": "user" if has_legacy else "default",
            "format": "json_specs",
            "hasLegacyPythonRules": has_legacy,
        }),
    })


async def _record_feedback(extraction_id: str, rules_version: str) -> None:
    """Fire-and-forget: record negative feedback for a rule set + extraction."""
    try:
        await expdb.rule_extraction_feedback.insert_one({
            "extractionId": extraction_id,
            "rulesVersion": rules_version,
            "signal": "negative",
            "createdAt": datetime.now(timezone.utc),
        })
    except Exception:
        await aemit(Event("ExtractionFeedbackFailed", {}))


MAX_RULES = 35


def _build_rules_summary() -> str:
    """Build a human-readable summary of active rules for the LLM prompt."""
    try:
        rules = get_rules()
        count = len(rules)
        remaining = MAX_RULES - count
        descriptions = []
        for i, rule in enumerate(rules):
            desc = rule.description or "(no description)"
            tf_names = ", ".join(t.__class__.__name__ for t in rule.transformations)
            descriptions.append(f"  {i + 1}. {desc} [{tf_names}]")
        header = (
            f"Active extraction rules ({count}/{MAX_RULES} used, "
            f"{remaining} remaining):"
        )
        return header + "\n" + "\n".join(descriptions)
    except Exception:
        return "(unable to load current rules)"


MAX_SUGGEST_ATTEMPTS = 5


def _summarize_dag_nodes(auto_dag: dict) -> str:
    """Compact inventory of DAG nodes for structural feedback to the LLM."""
    lines = []
    for nid, node in auto_dag.get("nodes", {}).items():
        ct = node.get("class_type", "?")
        if ct == "Operator":
            lines.append(f"  - [{nid}] Operator: {node.get('name', '?')}")
        elif ct == "Parameter":
            lines.append(
                f"  - [{nid}] Parameter: {node.get('name', '?')}"
                f" (dtype={node.get('dtype', '?')})"
            )
        elif ct == "Snippet":
            lines.append(f"  - [{nid}] Snippet: {node.get('name', '?')}")
        else:
            text = (node.get("text", "") or "")[:60]
            lines.append(f"  - [{nid}] {ct} type={node.get('type', '?')} text=\"{text}\"")
    return "\n".join(lines)


def _summarize_dag_edges(auto_dag: dict) -> str:
    """Compact inventory of DAG edges for structural feedback."""
    lines = []
    for e in auto_dag.get("edges", []):
        lines.append(f"  - {e.get('source')} -> {e.get('destination')}")
    return "\n".join(lines)


def _build_structural_feedback(
    attempt: int,
    max_attempts: int,
    error_type: str,
    auto_dag: dict,
    failed_spec: dict | None = None,
    errors: list[str] | None = None,
) -> str:
    """Build structural self-correction feedback for the LLM retry.

    This is Layer 1 feedback — the pattern doesn't match the graph.
    It's a mechanical problem (wrong node names/types), not a semantic
    one (wrong transformation logic).
    """
    parts = [
        f"\n<previous_attempt number=\"{attempt}\" max=\"{max_attempts}\" result=\"{error_type}\">",
    ]

    if error_type == "no_match":
        parts.append(
            "Your rule compiled successfully but its pattern did NOT match any "
            "nodes in the auto-extracted DAG. The rule was applied and produced "
            "no changes."
        )
        if failed_spec:
            parts.append(f"\nFailed rule:\n```json\n{json.dumps(failed_spec, indent=2)}\n```")
        parts.append(f"\nThe DAG contains these nodes:\n{_summarize_dag_nodes(auto_dag)}")
        parts.append(f"\nAnd these edges:\n{_summarize_dag_edges(auto_dag)}")
        parts.append(
            "\nYour pattern `type` and `text` fields must match the nodes "
            "EXACTLY as listed above. Try a DIFFERENT approach."
        )

    elif error_type == "schema_error":
        parts.append("Your rule failed schema validation.")
        if errors:
            parts.append(f"Errors:\n" + "\n".join(f"  - {e}" for e in errors))
        if failed_spec:
            parts.append(f"\nFailed rule:\n```json\n{json.dumps(failed_spec, indent=2)}\n```")
        parts.append("\nFix the schema errors and try again.")

    elif error_type == "compile_error":
        parts.append("Your rule passed schema validation but failed to compile.")
        if errors:
            parts.append(f"Errors:\n" + "\n".join(f"  - {e}" for e in errors))
        if failed_spec:
            parts.append(f"\nFailed rule:\n```json\n{json.dumps(failed_spec, indent=2)}\n```")
        parts.append("\nFix the compilation errors and try again.")

    elif error_type == "no_rules":
        parts.append(
            "You returned zero rules. You MUST return exactly one rule "
            "in the `rules` array."
        )

    elif error_type == "parse_error":
        parts.append(
            "Your response could not be parsed as JSON. "
            "Respond with a valid JSON object as specified in the instructions."
        )

    parts.append("</previous_attempt>")
    return "\n".join(parts)


def _build_semantic_feedback(
    attempt: int,
    max_attempts: int,
    failed_spec: dict,
    baseline_score: int,
    new_score: int,
    result_diff: dict,
) -> str:
    """Build semantic convergence feedback for the LLM retry.

    This is Layer 2 feedback — the rule matched and changed the DAG,
    but moved it further from the ground truth (score got worse).
    """
    from dorian.mcp.dag_tools import format_semantic_diff

    delta = new_score - baseline_score
    description = failed_spec.get("description", "(no description)")
    parts = [
        f"\n<previous_attempt number=\"{attempt}\" max=\"{max_attempts}\" result=\"semantic_regression\">",
        f"Your rule '{description}' was REJECTED: it made the extraction WORSE.",
        f"Semantic error score: {baseline_score} → {new_score} (+{delta}).",
        "",
        "What got worse after applying your rule:",
        format_semantic_diff(result_diff) or "(diff unavailable)",
        "",
        "Propose a DIFFERENT rule that reduces the error count. "
        "Do not repeat the same transformation.",
        "</previous_attempt>",
    ]
    return "\n".join(parts)


async def handle_suggest_rules(
    event, *, uid, session, payload, request_id, ts,
):
    """Ask the LLM to suggest new rewrite rules for an existing extraction.

    Uses a structural self-correction loop: if the suggested rule's pattern
    doesn't match the DAG, the LLM is retried with feedback (up to
    ``MAX_SUGGEST_ATTEMPTS`` times) before giving up.

    Sends ``extraction/rules-suggestion`` on success, or
    ``extraction/suggest-error`` after all attempts are exhausted.
    """
    d = {**event.data, **payload}
    extraction_id = d.get("extractionId")
    rules_version = d.get("rulesVersion")

    if not uid or not session:
        return
    if not extraction_id:
        await _xadd(uid, session, {
            "event": "extraction/suggest-error",
            "value": _safe_json({"error": "Missing extractionId"}),
        })
        return

    from dorian.code.extraction_store import get_extraction
    from dorian.code.rule_learning import suggest_rules
    from dorian.mcp.stores import draft_store
    from dorian.mcp.rule_tools import rule_create, rule_test
    from dorian.code.rule_debug_log import (
        log_suggestion_start, log_llm_response,
        log_validation, log_test_result, log_retry,
        log_semantic_rejection, log_score_progress,
    )

    record = await get_extraction(extraction_id)
    if not record:
        await _xadd(uid, session, {
            "event": "extraction/suggest-error",
            "value": _safe_json({"error": "Extraction not found"}),
        })
        return

    auto_dag = record["autoDag"]

    # If custom rules were committed after the last extraction, recompute
    # auto_dag from initialDag so the baseline_score reflects the current state.
    stored_rv = record.get("rulesVersion", "")
    json_doc = await expdb.extraction_rule_versions.find_one(
        {"uid": uid, "isValid": True, "format": "json_specs"},
        sort=[("createdAt", -1)],
    )
    if json_doc and json_doc.get("content"):
        from dorian.code.parsing.rules import get_rules as _get_rules, _compute_rules_hash
        from dorian.mcp.rule_compiler import compile_rule as _compile_rule
        from dorian.mcp.dag_tools import _parse_dag
        from dorian.code.parsing.parser import transform as _transform
        _specs = json.loads(json_doc["content"])
        _custom = [r for s in _specs for r in [_compile_rule(s)[0]] if r]
        _base = _get_rules()
        _current_rv = _compute_rules_hash(_base + _custom)
        if _current_rv != stored_rv and _custom:
            _initial = record.get("initialDag", {})
            if _initial:
                _recomputed = await _transform(_parse_dag(_initial), _base + _custom)
                auto_dag = _recomputed.to_json_dict()

    run_id = log_suggestion_start(
        extraction_id, uid, session, auto_dag, len(record.get("code", "")),
    )

    # Implicit negative feedback — user is asking for better rules
    if rules_version:
        asyncio.create_task(_record_feedback(extraction_id, rules_version))

    # ── Concurrency guard ─────────────────────────────────────────
    active_key = RedisKeys.suggest_active(extraction_id)
    acquired = await aioredis.set(active_key, "1", ex=120, nx=True)
    if not acquired:
        await _xadd(uid, session, {
            "event": "extraction/suggest-error",
            "value": _safe_json({"error": "A suggestion is already in progress"}),
        })
        return

    cancel_key = RedisKeys.cancel_suggest(extraction_id)
    feedback = ""
    matched = False
    last_draft_id = None
    result = None
    suggestion = None
    # Partial-progress tracking — populated by the semantic gate when a
    # rule passes GED regression but doesn't fully close the gap. The
    # emit at loop exit attaches these to the suggestion payload so
    # the frontend can render "Partial accept" instead of "Accept".
    emit_is_partial = False
    emit_intermediate_dag: dict | None = None
    emit_ged_before: int | None = None
    emit_ged_after: int | None = None

    # ── Ground truth lookup ───────────────────────────────────
    #
    # Preference order:
    #   1. record["correctedDag"] — the user's hand-edit, persisted by
    #      handle_extraction_corrected. This is the production signal;
    #      it carries the user's actual intent and preserves node IDs
    #      from auto_dag so the edit path is ID-keyed (cheap, exact).
    #   2. data/<filename-stem>_ground_truth.json — dev crutch for
    #      regression testing with synthetic pipelines where no user is
    #      available. Never rely on this in production.
    ground_truth_diff = ""
    gt_dag = None
    baseline_score = 0
    baseline_ged: int | None = None
    edit_path: dict = {"ops": [], "truncated": False, "strategy": "none"}
    from dorian.mcp.dag_tools import (
        semantic_dag_diff, format_semantic_diff, semantic_diff_score,
        graph_edit_distance, graph_edit_path,
    )
    corrected = record.get("correctedDag")
    if corrected and (corrected.get("nodes") or corrected.get("edges")):
        gt_dag = corrected
        gt_source = "corrected"
    else:
        filename = record.get("filename", "")
        gt_source = "none"
        if filename:
            stem = Path(filename).stem
            gt_path = Path(__file__).resolve().parents[3] / "data" / f"{stem}_ground_truth.json"
            if gt_path.exists():
                try:
                    gt_dag = json.loads(gt_path.read_text(encoding="utf-8"))
                    gt_source = "dev_file"
                except Exception:
                    gt_dag = None
    if gt_dag is not None:
        try:
            diff = semantic_dag_diff(auto_dag, gt_dag)
            ground_truth_diff = format_semantic_diff(diff)
            baseline_score = semantic_diff_score(diff)
            baseline_ged = graph_edit_distance(auto_dag, gt_dag)
            edit_path = graph_edit_path(auto_dag, gt_dag)
        except Exception:
            gt_dag = None
            gt_source = "error"

    # ── Few-shot retrieval ────────────────────────────────────
    # Query the extraction corpus for similar past extractions.
    # Positive examples are auto-accepted (the extractor got it right);
    # negatives are user-corrected (negative one-shots). Corpus is
    # naturally self-built from persisted extractions — cold-start at
    # startup; grows as users accept/correct.
    few_shots: dict = {"positives": [], "negatives": [], "corpus_size": 0}
    try:
        from dorian.mcp.retrieval import retrieve_few_shots
        few_shots = await retrieve_few_shots(
            code=record.get("code", ""),
            dag=auto_dag,
            k_pos=3, k_neg=3,
        )
    except Exception:
        pass

    try:
        for attempt in range(1, MAX_SUGGEST_ATTEMPTS + 1):
            # ── Cancel check ──────────────────────────────────
            if await aioredis.get(cancel_key):
                await aioredis.delete(cancel_key)
                await _xadd(uid, session, {
                    "event": "extraction/suggest-cancelled",
                    "value": _safe_json({"extractionId": extraction_id}),
                })
                return

            # ── Mode selection (Python orchestrator, no LLM) ──
            #   reorder — edit_path has zero node-level inserts/deletes
            #             AND we're past attempt 1 (try ADD first to see
            #             if a new rule is easier than a reorder)
            #   partial — we're past the halfway point of the retry budget
            #             and no previous attempt fully closed the gap
            #   add     — default
            mode = "add"
            if edit_path and edit_path.get("ops") and attempt > 1:
                ops = edit_path["ops"]
                has_node_structural = any(
                    op.get("kind") in ("InsertNode", "DeleteNode") for op in ops
                )
                if not has_node_structural:
                    mode = "reorder"
            if attempt > MAX_SUGGEST_ATTEMPTS // 2 and mode == "add":
                mode = "partial"

            # ── Progress event (attempt 2+) ───────────────────
            if attempt > 1:
                await _xadd(uid, session, {
                    "event": "extraction/suggest-progress",
                    "value": _safe_json({
                        "extractionId": extraction_id,
                        "attempt": attempt,
                        "maxAttempts": MAX_SUGGEST_ATTEMPTS,
                        "mode": mode,
                    }),
                })

            # ── LLM call ──────────────────────────────────────
            try:
                result = await suggest_rules(
                    extraction_id=extraction_id,
                    code=record["code"],
                    auto_dag_json=auto_dag,
                    rules_summary=_build_rules_summary(),
                    feedback=feedback,
                    ground_truth_diff=ground_truth_diff,
                    edit_path=edit_path,
                    few_shots=few_shots,
                    mode=mode,
                )
            except Exception as exc:
                await _xadd(uid, session, {
                    "event": "extraction/suggest-error",
                    "value": _safe_json({"error": f"LLM error: {exc}"}),
                })
                return

            # ── Post-LLM cancel check (avoid wasted work) ────
            if await aioredis.get(cancel_key):
                await aioredis.delete(cancel_key)
                await _xadd(uid, session, {
                    "event": "extraction/suggest-cancelled",
                    "value": _safe_json({"extractionId": extraction_id}),
                })
                return

            first_spec = result.rules[0].spec if result.rules else None
            log_llm_response(run_id, result.reasoning, len(result.rules), first_spec)

            # ── No rules returned ─────────────────────────────
            if not result.rules:
                log_retry(run_id, attempt, "no_rules")
                feedback += _build_structural_feedback(
                    attempt, MAX_SUGGEST_ATTEMPTS, "no_rules", auto_dag,
                )
                continue

            suggestion = result.rules[0]

            # ── Validation failed ─────────────────────────────
            if not suggestion.valid:
                error_type = "schema_error"
                log_validation(run_id, False, schema_errors=suggestion.errors)
                log_retry(run_id, attempt, error_type, failed_spec=suggestion.spec)
                feedback += _build_structural_feedback(
                    attempt, MAX_SUGGEST_ATTEMPTS, error_type, auto_dag,
                    failed_spec=suggestion.spec, errors=suggestion.errors,
                )
                continue

            log_validation(run_id, True)

            # ── Stage & test ──────────────────────────────────
            try:
                create_result = rule_create(draft_store, suggestion.spec)
                last_draft_id = create_result["draft_id"]
                suggestion.draft_id = last_draft_id

                test_result = rule_test(draft_store, last_draft_id, auto_dag)
                matched = test_result.get("matched", False)
                diff = test_result.get("diff", {})

                log_test_result(
                    run_id, last_draft_id, matched,
                    diff.get("summary", "N/A"),
                    edges_before=len(auto_dag.get("edges", [])),
                    edges_after=len(
                        test_result.get("result_pipeline_json", {}).get("edges", [])
                    ),
                )
            except Exception as exc:
                log_retry(run_id, attempt, "test_error")
                feedback += _build_structural_feedback(
                    attempt, MAX_SUGGEST_ATTEMPTS, "no_match", auto_dag,
                    failed_spec=suggestion.spec,
                    errors=[f"Rule test crashed: {exc}"],
                )
                if last_draft_id:
                    draft_store.remove_rule(last_draft_id)
                    last_draft_id = None
                continue

            if matched:
                # ── Semantic convergence gate ─────────────────
                if gt_dag is not None:
                    from dorian.mcp.dag_tools import (
                        semantic_dag_diff, semantic_diff_score, _parse_dag,
                        graph_edit_distance,
                    )
                    from dorian.code.parsing.parser import transform
                    # Apply only the new rule to fixpoint on the already-transformed
                    # auto_dag. Existing rules already ran during extraction — no need
                    # to re-run them.
                    draft = draft_store.get_rule(last_draft_id)
                    result_dag_obj = await transform(
                        _parse_dag(auto_dag), [draft.compiled] if draft else []
                    )
                    result_dag_json = result_dag_obj.to_json_dict()
                    if result_dag_json:
                        result_diff = semantic_dag_diff(result_dag_json, gt_dag)
                        result_score = semantic_diff_score(result_diff)
                        # Secondary signal: Rust-native GED against ground truth.
                        # A rule that passes the semantic gate but increases GED
                        # is likely locally-correct yet globally-regressive
                        # (e.g. rewires an edge that happens to match an operator
                        # set requirement but destroys structure elsewhere).
                        result_ged = graph_edit_distance(result_dag_json, gt_dag)
                        ged_regressed = (
                            baseline_ged is not None
                            and result_ged is not None
                            and result_ged > baseline_ged
                        )
                        if result_score > baseline_score or ged_regressed:
                            log_semantic_rejection(
                                run_id, attempt, baseline_score, result_score,
                                failed_spec=suggestion.spec,
                            )
                            draft_store.remove_rule(last_draft_id)
                            last_draft_id = None
                            matched = False  # prevent stale True reaching exhaustion check
                            feedback += _build_semantic_feedback(
                                attempt, MAX_SUGGEST_ATTEMPTS,
                                suggestion.spec, baseline_score, result_score, result_diff,
                            )
                            continue
                        # ── Passed gate — log score progress ─────
                        log_score_progress(
                            run_id, attempt, baseline_score, result_score,
                            accepted_spec=suggestion.spec,
                        )
                        # Distinguish full vs partial: a rule that drives
                        # result_score and result_ged to 0 closes the gap;
                        # anything else is partial progress. The orchestrator
                        # emits both with an isPartial flag so the card UI
                        # shows "Accept" vs "Partial accept".
                        emit_is_partial = bool(
                            (result_ged is not None and result_ged > 0)
                            or result_score > 0
                        )
                        emit_intermediate_dag = result_dag_json if emit_is_partial else None
                        emit_ged_before = baseline_ged
                        emit_ged_after = result_ged
                else:
                    # No ground truth available → treat as a plain full-match
                    # suggestion. The user is the final arbiter.
                    emit_is_partial = False
                    emit_intermediate_dag = None
                    emit_ged_before = None
                    emit_ged_after = None
                break

            # ── Not matched — structural self-correction ──────
            log_retry(run_id, attempt, "no_match",
                      diff_summary=diff.get("summary", "N/A"),
                      failed_spec=suggestion.spec)
            feedback += _build_structural_feedback(
                attempt, MAX_SUGGEST_ATTEMPTS, "no_match", auto_dag,
                failed_spec=suggestion.spec,
            )
            draft_store.remove_rule(last_draft_id)
            last_draft_id = None

        # ── Loop exhausted without match ──────────────────────
        if not matched:
            await aemit(Event("RuleSuggestionRetryExhausted", {
                "extraction_id": extraction_id,
                "attempts": MAX_SUGGEST_ATTEMPTS,
            }))
            await _xadd(uid, session, {
                "event": "extraction/suggest-error",
                "value": _safe_json({
                    "error": (
                        f"Could not generate a rule that improves the extraction after "
                        f"{MAX_SUGGEST_ATTEMPTS} attempts."
                    ),
                    "extractionId": extraction_id,
                    "attempts": MAX_SUGGEST_ATTEMPTS,
                }),
            })
            return

        # ── Success — persist & send ──────────────────────────
        try:
            await expdb.rule_suggestions.insert_one({
                "_id": result.suggestion_id,
                "extractionId": result.extraction_id,
                "uid": uid,
                "rules": [
                    {
                        "ruleId": r.rule_id,
                        "spec": r.spec,
                        "draftId": r.draft_id,
                        "valid": r.valid,
                        "errors": r.errors,
                    }
                    for r in result.rules
                ],
                "reasoning": result.reasoning,
                "createdAt": datetime.now(timezone.utc),
            })
        except Exception:
            await aemit(Event("RuleSuggestionPersistFailed", {}))

        await _xadd(uid, session, {
            "event": "extraction/rules-suggestion",
            "value": _safe_json({
                "suggestionId": result.suggestion_id,
                "extractionId": result.extraction_id,
                "rules": [
                    {
                        "ruleId": r.rule_id,
                        "description": r.description,
                        "spec": r.spec,
                        "draftId": r.draft_id,
                        "valid": r.valid,
                        "errors": r.errors,
                        "warnings": r.warnings,
                        # Partial-progress metadata attaches to each rule
                        # (currently always a single rule per suggestion).
                        # intermediateDag is the DAG produced by applying
                        # this rule to auto_dag — the new baseline for a
                        # follow-up suggest if the user accepts the partial.
                        "isPartial": emit_is_partial,
                        "intermediateDag": emit_intermediate_dag,
                        "gedBefore": emit_ged_before,
                        "gedAfter": emit_ged_after,
                    }
                    for r in result.rules
                ],
                "reasoning": result.reasoning,
            }),
        })

    finally:
        await aioredis.delete(active_key)
        await aioredis.delete(cancel_key)
        if last_draft_id and not matched:
            draft_store.remove_rule(last_draft_id)


async def handle_create_mcp_token(
    event, *, uid, session, payload, request_id, ts,
):
    """Issue a short-lived MCP token bound to ``(uid, session)``.

    The frontend's "Connect MCP" button fires ``CreateMcpToken``; the
    handler mints a hex token via ``dorian.mcp.token.issue_token``
    (TTL 1h, Redis-backed) and streams it back on
    ``mcp/token-issued``. The user pastes the token into their MCP
    client config and every subsequent MCP tool call authenticates
    against it.
    """
    if not uid or not session:
        return
    from dorian.mcp.token import issue_token
    try:
        token = await issue_token(uid, session)
    except Exception as exc:
        await aemit(Event("McpTokenIssueFailed", {"error": str(exc)}))
        await _xadd(uid, session, {
            "event": "mcp/token-issued",
            "value": _safe_json({"error": str(exc)}),
        })
        return
    await aemit(Event("McpTokenIssued", {
        "uid": uid, "session": session, "ttl_seconds": 3600,
    }))
    await _xadd(uid, session, {
        "event": "mcp/token-issued",
        "value": _safe_json({"token": token, "ttl_seconds": 3600}),
    })


async def handle_cancel_suggest_rules(
    event, *, uid, session, payload, request_id, ts,
):
    """Set a cancellation flag for in-flight rule suggestion retries."""
    d = {**event.data, **payload}
    extraction_id = d.get("extractionId")
    if not extraction_id:
        return
    await aioredis.set(RedisKeys.cancel_suggest(extraction_id), "1", ex=60)
    await aemit(Event("RuleSuggestionCancelRequested", {
        "extraction_id": extraction_id,
        "uid": uid,
    }))


async def handle_accept_rule(
    event, *, uid, session, payload, request_id, ts,
):
    """Accept a suggested rule via the DraftStore create→test→commit flow.

    Uses ``rule_commit`` to add the rule to the in-memory active set, then
    persists the JSON spec to ``doc_extraction_rule_versions`` (format
    ``json_specs``).
    """
    d = {**event.data, **payload}
    suggestion_id = d.get("suggestionId")
    rule_id = d.get("ruleId")
    draft_id = d.get("draftId")

    if not uid or not session or not suggestion_id or not rule_id:
        return

    from dorian.mcp.stores import draft_store
    from dorian.mcp.rule_tools import rule_create, rule_test, rule_commit

    doc = await expdb.rule_suggestions.find_one({"_id": suggestion_id})
    if not doc:
        await _xadd(uid, session, {
            "event": "extraction/rule-accepted",
            "value": _safe_json({"error": "Suggestion not found", "ruleId": rule_id}),
        })
        return

    matched = next((r for r in doc.get("rules", []) if r.get("ruleId") == rule_id), None)
    if not matched:
        await _xadd(uid, session, {
            "event": "extraction/rule-accepted",
            "value": _safe_json({"error": "Rule not found in suggestion", "ruleId": rule_id}),
        })
        return

    # Try to resolve the draft — may be missing after server restart
    draft = draft_store.get_rule(draft_id) if draft_id else None

    if draft is None:
        # Fallback: re-create from the spec stored in the suggestion doc
        spec = matched.get("spec", {})
        create_result = rule_create(draft_store, spec)
        draft_id = create_result["draft_id"]

        # Need a test pass to satisfy commit gate — use extraction's autoDag
        from dorian.code.extraction_store import get_extraction
        extraction_id = doc.get("extractionId")
        record = await get_extraction(extraction_id) if extraction_id else None
        if record and record.get("autoDag"):
            rule_test(draft_store, draft_id, record["autoDag"])
        else:
            pass  # extraction record missing — skip re-test, commit will still proceed

    # Commit the draft to the active rule set
    from dorian.code.rule_debug_log import log_accept
    commit_result = rule_commit(draft_store, draft_id)
    if commit_result.get("error"):
        log_accept("accept", rule_id, draft_id, False, error=commit_result["error"])
        await _xadd(uid, session, {
            "event": "extraction/rule-accepted",
            "value": _safe_json({
                "error": commit_result["error"],
                "ruleId": rule_id,
            }),
        })
        return

    log_accept("accept", rule_id, draft_id, True)
    await aemit(Event("RuleCommitted", {"description": commit_result.get("description", "")}))

    # Persist JSON spec to extraction_rule_versions
    spec = matched.get("spec", {})
    try:
        latest = await expdb.extraction_rule_versions.find_one(
            {"uid": uid, "isValid": True, "format": "json_specs"},
            sort=[("createdAt", -1)],
        )
        if latest and latest.get("content"):
            existing_specs = json.loads(latest["content"])
        else:
            existing_specs = []
        existing_specs.append(spec)

        await expdb.extraction_rule_versions.insert_one({
            "uid": uid,
            "session": session,
            "content": json.dumps(existing_specs),
            "rulesHash": _compute_rules_hash(get_rules()),
            "createdAt": datetime.now(timezone.utc),
            "isValid": True,
            "format": "json_specs",
            "source": "suggestion_accept",
        })
    except Exception as exc:
        await aemit(Event("RuleSpecPersistFailed", {"error": str(exc)}))

    # ── Partial-progress continuation ────────────────────────
    # If this suggestion was marked partial (GED improved but gap not
    # closed), advance the extraction record's autoDag to the partial
    # result so the next SuggestExtractionRules call picks up from the
    # improved baseline instead of re-discovering the same gap.
    is_partial = bool(matched.get("isPartial"))
    intermediate_dag = matched.get("intermediateDag")
    if is_partial and intermediate_dag:
        extraction_id = doc.get("extractionId")
        if extraction_id:
            try:
                await expdb.extractions.update_one(
                    {"_id": extraction_id},
                    {"$set": {"autoDag": intermediate_dag,
                              "partialAcceptedAt": datetime.now(timezone.utc)}},
                )
                await aemit(Event("PartialRuleAccepted", {
                    "extraction_id": extraction_id,
                    "rule_id": rule_id,
                    "ged_before": matched.get("gedBefore"),
                    "ged_after": matched.get("gedAfter"),
                }))
                # Also push the new DAG to the canvas so the user sees
                # the applied rule's effect immediately — same payload
                # shape as extraction/extracted uses.
                await _xadd(uid, session, {
                    "event": "extraction/partial-applied",
                    "value": _safe_json({
                        "extractionId": extraction_id,
                        "ruleId": rule_id,
                        "updatedDag": intermediate_dag,
                        "gedBefore": matched.get("gedBefore"),
                        "gedAfter": matched.get("gedAfter"),
                    }),
                })
            except Exception as exc:
                await aemit(Event("PartialRuleApplyFailed", {"error": str(exc)}))

    await _xadd(uid, session, {
        "event": "extraction/rule-accepted",
        "value": _safe_json({
            "ruleId": rule_id,
            "suggestionId": suggestion_id,
            "draftId": draft_id,
            "isPartial": is_partial,
        }),
    })


async def handle_reject_rule(
    event, *, uid, session, payload, request_id, ts,
):
    """Acknowledge a rule rejection — cleans up the DraftStore draft."""
    d = {**event.data, **payload}
    rule_id = d.get("ruleId")
    suggestion_id = d.get("suggestionId")
    draft_id = d.get("draftId")

    if draft_id:
        from dorian.mcp.stores import draft_store
        draft_store.remove_rule(draft_id)

    await _xadd(uid, session, {
        "event": "extraction/rule-rejected",
        "value": _safe_json({"ruleId": rule_id, "suggestionId": suggestion_id}),
    })
