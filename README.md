# Collab.Vet Review App

Standalone Flask application for clinician review and approval of longitudinal case
extractions. It has no runtime dependency on `collabvet-unified-model`.

## Setup

```powershell
cd C:\Projects\collabvet-review-app
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Replace `REVIEW_SECRET_KEY` in `.env`, and set `REVIEW_SOURCE_ROOT` to the
PII-removed patient-document directory if source-document viewing is required.

## Initialize and run

```powershell
$env:FLASK_APP = "collabvet_review_app:create_app"
python -m flask init-db
python -m flask create-user
python -m flask index-cases
collabvet-review
```

The server listens on `http://127.0.0.1:5055` by default. A non-loopback bind is
rejected unless `REVIEW_ALLOW_LAN_BIND=true`.

## Local data

- `data/review_inputs/`: immutable twopass JSON inputs
- `data/teacher/teacher/gap/`: Stage A teacher artifacts
- `data/review_app/review.sqlite3`: users, reviews, revisions, and audit history
- `data/review_app/approved/`: scrubbed approved exports

These directories are ignored by git because they may contain sensitive clinical
workflow data.

## Development

```powershell
python -m pytest -q
python -m ruff check .
python scripts\generate_review_demo.py
```

See `docs/clinician_review_app.md` for workflow, security, backup, and Cloudflare
Access details.
