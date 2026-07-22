"""Canonical case-file text serialization (design doc §3.2)."""

from __future__ import annotations

import json

from collabvet_review_app.training.tokens import truncate_to_tokens


def _section(title: str, body: str) -> str:
    body = (body or "").strip()
    if not body:
        return ""
    return f"=== {title} ===\n{body}\n"


def _history_form_lines(history: dict) -> str:
    lines: list[str] = []
    for qa in history.get("qa_pairs") or []:
        label = qa.get("question") or qa.get("label") or qa.get("field_id") or "Q"
        val = qa.get("answer") or qa.get("value") or ""
        if str(val).strip():
            lines.append(f"{label}: {val}")
    return "\n".join(lines)


def _digest_lines(digest: dict, *, as_of_date: str | None, supplements: list[dict]) -> str:
    parts: list[str] = []
    meds = digest.get("medications") or []
    if meds:
        parts.append("Medications:")
        for m in meds[:40]:
            name = m.get("name") or m.get("medication") or "?"
            dose = m.get("dose") or m.get("directions") or ""
            parts.append(f"  - {name} {dose}".strip())
    labs = digest.get("lab_results") or []
    abnormal = [x for x in labs if (x.get("interpretation") or "").lower() not in ("", "normal", "wnl")]
    show_labs = abnormal[:20] if abnormal else labs[:10]
    if show_labs:
        parts.append("Labs (abnormal-first):")
        for lab in show_labs:
            parts.append(f"  - {lab.get('test_name') or lab.get('name')}: {lab.get('value')} {lab.get('units') or ''}")
    for ev in (digest.get("narrative_events") or [])[:25]:
        d = ev.get("date") or "?"
        parts.append(f"[{d}] {ev.get('summary') or ev.get('event') or ''}")
    for sup in supplements:
        recv = sup.get("received_date") or ""
        if as_of_date and recv and recv > as_of_date:
            continue
        for entry in sup.get("pre_intake_entries") or []:
            parts.append(f"[supplement {recv}] {entry}")
    return "\n".join(parts)


def _comms_lines(comms: list[dict], *, before_date: str | None) -> str:
    lines: list[str] = []
    for c in sorted(comms, key=lambda x: x.get("date") or ""):
        d = c.get("date") or "?"
        if before_date and d and d > before_date:
            continue
        party = c.get("party") or c.get("from_party") or "?"
        direction = c.get("direction") or "?"
        summary = c.get("summary") or ""
        excerpt = c.get("verbatim_excerpt") or ""
        body = excerpt if excerpt else summary
        if body:
            lines.append(f"[{d}, {party} {direction}] {truncate_to_tokens(body, 400)}")
    return "\n".join(lines)


def _triage_lines(qa_pairs: list[dict]) -> str:
    lines: list[str] = []
    for item in qa_pairs:
        q = item.get("question") or item.get("q") or ""
        a = item.get("answer") or item.get("a") or ""
        if q:
            lines.append(f"Q: {q}")
            if a:
                lines.append(f"A: {a}")
            else:
                lines.append("[UNANSWERED]")
    return "\n".join(lines)


def _prior_notes(visits: list[dict], *, before_idx: int, compact: bool) -> str:
    lines: list[str] = []
    for v in visits[:before_idx]:
        d = v.get("date") or "?"
        vtype = v.get("visit_type") or "visit"
        if compact:
            ap = v.get("assessment") or {}
            reasoning = ap.get("chris_final_reasoning") or ""
            plan = v.get("plan") or {}
            recs = plan.get("recommendations") or []
            lines.append(f"[{d}, {vtype}] A/P summary: {truncate_to_tokens(reasoning, 200)}")
            if recs:
                lines.append(f"  Plan: {'; '.join(recs[:5])}")
        else:
            lines.append(f"[{d}, {vtype}] S: {truncate_to_tokens(v.get('subjective') or '', 800)}")
            lines.append(f"O: {truncate_to_tokens(v.get('objective') or '', 400)}")
    return "\n".join(lines)


def _interval_lines(
    visits: list[dict],
    comms: list[dict],
    *,
    after_date: str,
    before_date: str,
) -> str:
    lines: list[str] = []
    for v in visits:
        d = v.get("date") or ""
        if d and after_date < d <= before_date:
            if v.get("interval_summary"):
                lines.append(f"[visit {d}] interval: {v.get('interval_summary')}")
    for c in comms:
        d = c.get("date") or ""
        if d and after_date < d <= before_date:
            lines.append(f"[{d} comm] {truncate_to_tokens(c.get('summary') or c.get('verbatim_excerpt') or '', 200)}")
    return "\n".join(lines)


def serialize_case_file(
    case: dict,
    *,
    stage: str,
    visit_index: int | None = None,
    triage_qa: list[dict] | None = None,
    draft_so: dict | None = None,
    as_of_date: str | None = None,
    max_section_tokens: int = 1500,
    compact_prior: bool = True,
) -> str:
    """Build canonical case-file text for a stage/time slice."""
    sig = case.get("signalment") or {}
    signalment = ", ".join(
        str(sig.get(k) or "")
        for k in ("pet_name", "species", "breed", "age", "sex")
        if sig.get(k)
    )
    history = case.get("history_form") or {}
    digest = case.get("pre_intake_medical_digest") or {}
    comms = case.get("communications") or []
    visits = case.get("visits") or []
    supplements = case.get("medical_record_supplements") or []

    intake_idx = next((i for i, v in enumerate(visits) if v.get("visit_type") == "intake"), 0)
    intake_date = (visits[intake_idx].get("date") if visits else None) or as_of_date or "9999-99-99"
    slice_date = as_of_date or intake_date

    parts: list[str] = []
    parts.append(_section("SIGNALMENT", signalment))
    parts.append(_section("HISTORY FORM", truncate_to_tokens(_history_form_lines(history), max_section_tokens)))
    parts.append(
        _section(
            "PRE-INTAKE MEDICAL RECORD DIGEST",
            truncate_to_tokens(_digest_lines(digest, as_of_date=slice_date, supplements=supplements), max_section_tokens),
        )
    )

    if stage in ("A", "B", "C", "D"):
        comm_cutoff = slice_date if stage in ("A", "B") else None
        comm_text = _comms_lines(comms, before_date=comm_cutoff)
        if comm_text:
            parts.append(_section("COMMUNICATIONS", truncate_to_tokens(comm_text, max_section_tokens)))

    if stage in ("B", "C") and triage_qa:
        parts.append(_section("TRIAGE Q&A", truncate_to_tokens(_triage_lines(triage_qa), max_section_tokens)))

    if stage == "A" and draft_so:
        so = f"SUBJECTIVE: {draft_so.get('subjective', '')}\nOBJECTIVE: {draft_so.get('objective', '')}"
        parts.append(_section("SUBJECTIVE / OBJECTIVE (DRAFT)", truncate_to_tokens(so, max_section_tokens)))
    elif stage in ("B", "C", "D"):
        vi = visit_index if visit_index is not None else intake_idx
        if vi < len(visits):
            v = visits[vi]
            if stage in ("B", "C") and vi == intake_idx:
                so = f"SUBJECTIVE: {v.get('subjective') or ''}\nOBJECTIVE: {v.get('objective') or ''}"
                if stage == "C":
                    parts.append(_section("SUBJECTIVE / OBJECTIVE", truncate_to_tokens(so, max_section_tokens * 2)))
            elif stage == "D":
                parts.append(_section("SUBJECTIVE / OBJECTIVE", ""))

    if stage == "D" and visit_index is not None and visits:
        parts.append(
            _section(
                "PRIOR VB NOTES",
                truncate_to_tokens(_prior_notes(visits, before_idx=visit_index, compact=compact_prior), max_section_tokens),
            )
        )
        prev_date = visits[visit_index - 1].get("date") if visit_index > 0 else intake_date
        cur_date = visits[visit_index].get("date") or slice_date
        interval = _interval_lines(visits, comms, after_date=prev_date or intake_date, before_date=cur_date)
        v = visits[visit_index]
        if v.get("interval_summary"):
            interval = f"{v.get('interval_summary')}\n{interval}"
        parts.append(_section("INTERVAL SINCE LAST PLAN", truncate_to_tokens(interval, max_section_tokens)))

    return "\n".join(p for p in parts if p.strip())


def serialize_stage_c_plan(
    case: dict,
    *,
    assessment: dict,
    behavior_diagnoses: list[dict],
    visit_index: int | None = None,
    triage_qa: list[dict] | None = None,
    as_of_date: str | None = None,
    max_section_tokens: int = 1500,
) -> str:
    """Build Plan input with an explicit, observable assessment-reasoning block."""

    base = serialize_case_file(
        case,
        stage="C",
        visit_index=visit_index,
        triage_qa=triage_qa,
        as_of_date=as_of_date,
        max_section_tokens=max_section_tokens,
    )
    themes = assessment.get("themes") or []
    reasoning = str(assessment.get("chris_final_reasoning") or "")
    lines = [
        f"THEMES: {json.dumps(themes, ensure_ascii=False)}",
        f"CLINICAL REASONING:\n{reasoning}",
        "BEHAVIOR DIAGNOSES:",
        json.dumps(behavior_diagnoses or [], ensure_ascii=False, indent=2),
    ]
    assessment_block = _section(
        "DOCUMENTED ASSESSMENT / REASONING",
        truncate_to_tokens("\n".join(lines), max_section_tokens * 2),
    )
    return "\n".join(part for part in (base, assessment_block) if part.strip())


def gold_output_stage_b(visit: dict) -> str:
    return json.dumps(
        {"subjective": visit.get("subjective") or "", "objective": visit.get("objective") or ""},
        ensure_ascii=False,
    )


def gold_output_stage_c(visit: dict) -> str:
    return json.dumps(
        {
            "assessment": visit.get("assessment") or {},
            "behavior_diagnoses": visit.get("behavior_diagnoses") or [],
            "plan": visit.get("plan") or {},
        },
        ensure_ascii=False,
    )


def gold_output_stage_c_assessment(visit: dict) -> str:
    return json.dumps(
        {
            "assessment": visit.get("assessment") or {},
            "behavior_diagnoses": visit.get("behavior_diagnoses") or [],
        },
        ensure_ascii=False,
    )


def gold_output_stage_c_plan(visit: dict) -> str:
    return json.dumps({"plan": visit.get("plan") or {}}, ensure_ascii=False)


def gold_output_stage_d(visit: dict) -> str:
    return json.dumps(
        {
            "subjective": visit.get("subjective") or "",
            "objective": visit.get("objective") or "",
            "assessment": visit.get("assessment") or {},
            "plan": visit.get("plan") or {},
        },
        ensure_ascii=False,
    )


def gold_output_stage_a(gap: dict) -> str:
    return json.dumps(gap, ensure_ascii=False)


def format_assistant_content(gold: str, thinking: str | None = None) -> str:
    if thinking:
        return f"\n{thinking.strip()}\n\n{gold}"
    return gold

