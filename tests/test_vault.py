"""
tests/test_vault.py
-------------------
Unit and contract tests for the encrypted environment variable vault.

Tests cover:
  - Crypto round-trip (encrypt → decrypt)
  - Redis key design
  - Vault storage module (store / list / delete / check)
  - Vault transform (${VAR_NAME} resolution in DAG)
  - API route module structure
  - Frontend type contracts (TypeScript source file assertions)
  - Parameter registry (api_key dtype=env for openrouter)
  - Operator resolver (env dtype handling)
  - DAG SupportedType includes "env"
"""
from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import re
from pathlib import Path
from typing import get_args

import pytest


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[1]
_FRONTEND = _ROOT / "frontend"
_BACKEND = _ROOT / "dorian"


# ===========================================================================
# 1. Crypto module
# ===========================================================================

class TestVaultCrypto:
    """Server-side AES-256-GCM decryption (dorian.vault.crypto)."""

    def test_module_importable(self):
        from dorian.vault.crypto import decrypt_envelope
        assert callable(decrypt_envelope)

    def test_decrypt_envelope_signature(self):
        from dorian.vault.crypto import decrypt_envelope
        sig = inspect.signature(decrypt_envelope)
        params = list(sig.parameters.keys())
        assert params == ["envelope", "passphrase"]

    def test_derive_key_signature(self):
        from dorian.vault.crypto import _derive_key
        sig = inspect.signature(_derive_key)
        params = list(sig.parameters.keys())
        assert params == ["passphrase", "salt"]

    def test_pbkdf2_iterations_constant(self):
        from dorian.vault.crypto import _PBKDF2_ITERATIONS
        assert _PBKDF2_ITERATIONS == 600_000

    def test_key_length_constant(self):
        from dorian.vault.crypto import _KEY_LENGTH_BYTES
        assert _KEY_LENGTH_BYTES == 32

    def test_round_trip_encrypt_decrypt(self):
        """Encrypt with the Python module and verify decryption recovers plaintext."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from dorian.vault.crypto import _derive_key, decrypt_envelope

        passphrase = "test-passphrase-42"
        plaintext = "sk-proj-my-secret-api-key-12345"

        # Encrypt (simulating frontend)
        salt = os.urandom(16)
        iv = os.urandom(12)
        key = _derive_key(passphrase, salt)
        ciphertext = AESGCM(key).encrypt(iv, plaintext.encode(), None)

        envelope = {
            "ciphertext": base64.b64encode(ciphertext).decode(),
            "iv": base64.b64encode(iv).decode(),
            "salt": base64.b64encode(salt).decode(),
        }

        # Decrypt with vault module
        result = decrypt_envelope(envelope, passphrase)
        assert result == plaintext

    def test_wrong_passphrase_raises(self):
        """Wrong passphrase should raise InvalidTag."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.exceptions import InvalidTag
        from dorian.vault.crypto import _derive_key, decrypt_envelope

        passphrase = "correct-passphrase"
        salt = os.urandom(16)
        iv = os.urandom(12)
        key = _derive_key(passphrase, salt)
        ciphertext = AESGCM(key).encrypt(iv, b"secret", None)

        envelope = {
            "ciphertext": base64.b64encode(ciphertext).decode(),
            "iv": base64.b64encode(iv).decode(),
            "salt": base64.b64encode(salt).decode(),
        }

        with pytest.raises(InvalidTag):
            decrypt_envelope(envelope, "wrong-passphrase")


# ===========================================================================
# 2. Redis key design
# ===========================================================================

class TestVaultRedisKeys:
    """RedisKeys contract for vault-related keys."""

    def test_vault_env_key_format(self):
        from dorian.infra.keys import RedisKeys
        key = RedisKeys.vault_env("user42", "MY_API_KEY")
        assert key == "vault:user42:env:MY_API_KEY"

    def test_vault_env_index_key_format(self):
        from dorian.infra.keys import RedisKeys
        key = RedisKeys.vault_env_index("user42")
        assert key == "vault:user42:env:__index"

    def test_vault_passphrase_nonce_key_format(self):
        from dorian.infra.keys import RedisKeys
        key = RedisKeys.vault_passphrase_nonce("abc123")
        assert key == "vault:nonce:abc123"

    def test_keys_are_uid_scoped(self):
        """Vault keys must be scoped by uid for multi-user isolation."""
        from dorian.infra.keys import RedisKeys
        k1 = RedisKeys.vault_env("alice", "KEY")
        k2 = RedisKeys.vault_env("bob", "KEY")
        assert k1 != k2
        assert "alice" in k1
        assert "bob" in k2


# ===========================================================================
# 3. Storage module
# ===========================================================================

class TestVaultStorage:
    """Contract tests for dorian.vault.storage exports."""

    def test_module_importable(self):
        import dorian.vault.storage as m
        assert hasattr(m, "store_env_var")
        assert hasattr(m, "delete_env_var")
        assert hasattr(m, "list_env_vars")
        assert hasattr(m, "get_env_var_envelope")
        assert hasattr(m, "check_env_vars")
        assert hasattr(m, "store_passphrase_nonce")
        assert hasattr(m, "consume_passphrase_nonce")

    def test_store_env_var_signature(self):
        from dorian.vault.storage import store_env_var
        sig = inspect.signature(store_env_var)
        params = list(sig.parameters.keys())
        assert params == ["uid", "var_name", "envelope"]

    def test_check_env_vars_signature(self):
        from dorian.vault.storage import check_env_vars
        sig = inspect.signature(check_env_vars)
        params = list(sig.parameters.keys())
        assert params == ["uid", "required"]

    def test_consume_passphrase_nonce_signature(self):
        from dorian.vault.storage import consume_passphrase_nonce
        sig = inspect.signature(consume_passphrase_nonce)
        params = list(sig.parameters.keys())
        assert params == ["nonce"]

    def test_nonce_ttl_constant(self):
        from dorian.vault.storage import _NONCE_TTL_SECONDS
        assert _NONCE_TTL_SECONDS == 60


# ===========================================================================
# 4. Vault transform (DAG resolution)
# ===========================================================================

class TestVaultTransform:
    """Contract tests for dorian.pipeline.vault_transform."""

    def test_module_importable(self):
        from dorian.pipeline.vault_transform import resolve_vault_references
        assert callable(resolve_vault_references)

    def test_resolve_signature(self):
        from dorian.pipeline.vault_transform import resolve_vault_references
        sig = inspect.signature(resolve_vault_references)
        params = list(sig.parameters.keys())
        assert params == ["pipeline", "uid", "passphrase"]

    def test_env_ref_regex(self):
        from dorian.pipeline.vault_transform import _ENV_REF
        assert _ENV_REF.match("${MY_API_KEY}").group(1) == "MY_API_KEY"
        assert _ENV_REF.match("${OPENROUTER_KEY_123}").group(1) == "OPENROUTER_KEY_123"
        assert _ENV_REF.match("plain_text") is None
        assert _ENV_REF.match("${with spaces}") is None
        assert _ENV_REF.match("${}") is None

    def test_passthrough_non_env_nodes(self):
        """Nodes that are not dtype=env should pass through unchanged."""
        from dorian.dag import DAG, Parameter, Operator
        from dorian.pipeline.vault_transform import resolve_vault_references

        dag = DAG(
            nodes={
                "p1": Parameter(name="x", dtype="int", value="42"),
                "op1": Operator(name="sklearn.svm.SVC", language="python"),
            },
            edges=[],
        )
        # No vault passphrase needed — no env nodes to resolve
        result = resolve_vault_references(dag, "user1", "any-passphrase")
        assert "p1" in result.nodes
        assert result.nodes["p1"].value == "42"


# ===========================================================================
# 5. DAG type system
# ===========================================================================

class TestDagEnvType:
    """DAG SupportedType must include 'env'."""

    def test_env_in_supported_type(self):
        from dorian.dag import SupportedType
        assert "env" in get_args(SupportedType)

    def test_parameter_accepts_env_dtype(self):
        from dorian.dag import Parameter
        p = Parameter(name="api_key", dtype="env", value="${MY_KEY}")
        assert p.dtype == "env"
        assert p.value == "${MY_KEY}"


# ===========================================================================
# 6. Operator resolver
# ===========================================================================

class TestOperatorResolverEnv:
    """Operator resolver must handle dtype='env' without crashing."""

    def test_env_in_safe_dtypes(self):
        from dorian.dag import Parameter
        from dorian.pipeline.operator_resolver import _resolve_parameter
        p = Parameter(name="api_key", dtype="env", value="${MY_KEY}")
        fn = _resolve_parameter(p)
        # env dtype resolves as str — returns the reference string
        result = fn()
        assert result == "${MY_KEY}"


# ===========================================================================
# 7. Parameter registry
# ===========================================================================

class TestParamRegistryEnv:
    """openrouter.chat.completion must have api_key with dtype=env in the KB source."""

    def test_api_key_in_openrouter_params(self):
        # Verify the KB source declares api_key with dtype=env for openrouter.
        # The python source modules were converted to .kb files; check the
        # .kb file directly. The rust KB loader parses these into the snapshot.
        from pathlib import Path
        kb = Path(__file__).resolve().parents[1] / "dorian/knowledge/sources/llm.kb"
        knowledge = kb.read_text()
        assert "openrouter.chat.completion has parameter api_key" in knowledge
        assert "is of type env" in knowledge


# ===========================================================================
# 8. API routes
# ===========================================================================

class TestVaultRoutes:
    """Contract tests for dorian.api.routes.vault module."""

    def test_module_importable(self):
        from dorian.api.routes.vault import router
        assert router is not None

    def test_route_paths(self):
        from dorian.api.routes.vault import router
        paths = {r.path for r in router.routes}
        assert "/vault/env" in paths
        assert "/vault/env/{var_name}" in paths
        assert "/vault/env/check-pipeline" in paths
        assert "/vault/nonce" in paths

    def test_main_includes_vault_router(self):
        """main.py must register the vault router."""
        main_src = (_ROOT / "main.py").read_text()
        assert "vault" in main_src
        assert "app.include_router(vault.router)" in main_src


# ===========================================================================
# 9. Event registry
# ===========================================================================

class TestVaultEventRegistry:
    """Vault events must be registered in the event registry."""

    def test_registry_subscribes_vault_events(self):
        src = (_BACKEND / "event" / "registry.py").read_text()
        assert "VaultEnvVarStored" in src
        assert "VaultEnvVarDeleted" in src


# ===========================================================================
# 10. Frontend contracts (TypeScript source assertions)
# ===========================================================================

class TestVaultFrontendContracts:
    """Verify TypeScript source files contain expected vault symbols."""

    def test_app_event_name_includes_vault_events(self):
        src = (_FRONTEND / "types" / "index.ts").read_text()
        assert '"VaultEnvVarStored"' in src
        assert '"VaultEnvVarDeleted"' in src

    def test_ws_events_has_vault_wrappers(self):
        src = (_FRONTEND / "helpers" / "ws-events.ts").read_text()
        assert "vaultEnvVarStored" in src
        assert "vaultEnvVarDeleted" in src

    def test_vault_store_exists(self):
        p = _FRONTEND / "store" / "vault.ts"
        assert p.exists(), "frontend/store/vault.ts missing"
        src = p.read_text()
        assert "useVaultStore" in src
        assert "envVars" in src
        assert "passphraseUnlocked" in src
        assert "missingVars" in src

    def test_vault_crypto_exists(self):
        p = _FRONTEND / "lib" / "vault-crypto.ts"
        assert p.exists(), "frontend/lib/vault-crypto.ts missing"
        src = p.read_text()
        assert "deriveKey" in src
        assert "encrypt" in src
        assert "decrypt" in src
        assert "EncryptedEnvelope" in src
        assert "PBKDF2" in src

    def test_vault_api_client_exists(self):
        p = _FRONTEND / "app" / "api" / "vault.ts"
        assert p.exists(), "frontend/app/api/vault.ts missing"
        src = p.read_text()
        assert "storeEnvVar" in src
        assert "deleteEnvVar" in src
        assert "listEnvVars" in src
        assert "checkPipelineEnvVars" in src
        assert "storePassphraseNonce" in src

    def test_environment_panel_exists(self):
        p = _FRONTEND / "components" / "vault" / "EnvironmentPanel.tsx"
        assert p.exists(), "EnvironmentPanel.tsx missing"
        src = p.read_text()
        assert "EnvironmentPanel" in src
        assert "PassphraseGate" in src
        assert "AddEnvVarForm" in src

    def test_security_info_panel_exists(self):
        p = _FRONTEND / "components" / "vault" / "SecurityInfoPanel.tsx"
        assert p.exists(), "SecurityInfoPanel.tsx missing"
        src = p.read_text()
        assert "SecurityInfoPanel" in src
        assert "Research Prototype" in src
        assert "DFKI" in src

    def test_missing_env_vars_dialog_exists(self):
        p = _FRONTEND / "components" / "vault" / "MissingEnvVarsDialog.tsx"
        assert p.exists(), "MissingEnvVarsDialog.tsx missing"
        src = p.read_text()
        assert "MissingEnvVarsDialog" in src
        assert "synonym" in src.lower()

    def test_parameter_node_has_env_var_support(self):
        p = _FRONTEND / "components" / "pipeline" / "composition" / "Nodes" / "parameter.tsx"
        assert p.exists()
        src = p.read_text()
        assert "isEnvVar" in src or "EnvVarInput" in src
        assert "useVaultStore" in src

    def test_store_barrel_exports_vault(self):
        src = (_FRONTEND / "store" / "index.ts").read_text()
        assert "useVaultStore" in src


# ===========================================================================
# 11. Execution integration
# ===========================================================================

class TestExecutionVaultIntegration:
    """Pipeline execution must accept and propagate vault_passphrase."""

    def test_run_pipeline_accepts_vault_passphrase(self):
        from dorian.pipeline.execution import run_pipeline
        sig = inspect.signature(run_pipeline)
        assert "vault_passphrase" in sig.parameters

    def test_run_pipeline_vault_passphrase_optional(self):
        from dorian.pipeline.execution import run_pipeline
        sig = inspect.signature(run_pipeline)
        param = sig.parameters["vault_passphrase"]
        assert param.default is None

    def test_execution_imports_vault_transform(self):
        """execution.py must reference vault_transform for resolution."""
        src = (_BACKEND / "pipeline" / "execution.py").read_text()
        assert "vault_transform" in src or "resolve_vault_references" in src

    def test_execution_forgets_passphrase(self):
        """execution.py must set vault_passphrase = None after use."""
        src = (_BACKEND / "pipeline" / "execution.py").read_text()
        assert "vault_passphrase = None" in src
