"""Replay longitudinal cases into stage-conditioned training examples."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from collabvet_review_app.training.serialize import (
    gold_output_stage_a,
    gold_output_stage_b,
    gold_output_stage_c,
    gold_output_stage_c_assessment,
    gold_output_stage_c_plan,
    gold_output_stage_d,
    serialize_case_file,
    serialize_stage_c_plan,
)
from collabvet_review_app.training.tokens import estimate_tokens


@dataclass
class StageExample:
    example_id: str
    case_id: str
    patient_folder: str
    stage: str
    visit_id: str | None
    split: str
    case_file: str
    gold_output: str
    est_tokens: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


def _intake_index(visits: list[dict]) -> int:
    for i, v in enumerate(visits):
        if v.get("visit_type") == "intake":
            return i
    return 0


def replay_case(
    case: dict,
    *,
    split: str,
    gap_analysis: dict | None = None,
    max_case_tokens: int = 6000,
    exclude_stage_d: bool = False,
    max_followups: int | None = None,
) -> list[StageExample]:
    patient = case.get("patient_folder") or "unknown"
    case_id = case.get("case_id") or patient.replace(" ", "-").lower()
    visits = case.get("visits") or []
    if not visits:
        return []

    intake_i = _intake_index(visits)
    intake = visits[intake_i]
    examples: list[StageExample] = []
    section_budget = min(1500, max(400, max_case_tokens // 4))

    gap = gap_analysis or {}
    triage_qa = gap.get("triage_qa") or []
    draft_so = gap.get("draft_so")

    if gap.get("stage_a"):
        cf = serialize_case_file(
            case,
            stage="A",
            draft_so=draft_so,
            as_of_date=intake.get("date"),
            max_section_tokens=section_budget,
        )
        gold = gold_output_stage_a(gap["stage_a"])
        ex_id = f"{case_id}-A-intake"
        examples.append(
            StageExample(
                example_id=ex_id,
                case_id=case_id,
                patient_folder=patient,
                stage="A",
                visit_id=intake.get("visit_id"),
                split=split,
                case_file=cf,
                gold_output=gold,
                est_tokens=estimate_tokens(cf + gold),
            )
        )

    cf_b = serialize_case_file(
        case,
        stage="B",
        triage_qa=triage_qa if triage_qa else None,
        visit_index=intake_i,
        as_of_date=intake.get("date"),
        max_section_tokens=section_budget,
    )
    gold_b = gold_output_stage_b(intake)
    examples.append(
        StageExample(
            example_id=f"{case_id}-B-intake",
            case_id=case_id,
            patient_folder=patient,
            stage="B",
            visit_id=intake.get("visit_id"),
            split=split,
            case_file=cf_b,
            gold_output=gold_b,
            est_tokens=estimate_tokens(cf_b + gold_b),
        )
    )

    cf_c = serialize_case_file(
        case,
        stage="C",
        triage_qa=triage_qa if triage_qa else None,
        visit_index=intake_i,
        as_of_date=intake.get("date"),
        max_section_tokens=section_budget,
    )
    gold_c = gold_output_stage_c(intake)
    examples.append(
        StageExample(
            example_id=f"{case_id}-C-intake",
            case_id=case_id,
            patient_folder=patient,
            stage="C",
            visit_id=intake.get("visit_id"),
            split=split,
            case_file=cf_c,
            gold_output=gold_c,
            est_tokens=estimate_tokens(cf_c + gold_c),
        )
    )

    if exclude_stage_d:
        return examples

    followups = [i for i in range(len(visits)) if i != intake_i]
    if max_followups is not None:
        followups = followups[:max_followups]

    for vi in followups:
        v = visits[vi]
        cf_d = serialize_case_file(
            case,
            stage="D",
            visit_index=vi,
            as_of_date=v.get("date"),
            max_section_tokens=section_budget,
            compact_prior=True,
        )
        gold_d = gold_output_stage_d(v)
        tok = estimate_tokens(cf_d + gold_d)
        if tok > max_case_tokens * 2:
            continue
        examples.append(
            StageExample(
                example_id=f"{case_id}-D-{v.get('visit_id') or vi}",
                case_id=case_id,
                patient_folder=patient,
                stage="D",
                visit_id=v.get("visit_id"),
                split=split,
                case_file=cf_d,
                gold_output=gold_d,
                est_tokens=tok,
                meta={"visit_date": v.get("date"), "visit_type": v.get("visit_type")},
            )
        )

    return examples


def replay_case_v03_split(
    case: dict,
    *,
    split: str,
    gap_analysis: dict | None = None,
    max_case_tokens: int = 6000,
    exclude_stage_d: bool = False,
    max_followups: int | None = None,
) -> list[StageExample]:
    """Replay v0.3 while replacing joint Stage C with Assessment then Plan."""

    examples = replay_case(
        case,
        split=split,
        gap_analysis=gap_analysis,
        max_case_tokens=max_case_tokens,
        exclude_stage_d=exclude_stage_d,
        max_followups=max_followups,
    )
    stage_c = next((example for example in examples if example.stage == "C"), None)
    if stage_c is None:
        return examples
    visits = case.get("visits") or []
    intake_i = _intake_index(visits)
    intake = visits[intake_i]
    assessment = intake.get("assessment") or {}
    diagnoses = intake.get("behavior_diagnoses") or []
    gap = gap_analysis or {}
    section_budget = min(1500, max(400, max_case_tokens // 4))
    cf_assessment = serialize_case_file(
        case,
        stage="C",
        triage_qa=gap.get("triage_qa") or None,
        visit_index=intake_i,
        as_of_date=intake.get("date"),
        max_section_tokens=section_budget,
    )
    cf_plan = serialize_stage_c_plan(
        case,
        assessment=assessment,
        behavior_diagnoses=diagnoses,
        triage_qa=gap.get("triage_qa") or None,
        visit_index=intake_i,
        as_of_date=intake.get("date"),
        max_section_tokens=section_budget,
    )
    split_examples = [
        StageExample(
            example_id=f"{stage_c.case_id}-C_ASSESSMENT-intake",
            case_id=stage_c.case_id,
            patient_folder=stage_c.patient_folder,
            stage="C_ASSESSMENT",
            visit_id=stage_c.visit_id,
            split=split,
            case_file=cf_assessment,
            gold_output=gold_output_stage_c_assessment(intake),
            est_tokens=estimate_tokens(
                cf_assessment + gold_output_stage_c_assessment(intake)
            ),
        ),
        StageExample(
            example_id=f"{stage_c.case_id}-C_PLAN-intake",
            case_id=stage_c.case_id,
            patient_folder=stage_c.patient_folder,
            stage="C_PLAN",
            visit_id=stage_c.visit_id,
            split=split,
            case_file=cf_plan,
            gold_output=gold_output_stage_c_plan(intake),
            est_tokens=estimate_tokens(cf_plan + gold_output_stage_c_plan(intake)),
        ),
    ]
    position = examples.index(stage_c)
    return examples[:position] + split_examples + examples[position + 1 :]

