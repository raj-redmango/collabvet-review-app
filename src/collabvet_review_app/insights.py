"""Server-side client for Sachin's authenticated Clinical Insights API."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from flask import current_app


class InsightsAPIError(RuntimeError):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


_token_lock = threading.Lock()
_token_cache: dict[tuple[str, str], str] = {}


def staging_environment() -> dict[str, str]:
    path = Path(current_app.config["STAGING_API_ENV_FILE"]).resolve()
    if not path.is_file():
        raise InsightsAPIError("Staging API configuration file is missing.", 503)
    return {key: str(value) for key, value in dotenv_values(path).items() if value}


def discover_api_base_url(values: dict[str, str]) -> str:
    """Derive the API origin from configured CORS origins, excluding the frontend."""

    frontend = values.get("FRONTEND_URL", "").rstrip("/").lower()
    origins = {
        item.strip().rstrip("/")
        for item in values.get("CORS_ORIGINS", "").split(",")
        if item.strip().startswith(("https://", "http://"))
    }
    candidates = sorted(origin for origin in origins if origin.lower() != frontend)
    if len(candidates) != 1:
        raise InsightsAPIError(
            "The staging API base URL is not unambiguous in CORS_ORIGINS.", 503
        )
    return candidates[0]


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
        f"{base_url}{path}", data=payload, headers=headers, method=method
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
                "The API denied this tenant, VB, or role scope.", 403
            ) from exc
        raise InsightsAPIError(str(detail or f"Clinical Insights API returned {exc.code}.")) from exc
    except (OSError, TimeoutError, json.JSONDecodeError) as exc:
        raise InsightsAPIError("Clinical Insights API is unavailable.") from exc
    if not isinstance(data, dict):
        raise InsightsAPIError("Clinical Insights API returned an invalid response.")
    return data


def _app_admin_token(values: dict[str, str], base_url: str) -> str:
    configured = str(current_app.config.get("CLINICAL_INSIGHTS_API_TOKEN") or "")
    if configured:
        return configured
    username = values.get("APP_ADMIN_USERNAME", "")
    password = values.get("APP_ADMIN_PASSWORD", "")
    if not username or not password:
        raise InsightsAPIError(
            "Clinical Insights requires an API-issued bearer token. The supplied "
            "configuration contains only an App Admin password hash, which cannot be "
            "used for API authentication.",
            503,
        )
    key = (base_url, username)
    with _token_lock:
        if key not in _token_cache:
            result = _request_json(
                base_url,
                "/api/v1/app-admin/auth/login/password",
                method="POST",
                body={"username": username, "password": password},
            )
            token = result.get("access_token")
            if not token:
                if result.get("requires_mfa") or result.get("preauth_token"):
                    raise InsightsAPIError(
                        "App Admin MFA cannot be completed by the review application.", 503
                    )
                raise InsightsAPIError("App Admin login did not return an access token.", 503)
            _token_cache[key] = str(token)
        return _token_cache[key]


def clinical_insights_request(path: str, query: dict[str, str]) -> dict[str, Any]:
    values = staging_environment()
    base_url = discover_api_base_url(values)
    token = _app_admin_token(values, base_url)
    suffix = urllib.parse.urlencode(query)
    return _request_json(base_url, path + (f"?{suffix}" if suffix else ""), token=token)

