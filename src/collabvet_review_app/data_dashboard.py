"""Privacy-preserving inventory for the local clinical-data pipeline."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import current_app

from collabvet_review_app.models import ApprovedExport, CaseRecord
from collabvet_review_app.services import read_json, sha256_file, source_documents_root

CACHE_TTL_SECONDS = 300
REFRESH_COOLDOWN_SECONDS = 30
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

STAGES = {
    "all",
    "pii_removed",
    "process_ready",
    "longitudinal",
    "legacy_training",
    "new",
    "awaiting_review",
    "clinician_approved",
    "future_training",
    "broken",
    "ready_without_longitudinal",
    "approved_not_future",
}
SORTS = {
    "pseudonym",
    "-pseudonym",
    "status",
    "-status",
    "visit_count",
    "-visit_count",
    "next_action",
    "-next_action",
}

METRIC_DEFINITIONS = {
    "raw": "Raw inventory is deliberately not connected and is never scanned.",
    "pii_removed": "Patient folders present under the configured PII-removed source root.",
    "process_ready": (
        "PII-removed patients with history_form.json, a clinical-summary PDF, "
        "and a medfiles PDF."
    ),
    "longitudinal": "Readable schema-v2 longitudinal JSON records in the canonical cases folder.",
    "legacy_training": (
        "Cases listed in the v0.2 training manifest; this does not prove a model run."
    ),
    "new": "Longitudinal cases that are absent from the v0.2 training manifest.",
    "clinician_approved": (
        "Cases with a current-hash approved export in the local review application."
    ),
    "future_training": "Cases accepted by the clinician-approved training manifest.",
    "actually_trained": "Unavailable until a training-run ledger explicitly records model usage.",
}

_cache_lock = threading.RLock()
_cache: dict[tuple[str, ...], dict[str, Any]] = {}
_last_forced_refresh: dict[tuple[str, ...], float] = {}


class DashboardRefreshLimited(RuntimeError):
    def __init__(self, retry_after: int):
        super().__init__(f"Refresh is available again in {retry_after} seconds.")
        self.retry_after = retry_after


class DashboardPatientNotFound(KeyError):
    pass


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    return "-".join(
        part
        for part in "".join(
            char.lower() if char.isalnum() else "-" for char in value
        ).split("-")
        if part
    )


def _opaque_key(identity: str) -> str:
    secret = str(current_app.config["SECRET_KEY"]).encode("utf-8")
    return hmac.new(secret, identity.encode("utf-8"), hashlib.sha256).hexdigest()[:20]


def _cache_key() -> tuple[str, ...]:
    return (
        str(Path(current_app.config["CLINICAL_DATA_ROOT"]).resolve()),
        str(Path(current_app.config["SOURCE_ROOT"]).resolve()),
        str(Path(current_app.config["INPUT_ROOT"]).resolve()),
        str(Path(current_app.config["CONFIG_ROOT"]).resolve()),
        str(Path(current_app.config["OUTPUT_ROOT"]).resolve()),
        str(current_app.config["SQLALCHEMY_DATABASE_URI"]),
    )


def clear_dashboard_cache() -> None:
    """Clear process-local inventory state. Primarily useful for tests."""

    with _cache_lock:
        _cache.clear()
        _last_forced_refresh.clear()


def _safe_source_root() -> Path:
    return source_documents_root()


def _json_or_none(path: Path, warnings: list[str], label: str) -> dict[str, Any] | None:
    if not path.is_file():
        warnings.append(f"{label} is unavailable.")
        return None
    try:
        return read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        warnings.append(f"{label} is unreadable.")
        return None


def _git(checkout: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={checkout.as_posix()}", "-C", str(checkout), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return result.stdout.strip()


def _git_metadata(clinical_root: Path, warnings: list[str]) -> dict[str, Any]:
    try:
        commit = _git(clinical_root, "rev-parse", "HEAD")
        branch = _git(clinical_root, "branch", "--show-current") or "detached"
        # Deliberately scope status to non-raw paths. The dashboard never inventories raw/.
        status = _git(
            clinical_root,
            "status",
            "--porcelain",
            "--untracked-files=normal",
            "--",
            "source",
            "cases",
            "approved",
            "exports",
            "docs",
        )
        dirty = bool(status)
        if dirty:
            warnings.append("Clinical-data safe paths have uncommitted changes.")
        return {
            "commit": commit,
            "commit_short": commit[:8],
            "branch": branch,
            "dirty": dirty,
            "working_tree_scope": "source, cases, approved, exports, and docs; raw excluded",
        }
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        warnings.append("Clinical-data Git metadata is unavailable.")
        return {
            "commit": None,
            "commit_short": None,
            "branch": None,
            "dirty": None,
            "working_tree_scope": "raw excluded",
        }


def _source_inventory(warnings: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    source_root = _safe_source_root()
    if not source_root.is_dir():
        warnings.append("PII-removed source root is unavailable.")
        return {}, {"history": 0, "clinical_summary": 0, "medfiles": 0, "complete": 0}

    rows: dict[str, dict[str, Any]] = {}
    duplicate_count = 0
    for folder in source_root.iterdir():
        if not folder.is_dir():
            continue
        identity = _slug(folder.name)
        if not identity:
            continue
        if identity in rows:
            duplicate_count += 1
            rows[identity]["issues"].append("duplicate_source_identity")
            continue
        try:
            filenames = [item.name.lower() for item in folder.iterdir() if item.is_file()]
        except OSError:
            filenames = []
            issues = ["source_folder_unreadable"]
        else:
            issues = []
        has_history = "history_form.json" in filenames
        has_clinical = any(
            name.endswith(".pdf")
            and "history form" not in name
            and "medfiles" not in name
            for name in filenames
        )
        has_medfiles = any(name.endswith(".pdf") and "medfiles" in name for name in filenames)
        rows[identity] = {
            "identity": identity,
            "pseudonym": folder.name,
            "pii_removed": True,
            "source": {
                "history": has_history,
                "clinical_summary": has_clinical,
                "medfiles": has_medfiles,
            },
            "process_ready": has_history and has_clinical and has_medfiles,
            "issues": issues,
        }
    if duplicate_count:
        warnings.append(f"{duplicate_count} duplicate PII-removed source identity mapping(s).")
    readiness = {
        "history": sum(row["source"]["history"] for row in rows.values()),
        "clinical_summary": sum(row["source"]["clinical_summary"] for row in rows.values()),
        "medfiles": sum(row["source"]["medfiles"] for row in rows.values()),
        "complete": sum(row["process_ready"] for row in rows.values()),
    }
    return rows, readiness


def _case_inventory(warnings: list[str]) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    input_root = Path(current_app.config["INPUT_ROOT"]).resolve()
    cases: dict[str, dict[str, Any]] = {}
    problems: list[dict[str, str]] = []
    if not input_root.is_dir():
        warnings.append("Canonical longitudinal cases root is unavailable.")
        return cases, problems

    for path in sorted(input_root.glob("*.json")):
        if path.name == "twopass_completed.json":
            continue
        try:
            data = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            problems.append({"code": "unreadable_case", "label": path.stem})
            continue
        pseudonym = str(data.get("patient_folder") or "").strip()
        case_id = str(data.get("case_id") or path.stem).strip()
        identity = _slug(pseudonym or case_id)
        history_form = (
            data.get("history_form") if isinstance(data.get("history_form"), dict) else {}
        )
        source_identity = _slug(str(history_form.get("patient_folder") or ""))
        if not identity:
            problems.append({"code": "case_missing_identity", "label": path.stem})
            continue
        if identity in cases:
            problems.append({"code": "duplicate_case_identity", "label": pseudonym or case_id})
            cases[identity]["issues"].append("duplicate_case_identity")
            continue
        visits = data.get("visits") if isinstance(data.get("visits"), list) else []
        communications = (
            data.get("communications") if isinstance(data.get("communications"), list) else []
        )
        visit_dates = sorted(
            str(visit.get("date"))
            for visit in visits
            if isinstance(visit, dict) and visit.get("date")
        )
        source = data.get("source") if isinstance(data.get("source"), dict) else {}
        case_hash = sha256_file(path)
        cases[identity] = {
            "identity": identity,
            "source_identity": source_identity or identity,
            "pseudonym": pseudonym or case_id,
            "case_id": case_id,
            "longitudinal": True,
            "schema_version": data.get("schema_version"),
            "visit_count": len(visits),
            "communication_count": len(communications),
            "first_event_date": visit_dates[0] if visit_dates else None,
            "last_event_date": visit_dates[-1] if visit_dates else None,
            "extraction": {
                "medfiles_model": source.get("medfiles_model"),
                "clinical_summary_model": source.get("cs_model"),
                "medfiles_prompt": source.get("medfiles_prompt_version"),
                "clinical_summary_prompt": source.get("cs_prompt_version"),
            },
            "source_hash": case_hash,
            "issues": ["invalid_schema_version"] if data.get("schema_version") != 2 else [],
        }
    if problems:
        warnings.append(f"{len(problems)} longitudinal case inventory problem(s) detected.")
    return cases, problems


def _manifest_inventory(warnings: list[str]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    config_root = Path(current_app.config["CONFIG_ROOT"]).resolve()
    legacy = _json_or_none(
        config_root / "v02_train_manifest.json", warnings, "Legacy v0.2 training manifest"
    )
    approved = _json_or_none(
        config_root / "v04_approved_train_manifest.json",
        warnings,
        "Clinician-approved training manifest",
    )
    legacy_rows: dict[str, dict[str, Any]] = {}
    if legacy:
        for item in legacy.get("patients") or []:
            if not isinstance(item, dict):
                continue
            case_id = str(item.get("case_id") or "")
            pseudonym = str(item.get("patient_folder") or "")
            identity = _slug(pseudonym or case_id)
            if identity:
                legacy_rows[identity] = {
                    "case_id": case_id,
                    "pseudonym": pseudonym,
                    "split": str(item.get("split") or ""),
                }
    approved_ids = {
        _slug(str(item.get("case_id") or ""))
        for item in (approved or {}).get("patients") or []
        if isinstance(item, dict) and item.get("case_id")
    }
    return legacy_rows, approved_ids


def _review_inventory() -> tuple[dict[str, dict[str, Any]], set[str]]:
    records: dict[str, dict[str, Any]] = {}
    current_approved: set[str] = set()
    output_root = Path(current_app.config["OUTPUT_ROOT"]).resolve()
    latest_exports: dict[int, ApprovedExport] = {}
    for exported in ApprovedExport.query.order_by(ApprovedExport.approved_at.asc()).all():
        latest_exports[exported.case_id] = exported
    for record in CaseRecord.query.all():
        identity = _slug(record.patient_folder or record.case_id)
        exported = latest_exports.get(record.id)
        approved = False
        if exported and exported.source_hash == record.source_hash:
            approved_path = Path(exported.approved_path).resolve()
            review_path = Path(exported.review_path).resolve()
            approved = (
                approved_path.is_relative_to(output_root)
                and review_path.is_relative_to(output_root)
                and approved_path.is_file()
                and review_path.is_file()
            )
        if approved:
            current_approved.add(_slug(record.case_id))
        records[identity] = {
            "case_id": record.case_id,
            "status": record.status,
            "split": record.split,
            "holdout": record.is_holdout,
            "reviewer": record.assigned_user.display_name if record.assigned_user else None,
            "source_hash": record.source_hash,
            "clinician_approved": approved,
        }
    return records, current_approved


def _recommended_action(row: dict[str, Any]) -> str:
    issues = set(row["issues"])
    if issues:
        return "Resolve inventory mismatch"
    if row["future_training"]:
        return "Ready for future training"
    if row["clinician_approved"]:
        return "Rebuild approved training manifest"
    if row["longitudinal"] and row["review_status"] != "approved":
        return "Complete clinician review"
    if row["longitudinal"] and row["new"]:
        return "Review new longitudinal record"
    if row["process_ready"] and not row["longitudinal"]:
        return "Ready for longitudinal extraction"
    if row["pii_removed"] and not row["process_ready"]:
        return "Complete PII-removed source set"
    if row["legacy_training"] and not row["longitudinal"]:
        return "Repair training manifest"
    return "Inventory source data"


def _row_status(row: dict[str, Any]) -> str:
    if row["issues"]:
        return "broken"
    if row["future_training"]:
        return "future training"
    if row["clinician_approved"]:
        return "clinician approved"
    if row["new"]:
        return "new longitudinal"
    if row["legacy_training"]:
        return "legacy training corpus"
    if row["longitudinal"]:
        return "longitudinal"
    if row["process_ready"]:
        return "process ready"
    return "PII removed"


def _build_snapshot() -> dict[str, Any]:
    warnings: list[str] = []
    clinical_root = Path(current_app.config["CLINICAL_DATA_ROOT"]).resolve()
    sources, readiness = _source_inventory(warnings)
    cases, case_problems = _case_inventory(warnings)
    legacy, future_ids = _manifest_inventory(warnings)
    reviews, approved_ids = _review_inventory()

    # Older extractions may use a later repository pseudonym at the case level while
    # retaining the source-folder pseudonym in history_form.patient_folder.
    identity_aliases: dict[str, str] = {}
    joined_cases: dict[str, dict[str, Any]] = {}
    source_alias_counts: dict[str, int] = {}
    for case in cases.values():
        source_identity = str(case.get("source_identity") or case["identity"])
        source_alias_counts[source_identity] = source_alias_counts.get(source_identity, 0) + 1
    for identity, case in cases.items():
        source_identity = str(case.get("source_identity") or identity)
        ambiguous_source = source_alias_counts[source_identity] > 1
        joined_identity = (
            source_identity if source_identity in sources and not ambiguous_source else identity
        )
        if ambiguous_source:
            case["issues"].append("ambiguous_source_identity")
            if source_identity in sources:
                sources[source_identity]["issues"].append("ambiguous_source_identity")
        identity_aliases[identity] = joined_identity
        if joined_identity in joined_cases:
            joined_cases[joined_identity]["issues"].append("duplicate_case_identity")
            case_problems.append(
                {"code": "duplicate_case_identity", "label": case.get("pseudonym") or identity}
            )
        else:
            joined_cases[joined_identity] = case
    cases = joined_cases
    legacy = {identity_aliases.get(identity, identity): row for identity, row in legacy.items()}
    reviews = {identity_aliases.get(identity, identity): row for identity, row in reviews.items()}

    identities = set(sources) | set(cases) | set(legacy) | set(reviews)
    rows: list[dict[str, Any]] = []
    for identity in sorted(identities):
        source = sources.get(identity, {})
        case = cases.get(identity, {})
        manifest = legacy.get(identity, {})
        review = reviews.get(identity, {})
        case_id = str(
            case.get("case_id") or review.get("case_id") or manifest.get("case_id") or ""
        )
        issues = list(source.get("issues") or []) + list(case.get("issues") or [])
        if case and not source:
            issues.append("longitudinal_without_source")
        if manifest and not case:
            issues.append("manifest_without_longitudinal")
        if review and not case:
            issues.append("review_record_without_longitudinal")
        if review and case and review.get("source_hash") != case.get("source_hash"):
            issues.append("stale_review_source_hash")

        source_components = source.get(
            "source", {"history": False, "clinical_summary": False, "medfiles": False}
        )
        legacy_training = bool(manifest)
        longitudinal = bool(case)
        clinician_approved = _slug(case_id) in approved_ids
        row = {
            "key": _opaque_key(identity),
            "pseudonym": str(
                case.get("pseudonym")
                or source.get("pseudonym")
                or manifest.get("pseudonym")
                or identity.replace("-", " ").title()
            ),
            "case_id": case_id or None,
            "pii_removed": bool(source),
            "source": source_components,
            "missing_components": [
                label
                for key, label in (
                    ("history", "history form"),
                    ("clinical_summary", "clinical summary"),
                    ("medfiles", "medfiles"),
                )
                if not source_components.get(key)
            ],
            "process_ready": bool(source.get("process_ready")),
            "longitudinal": longitudinal,
            "schema_version": case.get("schema_version"),
            "visit_count": int(case.get("visit_count") or 0),
            "communication_count": int(case.get("communication_count") or 0),
            "first_event_date": case.get("first_event_date"),
            "last_event_date": case.get("last_event_date"),
            "extraction": case.get("extraction") or {},
            "review_status": review.get("status")
            or ("not indexed" if longitudinal else "not available"),
            "reviewer": review.get("reviewer"),
            "holdout": bool(review.get("holdout")),
            "legacy_training": legacy_training,
            "training_split": manifest.get("split") or review.get("split"),
            "new": longitudinal and not legacy_training,
            "clinician_approved": clinician_approved,
            "future_training": _slug(case_id) in future_ids,
            "issues": sorted(set(issues)),
        }
        row["status"] = _row_status(row)
        row["next_action"] = _recommended_action(row)
        rows.append(row)

    known_case_ids = {_slug(str(row.get("case_id") or "")) for row in rows if row.get("case_id")}
    missing_future = sorted(future_ids - known_case_ids)
    if missing_future:
        warnings.append(
            f"{len(missing_future)} approved-training manifest case(s) "
            "lack a current inventory row."
        )

    metrics = {
        "raw": None,
        "pii_removed": sum(row["pii_removed"] for row in rows),
        "process_ready": sum(row["process_ready"] for row in rows),
        "longitudinal": sum(row["longitudinal"] for row in rows),
        "legacy_training": sum(row["legacy_training"] and row["longitudinal"] for row in rows),
        "legacy_train_split": sum(
            row["legacy_training"] and row["training_split"] == "train" for row in rows
        ),
        "legacy_validation_split": sum(
            row["legacy_training"] and row["training_split"] == "val" for row in rows
        ),
        "new": sum(row["new"] for row in rows),
        "clinician_approved": sum(row["clinician_approved"] for row in rows),
        "future_training": sum(row["future_training"] for row in rows),
        "actually_trained": None,
    }
    opportunities = {
        "ready_without_longitudinal": sum(
            row["process_ready"] and not row["longitudinal"] for row in rows
        ),
        "new_longitudinal": metrics["new"],
        "awaiting_review": sum(
            row["longitudinal"]
            and not row["clinician_approved"]
            and row["review_status"] != "approved"
            for row in rows
        ),
        "approved_not_in_future_manifest": sum(
            row["clinician_approved"] and not row["future_training"] for row in rows
        ),
        "broken": sum(bool(row["issues"]) for row in rows) + len(case_problems),
    }
    denominator = metrics["pii_removed"]
    conversions = {
        "source_to_ready": round(100 * metrics["process_ready"] / denominator, 1)
        if denominator
        else None,
        "ready_to_longitudinal": round(
            100 * metrics["longitudinal"] / metrics["process_ready"], 1
        )
        if metrics["process_ready"]
        else None,
        "longitudinal_to_legacy": round(
            100 * metrics["legacy_training"] / metrics["longitudinal"], 1
        )
        if metrics["longitudinal"]
        else None,
        "longitudinal_to_approved": round(
            100 * metrics["clinician_approved"] / metrics["longitudinal"], 1
        )
        if metrics["longitudinal"]
        else None,
    }
    generated_at = _utc_iso()
    return {
        "generated_at": generated_at,
        "monotonic_created": time.monotonic(),
        "stale": False,
        "warnings": warnings,
        "meta": {
            "generated_at": generated_at,
            "cache_ttl_seconds": CACHE_TTL_SECONDS,
            "raw_inventory": "not_connected",
            "git": _git_metadata(clinical_root, warnings),
        },
        "overview": {
            "metrics": metrics,
            "readiness": readiness,
            "opportunities": opportunities,
            "conversions": conversions,
            "definitions": METRIC_DEFINITIONS,
        },
        "patients": rows,
        "patient_index": {row["key"]: row for row in rows},
    }


def dashboard_snapshot(*, force: bool = False) -> dict[str, Any]:
    key = _cache_key()
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if force:
            last = _last_forced_refresh.get(key)
            if last is not None and now - last < REFRESH_COOLDOWN_SECONDS:
                raise DashboardRefreshLimited(
                    max(1, int(REFRESH_COOLDOWN_SECONDS - (now - last)))
                )
            _last_forced_refresh[key] = now
        elif cached and now - cached["monotonic_created"] < CACHE_TTL_SECONDS:
            return cached
        try:
            snapshot = _build_snapshot()
        except Exception as exc:
            if cached:
                stale = copy.deepcopy(cached)
                stale["stale"] = True
                stale["warnings"] = list(stale["warnings"]) + [
                    f"Refresh failed; showing the previous snapshot ({type(exc).__name__})."
                ]
                return stale
            raise
        _cache[key] = snapshot
        return snapshot


def dashboard_overview(*, force: bool = False) -> dict[str, Any]:
    snapshot = dashboard_snapshot(force=force)
    created = snapshot["monotonic_created"]
    return {
        **snapshot["overview"],
        "meta": {
            **snapshot["meta"],
            "cache_age_seconds": max(0, int(time.monotonic() - created)),
            "stale": snapshot["stale"],
            "warnings": snapshot["warnings"],
        },
    }


def _matches_stage(row: dict[str, Any], stage: str) -> bool:
    return {
        "all": True,
        "pii_removed": row["pii_removed"],
        "process_ready": row["process_ready"],
        "longitudinal": row["longitudinal"],
        "legacy_training": row["legacy_training"] and row["longitudinal"],
        "new": row["new"],
        "awaiting_review": row["longitudinal"] and not row["clinician_approved"],
        "clinician_approved": row["clinician_approved"],
        "future_training": row["future_training"],
        "broken": bool(row["issues"]),
        "ready_without_longitudinal": row["process_ready"] and not row["longitudinal"],
        "approved_not_future": row["clinician_approved"] and not row["future_training"],
    }[stage]


def dashboard_patients(
    *,
    query: str = "",
    stage: str = "all",
    sort: str = "pseudonym",
    offset: int = 0,
    limit: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    stage = stage if stage in STAGES else "all"
    sort = sort if sort in SORTS else "pseudonym"
    offset = max(0, offset)
    limit = min(MAX_PAGE_SIZE, max(1, limit))
    rows = [
        row for row in dashboard_snapshot()["patients"] if _matches_stage(row, stage)
    ]
    term = query.strip().casefold()
    if term:
        rows = [
            row
            for row in rows
            if term
            in " ".join(
                [
                    row["pseudonym"],
                    str(row["case_id"] or ""),
                    row["status"],
                    row["review_status"],
                    row["training_split"] or "",
                    row["next_action"],
                    " ".join(row["issues"]),
                    " ".join(str(value or "") for value in row["extraction"].values()),
                ]
            ).casefold()
        ]
    reverse = sort.startswith("-")
    field = sort.removeprefix("-")
    rows.sort(
        key=lambda row: (
            row[field] if field == "visit_count" else str(row[field] or "").casefold(),
            row["pseudonym"].casefold(),
        ),
        reverse=reverse,
    )
    total = len(rows)
    items = rows[offset : offset + limit]
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "query": query,
        "stage": stage,
        "sort": sort,
    }


def dashboard_patient(key: str) -> dict[str, Any]:
    row = dashboard_snapshot()["patient_index"].get(key)
    if row is None:
        raise DashboardPatientNotFound(key)
    return {
        **row,
        "timeline": [
            {
                "stage": "PII removed",
                "status": "available" if row["pii_removed"] else "unavailable",
                "detail": "Safe source metadata only; raw inventory is not connected.",
            },
            {
                "stage": "Process ready",
                "status": "ready" if row["process_ready"] else "incomplete",
                "detail": (
                    "All required components are present."
                    if row["process_ready"]
                    else "Missing: " + ", ".join(row["missing_components"])
                ),
            },
            {
                "stage": "Longitudinal",
                "status": "available" if row["longitudinal"] else "not available",
                "detail": (
                    f"Schema {row['schema_version']}; {row['visit_count']} visits; "
                    f"{row['communication_count']} communications."
                    if row["longitudinal"]
                    else "No longitudinal schema-v2 record."
                ),
            },
            {
                "stage": "Training corpus",
                "status": "included" if row["legacy_training"] else "not included",
                "detail": (
                    f"v0.2 {row['training_split'] or 'unspecified'} split; corpus membership "
                    "does not prove a model run."
                    if row["legacy_training"]
                    else "Not present in the v0.2 training manifest."
                ),
            },
            {
                "stage": "Clinician approval",
                "status": "approved" if row["clinician_approved"] else row["review_status"],
                "detail": (
                    "Current source hash has an approved export."
                    if row["clinician_approved"]
                    else "No current-hash approved export."
                ),
            },
            {
                "stage": "Future training",
                "status": "eligible" if row["future_training"] else "not included",
                "detail": (
                    "Accepted by the approved-only training manifest."
                    if row["future_training"]
                    else "Not accepted by the approved-only training manifest."
                ),
            },
        ],
    }
