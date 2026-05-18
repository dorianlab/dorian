"""Experiment Store — persistent storage and similarity indices for datasets,
pipelines, evaluations, and user interactions.

The Experiment Store is a *logical* concept spanning multiple backends:
- **PostgreSQL**: Interaction Table, dataset profiles, pipeline references, evaluations
- **Docstore**: Pipeline documents (already stored by the seeder / pipeline save flow)
- **Neo4j**: Operator metadata and KB (unchanged, accessed via existing queries)
- **In-memory indices**: KD-Tree (dataset similarity) and BK-Tree (pipeline similarity)

Public API::

    from dorian.experiment.store import (
        init_experiment_store,
        get_experiment_store,
        shutdown_experiment_store,
    )
"""
