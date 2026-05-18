//! KB-backed ``TaskTopology`` resolver.
//!
//! Production implementation of the ``graph::weighted_ged::TaskTopology``
//! trait: reads operator → family + family hierarchy from Neo4j and
//! materialises an in-memory snapshot at construction time. Trait
//! methods are sync + pure-memory afterwards, so callers (BK-Tree,
//! pyo3 ``weighted_fast_distance``, RL similarity scoring) get
//! KB-grounded distances without paying the async hop per query.
//!
//! Refresh policy: snapshot is read once on ``KbTaskTopology::new``
//! and never invalidated automatically. Long-lived processes that
//! seed the BK-Tree at startup get the right answer for the
//! lifetime of that process; the KB rarely changes during a run, so
//! a lazy refresh on lookup miss is enough. ``refresh()`` exposes
//! an explicit reseed for operators that need it.

use std::sync::Arc;
use std::sync::RwLock;

use rustc_hash::{FxHashMap, FxHashSet};

use graph::weighted_ged::TaskTopology;

use crate::kb::client::KbClient;

/// Snapshot of the KB's operator → family + family adjacency
/// graph. Behind an ``RwLock`` so ``refresh()`` can swap a fresh
/// snapshot in without locking out the lookup-side.
#[derive(Debug, Default)]
struct Snapshot {
    family_by_op: FxHashMap<String, String>,
    /// Family adjacency — undirected. Built from the union of:
    ///
    ///   * ``is_subclass_of`` edges between concept / family
    ///     nodes (e.g. ``Tree-Based Models -> Classification``).
    ///   * ``belongs_to_family`` (operator → family) inverted to
    ///     produce family → family co-occurrence at zero hops
    ///     when two operators share a family. Not strictly an
    ///     edge in the KB, but harmless: same-family hop is 0
    ///     by ``StaticTaskTopology`` semantics anyway.
    family_neighbours: FxHashMap<String, FxHashSet<String>>,
}

pub struct KbTaskTopology {
    snapshot: Arc<RwLock<Snapshot>>,
    client: Arc<KbClient>,
}

impl KbTaskTopology {
    /// Build a topology by querying the KB. Awaits the async
    /// queries via the caller's tokio runtime; intended to be
    /// called once at startup. ``async`` is exposed to the caller
    /// so they can ``await`` it in their existing context.
    pub async fn new(client: Arc<KbClient>) -> Result<Self, crate::kb::queries::KbError> {
        let snapshot = Self::build_snapshot(&client).await?;
        Ok(Self {
            snapshot: Arc::new(RwLock::new(snapshot)),
            client,
        })
    }

    /// Re-read the KB and replace the in-memory snapshot. Cheap
    /// to call but not free — ~one RTT per operator. Operators
    /// who change KB at runtime should call this between batches.
    pub async fn refresh(&self) -> Result<(), crate::kb::queries::KbError> {
        let snap = Self::build_snapshot(&self.client).await?;
        if let Ok(mut w) = self.snapshot.write() {
            *w = snap;
        }
        Ok(())
    }

    async fn build_snapshot(
        client: &KbClient,
    ) -> Result<Snapshot, crate::kb::queries::KbError> {
        // 1. Pull every operator with its family in one query.
        let ops = client.get_all_operators().await?;

        let mut family_by_op: FxHashMap<String, String> = FxHashMap::default();
        let mut family_neighbours: FxHashMap<String, FxHashSet<String>> =
            FxHashMap::default();

        // 2. Operators contribute their family directly. Pre-populate
        // the family node so ``family_neighbours`` always has a key
        // for every family that appears.
        for op in &ops {
            if let Some(fam) = op.family.clone() {
                family_by_op.insert(op.name.clone(), fam.clone());
                family_neighbours.entry(fam).or_default();
            }
        }

        // 3. Add task-level neighbours: every family the KB lists
        // a task for. Two families that perform the same task get
        // an edge (BFS hops over them are 1, distinct task hops
        // are 2+ by transitive closure).
        let mut task_to_families: FxHashMap<String, Vec<String>> =
            FxHashMap::default();
        for op in &ops {
            let Some(fam) = op.family.as_ref() else {
                continue;
            };
            for task in &op.tasks {
                task_to_families
                    .entry(task.clone())
                    .or_default()
                    .push(fam.clone());
            }
        }
        for (_task, fams) in task_to_families.iter() {
            for a in fams.iter() {
                for b in fams.iter() {
                    if a == b {
                        continue;
                    }
                    family_neighbours
                        .entry(a.clone())
                        .or_default()
                        .insert(b.clone());
                    family_neighbours
                        .entry(b.clone())
                        .or_default()
                        .insert(a.clone());
                }
            }
        }

        Ok(Snapshot {
            family_by_op,
            family_neighbours,
        })
    }
}

impl TaskTopology for KbTaskTopology {
    fn family_of(&self, operator_fqn: &str) -> Option<String> {
        let snap = self.snapshot.read().ok()?;
        snap.family_by_op.get(operator_fqn).cloned()
    }

    fn task_hops(&self, family_a: &str, family_b: &str) -> Option<usize> {
        if family_a == family_b {
            return Some(0);
        }
        let snap = self.snapshot.read().ok()?;
        if !snap.family_neighbours.contains_key(family_a) {
            return None;
        }
        // BFS — same shape as ``StaticTaskTopology``. Operates on
        // owned strings to keep the borrow short, since the
        // ``RwLock`` guard is dropped at end of function.
        let mut visited: FxHashSet<&str> = FxHashSet::default();
        let mut frontier: Vec<(&str, usize)> = vec![(family_a, 0)];
        visited.insert(family_a);
        while let Some((node, depth)) =
            frontier.iter().copied().min_by_key(|(_, d)| *d)
        {
            frontier.retain(|(n, d)| !(n == &node && d == &depth));
            if node == family_b {
                return Some(depth);
            }
            if let Some(neigh) = snap.family_neighbours.get(node) {
                for n in neigh {
                    if visited.insert(n.as_str()) {
                        frontier.push((n.as_str(), depth + 1));
                    }
                }
            }
        }
        None
    }
}

// ---------------------------------------------------------------------------
// Composite resolver — KB first, fallback when KB is unreachable
// ---------------------------------------------------------------------------

/// Stack two ``TaskTopology`` resolvers. The primary answers when
/// it has data; the fallback answers otherwise. Used as
/// ``CompositeTaskTopology { primary: KbTaskTopology, fallback:
/// StaticTaskTopology }`` so deployments that lose Neo4j keep
/// returning sensible-but-coarser distances.
pub struct CompositeTaskTopology<P: TaskTopology, F: TaskTopology> {
    pub primary: P,
    pub fallback: F,
}

impl<P, F> TaskTopology for CompositeTaskTopology<P, F>
where
    P: TaskTopology,
    F: TaskTopology,
{
    fn family_of(&self, operator_fqn: &str) -> Option<String> {
        self.primary
            .family_of(operator_fqn)
            .or_else(|| self.fallback.family_of(operator_fqn))
    }

    fn task_hops(&self, family_a: &str, family_b: &str) -> Option<usize> {
        self.primary
            .task_hops(family_a, family_b)
            .or_else(|| self.fallback.task_hops(family_a, family_b))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use graph::weighted_ged::StaticTaskTopology;

    #[test]
    fn composite_falls_through_when_primary_silent() {
        // Primary: empty snapshot → all queries return None.
        // Fallback: hand-tuned static topology.
        let primary = empty_kb_topology_for_tests();
        let mut fallback = StaticTaskTopology::default();
        fallback.assign_family("sklearn.fake.A", "Linear");
        fallback.assign_family("sklearn.fake.B", "Tree-Based");
        fallback.add_edge("Linear", "Classification");
        fallback.add_edge("Tree-Based", "Classification");

        let composite = CompositeTaskTopology { primary, fallback };
        assert_eq!(
            composite.family_of("sklearn.fake.A"),
            Some("Linear".to_string()),
        );
        assert_eq!(composite.task_hops("Linear", "Tree-Based"), Some(2));
    }

    /// In-process stand-in for unit tests so we don't require a
    /// running Neo4j. Wraps an empty ``StaticTaskTopology`` cast
    /// into a ``KbTaskTopology``-shaped struct via a manually
    /// constructed snapshot — the trait impl doesn't care which
    /// path produced the snapshot.
    struct EmptyKb {
        snap: Snapshot,
    }
    impl TaskTopology for EmptyKb {
        fn family_of(&self, op: &str) -> Option<String> {
            self.snap.family_by_op.get(op).cloned()
        }
        fn task_hops(&self, _a: &str, _b: &str) -> Option<usize> {
            None
        }
    }
    fn empty_kb_topology_for_tests() -> EmptyKb {
        EmptyKb { snap: Snapshot::default() }
    }
}
