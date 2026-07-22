"""Case indexing, validation, revision, approval, and document services."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jsonpatch
from flask import current_app, g, has_request_context, request
from sqlalchemy import func

from collabvet_review_app.normalization import _normalize_assessment, normalize_cs_visits
from collabvet_review_app.models import (
    ApprovedExport,
    AuditEvent,
    CaseRecord,
    Revision,
    SectionReview,
    User,
    db,
)
from collabvet_review_app.training.replay import StageExample, replay_case

ALLOWED_SECTION_STATUSES = {"pending", "approved", "needs_changes", "not_applicable"}
ALLOWED_CASE_STATUSES = {
    "pending",
    "in_review",
    "needs_changes",
    "approved",
    "rejected",
    "retired",
}
VISIT_TYPES = {"intake", "recheck", "phone_consult", "v2v_consult", "other"}
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
ABSOLUTE_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/(?:Users|home|var|tmp)/)")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return data


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _canonical_git_remote(value: str) -> str:
    remote = value.strip().replace("\\", "/").lower()
    if remote.startswith("git@github.com:"):
        remote = "https://github.com/" + remote.removeprefix("git@github.com:")
    remote = remote.rstrip("/")
    if remote.endswith(".git"):
        remote = remote[:-4]
    return remote


def _git(checkout: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={checkout.as_posix()}",
                "-C",
                str(checkout),
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Git is required to verify the clinical-data checkout") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "Git command failed").strip()
        raise RuntimeError(f"Cannot verify clinical-data checkout: {detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Timed out while verifying the clinical-data checkout") from exc
    return result.stdout.strip()


def verify_clinical_data_checkout() -> str:
    """Verify that INPUT_ROOT is cases/ in the approved external Git checkout."""

    if current_app.config.get("TESTING"):
        return "testing"
    input_root = Path(current_app.config["INPUT_ROOT"]).resolve()
    checkout = Path(current_app.config["CLINICAL_DATA_ROOT"]).resolve()
    expected_input = (checkout / "cases").resolve()
    if input_root != expected_input:
        raise RuntimeError("Case input must be the cases/ directory in REVIEW_CLINICAL_DATA_ROOT")
    if not input_root.is_dir():
        raise RuntimeError(
            f"Clinical case directory not found: {input_root}. Clone the private "
            "collabvet-clinical-data repository and set REVIEW_CLINICAL_DATA_ROOT."
        )
    top_level = Path(_git(checkout, "rev-parse", "--show-toplevel")).resolve()
    if top_level != checkout:
        raise RuntimeError("REVIEW_CLINICAL_DATA_ROOT is not the Git checkout root")
    expected_remote = _canonical_git_remote(current_app.config["CLINICAL_DATA_REPOSITORY"])
    actual_remote = _canonical_git_remote(_git(checkout, "remote", "get-url", "origin"))
    if actual_remote != expected_remote:
        raise RuntimeError(
            "Clinical-data origin must be "
            f"{current_app.config['CLINICAL_DATA_REPOSITORY']}; found {actual_remote}"
        )
    branch = _git(checkout, "branch", "--show-current")
    if branch != "main":
        raise RuntimeError(f"Clinical-data checkout must be on main; found {branch or 'detached HEAD'}")
    if _git(checkout, "status", "--porcelain", "--", "cases"):
        raise RuntimeError("Clinical-data cases/ has uncommitted changes")
    return _git(checkout, "rev-parse", "HEAD")


def load_holdouts() -> tuple[set[str], set[str]]:
    config_root = Path(current_app.config["CONFIG_ROOT"]).resolve()
    ids_path = config_root / "holdout_golden.json"
    cases_path = config_root / "golden_regression_cases.json"
    ids: set[str] = set()
    folders: set[str] = set()
    if ids_path.exists():
        ids = {_slug(str(item)) for item in read_json(ids_path).get("case_ids", [])}
    if cases_path.exists():
        folders = {
            str(item.get("patient_folder") or "").strip().lower()
            for item in read_json(cases_path).get("cases", [])
            if item.get("patient_folder")
        }
    return ids, folders


def _manifest_splits() -> dict[str, str]:
    path = Path(current_app.config["CONFIG_ROOT"]).resolve() / "v02_train_manifest.json"
    if not path.exists():
        return {}
    return {
        str(row.get("patient_folder")): str(row.get("split") or "")
        for row in read_json(path).get("patients", [])
    }


def index_cases() -> dict[str, int | str]:
    """Upsert all completed extractions into the review queue."""

    source_revision = verify_clinical_data_checkout()
    input_root = Path(current_app.config["INPUT_ROOT"]).resolve()
    registry = input_root / "twopass_completed.json"
    rows: list[tuple[str, Path]] = []
    if registry.exists():
        for patient, entry in (read_json(registry).get("patients") or {}).items():
            name = entry.get("longitudinal_file")
            if name:
                rows.append((patient, input_root / name))
    else:
        rows = [("", path) for path in sorted(input_root.glob("*.json"))]

    holdout_ids, holdout_folders = load_holdouts()
    splits = _manifest_splits()
    created = updated = stale = skipped = retired = 0
    indexed_case_ids: set[str] = set()
    for patient, path in rows:
        path = path.resolve()
        if not path.is_file() or not path.is_relative_to(input_root):
            skipped += 1
            continue
        try:
            data = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            skipped += 1
            continue
        patient = str(
            patient
            or data.get("patient_folder")
            or path.stem.removeprefix("v02-").replace("-", " ").title()
        )
        case_id = str(data.get("case_id") or _slug(patient))
        indexed_case_ids.add(case_id)
        source_hash = sha256_file(path)
        is_holdout = (
            _slug(case_id) in holdout_ids
            or _slug(patient) in holdout_ids
            or patient.lower() in holdout_folders
        )
        record = CaseRecord.query.filter_by(case_id=case_id).one_or_none()
        if record is None:
            record = CaseRecord(
                case_id=case_id,
                patient_folder=patient,
                source_path=str(path),
                source_hash=source_hash,
                split=splits.get(patient),
                is_holdout=is_holdout,
                visit_count=len(data.get("visits") or []),
            )
            db.session.add(record)
            created += 1
        else:
            if record.status == "retired":
                record.status = "pending"
            if record.source_hash != source_hash:
                record.source_hash = source_hash
                record.current_revision = (
                    db.session.query(func.max(Revision.revision_number))
                    .filter(Revision.case_id == record.id)
                    .scalar()
                    or 0
                )
                record.status = "needs_changes"
                SectionReview.query.filter_by(case_id=record.id).update(
                    {"status": "pending", "reviewed_at": None, "reviewed_by_id": None}
                )
                stale += 1
            record.source_path = str(path)
            record.patient_folder = patient
            record.split = splits.get(patient)
            record.is_holdout = is_holdout
            record.visit_count = len(data.get("visits") or [])
            updated += 1
    for record in CaseRecord.query.filter(CaseRecord.case_id.notin_(indexed_case_ids)).all():
        if record.status != "retired":
            record.status = "retired"
            retired += 1
    db.session.commit()
    return {
        "source_revision": source_revision,
        "created": created,
        "updated": updated,
        "stale": stale,
        "retired": retired,
        "skipped": skipped,
    }


def source_case(record: CaseRecord) -> dict[str, Any]:
    input_root = Path(current_app.config["INPUT_ROOT"]).resolve()
    path = Path(record.source_path).resolve()
    if not path.is_file() or not path.is_relative_to(input_root):
        raise FileNotFoundError("Case source is outside the configured input root")
    if sha256_file(path) != record.source_hash:
        raise RuntimeError("Case source changed; re-index before review")
    return read_json(path)


def revisions_for(record: CaseRecord) -> list[Revision]:
    return (
        Revision.query.filter_by(case_id=record.id, source_hash=record.source_hash)
        .order_by(Revision.revision_number.asc())
        .all()
    )


def materialize_case(record: CaseRecord) -> dict[str, Any]:
    data = source_case(record)
    for revision in revisions_for(record):
        data = jsonpatch.apply_patch(data, json.loads(revision.patch_json), in_place=False)
    return data


def stage_examples(record: CaseRecord) -> tuple[dict[str, list[StageExample]], str | None]:
    """Build exact stage-conditioned examples and hash the Stage A teacher artifact."""

    data = materialize_case(record)
    data["patient_folder"] = record.patient_folder
    teacher_root = Path(current_app.config["TEACHER_ROOT"]).resolve()
    gap_path = (
        teacher_root
        / "teacher"
        / "gap"
        / f"{record.patient_folder.replace(' ', '_')}_gap.json"
    ).resolve()
    gap: dict[str, Any] | None = None
    gap_hash: str | None = None
    if gap_path.is_file() and gap_path.is_relative_to(teacher_root):
        gap = read_json(gap_path)
        gap_hash = sha256_file(gap_path)
    examples = replay_case(
        data,
        split=record.split or "review",
        gap_analysis=gap,
        max_case_tokens=6000,
    )
    grouped: dict[str, list[StageExample]] = {"A": [], "B": [], "C": [], "D": []}
    for example in examples:
        grouped[example.stage].append(example)
    return grouped, gap_hash


def required_stage_keys(record: CaseRecord) -> list[str]:
    examples, _ = stage_examples(record)
    keys = ["stage:A", "stage:B", "stage:C"]
    if examples["D"]:
        keys.append("stage:D")
    return keys


def normalize_for_review(data: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(data)
    out["visits"] = normalize_cs_visits(list(out.get("visits") or []))
    out["assessment"] = _normalize_assessment(out.get("assessment"))
    out.setdefault("communications", [])
    out.setdefault("medical_record_supplements", [])
    for visit in out["visits"]:
        if visit.get("visit_type") in {"phone_consult", "v2v_consult"}:
            visit.setdefault("objective", "")
    for communication in out["communications"]:
        if not communication.get("party"):
            candidate = str(
                communication.get("from_party")
                or communication.get("to_party")
                or "other"
            ).lower()
            if any(value in candidate for value in ("owner", "client", "caregiver")):
                communication["party"] = "owner"
            elif any(value in candidate for value in ("gp", "vet", "veterinar")):
                communication["party"] = "gp_vet"
            else:
                communication["party"] = "other"
    return out


def validate_longitudinal(data: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []

    def issue(severity: str, path: str, message: str) -> None:
        issues.append({"severity": severity, "path": path, "message": message})

    if data.get("schema_version") != 2:
        issue("error", "/schema_version", "schema_version must be 2")
    if not isinstance(data.get("signalment"), dict):
        issue("error", "/signalment", "signalment must be an object")
    if not isinstance(data.get("history_form"), dict):
        issue("error", "/history_form", "history_form must be an object")
    if not isinstance(data.get("pre_intake_medical_digest"), dict):
        issue("error", "/pre_intake_medical_digest", "medical digest must be an object")

    visits = data.get("visits")
    if not isinstance(visits, list) or not visits:
        issue("error", "/visits", "visits must be a non-empty array")
        return issues

    previous: date | None = None
    first_date: date | None = None
    last_date: date | None = None
    intake_indexes: list[int] = []
    visit_ids: set[str] = set()
    for index, visit in enumerate(visits):
        base = f"/visits/{index}"
        if not isinstance(visit, dict):
            issue("error", base, "visit must be an object")
            continue
        visit_type = visit.get("visit_type")
        if visit_type not in VISIT_TYPES:
            issue("error", f"{base}/visit_type", f"invalid visit type: {visit_type!r}")
        if visit_type == "intake":
            intake_indexes.append(index)
        visit_id = str(visit.get("visit_id") or "")
        if not visit_id:
            issue("error", f"{base}/visit_id", "visit_id is required")
        elif visit_id in visit_ids:
            issue("error", f"{base}/visit_id", "visit_id must be unique")
        visit_ids.add(visit_id)
        try:
            parsed = date.fromisoformat(str(visit.get("date") or ""))
            if previous and parsed < previous:
                issue("error", f"{base}/date", "visits are not chronological")
            previous = parsed
            first_date = first_date or parsed
            last_date = parsed
        except ValueError:
            issue("error", f"{base}/date", "date must use YYYY-MM-DD")
        if not str(visit.get("subjective") or "").strip():
            issue("warning", f"{base}/subjective", "subjective is empty")
        if visit_type not in {"phone_consult", "v2v_consult"} and not str(
            visit.get("objective") or ""
        ).strip():
            issue("warning", f"{base}/objective", "objective is empty")
        assessment = visit.get("assessment")
        if not isinstance(assessment, dict):
            issue("error", f"{base}/assessment", "assessment must be an object")
        else:
            if not isinstance(assessment.get("themes"), list):
                issue("error", f"{base}/assessment/themes", "themes must be an array")
            if not isinstance(assessment.get("chris_final_reasoning"), str):
                issue(
                    "error",
                    f"{base}/assessment/chris_final_reasoning",
                    "chris_final_reasoning must be a string",
                )
        plan = visit.get("plan")
        if not isinstance(plan, dict):
            issue("error", f"{base}/plan", "plan must be an object")
        elif not isinstance(plan.get("recommendations"), list):
            issue("error", f"{base}/plan/recommendations", "recommendations must be an array")
        else:
            for field in ("action", "rationale", "monitoring", "follow_up"):
                if field in plan and not isinstance(plan[field], list):
                    issue("error", f"{base}/plan/{field}", f"{field} must be an array")
        if visit_type == "intake" and str(visit.get("interval_summary") or "").strip():
            issue("warning", f"{base}/interval_summary", "intake interval_summary should be empty")
        if visit_type == "recheck" and not str(visit.get("interval_summary") or "").strip():
            issue("warning", f"{base}/interval_summary", "recheck interval_summary is empty")
        for med_index, medication in enumerate(visit.get("medication_changes") or []):
            if not medication.get("name") or medication.get("change") not in {
                "started",
                "dose_changed",
                "discontinued",
                "continued",
            }:
                issue(
                    "error",
                    f"{base}/medication_changes/{med_index}",
                    "medication change requires name and valid change",
                )

    if len(intake_indexes) > 1:
        issue("error", "/visits", "only one intake visit is allowed")
    if intake_indexes and intake_indexes[0] != 0:
        issue("error", "/visits", "intake must be the first visit")
    if intake_indexes:
        intake = visits[intake_indexes[0]]
        case_reasoning = (data.get("assessment") or {}).get("chris_final_reasoning")
        intake_reasoning = (intake.get("assessment") or {}).get("chris_final_reasoning")
        if case_reasoning != intake_reasoning:
            issue(
                "error",
                "/assessment/chris_final_reasoning",
                "case-level assessment reasoning must mirror intake",
            )
        if data.get("plan") != intake.get("plan"):
            issue("error", "/plan", "case-level plan must mirror intake visit")

    communications = data.get("communications")
    if not isinstance(communications, list):
        issue("error", "/communications", "communications must be an array")
    else:
        for index, communication in enumerate(communications):
            base = f"/communications/{index}"
            try:
                communication_date = date.fromisoformat(str(communication.get("date") or ""))
                if (
                    first_date
                    and last_date
                    and not (
                        first_date - timedelta(days=365)
                        <= communication_date
                        <= last_date + timedelta(days=365)
                    )
                ):
                    issue("warning", f"{base}/date", "date is outside the visit sanity window")
            except ValueError:
                issue("error", f"{base}/date", "date must use YYYY-MM-DD")
            if communication.get("party") not in {"owner", "gp_vet", "other"}:
                issue("error", f"{base}/party", "invalid party")
            if not str(communication.get("summary") or "").strip():
                issue("error", f"{base}/summary", "summary is required")
    supplements = data.get("medical_record_supplements")
    if not isinstance(supplements, list):
        issue("error", "/medical_record_supplements", "supplements must be an array")
    else:
        intake_date = str(visits[0].get("date") or "") if intake_indexes else ""
        for index, supplement in enumerate(supplements):
            base = f"/medical_record_supplements/{index}"
            received = str(supplement.get("received_date") or "")
            if not received:
                issue("error", base, "received_date required")
            else:
                try:
                    parsed_received = date.fromisoformat(received)
                    if intake_date and parsed_received < date.fromisoformat(intake_date):
                        issue(
                            "error",
                            f"{base}/received_date",
                            "supplement cannot be received before intake",
                        )
                except ValueError:
                    issue("error", f"{base}/received_date", "date must use YYYY-MM-DD")
            if supplement.get("source") not in {"owner", "gp_vet", "other"}:
                issue("error", f"{base}/source", "invalid source")
            for group in ("pre_intake_entries", "post_intake_entries"):
                entries = supplement.get(group)
                if not isinstance(entries, list):
                    issue("error", f"{base}/{group}", f"{group} must be an array")
                    continue
                for entry_index, entry in enumerate(entries):
                    event_date = str(entry.get("event_date") or "")
                    event_path = f"{base}/{group}/{entry_index}/event_date"
                    try:
                        parsed_event = date.fromisoformat(event_date)
                        parsed_intake = date.fromisoformat(intake_date) if intake_date else None
                        if (
                            parsed_intake
                            and group == "pre_intake_entries"
                            and parsed_event >= parsed_intake
                        ):
                            issue("error", event_path, "pre-intake event must precede intake")
                        if (
                            parsed_intake
                            and group == "post_intake_entries"
                            and parsed_event < parsed_intake
                        ):
                            issue("error", event_path, "post-intake event cannot precede intake")
                        elif (
                            parsed_intake
                            and group == "post_intake_entries"
                            and parsed_event == parsed_intake
                        ):
                            issue("warning", event_path, "same-day event may belong in intake")
                    except ValueError:
                        issue("error", event_path, "date must use YYYY-MM-DD")
    return issues


def required_section_keys(data: dict[str, Any]) -> list[str]:
    keys = ["signalment", "history_form", "medical_digest"]
    for index, visit in enumerate(data.get("visits") or []):
        keys.extend(
            [
                f"visit:{index}:subjective",
                f"visit:{index}:objective",
                f"visit:{index}:assessment",
                f"visit:{index}:plan",
            ]
        )
        for optional in ("behavior_diagnoses", "medication_changes", "interval_summary"):
            if optional in visit:
                keys.append(f"visit:{index}:{optional}")
    keys.extend(["communications", "supplements"])
    return keys


def build_review_timeline(
    data: dict[str, Any], reviews: dict[str, SectionReview]
) -> list[dict[str, Any]]:
    """Build clinician-oriented chronological navigation over review sections."""

    def status_for(keys: list[str]) -> str:
        states = [reviews[key].status if key in reviews else "pending" for key in keys]
        if any(state == "needs_changes" for state in states):
            return "needs_changes"
        if states and all(state in {"approved", "not_applicable"} for state in states):
            return "approved"
        return "pending"

    def pages_from(value: Any) -> list[int]:
        pages: set[int] = set()

        def walk(item: Any) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if key == "source_page":
                        try:
                            pages.add(int(child))
                        except (TypeError, ValueError):
                            pass
                    elif key == "source_pages" and isinstance(child, list):
                        for page in child:
                            try:
                                pages.add(int(page))
                            except (TypeError, ValueError):
                                pass
                    else:
                        walk(child)
            elif isinstance(item, list):
                for child in item:
                    walk(child)

        walk(value)
        return sorted(pages)

    events: list[dict[str, Any]] = [
        {
            "sort_key": ("0000-00-00", 0),
            "date": "Case setup",
            "type": "signalment",
            "title": "Patient signalment",
            "summary": "Patient identity and demographic information used throughout the case.",
            "section_keys": ["signalment"],
            "links": [("signalment", "Review signalment")],
            "document_kind": "history",
            "source_pages": pages_from(data.get("signalment") or {}),
            "stages": [],
        },
        {
            "sort_key": ("0000-00-00", 1),
            "date": "Pre-visit",
            "type": "history",
            "title": "Behavior history form",
            "summary": "Caregiver-provided history available before the first visit.",
            "section_keys": ["history_form"],
            "links": [("history_form", "Review history")],
            "document_kind": "history",
            "source_pages": pages_from(data.get("history_form") or {}),
            "stages": [],
        },
        {
            "sort_key": ("0000-00-00", 2),
            "date": "Pre-visit",
            "type": "medical",
            "title": "Pre-intake medical digest",
            "summary": "Medical information known before the behavior consultation.",
            "section_keys": ["medical_digest"],
            "links": [("medical_digest", "Review medical digest")],
            "document_kind": "medfiles",
            "source_pages": pages_from(data.get("pre_intake_medical_digest") or {}),
            "stages": [],
        },
    ]

    for index, visit in enumerate(data.get("visits") or []):
        keys = [
            f"visit:{index}:subjective",
            f"visit:{index}:objective",
            f"visit:{index}:assessment",
            f"visit:{index}:plan",
        ]
        links = [
            (f"visit:{index}:subjective", "S"),
            (f"visit:{index}:objective", "O"),
            (f"visit:{index}:assessment", "A"),
            (f"visit:{index}:plan", "P"),
        ]
        for optional, label in (
            ("behavior_diagnoses", "Diagnoses"),
            ("medication_changes", "Medication changes"),
            ("interval_summary", "Interval"),
        ):
            if optional in visit:
                keys.append(f"visit:{index}:{optional}")
                links.append((f"visit:{index}:{optional}", label))
        is_intake = visit.get("visit_type") == "intake"
        events.append(
            {
                "sort_key": (str(visit.get("date") or "9999-99-99"), 10),
                "date": visit.get("date") or "Undated",
                "type": "visit",
                "title": str(visit.get("visit_type") or "visit").replace("_", " ").title(),
                "summary": str(visit.get("subjective") or "")[:300],
                "section_keys": keys,
                "links": links,
                "document_kind": "clinical_summary",
                "source_pages": pages_from(visit),
                "stages": ["A", "B", "C"] if is_intake else ["D"],
            }
        )

    for index, communication in enumerate(data.get("communications") or []):
        events.append(
            {
                "sort_key": (str(communication.get("date") or "9999-99-99"), 20),
                "date": communication.get("date") or "Undated",
                "type": "communication",
                "title": f"Communication · {communication.get('party') or 'other'}",
                "summary": str(communication.get("summary") or "")[:300],
                "section_keys": ["communications"],
                "links": [("communications", "Review communications")],
                "document_kind": "clinical_summary",
                "source_pages": pages_from(communication),
                "stages": [],
            }
        )

    for index, supplement in enumerate(data.get("medical_record_supplements") or []):
        events.append(
            {
                "sort_key": (str(supplement.get("received_date") or "9999-99-99"), 30),
                "date": supplement.get("received_date") or "Undated",
                "type": "supplement",
                "title": f"Medical supplement · {supplement.get('source') or 'other'}",
                "summary": (
                    f"{len(supplement.get('pre_intake_entries') or [])} pre-intake and "
                    f"{len(supplement.get('post_intake_entries') or [])} post-intake entries"
                ),
                "section_keys": ["supplements"],
                "links": [("supplements", "Review supplement")],
                "document_kind": "medfiles",
                "source_pages": pages_from(supplement),
                "stages": [],
            }
        )

    for event in events:
        event["status"] = status_for(event["section_keys"])
    return sorted(events, key=lambda event: event["sort_key"])


def add_revision(
    record: CaseRecord,
    patch: list[dict[str, Any]],
    *,
    base_revision: int,
    author: User,
    reason: str,
) -> Revision:
    if base_revision != record.current_revision:
        raise RuntimeError("This case changed in another session; reload before saving")
    current = materialize_case(record)
    updated = jsonpatch.apply_patch(current, patch, in_place=False)
    if not isinstance(updated, dict):
        raise ValueError("A revision must preserve the case as a JSON object")
    number = record.current_revision + 1
    revision = Revision(
        case_id=record.id,
        source_hash=record.source_hash,
        revision_number=number,
        patch_json=json.dumps(patch, ensure_ascii=False),
        reason=reason,
        author_id=author.id,
    )
    db.session.add(revision)
    record.current_revision = number
    record.status = "in_review"
    for review in SectionReview.query.filter_by(case_id=record.id).all():
        stage_changed = review.section_key.startswith("stage:")
        section_changed = any(
            _patch_touches_section(op.get("path", ""), review.section_key) for op in patch
        )
        if review.status == "approved" and (stage_changed or section_changed):
            review.status = "pending"
            review.reviewed_at = None
            review.reviewed_by_id = None
    db.session.commit()
    audit(
        "revision_saved",
        record=record,
        user=author,
        detail={"revision": number, "reason": reason},
    )
    return revision


def _patch_touches_section(path: str, section_key: str) -> bool:
    mapping = {
        "signalment": "/signalment",
        "history_form": "/history_form",
        "medical_digest": "/pre_intake_medical_digest",
        "communications": "/communications",
        "supplements": "/medical_record_supplements",
    }
    prefix = mapping.get(section_key)
    if prefix:
        return path.startswith(prefix)
    match = re.fullmatch(
        r"visit:(\d+):(subjective|objective|assessment|behavior_diagnoses|plan|medication_changes|interval_summary)",
        section_key,
    )
    return bool(match and path.startswith(f"/visits/{match.group(1)}/{match.group(2)}"))


def update_section(
    record: CaseRecord,
    section_key: str,
    status: str,
    comment: str,
    reviewer: User,
    *,
    base_revision: int | None = None,
) -> SectionReview:
    if base_revision is not None and base_revision != record.current_revision:
        raise RuntimeError("This case changed in another session; reload before sign-off")
    if status not in ALLOWED_SECTION_STATUSES:
        raise ValueError("Invalid section status")
    is_stage = section_key.startswith("stage:")
    stage_hash: str | None = None
    if is_stage:
        stage = section_key.removeprefix("stage:")
        examples, stage_hash = stage_examples(record)
        if stage not in examples or (stage == "D" and not examples["D"]):
            raise ValueError("Unknown or inapplicable stage")
        if status == "not_applicable":
            raise ValueError("Required training stages cannot be marked not applicable")
        if stage == "A" and status == "approved" and not examples["A"]:
            raise ValueError("Stage A teacher artifact is missing and cannot be approved")
    elif section_key not in required_section_keys(
        normalize_for_review(materialize_case(record))
    ):
        raise ValueError("Unknown section")
    review = SectionReview.query.filter_by(case_id=record.id, section_key=section_key).one_or_none()
    if review is None:
        review = SectionReview(case_id=record.id, section_key=section_key)
        db.session.add(review)
    review.status = status
    review.comment = comment
    review.revision_number = record.current_revision
    review.reviewed_by_id = reviewer.id
    review.reviewed_at = datetime.now(timezone.utc) if status in {"approved", "not_applicable"} else None
    review.artifact_hash = stage_hash if section_key == "stage:A" else None
    record.status = "needs_changes" if status == "needs_changes" else "in_review"
    db.session.commit()
    audit(
        "section_updated",
        record=record,
        user=reviewer,
        detail={"section": section_key, "status": status},
    )
    return review


def audit(
    event_type: str,
    *,
    record: CaseRecord | None = None,
    user: User | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    identity = getattr(g, "cf_identity", None)
    event = AuditEvent(
        event_type=event_type,
        case_id=record.id if record else None,
        user_id=user.id if user else None,
        cf_identity=identity,
        remote_addr=request.remote_addr if has_request_context() else None,
        detail_json=json.dumps(detail or {}, ensure_ascii=False),
    )
    db.session.add(event)
    db.session.commit()


def document_paths(record: CaseRecord) -> dict[str, Path]:
    data = source_case(record)
    raw = ((data.get("source") or {}).get("input_paths") or {})
    allowed = Path(current_app.config["SOURCE_ROOT"]).resolve()
    result: dict[str, Path] = {}
    aliases = {
        "history": ("history_form", "history_form_path"),
        "medfiles": ("medfiles_pdf", "medfiles_digest", "medfiles"),
        "clinical_summary": ("clinical_summary_pdf", "clinical_summary"),
    }
    for kind, names in aliases.items():
        for name in names:
            value = raw.get(name)
            if not value:
                continue
            values = value if isinstance(value, list) else [value]
            for item_index, item in enumerate(values):
                path = Path(item).resolve()
                if path.is_file() and path.is_relative_to(allowed):
                    key = kind if item_index == 0 else f"{kind}_{item_index + 1}"
                    result[key] = path
            if any(key == kind or key.startswith(f"{kind}_") for key in result):
                break
    if not result:
        patient_root = (allowed / record.patient_folder).resolve()
        if patient_root.is_dir() and patient_root.is_relative_to(allowed):
            history = patient_root / "history_form.json"
            if history.is_file():
                result["history"] = history
            pdfs = sorted(patient_root.glob("*.pdf"))
            clinical = next(
                (path for path in pdfs if "clinical" in path.name.lower()),
                None,
            )
            if clinical is not None:
                result["clinical_summary"] = clinical
            medfiles = [path for path in pdfs if path != clinical]
            for index, path in enumerate(medfiles):
                key = "medfiles" if index == 0 else f"medfiles_{index + 1}"
                result[key] = path
    return result


def scrub_approved_case(data: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(data)
    source = out.get("source")
    if isinstance(source, dict):
        source.pop("input_paths", None)
        source["scrubbed_for_review_export"] = True

    def scrub_paths(value: Any) -> None:
        if isinstance(value, dict):
            for key in list(value):
                if key in {"csv_path", "source_path", "local_path", "file_id"}:
                    value.pop(key, None)
                else:
                    scrub_paths(value[key])
        elif isinstance(value, list):
            for item in value:
                scrub_paths(item)

    scrub_paths(out)
    return out


def scrub_findings(data: dict[str, Any]) -> list[str]:
    """Find obvious owner contact PII and local paths before promotion."""

    findings: list[str] = []
    sensitive_keys = {
        "owner_name",
        "owner_email",
        "owner_phone",
        "client_name",
        "client_email",
        "client_phone",
        "street_address",
    }

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}/{key}"
                if key.lower() in sensitive_keys and str(child or "").strip():
                    findings.append(f"{child_path}: owner contact field is populated")
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}/{index}")
        elif isinstance(value, str):
            if EMAIL_RE.search(value):
                findings.append(f"{path}: email-like value")
            if PHONE_RE.search(value):
                findings.append(f"{path}: phone-like value")
            if ABSOLUTE_PATH_RE.search(value):
                findings.append(f"{path}: absolute local path")

    walk(data)
    return sorted(set(findings))


def revision_diff_rows(record: CaseRecord) -> list[dict[str, Any]]:
    """Materialize field-level before/after rows for clinician sign-off."""

    state = source_case(record)
    rows: list[dict[str, Any]] = []
    for revision in revisions_for(record):
        for operation in json.loads(revision.patch_json):
            path = operation.get("path", "")
            try:
                before = jsonpatch.JsonPointer(path).resolve(state)
            except Exception:
                before = None
            state = jsonpatch.apply_patch(state, [operation], in_place=False)
            try:
                after = jsonpatch.JsonPointer(path).resolve(state)
            except Exception:
                after = None
            rows.append(
                {
                    "revision": revision.revision_number,
                    "path": path,
                    "operation": operation.get("op"),
                    "before": before,
                    "after": after,
                    "reason": revision.reason,
                    "author": revision.author.display_name,
                }
            )
    return rows


def approval_blockers(record: CaseRecord) -> list[str]:
    if record.status == "retired":
        return ["Case is no longer present in the clinical-data cases/ source"]
    data = normalize_for_review(materialize_case(record))
    blockers = [
        f"{item['path']}: {item['message']}"
        for item in validate_longitudinal(data)
        if item["severity"] == "error"
    ]
    reviews = {
        row.section_key: row
        for row in SectionReview.query.filter_by(case_id=record.id).all()
    }
    for key in required_section_keys(data):
        review = reviews.get(key)
        if review is None or review.status not in {"approved", "not_applicable"}:
            blockers.append(f"Section not approved: {key}")
        elif review.revision_number != record.current_revision:
            blockers.append(f"Section approval is stale: {key}")
    examples, gap_hash = stage_examples(record)
    if not examples["A"]:
        blockers.append("Stage A teacher artifact is missing")
    for key in required_stage_keys(record):
        review = reviews.get(key)
        if review is None or review.status != "approved":
            blockers.append(f"Training stage not approved: {key}")
        elif review.revision_number != record.current_revision:
            blockers.append(f"Training stage approval is stale: {key}")
        elif key == "stage:A" and review.artifact_hash != gap_hash:
            blockers.append("Stage A approval is stale because its teacher artifact changed")
    unresolved = [
        row
        for row in record_comments(record)
        if row.blocking and not row.resolved
    ]
    if unresolved:
        blockers.append(f"{len(unresolved)} unresolved blocking comment(s)")
    for finding in scrub_findings(scrub_approved_case(data)):
        blockers.append(f"PII/scrub check: {finding}")
    return blockers


def record_comments(record: CaseRecord):
    from collabvet_review_app.models import ReviewComment

    return ReviewComment.query.filter_by(case_id=record.id).order_by(ReviewComment.created_at).all()


def export_approved(record: CaseRecord, approver: User) -> ApprovedExport:
    blockers = approval_blockers(record)
    if blockers:
        raise ValueError("\n".join(blockers))
    output_root = Path(current_app.config["OUTPUT_ROOT"]).resolve()
    approved_dir = output_root / "cases"
    review_dir = output_root / "reviews"
    data = scrub_approved_case(materialize_case(record))
    approved_path = approved_dir / f"{record.case_id}.approved.json"
    review_path = review_dir / f"{record.case_id}.review.json"
    reviews = SectionReview.query.filter_by(case_id=record.id).all()
    revisions = revisions_for(record)
    review_doc = {
        "version": 1,
        "case_id": record.case_id,
        "extraction_ref": {
            "source_hash": record.source_hash,
            "source_file": Path(record.source_path).name,
        },
        "review_status": "approved",
        "training_eligible": not record.is_holdout,
        "holdout": record.is_holdout,
        "reviewer": {"email": approver.email, "display_name": approver.display_name},
        "approved_at": utc_iso(),
        "revision": record.current_revision,
        "section_signoffs": [
            {
                "section": row.section_key,
                "status": row.status,
                "revision": row.revision_number,
                "reviewed_by": row.reviewed_by.email if row.reviewed_by else None,
                "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
                "comment": row.comment,
            }
            for row in reviews
        ],
        "field_edits": [
            {
                "revision": row.revision_number,
                "patch": json.loads(row.patch_json),
                "reason": row.reason,
                "author": row.author.email,
                "created_at": row.created_at.isoformat(),
            }
            for row in revisions
        ],
        "validation_results": validate_longitudinal(normalize_for_review(data)),
        "scrub_check": {"passed": not scrub_findings(data), "findings": scrub_findings(data)},
        "comments": [
            {
                "section": row.section_key,
                "body": row.body,
                "blocking": row.blocking,
                "resolved": row.resolved,
                "author": row.author.email,
                "created_at": row.created_at.isoformat(),
            }
            for row in record_comments(record)
        ],
    }
    atomic_write_json(approved_path, data)
    atomic_write_json(review_path, review_doc)
    exported = ApprovedExport(
        case_id=record.id,
        source_hash=record.source_hash,
        approved_path=str(approved_path),
        review_path=str(review_path),
        approved_by_id=approver.id,
    )
    db.session.add(exported)
    record.status = "approved"
    db.session.commit()
    rebuild_approved_index()
    audit("case_approved", record=record, user=approver, detail={"holdout": record.is_holdout})
    return exported


def rebuild_approved_index() -> Path:
    output_root = Path(current_app.config["OUTPUT_ROOT"]).resolve()
    rows = []
    latest: dict[int, ApprovedExport] = {}
    for export in ApprovedExport.query.order_by(ApprovedExport.approved_at.asc()).all():
        latest[export.case_id] = export
    for export in latest.values():
        record = db.session.get(CaseRecord, export.case_id)
        if (
            record is None
            or record.status == "retired"
            or export.source_hash != record.source_hash
        ):
            continue
        rows.append(
            {
                "case_id": record.case_id,
                "source_hash": record.source_hash,
                "review_status": record.status,
                "schema_version": 2,
                "holdout": record.is_holdout,
                "training_eligible": not record.is_holdout,
                "split": record.split,
                "approved_at": export.approved_at.isoformat(),
                "approved_file": str(Path(export.approved_path).resolve().relative_to(output_root)),
                "review_file": str(Path(export.review_path).resolve().relative_to(output_root)),
            }
        )
    index = {
        "version": 1,
        "generated_at": utc_iso(),
        "case_count": len(rows),
        "training_eligible_count": sum(bool(row["training_eligible"]) for row in rows),
        "cases": sorted(rows, key=lambda row: row["case_id"]),
    }
    path = output_root / "approved_cases_index.json"
    atomic_write_json(path, index)
    return path

