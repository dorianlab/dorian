"""
dorian/pipeline/vault_transform.py
----------------------------------
In-memory resolution of ``${VAR_NAME}`` env var references in the pipeline DAG.

Called during the expansion chain in ``execution.py``, after dataset / compound
expansion but before ``build_dag_graph()``.  The resolved DAG is **never
persisted** — it exists only in the execution thread's memory and is discarded
after Dask finishes.

Pattern
~~~~~~~
Env var references follow the same architectural pattern as ``dorian.io.dataset``:
bound by reference (``${VAR_NAME}``), resolved at execution time.  The key
difference is that dataset references resolve from Redis session metadata,
while vault references resolve from encrypted Redis envelopes using the user's
passphrase.

Security invariants
~~~~~~~~~~~~~~~~~~~
* The passphrase arrives via a one-shot nonce (60 s TTL, consumed on use).
* Decrypted plaintext lives only in local Python variables — never written to
  Redis, the docstore, logs, or any persistent store.
* The passphrase variable is set to ``None`` by the caller immediately after
  this function returns.
"""
from __future__ import annotations

import re

from dorian.dag import DAG, Parameter
from dorian.vault.crypto import decrypt_envelope

# Synchronous Redis client — vault_transform runs in the Dask background thread,
# not in an asyncio event loop.
from backend.envs import redis
from backend.events import Event, emit
from dorian.infra.keys import RedisKeys

_ENV_REF = re.compile(r"^\$\{(\w+)\}$")


def _get_envelope_sync(uid: str, var_name: str) -> dict | None:
    """Retrieve an encrypted envelope from Redis (synchronous)."""
    import json
    raw = redis.get(RedisKeys.vault_env(uid, var_name))
    if raw is None:
        return None
    return json.loads(raw)


def resolve_vault_references(pipeline: DAG, uid: str, passphrase: str) -> DAG:
    """Replace ``${VAR_NAME}`` Parameter values with decrypted secrets.

    Parameters
    ----------
    pipeline : DAG
        The expanded pipeline (after dataset + compound expansion).
    uid : str
        User identifier — used to locate encrypted envelopes in Redis.
    passphrase : str
        The user's vault passphrase (from the one-shot nonce).

    Returns
    -------
    DAG
        A new DAG with env var parameters replaced by resolved string values.
        Original DAG is not mutated.

    Raises
    ------
    ValueError
        If a referenced env var is not found in the user's vault.
    cryptography.exceptions.InvalidTag
        If the passphrase is incorrect.
    """
    new_nodes = {}
    resolved_count = 0

    for nid, node in pipeline.nodes.items():
        if isinstance(node, Parameter) and node.dtype == "env":
            m = _ENV_REF.match(node.value)
            if m:
                var_name = m.group(1)
                envelope = _get_envelope_sync(uid, var_name)
                if not envelope:
                    raise ValueError(
                        f"Environment variable '{var_name}' not found in your vault. "
                        f"Please add it via the Environment Variables panel."
                    )
                plaintext = decrypt_envelope(envelope, passphrase)
                # Replace with a resolved string Parameter (value is the decrypted secret).
                # dtype is set to "string" so the operator resolver evaluates it as str().
                new_nodes[nid] = Parameter(
                    name=node.name, dtype="string", value=plaintext
                )
                resolved_count += 1
                emit(Event("VaultReferenceResolved", {"var_name": var_name, "node_id": nid}))
            else:
                # dtype=env but value doesn't match ${...} pattern — the user
                # hasn't bound a vault variable yet (empty or free-text value).
                raise ValueError(
                    f"Parameter '{node.name}' is an environment variable but has no "
                    f"vault binding.  Click the parameter node and select a variable "
                    f"from the dropdown (value should be ${{VAR_NAME}})."
                )
        else:
            new_nodes[nid] = node

    if resolved_count > 0:
        emit(Event("VaultReferencesResolved", {"count": resolved_count, "uid": uid}))

    return DAG(nodes=new_nodes, edges=pipeline.edges)
