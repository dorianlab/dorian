"""
backend/hmac_auth.py
--------------------
HMAC-SHA256 request signing middleware.

Every HTTP request (except WebSocket upgrades and exempt paths) must carry
three headers:

    X-HMAC-Signature:  hex(HMAC-SHA256(secret, canonical_request))
    X-HMAC-Timestamp:  Unix epoch seconds (integer) when the request was signed
    X-HMAC-Nonce:      Random hex string (≥16 chars) — prevents replay

The **canonical request** is the UTF-8 encoding of:

    {METHOD}\n{PATH}\n{TIMESTAMP}\n{NONCE}\n{BODY_SHA256}

where BODY_SHA256 = hex(SHA-256(raw_body))  (empty string → SHA-256 of b"").

Replay protection:
  - Timestamp must be within ±tolerance seconds of server time.
  - Nonce must not have been seen within the tolerance window (stored in Redis
    with TTL = tolerance).

Configuration (config.yaml → development.hmac):
  - secret:               hex-encoded shared key
  - timestamp_tolerance:  allowed clock skew in seconds (default 300)
  - algorithm:            "sha256" (reserved for future extension)
"""

from __future__ import annotations

import hashlib
import hmac
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

import os

from backend.config import config
from backend.envs import aioredis

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_hmac_cfg = config.hmac
_raw_secret: str = os.environ.get("DORIAN_HMAC_SECRET", str(getattr(_hmac_cfg, "secret", "") or ""))

# Fail fast if HMAC is unconfigured outside dev. Silent ``HMAC_ENABLED=False``
# made the backend serve every request unauthenticated whenever the secret
# happened to be empty — a hands-off security regression. Dev users running
# tests / local stand-alone code can still set ``DORIAN_ENV=dev`` to bypass.
if not _raw_secret:
    _env = os.environ.get("DORIAN_ENV", "").lower()
    if _env not in ("", "dev", "test"):
        raise RuntimeError(
            f"DORIAN_HMAC_SECRET is empty in DORIAN_ENV={_env!r}. "
            "Auth would silently disable. Set the secret (openssl rand -hex 32) "
            "or set DORIAN_ENV=dev to allow the unsigned dev path."
        )

HMAC_SECRET: bytes = _raw_secret.encode()
HMAC_ENABLED: bool = len(_raw_secret) > 0
TIMESTAMP_TOLERANCE: int = int(getattr(_hmac_cfg, "timestamp_tolerance", 300))

# Paths that skip HMAC verification.
#   /ws*           — WebSocket (signed by the NextAuth session flow)
#   /docs, /redoc  — OpenAPI docs (dev convenience)
#   /openapi.json  — OpenAPI spec
#   /stats         — welcome-screen platform counters; public by design
#                    (browser hits it directly, no HMAC signature possible
#                    without routing through the gateway)
_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/ws", "/docs", "/redoc", "/openapi.json", "/stats",
    # /healthz is the docker-compose worker-pool liveness probe — it
    # MUST stay reachable without an HMAC nonce so a stalled / dead
    # backend can return 503 and trigger a container restart.
    "/healthz",
    # /catalog/objectives and /catalog/evals are called by the Rust
    # gateway's full_catalog handler without HMAC (internal service call).
    "/catalog/objectives",
    "/catalog/evals",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _canonical_request(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> bytes:
    """Build the canonical string that both client and server sign."""
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method}\n{path}\n{timestamp}\n{nonce}\n{body_hash}".encode()


def _compute_signature(canonical: bytes) -> str:
    return hmac.new(HMAC_SECRET, canonical, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class HMACAuthMiddleware(BaseHTTPMiddleware):
    """Validate HMAC-SHA256 signatures on every non-exempt HTTP request."""

    async def dispatch(self, request: Request, call_next):
        # Pass through when HMAC is not configured (dev mode).
        if not HMAC_ENABLED:
            return await call_next(request)

        # Skip WebSocket upgrades and exempt paths.
        if request.scope.get("type") == "websocket":
            return await call_next(request)

        # Skip CORS preflight — handled by CORSMiddleware.
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in _EXEMPT_PREFIXES):
            return await call_next(request)

        # --- Gateway pre-verified bypass --------------------------------------
        # When the Go gateway proxies a request to this backend, it strips the
        # original HMAC headers (to avoid nonce double-spend) and injects
        # X-Gateway-Verified with an HMAC of the secret itself.  This proves
        # the gateway possesses the same shared secret without replaying the
        # client's nonce.
        gw_token = request.headers.get("x-gateway-verified")
        if gw_token:
            expected = hmac.new(
                HMAC_SECRET, b"gateway-verified", hashlib.sha256
            ).hexdigest()
            if hmac.compare_digest(gw_token, expected):
                return await call_next(request)
            # Invalid token — fall through to normal HMAC validation which
            # will fail on missing headers and return 401.

        # --- Extract headers ---------------------------------------------------
        sig = request.headers.get("x-hmac-signature")
        ts = request.headers.get("x-hmac-timestamp")
        nonce = request.headers.get("x-hmac-nonce")

        if not sig or not ts or not nonce:
            return JSONResponse(
                {"detail": "Missing HMAC authentication headers"},
                status_code=401,
            )

        # --- Timestamp freshness -----------------------------------------------
        try:
            req_time = int(ts)
        except (ValueError, TypeError):
            return JSONResponse(
                {"detail": "Invalid X-HMAC-Timestamp"},
                status_code=401,
            )

        drift = abs(time.time() - req_time)
        if drift > TIMESTAMP_TOLERANCE:
            return JSONResponse(
                {"detail": "Request timestamp outside acceptable window"},
                status_code=401,
            )

        # --- Nonce replay protection -------------------------------------------
        nonce_key = f"hmac:nonce:{nonce}"
        # SET NX returns True if the key was created (nonce is fresh).
        is_fresh = await aioredis.set(nonce_key, "1", nx=True, ex=TIMESTAMP_TOLERANCE * 2)
        if not is_fresh:
            return JSONResponse(
                {"detail": "Nonce already used (possible replay)"},
                status_code=401,
            )

        # --- Signature verification --------------------------------------------
        # For multipart/form-data uploads the browser serialises the body
        # non-deterministically (boundary tokens vary), so both client and
        # server agree to hash an empty body for multipart requests.
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            body = b""
        else:
            body = await request.body()
        canonical = _canonical_request(
            method=request.method.upper(),
            path=path,
            timestamp=ts,
            nonce=nonce,
            body=body,
        )
        expected = _compute_signature(canonical)

        if not hmac.compare_digest(sig, expected):
            return JSONResponse(
                {"detail": "Invalid HMAC signature"},
                status_code=403,
            )

        return await call_next(request)
