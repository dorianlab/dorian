//! tree-sitter-python source → raw AST [`Model`].
//!
//! Each tree-sitter node becomes an [`Actor`] of kind
//! [`ActorKind::ParserLeaf`] with its `(type, text)` captured in
//! [`ParserPayload`]; parent→child edges become point-to-point
//! [`Relation`]s carrying the child's sibling index in the
//! destination port name. The rule engine's curated chain promotes
//! parser leaves to semantic actors as it fires.

use anyhow::Result;
use tree_sitter::{Node as TsNode, Parser};

use crate::model::{Actor, Model, PortKind, PortRef, Region, Relation, SourceSpan};

/// Parse a Python source string into a raw AST [`Model`]. The
/// model's root [`Region`] holds one [`Actor`] of kind
/// [`ActorKind::ParserLeaf`] per tree-sitter node and one
/// [`Relation`] per parent→child edge. Each relation's destination
/// port carries the child's sibling index (`"0"`, `"1"`, …) so the
/// rule engine's positional-matching primitives can run unchanged
/// against the new shape; rules upgrade those indices to semantic
/// names from the KB port table as they fire.
pub fn parse_python(code: &str) -> Result<Model> {
    let tree = parse_tree(code)?;
    let mut region = Region::new();
    let mut next_id: u64 = 0;
    walk(&tree.root_node(), code.as_bytes(), &mut region, &mut next_id, None);
    Ok(Model { root: region })
}

fn walk(
    ts: &TsNode,
    src: &[u8],
    region: &mut Region,
    next_id: &mut u64,
    parent_id: Option<&str>,
) {
    let id = next_id.to_string();
    *next_id += 1;

    let node_type = ts.kind().to_string();
    let text = ts.utf8_text(src).unwrap_or("").to_string();

    let mut actor = Actor::parser_leaf(id.clone(), node_type, text);
    actor.source = Some(SourceSpan {
        start_byte: ts.start_byte() as u32,
        end_byte: ts.end_byte() as u32,
        start_line: ts.start_position().row as u32,
        start_col: ts.start_position().column as u32,
    });

    let position = parent_id.map(|_| sibling_index(ts));
    if let Some(p) = position.as_ref() {
        // Pre-create the destination port so downstream pattern
        // matchers see its `kind` without inspecting the relation.
        // Parser leaves are uniformly Positional; semantic promotion
        // (Kwarg, SelfRef, …) happens via rule primitives.
        actor.upsert_input(p, PortKind::Positional);
    }

    region.actors.push(actor);

    if let (Some(pid), Some(p)) = (parent_id, position) {
        region.relations.push(Relation {
            id: format!("rel:{}->{}", pid, id),
            sources: vec![PortRef {
                actor: pid.to_string(),
                port: String::new(),
            }],
            destinations: vec![PortRef {
                actor: id.clone(),
                port: p,
            }],
        });
    }

    let mut cursor = ts.walk();
    for child in ts.children(&mut cursor) {
        walk(&child, src, region, next_id, Some(&id));
    }
}

fn parse_tree(code: &str) -> Result<tree_sitter::Tree> {
    let mut parser = Parser::new();
    let lang = tree_sitter_python::LANGUAGE.into();
    parser
        .set_language(&lang)
        .map_err(|e| anyhow::anyhow!("set_language(python): {e}"))?;
    parser
        .parse(code, None)
        .ok_or_else(|| anyhow::anyhow!("tree-sitter returned no tree"))
}

fn sibling_index(ts: &TsNode) -> String {
    let parent = match ts.parent() {
        Some(p) => p,
        None => return "0".to_string(),
    };
    let mut cursor = parent.walk();
    for (i, sib) in parent.children(&mut cursor).enumerate() {
        if sib.id() == ts.id() {
            return i.to_string();
        }
    }
    "0".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::ActorKind;

    #[test]
    fn parses_simple_assignment() {
        let model = parse_python("X = 1\n").expect("parse");
        let region = &model.root;
        assert!(region.actors.iter().all(|a| a.kind == ActorKind::ParserLeaf));
        assert!(region
            .actors
            .iter()
            .any(|a| a.parser.r#type == "assignment"));
        assert!(region
            .actors
            .iter()
            .any(|a| a.parser.r#type == "identifier" && a.parser.text == "X"));
    }

    #[test]
    fn parses_imports() {
        let model = parse_python("from sklearn.svm import SVC\n").expect("parse");
        assert!(model
            .root
            .actors
            .iter()
            .any(|a| a.parser.r#type == "import_from_statement"));
    }

    #[test]
    fn relations_carry_sibling_index_in_destination_port() {
        let model = parse_python("X\n").expect("parse");
        let has_zero_port = model
            .root
            .relations
            .iter()
            .any(|r| r.destinations.iter().any(|p| p.port == "0"));
        assert!(has_zero_port, "expected at least one destination port \"0\"");
    }

    #[test]
    fn actors_carry_source_spans() {
        let model = parse_python("X = 1\n").expect("parse");
        assert!(
            model.root.actors.iter().any(|a| a.source.is_some()),
            "expected at least one actor to carry a SourceSpan"
        );
    }
}
