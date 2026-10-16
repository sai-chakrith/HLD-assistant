"""Optional local access tokens. User names and roles still come from the project database."""
from __future__ import annotations

import hashlib
import hmac
import json
import os

from fastapi import HTTPException, Request

COOKIE = "hld_access"


def token_hashes() -> dict[str, str] | None:
    raw = os.environ.get("HLD_AUTH_TOKEN_HASHES")
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or not parsed or any(
            not isinstance(k, str) or not isinstance(v, str) or len(v) != 64
            or any(c not in "0123456789abcdef" for c in v) for k, v in parsed.items()
        ):
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise HTTPException(503, "Access token configuration is invalid; access is disabled.") from None


def enabled() -> bool:
    return token_hashes() is not None


def identify(request: Request, demo_user: str | None = None) -> str:
    configured = token_hashes()
    if configured is None:
        if not demo_user:
            raise HTTPException(401, "missing X-User header")
        return demo_user
    header = request.headers.get("authorization", "")
    if header and not header.startswith("Bearer "):
        raise HTTPException(401, "Use a bearer access token.")
    token = header[7:] if header else request.cookies.get(COOKIE, "")
    if not token or len(token) > 4096:
        raise HTTPException(401, "Sign in with an access token.")
    digest = hashlib.sha256(token.encode()).hexdigest()
    for user, expected in configured.items():
        if hmac.compare_digest(digest, expected):
            return user
    raise HTTPException(401, "Access token not recognised.")
