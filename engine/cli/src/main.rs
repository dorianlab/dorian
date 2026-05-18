//! `dem-cli` — small command-line tool for the Dorian Execution Model.
//!
//! Subcommands:
//!
//!   * `dem-map <pipelines.json>` — same as `examples/dem_map_check`,
//!     reports SDF/DE counts + determinism distribution + top
//!     operators across the corpus.
//!   * `batch-plan <pipelines.json>` — runs the batch planner over
//!     every pipeline in the dump, reports collapse ratio and the
//!     top-N most-shared cache keys (RL fan-out targets).
//!   * `parse-check <pipeline.json>` — single-pipeline parser
//!     diagnostic; prints the parsed node + edge counts and any
//!     parser errors.
//!
//! Built once, re-used from CI and from the engine's bench harness.

use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::process;

use cache::{plan_batch, ExperimentGraphIndex};
use graph::{parse_pipeline_json, summarise_domain_map, DomainKind};

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 3 {
        eprintln!("usage: dem-cli <subcommand> <path>");
        eprintln!("subcommands: dem-map, batch-plan, parse-check");
        process::exit(2);
    }
    let cmd = &args[1];
    let path = &args[2];

    let raw = fs::read_to_string(path).unwrap_or_else(|e| {
        eprintln!("read {path}: {e}");
        process::exit(1);
    });
    let value: serde_json::Value = serde_json::from_str(&raw).unwrap_or_else(|e| {
        eprintln!("parse {path}: {e}");
        process::exit(1);
    });

    match cmd.as_str() {
        "dem-map" => cmd_dem_map(&value),
        "batch-plan" => cmd_batch_plan(&value),
        "parse-check" => cmd_parse_check(&value),
        other => {
            eprintln!("unknown subcommand: {other}");
            process::exit(2);
        }
    }
}

fn pipelines_iter(value: &serde_json::Value) -> Vec<&serde_json::Value> {
    match value {
        serde_json::Value::Array(arr) => arr.iter().collect(),
        _ => vec![value],
    }
}

fn cmd_dem_map(value: &serde_json::Value) {
    let pipelines = pipelines_iter(value);
    let mut total_nodes = 0;
    let mut sdf = 0;
    let mut de = 0;
    let mut det = 0;
    let mut nondet = 0;
    let mut unk = 0;
    let mut by_fqn: BTreeMap<String, (usize, usize)> = BTreeMap::new();

    for pipe in &pipelines {
        match parse_pipeline_json(pipe) {
            Ok((g, dem)) => {
                let s = summarise_domain_map(&dem);
                total_nodes += g.node_count();
                sdf += s.sdf_count;
                de += s.de_count;
                det += s.deterministic_count;
                nondet += s.non_deterministic_count;
                unk += s.unknown_count;
                for (id, node) in &g.nodes {
                    if let graph::Node::Operator(op) = node {
                        let entry = by_fqn.entry(op.name.clone()).or_insert((0, 0));
                        match dem.actor(id).map(|a| a.domain).unwrap_or(DomainKind::Sdf) {
                            DomainKind::Sdf => entry.0 += 1,
                            DomainKind::De => entry.1 += 1,
                        }
                    }
                }
            }
            Err(e) => eprintln!("parse failure: {e}"),
        }
    }

    println!("pipelines: {}", pipelines.len());
    println!("nodes:     {total_nodes}");
    println!("  SDF: {sdf}");
    println!("  DE:  {de}");
    println!("determinism:");
    println!("  deterministic:     {det}");
    println!("  non-deterministic: {nondet}");
    println!("  unknown:           {unk}");
    println!();
    println!("top operators (sdf / de):");
    let mut rows: Vec<_> = by_fqn.iter().collect();
    rows.sort_by(|a, b| (b.1 .0 + b.1 .1).cmp(&(a.1 .0 + a.1 .1)));
    for (fqn, (s, d)) in rows.iter().take(20) {
        println!("  {:>6} / {:>6}  {}", s, d, fqn);
    }
}

fn cmd_batch_plan(value: &serde_json::Value) {
    let pipelines = pipelines_iter(value);
    let mut graphs = Vec::new();
    let mut anns = Vec::new();
    for pipe in &pipelines {
        if let Ok((g, ann)) = parse_pipeline_json(pipe) {
            graphs.push(g);
            anns.push(ann);
        }
    }
    let g_refs: Vec<&graph::ProcessGraph> = graphs.iter().collect();
    let a_refs: Vec<&graph::DemAnnotations> = anns.iter().collect();
    let idx = ExperimentGraphIndex::new();
    let plan = plan_batch(&idx, &g_refs, &a_refs);
    println!("pipelines:        {}", graphs.len());
    println!("naive firings:    {}", plan.naive_fire_count());
    println!("unique firings:   {}", plan.unique_fire_count());
    println!("collapsed:        {}", plan.collapsed_firings);
    println!(
        "collapse ratio:   {:.3}  (1.0 = full reuse, 0.0 = no overlap)",
        plan.collapse_ratio()
    );
    println!(
        "implied speedup:  {:.2}x  (naive/unique)",
        if plan.unique_fire_count() == 0 {
            0.0
        } else {
            plan.naive_fire_count() as f64 / plan.unique_fire_count() as f64
        }
    );
}

fn cmd_parse_check(value: &serde_json::Value) {
    match parse_pipeline_json(value) {
        Ok((g, dem)) => {
            println!("ok: {} nodes, {} edges", g.node_count(), g.edge_count());
            let s = summarise_domain_map(&dem);
            println!("  SDF: {}, DE: {}", s.sdf_count, s.de_count);
            println!(
                "  determinism: det={} nondet={} unk={}",
                s.deterministic_count, s.non_deterministic_count, s.unknown_count
            );
        }
        Err(e) => {
            eprintln!("parse error: {e}");
            process::exit(1);
        }
    }
}
