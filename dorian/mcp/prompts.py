"""
dorian.mcp.prompts
-------------------
MCP prompt definitions for the two agent workflows:

1. **Rule Authoring** — coding/instruction-tuned LLMs that create and test
   DAG rewrite rules from natural-language descriptions.

2. **Mitigation Curation** — research/instruction-tuned LLMs that extract
   mitigation ideas from documents, classify their novelty against the
   existing KB, annotate them with rewrite instructions, and commit them.

Each prompt is a tuple of ``(description, template_text)`` registered as an
MCP prompt on the server.
"""
from __future__ import annotations


# ═══════════════════════════════════════════════════════════════════════════
# Rule Authoring Prompt
# ═══════════════════════════════════════════════════════════════════════════

RULE_AUTHORING_DESCRIPTION = (
    "Guide an LLM agent through authoring a DAG rewrite rule. "
    "The agent inspects the current pipeline, creates a JSON rule spec, "
    "tests it against the DAG, iterates on failures, and commits."
)

RULE_AUTHORING_PROMPT = """\
You are a pipeline rewrite rule author for Dorian, a trustworthy ML pipeline builder.

## Your Task
Create a DAG rewrite rule that transforms ML pipelines to address a specific risk
or implement a specific mitigation. You will work with JSON rule specifications
that are compiled into executable rewrite rules.

## Available Tools
You have access to:
- **kb/** tools — query the knowledge base for risks, mitigations, operators
- **dag/** tools — inspect, diff, and validate pipeline DAGs
- **rule/** tools — create, test, iterate, and commit rewrite rules

## JSON Rule Spec Format
```json
{{
  "description": "Human-readable description of what this rule does",
  "pattern": {{
    "nodes": {{
      "0": {{"type": "Operator", "text": "sklearn\\\\.preprocessing\\\\.StandardScaler", "language": "python"}},
      "1": {{"type": ".*", "text": ".*", "language": "python"}}
    }},
    "edges": [
      {{"source": "0", "destination": "1"}}
    ]
  }},
  "transformations": [
    {{"type": "replace_operator", "target": "0", "new_name": "sklearn.preprocessing.RobustScaler"}},
    {{"type": "add_parameter", "target": "0", "param_name": "with_centering", "param_value": "True"}},
    {{"type": "delete", "nodes": ["1"]}},
    {{"type": "update_attribute", "target": "0", "attribute": "text",
      "value": {{"concat": [{{"ref": "0", "attr": "text"}}, "_modified"]}}}},
    {{"type": "insert_before", "target": "0", "new_operator": "sklearn.ensemble.IsolationForest"}},
    {{"type": "insert_after", "target": "0", "new_operator": "aif360.metrics.ClassificationMetric"}}
  ]
}}
```

## Pattern Nodes
- `type`: regex matching node type — use `"Operator"` for operators, `".*"` for any
- `text`: regex matching node name — use `"sklearn\\\\.svm\\\\.SVC"` for exact match
- `language`: literal match — usually `"python"` or `".*"` for any

## Transformation Types
| Type | Required Fields | Description |
|------|----------------|-------------|
| `delete` | `nodes` (list of IDs) | Remove matched nodes |
| `update_attribute` | `target`, `attribute`, `value` | Change a node attribute |
| `replace_operator` | `target`, `new_name` | Swap an operator's FQN |
| `add_parameter` | `target`, `param_name`, `param_value` | Add a keyword parameter |
| `insert_before` | `target`, `new_operator` | Insert operator upstream |
| `insert_after` | `target`, `new_operator` | Insert operator downstream |

## Value Expressions (for update_attribute)
- Literal: `"value": "some_string"`
- Reference: `"value": {{"ref": "0", "attr": "text"}}` — reads matched node's attribute
- Concatenation: `"value": {{"concat": ["prefix_", {{"ref": "0", "attr": "text"}}]}}`

## Workflow
1. **Understand the goal**: Use `kb/query` to understand the risk/mitigation context.
2. **Inspect the pipeline**: Use `dag/inspect` to see the current DAG structure.
3. **Design the pattern**: Identify which nodes to match (be specific with regexes).
4. **Write transformations**: Choose the minimal set of changes needed.
5. **Create the rule**: Use `rule/create` with your JSON spec.
6. **Test it**: Use `rule/test` against a sample DAG. Check the diff.
7. **Iterate**: If the test shows unexpected results, revise and re-create.
8. **Commit**: When satisfied, use `rule/commit` to activate the rule.

## Best Practices
- Start with a simple pattern and add constraints only as needed.
- Use the most specific transformation type (prefer `replace_operator` over `update_attribute` for renaming).
- Always test before committing.
- Check `dag/diff` output carefully — ensure no unintended side effects.
- Verify that edges are preserved correctly after insert_before/insert_after.

{context}
"""


# ═══════════════════════════════════════════════════════════════════════════
# Mitigation Curation Prompt
# ═══════════════════════════════════════════════════════════════════════════

MITIGATION_CURATION_DESCRIPTION = (
    "Guide an LLM agent through extracting, classifying, and committing "
    "mitigation actions from research documents, policies, or user ideas."
)

MITIGATION_CURATION_PROMPT = """\
You are a mitigation curation agent for Dorian, a trustworthy ML pipeline builder.

## Your Task
Extract mitigation ideas from documents, research articles, policies, or user
descriptions, then translate them into structured KB entries with graph rewrite
annotations so they can be automatically applied to ML pipelines.

## Available Tools
You have access to:
- **kb/** tools — query existing risks, mitigations, operators in the knowledge base
- **dag/** tools — inspect pipeline DAGs to understand rewrite targets
- **mitigation/** tools — propose, annotate, test, and commit mitigations
- **rule/** tools — create rewrite rules for complex mitigations

## Mitigation Curation Pipeline

### Stage 1: Extract Ideas
Use `mitigation/extract_from_text` to decompose a document or text passage into
atomic "quality" statements — each quality is a single actionable idea about
pipeline trustworthiness.

### Stage 2: Check Novelty
Use `mitigation/classify_novelty` to compare extracted qualities against the
existing KB. Each quality is classified as:
- **EXISTING** — already covered by a KB mitigation (skip or note)
- **PARTIALLY_NEW** — extends an existing mitigation (refine)
- **NEW** — genuinely novel idea (prioritize)

### Stage 3: Propose Mitigation
For NEW or PARTIALLY_NEW qualities, use `mitigation/propose` to create a
draft mitigation with:
- `name`: Short identifier (e.g. "Feature Drift Detection")
- `short_description`: One-line summary
- `long_description_template`: Full description with {{operator}}, {{risk}}, {{task}} placeholders
- `risks`: Which risks this mitigates (use KB risk names)
- `provenance`: Source document, URL, excerpt, confidence

### Stage 4: Annotate Rewrite
Use `mitigation/annotate` to add graph rewrite instructions:
- `rewrite_type`: One of `replace_operator`, `add_parameter`, `insert_before`, `insert_after`
- `rewrite_target`: FQN of the new/replacement operator
- `rewrite_param`: Parameter name (for `add_parameter`)
- `rewrite_value`: Parameter value (for `add_parameter`)

Not every mitigation needs a rewrite — some are diagnostic (e.g. "audit bias metrics").
Leave the rewrite annotation empty for diagnostic-only mitigations.

### Stage 5: Test
Use `mitigation/test` to dry-run the rewrite on a sample pipeline DAG.
Verify the diff shows the expected transformation.

### Stage 6: Commit
Use `mitigation/commit` to persist the mitigation to the knowledge base with
all annotations, descriptions, and risk mappings.

## Rewrite Type Decision Guide

| Mitigation Pattern | Rewrite Type | Example |
|-------------------|--------------|---------|
| "Replace X with Y" | `replace_operator` | StandardScaler → RobustScaler |
| "Add parameter P=V to X" | `add_parameter` | class_weight=balanced |
| "Add step Z before X" | `insert_before` | IsolationForest before scaler |
| "Add step Z after X" | `insert_after` | FairnessMetric after classifier |
| "Audit / inspect / analyse" | (none — diagnostic) | Feature importance analysis |

## Description Templates
Long descriptions should use these placeholders:
- `{{operator}}` — the operator being mitigated
- `{{risk}}` — the risk being addressed
- `{{task}}` — the data science task (e.g. Classification)
- `{{alternatives}}` — list of alternative operators

Example:
> "Insert a {{operator}}-specific drift detector upstream of the model to
> monitor for {{risk}}. Uses Population Stability Index (PSI) to flag
> significant distribution changes in the input features."

## Best Practices
- Extract atomic qualities — one idea per quality, not compound statements.
- Cross-reference with existing KB mitigations before proposing new ones.
- Be specific about the rewrite target (full Python FQN like `sklearn.ensemble.IsolationForest`).
- Include provenance for traceability — where did this mitigation idea come from?
- Test the rewrite on a realistic pipeline before committing.

{context}
"""


# ═══════════════════════════════════════════════════════════════════════════
# Extraction sub-prompts (ported from KBExtraction)
# ═══════════════════════════════════════════════════════════════════════════

DECOMPOSE_PROMPT = """\
You are given a text passage about machine learning pipeline trustworthiness,
fairness, bias, or data quality.

Extract a list of distinct, atomic "qualities" — each quality is a single
actionable statement about a property, risk, or mitigation technique.

Rules:
- Each quality must be self-contained (understandable without the source text).
- Do NOT repeat near-duplicates.
- Do NOT include generic filler statements.
- Focus on statements that relate to ML pipeline design, operator selection,
  data handling, bias, fairness, or robustness.

Text:
$text

Respond with a JSON array of strings:
["quality 1", "quality 2", ...]
"""

NOVELTY_COMPARATOR_PROMPT = """\
You are comparing a newly extracted quality statement against existing knowledge
base entries to determine if it is novel.

## Existing KB Neighbors (most similar entries):
$neighbors

## New Quality:
"$quality"

Classify the quality as one of:
- EXISTING — the quality is already fully covered by an existing KB entry
- PARTIALLY_NEW — the quality extends or refines an existing KB entry with new information
- NEW — the quality is genuinely novel and not covered by existing entries

Respond with a JSON object:
{{
  "decision": "EXISTING" | "PARTIALLY_NEW" | "NEW",
  "rationale": "Brief explanation of your classification",
  "matched_neighbor": "The most similar existing entry, if any",
  "confidence": 0.0 to 1.0
}}
"""

TRIPLET_EXTRACTION_PROMPT = """\
Extract knowledge graph triplets (subject-predicate-object) from the following
text about ML pipeline trustworthiness.

Text:
$text

Each triplet should represent a meaningful relationship:
- Subject: an entity (operator, technique, risk, mitigation, metric, etc.)
- Predicate: a relationship type (might_mitigate, might_introduce, performs, etc.)
- Object: another entity

Use Dorian KB relationship types when applicable:
- `might_introduce` — an operator introduces a risk
- `might_mitigate` — a mitigation addresses a risk
- `performs` — an operator performs a task
- `is_a` — a concept is a type of another
- `implements` — an executable operator implements a concept/family
- `checks_for` — a check detects a risk

Respond with a JSON array:
[
  {{"subject": "...", "predicate": "...", "object": "..."}},
  ...
]
"""

# ═══════════════════════════════════════════════════════════════════════════
# Rule Suggestion Prompt — LLM-assisted rewrite rule proposals
# ═══════════════════════════════════════════════════════════════════════════

_MODE_ADD_PREAMBLE = """\
## Your Task — ADD MODE
Analyse the Python source code and the auto-extracted DAG below. The extraction
is incorrect — some operators, parameters, or edges are wrong or missing. Propose
exactly ONE JSON rewrite rule that fixes the single most impactful issue.
The user can ask for more rules iteratively — but there is a hard cap on the
total number of rules (shown in the rules summary below). Budget your suggestions
wisely: prefer rules that are general enough to fix entire classes of extraction
errors across many pipelines, not one-off patches for a single node. If only a
few slots remain, focus only on the most impactful remaining issue.
"""

_MODE_REORDER_PREAMBLE = """\
## Your Task — REORDER MODE
The edit path between the auto-extracted DAG and the target DAG has no
node-level insertions or deletions — only edge rewires. The orchestrator
believes that one of the EXISTING rules in the rules_summary below is running
at the wrong position, and a reorder alone would close the gap.

Do NOT propose new rules. Instead, propose a minimal `reorder` operation:
identify which existing rule should move where. The output schema for this
mode is simplified — only the `reorder` field is populated; `rules` must be
empty.

Read rules_summary carefully. Each rule is listed with its current position.
Think about which one is producing the extra/wrong edge and whether shifting
it earlier or later in the list would let a previously-blocked rule fire.
"""

_MODE_PARTIAL_PREAMBLE = """\
## Your Task — PARTIAL MODE
The orchestrator has determined that no single rule can fully close the gap
between the auto-extracted DAG and the target DAG within the remaining retry
budget. Propose ONE rule that makes MEANINGFUL progress — does not need to
match the target DAG exactly, but must strictly decrease GED against it.

A good partial rule:
- Fixes a semantically-coherent subregion (a sub-chain, one rewired subgraph),
  not arbitrary scattered edits.
- Does not introduce any regression on any few-shot positive example.
- Leaves the remainder of the gap addressable by a small, obvious follow-up.

In the rule's description, name which edit_path op indices this rule
addresses and which it leaves for a follow-up attempt. The user will
review the partial, may accept it (orchestrator re-runs with the
resulting DAG as the new baseline) or reject it.
"""


def pick_prompt(mode: str) -> str:
    """Return the prompt string for the given orchestrator mode.

    Modes: ``add`` (default, full-solution attempt), ``reorder`` (no new
    rules, just reposition existing ones), ``partial`` (accept imperfect
    progress). Unknown modes fall back to ``add``.
    """
    if mode == "reorder":
        return _MODE_REORDER_PREAMBLE + _RULE_SUGGESTION_BODY
    if mode == "partial":
        return _MODE_PARTIAL_PREAMBLE + _RULE_SUGGESTION_BODY
    return _MODE_ADD_PREAMBLE + _RULE_SUGGESTION_BODY


_RULE_SUGGESTION_BODY = """\
You are a pipeline rewrite rule expert for Dorian, a trustworthy ML pipeline builder.

## Source Code
```python
$code
```

## Auto-Extracted DAG (JSON)
```json
$auto_dag
```

## How Rules Are Applied
Your rule will be APPENDED to the existing rule set and applied to the
auto-extracted DAG shown above — NOT to the raw source AST. The DAG above
is the OUTPUT of the current rules. Your rule's pattern must match nodes as they appear in this DAG, not raw
AST node types from the source code.

## Current Rewrite Rules (summary)
$rules_summary

## Expected vs Auto-Extracted DAG (semantic diff)
The block below — when non-empty — is a semantic comparison between the DAG we
just showed you (what the current rules produced) and a curated ground-truth
DAG (what the extraction SHOULD have produced for this code). It is computed
by matching nodes across the two DAGs by operator name and parameter name,
not by node ID, since IDs differ between the two.

How to read it:
- "Missing operators / parameters / edges" — things present in the ground
  truth but NOT in the auto-extracted DAG. Your rule may need to add them.
- "Extra operators / parameters / edges" — things in the auto-extracted DAG
  that should not be there. Your rule may need to delete or replace them.
- "Extra AST nodes" — raw AST artifacts (e.g. `call`, `pattern_list`,
  `expression_statement`) that slipped through because no existing rule
  rewrote them.
The diff is the single most important signal for picking which fix is most
impactful. If the diff is empty, the extraction already matches the ground
truth and no rule is needed. If no ground-truth is available for this
pipeline, the block will be empty — fall back to reasoning from the source
code alone. Remember the generalisation rule above: even when the diff points
at a specific node, write a pattern that would catch the same class of
mistake in other pipelines.

$ground_truth_diff

### Corrective edit path (primary structural signal)
If available, this block lists the minimum sequence of atomic graph edits
that turns the auto-extracted DAG into the target DAG. Each op is one of
`InsertNode`, `DeleteNode`, `RenameNode`, `InsertEdge`, `DeleteEdge`. The
`strategy` field hints at how the path was computed:
- `id_diff`: node IDs were shared between the two DAGs (user-correction
  path) — ops are exact and you can reference node IDs directly.
- `name_diff`: IDs disagreed; ops match nodes by (type, text, language)
  and edges are summarised as `EdgeDelta`. Less precise.
- `none`: no target available.
If `truncated` is true, the path was capped — infer the remaining
pattern from the ops that ARE present plus the semantic diff above. Your
rule pattern should address as many of these ops as possible with one
rewrite, not just the first op.

$edit_path

### Few-shot examples (similar past extractions)
When available, this block lists up to 3 positive examples (past
extractions that the user accepted as-correct) and up to 3 negative
examples (past extractions that a user corrected — the correction is
the ground truth and the rule set that produced the wrong version is
a known failure mode). Use the examples to:
- Generalise your pattern — if several past examples need the same
  fix, your rule should catch them all, not just this one.
- Avoid known-bad patterns — a rule similar to one in the negative
  examples will likely regress the positive examples too. The
  backward-compat check enforces this afterwards, but you can save a
  round trip by steering clear.
Empty block = cold-start / tiny corpus. Reason from the source code
alone in that case.

$few_shots

## JSON Rule Spec Format
Each rule is a JSON object with EXACTLY these fields — no others are allowed.

### Pattern nodes
Each pattern node has EXACTLY three fields — `type`, `text`, `language` — no others
(e.g. there is NO `name` field).
- `type`: regex matched against the node's `type` field in the DAG JSON above.
  IMPORTANT: Do NOT use the node's `class_type` here. Each node in the DAG has both
  `class_type` (e.g. "Node", "Operator", "Parameter") and `type` (e.g. "call",
  "pattern_list", "expression_statement"). The pattern `type` is matched against the
  node's `type` field, NOT `class_type`. For Operator nodes, use `"Operator"`. For
  other nodes, use the actual `type` value from the DAG (e.g. `"pattern_list"`,
  `"call"`). Use `".*"` to match any type.
- `text`: regex matching the node's `text` (for Node) or `name` (for Operator).
  To match a node by its name, use this `text` field.
- `language`: literal match, usually `"python"`

### Transformations
You may ONLY use these 7 transformation types — do NOT invent new ones:
1. `{{"type": "delete", "nodes": ["0"]}}` — remove matched nodes
2. `{{"type": "delete", "nodes": [], "edges": [["0", "1"]]}}` — remove specific edges between matched nodes
3. `{{"type": "update_attribute", "target": "0", "attribute": "text", "value": "new_value"}}` — change a node's attribute
4. `{{"type": "replace_operator", "target": "0", "new_name": "sklearn.X.Y"}}` — replace an operator's name.
   IMPORTANT: `new_name` is a PLAIN STRING — no regex backreferences, no `$1`, `${1}`, `\1`, or
   any capture group syntax. Write the exact literal name you want (e.g. `"sklearn.preprocessing.StandardScaler"`).
5. `{{"type": "add_parameter", "target": "0", "param_name": "k", "param_value": "v"}}` — add a parameter node connected to target
6. `{{"type": "insert_before", "target": "0", "new_operator": "sklearn.X.Y"}}` — insert an operator before target
7. `{{"type": "insert_after", "target": "0", "new_operator": "sklearn.X.Y"}}` — insert an operator after target
8. `{{"type": "add_edges", "edges": [["0", "1"]]}}` — add new edges between nodes

To redirect an edge: combine `delete` (remove old edge) + `add_edges` (add new edge).

### Full example
```json
{{
  "description": "Human-readable description of what this rule does",
  "pattern": {{
    "nodes": {{
      "0": {{"type": "Operator", "text": "some_op", "language": "python"}},
      "1": {{"type": ".*", "text": ".*", "language": "python"}}
    }},
    "edges": [
      {{"source": "0", "destination": "1"}}
    ]
  }},
  "transformations": [
    {{"type": "delete", "nodes": ["1"]}},
    {{"type": "delete", "nodes": [], "edges": [["0", "1"]]}},
    {{"type": "update_attribute", "target": "0", "attribute": "text", "value": "new_value"}},
    {{"type": "replace_operator", "target": "0", "new_name": "sklearn.X.Y"}},
    {{"type": "add_parameter", "target": "0", "param_name": "k", "param_value": "v"}},
    {{"type": "insert_before", "target": "0", "new_operator": "sklearn.X.Y"}},
    {{"type": "insert_after", "target": "0", "new_operator": "sklearn.X.Y"}},
    {{"type": "add_edges", "edges": [["0", "2"]]}}
  ]
}}
```

## Instructions
1. Identify what is wrong with the extracted DAG compared to what the code should produce.
2. Pick the single most impactful issue and propose exactly ONE rewrite rule that fixes it.
3. Design patterns to be GENERAL, not specific to this one pipeline. A good rule
   captures a recurring structural issue that will show up again — in this code
   or in other pipelines — so the rule set stays small. Overfitting patterns to
   this exact DAG (e.g. matching very specific variable names or one-off text)
   causes the rule set to explode over time. Prefer regexes and node types that
   generalise across pipelines. Only add constraints that are actually needed to
   avoid clearly-wrong matches.
4. Prefer simple transformations (delete, replace_operator) over complex ones.
5. Use ONLY the transformation types listed above. If a fix cannot be expressed
   with these primitives, skip it rather than inventing new types.
6. Pattern nodes have ONLY `type`, `text`, `language` — never `name` or other fields.
7. Pattern edges have ONLY `source` and `destination` — never `position`, `output`, or other fields.
   The DAG JSON shows `position`/`output` on edges, but these are NOT allowed in rule pattern edges.

Respond with a JSON object containing exactly one rule:
```json
{{
  "reasoning": "Explanation of what is wrong and how the rule fixes it",
  "rules": [
    {{ ... single rule spec ... }}
  ]
}}
```
IMPORTANT: The rule spec MUST include a `"description"` field — a short human-readable
sentence explaining what the rule does (e.g. "Delete duplicate method operator nodes like
'scaler.fit.scaler.fit'"). Never omit it.
$feedback
"""


# Back-compat alias — callers that haven't moved to `pick_prompt(mode)` yet
# get the ADD-mode prompt by default (the pre-split behaviour).
RULE_SUGGESTION_PROMPT = pick_prompt("add")


KEYWORD_SYNONYMS_PROMPT = """\
Generate keyword synonyms and related terms for semantic search in a machine
learning knowledge base.

Keyword: $keyword

Return 5-10 related terms that would help find relevant entries:
["synonym1", "synonym2", ...]
"""
