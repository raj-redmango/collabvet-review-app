# Clinician longitudinal review app

This local-first Flask application compares immutable schema-v2 longitudinal extractions with allowlisted source documents, records auditable JSON-patch corrections, and exports only clinician-approved records. Source records, PDFs, SQLite, and approved artifacts stay on the Windows desktop. Cloudflare Tunnel is transport and access infrastructure; remote responses still traverse Cloudflare and render on the clinician's device.

## Windows setup

From PowerShell in the repository:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
$env:FLASK_APP = "collabvet_review_app:create_app"
py -m flask generate-secret
```

Set these environment variables in the launch session or a protected local environment file:

```powershell
$env:REVIEW_SECRET_KEY = "<generated value>"
$env:REVIEW_INPUT_ROOT = "C:\path\to\twopass"
$env:REVIEW_SOURCE_ROOT = "C:\path\to\PII-removed source folders"
$env:REVIEW_OUTPUT_ROOT = "C:\path\to\review artifacts"
$env:REVIEW_INSTANCE_ROOT = "C:\path\to\review instance"
$env:REVIEW_TEACHER_ROOT = "C:\path\to\teacher data"
$env:REVIEW_CONFIG_ROOT = "C:\path\to\review config"
$env:REVIEW_DATABASE_URL = "sqlite:///C:/path/to/review-instance/review.sqlite3"
$env:REVIEW_BIND_HOST = "127.0.0.1"
$env:REVIEW_PORT = "5055"
```

Initialize and index:

```powershell
py -m flask init-db
py -m flask create-user
py -m flask index-cases
collabvet-review
```

Open `http://127.0.0.1:5055` for local-only use. To listen on both localhost and the
computer's LAN address, explicitly enable a wildcard LAN bind and trust each public host:

```powershell
$env:REVIEW_BIND_HOST = "0.0.0.0"
$env:REVIEW_ALLOW_LAN_BIND = "true"
$env:REVIEW_TRUSTED_HOSTS = "127.0.0.1,localhost,192.168.86.133,review.collab.vet"
collabvet-review
```

This exposes port 5055 to the local network; Flask authentication remains required. Keep
the Windows network profile private and restrict remote internet entry through Cloudflare.

## Synthetic three-case pilot

Generate an intake-only case, a multi-visit case, and a golden-holdout case:

```powershell
py scripts\generate_review_demo.py
```

Copy the values from `data\review_demo\review-demo.env` into the current PowerShell environment, replace its secret, then initialize, create a user, index, and start the app as above.

Pilot checklist:

1. Assign and review **Demo Intake**. Compare every section with the synthetic PDF, make one correction, approve all sections, and export.
2. Review **Demo Longitudinal**. Verify timeline order, both visits, Stage B/C/D previews, the PDF's second page, and revision conflict handling in two browser sessions.
3. Review **Agatha Boccia**. It is synthetic but uses a configured golden ID. Approval may create portable artifacts, while training promotion must reject it.
4. Record confusing labels, section grouping problems, and navigation delays as comments. Refine the workflow before indexing clinical records.
5. Delete the demo instance before switching paths to real records.

Never copy clinical content into bug reports or test fixtures.

## Named Cloudflare Tunnel

Do not use a quick tunnel. Install `cloudflared`, authenticate it, and create a named tunnel:

```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel login
cloudflared tunnel create collabvet-review
cloudflared tunnel route dns collabvet-review review.example.org
```

Create `%USERPROFILE%\.cloudflared\config.yml`:

```yaml
tunnel: <tunnel UUID>
credentials-file: C:\Users\<user>\.cloudflared\<tunnel UUID>.json
ingress:
  - hostname: review.example.org
    service: http://127.0.0.1:5055
  - service: http_status:404
```

In Cloudflare Zero Trust, create a self-hosted Access application for `review.example.org`, allow only named clinic identities or one-time-pin addresses, and deny everyone else. Copy the application audience tag and enable application-side assertion validation:

```powershell
$env:REVIEW_CF_ACCESS_REQUIRED = "true"
$env:REVIEW_CF_ACCESS_TEAM_DOMAIN = "https://<team>.cloudflareaccess.com"
$env:REVIEW_CF_ACCESS_AUDIENCE = "<application AUD tag>"
$env:REVIEW_CF_ALLOWED_EMAILS = "clinician1@example.org,clinician2@example.org"
$env:REVIEW_SECURE_COOKIES = "true"
cloudflared tunnel run collabvet-review
```

Run Flask and `cloudflared` in separate terminals. Confirm Access policies, Cloudflare logging/retention, caching, and clinic confidentiality requirements before exposing real records.

Immediate shutdown:

```powershell
Stop-Process -Name cloudflared
cloudflared tunnel route dns delete review.example.org
```

Also disable the Access application or revoke identities if credentials may be compromised. Rotate `REVIEW_SECRET_KEY`, application passwords, and tunnel credentials before restoring access.

## Backups and approved-only promotion

Back up SQLite and portable artifacts while no review writes are active:

```powershell
py -m flask backup --destination "D:\EncryptedBackups\collabvet-review"
```

Final approval writes:

- `cases\<case_id>.approved.json` — corrected, path-stripped snapshot
- `reviews\<case_id>.review.json` — portable provenance, patches, comments, sign-offs, validation, and reviewer identity
- `approved_cases_index.json` — current approved-case index

Build the clinician-approved v0.4 input manifest:

```powershell
py scripts\build_v04_approved_manifest.py
```

The gate accepts only approved snapshots whose immutable source hash is still current, whose schema and scrub checks pass, and whose case is not in either golden holdout configuration. The existence of a longitudinal JSON file never implies approval.

Final approval also requires explicit clinician verification of every applicable training projection:

- Stage A: pre-visit input and teacher-generated triage/draft target
- Stage B: intake context and expected Subjective/Objective
- Stage C: intake S/O context and expected Assessment/diagnoses/Plan
- Stage D: each follow-up context and expected follow-up S/O/A/P

Stage A cannot be approved when its gap-analysis artifact is missing. Any case correction resets stage approvals, and replacing the Stage A artifact invalidates its prior sign-off.

## Security boundaries

- Keep the origin on `127.0.0.1`; the named tunnel is the only remote path.
- Application login remains mandatory after Cloudflare Access.
- Only files resolved beneath `REVIEW_SOURCE_ROOT` are served.
- Clinical responses use no-store, restrictive CSP, no-referrer, and same-origin framing headers.
- Clinical content is not placed in browser local storage or application logs.
- Every case/document view, edit, comment, sign-off, export, login failure, and admin action is attributable in the audit table.
- Stop the tunnel before changing paths, restoring backups, or rotating credentials.
