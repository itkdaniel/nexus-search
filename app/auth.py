"""
HMAC-SHA256 JWT authentication for nexus-search admin endpoints.

Identical scheme to the main NexusConsult portfolio:
  Header.Payload.Signature — base64url-encoded, no padding
  Payload: {sub, role, exp}
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = structlog.get_logger(__name__)

_settings = None
_bearer = HTTPBearer(auto_error=False)


def configure_auth(settings) -> None:
    """Called once at startup with the injected Settings instance."""
    global _settings
    _settings = settings


def _b64decode(s: str) -> bytes:
    """Base64url decode without padding."""
    pad = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (pad % 4))


def verify_token(token: str) -> dict:
    """
    Verify a nexus-style HMAC-SHA256 JWT.
    Returns payload dict on success; raises ValueError on failure.
    """
    if not _settings:
        raise ValueError("Auth not configured")

    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Malformed token")
        header_b64, payload_b64, sig_b64 = parts

        expected_sig = hmac.new(
            _settings.jwt_secret.encode(),
            f"{header_b64}.{payload_b64}".encode(),
            hashlib.sha256,
        ).digest()
        expected_b64 = base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode()

        if not hmac.compare_digest(sig_b64, expected_b64):
            raise ValueError("Invalid signature")

        payload = json.loads(_b64decode(payload_b64))
        if payload.get("exp", 0) < int(time.time()):
            raise ValueError("Token expired")

        return payload
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Token parse error: {exc}") from exc


async def require_admin(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    """
    FastAPI dependency: verify JWT and assert role == 'admin'.
    Raises 401/403 on failure.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = verify_token(credentials.credentials)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    if payload.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return payload
