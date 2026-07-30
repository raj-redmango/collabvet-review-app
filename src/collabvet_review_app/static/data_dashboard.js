(() => {
  "use strict";

  const root = document.getElementById("data-dashboard");
  if (!root) return;

  const pageSize = 25;
  const state = {
    query: "",
    stage: "all",
    sort: "pseudonym",
    offset: 0,
    total: 0,
    controller: null
  };
  const number = new Intl.NumberFormat();
  const metricStages = {
    pii_removed: "pii_removed",
    process_ready: "process_ready",
    longitudinal: "longitudinal",
    legacy_training: "legacy_training",
    new: "new",
    clinician_approved: "clinician_approved",
    future_training: "future_training"
  };
  const metricLabels = {
    raw: "Raw inventory",
    pii_removed: "PII removed",
    process_ready: "Process ready",
    longitudinal: "Longitudinal",
    legacy_training: "v0.2 training corpus",
    clinician_approved: "Clinician approved"
  };
  const lifecycle = [
    ["raw", "Raw"],
    ["pii_removed", "PII removed"],
    ["process_ready", "Process ready"],
    ["longitudinal", "Longitudinal"],
    ["legacy_training", "Training corpus"],
    ["clinician_approved", "Clinician approved"]
  ];

  const escapeHTML = (value) => String(value ?? "").replace(
    /[&<>'"]/g,
    (character) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"}[character])
  );
  const words = (value) => String(value ?? "").replaceAll("_", " ");
  const badge = (value, extra = "") =>
    `<span class="badge ${escapeHTML(extra)}">${escapeHTML(words(value || "not available"))}</span>`;
  const displayValue = (value, unavailable = "Unavailable") =>
    value === null || value === undefined ? unavailable : number.format(value);

  async function fetchJSON(path, options = {}) {
    const response = await fetch(path, {
      credentials: "same-origin",
      headers: {Accept: "application/json", ...(options.headers || {})},
      ...options
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.error || `Data Dashboard request failed (${response.status}).`);
      error.status = response.status;
      error.retryAfter = payload.retry_after;
      throw error;
    }
    return payload;
  }

  function showError(message = "") {
    const node = document.getElementById("dashboard-alert");
    node.textContent = message;
    node.hidden = !message;
  }

  function applyStage(stage) {
    state.stage = stage;
    state.offset = 0;
    document.getElementById("patient-stage").value = stage;
    loadPatients();
    document.getElementById("explorer-title").scrollIntoView({behavior: "smooth", block: "start"});
  }

  function renderLifecycle(metrics) {
    document.getElementById("lifecycle-funnel").innerHTML = lifecycle.map(([key, label], index) => {
      const value = metrics[key];
      const enabled = Boolean(metricStages[key]) && value !== null && value !== undefined;
      return `<button type="button" class="lifecycle-stage" ${enabled ? `data-stage="${metricStages[key]}"` : "disabled"}>
        <span class="lifecycle-step">${index + 1}</span>
        <span><strong>${escapeHTML(label)}</strong><small>${escapeHTML(displayValue(value, "Not connected"))}</small></span>
      </button>`;
    }).join("");
    document.querySelectorAll(".lifecycle-stage[data-stage]").forEach((node) => {
      node.addEventListener("click", () => applyStage(node.dataset.stage));
    });
  }

  function renderConversions(conversions) {
    const rows = [
      ["PII removed → process ready", conversions.source_to_ready],
      ["Process ready → longitudinal", conversions.ready_to_longitudinal],
      ["Longitudinal → v0.2 corpus", conversions.longitudinal_to_legacy],
      ["Longitudinal → clinician approved", conversions.longitudinal_to_approved]
    ];
    document.getElementById("conversion-strip").innerHTML = rows.map(([label, value]) =>
      `<span><strong>${escapeHTML(value === null ? "—" : `${value}%`)}</strong> ${escapeHTML(label)}</span>`
    ).join("");
  }

  function renderReadiness(readiness) {
    const rows = [
      ["History JSON", readiness.history],
      ["Clinical summary", readiness.clinical_summary],
      ["Medfiles", readiness.medfiles],
      ["Complete source sets", readiness.complete]
    ];
    document.getElementById("readiness-grid").innerHTML = rows.map(([label, value]) =>
      `<article class="metric-card static-metric">
        <span class="metric-label">${escapeHTML(label)}</span>
        <span class="metric-value">${escapeHTML(displayValue(value))}</span>
        <small>PII-removed file-presence metadata</small>
      </article>`
    ).join("");
  }

  function renderOpportunities(opportunities) {
    const rows = [
      ["Ready for extraction", opportunities.ready_without_longitudinal, "ready_without_longitudinal", "Complete source set, no longitudinal record"],
      ["New longitudinal", opportunities.new_longitudinal, "new", "Not in the v0.2 training manifest"],
      ["Awaiting clinician review", opportunities.awaiting_review, "awaiting_review", "No current-hash approval"],
      ["Approved, not in future manifest", opportunities.approved_not_in_future_manifest, "approved_not_future", "Rebuild the approved-only manifest"],
      ["Inventory issues", opportunities.broken, "broken", "Mismatch, unreadable data, or stale hash"]
    ];
    document.getElementById("opportunity-grid").innerHTML = rows.map(([label, value, stage, hint]) =>
      `<button type="button" class="opportunity-item" data-stage="${escapeHTML(stage)}">
        <span><strong>${escapeHTML(label)}</strong><small>${escapeHTML(hint)}</small></span>
        <span class="opportunity-value">${escapeHTML(displayValue(value))}</span>
      </button>`
    ).join("");
    document.querySelectorAll(".opportunity-item").forEach((node) => {
      node.addEventListener("click", () => applyStage(node.dataset.stage));
    });
  }

  function renderDefinitions(definitions) {
    const entries = Object.entries(definitions);
    document.getElementById("dashboard-definitions").innerHTML = entries.map(([key, value]) =>
      `<dt>${escapeHTML(metricLabels[key] || words(key))}</dt><dd>${escapeHTML(value)}</dd>`
    ).join("");
  }

  function renderMetadata(meta) {
    const generated = meta.generated_at ? new Date(meta.generated_at) : null;
    const age = Number(meta.cache_age_seconds || 0);
    document.getElementById("dashboard-freshness").textContent = generated
      ? `Scanned ${generated.toLocaleString()} · cache age ${age}s${meta.stale ? " · stale" : ""}`
      : "Inventory time unavailable";
    const git = meta.git || {};
    document.getElementById("dashboard-git").innerHTML = [
      git.commit_short ? `<span><strong>Commit:</strong> <code>${escapeHTML(git.commit_short)}</code></span>` : "",
      git.branch ? `<span><strong>Branch:</strong> ${escapeHTML(git.branch)}</span>` : "",
      git.dirty === true ? badge("uncommitted safe-path changes", "needs_changes") : "",
      git.dirty === false ? badge("safe paths clean", "approved") : ""
    ].join("");
    const warnings = Array.isArray(meta.warnings) ? meta.warnings : [];
    const warningNode = document.getElementById("dashboard-warnings");
    warningNode.innerHTML = warnings.length
      ? `<strong>Inventory notices</strong><ul>${warnings.map((warning) => `<li>${escapeHTML(warning)}</li>`).join("")}</ul>`
      : "";
    warningNode.hidden = !warnings.length;
  }

  function renderOverview(payload) {
    renderLifecycle(payload.metrics || {});
    renderConversions(payload.conversions || {});
    renderReadiness(payload.readiness || {});
    renderOpportunities(payload.opportunities || {});
    renderDefinitions(payload.definitions || {});
    renderMetadata(payload.meta || {});
  }

  function sourceSummary(row) {
    const source = row.source || {};
    const tokens = [
      source.history ? badge("history", "approved") : badge("no history"),
      source.clinical_summary ? badge("clinical", "approved") : badge("no clinical"),
      source.medfiles ? badge("medfiles", "approved") : badge("no medfiles")
    ];
    return `${tokens.join(" ")}<br><small>${row.process_ready ? "Process ready" : `Missing: ${(row.missing_components || []).join(", ") || "source folder"}`}</small>`;
  }

  function longitudinalSummary(row) {
    if (!row.longitudinal) return `<span class="muted">Not available</span>`;
    const dateRange = row.first_event_date
      ? `<br><small>${escapeHTML(row.first_event_date)} → ${escapeHTML(row.last_event_date || row.first_event_date)}</small>`
      : "";
    return `<strong>Schema ${escapeHTML(row.schema_version)}</strong><br>
      <span>${escapeHTML(row.visit_count)} visits · ${escapeHTML(row.communication_count)} communications</span>${dateRange}`;
  }

  function trainingSummary(row) {
    const parts = [];
    if (row.legacy_training) parts.push(badge(`v0.2 ${row.training_split || "corpus"}`, "approved"));
    if (row.new) parts.push(badge("new", "in_review"));
    if (row.holdout) parts.push(badge("holdout", "holdout"));
    if (row.future_training) parts.push(badge("future eligible", "approved"));
    return parts.length ? parts.join(" ") : `<span class="muted">Not in a training manifest</span>`;
  }

  function renderPatients(payload) {
    state.total = Number(payload.total || 0);
    const results = document.getElementById("patient-results");
    results.innerHTML = (payload.items || []).map((row) =>
      `<tr data-patient-key="${escapeHTML(row.key)}" tabindex="0" role="button" aria-label="View pipeline for ${escapeHTML(row.pseudonym)}">
        <td><strong>${escapeHTML(row.pseudonym)}</strong><br><small>${escapeHTML(row.case_id || "No case ID")}</small><br>${badge(row.status, row.issues?.length ? "needs_changes" : "")}</td>
        <td>${sourceSummary(row)}</td>
        <td>${longitudinalSummary(row)}</td>
        <td>${badge(row.review_status, row.clinician_approved ? "approved" : "")}<br><small>${escapeHTML(row.reviewer || "Unassigned")}</small></td>
        <td>${trainingSummary(row)}</td>
        <td><strong>${escapeHTML(row.next_action)}</strong>${row.issues?.length ? `<br><small>${escapeHTML(row.issues.map(words).join(", "))}</small>` : ""}</td>
      </tr>`
    ).join("");
    document.getElementById("patient-state").textContent = state.total
      ? ""
      : (state.query ? "No matching patients." : "No patients are available for this stage.");
    document.getElementById("patient-summary").textContent = `${number.format(state.total)} matching patient${state.total === 1 ? "" : "s"}`;
    const pagination = document.getElementById("patient-pagination");
    pagination.hidden = state.total === 0;
    document.getElementById("patient-page-summary").textContent = state.total
      ? `Showing ${number.format(state.offset + 1)}–${number.format(Math.min(state.offset + payload.items.length, state.total))} of ${number.format(state.total)}`
      : "";
    document.getElementById("patient-prev").disabled = state.offset === 0;
    document.getElementById("patient-next").disabled = state.offset + payload.items.length >= state.total;
    document.querySelectorAll("[data-patient-key]").forEach((row) => {
      row.addEventListener("click", () => openPatient(row.dataset.patientKey));
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openPatient(row.dataset.patientKey);
        }
      });
    });
  }

  async function loadOverview() {
    try {
      renderOverview(await fetchJSON("/data-dashboard/api/overview"));
    } catch (error) {
      showError(error.message);
    }
  }

  async function loadPatients() {
    if (state.controller) state.controller.abort();
    state.controller = new AbortController();
    document.getElementById("patient-state").textContent = "Loading patients…";
    const query = new URLSearchParams({
      q: state.query,
      stage: state.stage,
      sort: state.sort,
      offset: state.offset,
      limit: pageSize
    });
    try {
      const payload = await fetchJSON(`/data-dashboard/api/patients?${query}`, {signal: state.controller.signal});
      renderPatients(payload);
    } catch (error) {
      if (error.name === "AbortError") return;
      document.getElementById("patient-state").textContent = error.message;
      document.getElementById("patient-results").innerHTML = "";
    }
  }

  function renderPatientDetail(row) {
    document.getElementById("patient-detail-title").textContent = row.pseudonym;
    document.getElementById("patient-detail-subtitle").textContent =
      `${row.case_id || "No longitudinal case"} · ${row.next_action}`;
    document.getElementById("patient-detail-state").textContent = row.issues?.length
      ? `Inventory issues: ${row.issues.map(words).join(", ")}`
      : "";
    const extraction = row.extraction || {};
    const provenance = [
      ["Medfiles model", extraction.medfiles_model],
      ["Clinical-summary model", extraction.clinical_summary_model],
      ["Medfiles prompt", extraction.medfiles_prompt],
      ["Clinical-summary prompt", extraction.clinical_summary_prompt]
    ].filter(([, value]) => value);
    document.getElementById("patient-detail-results").innerHTML = `
      <div class="patient-pipeline">
        ${(row.timeline || []).map((event) => `<article>
          <span class="pipeline-dot"></span>
          <div><h3>${escapeHTML(event.stage)} ${badge(event.status, event.status === "approved" || event.status === "eligible" ? "approved" : "")}</h3>
          <p>${escapeHTML(event.detail)}</p></div>
        </article>`).join("")}
      </div>
      <section class="detail-section">
        <h3>Extraction provenance</h3>
        ${provenance.length ? `<dl>${provenance.map(([label, value]) => `<dt>${escapeHTML(label)}</dt><dd>${escapeHTML(value)}</dd>`).join("")}</dl>` : "<p class=\"muted\">No extraction provenance is available.</p>"}
      </section>`;
  }

  async function openPatient(key) {
    const dialog = document.getElementById("patient-detail");
    document.getElementById("patient-detail-title").textContent = "Patient pipeline";
    document.getElementById("patient-detail-subtitle").textContent = "Loading safe metadata…";
    document.getElementById("patient-detail-state").textContent = "";
    document.getElementById("patient-detail-results").innerHTML = "";
    dialog.showModal();
    try {
      renderPatientDetail(await fetchJSON(`/data-dashboard/api/patients/${encodeURIComponent(key)}`));
    } catch (error) {
      document.getElementById("patient-detail-state").textContent = error.message;
    }
  }

  async function refreshInventory() {
    const button = document.getElementById("dashboard-refresh");
    button.disabled = true;
    button.textContent = "Refreshing…";
    showError("");
    try {
      const payload = await fetchJSON("/data-dashboard/api/refresh", {
        method: "POST",
        headers: {"X-CSRFToken": root.dataset.csrfToken}
      });
      renderOverview(payload);
      state.offset = 0;
      await loadPatients();
    } catch (error) {
      showError(error.message);
    } finally {
      button.disabled = false;
      button.textContent = "Refresh inventory";
    }
  }

  let searchTimer;
  document.getElementById("patient-search").addEventListener("input", (event) => {
    state.query = event.target.value;
    state.offset = 0;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadPatients, 250);
  });
  document.getElementById("patient-stage").addEventListener("change", (event) => {
    state.stage = event.target.value;
    state.offset = 0;
    loadPatients();
  });
  document.getElementById("patient-sort").addEventListener("change", (event) => {
    state.sort = event.target.value;
    state.offset = 0;
    loadPatients();
  });
  document.getElementById("clear-patient-filters").addEventListener("click", () => {
    state.query = "";
    state.stage = "all";
    state.sort = "pseudonym";
    state.offset = 0;
    document.getElementById("patient-search").value = "";
    document.getElementById("patient-stage").value = "all";
    document.getElementById("patient-sort").value = "pseudonym";
    loadPatients();
    document.getElementById("patient-search").focus();
  });
  document.getElementById("patient-prev").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - pageSize);
    loadPatients();
  });
  document.getElementById("patient-next").addEventListener("click", () => {
    state.offset += pageSize;
    loadPatients();
  });
  document.getElementById("dashboard-refresh").addEventListener("click", refreshInventory);
  document.getElementById("patient-detail-close").addEventListener("click", () => {
    document.getElementById("patient-detail").close();
  });

  Promise.all([loadOverview(), loadPatients()]);
})();
