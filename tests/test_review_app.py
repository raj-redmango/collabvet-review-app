"""Synthetic-only security and workflow tests for the clinician review app."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import collabvet_review_app.insights as review_insights
import collabvet_review_app.services as review_services
from collabvet_review_app import create_app
from collabvet_review_app import security as review_security
from collabvet_review_app.data_dashboard import (
    clear_dashboard_cache,
    dashboard_overview,
    dashboard_patient,
    dashboard_patients,
)
from collabvet_review_app.insights import (
    InsightsAPIError,
    clear_token_cache,
    clinical_insights_request,
)
from collabvet_review_app.models import (
    ApprovedExport,
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
from scripts.generate_review_demo import generate


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
    review_security._attempts.clear()
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
            "COLLABVET_API_BASE_URL": "",
            "COLLABVET_REVIEW_CLIENT_ID": "",
            "COLLABVET_REVIEW_CLIENT_SECRET": "",
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


def test_clinical_insights_is_authenticated_and_has_bound_searches(app):
    client = app.test_client()
    assert client.get("/clinical-insights").status_code == 302
    login(client)
    response = client.get("/clinical-insights")
    assert response.status_code == 200
    for tab in (
        "patterns",
        "pathways",
        "treatments",
        "medications",
        "safety",
        "cases",
        "knowledge",
        "runs",
    ):
        assert f'data-search-tab="{tab}"'.encode() in response.data
        assert f'data-clear-tab="{tab}"'.encode() in response.data
    assert b'id="graph-case-search"' in response.data
    assert b'id="graph-clear-search"' in response.data


def test_clinical_insights_proxy_preserves_filters_and_rejects_unknowns(app, monkeypatch):
    captured = {}

    def fake_request(path, query):
        captured.update({"path": path, "query": query})
        return {"items": [], "total": 0}

    monkeypatch.setattr("collabvet_review_app.routes.clinical_insights_request", fake_request)
    client = app.test_client()
    login(client)
    response = client.get(
        "/clinical-insights/api/cases?run_id=run-1&vb_id=vb-1&q=fear&offset=10&limit=10&unsafe=x"
    )
    assert response.status_code == 200
    assert captured == {
        "path": "/api/v1/review/clinical-insights/cases",
        "query": {
            "run_id": "run-1",
            "vb_id": "vb-1",
            "q": "fear",
            "offset": "10",
            "limit": "10",
        },
    }
    assert client.get("/clinical-insights/api/not-real").status_code == 404


def test_clinical_graph_proxy_is_authenticated_and_strictly_allowlisted(app, monkeypatch):
    captured = []

    def fake_request(path, query, **kwargs):
        captured.append((path, query, kwargs))
        return {"items": [], "total": 0}

    monkeypatch.setattr("collabvet_review_app.routes.clinical_insights_request", fake_request)
    client = app.test_client()
    assert client.get("/clinical-insights/api/graph/summary").status_code == 302
    login(client)

    response = client.get(
        "/clinical-insights/api/graph/cases"
        "?q=fear&offset=25&limit=25&source_vb_id=vb-1"
        "&mining_run_id=run-1&review_state=confirmed"
        "&include_quarantined=true&unsafe=secret"
    )
    assert response.status_code == 200
    assert captured[-1] == (
        "/api/v1/review/clinical-insights/graph/cases",
        {
            "q": "fear",
            "offset": "25",
            "limit": "25",
            "source_vb_id": "vb-1",
            "mining_run_id": "run-1",
            "review_state": "confirmed",
            "include_quarantined": "true",
        },
        {},
    )
    assert client.get("/clinical-insights/api/graph/cases/id/not-real").status_code == 404
    assert client.get("/clinical-insights/api/graph/neo4j").status_code == 404


def test_clinical_graph_relationship_and_global_queries_use_review_namespace(
    app, monkeypatch
):
    captured = []

    def fake_request(path, query, **kwargs):
        captured.append((path, query, kwargs))
        return {"nodes": [], "edges": [], "projection_source": "neo4j"}

    monkeypatch.setattr("collabvet_review_app.routes.clinical_insights_request", fake_request)
    client = app.test_client()
    login(client)

    response = client.get(
        "/clinical-insights/api/graph/cases/case-id/relationships"
        "?include_quarantined=true&target_key=not-allowed"
    )
    assert response.status_code == 200
    assert captured[-1] == (
        "/api/v1/review/clinical-insights/graph/cases/case-id/relationships",
        {"include_quarantined": "true"},
        {},
    )

    response = client.get(
        "/clinical-insights/api/graph/global-graph"
        "?max_nodes=80&include_machine_only=true&include_quarantined=true"
        "&aggregation_level=claim&password=not-allowed"
    )
    assert response.status_code == 200
    assert captured[-1] == (
        "/api/v1/review/clinical-insights/graph/global-graph",
        {
            "max_nodes": "80",
            "include_machine_only": "true",
            "include_quarantined": "true",
            "aggregation_level": "claim",
        },
        {},
    )


def test_clinical_graph_review_uses_existing_api_and_validates_payload(app, monkeypatch):
    captured = []

    def fake_request(path, query, **kwargs):
        captured.append((path, query, kwargs))
        return {"status": "accepted"}

    monkeypatch.setattr("collabvet_review_app.routes.clinical_insights_request", fake_request)
    client = app.test_client()
    login(client)

    response = client.post(
        "/clinical-insights/api/graph/cases/graph-id/review",
        json={
            "target_kind": "node",
            "target_key": "claim-1",
            "action": "correct",
            "rationale": "Documentation supports clearer wording.",
            "expected_payload_hash": "payload-hash",
            "replacement": {"headline": "Documented fear response"},
            "unexpected": "discarded",
        },
    )
    assert response.status_code == 200
    assert captured[-1] == (
        "/api/v1/review/clinical-insights/graph/cases/graph-id/review",
        {},
        {
            "method": "POST",
            "body": {
                "target_kind": "node",
                "target_key": "claim-1",
                "action": "correct",
                "rationale": "Documentation supports clearer wording.",
                "expected_payload_hash": "payload-hash",
                "replacement": {"headline": "Documented fear response"},
            },
        },
    )
    assert client.post(
        "/clinical-insights/api/graph/cases/graph-id/review",
        json={"target_kind": "node", "target_key": "claim-1", "action": "delete"},
    ).status_code == 400
    assert client.post(
        "/clinical-insights/api/graph/cases/graph-id/review",
        json={"target_kind": "node", "target_key": "claim-1", "action": "annotate"},
    ).status_code == 400


def test_graph_frontend_is_api_only_and_has_clinical_interactions():
    graph_script = Path(
        "src/collabvet_review_app/static/clinical_graph.js"
    ).read_text(encoding="utf-8")
    template = Path(
        "src/collabvet_review_app/templates/clinical_insights.html"
    ).read_text(encoding="utf-8")

    for marker in (
        "data-graph-view=\"overview\"",
        "data-graph-view=\"pathway\"",
        "data-graph-view=\"timeline\"",
        "data-graph-view=\"review\"",
        "data-graph-view=\"network\"",
        "data-graph-mode=\"corpus\"",
    ):
        assert marker in template
    for behavior in (
        "projectionLabel",
        "is_temporal_only",
        "requestFullscreen",
        "pointerdown",
        "data-network-fit",
        "data-review-action",
        "Patient name not linked",
        "hasDisplayIdentity",
        "identity_mapping_status",
        'setAttribute("visibility"',
        "Sequence does not prove causation",
    ):
        assert behavior in graph_script
    assert "bolt://" not in graph_script
    assert "neo4j://" not in graph_script
    assert "NEO4J_" not in graph_script
    assert "/api/v1/app-admin/" not in graph_script


def configure_insights_service(app):
    app.config.update(
        COLLABVET_API_BASE_URL="https://api.example",
        COLLABVET_REVIEW_CLIENT_ID="collabvet-review",
        COLLABVET_REVIEW_CLIENT_SECRET="server-only-test-secret",
    )
    clear_token_cache()


def test_clinical_insights_missing_service_auth_fails_gracefully(app):
    client = app.test_client()
    login(client)
    response = client.get("/clinical-insights/api/overview")
    assert response.status_code == 503
    assert b"service credentials are not configured" in response.data


def test_clinical_insights_uses_exact_client_credentials_and_caches(app, monkeypatch):
    configure_insights_service(app)
    calls = []

    def fake_request(base_url, path, *, token=None, method="GET", body=None):
        calls.append(
            {
                "base_url": base_url,
                "path": path,
                "token": token,
                "method": method,
                "body": body,
            }
        )
        if path == review_insights.TOKEN_PATH:
            return {"access_token": "test-bearer", "expires_in": 900}
        assert token == "test-bearer"
        return {"totals": {"cases": 2}}

    monkeypatch.setattr(review_insights, "_request_json", fake_request)
    with app.app_context():
        path = "/api/v1/review/clinical-insights/overview"
        assert clinical_insights_request(path, {})["totals"]["cases"] == 2
        assert clinical_insights_request(path, {})["totals"]["cases"] == 2

    exchanges = [call for call in calls if call["path"] == review_insights.TOKEN_PATH]
    assert len(exchanges) == 1
    assert exchanges[0] == {
        "base_url": "https://api.example",
        "path": "/api/v1/review/auth/token",
        "token": None,
        "method": "POST",
        "body": {
            "grant_type": "client_credentials",
            "client_id": "collabvet-review",
            "client_secret": "server-only-test-secret",
            "scope": "clinical_insights:read",
        },
    }


def test_clinical_insights_refreshes_expired_token(app, monkeypatch):
    configure_insights_service(app)
    clock = [100.0]
    token_count = 0

    def fake_request(base_url, path, *, token=None, method="GET", body=None):
        nonlocal token_count
        if path == review_insights.TOKEN_PATH:
            token_count += 1
            return {"access_token": f"token-{token_count}"}
        return {"token_used": token}

    monkeypatch.setattr(review_insights.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(review_insights, "_request_json", fake_request)
    with app.app_context():
        path = "/api/v1/review/clinical-insights/overview"
        assert clinical_insights_request(path, {})["token_used"] == "token-1"
        clock[0] += review_insights.TOKEN_CACHE_SECONDS + 1
        assert clinical_insights_request(path, {})["token_used"] == "token-2"
    assert token_count == 2


def test_clinical_insights_retries_once_after_unauthorized(app, monkeypatch):
    configure_insights_service(app)
    token_count = 0
    resource_count = 0

    def fake_request(base_url, path, *, token=None, method="GET", body=None):
        nonlocal token_count, resource_count
        if path == review_insights.TOKEN_PATH:
            token_count += 1
            return {"access_token": f"token-{token_count}"}
        resource_count += 1
        if resource_count == 1:
            raise InsightsAPIError("expired", 401)
        return {"token_used": token}

    monkeypatch.setattr(review_insights, "_request_json", fake_request)
    with app.app_context():
        result = clinical_insights_request(
            "/api/v1/review/clinical-insights/overview",
            {},
        )
    assert result["token_used"] == "token-2"
    assert (token_count, resource_count) == (2, 2)


def test_clinical_insights_does_not_loop_after_second_unauthorized(app, monkeypatch):
    configure_insights_service(app)
    calls = {"token": 0, "resource": 0}

    def fake_request(base_url, path, *, token=None, method="GET", body=None):
        if path == review_insights.TOKEN_PATH:
            calls["token"] += 1
            return {"access_token": f"token-{calls['token']}"}
        calls["resource"] += 1
        raise InsightsAPIError("unauthorized", 401)

    monkeypatch.setattr(review_insights, "_request_json", fake_request)
    with app.app_context(), pytest.raises(InsightsAPIError) as caught:
        clinical_insights_request(
            "/api/v1/review/clinical-insights/overview",
            {},
        )
    assert caught.value.status == 503
    assert calls == {"token": 2, "resource": 2}


def test_clinical_insights_serializes_concurrent_token_exchange(app, monkeypatch):
    configure_insights_service(app)
    exchange_count = 0
    exchange_started = threading.Event()

    def fake_request(base_url, path, *, token=None, method="GET", body=None):
        nonlocal exchange_count
        if path == review_insights.TOKEN_PATH:
            exchange_count += 1
            exchange_started.set()
            time.sleep(0.03)
            return {"access_token": "shared-token"}
        return {"ok": token == "shared-token"}

    monkeypatch.setattr(review_insights, "_request_json", fake_request)

    def worker():
        with app.app_context():
            return clinical_insights_request(
                "/api/v1/review/clinical-insights/overview",
                {},
            )

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(worker) for _ in range(6)]
        assert exchange_started.wait(timeout=1)
        assert all(future.result()["ok"] for future in futures)
    assert exchange_count == 1


def test_clinical_insights_rejects_bad_service_auth_and_token_shape(app, monkeypatch):
    configure_insights_service(app)

    def rejected(*args, **kwargs):
        raise InsightsAPIError("unauthorized", 401)

    monkeypatch.setattr(review_insights, "_request_json", rejected)
    with app.app_context(), pytest.raises(InsightsAPIError) as rejected_error:
        clinical_insights_request(
            "/api/v1/review/clinical-insights/overview",
            {},
        )
    assert rejected_error.value.status == 503
    assert "server-only-test-secret" not in str(rejected_error.value)

    clear_token_cache()
    monkeypatch.setattr(review_insights, "_request_json", lambda *args, **kwargs: {})
    with app.app_context(), pytest.raises(InsightsAPIError) as malformed_error:
        clinical_insights_request(
            "/api/v1/review/clinical-insights/overview",
            {},
        )
    assert malformed_error.value.status == 503
    assert "invalid response" in str(malformed_error.value)


def test_clinical_insights_rejects_other_upstream_namespaces(app):
    with app.app_context(), pytest.raises(InsightsAPIError) as caught:
        clinical_insights_request(
            "/api/v1/app-admin/clinical-insights/overview",
            {},
        )
    assert caught.value.status == 500


def test_clinical_insights_preserves_rate_limit_retry_after(app, monkeypatch):
    def rate_limited(path, query):
        raise InsightsAPIError("Slow down.", 429, retry_after="17")

    monkeypatch.setattr(
        "collabvet_review_app.routes.clinical_insights_request",
        rate_limited,
    )
    client = app.test_client()
    login(client)
    response = client.get("/clinical-insights/api/overview")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "17"
    assert b"server-only-test-secret" not in response.data

    def forbidden(path, query):
        raise InsightsAPIError("Wrong scope.", 403)

    monkeypatch.setattr(
        "collabvet_review_app.routes.clinical_insights_request",
        forbidden,
    )
    response = client.get("/clinical-insights/api/overview")
    assert response.status_code == 403
    assert b"Wrong scope" in response.data


def test_every_insights_tab_search_resets_its_own_pagination():
    script = Path("src/collabvet_review_app/static/clinical_insights.js").read_text(
        encoding="utf-8"
    )
    assert 'input.dataset.searchTab' in script
    assert 'state.offsets[tab] = 0' in script
    assert 'data-search-tab="${tab}"' in script
    assert "previous.abort()" in script


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


def test_case_detail_uses_full_review_identity_not_signalment_first_name(app):
    write_external_case(app)
    with app.app_context():
        index_cases()
        record_id = CaseRecord.query.one().id

    client = app.test_client()
    login(client)
    response = client.get(f"/cases/{record_id}")

    assert response.status_code == 200
    assert b"<dt>Patient</dt><dd>Synthetic One</dd>" in response.data
    assert b"<dt>Patient</dt><dd>Demo</dd>" not in response.data


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


def test_document_resolver_uses_canonical_source_and_history_alias(app, tmp_path: Path):
    clinical_root = tmp_path / "clinical-data"
    source_root = clinical_root / "source"
    cases_root = clinical_root / "cases"
    patient_root = source_root / "Original Alias"
    patient_root.mkdir(parents=True)
    cases_root.mkdir()
    (patient_root / "history_form.json").write_bytes(b'{"safe": true}')
    (patient_root / "Original Alias History Form.pdf").write_bytes(b"%PDF-history")
    (patient_root / "clinicalSummary_demo.pdf").write_bytes(b"%PDF-clinical")
    (patient_root / "Original Alias MEDFILES.pdf").write_bytes(b"%PDF-medfiles")
    case = synthetic_case("v02-display-alias")
    case["patient_folder"] = "Display Alias"
    case["history_form"]["patient_folder"] = "Original Alias"
    (cases_root / "v02-display-alias.json").write_text(json.dumps(case), encoding="utf-8")
    app.config.update(
        {
            "CLINICAL_DATA_ROOT": clinical_root,
            "INPUT_ROOT": cases_root,
            "SOURCE_ROOT": tmp_path / "retired-source-location",
        }
    )
    with app.app_context():
        index_cases()
        record_id = CaseRecord.query.one().id
    client = app.test_client()
    login(client)
    history = client.get(f"/cases/{record_id}/documents/history")
    clinical = client.get(f"/cases/{record_id}/documents/clinical_summary")
    medfiles = client.get(f"/cases/{record_id}/documents/medfiles")
    assert history.status_code == 200
    assert history.get_json() == {"safe": True}
    assert clinical.status_code == 200
    assert clinical.data == b"%PDF-clinical"
    assert medfiles.status_code == 200
    assert medfiles.data == b"%PDF-medfiles"


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


def configure_dashboard_inventory(app, tmp_path: Path) -> dict[str, Path]:
    clinical_root = tmp_path / "clinical-data"
    source_root = clinical_root / "source"
    input_root = clinical_root / "cases"
    raw_root = clinical_root / "raw"
    config_root = tmp_path / "dashboard-config"
    output_root = tmp_path / "dashboard-output"
    for path in (source_root, input_root, raw_root, config_root, output_root):
        path.mkdir(parents=True, exist_ok=True)
    (raw_root / "do-not-read.txt").write_text("raw-secret-name", encoding="utf-8")

    def source_patient(
        name: str,
        *,
        history=True,
        clinical=True,
        medfiles=True,
        clinical_filename="clinicalSummary_demo.pdf",
    ):
        folder = source_root / name
        folder.mkdir()
        if history:
            (folder / "history_form.json").write_text("{}", encoding="utf-8")
        if clinical:
            (folder / clinical_filename).write_bytes(b"synthetic")
        if medfiles:
            (folder / "patient_medfiles.pdf").write_bytes(b"synthetic")

    source_patient("Alpha Ready")
    source_patient("Beta New")
    source_patient("Gamma Partial", medfiles=False, clinical_filename="Patient CH.pdf")
    source_patient("Delta Ready")

    def case(patient: str, case_id: str, *, model: str):
        data = synthetic_case(case_id)
        data["patient_folder"] = patient
        data["source"] = {
            "medfiles_model": model,
            "cs_model": model,
            "medfiles_prompt_version": "3.0.0-full",
            "cs_prompt_version": "3.2.1-full",
        }
        (input_root / f"{case_id}.json").write_text(json.dumps(data), encoding="utf-8")

    case("Alpha Ready", "v02-alpha-ready", model="legacy-model")
    case("Beta New", "v02-beta-new", model="gpt-5.6-sol")
    case("Orphan Case", "v02-orphan-case", model="gpt-5.6-sol")

    legacy = {
        "version": 2,
        "train_count": 1,
        "val_count": 0,
        "patients": [
            {
                "patient_folder": "Alpha Ready",
                "case_id": "v02-alpha-ready",
                "split": "train",
            }
        ],
    }
    approved = {
        "version": 1,
        "case_count": 1,
        "patients": [{"patient_folder": "Alpha Ready", "case_id": "v02-alpha-ready"}],
    }
    (config_root / "v02_train_manifest.json").write_text(
        json.dumps(legacy), encoding="utf-8"
    )
    (config_root / "v04_approved_train_manifest.json").write_text(
        json.dumps(approved), encoding="utf-8"
    )
    (config_root / "holdout_golden.json").write_text(
        json.dumps({"case_ids": []}), encoding="utf-8"
    )
    (config_root / "golden_regression_cases.json").write_text(
        json.dumps({"cases": []}), encoding="utf-8"
    )

    app.config.update(
        {
            "CLINICAL_DATA_ROOT": clinical_root,
            "SOURCE_ROOT": source_root,
            "INPUT_ROOT": input_root,
            "CONFIG_ROOT": config_root,
            "OUTPUT_ROOT": output_root,
        }
    )
    with app.app_context():
        assert index_cases()["created"] == 3
        alpha = CaseRecord.query.filter_by(case_id="v02-alpha-ready").one()
        alpha.status = "approved"
        approved_case = output_root / "cases" / "v02-alpha-ready.approved.json"
        approved_review = output_root / "reviews" / "v02-alpha-ready.review.json"
        approved_case.parent.mkdir(parents=True)
        approved_review.parent.mkdir(parents=True)
        approved_case.write_text("{}", encoding="utf-8")
        approved_review.write_text("{}", encoding="utf-8")
        exported = ApprovedExport(
            case_id=alpha.id,
            source_hash=alpha.source_hash,
            approved_path=str(approved_case),
            review_path=str(approved_review),
            approved_by_id=User.query.one().id,
        )
        db.session.add(exported)
        db.session.commit()
    clear_dashboard_cache()
    return {
        "clinical": clinical_root,
        "source": source_root,
        "input": input_root,
        "raw": raw_root,
        "config": config_root,
        "output": output_root,
    }


def test_data_dashboard_inventory_metrics_filters_and_drilldown(
    app, tmp_path: Path, monkeypatch
):
    paths = configure_dashboard_inventory(app, tmp_path)
    original_iterdir = Path.iterdir

    def guarded_iterdir(path):
        assert path.resolve() != paths["raw"].resolve(), "raw/ must never be enumerated"
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
    with app.app_context():
        overview = dashboard_overview()
        assert overview["metrics"] == {
            "raw": None,
            "pii_removed": 4,
            "process_ready": 3,
            "longitudinal": 3,
            "legacy_training": 1,
            "legacy_train_split": 1,
            "legacy_validation_split": 0,
            "new": 2,
            "clinician_approved": 1,
            "future_training": 1,
            "actually_trained": None,
        }
        assert overview["readiness"] == {
            "history": 4,
            "clinical_summary": 4,
            "medfiles": 3,
            "complete": 3,
        }
        assert overview["opportunities"] == {
            "ready_without_longitudinal": 1,
            "new_longitudinal": 2,
            "awaiting_review": 2,
            "approved_not_in_future_manifest": 0,
            "broken": 1,
        }
        new_rows = dashboard_patients(stage="new", sort="-visit_count")
        assert new_rows["total"] == 2
        beta = dashboard_patients(query="gpt-5.6-sol", stage="new", limit=1)
        assert beta["total"] == 2
        assert len(beta["items"]) == 1
        detail = dashboard_patient(
            next(row["key"] for row in new_rows["items"] if row["case_id"] == "v02-beta-new")
        )
        assert detail["pseudonym"] == "Beta New"
        assert len(detail["timeline"]) == 6
        serialized = json.dumps({"overview": overview, "detail": detail})
        assert str(paths["raw"]) not in serialized
        assert "raw-secret-name" not in serialized
        assert "Caregiver reports barking" not in serialized


def test_data_dashboard_routes_auth_refresh_and_page_controls(app, tmp_path: Path):
    configure_dashboard_inventory(app, tmp_path)
    client = app.test_client()
    for path in (
        "/data-dashboard",
        "/data-dashboard/api/overview",
        "/data-dashboard/api/patients",
        "/data-dashboard/api/patients/not-a-key",
    ):
        assert client.get(path).status_code == 302
    assert client.post("/data-dashboard/api/refresh").status_code == 302

    login(client)
    page = client.get("/data-dashboard")
    assert page.status_code == 200
    for marker in (
        b"Data Dashboard",
        b"Raw inventory is not connected",
        b'id="patient-search"',
        b'id="patient-stage"',
        b'id="patient-sort"',
        b'id="patient-detail"',
        b"data_dashboard.js",
    ):
        assert marker in page.data
    overview = client.get("/data-dashboard/api/overview")
    assert overview.status_code == 200
    patients = client.get(
        "/data-dashboard/api/patients?q=Beta&stage=new&sort=pseudonym&offset=0&limit=10"
    )
    assert patients.status_code == 200
    assert patients.get_json()["total"] == 1
    first_refresh = client.post("/data-dashboard/api/refresh")
    assert first_refresh.status_code == 200
    second_refresh = client.post("/data-dashboard/api/refresh")
    assert second_refresh.status_code == 429
    assert int(second_refresh.headers["Retry-After"]) > 0
    with app.app_context():
        assert AuditEvent.query.filter_by(event_type="data_dashboard_refreshed").count() == 1


def test_data_dashboard_missing_manifest_and_stale_review_are_visible(app, tmp_path: Path):
    paths = configure_dashboard_inventory(app, tmp_path)
    (paths["config"] / "v04_approved_train_manifest.json").unlink()
    beta_path = paths["input"] / "v02-beta-new.json"
    beta = json.loads(beta_path.read_text(encoding="utf-8"))
    beta["visits"][0]["subjective"] = "Changed after indexing."
    beta_path.write_text(json.dumps(beta), encoding="utf-8")
    clear_dashboard_cache()

    with app.app_context():
        overview = dashboard_overview()
        assert any("Clinician-approved training manifest is unavailable" in warning
                   for warning in overview["meta"]["warnings"])
        broken = dashboard_patients(stage="broken", limit=100)
        beta_row = next(row for row in broken["items"] if row["case_id"] == "v02-beta-new")
        assert "stale_review_source_hash" in beta_row["issues"]

