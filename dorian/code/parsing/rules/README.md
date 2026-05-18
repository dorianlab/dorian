# Extraction rules (JSON)

This directory holds the JSON-spec rewrite rules the rust extractor
(`engine/extractor`) consumes. Each `.json` file is one rule; the
filename's leading number controls execution order
(`01_*` runs before `10_*` runs before `30_*`).

See the internal design note (not in public repo)
for the format reference and primitive catalogue.

## Status

This is the migration target for `dorian/code/parsing/rules.py`'s
~48 hand-written `RewriteRule` definitions. The current rule chain
covers the full sample-pipeline path end-to-end:

| File | Notes |
|---|---|
| `01_drop_noise_types.json` | tree-sitter noise tokens — `comment`, `string_start`, `string_end`, `string_content`, `from`, `as` (mirrors python's `types_to_delete`) |
| `02_drop_punctuation.json` | single-character tokens (`(`, `)`, `,`, `:`, …) |
| `03_drop_module_docstring.json` | module-level triple-quoted statements |
| `15_keyword_argument_to_parameter.json` | `k=v` → `Parameter(name=k, value=v, dtype=…)` |
| `20_expand_argument_list.json` | `f(X, y, k=v)` → wire X@0, y@1, v@k onto the call |
| `30_subscript_to_snippet.json` | `df['col']` → Snippet running the slice |
| `40_call_with_attribute_to_operator.json` | `obj.method(...)` → `Operator(name="obj.method")` (recursive delete drops attribute children) |
| `41_call_to_operator.json` | bare `foo(...)` → `Operator(name="foo")` |
| `45_resolve_imports.json` | walk import subtrees, build alias→FQN table, rewrite Operator names (`pd.read_csv` → `pandas.read_csv`, `RandomForestClassifier` → `sklearn.ensemble.RandomForestClassifier`), drop the imports |
| `50_collapse_assignment.json` | `scaler = StandardScaler()` → RHS → LHS edge + drop the assignment |
| `51_unpack_pattern_list.json` | `X, y = f()` → `f → X@out=0`, `f → y@out=1` |
| `60_chain_method_shortcuts.json` | `Operator(name="clf.fit")` → bare `fit` + `<producer>—self→fit` chain edge, with KB port-table rename of bumped numeric positions (`1`→`X`, `2`→`y`, …) |
| `61_resolve_var_references.json` | global pass: every identifier-use without a producer edge rewires to the matching LHS producer (preserving tuple-unpack output indices) |

The rust extractor is wired through the rust ``ExtractPipeline``
event handler in ``engine/backend/src/handlers/extraction.rs`` —
it calls the ``extractor`` crate directly (no FFI, no shim) and
persists the resulting ``Model`` to ``doc_extractions``. The
``$DORIAN_EXTRACTOR_RULES_DIR`` env var points the handler at this
directory at runtime.

## Remaining python rules to port (Phase C-2 backlog)

The python `rules.py` keeps a few specialised rules that aren't
exercised by the sample pipeline but matter for less-common shapes:

* `unary_operator` collapse — `-x` patterns. Needs a typed
  primitive that copies the inner type onto the outer node.
* `dotted_name → identifier` cleanup — auxiliary import-handling
  step. Likely subsumed by `45_resolve_imports`.
* `subscript Revert` — alternate path that turns slicing into a
  function call. Currently we route everything through
  `30_subscript_to_snippet`; the Revert path is only used by
  legacy code.
* `expression_statement` collapse — `clf.fit(X, y)` leaves an
  expression_statement wrapper around the Operator. Add a rule:
  `{"type": "delete", "nodes": ["0"], "mode": "isolated"}` for
  `expression_statement → Operator|call`.
* Compound-operator method expansion — runs *after* extraction,
  not during it; the KB sequences sklearn methods (`__init__`,
  `fit`, `predict`) into sub-DAGs at execution time. Tracked
  separately under `dorian/pipeline/compound_operator.py`.

## How rules execute

The engine's outer loop:

```
queue = [r1, r2, r3, ...]
while queue not empty:
    rule = queue.pop_front()
    processed = []
    while match_first(rule.pattern, dag, processed) is Some(m):
        processed.push(m)
        for op in rule.transformations:
            dag = apply(dag, m, op)
```

Each rule runs to fixpoint (until no new mapping satisfies its
pattern) before the next rule starts. Order in this directory
controls the execution sequence.

## Schema gaps that still need closing

* **Match-time-bound parameter**: python's `transformations.rules`
  field generates *per-match* sub-rules whose pattern references
  the matched node's text. JSON's equivalent is to lift such
  patterns into a global pass — `ResolveImports`,
  `ResolveVarReferences`, `ChainAllMethodShortcuts` all use this
  approach successfully.
* **Apply-with-data-projection**: the python `_update(g, m["0"],
  "type", g.nodes[m["2"]].type)` lambda is expressible as
  `update_attribute` with a `Ref` value — already supported.
* **Conditional gates**: a few python rules check `g.nodes[m["X"]].
  text in {...}` before applying. Today we keep the gate inside
  the primitive (e.g. `ChainAllMethodShortcuts` only fires for
  names in `METHOD_SHORTCUT_NAMES`). Adding a generic `"when"`
  clause would require a small expression DSL — defer until a use
  case actually demands it.
