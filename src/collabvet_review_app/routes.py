"""HTTP routes for clinician review."""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

import fitz
import jsonpatch
from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import or_

from collabvet_review_app.data_dashboard import (
    DashboardPatientNotFound,
    DashboardRefreshLimited,
    dashboard_overview,
    dashboard_patient,
    dashboard_patients,
)
from collabvet_review_app.insights import InsightsAPIError, clinical_insights_request
from collabvet_review_app.models import (
    CaseRecord,
    ReviewComment,
    SectionReview,
    User,
    db,
    utcnow,
)
from collabvet_review_app.security import rate_limit
from collabvet_review_app.services import (
    add_revision,
    approval_blockers,
    audit,
    build_review_timeline,
    document_paths,
    export_approved,
    materialize_case,
    normalize_for_review,
    record_comments,
    required_section_keys,
    revision_diff_rows,
    stage_examples,
    update_section,
    validate_longitudinal,
)
from collabvet_review_app.training.prompts import SYSTEM_BY_STAGE

bp = Blueprint("review", __name__)

INSIGHTS_RESOURCES = {
    "overview": "/api/v1/review/clinical-insights/overview",
    "patterns": "/api/v1/review/clinical-insights/patterns",
    "pathways": "/api/v1/review/clinical-insights/pathways",
    "safety": "/api/v1/review/clinical-insights/safety",
    "interventions": "/api/v1/review/clinical-insights/interventions",
    "runs": "/api/v1/review/clinical-insights/runs",
    "cases": "/api/v1/review/clinical-insights/cases",
    "knowledge": "/api/v1/review/clinical-insights/knowledge",
}
INSIGHTS_QUERY_KEYS = {"run_id", "vb_id", "offset", "limit", "q"}
GRAPH_PREFIX = "/api/v1/review/clinical-insights/graph"
GRAPH_CASE_QUERY_KEYS = {
    "q",
    "offset",
    "limit",
    "source_vb_id",
    "mining_run_id",
    "review_state",
    "include_quarantined",
}
GRAPH_GLOBAL_QUERY_KEYS = {
    "stage",
    "source_vb_id",
    "mining_run_id",
    "review_state",
    "include_quarantined",
    "include_machine_only",
    "aggregation_level",
    "max_nodes",
}


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("review.queue"))
    if request.method == "POST":
        key = f"login:{request.remote_addr or 'unknown'}"
        if not rate_limit(key, limit=10, window_seconds=300):
            abort(429)
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email, active=True).one_or_none()
        if user and user.check_password(request.form.get("password", "")):
            login_user(user, remember=False)
            audit("login_success", user=user)
            next_url = request.args.get("next")
            if next_url and next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)
            return redirect(url_for("review.queue"))
        audit("login_failure", detail={"email": email[:64]})
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@bp.post("/logout")
@login_required
def logout():
    audit("logout", user=current_user)
    logout_user()
    return redirect(url_for("review.login"))


@bp.get("/")
@login_required
def queue():
    query = CaseRecord.query
    reviewers = (
        User.query.filter_by(active=True, role="reviewer")
        .order_by(User.display_name)
        .all()
    )
    status = request.args.get("status", "").strip()
    if status:
        query = query.filter_by(status=status)
    holdout = request.args.get("holdout", "").strip()
    if holdout in {"yes", "no"}:
        query = query.filter_by(is_holdout=holdout == "yes")
    assigned = request.args.get("assigned", "").strip()
    if assigned == "me":
        query = query.filter_by(assigned_user_id=current_user.id)
    elif assigned == "unassigned":
        query = query.filter(CaseRecord.assigned_user_id.is_(None))
    elif assigned.isdigit():
        query = query.filter_by(assigned_user_id=int(assigned))
    search = request.args.get("q", "").strip()
    if search:
        query = query.filter(
            or_(
                CaseRecord.patient_folder.ilike(f"%{search}%"),
                CaseRecord.case_id.ilike(f"%{search}%"),
            )
        )
    cases = query.order_by(CaseRecord.status, CaseRecord.patient_folder).all()
    return render_template("queue.html", cases=cases, reviewers=reviewers)


@bp.get("/clinical-insights")
@login_required
def clinical_insights():
    return render_template("clinical_insights.html")


@bp.get("/data-dashboard")
@login_required
def data_dashboard():
    return render_template("data_dashboard.html")


@bp.get("/data-dashboard/api/overview")
@login_required
def data_dashboard_overview():
    try:
        return jsonify(dashboard_overview())
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503


@bp.get("/data-dashboard/api/patients")
@login_required
def data_dashboard_patients():
    offset = request.args.get("offset", default=0, type=int)
    limit = request.args.get("limit", default=25, type=int)
    return jsonify(
        dashboard_patients(
            query=request.args.get("q", ""),
            stage=request.args.get("stage", "all"),
            sort=request.args.get("sort", "pseudonym"),
            offset=offset if offset is not None else 0,
            limit=limit if limit is not None else 25,
        )
    )


@bp.get("/data-dashboard/api/patients/<key>")
@login_required
def data_dashboard_patient(key: str):
    try:
        return jsonify(dashboard_patient(key))
    except DashboardPatientNotFound:
        abort(404)


@bp.post("/data-dashboard/api/refresh")
@login_required
def data_dashboard_refresh():
    try:
        payload = dashboard_overview(force=True)
    except DashboardRefreshLimited as exc:
        response = jsonify({"error": str(exc), "retry_after": exc.retry_after})
        response.status_code = 429
        response.headers["Retry-After"] = str(exc.retry_after)
        return response
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    audit("data_dashboard_refreshed", user=current_user)
    return jsonify(payload)


def _insights_json(
    path: str,
    *,
    allowed_query_keys: set[str] | None = None,
    method: str = "GET",
    body: dict[str, Any] | None = None,
):
    allowed_query_keys = allowed_query_keys or INSIGHTS_QUERY_KEYS
    query = {
        key: value
        for key, value in request.args.items()
        if key in allowed_query_keys and value != ""
    }
    try:
        if method == "GET" and body is None:
            payload = clinical_insights_request(path, query)
        else:
            payload = clinical_insights_request(
                path,
                query,
                method=method,
                body=body,
            )
        return jsonify(payload)
    except InsightsAPIError as exc:
        response = jsonify({"error": str(exc), "status": exc.status})
        response.status_code = exc.status
        if exc.retry_after:
            response.headers["Retry-After"] = exc.retry_after
        return response


@bp.get("/clinical-insights/api/<resource>")
@login_required
def clinical_insights_api(resource: str):
    path = INSIGHTS_RESOURCES.get(resource)
    if path is None:
        abort(404)
    return _insights_json(path)


@bp.get("/clinical-insights/api/metrics/<metric>")
@login_required
def clinical_insights_metric(metric: str):
    allowed = {
        "medications",
        "adverse_effects",
        "training_support",
        "provider_caution",
        "improvements",
        "follow_up_outcomes",
    }
    if metric not in allowed:
        abort(404)
    return _insights_json(f"/api/v1/review/clinical-insights/metrics/{metric}")


@bp.get("/clinical-insights/api/cases/<case_id>")
@login_required
def clinical_insights_case(case_id: str):
    safe_id = urllib.parse.quote(case_id, safe="")
    return _insights_json(f"/api/v1/review/clinical-insights/cases/{safe_id}")


@bp.get("/clinical-insights/api/pathway-comparisons")
@login_required
def clinical_insights_comparisons():
    return _insights_json(
        "/api/v1/review/clinical-insights/pathway-comparisons"
    )


@bp.get("/clinical-insights/api/graph/summary")
@login_required
def clinical_graph_summary():
    return _insights_json(f"{GRAPH_PREFIX}/summary", allowed_query_keys=set())


@bp.get("/clinical-insights/api/graph/vocabulary")
@login_required
def clinical_graph_vocabulary():
    return _insights_json(f"{GRAPH_PREFIX}/vocabulary", allowed_query_keys=set())


@bp.get("/clinical-insights/api/graph/cases")
@login_required
def clinical_graph_cases():
    return _insights_json(
        f"{GRAPH_PREFIX}/cases",
        allowed_query_keys=GRAPH_CASE_QUERY_KEYS,
    )


@bp.get("/clinical-insights/api/graph/global-graph")
@login_required
def clinical_global_graph():
    return _insights_json(
        f"{GRAPH_PREFIX}/global-graph",
        allowed_query_keys=GRAPH_GLOBAL_QUERY_KEYS,
    )


GRAPH_CASE_VIEWS = {
    "relationships": {"include_quarantined"},
    "pathway": {"include_quarantined"},
    "timeline": set(),
    "review-table": {
        "q",
        "offset",
        "limit",
        "review_state",
        "stage",
        "include_quarantined",
    },
    "versions": set(),
    "history": {"target_kind", "target_key"},
}


@bp.get("/clinical-insights/api/graph/cases/<case_id>/<view>")
@login_required
def clinical_graph_case_view(case_id: str, view: str):
    allowed = GRAPH_CASE_VIEWS.get(view)
    if allowed is None:
        abort(404)
    safe_id = urllib.parse.quote(case_id, safe="")
    return _insights_json(
        f"{GRAPH_PREFIX}/cases/{safe_id}/{view}",
        allowed_query_keys=allowed,
    )


def _validated_graph_review_payload() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400, "A JSON review decision is required.")
    allowed_actions = {
        "confirm",
        "correct",
        "mark_uncertain",
        "request_evidence",
        "annotate",
        "retire",
        "restore",
    }
    target_kind = payload.get("target_kind")
    target_key = payload.get("target_key")
    action = payload.get("action")
    if target_kind not in {"node", "edge"}:
        abort(400, "Invalid review target.")
    if not isinstance(target_key, str) or not 1 <= len(target_key) <= 300:
        abort(400, "Invalid review target key.")
    if action not in allowed_actions:
        abort(400, "Invalid review action.")
    cleaned: dict[str, Any] = {
        "target_kind": target_kind,
        "target_key": target_key,
        "action": action,
    }
    rationale = payload.get("rationale")
    if rationale is not None:
        if not isinstance(rationale, str) or len(rationale) > 2000:
            abort(400, "Invalid review rationale.")
        cleaned["rationale"] = rationale.strip()
    expected_hash = payload.get("expected_payload_hash")
    if expected_hash is not None:
        if not isinstance(expected_hash, str) or len(expected_hash) > 256:
            abort(400, "Invalid expected payload hash.")
        cleaned["expected_payload_hash"] = expected_hash
    if action == "annotate":
        annotation = payload.get("annotation")
        if not isinstance(annotation, str) or not annotation.strip():
            abort(400, "An annotation is required.")
        cleaned["annotation"] = annotation.strip()[:4000]
    if action == "correct":
        replacement = payload.get("replacement")
        field = "headline" if target_kind == "node" else "relationship_label"
        if (
            not isinstance(replacement, dict)
            or not isinstance(replacement.get(field), str)
            or not replacement[field].strip()
        ):
            abort(400, "Corrected wording is required.")
        cleaned["replacement"] = {field: replacement[field].strip()[:1000]}
    return cleaned


@bp.post("/clinical-insights/api/graph/cases/<case_id>/review")
@login_required
def clinical_graph_review(case_id: str):
    safe_id = urllib.parse.quote(case_id, safe="")
    payload = _validated_graph_review_payload()
    response = _insights_json(
        f"{GRAPH_PREFIX}/cases/{safe_id}/review",
        allowed_query_keys=set(),
        method="POST",
        body=payload,
    )
    if response.status_code < 400:
        audit(
            "clinical_graph_review_submitted",
            user=current_user,
            detail={
                "case_id": case_id,
                "target_kind": payload["target_kind"],
                "action": payload["action"],
            },
        )
    return response


def _record_or_404(record_id: int) -> CaseRecord:
    record = db.session.get(CaseRecord, record_id)
    if record is None:
        abort(404)
    return record


@bp.get("/cases/<int:record_id>")
@login_required
def case_overview(record_id: int):
    record = _record_or_404(record_id)
    try:
        data = normalize_for_review(materialize_case(record))
    except (OSError, ValueError, RuntimeError) as exc:
        flash(str(exc), "error")
        data = {}
    reviews = {
        row.section_key: row
        for row in SectionReview.query.filter_by(case_id=record.id).all()
    }
    issues = validate_longitudinal(data) if data else []
    documents = document_paths(record) if data else {}
    examples, _ = stage_examples(record) if data else ({"A": [], "B": [], "C": [], "D": []}, None)
    audit("case_viewed", record=record, user=current_user)
    return render_template(
        "case.html",
        record=record,
        case=data,
        sections=required_section_keys(data) if data else [],
        reviews=reviews,
        issues=issues,
        documents=documents,
        comments=record_comments(record),
        stage_examples=examples,
        timeline=build_review_timeline(data, reviews) if data else [],
        reviewers=(
            User.query.filter_by(active=True, role="reviewer")
            .order_by(User.display_name)
            .all()
        ),
    )


@bp.post("/cases/<int:record_id>/assign")
@login_required
def assign_case(record_id: int):
    record = _record_or_404(record_id)
    assignee_id = request.form.get("assignee_id", "").strip()
    if assignee_id == "self":
        assignee = current_user
    elif not assignee_id:
        assignee = None
    elif assignee_id.isdigit():
        assignee = User.query.filter_by(
            id=int(assignee_id), active=True, role="reviewer"
        ).one_or_none()
        if assignee is None:
            abort(400, "Unknown reviewer")
    else:
        abort(400, "Invalid reviewer")
    record.assigned_user_id = assignee.id if assignee else None
    if record.status == "pending":
        record.status = "in_review"
    db.session.commit()
    audit(
        "case_assigned",
        record=record,
        user=current_user,
        detail={"assignee": assignee.email if assignee else None},
    )
    flash(
        f"Assigned to {assignee.display_name}." if assignee else "Case unassigned.",
        "success",
    )
    return redirect(url_for("review.case_overview", record_id=record.id))


def section_pointer(section_key: str) -> str:
    mapping = {
        "signalment": "/signalment",
        "history_form": "/history_form",
        "medical_digest": "/pre_intake_medical_digest",
        "communications": "/communications",
        "supplements": "/medical_record_supplements",
    }
    if section_key in mapping:
        return mapping[section_key]
    parts = section_key.split(":")
    if len(parts) == 3 and parts[0] == "visit" and parts[1].isdigit():
        field = parts[2]
        if field in {
            "subjective",
            "objective",
            "assessment",
            "behavior_diagnoses",
            "plan",
            "medication_changes",
            "interval_summary",
        }:
            return f"/visits/{parts[1]}/{field}"
    raise ValueError("Unknown section")


def pointer_value(data: Any, pointer: str) -> Any:
    return jsonpatch.JsonPointer(pointer).resolve(data)


def _coerce(raw: str, original: Any) -> Any:
    if isinstance(original, bool):
        return raw.lower() in {"true", "1", "yes", "on"}
    if isinstance(original, int) and not isinstance(original, bool):
        return int(raw)
    if isinstance(original, float):
        return float(raw)
    if original is None:
        if raw.strip() == "":
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    if isinstance(original, (list, dict)):
        return json.loads(raw)
    return raw


@bp.route("/cases/<int:record_id>/review/<path:section_key>", methods=["GET", "POST"])
@login_required
def review_section(record_id: int, section_key: str):
    record = _record_or_404(record_id)
    raw_data = materialize_case(record)
    data = normalize_for_review(raw_data)
    try:
        pointer = section_pointer(section_key)
        section = pointer_value(data, pointer)
    except (ValueError, jsonpatch.JsonPointerException):
        abort(404)
    if request.method == "POST":
        base_revision = int(request.form.get("base_revision", "-1"))
        reason = request.form.get("reason", "").strip()
        mode = request.form.get("mode", "fields")
        try:
            if mode == "raw":
                updated = json.loads(request.form.get("raw_json", ""))
                patch = jsonpatch.make_patch(section, updated).patch
                patch = [
                    {
                        **op,
                        "path": pointer + op["path"] if op["path"] else pointer,
                        **(
                            {"from": pointer + op["from"] if op.get("from") else pointer}
                            if "from" in op
                            else {}
                        ),
                    }
                    for op in patch
                ]
            else:
                patch = []
                for key, raw in request.form.items():
                    if not key.startswith("field|"):
                        continue
                    field_pointer = key.split("|", 1)[1]
                    original = pointer_value(data, field_pointer)
                    value = _coerce(raw, original)
                    if value != original:
                        try:
                            pointer_value(raw_data, field_pointer)
                            operation = "replace"
                        except jsonpatch.JsonPointerException:
                            operation = "add"
                        patch.append({"op": operation, "path": field_pointer, "value": value})
            if not patch:
                flash("No changes detected.", "info")
            else:
                add_revision(
                    record,
                    patch,
                    base_revision=base_revision,
                    author=current_user,
                    reason=reason,
                )
                flash("Changes saved; affected approvals were reset.", "success")
            return redirect(
                url_for(
                    "review.review_section",
                    record_id=record.id,
                    section_key=section_key,
                )
            )
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            flash(str(exc), "error")
    review = SectionReview.query.filter_by(
        case_id=record.id, section_key=section_key
    ).one_or_none()
    audit("section_viewed", record=record, user=current_user, detail={"section": section_key})
    return render_template(
        "review_section.html",
        record=record,
        case=data,
        section_key=section_key,
        pointer=pointer,
        section=section,
        raw_json=json.dumps(section, ensure_ascii=False, indent=2),
        review=review,
        documents=document_paths(record),
    )


@bp.post("/cases/<int:record_id>/sections/<path:section_key>/status")
@login_required
def section_status(record_id: int, section_key: str):
    record = _record_or_404(record_id)
    try:
        update_section(
            record,
            section_key,
            request.form.get("status", ""),
            request.form.get("comment", "").strip(),
            current_user,
            base_revision=int(request.form.get("base_revision", "-1")),
        )
        flash("Section status updated.", "success")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "error")
    if section_key.startswith("stage:"):
        return redirect(
            url_for(
                "review.stage_preview",
                record_id=record.id,
                stage=section_key.removeprefix("stage:"),
            )
        )
    return redirect(url_for("review.review_section", record_id=record.id, section_key=section_key))


@bp.post("/cases/<int:record_id>/comments")
@login_required
def add_comment(record_id: int):
    record = _record_or_404(record_id)
    body = request.form.get("body", "").strip()
    if body:
        comment = ReviewComment(
            case_id=record.id,
            section_key=request.form.get("section_key", "case"),
            body=body,
            blocking=request.form.get("blocking") == "on",
            author_id=current_user.id,
        )
        db.session.add(comment)
        db.session.commit()
        audit("comment_added", record=record, user=current_user)
    return redirect(url_for("review.case_overview", record_id=record.id))


@bp.post("/comments/<int:comment_id>/resolve")
@login_required
def resolve_comment(comment_id: int):
    comment = db.session.get(ReviewComment, comment_id)
    if comment is None:
        abort(404)
    comment.resolved = True
    comment.resolved_at = utcnow()
    db.session.commit()
    audit("comment_resolved", record=comment.case, user=current_user)
    return redirect(url_for("review.case_overview", record_id=comment.case_id))


@bp.route("/cases/<int:record_id>/approve", methods=["GET", "POST"])
@login_required
def approve_case(record_id: int):
    record = _record_or_404(record_id)
    blockers = approval_blockers(record)
    if request.method == "POST" and not blockers:
        try:
            export_approved(record, current_user)
            flash("Case approved and exported.", "success")
            return redirect(url_for("review.case_overview", record_id=record.id))
        except ValueError as exc:
            blockers = str(exc).splitlines()
    return render_template(
        "approve.html",
        record=record,
        blockers=blockers,
        diff_rows=revision_diff_rows(record),
    )


@bp.get("/cases/<int:record_id>/preview/<stage>")
@login_required
def stage_preview(record_id: int, stage: str):
    if stage not in {"A", "B", "C", "D"}:
        abort(404)
    record = _record_or_404(record_id)
    grouped, artifact_hash = stage_examples(record)
    examples = grouped[stage]
    if stage == "D" and not examples:
        abort(404, "No follow-up visit")
    review = SectionReview.query.filter_by(
        case_id=record.id, section_key=f"stage:{stage}"
    ).one_or_none()
    audit(
        "stage_previewed",
        record=record,
        user=current_user,
        detail={"stage": stage, "examples": len(examples)},
    )
    return render_template(
        "preview.html",
        record=record,
        stage=stage,
        examples=[
            {
                "example_id": example.example_id,
                "visit_id": example.visit_id,
                "case_file": example.case_file,
                "expected_output": json.dumps(
                    json.loads(example.gold_output), ensure_ascii=False, indent=2
                ),
            }
            for example in examples
        ],
        system_prompt=SYSTEM_BY_STAGE[stage],
        review=review,
        artifact_hash=artifact_hash,
    )


@bp.get("/cases/<int:record_id>/documents/<kind>")
@login_required
def source_document(record_id: int, kind: str):
    record = _record_or_404(record_id)
    key = f"document:{current_user.id}:{request.remote_addr or 'unknown'}"
    if not rate_limit(key, limit=120, window_seconds=60):
        abort(429)
    path = document_paths(record).get(kind)
    if path is None:
        abort(404)
    audit("document_viewed", record=record, user=current_user, detail={"kind": kind})
    return send_file(
        path,
        mimetype="application/pdf" if path.suffix.lower() == ".pdf" else "application/json",
        as_attachment=False,
        conditional=True,
        download_name=f"{record.case_id}-{kind}{path.suffix.lower()}",
    )


@bp.get("/cases/<int:record_id>/documents/<kind>/search")
@login_required
def search_document(record_id: int, kind: str):
    record = _record_or_404(record_id)
    key = f"document-search:{current_user.id}:{request.remote_addr or 'unknown'}"
    if not rate_limit(key, limit=60, window_seconds=60):
        abort(429)
    path = document_paths(record).get(kind)
    query = request.args.get("q", "").strip()
    if path is None or path.suffix.lower() != ".pdf" or len(query) < 3:
        return jsonify({"pages": []})
    pages: list[int] = []
    with fitz.open(path) as document:
        for index, page in enumerate(document):
            if query.lower() in page.get_text().lower():
                pages.append(index + 1)
    return jsonify({"pages": pages[:50]})

