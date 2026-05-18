"""
dorian/vault/storage.py
-----------------------
Redis CRUD for encrypted user environment variables.

Each env var is stored as a JSON-encoded ``EncryptedEnvelope`` at
``vault:{uid}:env:{var_name}``.  A per-user index SET at
``vault:{uid}:env:__index`` tracks defined variable names so we can
list them without a SCAN.

The server never sees plaintext values — it stores and retrieves only
the encrypted envelopes produced by the frontend's AES-256-GCM encryption.
"""
from __future__ import annotations

import json

from datetime import datetime, timezone

from backend.events import Event, aemit
from backend.envs import aioredis, expdb
from dorian.infra.keys import RedisKeys


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Write / Delete
# ---------------------------------------------------------------------------

async def store_env_var(uid: str, var_name: str, envelope: dict) -> None:
    """Persist an encrypted envelope for a single env var.

    Parameters
    ----------
    uid : str
        User identifier.
    var_name : str
        Environment variable name (e.g. ``OPENROUTER_API_KEY``).
    envelope : dict
        ``{ciphertext, iv, salt}`` — all base64-encoded strings.
    """
    key = RedisKeys.vault_env(uid, var_name)
    await aioredis.set(key, json.dumps(envelope))
    await aioredis.sadd(RedisKeys.vault_env_index(uid), var_name)

    # Durable backup — expdb survives Redis flush / compose down -v.
    try:
        await expdb.vault_secrets.update_one(
            {"uid": uid, "var_name": var_name},
            {"$set": {"envelope": envelope, "updatedAt": _now()}},
            upsert=True,
        )
    except Exception:
        pass  # best-effort; Redis is authoritative at runtime

    await aemit(Event("VaultEnvVarStored", {"var_name": var_name, "uid": uid}))


async def delete_env_var(uid: str, var_name: str) -> bool:
    """Delete an env var envelope and remove it from the index.

    Returns True if the key existed and was deleted.
    """
    key = RedisKeys.vault_env(uid, var_name)
    deleted = await aioredis.delete(key)
    await aioredis.srem(RedisKeys.vault_env_index(uid), var_name)

    try:
        await expdb.vault_secrets.delete_one({"uid": uid, "var_name": var_name})
    except Exception:
        pass

    if deleted:
        await aemit(Event("VaultEnvVarDeleted", {"var_name": var_name, "uid": uid}))
    return bool(deleted)


# ---------------------------------------------------------------------------
# Read / List
# ---------------------------------------------------------------------------

async def list_env_vars(uid: str) -> list[str]:
    """Return the names of all env vars defined by a user (never the values).

    Uses the index SET so this is O(N) in the number of vars, not a SCAN.
    """
    members = await aioredis.smembers(RedisKeys.vault_env_index(uid))
    # smembers returns bytes or str depending on decode_responses config
    return sorted(m.decode() if isinstance(m, bytes) else m for m in members)


async def get_env_var_envelope(uid: str, var_name: str) -> dict | None:
    """Retrieve the encrypted envelope for one env var.

    Returns None if the variable doesn't exist.
    """
    raw = await aioredis.get(RedisKeys.vault_env(uid, var_name))
    if raw is None:
        return None
    return json.loads(raw)


async def check_env_vars(uid: str, required: set[str]) -> dict:
    """Check which of the required env vars are defined in the user's vault.

    Parameters
    ----------
    uid : str
        User identifier.
    required : set[str]
        Set of env var names that the pipeline requires.

    Returns
    -------
    dict
        ``{required: [...], available: [...], missing: [...]}``
    """
    user_vars = set(await list_env_vars(uid))
    available = required & user_vars
    missing = required - user_vars
    return {
        "required": sorted(required),
        "available": sorted(available),
        "missing": sorted(missing),
    }


# ---------------------------------------------------------------------------
# Passphrase nonce (ephemeral, for execution-time decryption)
# ---------------------------------------------------------------------------

_NONCE_TTL_SECONDS = 60


async def store_passphrase_nonce(nonce: str, passphrase: str) -> None:
    """Store a vault passphrase under a random nonce with a 60-second TTL.

    The frontend generates the nonce, POSTs it here, then includes the nonce
    in the ``ExecutePipeline`` payload.  The backend retrieves the passphrase
    from this key, decrypts env vars, and immediately deletes it.
    """
    key = RedisKeys.vault_passphrase_nonce(nonce)
    await aioredis.set(key, passphrase, ex=_NONCE_TTL_SECONDS)


async def consume_passphrase_nonce(nonce: str) -> str | None:
    """Retrieve and delete the passphrase stored under a nonce.

    Returns None if the nonce has expired or doesn't exist (one-shot use).
    """
    key = RedisKeys.vault_passphrase_nonce(nonce)
    passphrase = await aioredis.get(key)
    if passphrase is not None:
        await aioredis.delete(key)
        if isinstance(passphrase, bytes):
            passphrase = passphrase.decode()
    return passphrase


# ---------------------------------------------------------------------------
# Recovery — reload vault entries from the experiment DB into Redis on
# startup. ``expdb.vault_secrets`` is the durable copy; Redis holds the
# hot path but can be wiped (compose down -v, flushdb) without data loss.
# ---------------------------------------------------------------------------

async def recover_vault_from_store() -> int:
    """Reload any vault secrets from the document store that are missing
    in Redis. Called once at app startup. Returns the number of entries
    recovered.
    """
    recovered = 0
    try:
        cursor = expdb.vault_secrets.find({})
        async for doc in cursor:
            uid = doc["uid"]
            var_name = doc["var_name"]
            envelope = doc["envelope"]
            key = RedisKeys.vault_env(uid, var_name)
            # Only restore if Redis key is missing (don't overwrite live data).
            if not await aioredis.exists(key):
                await aioredis.set(key, json.dumps(envelope))
                await aioredis.sadd(RedisKeys.vault_env_index(uid), var_name)
                recovered += 1
    except Exception:
        pass  # best-effort on startup
    return recovered
