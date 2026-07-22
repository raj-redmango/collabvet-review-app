"""Generate three synthetic review cases with source PDFs for a local pilot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import fitz

from collabvet_review_app.config import PROJECT_ROOT


def _case(case_id: str, patient: str, *, followup: bool = False) -> dict:
    assessment = {
        "themes": ["fear of unfamiliar people"],
        "chris_final_reasoning": "History and observed avoidance are consistent with fear.",
    }
    plan = {
        "recommendations": ["Use distance and counterconditioning."],
        "action": ["Begin staged visitor practice."],
        "rationale": ["Prevent rehearsal while changing the emotional response."],
        "monitoring": ["Track threshold distance and recovery time."],
        "follow_up": ["Recheck in four weeks."],
    }
    visits = [
        {
            "visit_id": "v01",
            "date": "2026-01-10",
            "visit_type": "intake",
            "subjective": "Caregiver reports barking and retreating when visitors enter.",
            "objective": "Patient remained behind caregiver and accepted treats at distance.",
            "assessment": assessment,
            "plan": plan,
            "medication_changes": [],
            "interval_summary": "",
        }
    ]
    if followup:
        visits.append(
            {
                "visit_id": "v02",
                "date": "2026-02-12",
                "visit_type": "recheck",
                "subjective": "Recovery is faster and threshold distance is smaller.",
                "objective": "No direct examination; video showed relaxed treat taking.",
                "assessment": {
                    "themes": ["fear improving with structured exposure"],
                    "chris_final_reasoning": "Improved recovery supports continuing the plan.",
                },
                "plan": {
                    **plan,
                    "recommendations": ["Continue gradual visitor practice."],
                },
                "medication_changes": [],
                "interval_summary": "Completed three successful visitor sessions.",
            }
        )
    return {
        "schema_version": 2,
        "case_id": case_id,
        "source": {"pipeline": "synthetic-review-demo", "input_paths": {}},
        "signalment": {
            "pet_name": patient.split()[0],
            "species": "Canine",
            "breed": "Mixed breed",
            "age": "3 years",
            "sex": "Spayed female",
        },
        "history_form": {"presenting_concerns": "Fearful behavior around unfamiliar visitors."},
        "pre_intake_medical_digest": {"summary": "Synthetic record; no relevant abnormalities."},
        "assessment": assessment,
        "plan": plan,
        "visits": visits,
        "communications": [],
        "medical_record_supplements": [],
    }


def _pdf(path: Path, patient: str, followup: bool) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        (
            f"SYNTHETIC CLINICAL SOURCE — {patient}\n\n"
            "2026-01-10 Intake\n"
            "Caregiver reports barking and retreating when visitors enter.\n"
            "Patient remained behind caregiver and accepted treats at distance.\n"
            "Assessment: fear of unfamiliar people.\n"
            "Plan: distance, counterconditioning, and tracking recovery time."
        ),
        fontsize=11,
    )
    if followup:
        page = document.new_page()
        page.insert_text(
            (72, 72),
            (
                "2026-02-12 Recheck\n"
                "Recovery is faster after three successful visitor sessions.\n"
                "Continue gradual visitor practice."
            ),
            fontsize=11,
        )
    document.save(path)
    document.close()


def generate(root: Path) -> None:
    input_root = root / "input"
    source_root = root / "sources"
    output_root = root / "output"
    instance_root = root / "instance"
    teacher_root = root / "teacher"
    for path in (input_root, source_root, output_root, instance_root, teacher_root):
        path.mkdir(parents=True, exist_ok=True)
    gap_root = teacher_root / "teacher" / "gap"
    gap_root.mkdir(parents=True, exist_ok=True)
    cases = [
        ("demo-intake", "Demo Intake", False),
        ("demo-longitudinal", "Demo Longitudinal", True),
        ("agatha-boccia", "Agatha Boccia", False),
    ]
    registry = {"version": 1, "description": "Synthetic clinician review pilot", "patients": {}}
    for case_id, patient, followup in cases:
        patient_root = source_root / patient.replace(" ", "_")
        patient_root.mkdir(exist_ok=True)
        pdf = patient_root / "synthetic_clinical_summary.pdf"
        history = patient_root / "history_form.json"
        _pdf(pdf, patient, followup)
        history.write_text(
            json.dumps({"presenting_concerns": "Fearful behavior around visitors."}, indent=2),
            encoding="utf-8",
        )
        data = _case(case_id, patient, followup=followup)
        data["source"]["input_paths"] = {
            "history_form": str(history.resolve()),
            "clinical_summary_pdf": str(pdf.resolve()),
        }
        filename = f"{patient.replace(' ', '_')}_twopass_longitudinal.json"
        (input_root / filename).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        gap = {
            "draft_so": {
                "subjective": data["visits"][0]["subjective"],
                "objective": "In-clinic observations pending.",
            },
            "triage_qa": [],
            "stage_a": {
                "draft_so": {
                    "subjective": data["visits"][0]["subjective"],
                    "objective": "In-clinic observations pending.",
                },
                "hypothesized_patterns": [
                    {
                        "pattern_id": "fear_unfamiliar_people",
                        "confidence": "high",
                        "evidence": "Caregiver reports barking and retreating around visitors.",
                    }
                ],
                "questions": [
                    {
                        "text": "How quickly does the patient recover after a visitor leaves?",
                        "pattern_ids": ["fear_unfamiliar_people"],
                        "answer_type": "duration",
                    }
                ],
            },
        }
        (gap_root / f"{patient.replace(' ', '_')}_gap.json").write_text(
            json.dumps(gap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        registry["patients"][patient] = {
            "original_folder": patient,
            "pseudonym_folder": patient,
            "longitudinal_file": filename,
        }
    (input_root / "twopass_completed.json").write_text(
        json.dumps(registry, indent=2) + "\n", encoding="utf-8"
    )
    env = root / "review-demo.env"
    env.write_text(
        "\n".join(
            [
                f"REVIEW_INPUT_ROOT={input_root.resolve()}",
                f"REVIEW_SOURCE_ROOT={source_root.resolve()}",
                f"REVIEW_OUTPUT_ROOT={output_root.resolve()}",
                f"REVIEW_INSTANCE_ROOT={instance_root.resolve()}",
                f"REVIEW_TEACHER_ROOT={teacher_root.resolve()}",
                f"REVIEW_DATABASE_URL=sqlite:///{(instance_root / 'review.sqlite3').resolve().as_posix()}",
                "REVIEW_BIND_HOST=127.0.0.1",
                "REVIEW_PORT=5055",
                "REVIEW_CF_ACCESS_REQUIRED=false",
                "REVIEW_SECRET_KEY=replace-with-output-from-flask-generate-secret",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(f"Synthetic pilot created at {root}")
    print(f"Environment template: {env}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=PROJECT_ROOT / "data" / "review_demo",
    )
    args = parser.parse_args()
    generate(args.root.resolve())


if __name__ == "__main__":
    main()

