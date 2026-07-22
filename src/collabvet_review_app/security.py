"""Cloudflare Access validation and response hardening."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from functools import lru_cache

import jwt
from flask import abort, current_app, g, request

_attempts: dict[str, deque[float]] = defaultdict(deque)


@lru_cache(maxsize=4)
def _jwk_client(certs_url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(certs_url, cache_keys=True)


def validate_cloudflare_request() -> None:
    """Require and verify Cloudflare Access identity when tunnel mode is enabled."""

    g.cf_identity = None
    if not current_app.config.get("CF_ACCESS_REQUIRED"):
        g.cf_identity = "local"
        return
    token = request.headers.get("Cf-Access-Jwt-Assertion", "")
    if not token:
        abort(403, "Cloudflare Access assertion required")
    team_domain = current_app.config["CF_ACCESS_TEAM_DOMAIN"].rstrip("/")
    audience = current_app.config["CF_ACCESS_AUDIENCE"]
    try:
        key = _jwk_client(f"{team_domain}/cdn-cgi/access/certs").get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=team_domain,
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except jwt.PyJWTError:
        abort(403, "Invalid Cloudflare Access assertion")
    email = str(claims.get("email") or claims.get("sub") or "").strip().lower()
    allowed = current_app.config.get("CF_ALLOWED_EMAILS") or set()
    if not email or (allowed and email not in allowed):
        abort(403, "Cloudflare identity is not allowed")
    g.cf_identity = email


def rate_limit(key: str, *, limit: int, window_seconds: int) -> bool:
    """Return False when a process-local rate limit is exceeded."""

    now = time.monotonic()
    bucket = _attempts[key]
    while bucket and bucket[0] <= now - window_seconds:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def apply_security_headers(response):
    response.headers["Cache-Control"] = "no-store, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; frame-src 'self'; object-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'self'"
    )
    return response

