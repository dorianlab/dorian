# Dev Datastores: Redis, MongoDB, Neo4j (Dockerized)

Spin up Redis, MongoDB, and Neo4j with minimal friction, persistence, and healthchecks. This stack is intended for local development and CI.

## Quick Start

TBD

```bash
docker compose up -d --build

docker compose ps
docker compose logs -f redis
docker compose logs -f mongo
docker compose logs -f neo4j
```