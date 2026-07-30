# Data Dashboard

The authenticated Data Dashboard is an operational view of the local clinical-data
pipeline. It is separate from Clinical Insights: it reads local inventory metadata and
review state, while Clinical Insights reads mined analytics from the staging API.

## Privacy boundary

- `raw/` is never opened, listed, counted, or returned. Raw inventory is displayed as
  **Not connected** until a separate sanitized manifest is intentionally introduced.
- The source scan reads only immediate PII-removed folder and filename metadata needed
  to determine component presence.
- Longitudinal JSON is read server-side, but browser responses include only schema,
  counts, dates, extraction provenance, and workflow state. Clinical narrative, history
  answers, source quotes, filenames, and local paths are not returned.
- Patient URLs use a keyed opaque identifier rather than the pseudonym or case ID.

## Metric definitions

- **PII removed:** a patient folder exists under the clinical-data repository's
  canonical `source/` directory. `REVIEW_SOURCE_ROOT` is used only as a fallback when
  that directory is unavailable.
- **Process ready:** `history_form.json`, a clinical-summary PDF, and a medfiles PDF
  are present.
- **Longitudinal:** a readable schema-v2 JSON exists under the canonical `cases/` root.
- **Included in v0.2 corpus:** the case is in `config/v02_train_manifest.json`.
  This proves manifest membership, not that a specific model run consumed it.
- **New:** a longitudinal case is absent from the v0.2 manifest.
- **Clinician approved:** a local approved export exists, is under the configured output
  root, and matches the current indexed source hash.
- **Future-training eligible:** the case is accepted by
  `config/v04_approved_train_manifest.json`.
- **Actually trained:** unavailable until a future training-run ledger records model,
  dataset hash, run ID, and completion status.

## Refresh and failure behavior

Inventory snapshots are cached in process for five minutes. Any authenticated user may
request a refresh; forced refreshes have a process-wide 30-second cooldown and are
audited. Concurrent refreshes are serialized. If a refresh fails after a successful
scan, the last snapshot remains available with a stale-data warning.

Git metadata includes the clinical-data commit and branch. Working-tree inspection is
explicitly scoped to `source`, `cases`, `approved`, `exports`, and `docs`; `raw` remains
excluded.
