"""WL-Nyström DAG state encoder.

Produces fixed-dimensional embeddings for DAGs of varying sizes by combining
Weisfeiler-Lehman (WL) graph relabeling with Nyström kernel approximation.

The encoder operates directly on ``dorian.dag.DAG`` instances, using
``Operator.name`` and ``Parameter.name`` as initial node labels.

Algorithm
---------
1. **WL relabeling** (``h`` iterations): each node's label becomes a hash of
   its current label concatenated with the sorted labels of its neighbors.
   After ``h`` rounds, two nodes with identical labels have identical
   ``h``-hop neighborhoods.

2. **WL subtree kernel**: the kernel value ``K(G1, G2)`` counts matching
   subtree patterns across all ``h`` iterations (histogram intersection of
   label frequencies).

3. **Nyström approximation**: given a set of ``m`` landmark DAGs, compute
   the ``m×m`` kernel matrix and derive a projection that maps any new DAG
   to an ``m``-dimensional embedding in O(m·|V|·h) time.

The embedding vector is concatenated with the dataset metafeature vector to
form the MDP state.

Reference: thesis §4.2.2 "State Representation — WL-Nyström Encoding".
"""
from __future__ import annotations

import hashlib
from collections import Counter
from typing import Sequence

import numpy as np

from dorian.dag import DAG, Operator, Snippet, Parameter


# ---------------------------------------------------------------------------
# WL relabeling
# ---------------------------------------------------------------------------

def _initial_label(node) -> str:
    """Derive a string label from a DAG node."""
    if isinstance(node, Operator):
        return f"op:{node.name}"
    if isinstance(node, Snippet):
        return f"snippet:{node.name}"
    if isinstance(node, Parameter):
        return f"param:{node.name}"
    # Node (pattern-matching sentinel) — use type field
    return f"node:{getattr(node, 'type', 'unknown')}"


def _hash_label(label: str) -> str:
    """Compact deterministic hash for a WL label string."""
    return hashlib.md5(label.encode(), usedforsecurity=False).hexdigest()[:12]


def wl_relabel(dag: DAG, iterations: int = 3) -> list[Counter]:
    """Run WL relabeling and return label histograms per iteration.

    Returns a list of ``Counter`` objects (one per WL iteration, including
    the initial labeling at iteration 0).  Each counter maps label hashes
    to their frequency in the DAG.
    """
    if not dag.nodes:
        return [Counter() for _ in range(iterations + 1)]

    # Build adjacency (undirected for WL — both forward and backward edges)
    neighbors: dict[str, list[str]] = {nid: [] for nid in dag.nodes}
    for edge in dag.edges:
        if edge.source in neighbors and edge.destination in neighbors:
            neighbors[edge.source].append(edge.destination)
            neighbors[edge.destination].append(edge.source)

    # Iteration 0: initial labels
    labels: dict[str, str] = {
        nid: _hash_label(_initial_label(node))
        for nid, node in dag.nodes.items()
    }
    histograms: list[Counter] = [Counter(labels.values())]

    # Iterations 1..h
    for _ in range(iterations):
        new_labels: dict[str, str] = {}
        for nid in dag.nodes:
            neighbor_labels = sorted(labels[nb] for nb in neighbors[nid])
            combined = labels[nid] + ":" + ",".join(neighbor_labels)
            new_labels[nid] = _hash_label(combined)
        labels = new_labels
        histograms.append(Counter(labels.values()))

    return histograms


# ---------------------------------------------------------------------------
# WL subtree kernel
# ---------------------------------------------------------------------------

def wl_kernel(hist_a: list[Counter], hist_b: list[Counter]) -> float:
    """Compute the WL subtree kernel between two DAGs (given their histograms).

    The kernel value is the sum of histogram intersections across all WL
    iterations.
    """
    k = 0.0
    for ca, cb in zip(hist_a, hist_b):
        # Histogram intersection: sum of min counts for shared labels
        for label in ca:
            if label in cb:
                k += min(ca[label], cb[label])
    return k


# ---------------------------------------------------------------------------
# Nyström encoder
# ---------------------------------------------------------------------------

class WLNystromEncoder:
    """Fixed-dimensional DAG encoder using WL kernel + Nyström approximation.

    Parameters
    ----------
    wl_iterations : int
        Number of WL relabeling iterations.
    n_components : int
        Output dimensionality (number of Nyström landmark DAGs).
    regularization : float
        Small positive value added to the diagonal of the landmark kernel
        matrix for numerical stability.
    """

    def __init__(
        self,
        wl_iterations: int = 3,
        n_components: int = 64,
        regularization: float = 1e-6,
    ):
        self.wl_iterations = wl_iterations
        self.n_components = n_components
        self.regularization = regularization

        # Fitted state
        self._landmark_hists: list[list[Counter]] | None = None
        self._U: np.ndarray | None = None           # eigenvectors of K_mm
        self._S_inv_sqrt: np.ndarray | None = None   # inverse sqrt of eigenvalues

    @property
    def is_fitted(self) -> bool:
        return self._landmark_hists is not None

    @property
    def output_dim(self) -> int:
        """Dimensionality of the output embedding."""
        if self._U is not None:
            return self._U.shape[1]
        return self.n_components

    def fit(self, landmark_dags: Sequence[DAG]) -> WLNystromEncoder:
        """Fit the Nyström projection from a set of landmark DAGs.

        Parameters
        ----------
        landmark_dags : sequence of DAG
            Representative DAGs (e.g. sampled from episodic memory).
            The number of landmarks determines the embedding dimension.
        """
        m = min(len(landmark_dags), self.n_components)
        landmarks = landmark_dags[:m]

        # Compute WL histograms for all landmarks
        self._landmark_hists = [wl_relabel(dag, self.wl_iterations) for dag in landmarks]

        # Build m×m kernel matrix
        K_mm = np.zeros((m, m), dtype=np.float64)
        for i in range(m):
            for j in range(i, m):
                k = wl_kernel(self._landmark_hists[i], self._landmark_hists[j])
                K_mm[i, j] = k
                K_mm[j, i] = k

        # Regularize and decompose
        K_mm += self.regularization * np.eye(m)
        eigenvalues, eigenvectors = np.linalg.eigh(K_mm)

        # Keep only positive eigenvalues
        pos_mask = eigenvalues > self.regularization
        eigenvalues = eigenvalues[pos_mask]
        eigenvectors = eigenvectors[:, pos_mask]

        self._U = eigenvectors
        self._S_inv_sqrt = np.diag(1.0 / np.sqrt(eigenvalues))

        return self

    def encode(self, dag: DAG) -> np.ndarray:
        """Encode a DAG as a fixed-dimensional numpy vector.

        Returns a 1-D array of shape ``(output_dim,)``.  If the encoder is
        not yet fitted, returns a zero vector of shape ``(n_components,)``.
        """
        if not self.is_fitted or self._landmark_hists is None:
            return np.zeros(self.n_components, dtype=np.float32)

        hist = wl_relabel(dag, self.wl_iterations)

        # Compute kernel vector k(x, landmarks)
        m = len(self._landmark_hists)
        k_vec = np.array(
            [wl_kernel(hist, self._landmark_hists[i]) for i in range(m)],
            dtype=np.float64,
        )

        # Nyström projection: φ(x) = K_xm @ U @ S^{-1/2}
        embedding = k_vec @ self._U @ self._S_inv_sqrt
        return embedding.astype(np.float32)

    def encode_batch(self, dags: Sequence[DAG]) -> np.ndarray:
        """Encode multiple DAGs.  Returns shape ``(len(dags), output_dim)``."""
        return np.stack([self.encode(dag) for dag in dags])
