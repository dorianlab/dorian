"""
dorian/infra/keys.py
---------------------
Central namespace for every Redis key pattern used across the Dorian backend.

A single source of truth prevents typos, makes key schema changes easy to
audit, and lets ``grep dorian.infra.keys`` find every Redis access in the
codebase.
"""
from __future__ import annotations


# Approximate cap for Redis Stream XADD calls.  Using approximate trimming
# (``~maxlen``) so Redis can optimise radix-tree node boundaries.
STREAM_MAXLEN = 10_000


def _tenant_prefix() -> str:
    """Return the current tenant's Redis key prefix (empty in single-tenant mode).

    When multi-tenancy is enabled, all Redis keys are prefixed with the
    tenant identifier to prevent cross-tenant data leakage.  The prefix
    is read from ``contextvars`` so it threads through async call chains
    automatically.
    """
    try:
        from dorian.infra.tenant import current_tenant
        prefix = current_tenant().key_prefix
        return f"{prefix}:" if prefix else ""
    except Exception:
        return ""


class RedisKeys:
    """Static factory methods for every Redis key pattern in the system."""

    @staticmethod
    def session_meta(session: str) -> str:
        """``session:{session}:meta`` — JSON blob with dataset + pipeline context.

        # TTL: 24 h — set by caller (ex=86400).
        """
        return f"session:{session}:meta"

    @staticmethod
    def stream(uid: str, session: str) -> str:
        """``{uid}:{session}:stream`` — per-user Redis Stream for frontend events.

        # TTL: no TTL — trimmed by MAXLEN on xadd; cleaned on session close.
        """
        return f"{uid}:{session}:stream"

    @staticmethod
    def cursor(uid: str, session: str) -> str:
        """``{uid}:{session}:last`` — XREAD cursor position for WS consumer.

        # TTL: no TTL — cleaned on session close.
        """
        return f"{uid}:{session}:last"

    # Key for the set of active WS connections — entries are "{uid}:{session}"
    ACTIVE_CONNECTIONS = "dorian:active_connections"

    @staticmethod
    def execution(run_id: str) -> str:
        """``execution:{run_id}`` — PipelineExecution JSON.

        # TTL: no TTL — cleaned on session close.
        """
        return f"execution:{run_id}"

    @staticmethod
    def node_state(run_id: str, node_id: str) -> str:
        """``execution:{run_id}:node:{node_id}`` — individual NodeState JSON.

        # TTL: no TTL — cleaned on session close.
        """
        return f"execution:{run_id}:node:{node_id}"

    @staticmethod
    def result(run_id: str, node_id: str) -> str:
        """``result:{run_id}:{node_id}`` — node output reference (inline or file path).

        # TTL: 24 h — set by caller.
        """
        return f"result:{run_id}:{node_id}"

    @staticmethod
    def dataset_fpath(did: str) -> str:
        """``dataset:fpath:{did}`` — absolute file path for a dataset.

        # TTL: no TTL — persists for lifetime of dataset.
        """
        return f"dataset:fpath:{did}"

    @staticmethod
    def dataset_feature_columns(did: str) -> str:
        """``dataset:{did}:feature_columns`` — JSON list of feature column names.

        # TTL: no TTL — persists for lifetime of dataset.
        """
        return f"dataset:{did}:feature_columns"

    @staticmethod
    def dataset_target_columns(did: str) -> str:
        """``dataset:{did}:target_columns`` — JSON list of target column names.

        # TTL: no TTL — persists for lifetime of dataset.
        """
        return f"dataset:{did}:target_columns"

    @staticmethod
    def protected_attributes(did: str) -> str:
        """``dataset:{did}:protected_attributes`` — JSON list of protected attribute names.

        # TTL: no TTL — persists for lifetime of dataset.
        """
        return f"dataset:{did}:protected_attributes"

    # ------------------------------------------------------------------
    # User interaction / feedback
    # ------------------------------------------------------------------

    @staticmethod
    def interactions(uid: str, session: str) -> str:
        """``interactions:{uid}:{session}`` — RPUSH list of canvas/UI interaction events.

        # TTL: no TTL — cleaned on session close.
        """
        return f"interactions:{uid}:{session}"

    @staticmethod
    def feedback(uid: str, session: str, request_id: str) -> str:
        """``feedback:{uid}:{session}:{request_id}`` — single feedback submission.

        # TTL: no TTL — cleaned on session close.
        """
        return f"feedback:{uid}:{session}:{request_id}"

    @staticmethod
    def feedback_history(uid: str, session: str) -> str:
        """``feedback:{uid}:{session}:history`` — RPUSH list of feedback blobs for replay.

        # TTL: no TTL — cleaned on session close.
        """
        return f"feedback:{uid}:{session}:history"

    # ------------------------------------------------------------------
    # Generation engine
    # ------------------------------------------------------------------

    @staticmethod
    def generation_metafeatures(session: str) -> str:
        """``generation:metafeatures:{session}`` — cached metafeature vector for generation.

        # TTL: no TTL — cleaned on session close.
        """
        return f"generation:metafeatures:{session}"

    @staticmethod
    def generation_state(session: str) -> str:
        """``generation:state:{session}`` — scheduler state for a session.

        # TTL: no TTL — cleaned on session close.
        """
        return f"generation:state:{session}"

    @staticmethod
    def recommendation_interactions(session: str) -> str:
        """``session:{session}:recommendations:interactions`` — JSON interaction log.

        # TTL: no TTL — cleaned on session close.
        """
        return f"session:{session}:recommendations:interactions"

    @staticmethod
    def recommendation_pipeline(session: str, pipeline_id: str) -> str:
        """``recommendation:{session}:{pipeline_id}`` — cached pipeline body for a recommendation candidate.

        # TTL: no TTL — cleaned on session close.
        """
        return f"recommendation:{session}:{pipeline_id}"

    # ------------------------------------------------------------------
    # Pipeline extraction
    # ------------------------------------------------------------------

    @staticmethod
    def active_extraction(session: str) -> str:
        """``session:{session}:active_extraction`` — ID of in-progress extraction.

        # TTL: no TTL — cleaned on session close.
        """
        return f"session:{session}:active_extraction"

    # ------------------------------------------------------------------
    # User vault (encrypted environment variables)
    # ------------------------------------------------------------------

    @staticmethod
    def vault_env(uid: str, var_name: str) -> str:
        """``vault:{uid}:env:{var_name}`` — encrypted envelope for one env var.

        Stores a JSON blob: ``{ciphertext, iv, salt}`` (all base64).
        Encrypted client-side with AES-256-GCM; server stores only ciphertext.

        # TTL: no TTL — persists until user deletes.
        """
        return f"vault:{uid}:env:{var_name}"

    @staticmethod
    def vault_env_index(uid: str) -> str:
        """``vault:{uid}:env:__index`` — SET of env var names defined by the user.

        Used for listing without SCAN; never contains actual values.

        # TTL: no TTL — persists until user deletes.
        """
        return f"vault:{uid}:env:__index"

    @staticmethod
    def vault_passphrase_nonce(nonce: str) -> str:
        """``vault:nonce:{nonce}`` — ephemeral passphrase for execution.

        The frontend POSTs a random nonce + the user's vault passphrase.
        The backend stores it here with a 60-second TTL, retrieves it when
        the ``ExecutePipeline`` event arrives (matching nonce), and deletes
        it immediately after decryption.

        # TTL: 60 s — set by caller (ex=60).
        """
        return f"vault:nonce:{nonce}"

    # ------------------------------------------------------------------
    # AI Debugger scope
    # ------------------------------------------------------------------

    @staticmethod
    def canvas_operators(session: str) -> str:
        """``session:{session}:canvas_operators`` — SET of operator FQNs on the canvas.

        The AI Debugger uses this set to decide which suggestions are
        applicable to the current pipeline.  Updated by
        ``PipelineNodeAdded`` (SADD), ``PipelineNodeRemoved`` (SREM),
        ``PipelineComposed`` (DEL), and ``PipelineRetrieved`` (full sync).

        # TTL: no TTL — cleaned on session close.
        """
        return f"session:{session}:canvas_operators"

    # ------------------------------------------------------------------
    # Pipeline cancellation
    # ------------------------------------------------------------------

    @staticmethod
    def cancel_run(run_id: str) -> str:
        """``cancel:{run_id}`` — flag key set when a user cancels a running pipeline.

        Checked cooperatively by ``_instrument()`` before each node starts.

        # TTL: 300 s — set by caller (ex=300).
        """
        return f"cancel:{run_id}"

    # ------------------------------------------------------------------
    # Rule suggestion self-correction loop
    # ------------------------------------------------------------------

    @staticmethod
    def cancel_suggest(extraction_id: str) -> str:
        """``cancel:suggest:{extraction_id}`` — flag set when user cancels a suggestion.

        Checked cooperatively by the retry loop in ``handle_suggest_rules()``.

        # TTL: 60 s — set by caller (ex=60).
        """
        return f"cancel:suggest:{extraction_id}"

    @staticmethod
    def suggest_active(extraction_id: str) -> str:
        """``suggest:active:{extraction_id}`` — concurrency guard for suggestion loop.

        Prevents duplicate suggestion requests on the same extraction.

        # TTL: 120 s — set by caller (ex=120).
        """
        return f"suggest:active:{extraction_id}"

    # ------------------------------------------------------------------
    # Two-tier cache (dorian.infra.cache)
    # ------------------------------------------------------------------

    @staticmethod
    def cache_entry(prefix: str, key_hash: str) -> str:
        """``cache:{prefix}:{hash}`` — TTL-based cache entry.

        Managed by ``dorian.infra.cache``.  Prefix examples:
        ``kb:operator_interface``, ``rec:candidates``, ``profile:meta``.

        # TTL: varies (set by caller via ex=).
        """
        return f"cache:{prefix}:{key_hash}"

    # ------------------------------------------------------------------
    # User tiers & queue
    # ------------------------------------------------------------------

    @staticmethod
    def user_tier(uid: str) -> str:
        """``user:tier:{uid}`` — user's subscription tier name.

        Values: ``free``, ``standard``, ``priority``, ``enterprise``.

        # TTL: no TTL — persists until admin changes tier.
        """
        return f"user:tier:{uid}"

    # ------------------------------------------------------------------
    # Batch notifications (be-right-back / offline accumulation)
    # ------------------------------------------------------------------

    @staticmethod
    def pending_notifications(uid: str, session: str) -> str:
        """``notifications:{uid}:{session}:pending`` — LIST of unsent notifications.

        Accumulated while the user's WS connection is down.  Flushed as a
        batch on reconnect via ``seed_session``.

        # TTL: 24 h — set by caller (ex=86400).
        """
        return f"notifications:{uid}:{session}:pending"
