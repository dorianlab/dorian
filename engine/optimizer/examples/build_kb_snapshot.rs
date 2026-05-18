//! Rebuild the KB snapshot JSON from curated .kb files + the
//! io_crawler extras. Standalone equivalent of
//! ``scripts/export_kb_snapshot.py`` for environments where the
//! python ``dorian_native`` extension isn't available.
//!
//! Usage:
//!   cargo run --example build_kb_snapshot --release -- \
//!     <repo_root> [out_path]

use std::fs;
use std::path::PathBuf;

use optimizer::kb::build_snapshot;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args().skip(1);
    let repo_root = PathBuf::from(
        args.next().expect("usage: build_kb_snapshot <repo_root> [out_path]"),
    );
    let out_path = args.next().map(PathBuf::from).unwrap_or_else(|| {
        repo_root.join("volumes").join("kb_snapshot").join("kb_snapshot.json")
    });

    let mut source_paths: Vec<PathBuf> = Vec::new();
    let sources_dir = repo_root.join("dorian/knowledge/sources");
    let mut kb_files: Vec<PathBuf> = fs::read_dir(&sources_dir)?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().map(|e| e == "kb").unwrap_or(false))
        .collect();
    kb_files.sort();
    source_paths.extend(kb_files);

    let extras = repo_root.join("volumes/io_crawler_extras.kb");
    if extras.is_file() && fs::metadata(&extras)?.len() > 0 {
        source_paths.push(extras);
    }

    let texts: Vec<(String, String)> = source_paths
        .iter()
        .map(|p| (p.display().to_string(), fs::read_to_string(p).unwrap()))
        .collect();
    let sources_ref: Vec<(&str, &str)> = texts
        .iter()
        .map(|(label, text)| (label.as_str(), text.as_str()))
        .collect();

    let (snap, errors) = build_snapshot(&sources_ref);
    if !errors.is_empty() {
        eprintln!("-- {} parse error(s):", errors.len());
        for e in &errors {
            eprintln!(
                "   {}:{}  {}  -> {}",
                e.source, e.line_no, e.line, e.message
            );
        }
    }

    if let Some(parent) = out_path.parent() {
        fs::create_dir_all(parent)?;
    }
    let json = serde_json::to_string_pretty(&snap)?;
    fs::write(&out_path, json)?;
    println!(
        "wrote KB snapshot: {} ({} operators, {} interfaces, {} mitigations, {} pathways)",
        out_path.display(),
        snap.operators.len(),
        snap.interfaces.len(),
        snap.mitigations.len(),
        snap.pathways.len(),
    );
    Ok(())
}
