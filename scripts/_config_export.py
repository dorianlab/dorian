"""Translate ``config/config.yaml`` into the docker-compose env vocabulary.

Emits shell ``export VAR='value'`` lines on stdout; sourced by
``scripts/up.sh``. This is the explicit mapping between the structured
YAML schema (the single source of truth) and the flat environment
variable names docker compose substitutes into the stack. There are
no defaults here: if a value is missing in config.yaml, it is exported
as an empty string and the compose file's ``:?required`` checks abort
startup.

If you add a new env var consumed by docker-compose.yml or the
frontend bundle, add an explicit line below.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

_repo_root = Path(__file__).parents[1]
_config_file = _repo_root / "config" / "config.yaml"

if not _config_file.is_file():
    sys.stderr.write(f"error: missing {_config_file}\n")
    sys.exit(1)

with _config_file.open() as fh:
    _raw = yaml.safe_load(fh)

if not isinstance(_raw, dict) or "type" not in _raw or _raw["type"] not in _raw:
    sys.stderr.write(
        f"error: {_config_file} must declare top-level `type:` field "
        f"naming a section defined in the same file\n"
    )
    sys.exit(1)

_cfg = _raw[_raw["type"]]


def get(*path, default=None):
    cur = _cfg
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def shellq(v):
    if v is None:
        v = ""
    return "'" + str(v).replace("'", "'\\''") + "'"


def emit(var, value):
    print(f"export {var}={shellq(value)}")


# --- Database credentials (compose substitution targets) -----------
emit("DORIAN_POSTGRES_PASSWORD", get("postgresql", "password"))
emit("DORIAN_REDIS_PASSWORD",    get("redis", "password"))
emit("DORIAN_REDIS_PORT",        get("redis", "port", default=6379))

# --- Request signing -----------------------------------------------
_hmac = get("hmac", "secret")
emit("DORIAN_HMAC_SECRET",      _hmac)
emit("NEXT_PUBLIC_HMAC_SECRET", _hmac)

# --- Admin -------------------------------------------------------- -
emit("DORIAN_ADMIN_TOKEN", get("admin", "token"))

# --- Public URLs (frontend bundle bake-in) -------------------------
emit("DORIAN_PUBLIC_URL",         get("urls", "frontend"))
emit("NEXT_PUBLIC_FRONTEND_URL",  get("urls", "frontend"))
emit("NEXT_PUBLIC_BACKEND_URL",   get("urls", "backend"))
emit("NEXT_PUBLIC_WS_URL",        get("urls", "ws"))

# --- NextAuth ------------------------------------------------------
emit("NEXTAUTH_URL",    get("nextauth", "url"))
emit("NEXTAUTH_SECRET", get("nextauth", "secret"))

# --- CORS ----------------------------------------------------------
_origins = get("urls", "cors_origins", default=[])
if isinstance(_origins, list):
    emit("DORIAN_CORS_ORIGINS", ",".join(_origins))

# --- Optional GitHub OAuth (empty disables the GitHub sign-in path) -
emit("NEXT_PUBLIC_GITHUB_ID",     get("oauth", "github", "client_id"))
emit("NEXT_PUBLIC_GITHUB_SECRET", get("oauth", "github", "client_secret"))
