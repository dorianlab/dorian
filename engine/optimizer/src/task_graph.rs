//! Task-graph representation — Rust equivalent of Dask's dict task graph,
//! under the Ptolemy II computation-model conventions already established
//! in the ``graph`` crate.
//!
//! Why
//! ~~~
//! Dorian's Python side currently runs pipelines via ``dask.threaded.get``
//! on a dict-shaped task graph produced by ``dorian/pipeline/execution.py``
//! and its helpers. Each key is a task name; each value is either:
//!
//!   * an inline constant (Python value), or
//!   * a tuple ``(callable, *args)`` where string args reference other
//!     task names and all other args are literal values.
//!
//! That format is the executable-form contract between the Python
//! scheduler and the operator-resolver. Retiring Dask requires the
//! Rust engine to own the same contract: build the task graph, resolve
//! dependencies, fire actors (via a ``Firer`` that can still call back
//! into Python while we migrate operator-by-operator), and return
//! results in the same shape.
//!
//! This module is the **representation**. Lowering from
//! ``graph::ProcessGraph`` lives in ``lowering.rs``; execution lives in
//! ``crate::sdf`` (existing synchronous-dataflow scheduler). The
//! explicit task-graph boundary between them makes each layer
//! independently testable.
//!
//! The structure mirrors Ptolemy II's SDF actor model:
//!
//!   * ``TaskKey`` — stable actor identity. Derived from node UUIDs so
//!     repeated lowerings of the same ``ProcessGraph`` produce the same
//!     keys (content-addressable caching in ``cache::`` stays correct).
//!   * ``Task`` — either a ``Value`` (Parameter / constant) or a
//!     ``Call`` (Operator / Snippet invocation with ordered args).
//!   * ``ArgRef`` — positional / keyword / literal / link-to-other-task.
//!     Keyword args become ``**kwargs`` at resolve time; positional
//!     slots by index. Mirrors ``dorian.dag.Edge.position``.
//!   * ``TaskGraph`` — the top-level map. Carries optional output keys
//!     (``roots``) so the executor knows which tasks' results to
//!     return to the caller.

use std::collections::BTreeMap;

use graph::model::Position;
use serde::{Deserialize, Serialize};

/// Stable identity for a task. By convention, this is the same string
/// as the source ``ProcessGraph`` node UUID so downstream cache / event
/// subsystems can correlate the two without a separate lookup.
pub type TaskKey = String;

/// Reference to an operator / callable. The format is a dotted path
/// (``sklearn.preprocessing.StandardScaler``) or a qualified
/// method-shortcut (``fit``, ``predict``, ``transform``); the
/// resolver at execution time handles the distinction. Snippets have
/// their source code inlined via a separate variant below so the
/// executor doesn't need a registry round-trip for inline bodies.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct OperatorRef {
    /// Fully-qualified operator name, or method-shortcut keyword.
    pub name: String,
    /// Source language for the resolver — almost always ``"python"``
    /// while we're still calling back into the Python interpreter for
    /// operator execution.
    pub language: String,
}

/// One leg of a ``Call``'s argument list.
///
/// The concrete resolution is:
///
///   * ``Literal`` — the inline JSON value is passed as-is. Python
///     ``Parameter`` nodes with ``dtype`` of ``int`` / ``float`` /
///     ``string`` materialise here after ``Parameter.__call__``
///     evaluates.
///   * ``TaskRef`` — depend on another task's result. The ``output``
///     index picks a slot out of a multi-output producer (e.g.
///     ``train_test_split`` returning 4 arrays). ``Position``
///     distinguishes positional vs keyword wiring — same semantics as
///     ``dorian.dag.Edge.position``.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ArgRef {
    Literal {
        value: serde_json::Value,
        position: Position,
    },
    TaskRef {
        task: TaskKey,
        output: i64,
        position: Position,
    },
}

impl ArgRef {
    /// Position the argument fills on the destination.
    pub fn position(&self) -> &Position {
        match self {
            ArgRef::Literal { position, .. } => position,
            ArgRef::TaskRef { position, .. } => position,
        }
    }
}

/// Task payload — one of three kinds matching ``ProcessGraph``'s node
/// payload variants.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Task {
    /// Inline constant — materialised at lowering time. Downstream
    /// ``Call`` tasks reference this via ``TaskRef``.
    Value {
        value: serde_json::Value,
    },
    /// Operator invocation. Args are ordered by their
    /// ``Position::Index`` when positional; keyword args live
    /// alongside in the same vector and are unpacked into ``**kwargs``
    /// by the executor.
    Call {
        op: OperatorRef,
        args: Vec<ArgRef>,
    },
    /// Inline Python source that defines a single ``foo(...)``
    /// callable. The executor ``exec``s the body once, then invokes
    /// ``foo`` with ``args`` in order. Kept distinct from ``Call``
    /// because the resolution path is different — no KB lookup, no
    /// class instantiation, just ``exec`` + ``foo()``.
    Snippet {
        source: String,
        args: Vec<ArgRef>,
    },
}

/// Top-level task graph.
///
/// ``BTreeMap`` (not ``HashMap``) keeps the serialised form
/// deterministic — matters for cache keys derived from a hashed
/// subgraph, for test golden files, and for humans diffing two plans.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct TaskGraph {
    pub tasks: BTreeMap<TaskKey, Task>,
    /// Keys whose results the caller wants back. The executor runs
    /// every transitive dependency of these; anything else is
    /// dead-code and can be pruned by ``crate::lowering::prune``.
    #[serde(default)]
    pub roots: Vec<TaskKey>,
}

impl TaskGraph {
    pub fn new() -> Self {
        Self::default()
    }

    /// Insert or overwrite a task.
    pub fn insert(&mut self, key: TaskKey, task: Task) {
        self.tasks.insert(key, task);
    }

    /// Mark *key* as a graph output. Callers typically roots are the
    /// final metric / printout nodes of the pipeline.
    pub fn add_root(&mut self, key: TaskKey) {
        if !self.roots.iter().any(|k| k == &key) {
            self.roots.push(key);
        }
    }

    pub fn len(&self) -> usize {
        self.tasks.len()
    }

    pub fn is_empty(&self) -> bool {
        self.tasks.is_empty()
    }

    /// Return every ``TaskKey`` that *task* depends on (direct parents only).
    pub fn direct_deps(&self, key: &str) -> Vec<TaskKey> {
        let Some(task) = self.tasks.get(key) else {
            return Vec::new();
        };
        let args = match task {
            Task::Value { .. } => return Vec::new(),
            Task::Call { args, .. } => args,
            Task::Snippet { args, .. } => args,
        };
        args.iter()
            .filter_map(|a| match a {
                ArgRef::TaskRef { task, .. } => Some(task.clone()),
                _ => None,
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn value_task_roundtrip() {
        let mut g = TaskGraph::new();
        g.insert(
            "fpath".into(),
            Task::Value { value: serde_json::json!("/tmp/x.csv") },
        );
        g.add_root("fpath".into());
        let s = serde_json::to_string(&g).unwrap();
        let g2: TaskGraph = serde_json::from_str(&s).unwrap();
        assert_eq!(g2.tasks.len(), 1);
        assert_eq!(g2.roots, vec!["fpath".to_string()]);
    }

    #[test]
    fn call_with_taskref_and_literal_args() {
        let mut g = TaskGraph::new();
        g.insert(
            "fpath".into(),
            Task::Value { value: serde_json::json!("/tmp/x.csv") },
        );
        g.insert(
            "df".into(),
            Task::Call {
                op: OperatorRef {
                    name: "pandas.read_csv".into(),
                    language: "python".into(),
                },
                args: vec![ArgRef::TaskRef {
                    task: "fpath".into(),
                    output: 0,
                    position: Position::Index(0),
                }],
            },
        );
        g.add_root("df".into());

        assert_eq!(g.direct_deps("df"), vec!["fpath".to_string()]);
        assert!(g.direct_deps("fpath").is_empty());
    }
}
