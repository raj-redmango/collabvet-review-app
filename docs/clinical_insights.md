# Clinical Insights frontend integration

## Configuration and authentication

The review app reads `collabvet-staging-api.env` locally. The file is ignored by Git.
The API base URL is discovered from `CORS_ORIGINS` by excluding the configured
`FRONTEND_URL`; the current configuration resolves to the Render staging API. The URL is
not embedded in browser code.

Clinical Insights uses the backend's isolated App Admin bearer-token flow:

- `POST /api/v1/app-admin/auth/login/password`
- `GET /api/v1/app-admin/me`
- `Authorization: Bearer <API-issued token>`

The supplied configuration has `APP_ADMIN_USERNAME`, `APP_ADMIN_PASSWORD_HASH`, and
`APP_ADMIN_JWT_SECRET`, but no plaintext App Admin password or API-issued service token.
A password hash cannot authenticate to the API, and the review app must not mint its own
token from a signing secret. Until Sachin provides a supported service credential or
identity-exchange contract, the frontend remains available but reports authorization as
unavailable. API credentials are never sent to browser JavaScript.

The review app's Flask-Login session protects both the page and its same-origin API proxy.
The proxy passes API authorization failures through as 401/403 and does not bypass API
tenant, VB, or role decisions.

## Existing API contract

The deployed App Admin frontend currently uses these read-only endpoints:

- `GET /api/v1/app-admin/clinical-insights/overview`
- `GET /api/v1/app-admin/clinical-insights/patterns`
- `GET /api/v1/app-admin/clinical-insights/pathways`
- `GET /api/v1/app-admin/clinical-insights/pathway-comparisons`
- `GET /api/v1/app-admin/clinical-insights/interventions`
- `GET /api/v1/app-admin/clinical-insights/safety`
- `GET /api/v1/app-admin/clinical-insights/cases`
- `GET /api/v1/app-admin/clinical-insights/cases/{id}`
- `GET /api/v1/app-admin/clinical-insights/knowledge`
- `GET /api/v1/app-admin/clinical-insights/runs`
- `GET /api/v1/app-admin/clinical-insights/metrics/{metric}`

Supported query parameters observed in the deployed client are `run_id`, `vb_id`, `q`,
`offset`, and `limit`. Server-side search/pagination is used for pathways, interventions,
and cases. Other tabs are filtered only after their endpoint is selected and loaded.

## Source inventory

- Extraction schema: longitudinal JSON schema version 2.
- Canonical source: `C:\Projects\collabvet-clinical-data\cases`.
- Current inventory: 100 JSON cases.
- Representative top-level fields: `signalment`, `history_form`,
  `pre_intake_medical_digest`, `visits`, `communications`, `assessment`, `plan`, and
  `behavior_diagnoses`.
- Existing extraction/schema implementation remains in `collabvet-unified-model`; this
  frontend does not create a competing mining or import pipeline.

## API gaps represented gracefully

The overview contract currently exposes cases, visits, follow-up outcomes, documented
improvement, adverse effects, medication counts, training-support referrals, and a
combined provider-caution/bite metric. It does not separately expose all requested
no-change, worsening, partial-record, data-quality, diagnosis/pattern, intervention,
bite-history, or provider-caution overview counts. Those cards display “Not exposed”
rather than synthesizing unsupported values.

Medication summaries are available through `metrics/medications`; no dedicated
medication-list endpoint was observed. The UI therefore treats this as a lazy metric
dataset and tolerates partial fields.

The API also needs a supported service-to-service authentication or identity-exchange
contract for `review.collab.vet`. It should return an API-issued bearer token whose
tenant/VB/role scope corresponds to the authenticated review user. No backend change is
made by this repository.

