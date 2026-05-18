//! Apply one transformation to a [`Region`] given a match mapping.
//!
//! Each variant in [`crate::rule::TransformationSpec`] dispatches to
//! a deterministic, named operation. No closures, no hidden state —
//! the same JSON spec produces the same Region mutation across runs.
//!
//! Layered approach:
//!
//! * Built-in primitives (`Delete`, `UpdateAttribute`, `AddEdges`,
//!   `ReplaceOperator`, `AddParameter`, `InsertBefore`,
//!   `InsertAfter`, `ToOperator`, `ToParameter`) — direct
//!   actor / relation manipulation, kept verbatim from the python
//!   `mcp.rule_compiler` semantics.
//! * Graph-walking primitives (`RewireVarUses`, `ChainMethod`,
//!   `UnpackPatternList`, `SubscriptToSnippet`, `ExpandArgList`) —
//!   wrap what the python `rules.py` Apply functions used to do;
//!   each is a typed operation with explicit parameter slots so
//!   JSON authors can reason about effects.
//! * Global passes (`ResolveVarReferences`,
//!   `ChainAllMethodShortcuts`, `ResolveImports`) — fire once
//!   per extraction and walk the whole region.

use crate::model::{
    ActorKind, ParameterLiteral, ParserPayload, PortKind, PortRef, Region, Relation,
};
use crate::pattern::Mapping;
use crate::rule::{DeleteMode, StructuredValue, TransformationSpec, ValueExpr};

/// Apply one transformation to a [`Region`]. Returns the mutated
/// region.
pub fn apply(region: Region, mapping: &Mapping, op: &TransformationSpec) -> Region {
    match op {
        TransformationSpec::Delete { nodes, edges, mode } => {
            apply_delete(region, mapping, nodes, edges, *mode)
        }
        TransformationSpec::UpdateAttribute {
            target,
            attribute,
            value,
        } => apply_update_attribute(region, mapping, target, attribute, value),
        TransformationSpec::ReplaceOperator { target, new_name } => {
            apply_replace_operator(region, mapping, target, new_name)
        }
        TransformationSpec::AddEdges { edges } => apply_add_edges(region, mapping, edges),
        TransformationSpec::ToOperator {
            target,
            content_key,
        } => apply_to_operator(region, mapping, target, content_key),
        TransformationSpec::ToParameter {
            target,
            kw_key,
            value_key,
        } => apply_to_parameter(region, mapping, target, kw_key, value_key),
        TransformationSpec::ResolveImports {} => apply_resolve_imports(region),
        TransformationSpec::AddParameter {
            target,
            param_name,
            param_value,
            param_dtype,
        } => apply_add_parameter(
            region,
            mapping,
            target,
            param_name,
            param_value,
            param_dtype,
        ),
        TransformationSpec::InsertBefore { target, new_operator } => {
            apply_insert_before(region, mapping, target, new_operator)
        }
        TransformationSpec::InsertAfter { target, new_operator } => {
            apply_insert_after(region, mapping, target, new_operator)
        }
        TransformationSpec::UnpackPatternList {
            pattern_list_key,
            source_call_key,
        } => apply_unpack_pattern_list(region, mapping, pattern_list_key, source_call_key),
        TransformationSpec::ExpandArgList {
            call_key,
            argument_list_key,
        } => apply_expand_arg_list(region, mapping, call_key, argument_list_key),
        TransformationSpec::SubscriptToSnippet { subscript_key } => {
            apply_subscript_to_snippet(region, mapping, subscript_key)
        }
        TransformationSpec::RewireVarUses {
            use_key,
            producer_id_from_match,
        } => apply_rewire_var_uses(region, mapping, use_key, producer_id_from_match),
        TransformationSpec::ChainMethod {
            op_key,
            producer_id_from_match,
            method_name,
        } => apply_chain_method(region, mapping, op_key, producer_id_from_match, method_name),
        TransformationSpec::ResolveVarReferences {} => apply_resolve_var_references(region),
        TransformationSpec::ChainAllMethodShortcuts {} => {
            apply_chain_all_method_shortcuts(region)
        }
    }
}

/// Region-aware [`apply_delete`]. Drops the matched actors (and any
/// concrete relations between them when ``edges`` is set). Mirrors
/// the legacy DAG semantics:
///
/// * `Isolated` — drop actor, rewire each upstream relation source
///   to each downstream relation destination so data flow continues
///   through the deleted node. Used by noise-removal rules.
/// * `Cascade` — drop the matched actors and any relation incident
///   to them; downstream consumers of the dropped output get
///   stranded.
/// * `Recursive` — recursively also drop the children (transitively)
///   of the matched actors. Used by attribute-promotion rules that
///   want the inner identifier subtree gone.
fn apply_delete(
    mut region: Region,
    mapping: &Mapping,
    pattern_nodes: &[String],
    pattern_edges: &[[String; 2]],
    mode: DeleteMode,
) -> Region {
    use rustc_hash::FxHashSet;

    let mut concrete_nodes: Vec<String> = pattern_nodes
        .iter()
        .filter_map(|p| mapping.get(p).cloned())
        .collect();
    let concrete_edges: Vec<(String, String)> = pattern_edges
        .iter()
        .filter_map(|e| {
            let s = mapping.get(&e[0])?;
            let d = mapping.get(&e[1])?;
            Some((s.clone(), d.clone()))
        })
        .collect();

    if matches!(mode, DeleteMode::Recursive) {
        let mut frontier: Vec<String> = concrete_nodes.clone();
        while let Some(nid) = frontier.pop() {
            // Children = destinations of any relation sourced at `nid`.
            let kids: Vec<String> = region
                .relations
                .iter()
                .filter(|r| r.sources.iter().any(|p| p.actor == nid))
                .flat_map(|r| r.destinations.iter().map(|p| p.actor.clone()))
                .collect();
            for child in kids {
                if !concrete_nodes.iter().any(|x| x == &child) {
                    concrete_nodes.push(child.clone());
                    frontier.push(child);
                }
            }
        }
    }

    if matches!(mode, DeleteMode::Isolated) {
        // For each deleted actor, bridge incoming → outgoing
        // relations so the data flow survives. We materialise the
        // bridges as fresh point-to-point relations and drop the
        // originals along with the actor.
        let mut bridges: Vec<Relation> = Vec::new();
        let mut bridge_idx: u32 = 0;
        for nid in &concrete_nodes {
            let incoming: Vec<&Relation> = region
                .relations
                .iter()
                .filter(|r| r.destinations.iter().any(|p| p.actor == *nid))
                .collect();
            let outgoing: Vec<&Relation> = region
                .relations
                .iter()
                .filter(|r| r.sources.iter().any(|p| p.actor == *nid))
                .collect();
            for inc in &incoming {
                for src in &inc.sources {
                    if src.actor == *nid {
                        continue;
                    }
                    for out in &outgoing {
                        for dst in &out.destinations {
                            if dst.actor == *nid {
                                continue;
                            }
                            bridges.push(Relation::point_to_point(
                                format!("rel:isolated-bridge:{}", bridge_idx),
                                src.clone(),
                                dst.clone(),
                            ));
                            bridge_idx += 1;
                        }
                    }
                }
            }
        }
        region.relations.extend(bridges);
    }

    let removed: FxHashSet<&str> = concrete_nodes.iter().map(|s| s.as_str()).collect();
    region.actors.retain(|a| !removed.contains(a.id.as_str()));

    region.relations.retain(|r| {
        let touches_removed = r.sources.iter().any(|p| removed.contains(p.actor.as_str()))
            || r.destinations.iter().any(|p| removed.contains(p.actor.as_str()));
        let in_explicit_drop = concrete_edges.iter().any(|(s, d)| {
            r.sources.iter().any(|p| p.actor == *s)
                && r.destinations.iter().any(|p| p.actor == *d)
        });
        !(touches_removed || in_explicit_drop)
    });

    region
}

/// Update one attribute on a target Actor. Mirrors
/// [`apply_update_attribute`].
///
/// Recognised attributes:
/// * ``type`` / ``text`` — write into the parser-leaf payload.
///   Useful for promotion-prep rules that mutate a leaf's AST type
///   before the rest of the chain matches it.
/// * ``name`` — Operator FQN, Parameter name, Snippet name,
///   Composite name.
/// * ``language`` — actor language tag.
/// * ``code`` — Snippet body.
/// * ``dtype`` / ``value`` — first inline ParameterLiteral on the
///   actor (legacy compat with rules that mutate parameter values).
fn apply_update_attribute(
    mut region: Region,
    mapping: &Mapping,
    target: &str,
    attribute: &str,
    value: &ValueExpr,
) -> Region {
    let target_id = match mapping.get(target) {
        Some(s) => s.clone(),
        None => return region,
    };
    let resolved = resolve_value(&region, mapping, value);
    if let Some(actor) = region.actor_mut(&target_id) {
        match attribute {
            "type" => actor.parser.r#type = resolved,
            "text" => actor.parser.text = resolved,
            "language" => actor.language = resolved,
            "name" => actor.name = resolved,
            "code" => actor.code = resolved,
            "dtype" => {
                if let Some(p) = actor.parameters.first_mut() {
                    p.dtype = resolved;
                }
            }
            "value" => {
                if let Some(p) = actor.parameters.first_mut() {
                    p.value = resolved;
                }
            }
            _ => {}
        }
    }
    region
}

fn resolve_value(region: &Region, mapping: &Mapping, value: &ValueExpr) -> String {
    match value {
        ValueExpr::Literal(s) => s.clone(),
        ValueExpr::Structured(StructuredValue::Ref { r#ref, attr }) => {
            let id = match mapping.get(r#ref) {
                Some(s) => s.as_str(),
                None => return String::new(),
            };
            let actor = match region.actor(id) {
                Some(a) => a,
                None => return String::new(),
            };
            match attr.as_str() {
                "type" => actor.parser.r#type.clone(),
                "text" => actor.parser.text.clone(),
                "name" => actor.name.clone(),
                "language" => actor.language.clone(),
                "code" => actor.code.clone(),
                _ => String::new(),
            }
        }
        ValueExpr::Structured(StructuredValue::Concat { concat }) => concat
            .iter()
            .map(|v| resolve_value(region, mapping, v))
            .collect(),
    }
}

/// Replace the matched Actor's payload with an Operator named
/// `new_name`. Mirrors [`apply_replace_operator`]. Clears the
/// parser-leaf payload, parameter-literal stash, and snippet
/// `code` field — a clean Operator slate.
fn apply_replace_operator(
    mut region: Region,
    mapping: &Mapping,
    target: &str,
    new_name: &str,
) -> Region {
    let target_id = match mapping.get(target) {
        Some(s) => s.clone(),
        None => return region,
    };
    if let Some(actor) = region.actor_mut(&target_id) {
        actor.kind = ActorKind::Operator;
        actor.name = new_name.to_string();
        actor.parser = ParserPayload::default();
        actor.parameters.clear();
        actor.code = String::new();
    }
    region
}

/// Add literal Relations between mapped actors. Mirrors
/// [`apply_add_edges`]. Each `[from, to]` pair is realised as a
/// point-to-point Relation with empty source-port and "0"
/// destination-port (the same defaults the legacy ``add_edges``
/// used; more specific port wiring goes through ``WirePorts`` —
/// added when the first rule needs it).
fn apply_add_edges(
    mut region: Region,
    mapping: &Mapping,
    edges: &[[String; 2]],
) -> Region {
    for (i, e) in edges.iter().enumerate() {
        let s = match mapping.get(&e[0]) {
            Some(s) => s.clone(),
            None => continue,
        };
        let d = match mapping.get(&e[1]) {
            Some(s) => s.clone(),
            None => continue,
        };
        let id = format!("rel:add-edge:{}->{}:{}", s, d, i);
        region.relations.push(Relation::point_to_point(
            id,
            PortRef {
                actor: s,
                port: String::new(),
            },
            PortRef {
                actor: d,
                port: "0".into(),
            },
        ));
    }
    region
}

/// Promote `target` Actor into an Operator whose name is taken
/// from the matched ``content_key`` actor's ``name`` (preferred)
/// or ``parser.text`` (fallback). Mirrors [`apply_to_operator`].
fn apply_to_operator(
    mut region: Region,
    mapping: &Mapping,
    target: &str,
    content_key: &str,
) -> Region {
    let target_id = match mapping.get(target) {
        Some(s) => s.clone(),
        None => return region,
    };
    let content_id = match mapping.get(content_key) {
        Some(s) => s.clone(),
        None => return region,
    };
    let (name, language) = region
        .actor(&content_id)
        .map(|a| {
            let n = if !a.name.is_empty() {
                a.name.clone()
            } else {
                a.parser.text.clone()
            };
            (n, a.language.clone())
        })
        .unwrap_or((String::new(), "python".to_string()));
    if let Some(actor) = region.actor_mut(&target_id) {
        actor.kind = ActorKind::Operator;
        actor.name = name;
        if !language.is_empty() {
            actor.language = language;
        }
        actor.parser = ParserPayload::default();
    }
    region
}

/// Promote `target` Actor into a Parameter whose ``name`` is
/// taken from the ``kw_key`` actor's text and whose ``value``
/// (and ``dtype`` heuristic) is taken from the ``value_key``
/// actor. Mirrors [`apply_to_parameter`].
///
/// `dtype` follows the python convention:
/// * `integer` AST type → `int`
/// * `float`             → `float`
/// * `string`            → `string`
/// * anything else       → `string`
fn apply_to_parameter(
    mut region: Region,
    mapping: &Mapping,
    target: &str,
    kw_key: &str,
    value_key: &str,
) -> Region {
    let target_id = match mapping.get(target) {
        Some(s) => s.clone(),
        None => return region,
    };
    let kw_id = match mapping.get(kw_key) {
        Some(s) => s.clone(),
        None => return region,
    };
    let value_id = match mapping.get(value_key) {
        Some(s) => s.clone(),
        None => return region,
    };
    let kw_text = region
        .actor(&kw_id)
        .map(|a| a.parser.text.clone())
        .unwrap_or_default();
    let (value_text, value_type) = region
        .actor(&value_id)
        .map(|a| (a.parser.text.clone(), a.parser.r#type.clone()))
        .unwrap_or_default();
    let dtype = match value_type.as_str() {
        "integer" => "int",
        "float" => "float",
        "string" => "string",
        _ => "string",
    }
    .to_string();
    if let Some(actor) = region.actor_mut(&target_id) {
        actor.kind = ActorKind::Parameter;
        actor.name = kw_text.clone();
        actor.parser = ParserPayload::default();
        actor.parameters.clear();
        actor.parameters.push(ParameterLiteral {
            name: kw_text,
            // Default destination port is the kwarg name; rules
            // that need a positional binding rewrite the port
            // afterwards via UpdateAttribute or a dedicated
            // primitive.
            port: actor.name.clone(),
            value: value_text,
            dtype,
        });
    }
    region
}

/// Walk every import-statement actor, build an alias→FQN table,
/// rewrite Operator names that begin with an alias prefix, and
/// drop the import subtrees. Region-aware port of
/// [`apply_resolve_imports`].
///
/// Tree-sitter import shapes (parser-leaf ``r#type``):
///   * ``import_statement`` — children: dotted_name(s) /
///     aliased_import.
///   * ``import_from_statement`` — children: module dotted_name +
///     imported names (dotted_name | identifier | aliased_import).
///   * ``aliased_import`` (standalone) — children: dotted_name +
///     identifier.
fn apply_resolve_imports(mut region: Region) -> Region {
    use rustc_hash::{FxHashMap, FxHashSet};

    let mut aliases: FxHashMap<String, String> = FxHashMap::default();
    let mut import_roots: Vec<String> = Vec::new();

    for a in &region.actors {
        if !matches!(a.kind, ActorKind::ParserLeaf) {
            continue;
        }
        match a.parser.r#type.as_str() {
            "import_statement" | "import_from_statement" | "aliased_import" => {
                import_roots.push(a.id.clone());
            }
            _ => {}
        }
    }

    let children_of = |parent: &str, region: &Region| -> Vec<String> {
        region
            .relations
            .iter()
            .filter(|r| r.sources.iter().any(|p| p.actor == parent))
            .flat_map(|r| r.destinations.iter().map(|p| p.actor.clone()))
            .collect()
    };

    for root_id in &import_roots {
        let root_type = match region.actor(root_id) {
            Some(a) => a.parser.r#type.clone(),
            None => continue,
        };
        let kids = children_of(root_id, &region);
        match root_type.as_str() {
            "import_statement" => {
                for kid in &kids {
                    let Some(kn) = region.actor(kid) else {
                        continue;
                    };
                    match kn.parser.r#type.as_str() {
                        "dotted_name" => {
                            if !kn.parser.text.is_empty() {
                                aliases
                                    .entry(kn.parser.text.clone())
                                    .or_insert(kn.parser.text.clone());
                            }
                        }
                        "aliased_import" => {
                            let mut module = String::new();
                            let mut alias = String::new();
                            for gk in children_of(kid, &region) {
                                if let Some(gn) = region.actor(&gk) {
                                    match gn.parser.r#type.as_str() {
                                        "dotted_name" if module.is_empty() => {
                                            module = gn.parser.text.clone();
                                        }
                                        "identifier" if alias.is_empty() => {
                                            alias = gn.parser.text.clone();
                                        }
                                        _ => {}
                                    }
                                }
                            }
                            if !alias.is_empty() && !module.is_empty() {
                                aliases.insert(alias, module);
                            }
                        }
                        _ => {}
                    }
                }
            }
            "import_from_statement" => {
                let mut module = String::new();
                for kid in &kids {
                    if let Some(kn) = region.actor(kid) {
                        if kn.parser.r#type == "dotted_name" && module.is_empty() {
                            module = kn.parser.text.clone();
                            break;
                        }
                    }
                }
                if module.is_empty() {
                    continue;
                }
                let mut seen_module = false;
                for kid in &kids {
                    let Some(kn) = region.actor(kid) else {
                        continue;
                    };
                    match kn.parser.r#type.as_str() {
                        "dotted_name" => {
                            if !seen_module {
                                seen_module = true;
                                continue;
                            }
                            if !kn.parser.text.is_empty() {
                                aliases.insert(
                                    kn.parser.text.clone(),
                                    format!("{module}.{}", kn.parser.text),
                                );
                            }
                        }
                        "identifier" => {
                            if !kn.parser.text.is_empty() {
                                aliases.insert(
                                    kn.parser.text.clone(),
                                    format!("{module}.{}", kn.parser.text),
                                );
                            }
                        }
                        "aliased_import" => {
                            let mut name = String::new();
                            let mut alias = String::new();
                            for gk in children_of(kid, &region) {
                                if let Some(gn) = region.actor(&gk) {
                                    match gn.parser.r#type.as_str() {
                                        "dotted_name" | "identifier" if name.is_empty() => {
                                            name = gn.parser.text.clone();
                                        }
                                        "identifier" => {
                                            alias = gn.parser.text.clone();
                                        }
                                        _ => {}
                                    }
                                }
                            }
                            if !name.is_empty() && !alias.is_empty() {
                                aliases.insert(alias, format!("{module}.{name}"));
                            }
                        }
                        _ => {}
                    }
                }
            }
            "aliased_import" => {
                let mut module = String::new();
                let mut alias = String::new();
                for gk in children_of(root_id, &region) {
                    if let Some(gn) = region.actor(&gk) {
                        match gn.parser.r#type.as_str() {
                            "dotted_name" => module = gn.parser.text.clone(),
                            "identifier" => alias = gn.parser.text.clone(),
                            _ => {}
                        }
                    }
                }
                if !alias.is_empty() && !module.is_empty() {
                    aliases.insert(alias, module);
                }
            }
            _ => {}
        }
    }

    // Compute the drop set BEFORE mutating actors (children_of
    // reads region.relations; doing it here avoids the usual
    // borrow-checker dance later in the function).
    let mut to_drop: FxHashSet<String> = FxHashSet::default();
    let mut frontier: Vec<String> = import_roots.clone();
    while let Some(id) = frontier.pop() {
        if !to_drop.insert(id.clone()) {
            continue;
        }
        for c in children_of(&id, &region) {
            frontier.push(c);
        }
    }

    // Apply aliases (longest-key-first so ``sklearn.ensemble`` wins
    // over ``sklearn``).
    let mut keys: Vec<String> = aliases.keys().cloned().collect();
    keys.sort_by(|a, b| b.len().cmp(&a.len()));
    for actor in region.actors.iter_mut() {
        if !matches!(actor.kind, ActorKind::Operator) || actor.name.is_empty() {
            continue;
        }
        for k in &keys {
            if actor.name == *k {
                actor.name = aliases[k].clone();
                break;
            }
            let prefix = format!("{k}.");
            if actor.name.starts_with(&prefix) {
                let suffix = &actor.name[prefix.len()..];
                actor.name = format!("{}.{}", aliases[k], suffix);
                break;
            }
        }
    }

    // Drop the import subtrees and relations incident to them.
    region.actors.retain(|a| !to_drop.contains(&a.id));
    region
        .relations
        .retain(|r| !r.sources.iter().any(|p| to_drop.contains(&p.actor))
            && !r.destinations.iter().any(|p| to_drop.contains(&p.actor)));
    region
}

/// Generate a fresh actor id. Mirrors the legacy
/// ``uuid::Uuid::new_v4()`` convention so id strings stay
/// recognisable across the migration.
fn fresh_actor_id() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}

/// Insert a Parameter actor and wire it as a kwarg into ``target``.
/// Mirrors [`apply_add_parameter`].
///
/// The new actor exposes a ``value`` output port. The target gains
/// (or reuses) a ``Kwarg``-kind input port named after
/// ``param_name``. A point-to-point Relation connects the two.
fn apply_add_parameter(
    mut region: Region,
    mapping: &Mapping,
    target: &str,
    param_name: &str,
    param_value: &str,
    param_dtype: &str,
) -> Region {
    let target_id = match mapping.get(target) {
        Some(s) => s.clone(),
        None => return region,
    };
    let new_id = fresh_actor_id();
    let mut p = Actor::parameter(new_id.clone(), param_name, param_value, param_dtype);
    p.upsert_output("value", PortKind::Data);
    region.actors.push(p);
    if let Some(t) = region.actor_mut(&target_id) {
        t.upsert_input(param_name, PortKind::Kwarg);
    }
    region.relations.push(Relation::point_to_point(
        format!("rel:add-param:{}->{}:{}", new_id, target_id, param_name),
        PortRef {
            actor: new_id,
            port: "value".into(),
        },
        PortRef {
            actor: target_id,
            port: param_name.into(),
        },
    ));
    region
}

/// Insert an Operator actor upstream of ``target``: rewire every
/// incoming relation of ``target`` to flow through the new actor
/// first. Mirrors [`apply_insert_before`].
///
/// The new actor gains a ``"0"`` Positional input port (where the
/// rerouted upstream sources land) and a ``"value"`` Data output
/// port (the new edge into ``target``). Each rerouted relation has
/// its destination PortRef rewritten to point at the new actor's
/// ``"0"`` port.
fn apply_insert_before(
    mut region: Region,
    mapping: &Mapping,
    target: &str,
    new_operator: &str,
) -> Region {
    let target_id = match mapping.get(target) {
        Some(s) => s.clone(),
        None => return region,
    };
    let new_id = fresh_actor_id();
    let mut op = Actor::operator(new_id.clone(), new_operator);
    op.upsert_input("0", PortKind::Positional);
    op.upsert_output("value", PortKind::Data);
    region.actors.push(op);

    for rel in region.relations.iter_mut() {
        for dst in rel.destinations.iter_mut() {
            if dst.actor == target_id {
                dst.actor = new_id.clone();
                dst.port = "0".into();
            }
        }
    }
    region.relations.push(Relation::point_to_point(
        format!("rel:insert-before:{}->{}", new_id, target_id),
        PortRef {
            actor: new_id,
            port: "value".into(),
        },
        PortRef {
            actor: target_id,
            port: "0".into(),
        },
    ));
    region
}

/// Insert an Operator actor downstream of ``target``: rewire every
/// outgoing relation of ``target`` to flow from the new actor.
/// Mirrors [`apply_insert_after`].
fn apply_insert_after(
    mut region: Region,
    mapping: &Mapping,
    target: &str,
    new_operator: &str,
) -> Region {
    let target_id = match mapping.get(target) {
        Some(s) => s.clone(),
        None => return region,
    };
    let new_id = fresh_actor_id();
    let mut op = Actor::operator(new_id.clone(), new_operator);
    op.upsert_input("0", PortKind::Positional);
    op.upsert_output("value", PortKind::Data);
    region.actors.push(op);

    for rel in region.relations.iter_mut() {
        for src in rel.sources.iter_mut() {
            if src.actor == target_id {
                src.actor = new_id.clone();
                src.port = "value".into();
            }
        }
    }
    region.relations.push(Relation::point_to_point(
        format!("rel:insert-after:{}->{}", target_id, new_id),
        PortRef {
            actor: target_id,
            port: String::new(),
        },
        PortRef {
            actor: new_id,
            port: "0".into(),
        },
    ));
    region
}

/// `X, y = f()` — fan a parser-leaf ``pattern_list`` into direct
/// `source_call.<i> → identifier_i."value"` Relations carrying the
/// tuple-slice index in the source port name. Mirrors
/// [`apply_unpack_pattern_list`].
///
/// Each identifier child of the pattern_list (in source order) becomes
/// the destination of a fresh Relation whose source port is the
/// stringified slice index. The pattern_list actor and any relation
/// incident to it are dropped.
fn apply_unpack_pattern_list(
    mut region: Region,
    mapping: &Mapping,
    pattern_list_key: &str,
    source_call_key: &str,
) -> Region {
    let pl_id = match mapping.get(pattern_list_key) {
        Some(s) => s.clone(),
        None => return region,
    };
    let parent_id = match mapping.get(source_call_key) {
        Some(s) => s.clone(),
        None => return region,
    };

    // Collect destination ids of relations sourced at pl_id (relation order).
    let mut candidate_dests: Vec<String> = Vec::new();
    for r in &region.relations {
        if r.sources.iter().any(|p| p.actor == pl_id) {
            for d in &r.destinations {
                candidate_dests.push(d.actor.clone());
            }
        }
    }
    // Filter to identifier parser leaves.
    let idents: Vec<String> = candidate_dests
        .into_iter()
        .filter(|id| {
            region
                .actor(id)
                .map(|a| {
                    matches!(a.kind, ActorKind::ParserLeaf) && a.parser.r#type == "identifier"
                })
                .unwrap_or(false)
        })
        .collect();

    // Drop relations incident to pattern_list.
    region.relations.retain(|r| {
        !r.sources.iter().any(|p| p.actor == pl_id)
            && !r.destinations.iter().any(|p| p.actor == pl_id)
    });

    // Make sure source_call has output ports for each slice and
    // each identifier has a "value" input port.
    if let Some(parent) = region.actor_mut(&parent_id) {
        for (i, _) in idents.iter().enumerate() {
            parent.upsert_output(&i.to_string(), PortKind::Data);
        }
    }
    for ident_id in &idents {
        if let Some(ident) = region.actor_mut(ident_id) {
            ident.upsert_input("value", PortKind::Data);
        }
    }

    // Wire source_call.<i> → identifier_i."value".
    for (i, ident_id) in idents.iter().enumerate() {
        region.relations.push(Relation::point_to_point(
            format!("rel:unpack:{}->{}:{}", parent_id, ident_id, i),
            PortRef {
                actor: parent_id.clone(),
                port: i.to_string(),
            },
            PortRef {
                actor: ident_id.clone(),
                port: "value".into(),
            },
        ));
    }

    // Drop the pattern_list actor.
    region.actors.retain(|a| a.id != pl_id);
    region
}

/// Wire a call's ``argument_list`` children into positional /
/// keyword input ports on the call. Mirrors [`apply_expand_arg_list`].
///
/// Parameter children land on a Kwarg-kind input port named after
/// the parameter; other children land on Positional ports named
/// "0", "1", …. Skips parser-leaf punctuation (`(`, `)`, `,`).
/// Drops the argument_list actor and incident relations.
fn apply_expand_arg_list(
    mut region: Region,
    mapping: &Mapping,
    call_key: &str,
    argument_list_key: &str,
) -> Region {
    let call_id = match mapping.get(call_key) {
        Some(s) => s.clone(),
        None => return region,
    };
    let al_id = match mapping.get(argument_list_key) {
        Some(s) => s.clone(),
        None => return region,
    };

    // Collect children destination ids in relation order.
    let mut candidate_dests: Vec<String> = Vec::new();
    for r in &region.relations {
        if r.sources.iter().any(|p| p.actor == al_id) {
            for d in &r.destinations {
                candidate_dests.push(d.actor.clone());
            }
        }
    }
    // Skip punctuation parser leaves; non-parser-leaf actors pass through.
    let children: Vec<String> = candidate_dests
        .into_iter()
        .filter(|id| {
            region
                .actor(id)
                .map(|a| {
                    if matches!(a.kind, ActorKind::ParserLeaf) {
                        !matches!(a.parser.r#type.as_str(), "(" | ")" | ",")
                    } else {
                        true
                    }
                })
                .unwrap_or(false)
        })
        .collect();

    // Drop relations incident to argument_list.
    region.relations.retain(|r| {
        !r.sources.iter().any(|p| p.actor == al_id)
            && !r.destinations.iter().any(|p| p.actor == al_id)
    });

    // For each child, wire its "value" output → call's positional /
    // kwarg input port.
    let mut pos_idx = 0u32;
    for cid in &children {
        let kwarg_name = region.actor(cid).and_then(|a| {
            if matches!(a.kind, ActorKind::Parameter) && !a.name.is_empty() {
                Some(a.name.clone())
            } else {
                None
            }
        });
        let (port_name, kind) = match kwarg_name {
            Some(kw) => (kw, PortKind::Kwarg),
            None => {
                let p = pos_idx.to_string();
                pos_idx += 1;
                (p, PortKind::Positional)
            }
        };
        if let Some(c) = region.actor_mut(&call_id) {
            c.upsert_input(&port_name, kind);
        }
        if let Some(child) = region.actor_mut(cid) {
            child.upsert_output("value", PortKind::Data);
        }
        region.relations.push(Relation::point_to_point(
            format!("rel:expand-arg:{}->{}:{}", cid, call_id, port_name),
            PortRef {
                actor: cid.clone(),
                port: "value".into(),
            },
            PortRef {
                actor: call_id.clone(),
                port: port_name,
            },
        ));
    }

    // Drop the argument_list actor.
    region.actors.retain(|a| a.id != al_id);
    region
}

/// Convert a parser-leaf ``subscript`` Actor into a Snippet that
/// runs the slice on the root identifier's value at runtime.
/// Mirrors [`apply_subscript_to_snippet`].
///
/// The subscript actor is flipped to ``Snippet`` with code
/// ``def foo(<root>): return <subscript_text>``. The subscript's
/// descendants are dropped EXCEPT for one descendant identifier
/// whose text matches the leading identifier of the subscript text;
/// that identifier survives so a downstream var-resolution rule
/// can rewire it to the actual producer. The kept identifier
/// becomes the source of a Relation pointing at the snippet's
/// ``<root>`` input port.
fn apply_subscript_to_snippet(
    mut region: Region,
    mapping: &Mapping,
    subscript_key: &str,
) -> Region {
    let sub_id = match mapping.get(subscript_key) {
        Some(s) => s.clone(),
        None => return region,
    };

    let (text, is_subscript) = region
        .actor(&sub_id)
        .map(|a| (a.parser.text.clone(), a.parser.r#type == "subscript"))
        .unwrap_or((String::new(), false));
    if !is_subscript {
        return region;
    }

    // Extract the leading identifier (root variable name).
    let mut end = text.len();
    for (i, ch) in text.char_indices() {
        if !(ch.is_ascii_alphanumeric() || ch == '_') {
            end = i;
            break;
        }
    }
    let root: String = if end > 0 {
        text[..end].to_string()
    } else {
        return region;
    };
    if root.is_empty() {
        return region;
    }

    let code = format!("def foo({root}):\n    return {text}\n");

    // BFS the subscript's descendants.
    let mut descendants: Vec<String> = Vec::new();
    {
        let mut frontier = vec![sub_id.clone()];
        let mut seen: rustc_hash::FxHashSet<String> = rustc_hash::FxHashSet::default();
        seen.insert(sub_id.clone());
        while let Some(id) = frontier.pop() {
            let kids: Vec<String> = region
                .relations
                .iter()
                .filter(|r| r.sources.iter().any(|p| p.actor == id))
                .flat_map(|r| r.destinations.iter().map(|d| d.actor.clone()))
                .collect();
            for k in kids {
                if seen.insert(k.clone()) {
                    descendants.push(k.clone());
                    frontier.push(k);
                }
            }
        }
    }

    // Find a descendant identifier whose text matches the root.
    let to_keep: Option<String> = descendants
        .iter()
        .find(|d| {
            region
                .actor(d)
                .map(|a| {
                    matches!(a.kind, ActorKind::ParserLeaf)
                        && a.parser.r#type == "identifier"
                        && a.parser.text == root
                })
                .unwrap_or(false)
        })
        .cloned();

    // Promote subscript actor to Snippet.
    if let Some(actor) = region.actor_mut(&sub_id) {
        actor.kind = ActorKind::Snippet;
        actor.code = code;
        actor.parser = ParserPayload::default();
        actor.upsert_input(&root, PortKind::Data);
    }

    // Drop descendants except the kept identifier.
    let drop_set: rustc_hash::FxHashSet<String> = descendants
        .iter()
        .filter(|d| Some(*d) != to_keep.as_ref())
        .cloned()
        .collect();
    region.actors.retain(|a| !drop_set.contains(&a.id));
    region.relations.retain(|r| {
        !r.sources.iter().any(|p| drop_set.contains(&p.actor))
            && !r.destinations.iter().any(|p| drop_set.contains(&p.actor))
    });

    // Rewire kept identifier: drop sub→ident relation, add ident→sub at port=root.
    if let Some(ident_id) = to_keep {
        region.relations.retain(|r| {
            !(r.sources.iter().any(|p| p.actor == sub_id)
                && r.destinations.iter().any(|p| p.actor == ident_id))
        });
        if let Some(ident) = region.actor_mut(&ident_id) {
            ident.upsert_output("value", PortKind::Data);
        }
        region.relations.push(Relation::point_to_point(
            format!("rel:subscript:{}->{}", ident_id, sub_id),
            PortRef {
                actor: ident_id,
                port: "value".into(),
            },
            PortRef {
                actor: sub_id.clone(),
                port: root,
            },
        ));
    }

    region
}

/// Replace every outgoing Relation of `use_key` with one whose
/// source is `producer_id_from_match`'s output port. Mirrors
/// [`apply_rewire_var_uses`].
///
/// Method-shortcut producers expose two output ports
/// (``instance``, ``result``); a use-site rewires off ``result``.
/// Non-method producers expose a single output port (``"0"`` /
/// ``"value"``); rewiring sources off the first available output
/// port. The use actor itself is dropped.
fn apply_rewire_var_uses(
    mut region: Region,
    mapping: &Mapping,
    use_key: &str,
    producer_id_from_match: &str,
) -> Region {
    let use_id = match mapping.get(use_key) {
        Some(s) => s.clone(),
        None => return region,
    };
    let producer_id = match mapping.get(producer_id_from_match) {
        Some(s) => s.clone(),
        None => return region,
    };
    let prod_port = method_shortcut_output_port(&region, &producer_id);

    for rel in region.relations.iter_mut() {
        for src in rel.sources.iter_mut() {
            if src.actor == use_id {
                src.actor = producer_id.clone();
                src.port = prod_port.clone();
            }
        }
    }
    // Drop relations where the use is now also a destination — those
    // were the producer→use edges that no longer have meaning.
    region
        .relations
        .retain(|r| !r.destinations.iter().any(|p| p.actor == use_id));
    region.actors.retain(|a| a.id != use_id);
    region
}

/// Determine which output port of the producer carries the
/// "downstream value." Method-shortcut producers expose
/// ``"result"`` (the method's actual return value); non-method
/// producers expose ``"value"`` (their single output) — falling
/// back to ``"0"`` when neither is declared.
fn method_shortcut_output_port(region: &Region, producer_id: &str) -> String {
    let actor = match region.actor(producer_id) {
        Some(a) => a,
        None => return "0".into(),
    };
    let is_method = matches!(actor.kind, ActorKind::Operator)
        && (METHOD_SHORTCUT_NAMES.iter().any(|m| *m == actor.name)
            || actor
                .name
                .rsplit_once('.')
                .map(|(_, m)| METHOD_SHORTCUT_NAMES.iter().any(|x| *x == m))
                .unwrap_or(false));
    if is_method {
        // Prefer the canonical "result" port name (Q2 design); fall
        // back to "1" for relations that haven't been refined yet.
        if actor.outputs.iter().any(|p| p.name == "result") {
            return "result".into();
        }
        return "1".into();
    }
    if actor.outputs.iter().any(|p| p.name == "value") {
        return "value".into();
    }
    "0".into()
}

/// Replace `op_key` (an Operator named ``<var>.<method>``) with
/// the bare method shortcut + add a chain Relation from
/// `producer_id` at port ``"self"``. Existing positional
/// destination ports shift +1 to free the chain slot; the KB port
/// table renames numeric positions to semantic names. Mirrors
/// [`apply_chain_method`].
fn apply_chain_method(
    mut region: Region,
    mapping: &Mapping,
    op_key: &str,
    producer_id_from_match: &str,
    method_name: &str,
) -> Region {
    let op_id = match mapping.get(op_key) {
        Some(s) => s.clone(),
        None => return region,
    };
    let producer_id = match mapping.get(producer_id_from_match) {
        Some(s) => s.clone(),
        None => return region,
    };
    let port_table = kb_port_table(method_name);

    if let Some(actor) = region.actor_mut(&op_id) {
        actor.name = method_name.to_string();
        // Make sure the method actor exposes both ``instance`` and
        // ``result`` outputs (Q2 design).
        actor.upsert_output("instance", PortKind::Data);
        actor.upsert_output("result", PortKind::Data);
        actor.upsert_input("self", PortKind::SelfRef);
    }

    // Bump positional destination ports +1 and rename via KB table.
    for rel in region.relations.iter_mut() {
        for d in rel.destinations.iter_mut() {
            if d.actor != op_id {
                continue;
            }
            if let Ok(n) = d.port.parse::<i64>() {
                let bumped = n + 1;
                d.port = port_table
                    .get(&bumped)
                    .cloned()
                    .unwrap_or_else(|| bumped.to_string());
            }
        }
    }
    // Add the chain edge: producer.<output> → op.self.
    let prod_port = if let Some(p) = region.actor(&producer_id) {
        if p.outputs.iter().any(|x| x.name == "instance") {
            "instance".to_string()
        } else if p.outputs.iter().any(|x| x.name == "value") {
            "value".to_string()
        } else {
            "0".to_string()
        }
    } else {
        "0".to_string()
    };
    region.relations.push(Relation::point_to_point(
        format!("rel:chain:{}->{}", producer_id, op_id),
        PortRef {
            actor: producer_id,
            port: prod_port,
        },
        PortRef {
            actor: op_id,
            port: "self".into(),
        },
    ));
    region
}

/// Global pass: for every parser-leaf identifier with an incoming
/// producer Relation (an LHS), rewire every other parser-leaf
/// identifier with the same text but no incoming producer (a use)
/// to source from the LHS's producer. Mirrors
/// [`apply_resolve_var_references`].
///
/// The producer→LHS Relation's source-port preserves output index
/// for tuple-unpack — a use of ``y`` resolves to ``producer.<port>``
/// where ``<port>`` is whatever port the LHS was bound at. Falls
/// back to the method-shortcut heuristic only when the bound port
/// is the default ``""`` / ``"0"``.
fn apply_resolve_var_references(mut region: Region) -> Region {
    use rustc_hash::{FxHashMap, FxHashSet};

    // Build text → (producer_actor, producer_output_port) for every
    // identifier with an incoming relation.
    let mut producers: FxHashMap<String, (String, String)> = FxHashMap::default();
    for actor in &region.actors {
        if !matches!(actor.kind, ActorKind::ParserLeaf)
            || actor.parser.r#type != "identifier"
            || actor.parser.text.is_empty()
        {
            continue;
        }
        if let Some(rel) = region
            .relations
            .iter()
            .find(|r| r.destinations.iter().any(|p| p.actor == actor.id))
        {
            if let Some(src) = rel.sources.first() {
                producers
                    .entry(actor.parser.text.clone())
                    .or_insert((src.actor.clone(), src.port.clone()));
            }
        }
    }
    if producers.is_empty() {
        return region;
    }

    // Collect identifier-use ids: parser-leaf identifiers WITHOUT
    // an incoming relation, whose text appears in the producers map.
    let with_incoming: FxHashSet<String> = region
        .relations
        .iter()
        .flat_map(|r| r.destinations.iter().map(|p| p.actor.clone()))
        .collect();
    let uses: Vec<(String, String)> = region
        .actors
        .iter()
        .filter_map(|a| {
            if !matches!(a.kind, ActorKind::ParserLeaf)
                || a.parser.r#type != "identifier"
                || with_incoming.contains(&a.id)
            {
                return None;
            }
            if !producers.contains_key(&a.parser.text) {
                return None;
            }
            Some((a.id.clone(), a.parser.text.clone()))
        })
        .collect();

    for (use_id, text) in &uses {
        let (prod_id, prod_port_raw) = match producers.get(text) {
            Some(p) => p.clone(),
            None => continue,
        };
        // Resolve the output port the rewire should source from.
        // If the LHS's incoming relation already specified a non-empty
        // port (set by unpack_pattern_list etc.), use it; otherwise
        // fall back to the method-shortcut heuristic.
        let prod_port = if !prod_port_raw.is_empty() && prod_port_raw != "0" {
            prod_port_raw
        } else {
            method_shortcut_output_port(&region, &prod_id)
        };
        // Rewrite every relation whose source was use_id.
        for rel in region.relations.iter_mut() {
            for src in rel.sources.iter_mut() {
                if src.actor == *use_id {
                    src.actor = prod_id.clone();
                    src.port = prod_port.clone();
                }
            }
        }
        // Drop relations where use_id was also a destination.
        region
            .relations
            .retain(|r| !r.destinations.iter().any(|p| p.actor == *use_id));
    }

    let removed: FxHashSet<String> = uses.iter().map(|(id, _)| id.clone()).collect();
    region.actors.retain(|a| !removed.contains(&a.id));
    region
}

/// Global pass: for every Operator whose name has shape
/// ``<var>.<method>`` (with ``<method>`` in
/// [`METHOD_SHORTCUT_NAMES`]), rename to the bare method and add a
/// chain Relation from ``<var>``'s producer at port ``"self"``.
/// Existing positional destination ports shift +1 and get renamed
/// via the KB port table. Mirrors
/// [`apply_chain_all_method_shortcuts`].
fn apply_chain_all_method_shortcuts(mut region: Region) -> Region {
    use rustc_hash::FxHashMap;

    // Producer-for-var map: identifier text → producer actor id.
    let mut producer_for_var: FxHashMap<String, String> = FxHashMap::default();
    for actor in &region.actors {
        if !matches!(actor.kind, ActorKind::ParserLeaf)
            || actor.parser.r#type != "identifier"
            || actor.parser.text.is_empty()
        {
            continue;
        }
        let producer = region
            .relations
            .iter()
            .find(|r| r.destinations.iter().any(|p| p.actor == actor.id))
            .and_then(|r| r.sources.first().map(|p| p.actor.clone()));
        if let Some(p) = producer {
            producer_for_var
                .entry(actor.parser.text.clone())
                .or_insert(p);
        }
    }

    // Identify candidate ops to chain: (op_id, producer_id, method).
    let candidates: Vec<(String, String, String)> = region
        .actors
        .iter()
        .filter_map(|a| {
            if !matches!(a.kind, ActorKind::Operator) {
                return None;
            }
            let (var_text, method) = a.name.rsplit_once('.')?;
            if !METHOD_SHORTCUT_NAMES.iter().any(|m| *m == method) {
                return None;
            }
            let producer = producer_for_var.get(var_text)?;
            Some((a.id.clone(), producer.clone(), method.to_string()))
        })
        .collect();

    // Snapshot the wrapper-source actor ids (parser leaves of type
    // module / expression_statement) before mutating relations —
    // those relations carry no operand semantics, only AST
    // ancestry, and would shadow real arg wiring once their numeric
    // destination ports get bumped/renamed below.
    let wrapper_actors: rustc_hash::FxHashSet<String> = region
        .actors
        .iter()
        .filter(|a| {
            matches!(a.kind, ActorKind::ParserLeaf)
                && matches!(a.parser.r#type.as_str(), "module" | "expression_statement")
        })
        .map(|a| a.id.clone())
        .collect();

    for (op_id, producer_id, method) in candidates {
        let port_table = kb_port_table(&method);
        if let Some(actor) = region.actor_mut(&op_id) {
            actor.name = method.clone();
            actor.upsert_output("instance", PortKind::Data);
            actor.upsert_output("result", PortKind::Data);
            actor.upsert_input("self", PortKind::SelfRef);
        }

        // Drop wrapper-source relations landing on op_id.
        region.relations.retain(|r| {
            let dst_op = r.destinations.iter().any(|p| p.actor == op_id);
            let src_wrapper = r.sources.iter().any(|p| wrapper_actors.contains(&p.actor));
            !(dst_op && src_wrapper)
        });

        // Bump numeric destination ports +1 and KB-rename.
        for rel in region.relations.iter_mut() {
            for d in rel.destinations.iter_mut() {
                if d.actor != op_id {
                    continue;
                }
                if let Ok(n) = d.port.parse::<i64>() {
                    let bumped = n + 1;
                    d.port = port_table
                        .get(&bumped)
                        .cloned()
                        .unwrap_or_else(|| bumped.to_string());
                }
            }
        }

        // Add the chain Relation: producer's instance/value/0 → op.self.
        let prod_port = if let Some(p) = region.actor(&producer_id) {
            if p.outputs.iter().any(|x| x.name == "instance") {
                "instance".to_string()
            } else if p.outputs.iter().any(|x| x.name == "value") {
                "value".to_string()
            } else {
                "0".to_string()
            }
        } else {
            "0".to_string()
        };
        region.relations.push(Relation::point_to_point(
            format!("rel:chain-shortcut:{}->{}", producer_id, op_id),
            PortRef {
                actor: producer_id,
                port: prod_port,
            },
            PortRef {
                actor: op_id,
                port: "self".into(),
            },
        ));
    }
    region
}
/// Method shortcuts the runtime resolver knows how to dispatch.
/// Mirrors python ``_METHOD_SHORTCUT_NAMES`` in
/// ``dorian/code/parsing/rules.py``.
const METHOD_SHORTCUT_NAMES: &[&str] = &[
    "fit",
    "predict",
    "transform",
    "fit_transform",
    "fit_predict",
    "predict_proba",
    "decision_function",
    "score",
    "score_samples",
    "inverse_transform",
    "validate",
    "create",
];

/// KB port table for a method shortcut — `{position_int → semantic_name}`.
///
/// Reads `optimizer::kb::KbSnapshot::method_io` for each interface
/// that owns the method (`Sklearn Estimator`, `Sklearn Transformer`,
/// `Sklearn Supervised Transformer`, …) and projects the input
/// `(name, position)` pairs into the table. First-wins on conflict —
/// the python ``_build_method_port_table`` uses the same rule.
///
/// Falls back to the inline canonical defaults when the snapshot is
/// missing or the method isn't declared (handy for tests that don't
/// load a snapshot).
pub fn kb_port_table_from_snapshot(
    snap: &optimizer::kb::KbSnapshot,
    method_name: &str,
) -> rustc_hash::FxHashMap<i64, String> {
    let mut t = rustc_hash::FxHashMap::default();
    for iface in [
        "Sklearn Estimator",
        "Sklearn Transformer",
        "Sklearn Supervised Transformer",
    ] {
        let mio = snap.method_io(iface);
        if let Some((ins, _)) = mio.get(method_name) {
            for inp in ins {
                let pos = match inp.position.parse::<i64>() {
                    Ok(n) => n,
                    Err(_) => continue,
                };
                if inp.name.is_empty()
                    || inp.name == "self"
                    || inp.name.chars().all(|c| c.is_ascii_digit())
                {
                    continue;
                }
                t.entry(pos).or_insert_with(|| inp.name.clone());
            }
        }
    }
    t
}

/// Built-in canonical defaults — used when no snapshot is available
/// (tests, bootstrap-time) so chain_method still emits semantic
/// positions for the common Sklearn methods.
fn kb_port_table(method_name: &str) -> rustc_hash::FxHashMap<i64, String> {
    let mut t = rustc_hash::FxHashMap::default();
    match method_name {
        "fit" | "fit_transform" | "fit_predict" => {
            t.insert(1, "X".into());
            t.insert(2, "y".into());
        }
        "predict" | "predict_proba" | "transform" | "decision_function" | "score_samples" => {
            t.insert(1, "X_test".into());
        }
        "score" => {
            t.insert(1, "X_test".into());
            t.insert(2, "y".into());
        }
        _ => {}
    }
    t
}


#[cfg(test)]
mod tests {
    use super::*;
    // ── Region-aware primitive tests ────────────────────────────────────────

    #[test]
    fn region_delete_isolated_bridges_through() {
        // Region: a → b → c. Delete b in isolated mode.
        // Expected: a → c (single bridge relation), b dropped, the
        // two original a→b and b→c relations gone.
        use crate::model::{Actor, PortRef, Region, Relation};
        let region = Region::new()
            .with_actor(Actor::parser_leaf("a", "x", "x"))
            .with_actor(Actor::parser_leaf("b", "y", "y"))
            .with_actor(Actor::parser_leaf("c", "z", "z"))
            .with_relation(Relation::point_to_point(
                "rel:a->b",
                PortRef { actor: "a".into(), port: String::new() },
                PortRef { actor: "b".into(), port: "0".into() },
            ))
            .with_relation(Relation::point_to_point(
                "rel:b->c",
                PortRef { actor: "b".into(), port: String::new() },
                PortRef { actor: "c".into(), port: "0".into() },
            ));

        let mut m: Mapping = rustc_hash::FxHashMap::default();
        m.insert("0".into(), "b".into());
        let out = apply_delete(region, &m, &["0".into()], &[], DeleteMode::Isolated);
        assert!(out.actors.iter().all(|a| a.id != "b"));
        // Some relation must connect a → c after the bridge.
        assert!(
            out.relations.iter().any(|r| r
                .sources
                .iter()
                .any(|p| p.actor == "a")
                && r.destinations.iter().any(|p| p.actor == "c")),
            "expected a → c bridge after isolated delete"
        );
    }

    #[test]
    fn region_delete_recursive_drops_subtree() {
        // Region: a → b, a → c (a has two children). Delete a in
        // recursive mode; expected: a, b, c all gone.
        use crate::model::{Actor, PortRef, Region, Relation};
        let region = Region::new()
            .with_actor(Actor::parser_leaf("a", "x", "x"))
            .with_actor(Actor::parser_leaf("b", "y", "y"))
            .with_actor(Actor::parser_leaf("c", "z", "z"))
            .with_relation(Relation::point_to_point(
                "rel:a->b",
                PortRef { actor: "a".into(), port: String::new() },
                PortRef { actor: "b".into(), port: "0".into() },
            ))
            .with_relation(Relation::point_to_point(
                "rel:a->c",
                PortRef { actor: "a".into(), port: String::new() },
                PortRef { actor: "c".into(), port: "1".into() },
            ));

        let mut m: Mapping = rustc_hash::FxHashMap::default();
        m.insert("0".into(), "a".into());
        let out = apply_delete(region, &m, &["0".into()], &[], DeleteMode::Recursive);
        assert!(out.actors.is_empty(), "recursive delete should drop a + descendants");
        assert!(out.relations.is_empty());
    }

    #[test]
    fn region_delete_cascade_drops_only_matched_actor() {
        use crate::model::{Actor, PortRef, Region, Relation};
        let region = Region::new()
            .with_actor(Actor::parser_leaf("a", "x", "x"))
            .with_actor(Actor::parser_leaf("b", "y", "y"))
            .with_relation(Relation::point_to_point(
                "rel:a->b",
                PortRef { actor: "a".into(), port: String::new() },
                PortRef { actor: "b".into(), port: "0".into() },
            ));

        let mut m: Mapping = rustc_hash::FxHashMap::default();
        m.insert("0".into(), "a".into());
        let out = apply_delete(region, &m, &["0".into()], &[], DeleteMode::Cascade);
        assert!(out.actors.iter().all(|act| act.id != "a"));
        assert!(
            out.actors.iter().any(|act| act.id == "b"),
            "cascade should leave b alive"
        );
        // Relations incident to the deleted actor are gone.
        assert!(out.relations.is_empty());
    }

    #[test]
    fn region_update_attribute_writes_into_parser_payload() {
        use crate::model::{Actor, Region};
        use crate::rule::ValueExpr;
        let region = Region::new().with_actor(Actor::parser_leaf("0", "old", "x"));
        let mut m: Mapping = rustc_hash::FxHashMap::default();
        m.insert("t".into(), "0".into());
        let out = apply_update_attribute(
            region,
            &m,
            "t",
            "type",
            &ValueExpr::Literal("identifier".into()),
        );
        assert_eq!(out.actor("0").unwrap().parser.r#type, "identifier");
    }

    #[test]
    fn region_replace_operator_clears_parser_payload() {
        use crate::model::{Actor, Region};
        let region = Region::new().with_actor(Actor::parser_leaf("0", "call", "f()"));
        let mut m: Mapping = rustc_hash::FxHashMap::default();
        m.insert("t".into(), "0".into());
        let out = apply_replace_operator(region, &m, "t", "sklearn.foo");
        let actor = out.actor("0").unwrap();
        assert!(matches!(actor.kind, ActorKind::Operator));
        assert_eq!(actor.name, "sklearn.foo");
        assert!(actor.parser.r#type.is_empty());
        assert!(actor.parser.text.is_empty());
    }

    #[test]
    fn region_add_edges_creates_relation_between_mapped_actors() {
        use crate::model::{Actor, Region};
        let region = Region::new()
            .with_actor(Actor::parser_leaf("a", "x", "x"))
            .with_actor(Actor::parser_leaf("b", "y", "y"));
        let mut m: Mapping = rustc_hash::FxHashMap::default();
        m.insert("0".into(), "a".into());
        m.insert("1".into(), "b".into());
        let out = apply_add_edges(
            region,
            &m,
            &[["0".into(), "1".into()]],
        );
        assert!(out.relations.iter().any(|r| r
            .sources
            .iter()
            .any(|p| p.actor == "a")
            && r.destinations.iter().any(|p| p.actor == "b")));
    }

    #[test]
    fn region_to_operator_promotes_using_content_text() {
        // ``call(0) → identifier(1, text="RandomForestClassifier")``
        // promotes call to Operator(name="RandomForestClassifier").
        use crate::model::{Actor, Region};
        let region = Region::new()
            .with_actor(Actor::parser_leaf("0", "call", "RandomForestClassifier(...)"))
            .with_actor(Actor::parser_leaf("1", "identifier", "RandomForestClassifier"));
        let mut m: Mapping = rustc_hash::FxHashMap::default();
        m.insert("0".into(), "0".into());
        m.insert("1".into(), "1".into());
        let out = apply_to_operator(region, &m, "0", "1");
        let actor = out.actor("0").unwrap();
        assert!(matches!(actor.kind, ActorKind::Operator));
        assert_eq!(actor.name, "RandomForestClassifier");
        assert!(actor.parser.r#type.is_empty());
    }

    #[test]
    fn region_to_parameter_packs_kw_and_value_with_dtype() {
        // ``keyword_argument(0) → identifier(1, text="random_state") +
        //   integer(2, text="42")`` promotes the kwarg to
        // Parameter(name="random_state", value="42", dtype="int").
        use crate::model::{Actor, Region};
        let region = Region::new()
            .with_actor(Actor::parser_leaf("0", "keyword_argument", "random_state=42"))
            .with_actor(Actor::parser_leaf("1", "identifier", "random_state"))
            .with_actor(Actor::parser_leaf("2", "integer", "42"));
        let mut m: Mapping = rustc_hash::FxHashMap::default();
        m.insert("0".into(), "0".into());
        m.insert("1".into(), "1".into());
        m.insert("2".into(), "2".into());
        let out = apply_to_parameter(region, &m, "0", "1", "2");
        let actor = out.actor("0").unwrap();
        assert!(matches!(actor.kind, ActorKind::Parameter));
        assert_eq!(actor.name, "random_state");
        let p = &actor.parameters[0];
        assert_eq!(p.value, "42");
        assert_eq!(p.dtype, "int");
    }

    #[test]
    fn region_resolve_imports_rewrites_from_import_to_fqn() {
        use crate::model::{Actor, PortRef, Region, Relation};
        let region = Region::new()
            .with_actor(Actor::parser_leaf("ifs", "import_from_statement", ""))
            .with_actor(Actor::parser_leaf("module", "dotted_name", "sklearn.ensemble"))
            .with_actor(Actor::parser_leaf("name", "dotted_name", "RandomForestClassifier"))
            .with_actor({
                let mut a = Actor::operator("rf", "RandomForestClassifier");
                a.kind = ActorKind::Operator;
                a
            })
            .with_relation(Relation::point_to_point(
                "rel:ifs->module",
                PortRef { actor: "ifs".into(), port: String::new() },
                PortRef { actor: "module".into(), port: "0".into() },
            ))
            .with_relation(Relation::point_to_point(
                "rel:ifs->name",
                PortRef { actor: "ifs".into(), port: String::new() },
                PortRef { actor: "name".into(), port: "1".into() },
            ));
        let out = apply_resolve_imports(region);
        assert_eq!(
            out.actor("rf").unwrap().name,
            "sklearn.ensemble.RandomForestClassifier"
        );
        // The import subtree is gone.
        assert!(out.actor("ifs").is_none());
        assert!(out.actor("module").is_none());
        assert!(out.actor("name").is_none());
    }

    #[test]
    fn region_add_parameter_creates_actor_and_kwarg_relation() {
        use crate::model::{Actor, Region};
        let region = Region::new().with_actor(Actor::operator("t", "RandomForestClassifier"));
        let mut m: Mapping = rustc_hash::FxHashMap::default();
        m.insert("0".into(), "t".into());
        let out = apply_add_parameter(region, &m, "0", "random_state", "42", "int");
        // A new Parameter actor exists with the right name/value.
        let param = out.actors.iter().find(|a| matches!(a.kind, ActorKind::Parameter));
        assert!(param.is_some());
        let p = param.unwrap();
        assert_eq!(p.name, "random_state");
        assert_eq!(p.parameters[0].value, "42");
        assert_eq!(p.parameters[0].dtype, "int");
        // Relation: param.value → t.random_state
        let rel = out.relations.iter().find(|r| {
            r.destinations
                .iter()
                .any(|d| d.actor == "t" && d.port == "random_state")
        });
        assert!(rel.is_some());
        // Target now has a Kwarg input port named "random_state".
        let t = out.actor("t").unwrap();
        let port = t.inputs.iter().find(|p| p.name == "random_state");
        assert!(port.is_some());
        assert!(matches!(port.unwrap().kind, PortKind::Kwarg));
    }

    #[test]
    fn region_insert_before_reroutes_incoming_through_new_actor() {
        // Region: a → t. Insert "preprocessor" before t.
        // Expected: a → preprocessor → t (a's relation now points at
        // the new actor's "0" port).
        use crate::model::{Actor, PortRef, Region, Relation};
        let region = Region::new()
            .with_actor(Actor::parser_leaf("a", "x", "x"))
            .with_actor(Actor::operator("t", "Target"))
            .with_relation(Relation::point_to_point(
                "rel:a->t",
                PortRef { actor: "a".into(), port: String::new() },
                PortRef { actor: "t".into(), port: "0".into() },
            ));
        let mut m: Mapping = rustc_hash::FxHashMap::default();
        m.insert("0".into(), "t".into());
        let out = apply_insert_before(region, &m, "0", "Preprocessor");
        // The new operator exists.
        let new_op = out
            .actors
            .iter()
            .find(|a| matches!(a.kind, ActorKind::Operator) && a.name == "Preprocessor");
        assert!(new_op.is_some());
        let new_id = &new_op.unwrap().id;
        // a's outgoing relation now lands on the new operator.
        assert!(out.relations.iter().any(|r| r
            .sources
            .iter()
            .any(|p| p.actor == "a")
            && r.destinations.iter().any(|p| p.actor == *new_id)));
        // A fresh relation runs new_op → t.
        assert!(out.relations.iter().any(|r| r
            .sources
            .iter()
            .any(|p| p.actor == *new_id)
            && r.destinations.iter().any(|p| p.actor == "t")));
    }

    #[test]
    fn region_insert_after_reroutes_outgoing_from_new_actor() {
        // Region: t → c. Insert "postprocessor" after t.
        // Expected: t → postprocessor → c.
        use crate::model::{Actor, PortRef, Region, Relation};
        let region = Region::new()
            .with_actor(Actor::operator("t", "Target"))
            .with_actor(Actor::parser_leaf("c", "z", "z"))
            .with_relation(Relation::point_to_point(
                "rel:t->c",
                PortRef { actor: "t".into(), port: String::new() },
                PortRef { actor: "c".into(), port: "0".into() },
            ));
        let mut m: Mapping = rustc_hash::FxHashMap::default();
        m.insert("0".into(), "t".into());
        let out = apply_insert_after(region, &m, "0", "Postprocessor");
        let new_op = out
            .actors
            .iter()
            .find(|a| matches!(a.kind, ActorKind::Operator) && a.name == "Postprocessor");
        assert!(new_op.is_some());
        let new_id = &new_op.unwrap().id;
        // The original relation's source now points at the new actor.
        assert!(out.relations.iter().any(|r| r
            .sources
            .iter()
            .any(|p| p.actor == *new_id)
            && r.destinations.iter().any(|p| p.actor == "c")));
        // A fresh relation runs t → new_op.
        assert!(out.relations.iter().any(|r| r
            .sources
            .iter()
            .any(|p| p.actor == "t")
            && r.destinations.iter().any(|p| p.actor == *new_id)));
    }

    #[test]
    fn region_unpack_pattern_list_fans_outputs() {
        // ``X, y = make_classification(...)`` — pattern_list children
        // (X, y) become destinations of relations from
        // make_classification's "0" / "1" output ports.
        use crate::model::{Actor, PortRef, Region, Relation};
        let region = Region::new()
            .with_actor(Actor::operator("call", "sklearn.datasets.make_classification"))
            .with_actor(Actor::parser_leaf("pl", "pattern_list", "X, y"))
            .with_actor(Actor::parser_leaf("X", "identifier", "X"))
            .with_actor(Actor::parser_leaf("y", "identifier", "y"))
            .with_relation(Relation::point_to_point(
                "rel:call->pl",
                PortRef { actor: "call".into(), port: String::new() },
                PortRef { actor: "pl".into(), port: "0".into() },
            ))
            .with_relation(Relation::point_to_point(
                "rel:pl->X",
                PortRef { actor: "pl".into(), port: String::new() },
                PortRef { actor: "X".into(), port: "0".into() },
            ))
            .with_relation(Relation::point_to_point(
                "rel:pl->y",
                PortRef { actor: "pl".into(), port: String::new() },
                PortRef { actor: "y".into(), port: "1".into() },
            ));
        let mut m: Mapping = rustc_hash::FxHashMap::default();
        m.insert("0".into(), "call".into());
        m.insert("1".into(), "pl".into());
        let out = apply_unpack_pattern_list(region, &m, "1", "0");
        assert!(out.actor("pl").is_none(), "pattern_list dropped");
        // call.0 → X.value
        assert!(out.relations.iter().any(|r| r
            .sources
            .iter()
            .any(|p| p.actor == "call" && p.port == "0")
            && r.destinations.iter().any(|p| p.actor == "X")));
        // call.1 → y.value
        assert!(out.relations.iter().any(|r| r
            .sources
            .iter()
            .any(|p| p.actor == "call" && p.port == "1")
            && r.destinations.iter().any(|p| p.actor == "y")));
    }

    #[test]
    fn region_expand_arg_list_assigns_positional_and_kwarg_ports() {
        // ``f(X, y, random_state=42)`` — argument_list with X
        // (positional), y (positional), Parameter random_state (kwarg).
        use crate::model::{Actor, PortRef, Region, Relation};
        let mut p = Actor::parameter("rs", "random_state", "42", "int");
        p.kind = ActorKind::Parameter;
        let region = Region::new()
            .with_actor(Actor::operator("call", "f"))
            .with_actor(Actor::parser_leaf("al", "argument_list", "(X, y, random_state=42)"))
            .with_actor(Actor::parser_leaf("X", "identifier", "X"))
            .with_actor(Actor::parser_leaf("y", "identifier", "y"))
            .with_actor(p)
            .with_relation(Relation::point_to_point(
                "rel:call->al",
                PortRef { actor: "call".into(), port: String::new() },
                PortRef { actor: "al".into(), port: "0".into() },
            ))
            .with_relation(Relation::point_to_point(
                "rel:al->X",
                PortRef { actor: "al".into(), port: String::new() },
                PortRef { actor: "X".into(), port: "0".into() },
            ))
            .with_relation(Relation::point_to_point(
                "rel:al->y",
                PortRef { actor: "al".into(), port: String::new() },
                PortRef { actor: "y".into(), port: "1".into() },
            ))
            .with_relation(Relation::point_to_point(
                "rel:al->rs",
                PortRef { actor: "al".into(), port: String::new() },
                PortRef { actor: "rs".into(), port: "2".into() },
            ));
        let mut m: Mapping = rustc_hash::FxHashMap::default();
        m.insert("0".into(), "call".into());
        m.insert("1".into(), "al".into());
        let out = apply_expand_arg_list(region, &m, "0", "1");
        assert!(out.actor("al").is_none());
        // X → call.0 (positional)
        assert!(out.relations.iter().any(|r| r
            .sources
            .iter()
            .any(|p| p.actor == "X")
            && r.destinations.iter().any(|p| p.actor == "call" && p.port == "0")));
        // y → call.1
        assert!(out.relations.iter().any(|r| r
            .sources
            .iter()
            .any(|p| p.actor == "y")
            && r.destinations.iter().any(|p| p.actor == "call" && p.port == "1")));
        // rs (Parameter) → call.random_state (kwarg)
        assert!(out.relations.iter().any(|r| r
            .sources
            .iter()
            .any(|p| p.actor == "rs")
            && r.destinations.iter().any(|p| p.actor == "call" && p.port == "random_state")));
        // call's input port "random_state" is Kwarg-kind.
        let call = out.actor("call").unwrap();
        let port = call.inputs.iter().find(|p| p.name == "random_state").unwrap();
        assert!(matches!(port.kind, PortKind::Kwarg));
    }

    #[test]
    fn region_subscript_to_snippet_creates_snippet_with_root_input() {
        // ``df["col"]`` — subscript with text="df["col"]" and a child
        // identifier "df". After conversion: subscript becomes a
        // Snippet, child identifier "df" stays as the input port at
        // port=df.
        use crate::model::{Actor, PortRef, Region, Relation};
        let region = Region::new()
            .with_actor(Actor::parser_leaf("sub", "subscript", "df[\"col\"]"))
            .with_actor(Actor::parser_leaf("df", "identifier", "df"))
            .with_relation(Relation::point_to_point(
                "rel:sub->df",
                PortRef { actor: "sub".into(), port: String::new() },
                PortRef { actor: "df".into(), port: "0".into() },
            ));
        let mut m: Mapping = rustc_hash::FxHashMap::default();
        m.insert("0".into(), "sub".into());
        let out = apply_subscript_to_snippet(region, &m, "0");
        let actor = out.actor("sub").unwrap();
        assert!(matches!(actor.kind, ActorKind::Snippet));
        assert!(actor.code.contains("def foo(df)"));
        // df identifier survives, wired into sub at port=df.
        assert!(out.actor("df").is_some());
        assert!(out.relations.iter().any(|r| r
            .sources
            .iter()
            .any(|p| p.actor == "df")
            && r.destinations.iter().any(|p| p.actor == "sub" && p.port == "df")));
    }

    #[test]
    fn region_rewire_var_uses_to_method_producer_picks_result_port() {
        // ``y_pred = clf.predict(X)``: predict's "result" port carries
        // the method's return value. Var-resolution at a use of
        // y_pred should source from predict.result, not predict.0.
        use crate::model::{Actor, PortRef, Region, Relation};
        let mut predict = Actor::operator("predict", "predict");
        predict.upsert_output("instance", PortKind::Data);
        predict.upsert_output("result", PortKind::Data);
        let region = Region::new()
            .with_actor(predict)
            .with_actor(Actor::parser_leaf("y_pred", "identifier", "y_pred"))
            .with_actor(Actor::parser_leaf("score", "identifier", "score"))
            .with_relation(Relation::point_to_point(
                "rel:predict->y_pred",
                PortRef { actor: "predict".into(), port: "result".into() },
                PortRef { actor: "y_pred".into(), port: "value".into() },
            ))
            .with_relation(Relation::point_to_point(
                "rel:y_pred->score",
                PortRef { actor: "y_pred".into(), port: "value".into() },
                PortRef { actor: "score".into(), port: "0".into() },
            ));
        let mut m: Mapping = rustc_hash::FxHashMap::default();
        m.insert("u".into(), "y_pred".into());
        m.insert("p".into(), "predict".into());
        let out = apply_rewire_var_uses(region, &m, "u", "p");
        // y_pred dropped.
        assert!(out.actor("y_pred").is_none());
        // predict.result → score
        assert!(out.relations.iter().any(|r| r
            .sources
            .iter()
            .any(|p| p.actor == "predict" && p.port == "result")
            && r.destinations.iter().any(|p| p.actor == "score")));
    }

    #[test]
    fn region_chain_method_renames_op_and_adds_self() {
        // Operator "clf.fit" with relations from X (pos=0) and y (pos=1).
        // After chain: name="fit", X bumped to "X" (KB rename), y to "y",
        // and a self-relation runs producer.value → fit.self.
        use crate::model::{Actor, PortRef, Region, Relation};
        let region = Region::new()
            .with_actor(Actor::operator("op", "clf.fit"))
            .with_actor(Actor::operator("rf", "RandomForestClassifier"))
            .with_actor(Actor::parser_leaf("X", "identifier", "X"))
            .with_actor(Actor::parser_leaf("y", "identifier", "y"))
            .with_relation(Relation::point_to_point(
                "rel:X->op",
                PortRef { actor: "X".into(), port: "value".into() },
                PortRef { actor: "op".into(), port: "0".into() },
            ))
            .with_relation(Relation::point_to_point(
                "rel:y->op",
                PortRef { actor: "y".into(), port: "value".into() },
                PortRef { actor: "op".into(), port: "1".into() },
            ));
        let mut m: Mapping = rustc_hash::FxHashMap::default();
        m.insert("op".into(), "op".into());
        m.insert("p".into(), "rf".into());
        let out = apply_chain_method(region, &m, "op", "p", "fit");
        let op_actor = out.actor("op").unwrap();
        assert_eq!(op_actor.name, "fit");
        assert!(op_actor.outputs.iter().any(|p| p.name == "instance"));
        assert!(op_actor.outputs.iter().any(|p| p.name == "result"));
        assert!(op_actor.inputs.iter().any(|p| p.name == "self"));
        // X ended up at port "X" (KB rename of bumped "1").
        assert!(out.relations.iter().any(|r| r
            .sources
            .iter()
            .any(|p| p.actor == "X")
            && r.destinations.iter().any(|p| p.actor == "op" && p.port == "X")));
        // y at "y".
        assert!(out.relations.iter().any(|r| r
            .sources
            .iter()
            .any(|p| p.actor == "y")
            && r.destinations.iter().any(|p| p.actor == "op" && p.port == "y")));
        // Chain edge: rf → op.self.
        assert!(out.relations.iter().any(|r| r
            .sources
            .iter()
            .any(|p| p.actor == "rf")
            && r.destinations.iter().any(|p| p.actor == "op" && p.port == "self")));
    }

    #[test]
    fn region_resolve_var_references_preserves_unpack_output_port() {
        // `X, y = train_test_split(...)` already wired tts.0 → X
        // and tts.1 → y. A use of `y` somewhere downstream rewires
        // to tts.1, NOT tts.0.
        use crate::model::{Actor, PortRef, Region, Relation};
        let region = Region::new()
            .with_actor(Actor::operator("tts", "train_test_split"))
            .with_actor(Actor::parser_leaf("X", "identifier", "X"))
            .with_actor(Actor::parser_leaf("y", "identifier", "y"))
            .with_actor(Actor::parser_leaf("y_use", "identifier", "y"))
            .with_actor(Actor::operator("score", "accuracy_score"))
            .with_relation(Relation::point_to_point(
                "rel:tts->X",
                PortRef { actor: "tts".into(), port: "0".into() },
                PortRef { actor: "X".into(), port: "value".into() },
            ))
            .with_relation(Relation::point_to_point(
                "rel:tts->y",
                PortRef { actor: "tts".into(), port: "1".into() },
                PortRef { actor: "y".into(), port: "value".into() },
            ))
            .with_relation(Relation::point_to_point(
                "rel:y_use->score",
                PortRef { actor: "y_use".into(), port: "value".into() },
                PortRef { actor: "score".into(), port: "0".into() },
            ));
        let out = apply_resolve_var_references(region);
        // y_use is gone.
        assert!(out.actor("y_use").is_none());
        // The relation that used to come from y_use now comes from
        // tts.1 (the second tuple slice).
        assert!(out.relations.iter().any(|r| r
            .sources
            .iter()
            .any(|p| p.actor == "tts" && p.port == "1")
            && r.destinations.iter().any(|p| p.actor == "score")));
    }

    #[test]
    fn region_chain_all_method_shortcuts_collapses_var_method_ops() {
        // ``clf = RandomForestClassifier(); clf.fit(X, y)`` after
        // assignment-collapse leaves: rf → identifier(clf) and
        // Operator(name="clf.fit") with X / y arg relations. The
        // global pass renames clf.fit → fit and adds rf → fit.self.
        use crate::model::{Actor, PortRef, Region, Relation};
        let region = Region::new()
            .with_actor(Actor::operator("rf", "RandomForestClassifier"))
            .with_actor(Actor::parser_leaf("clf", "identifier", "clf"))
            .with_actor(Actor::operator("op", "clf.fit"))
            .with_actor(Actor::parser_leaf("X", "identifier", "X"))
            .with_relation(Relation::point_to_point(
                "rel:rf->clf",
                PortRef { actor: "rf".into(), port: "value".into() },
                PortRef { actor: "clf".into(), port: "value".into() },
            ))
            .with_relation(Relation::point_to_point(
                "rel:X->op",
                PortRef { actor: "X".into(), port: "value".into() },
                PortRef { actor: "op".into(), port: "0".into() },
            ));
        let out = apply_chain_all_method_shortcuts(region);
        let op = out.actor("op").unwrap();
        assert_eq!(op.name, "fit");
        // X bumped: "0" → "1" → KB-renamed "X".
        assert!(out.relations.iter().any(|r| r
            .sources
            .iter()
            .any(|p| p.actor == "X")
            && r.destinations.iter().any(|p| p.actor == "op" && p.port == "X")));
        // Chain: rf → op.self.
        assert!(out.relations.iter().any(|r| r
            .sources
            .iter()
            .any(|p| p.actor == "rf")
            && r.destinations.iter().any(|p| p.actor == "op" && p.port == "self")));
    }

    #[test]
    fn region_resolve_imports_rewrites_aliased_module() {
        // ``import pandas as pd`` and an Operator(name="pd.read_csv").
        use crate::model::{Actor, PortRef, Region, Relation};
        let region = Region::new()
            .with_actor(Actor::parser_leaf("is", "import_statement", ""))
            .with_actor(Actor::parser_leaf("ai", "aliased_import", ""))
            .with_actor(Actor::parser_leaf("module", "dotted_name", "pandas"))
            .with_actor(Actor::parser_leaf("alias", "identifier", "pd"))
            .with_actor(Actor::operator("call", "pd.read_csv"))
            .with_relation(Relation::point_to_point(
                "rel:is->ai",
                PortRef { actor: "is".into(), port: String::new() },
                PortRef { actor: "ai".into(), port: "0".into() },
            ))
            .with_relation(Relation::point_to_point(
                "rel:ai->module",
                PortRef { actor: "ai".into(), port: String::new() },
                PortRef { actor: "module".into(), port: "0".into() },
            ))
            .with_relation(Relation::point_to_point(
                "rel:ai->alias",
                PortRef { actor: "ai".into(), port: String::new() },
                PortRef { actor: "alias".into(), port: "1".into() },
            ));
        let out = apply_resolve_imports(region);
        assert_eq!(out.actor("call").unwrap().name, "pandas.read_csv");
        assert!(out.actor("is").is_none());
    }
}
