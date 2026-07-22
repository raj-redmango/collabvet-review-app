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

Clone the private clinical-data repository beside this repository. The review app
reads canonical cases directly from its `cases/` directory and never copies them
into this repository:

```powershell
cd C:\Projects
git clone https://github.com/sachin-redmango/collabvet-clinical-data.git
cd collabvet-review-app
```

If the checkout is elsewhere, set `REVIEW_CLINICAL_DATA_ROOT` to its root. Before
indexing, the app verifies that the checkout is clean, on `main`, and has the
expected GitHub origin.

## Initialize and run

```powershell
$env:FLASK_APP = "collabvet_review_app:create_app"
python -m flask init-db
python -m flask create-user
python -m flask verify-case-source
python -m flask index-cases
collabvet-review
```

The server listens on `http://127.0.0.1:5055` by default. A non-loopback bind is
rejected unless `REVIEW_ALLOW_LAN_BIND=true`.

## Local data

- `<REVIEW_CLINICAL_DATA_ROOT>/cases/`: canonical immutable schema-v2 inputs
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
