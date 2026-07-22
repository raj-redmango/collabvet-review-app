"""Parse and validate Opus longitudinal JSON responses."""

from __future__ import annotations

import json
import re
from typing import Any


def strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_longitudinal_json(text: str) -> dict[str, Any]:
    cleaned = strip_markdown_fences(text)
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    validate_longitudinal_minimal(data)
    return data


def validate_longitudinal_minimal(data: dict[str, Any]) -> None:
    if data.get("schema_version") != 2:
        raise ValueError(f"schema_version must be 2, got {data.get('schema_version')!r}")
    visits = data.get("visits")
    if not isinstance(visits, list) or not visits:
        raise ValueError("visits[] must be a non-empty array")
    digest = data.get("pre_intake_medical_digest")
    if not isinstance(digest, dict):
        raise ValueError("pre_intake_medical_digest must be an object")


def parse_medfiles_json(text: str) -> dict[str, Any]:
    cleaned = strip_markdown_fences(text)
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    for key in ("medications", "immunizations", "lab_results", "diagnoses", "narrative_events"):
        if key not in data:
            data[key] = []
    return data


def _normalize_assessment(assessment: Any) -> dict[str, Any]:
    if assessment is None:
        return {"themes": [], "chris_final_reasoning": ""}
    if isinstance(assessment, str):
        return {"themes": [], "chris_final_reasoning": assessment}
    if isinstance(assessment, dict):
        themes = assessment.get("themes") or []
        reasoning = (
            assessment.get("chris_final_reasoning")
            or assessment.get("summary")
            or assessment.get("text")
            or ""
        )
        return {
            "themes": list(themes),
            "chris_final_reasoning": str(reasoning),
        }
    return {"themes": [], "chris_final_reasoning": str(assessment)}


def normalize_cs_visits(visits: list[dict]) -> list[dict]:
    out: list[dict] = []
    for visit in visits:
        row = dict(visit)
        row["assessment"] = _normalize_assessment(row.get("assessment"))
        plan = row.get("plan")
        if not isinstance(plan, dict):
            row["plan"] = {"recommendations": []}
        else:
            row["plan"] = {
                "recommendations": list(plan.get("recommendations") or []),
                "action": list(plan.get("action") or []),
                "rationale": list(plan.get("rationale") or []),
                "monitoring": list(plan.get("monitoring") or []),
                "follow_up": list(plan.get("follow_up") or []),
            }
        out.append(row)
    return out


def parse_cs_json(text: str) -> dict[str, Any]:
    cleaned = strip_markdown_fences(text)
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    visits = data.get("visits")
    if not isinstance(visits, list) or not visits:
        raise ValueError("visits[] must be a non-empty array")
    data["visits"] = normalize_cs_visits(visits)
    if "communications" not in data:
        data["communications"] = []
    if "medical_record_supplements" not in data:
        data["medical_record_supplements"] = []
    return data


def extract_text_from_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif hasattr(block, "type") and getattr(block, "type", None) == "text":
                parts.append(str(getattr(block, "text", "")))
        return "\n".join(parts)
    return str(content)

