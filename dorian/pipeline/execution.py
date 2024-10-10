from backend.envs import worker_client
from dorian.operator import executable
from dorian.dag import DAG


def flatten(_list):
    return [el for li in _list for el in li]


def unique(_list):
    # the order is preserved
    res = []
    for el in _list:
        if el not in res:
            res.append(el)
    return res


def execute(pipeline: DAG):
    ee = pipeline.edges
    i = lambda v, k: v[k] if isinstance(v, tuple|list) else v
    # q = set(e.source for e in ee).difference(set(e.destination for e in ee))
    candidates = unique(flatten([[e.source, e.destination] for e in ee]))
    args = lambda nid: sorted(filter(lambda e: e.destination == nid, ee), key=lambda e: e.position)
    with worker_client() as client:
        futures = {}
        for nid in candidates:
            futures[nid] = client.submit(executable(pipeline.nodes[nid]), *map(lambda x: i(futures[x.source].result(), x.output), args(nid)))
        return client.gather(futures[candidates[-1]])