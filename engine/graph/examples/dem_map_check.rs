//! `cargo run -p graph --example dem_map_check -- <pipelines.json>`
//!
//! Reads a docstore `pipelines.json` dump (array of pipeline documents)
//! and reports how today's pipelines map onto the SDF + DE DEM
//! classification.
//!
//! The plan-doc's acceptance criterion: the map is clean when every
//! operator has a domain assigned and the DE set contains only the
//! known async primitives. Any surprises printed here are input to
//! deciding whether to extend the DE allowlist or revisit the domain
//! choice before shadow-mode migration.

use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::process;

use graph::{parse_pipeline_json, summarise_domain_map, DomainKind};

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!(
            "usage: dem_map_check <pipelines.json>\n\
             pipelines.json is the docstore dump — either a single\n\
             pipeline object or a JSON array of pipeline documents."
        );
        process::exit(2);
    }
    let path = &args[1];
    let raw = fs::read_to_string(path).unwrap_or_else(|e| {
        eprintln!("failed to read {path}: {e}");
        process::exit(1);
    });
    let value: serde_json::Value = serde_json::from_str(&raw).unwrap_or_else(|e| {
        eprintln!("failed to parse JSON: {e}");
        process::exit(1);
    });

    let pipelines: Vec<serde_json::Value> = match &value {
        serde_json::Value::Array(arr) => arr.clone(),
        _ => vec![value],
    };

    let mut total_pipelines = 0usize;
    let mut parse_failures = 0usize;
    let mut total_nodes = 0usize;
    let mut sdf_nodes = 0usize;
    let mut de_nodes = 0usize;
    let mut deterministic = 0usize;
    let mut non_deterministic = 0usize;
    let mut unknown = 0usize;

    // Track which operator FQNs show up under which domain + determinism
    // class, so surprises jump out in the report.
    let mut domain_by_fqn: BTreeMap<String, (usize, usize)> = BTreeMap::new();
    let mut non_det_examples: BTreeMap<String, usize> = BTreeMap::new();

    for pipe in &pipelines {
        total_pipelines += 1;
        match parse_pipeline_json(pipe) {
            Ok((graph, dem)) => {
                let summary = summarise_domain_map(&dem);
                total_nodes += graph.node_count();
                sdf_nodes += summary.sdf_count;
                de_nodes += summary.de_count;
                deterministic += summary.deterministic_count;
                non_deterministic += summary.non_deterministic_count;
                unknown += summary.unknown_count;

                for (id, node) in &graph.nodes {
                    let fqn = match node {
                        graph::Node::Operator(op) => op.name.clone(),
                        graph::Node::Snippet(s) => format!("snippet::{}", s.name),
                        _ => continue,
                    };
                    let entry = domain_by_fqn.entry(fqn.clone()).or_insert((0, 0));
                    match dem.actor(id).map(|a| a.domain).unwrap_or(DomainKind::Sdf) {
                        DomainKind::Sdf => entry.0 += 1,
                        DomainKind::De => entry.1 += 1,
                    }
                    if summary.non_deterministic_node_ids.contains(id) {
                        *non_det_examples.entry(fqn).or_insert(0) += 1;
                    }
                }
            }
            Err(e) => {
                parse_failures += 1;
                eprintln!("parse failure: {e}");
            }
        }
    }

    println!(
        "pipelines: {} ({} parse failures)",
        total_pipelines, parse_failures
    );
    println!("nodes:     {total_nodes}");
    println!("  SDF:     {sdf_nodes}");
    println!("  DE:      {de_nodes}");
    println!("determinism:");
    println!("  deterministic:     {deterministic}");
    println!("  non-deterministic: {non_deterministic}");
    println!("  unknown:           {unknown}");
    println!();
    println!("top operators by occurrence (sdf / de):");
    let mut rows: Vec<(&String, &(usize, usize))> = domain_by_fqn.iter().collect();
    rows.sort_by(|a, b| (b.1 .0 + b.1 .1).cmp(&(a.1 .0 + a.1 .1)));
    for (fqn, (sdf, de)) in rows.iter().take(40) {
        println!("  {:>6} / {:>6}  {}", sdf, de, fqn);
    }
    if !non_det_examples.is_empty() {
        println!();
        println!("non-deterministic operators (by occurrence):");
        let mut rows: Vec<(&String, &usize)> = non_det_examples.iter().collect();
        rows.sort_by(|a, b| b.1.cmp(a.1));
        for (fqn, n) in rows {
            println!("  {n:>6}  {fqn}");
        }
    }
}
