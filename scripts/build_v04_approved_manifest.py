"""Build the v0.4 clinician-approved corpus manifest."""

from __future__ import annotations

import argparse
from pathlib import Path

from collabvet_review_app.config import PROJECT_ROOT, ReviewConfig
from collabvet_review_app.training_gate import build_approved_manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input-root",
        type=Path,
        default=ReviewConfig.INPUT_ROOT,
    )
    parser.add_argument(
        "--review-output-root",
        type=Path,
        default=ReviewConfig.OUTPUT_ROOT,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "config" / "v04_approved_train_manifest.json",
    )
    args = parser.parse_args()
    manifest = build_approved_manifest(
        project_root=PROJECT_ROOT,
        input_root=args.input_root,
        review_output_root=args.review_output_root,
        destination=args.output,
    )
    print(
        f"approved={manifest['case_count']} rejected={manifest['rejected_count']} "
        f"output={args.output}"
    )


if __name__ == "__main__":
    main()

