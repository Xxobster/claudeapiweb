"""Credentials API. The key goes in, a status comes out; the key is never returned."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, SecretStr

from cpm.providers.keycheck import KeyValidator, MockKeyValidator
from cpm.security.secrets_store import ANTHROPIC_KEY, SecretStoreUnavailable

log = logging.getLogger("cpm.credentials")
router = APIRouter(prefix="/api/credentials/anthropic", tags=["credentials"])


class KeyIn(BaseModel):
    api_key: SecretStr
    live: bool = False


class TestIn(BaseModel):
    live: bool = False


def _validator(request: Request, live: bool) -> KeyValidator:
    if live:
        if not request.app.state.settings.allow_live_key_test:
            raise HTTPException(403, "live key test is disabled (set CPM_ALLOW_LIVE_KEY_TEST=1)")
        validator: KeyValidator = request.app.state.live_validator
        return validator
    validator = request.app.state.key_validator or MockKeyValidator()
    return validator


def _store(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.secrets


def _status(request: Request) -> dict[str, object]:
    try:
        configured = _store(request).get(ANTHROPIC_KEY) is not None
        available = True
    except SecretStoreUnavailable:
        configured, available = False, False
    return {"configured": configured, "storage_available": available, "billing_mode": "api"}


@router.get("")
def status(request: Request) -> dict[str, object]:
    return _status(request)


@router.put("")
def save(body: KeyIn, request: Request) -> dict[str, object]:
    key = body.api_key.get_secret_value()
    check = _validator(request, body.live).validate(key)
    if not check.ok:
        raise HTTPException(422, check.message)
    try:
        _store(request).set(ANTHROPIC_KEY, key)
    except SecretStoreUnavailable as exc:
        raise HTTPException(503, "secure credential storage is unavailable") from exc
    log.info("Anthropic API key saved (%s)", "live-validated" if body.live else "mock-validated")
    return {**_status(request), "message": check.message}


@router.post("/test")
def test(body: TestIn, request: Request) -> dict[str, object]:
    try:
        key = _store(request).get(ANTHROPIC_KEY)
    except SecretStoreUnavailable as exc:
        raise HTTPException(503, "secure credential storage is unavailable") from exc
    if key is None:
        raise HTTPException(404, "no API key saved")
    check = _validator(request, body.live).validate(key)
    return {"ok": check.ok, "message": check.message, "live": body.live}


@router.delete("")
def remove(request: Request) -> dict[str, object]:
    try:
        _store(request).delete(ANTHROPIC_KEY)
    except SecretStoreUnavailable as exc:
        raise HTTPException(503, "secure credential storage is unavailable") from exc
    log.info("Anthropic API key removed")
    return _status(request)
