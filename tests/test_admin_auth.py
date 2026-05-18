"""Tests for backend/admin_auth.py — bearer-token + audit-username gate.

The conftest stubs backend.* into sys.modules, so we load the admin-auth
module directly and inject a stub ``backend.config`` that matches its
usage surface.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException


def _load(env=None, cfg_token="", cfg_usernames=("sergred",)):
    """Load admin_auth with a freshly-stubbed backend.config."""
    # Ensure env is in the state the test expects BEFORE module load.
    for k in ("DORIAN_ADMIN_TOKEN",):
        os.environ.pop(k, None)
    if env:
        for k, v in env.items():
            os.environ[k] = v

    # Build a fake backend.config matching admin.token / admin.usernames.
    fake_admin = types.SimpleNamespace(token=cfg_token, usernames=list(cfg_usernames))
    fake_config = types.SimpleNamespace(admin=fake_admin)
    fake_bc = types.ModuleType("backend.config")
    fake_bc.config = fake_config
    sys.modules["backend.config"] = fake_bc

    path = Path(__file__).resolve().parents[1] / "backend" / "admin_auth.py"
    name = f"_admin_auth_{os.urandom(4).hex()}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_503_when_no_token_configured():
    m = _load(cfg_token="")
    with pytest.raises(HTTPException) as ei:
        m.require_admin(x_admin_token="anything", x_admin_username="sergred")
    assert ei.value.status_code == 503


def test_401_when_token_missing():
    m = _load(cfg_token="s3cr3t")
    with pytest.raises(HTTPException) as ei:
        m.require_admin(x_admin_token=None, x_admin_username="sergred")
    assert ei.value.status_code == 401


def test_401_when_token_wrong():
    m = _load(cfg_token="s3cr3t")
    with pytest.raises(HTTPException) as ei:
        m.require_admin(x_admin_token="not-it", x_admin_username="sergred")
    assert ei.value.status_code == 401


def test_403_when_username_missing():
    m = _load(cfg_token="s3cr3t")
    with pytest.raises(HTTPException) as ei:
        m.require_admin(x_admin_token="s3cr3t", x_admin_username=None)
    assert ei.value.status_code == 403


def test_403_when_username_not_in_allowlist():
    m = _load(cfg_token="s3cr3t", cfg_usernames=("sergred",))
    with pytest.raises(HTTPException) as ei:
        m.require_admin(x_admin_token="s3cr3t", x_admin_username="random")
    assert ei.value.status_code == 403


def test_403_demo_usernames_rejected():
    m = _load(cfg_token="s3cr3t", cfg_usernames=("sergred", "demo-abc"))
    with pytest.raises(HTTPException) as ei:
        m.require_admin(x_admin_token="s3cr3t", x_admin_username="demo-abc")
    assert ei.value.status_code == 403


def test_env_token_overrides_config():
    m = _load(env={"DORIAN_ADMIN_TOKEN": "env-tok"}, cfg_token="cfg-tok")
    # Config token should NOT work now.
    with pytest.raises(HTTPException):
        m.require_admin(x_admin_token="cfg-tok", x_admin_username="sergred")
    # Env token works.
    caller = m.require_admin(x_admin_token="env-tok", x_admin_username="sergred")
    assert caller == "sergred"


def test_happy_path_returns_username():
    m = _load(cfg_token="s3cr3t")
    assert m.require_admin(x_admin_token="s3cr3t", x_admin_username="sergred") == "sergred"


def test_constant_time_compare_does_not_short_circuit():
    """Smoke — correct token of a different length still rejects, same length accepts."""
    m = _load(cfg_token="abcdefghij")  # length 10
    # Different length, mismatch → 401
    with pytest.raises(HTTPException):
        m.require_admin(x_admin_token="short", x_admin_username="sergred")
    # Same length, mismatch → 401
    with pytest.raises(HTTPException):
        m.require_admin(x_admin_token="1234567890", x_admin_username="sergred")
    # Exact match → success
    assert m.require_admin(x_admin_token="abcdefghij", x_admin_username="sergred") == "sergred"
