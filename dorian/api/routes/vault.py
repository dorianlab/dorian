"""
dorian/api/routes/vault.py
--------------------------
REST endpoints for encrypted user environment variables.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Form, Query
from fastapi.responses import JSONResponse

from backend.rate_limit import http_rate_limit
from dorian.vault.storage import (
    store_env_var as _store_env_var,
    delete_env_var as _delete_env_var,
    list_env_vars as _list_env_vars,
    check_env_vars as _check_env_vars,
    store_passphrase_nonce as _store_nonce,
)

router = APIRouter()


@router.post("/vault/env")
async def store_env_var(
    var_name: str = Form(...),
    envelope: str = Form(...),
    uid: str = Form(...),
    _rl=http_rate_limit("vault"),
):
    try:
        envelope_dict = json.loads(envelope)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "Invalid envelope JSON"})

    required_keys = {"ciphertext", "iv", "salt"}
    if not required_keys.issubset(envelope_dict.keys()):
        return JSONResponse(status_code=400, content={"error": f"Envelope must contain keys: {required_keys}"})

    await _store_env_var(uid, var_name, envelope_dict)
    return {"status": "stored", "var_name": var_name}


@router.delete("/vault/env/{var_name}")
async def delete_env_var(
    var_name: str,
    uid: str = Query(...),
    _rl=http_rate_limit("vault"),
):
    deleted = await _delete_env_var(uid, var_name)
    if not deleted:
        return JSONResponse(status_code=404, content={"error": "Variable not found"})
    return {"status": "deleted", "var_name": var_name}


@router.get("/vault/env")
async def list_env_vars(
    uid: str = Query(...),
    _rl=http_rate_limit("vault"),
):
    names = await _list_env_vars(uid)
    return [{"name": n, "hasValue": True} for n in names]


@router.post("/vault/env/check-pipeline")
async def check_pipeline_env_vars(
    pipeline_json: str = Form(...),
    uid: str = Form(...),
    _rl=http_rate_limit("vault"),
):
    try:
        pipeline = json.loads(pipeline_json)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "Invalid pipeline JSON"})

    required: set[str] = set()
    nodes = pipeline.get("nodes", {})
    if isinstance(nodes, dict):
        for node in nodes.values():
            if isinstance(node, dict):
                dtype = node.get("dtype") or node.get("type", "")
                value = node.get("value", "")
                if dtype == "env" and isinstance(value, str) \
                        and value.startswith("${") and value.endswith("}"):
                    required.add(value[2:-1])

    if not required:
        return {"required": [], "available": [], "missing": []}

    return await _check_env_vars(uid, required)


@router.post("/vault/nonce")
async def store_passphrase_nonce(
    nonce: str = Form(...),
    passphrase: str = Form(...),
    _rl=http_rate_limit("vault"),
):
    await _store_nonce(nonce, passphrase)
    return {"status": "ok"}
