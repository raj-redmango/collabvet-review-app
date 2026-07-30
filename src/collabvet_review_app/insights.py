"""Server-side client for Sachin's authenticated Clinical Insights API."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from flask import current_app

REVIEW_INSIGHTS_PREFIX = "/api/v1/review/clinical-insights/"
TOKEN_PATH = "/api/v1/review/auth/token"
TOKEN_CACHE_SECONDS = 14 * 60


class InsightsAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        status: int = 502,
        *,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class _TokenEntry:
    token: str
    expires_at: float


_token_lock = threading.Lock()
_token_cache: dict[tuple[str, str], _TokenEntry] = {}


def clear_token_cache() -> None:
    """Clear process-local service tokens, primarily for tests and credential rotation."""

    with _token_lock:
        _token_cache.clear()


def _service_config() -> tuple[str, str, str]:
    base_url = str(current_app.config.get("COLLABVET_API_BASE_URL") or "").rstrip("/")
    client_id = str(current_app.config.get("COLLABVET_REVIEW_CLIENT_ID") or "")
    client_secret = str(current_app.config.get("COLLABVET_REVIEW_CLIENT_SECRET") or "")
    if not base_url or not client_id or not client_secret:
        raise InsightsAPIError(
            "Clinical Insights service credentials are not configured.",
            503,
        )
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise InsightsAPIError("Clinical Insights API base URL is invalid.", 503)
    return base_url, client_id, client_secret


def _request_json(
    base_url: str,
    path: str,
    *,
    token: str | None = None,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    payload = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=payload,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail")
        except (ValueError, AttributeError):
            detail = None
        if exc.code == 401:
            raise InsightsAPIError("Clinical Insights API authorization failed.", 401) from exc
        if exc.code == 403:
            raise InsightsAPIError(
                "The API denied this tenant, VB, or role scope.",
                403,
            ) from exc
        if exc.code == 429:
            raise InsightsAPIError(
                str(detail or "Clinical Insights API rate limit exceeded."),
                429,
                retry_after=exc.headers.get("Retry-After"),
            ) from exc
        raise InsightsAPIError(
            str(detail or f"Clinical Insights API returned {exc.code}.")
        ) from exc
    except (OSError, TimeoutError, json.JSONDecodeError) as exc:
        raise InsightsAPIError("Clinical Insights API is unavailable.") from exc
    if not isinstance(data, dict):
        raise InsightsAPIError("Clinical Insights API returned an invalid response.")
    return data


def _token_key(base_url: str, client_id: str) -> tuple[str, str]:
    return base_url, client_id


def _service_token(base_url: str, client_id: str, client_secret: str) -> str:
    key = _token_key(base_url, client_id)
    now = time.monotonic()
    with _token_lock:
        cached = _token_cache.get(key)
        if cached and cached.expires_at > now:
            return cached.token
        try:
            result = _request_json(
                base_url,
                TOKEN_PATH,
                method="POST",
                body={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "clinical_insights:read",
                },
            )
        except InsightsAPIError as exc:
            if exc.status == 401:
                raise InsightsAPIError(
                    "Clinical Insights service credentials were rejected.",
                    503,
                ) from exc
            raise
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            raise InsightsAPIError(
                "Clinical Insights token endpoint returned an invalid response.",
                503,
            )
        _token_cache[key] = _TokenEntry(
            token=token,
            expires_at=time.monotonic() + TOKEN_CACHE_SECONDS,
        )
        return token


def _invalidate_token(base_url: str, client_id: str, token: str) -> None:
    key = _token_key(base_url, client_id)
    with _token_lock:
        cached = _token_cache.get(key)
        if cached and cached.token == token:
            _token_cache.pop(key, None)


def clinical_insights_request(
    path: str,
    query: dict[str, str],
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not path.startswith(REVIEW_INSIGHTS_PREFIX):
        raise InsightsAPIError("Clinical Insights upstream path is not allowed.", 500)
    if method not in {"GET", "POST"}:
        raise InsightsAPIError("Clinical Insights upstream method is not allowed.", 500)
    base_url, client_id, client_secret = _service_config()
    suffix = urllib.parse.urlencode(query)
    request_path = path + (f"?{suffix}" if suffix else "")
    token = _service_token(base_url, client_id, client_secret)
    try:
        return _request_json(
            base_url,
            request_path,
            token=token,
            method=method,
            body=body,
        )
    except InsightsAPIError as exc:
        if exc.status != 401:
            raise
    _invalidate_token(base_url, client_id, token)
    replacement = _service_token(base_url, client_id, client_secret)
    try:
        return _request_json(
            base_url,
            request_path,
            token=replacement,
            method=method,
            body=body,
        )
    except InsightsAPIError as exc:
        if exc.status == 401:
            _invalidate_token(base_url, client_id, replacement)
            raise InsightsAPIError(
                "Clinical Insights authorization failed after token refresh.",
                503,
            ) from exc
        raise
