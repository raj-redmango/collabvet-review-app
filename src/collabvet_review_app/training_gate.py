"""Approved-only gate for future corpus construction."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from collabvet_review_app.services import (
    normalize_for_review,
    read_json,
    scrub_findings,
    sha256_file,
    validate_longitudinal,
)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _holdout_keys(project_root: Path) -> set[str]:
    keys: set[str] = set()
    ids = project_root / "config" / "holdout_golden.json"
    if ids.exists():
        keys.update(_slug(str(item)) for item in read_json(ids).get("case_ids", []))
    mappings = project_root / "config" / "golden_regression_cases.json"
    if mappings.exists():
        for item in read_json(mappings).get("cases", []):
            keys.add(_slug(str(item.get("case_id") or "")))
            keys.add(_slug(str(item.get("patient_folder") or "")))
            keys.add(_slug(str(item.get("extraction_case_id") or "")))
    return keys


def _current_sources(input_root: Path) -> dict[str, tuple[Path, str]]:
    result: dict[str, tuple[Path, str]] = {}
    registry_path = input_root / "twopass_completed.json"
    candidates: list[tuple[str, Path]] = []
    if registry_path.exists():
        for patient, entry in (read_json(registry_path).get("patients") or {}).items():
            if entry.get("longitudinal_file"):
                candidates.append((patient, input_root / entry["longitudinal_file"]))
    else:
        candidates.extend(("", path) for path in sorted(input_root.glob("*.json")))
    for patient, path in candidates:
        if not path.is_file():
            continue
        data = read_json(path)
        patient = str(patient or data.get("patient_folder") or "")
        case_id = str(data.get("case_id") or _slug(patient or path.stem))
        value = (path.resolve(), sha256_file(path))
        result[_slug(case_id)] = value
        if patient:
            result[_slug(patient)] = value
    return result


def build_approved_manifest(
    *,
    project_root: Path,
    input_root: Path,
    review_output_root: Path,
    destination: Path,
) -> dict[str, Any]:
    """Build a manifest only from current, approved, non-holdout records."""

    project_root = project_root.resolve()
    input_root = input_root.resolve()
    review_output_root = review_output_root.resolve()
    index_path = review_output_root / "approved_cases_index.json"
    index = read_json(index_path)
    holdouts = _holdout_keys(project_root)
    sources = _current_sources(input_root)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []

    for row in index.get("cases", []):
        case_id = str(row.get("case_id") or "")
        reasons: list[str] = []
        explicit_holdout = (
            bool(row.get("holdout"))
            or _slug(case_id) in holdouts
            or _slug(str(row.get("patient_folder") or "")) in holdouts
        )
        if explicit_holdout:
            reasons.append("golden holdout")
        if row.get("review_status") != "approved":
            reasons.append("review status is not approved")
        if row.get("training_eligible") is not True:
            reasons.append("not marked training eligible")
        current = sources.get(_slug(case_id)) or sources.get(
            _slug(str(row.get("patient_folder") or ""))
        )
        if current is None:
            reasons.append("current immutable source not found")
        elif current[1] != row.get("source_hash"):
            reasons.append("source hash is stale")
        approved_path = (review_output_root / str(row.get("approved_file") or "")).resolve()
        if not approved_path.is_relative_to(review_output_root) or not approved_path.is_file():
            reasons.append("approved snapshot path is invalid")
        data: dict[str, Any] | None = None
        if not reasons:
            try:
                data = read_json(approved_path)
                errors = [
                    item
                    for item in validate_longitudinal(normalize_for_review(data))
                    if item["severity"] == "error"
                ]
                if errors:
                    reasons.append("approved snapshot fails schema validation")
                if scrub_findings(data):
                    reasons.append("approved snapshot fails PII/path scrub check")
            except (OSError, ValueError, json.JSONDecodeError):
                reasons.append("approved snapshot is unreadable")
        if reasons:
            rejected.append({"case_id": case_id, "reason": "; ".join(sorted(set(reasons)))})
            continue
        accepted.append(
            {
                "case_id": case_id,
                "patient_folder": row.get("patient_folder"),
                "split": row.get("split") or "train",
                "source_hash": row["source_hash"],
                "schema_version": row.get("schema_version", 2),
                "approved_file": str(approved_path),
            }
        )

    manifest = {
        "version": 1,
        "policy": "clinician-approved, current-source-hash, schema-valid, scrubbed, non-holdout only",
        "source_index": str(index_path),
        "case_count": len(accepted),
        "rejected_count": len(rejected),
        "holdout_excluded": True,
        "patients": sorted(accepted, key=lambda row: row["case_id"]),
        "rejected": sorted(rejected, key=lambda row: row["case_id"]),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest

