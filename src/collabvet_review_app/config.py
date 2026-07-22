"""Configuration for the local clinician review application."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

DATA_ROOT = PROJECT_ROOT / "data"


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv_env(name: str) -> set[str]:
    return {
        item.strip().lower()
        for item in os.environ.get(name, "").split(",")
        if item.strip()
    }


class ReviewConfig:
    """Secure local defaults; override with REVIEW_* environment variables."""

    SECRET_KEY = os.environ.get("REVIEW_SECRET_KEY", "")
    INPUT_ROOT = Path(
        os.environ.get(
            "REVIEW_INPUT_ROOT",
            DATA_ROOT / "review_inputs",
        )
    ).resolve()
    SOURCE_ROOT = Path(
        os.environ.get("REVIEW_SOURCE_ROOT", DATA_ROOT / "source_documents")
    ).resolve()
    OUTPUT_ROOT = Path(
        os.environ.get("REVIEW_OUTPUT_ROOT", DATA_ROOT / "review_app" / "approved")
    ).resolve()
    TEACHER_ROOT = Path(
        os.environ.get("REVIEW_TEACHER_ROOT", DATA_ROOT / "teacher")
    ).resolve()
    INSTANCE_ROOT = Path(
        os.environ.get("REVIEW_INSTANCE_ROOT", DATA_ROOT / "review_app")
    ).resolve()
    CONFIG_ROOT = Path(
        os.environ.get("REVIEW_CONFIG_ROOT", PROJECT_ROOT / "config")
    ).resolve()
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "REVIEW_DATABASE_URL",
        f"sqlite:///{(INSTANCE_ROOT / 'review.sqlite3').as_posix()}",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool_env("REVIEW_SECURE_COOKIES", False)
    PERMANENT_SESSION_LIFETIME = 60 * 60
    MAX_CONTENT_LENGTH = 2 * 1024 * 1024
    WTF_CSRF_TIME_LIMIT = 60 * 60
    CF_ACCESS_REQUIRED = _bool_env("REVIEW_CF_ACCESS_REQUIRED", False)
    CF_ACCESS_TEAM_DOMAIN = os.environ.get("REVIEW_CF_ACCESS_TEAM_DOMAIN", "").rstrip("/")
    CF_ACCESS_AUDIENCE = os.environ.get("REVIEW_CF_ACCESS_AUDIENCE", "")
    CF_ALLOWED_EMAILS = _csv_env("REVIEW_CF_ALLOWED_EMAILS")
    TRUSTED_HOSTS = [
        host.strip()
        for host in os.environ.get("REVIEW_TRUSTED_HOSTS", "127.0.0.1,localhost").split(",")
        if host.strip()
    ]
    BIND_HOST = os.environ.get("REVIEW_BIND_HOST", "127.0.0.1")
    ALLOW_LAN_BIND = _bool_env("REVIEW_ALLOW_LAN_BIND", False)
    PORT = int(os.environ.get("REVIEW_PORT", "5055"))
    TESTING = False


def validate_config(config: dict) -> None:
    """Reject unsafe or incomplete production-facing configuration."""

    if not config.get("SECRET_KEY"):
        if config.get("TESTING"):
            return
        raise RuntimeError("REVIEW_SECRET_KEY is required; generate a random 32+ byte value")
    if config.get("CF_ACCESS_REQUIRED"):
        missing = [
            name
            for name in ("CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUDIENCE")
            if not config.get(name)
        ]
        if missing:
            raise RuntimeError(f"Cloudflare Access enabled but missing: {', '.join(missing)}")

