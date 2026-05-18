"""Annotate a DAG node with the data-science task path it performs.

Walks the curated KB (rust snapshot) BFS-style from the operator
node toward ``Data Science Task``. The first reachable path is
emitted as a ``TaskIdentified`` event so the AI Debugger can scope
its risk identification to the right task.
"""
from __future__ import annotations

from collections import deque
from dataclasses import replace

from dorian.dag import DAG
from dorian.knowledge.ontology_kb import load_kb
from backend.events import Event, aemit


_TASK_ROOT = "Data Science Task"


def _shortest_path(kb, source: str, target: str, max_depth: int = 8) -> list[str]:
    """BFS from ``source`` toward ``target`` across all out-edges.

    Returns the sequence of node display names along the first
    shortest path, or ``[]`` when ``target`` isn't reachable within
    ``max_depth`` hops.
    """
    if source == target:
        return [kb.display(source)]
    visited: set[str] = {source}
    queue: deque[tuple[str, list[str]]] = deque([(source, [source])])
    while queue:
        node, path = queue.popleft()
        if len(path) > max_depth:
            continue
        for predicate, dests in (kb.adj.get(node, {}) or {}).items():
            for dst in dests:
                if dst in visited:
                    continue
                visited.add(dst)
                new_path = path + [dst]
                if kb.display(dst) == target or dst == target:
                    return [kb.display(n) for n in new_path]
                queue.append((dst, new_path))
    return []


async def populate_tasks(g: DAG, m: dict, meta: dict) -> DAG:
    operator = g.nodes[m["0"]].name
    kb = load_kb()
    tasks = _shortest_path(kb, operator, _TASK_ROOT)
    if tasks:
        await aemit(Event(
            "TaskIdentified",
            data={
                "uid": meta["uid"],
                "session": meta["session"],
                "paths": tasks,
                "operator": operator,
            },
        ))
        return DAG(
            nodes=dict(g.nodes, **{m["0"]: replace(g.nodes[m["0"]], tasks=tasks)}),
            edges=g.edges,
        )

    await aemit(Event(
        "UnknownTask",
        data={"uid": meta["uid"], "session": meta["session"], "operator": operator},
    ))
    return g
