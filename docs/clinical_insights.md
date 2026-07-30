# Clinical Insights frontend integration

## Configuration and authentication

The review app uses Sachin's dedicated, read-only machine-to-machine API. Configure these
values only in the review server environment or its ignored `.env` file:

```text
COLLABVET_API_BASE_URL=https://collabvet-staging-api.onrender.com
COLLABVET_REVIEW_CLIENT_ID=collabvet-review
COLLABVET_REVIEW_CLIENT_SECRET=<copy from REVIEW_API_CLIENT_SECRET in Render>
```

Never use a `NEXT_PUBLIC_*` variable or expose the secret in browser code, logs, screenshots,
Git, email, or error messages. The ignored `collabvet-staging-api.env` file is not used by
this integration.

The Flask server exchanges the credential at:

- `POST /api/v1/review/auth/token`
- grant: `client_credentials`
- scope: `clinical_insights:read`

The API-issued bearer token is held only in process memory for at most 14 minutes. The
client discards it and performs one replacement exchange after an upstream 401. A second
401 ends the request without an authentication loop.

The review app's Flask-Login session protects the page and every same-origin proxy route.
Browser JavaScript receives neither the client secret nor the bearer token. Tenant, VB,
role, and read-only restrictions remain authoritative in Sachin's API.

## Clinical Insights API contract

The server proxies only the dedicated review namespace:

- `GET /api/v1/review/clinical-insights/overview`
- `GET /api/v1/review/clinical-insights/patterns`
- `GET /api/v1/review/clinical-insights/pathways`
- `GET /api/v1/review/clinical-insights/pathway-comparisons`
- `GET /api/v1/review/clinical-insights/interventions`
- `GET /api/v1/review/clinical-insights/safety`
- `GET /api/v1/review/clinical-insights/cases`
- `GET /api/v1/review/clinical-insights/cases/{id}`
- `GET /api/v1/review/clinical-insights/knowledge`
- `GET /api/v1/review/clinical-insights/runs`
- `GET /api/v1/review/clinical-insights/metrics/{metric}`
- `GET /api/v1/review/clinical-insights/graph/summary`
- `GET /api/v1/review/clinical-insights/graph/vocabulary`
- `GET /api/v1/review/clinical-insights/graph/cases`
- `GET /api/v1/review/clinical-insights/graph/cases/{id}/relationships`
- `GET /api/v1/review/clinical-insights/graph/cases/{id}/pathway`
- `GET /api/v1/review/clinical-insights/graph/cases/{id}/timeline`
- `GET /api/v1/review/clinical-insights/graph/cases/{id}/review-table`
- `GET /api/v1/review/clinical-insights/graph/cases/{id}/history`
- `GET /api/v1/review/clinical-insights/graph/global-graph`
- `POST /api/v1/review/clinical-insights/graph/cases/{id}/review`

Allowed query parameters are `run_id`, `vb_id`, `q`, `offset`, and `limit`. Server-side
search and pagination are used where supported. Unknown resources, metrics, query
parameters, and upstream namespaces are rejected by the review server.

Upstream 403 and 429 responses remain visible to the authenticated reviewer. `Retry-After`
is preserved for rate limits. Missing or rejected service credentials return a safe 503
without disclosing credential material.

## Source inventory and API gaps

- Extraction schema: longitudinal JSON schema version 2.
- Canonical source: `C:\Projects\collabvet-clinical-data\cases`.
- Current longitudinal inventory: 102 JSON cases.
- Representative fields include `signalment`, `history_form`,
  `pre_intake_medical_digest`, `visits`, `communications`, `assessment`, `plan`, and
  `behavior_diagnoses`.

The overview contract does not expose every desired no-change, worsening, partial-record,
data-quality, diagnosis, intervention, bite-history, or provider-caution count. Unsupported
cards display "Not exposed" instead of synthesizing values. Medication summaries use the
dedicated metrics endpoint and tolerate partial fields.

## Clinical graph projection

The Case Graph experience is rendered from the API's graph projection. The Review App
does not query Neo4j, reproduce backend projection rules, or derive clinical
relationships from longitudinal records.

The relationship response supplies `projection_source`, graph and version metadata,
clinical claim nodes, relationship edges, source and target node keys, clinical category,
pathway stage, evidence references, review state, lifecycle state, quarantine state,
epistemic state, certainty, and temporal-only state.

The UI says **Live Neo4j projection** only when `projection_source` is exactly `neo4j`.
Other reported sources are described as a PostgreSQL graph preview. Temporal-only
relationships use dashed lines and an explicit warning that sequence does not prove
causality.

### Rendering approach

The graph uses a repository-local SVG renderer in `static/clinical_graph.js`. No graph
dependency or external CDN is required. This fits the Review App's server-rendered Flask
and vanilla JavaScript architecture and keeps its `default-src 'self'` content-security
policy intact.

The renderer provides deterministic force-directed layout, node dragging, pan, bounded
zoom, fit, reset, fullscreen, zoom-dependent labels, keyboard selection, and visual
states for patient, clinical claim, held, reviewed, retired, selected, and temporal
items. Selecting a node does not rerun the layout.

Case search and pagination are server-side. Patient display names are primary when the
API reports a linked identity; unlinked identities are labelled explicitly and case
identifiers remain secondary. Machine-only knowledge in the corpus graph is opt-in.

### Clinician review actions

Confirm, correct, mark uncertain, request evidence, annotate, retire, and restore submit
through the existing graph review endpoint. The Review App validates the payload,
requires its authenticated session and CSRF token, and forwards the decision through its
server-side API client. It never writes to Neo4j or PostgreSQL directly. The API remains
authoritative for versioning, audit history, conflicts, and projection resynchronization.
