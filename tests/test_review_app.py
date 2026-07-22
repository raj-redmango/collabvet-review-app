"""Synthetic-only security and workflow tests for the clinician review app."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.generate_review_demo import generate
import collabvet_review_app.services as review_services
from collabvet_review_app import create_app
from collabvet_review_app.models import (
    AuditEvent,
    CaseRecord,
    User,
    db,
)
from collabvet_review_app.services import (
    add_revision,
    index_cases,
    materialize_case,
    normalize_for_review,
    required_section_keys,
    required_stage_keys,
    sha256_file,
    update_section,
    validate_longitudinal,
    verify_clinical_data_checkout,
)
from collabvet_review_app.training_gate import build_approved_manifest


def synthetic_case(case_id: str = "synthetic-one") -> dict:
    assessment = {
        "themes": ["fear of unfamiliar people"],
        "chris_final_reasoning": "History and observed avoidance support a fear diagnosis.",
    }
    plan = {
        "recommendations": ["Use distance and counterconditioning."],
        "action": ["Start behavior modification."],
        "rationale": ["Reduce fear while preventing rehearsal."],
        "monitoring": ["Track recovery time."],
        "follow_up": ["Recheck in four weeks."],
    }
    return {
        "schema_version": 2,
        "case_id": case_id,
        "source": {"input_paths": {}},
        "signalment": {
            "pet_name": "Demo",
            "species": "Canine",
            "breed": "Mixed",
            "age": "3 years",
            "sex": "Spayed female",
        },
        "history_form": {"presenting_concerns": "Barks at unfamiliar visitors."},
        "pre_intake_medical_digest": {"summary": "No relevant abnormalities."},
        "assessment": assessment,
        "plan": plan,
        "visits": [
            {
                "visit_id": "v01",
                "date": "2026-01-10",
                "visit_type": "intake",
                "subjective": "Caregiver reports barking at visitors.",
                "objective": "Avoided approach and accepted treats at distance.",
                "assessment": assessment,
                "plan": plan,
                "medication_changes": [],
                "interval_summary": "",
            }
        ],
        "communications": [],
        "medical_record_supplements": [],
    }


@pytest.fixture()
def app(tmp_path: Path):
    input_root = tmp_path / "input"
    source_root = tmp_path / "sources"
    output_root = tmp_path / "output"
    instance_root = tmp_path / "instance"
    teacher_root = tmp_path / "teacher-data"
    for path in (input_root, source_root, output_root, instance_root, teacher_root):
        path.mkdir()
    gap_root = teacher_root / "teacher" / "gap"
    gap_root.mkdir(parents=True)
    (gap_root / "Synthetic_One_gap.json").write_text(
        json.dumps(
            {
                "draft_so": {"subjective": "Draft", "objective": "Pending examination."},
                "triage_qa": [],
                "stage_a": {
                    "draft_so": {
                        "subjective": "Draft",
                        "objective": "Pending examination.",
                    },
                    "hypothesized_patterns": [],
                    "questions": [],
                },
            }
        ),
        encoding="utf-8",
    )
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-is-long-and-random-enough",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(instance_root / 'test.sqlite3').as_posix()}",
            "INPUT_ROOT": input_root,
            "SOURCE_ROOT": source_root,
            "OUTPUT_ROOT": output_root,
            "INSTANCE_ROOT": instance_root,
            "TEACHER_ROOT": teacher_root,
            "WTF_CSRF_ENABLED": False,
            "CF_ACCESS_REQUIRED": False,
        }
    )
    with app.app_context():
        db.create_all()
        user = User(email="reviewer@example.test", display_name="Reviewer", role="admin")
        user.set_password("correct horse battery staple")
        db.session.add(user)
        db.session.commit()
    return app


def write_registry_case(app, data: dict | None = None, patient: str = "Synthetic One") -> Path:
    data = data or synthetic_case()
    root = Path(app.config["INPUT_ROOT"])
    case_path = root / "Synthetic_One_twopass_longitudinal.json"
    case_path.write_text(json.dumps(data), encoding="utf-8")
    registry = {
        "version": 1,
        "patients": {
            patient: {"longitudinal_file": case_path.name},
        },
    }
    (root / "twopass_completed.json").write_text(json.dumps(registry), encoding="utf-8")
    return case_path


def write_external_case(app, data: dict | None = None, patient: str = "Synthetic One") -> Path:
    data = data or synthetic_case()
    data["patient_folder"] = patient
    root = Path(app.config["INPUT_ROOT"])
    case_path = root / f"v02-{data['case_id']}.json"
    case_path.write_text(json.dumps(data), encoding="utf-8")
    return case_path


def login(client):
    return client.post(
        "/login",
        data={"email": "reviewer@example.test", "password": "correct horse battery staple"},
        follow_redirects=True,
    )


def test_full_validation_and_normalization():
    case = synthetic_case()
    assert not [item for item in validate_longitudinal(case) if item["severity"] == "error"]
    case["visits"][0]["assessment"] = "legacy assessment"
    normalized = normalize_for_review(case)
    assert normalized["visits"][0]["assessment"]["chris_final_reasoning"] == "legacy assessment"
    case = synthetic_case()
    case["visits"].append(
        {
            **case["visits"][0],
            "visit_id": "v02",
            "date": "2025-01-01",
            "visit_type": "recheck",
        }
    )
    assert any("chronological" in item["message"] for item in validate_longitudinal(case))


def test_login_headers_queue_and_audit(app):
    write_registry_case(app)
    with app.app_context():
        assert index_cases()["created"] == 1
    client = app.test_client()
    response = login(client)
    assert response.status_code == 200
    assert b"Synthetic One" in response.data
    assert response.headers["Cache-Control"].startswith("no-store")
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    with app.app_context():
        assert AuditEvent.query.filter_by(event_type="login_success").count() == 1


def test_external_cases_layout_is_indexed_and_missing_cases_are_removed(app):
    path = write_external_case(app)
    with app.app_context():
        first = index_cases()
        assert first["source_revision"] == "testing"
        assert first["created"] == 1
        record = CaseRecord.query.one()
        assert record.patient_folder == "Synthetic One"
        assert Path(record.source_path).name == "v02-synthetic-one.json"

        path.unlink()
        second = index_cases()
        assert second["removed"] == 1
        assert CaseRecord.query.count() == 0

    client = app.test_client()
    response = login(client)
    assert b"Synthetic One" not in response.data


def test_external_checkout_origin_is_verified(app, tmp_path: Path, monkeypatch):
    checkout = tmp_path / "collabvet-clinical-data"
    (checkout / "cases").mkdir(parents=True)
    app.config.update(
        {
            "TESTING": False,
            "CLINICAL_DATA_ROOT": checkout,
            "INPUT_ROOT": checkout / "cases",
            "CLINICAL_DATA_REPOSITORY": (
                "https://github.com/sachin-redmango/collabvet-clinical-data.git"
            ),
        }
    )

    def fake_git(_checkout, *args):
        values = {
            ("rev-parse", "--show-toplevel"): str(checkout),
            ("remote", "get-url", "origin"): (
                "git@github.com:sachin-redmango/collabvet-clinical-data.git"
            ),
            ("branch", "--show-current"): "main",
            ("status", "--porcelain", "--", "cases"): "",
            ("rev-parse", "HEAD"): "abc123",
        }
        return values[args]

    monkeypatch.setattr(review_services, "_git", fake_git)
    with app.app_context():
        assert verify_clinical_data_checkout() == "abc123"

        def wrong_origin(_checkout, *args):
            if args == ("remote", "get-url", "origin"):
                return "https://github.com/example/wrong.git"
            return fake_git(_checkout, *args)

        monkeypatch.setattr(review_services, "_git", wrong_origin)
        with pytest.raises(RuntimeError, match="origin must be"):
            verify_clinical_data_checkout()


def test_case_can_be_assigned_to_named_reviewer(app):
    write_registry_case(app)
    with app.app_context():
        index_cases()
        chris = User(email="chris@example.test", display_name="Chris", role="reviewer")
        chris.set_password("temporary reviewer password")
        db.session.add(chris)
        db.session.commit()
        record_id = CaseRecord.query.one().id
        reviewer_id = chris.id
    client = app.test_client()
    login(client)
    response = client.post(
        f"/cases/{record_id}/assign",
        data={"assignee_id": str(reviewer_id)},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Assigned to Chris" in response.data
    with app.app_context():
        assert db.session.get(CaseRecord, record_id).assigned_user_id == reviewer_id


def test_existing_user_password_can_be_rotated(app):
    result = app.test_cli_runner().invoke(
        args=["set-password", "--email", "reviewer@example.test"],
        input="new secure reviewer password\nnew secure reviewer password\n",
    )
    assert result.exit_code == 0
    with app.app_context():
        assert User.query.filter_by(email="reviewer@example.test").one().check_password(
            "new secure reviewer password"
        )


def test_optimistic_revision_and_stale_hash(app):
    path = write_registry_case(app)
    with app.app_context(), app.test_request_context("/"):
        index_cases()
        record = CaseRecord.query.one()
        user = User.query.one()
        revision = add_revision(
            record,
            [{"op": "replace", "path": "/visits/0/subjective", "value": "Corrected."}],
            base_revision=0,
            author=user,
            reason="Source page 2",
        )
        assert revision.revision_number == 1
        assert materialize_case(record)["visits"][0]["subjective"] == "Corrected."
        with pytest.raises(RuntimeError, match="another session"):
            add_revision(
                record,
                [{"op": "replace", "path": "/visits/0/subjective", "value": "Conflict."}],
                base_revision=0,
                author=user,
                reason="stale",
            )
        path.write_text(json.dumps(synthetic_case()) + " ", encoding="utf-8")
        with pytest.raises(RuntimeError, match="changed"):
            materialize_case(record)


def test_document_allowlist_rejects_outside_path(app, tmp_path: Path):
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4\n")
    case = synthetic_case()
    case["source"]["input_paths"]["clinical_summary_pdf"] = str(outside)
    write_registry_case(app, case)
    with app.app_context():
        index_cases()
        record_id = CaseRecord.query.one().id
    client = app.test_client()
    login(client)
    response = client.get(f"/cases/{record_id}/documents/clinical_summary")
    assert response.status_code == 404
    assert client.get(f"/cases/{record_id}/documents/..%2F..%2Foutside").status_code == 404


def test_end_to_end_edit_signoff_approve_and_export(app):
    write_registry_case(app)
    with app.app_context():
        index_cases()
        record_id = CaseRecord.query.one().id
    client = app.test_client()
    login(client)
    client.post(f"/cases/{record_id}/assign")
    response = client.post(
        f"/cases/{record_id}/review/visit:0:subjective",
        data={
            "base_revision": "0",
            "mode": "fields",
            "field|/visits/0/subjective": "Corrected caregiver report.",
            "reason": "Confirmed in synthetic source.",
        },
    )
    assert response.status_code == 302
    with app.app_context(), app.test_request_context("/"):
        record = db.session.get(CaseRecord, record_id)
        user = User.query.one()
        for key in required_section_keys(materialize_case(record)):
            update_section(record, key, "approved", "Verified", user)
        for key in required_stage_keys(record):
            update_section(record, key, "approved", "Verified training example", user)
    response = client.post(f"/cases/{record_id}/approve", follow_redirects=True)
    assert response.status_code == 200
    assert b"Case approved and exported" in response.data
    output_root = Path(app.config["OUTPUT_ROOT"])
    approved_path = output_root / "cases" / "synthetic-one.approved.json"
    first_export = approved_path.read_bytes()
    assert client.post(f"/cases/{record_id}/approve").status_code == 302
    assert approved_path.read_bytes() == first_export
    approved = json.loads(approved_path.read_text(encoding="utf-8"))
    sidecar = json.loads(
        (output_root / "reviews" / "synthetic-one.review.json").read_text(encoding="utf-8")
    )
    index = json.loads((output_root / "approved_cases_index.json").read_text(encoding="utf-8"))
    assert approved["visits"][0]["subjective"] == "Corrected caregiver report."
    assert "input_paths" not in approved["source"]
    assert sidecar["training_eligible"] is True
    assert index["training_eligible_count"] == 1


def test_cloudflare_access_and_csrf_fail_closed(tmp_path: Path):
    common = {
        "TESTING": True,
        "SECRET_KEY": "test-secret-is-long-and-random-enough",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "INPUT_ROOT": tmp_path,
        "SOURCE_ROOT": tmp_path,
        "OUTPUT_ROOT": tmp_path / "out",
        "INSTANCE_ROOT": tmp_path / "instance",
        "CF_ACCESS_REQUIRED": True,
        "CF_ACCESS_TEAM_DOMAIN": "https://example.cloudflareaccess.com",
        "CF_ACCESS_AUDIENCE": "test-audience",
    }
    protected = create_app({**common, "WTF_CSRF_ENABLED": False})
    assert protected.test_client().get("/login").status_code == 403
    assert (
        protected.test_client()
        .get("/login", headers={"Cf-Access-Jwt-Assertion": "forged"})
        .status_code
        == 403
    )

    csrf_app = create_app(
        {
            **common,
            "CF_ACCESS_REQUIRED": False,
            "WTF_CSRF_ENABLED": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        }
    )
    with csrf_app.app_context():
        db.create_all()
    assert csrf_app.test_client().post("/login", data={}).status_code == 400


def test_training_gate_requires_current_hash_and_excludes_holdout(tmp_path: Path):
    project = tmp_path / "project"
    input_root = tmp_path / "input"
    review_root = tmp_path / "review"
    (project / "config").mkdir(parents=True)
    (review_root / "cases").mkdir(parents=True)
    input_root.mkdir()
    (project / "config" / "holdout_golden.json").write_text(
        json.dumps({"case_ids": ["holdout-case"]}), encoding="utf-8"
    )
    (project / "config" / "golden_regression_cases.json").write_text(
        json.dumps({"cases": []}), encoding="utf-8"
    )
    registry = {"patients": {}}
    index_rows = []
    for case_id, patient in (("training-case", "Training Case"), ("holdout-case", "Holdout Case")):
        data = synthetic_case(case_id)
        source = input_root / f"{case_id}_twopass_longitudinal.json"
        approved = review_root / "cases" / f"{case_id}.approved.json"
        source.write_text(json.dumps(data), encoding="utf-8")
        approved.write_text(json.dumps(data), encoding="utf-8")
        registry["patients"][patient] = {"longitudinal_file": source.name}
        index_rows.append(
            {
                "case_id": case_id,
                "patient_folder": patient,
                "source_hash": sha256_file(source),
                "review_status": "approved",
                "schema_version": 2,
                "holdout": False,
                "training_eligible": True,
                "split": "train",
                "approved_file": f"cases/{approved.name}",
            }
        )
    (input_root / "twopass_completed.json").write_text(json.dumps(registry), encoding="utf-8")
    (review_root / "approved_cases_index.json").write_text(
        json.dumps({"cases": index_rows}), encoding="utf-8"
    )
    manifest = build_approved_manifest(
        project_root=project,
        input_root=input_root,
        review_output_root=review_root,
        destination=tmp_path / "manifest.json",
    )
    assert [row["case_id"] for row in manifest["patients"]] == ["training-case"]
    assert manifest["rejected"] == [{"case_id": "holdout-case", "reason": "golden holdout"}]


def test_three_case_synthetic_pilot(tmp_path: Path):
    pilot_root = tmp_path / "pilot"
    generate(pilot_root)
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-is-long-and-random-enough",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(pilot_root / 'instance' / 'pilot.sqlite3').as_posix()}",
            "INPUT_ROOT": pilot_root / "input",
            "SOURCE_ROOT": pilot_root / "sources",
            "OUTPUT_ROOT": pilot_root / "output",
            "INSTANCE_ROOT": pilot_root / "instance",
            "TEACHER_ROOT": pilot_root / "teacher",
            "WTF_CSRF_ENABLED": False,
            "CF_ACCESS_REQUIRED": False,
        }
    )
    with app.app_context():
        db.create_all()
        user = User(email="reviewer@example.test", display_name="Pilot Clinician")
        user.set_password("correct horse battery staple")
        db.session.add(user)
        db.session.commit()
        result = index_cases()
        assert result["created"] == 3
        records = {row.case_id: row for row in CaseRecord.query.all()}
        assert records["demo-intake"].visit_count == 1
        assert records["demo-longitudinal"].visit_count == 2
        assert records["agatha-boccia"].is_holdout is True
        intake_id = records["demo-intake"].id
        longitudinal_id = records["demo-longitudinal"].id
        holdout_id = records["agatha-boccia"].id
    client = app.test_client()
    login(client)
    stage_a = client.get(f"/cases/{intake_id}/preview/A")
    assert stage_a.status_code == 200
    assert b"Expected output" in stage_a.data
    assert client.get(f"/cases/{intake_id}/preview/B").status_code == 200
    assert client.get(f"/cases/{intake_id}/preview/C").status_code == 200
    assert client.get(f"/cases/{longitudinal_id}/preview/D").status_code == 200

    with app.app_context(), app.test_request_context("/"):
        record = db.session.get(CaseRecord, holdout_id)
        user = User.query.one()
        for key in required_section_keys(materialize_case(record)):
            update_section(record, key, "approved", "Synthetic pilot verification", user)
        for key in required_stage_keys(record):
            update_section(record, key, "approved", "Synthetic stage verification", user)
    assert client.post(f"/cases/{holdout_id}/approve").status_code == 302
    index = json.loads(
        (pilot_root / "output" / "approved_cases_index.json").read_text(encoding="utf-8")
    )
    holdout_row = next(row for row in index["cases"] if row["case_id"] == "agatha-boccia")
    assert holdout_row["training_eligible"] is False

