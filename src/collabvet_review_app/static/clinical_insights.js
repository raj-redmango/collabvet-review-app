"use strict";

(() => {
  const root = document.getElementById("overview-metrics");
  if (!root) return;

  const tabs = ["patterns", "pathways", "treatments", "medications", "safety", "cases", "graph", "knowledge", "runs"];
  const descriptions = {
    patterns: "Documented diagnoses remain distinct from documented behavioral patterns and differentials considered.",
    pathways: "Treatment links are descriptive. Differences between cases do not establish why a treatment was selected.",
    treatments: "Follow-up observations do not by themselves establish that an intervention caused an outcome.",
    medications: "Explicit attribution is shown separately from temporal association whenever the API provides it.",
    safety: "Safety is presented as documented criteria and evidence—not an unexplained risk score.",
    cases: "Only de-identified, derived evidence permitted by the API is displayed.",
    graph: "Clinician-friendly pathways, timelines, review state, and the API-provided graph projection.",
    knowledge: "Read-only VB-maintained knowledge shared with Collab.Vet.",
    runs: "Mining runs are immutable and versioned by source, schema, miner, prompt, and model."
  };
  const metricDefinitions = [
    ["cases", "Total unique cases", null, "Unique-case denominator"],
    ["visits", "Total visits or events", null, "Longitudinal events"],
    ["cases_with_patterns", "Cases with documented diagnoses or patterns", null, "Not exposed by current API"],
    ["cases_with_interventions", "Cases with interventions", null, "Not exposed by current API"],
    ["cases_with_outcomes", "Cases with follow-up outcomes", "follow_up_outcomes", "Documented linked follow-up"],
    ["cases_with_documented_improvement", "Cases with documented improvement", "improvements", "Descriptive outcome"],
    ["cases_with_no_meaningful_change", "Cases with no meaningful change", null, "Not exposed by current API"],
    ["cases_with_worsening", "Cases with worsening", null, "Not exposed by current API"],
    ["cases_with_adverse_effects", "Cases with adverse effects", "adverse_effects", "Medication or treatment effect documented"],
    ["distinct_medications", "Distinct recognized medications", "medications", "Canonical named agents"],
    ["cases_with_medication", "Cases with medication plans", "medications", "Medication documented"],
    ["cases_referred_to_training_support", "Cases referred to training support", "training_support", "Trainer, class, lesson, or consultant"],
    ["cases_with_provider_bite_caution", "Provider caution or bite history", "provider_caution", "Combined criterion exposed by current API"],
    ["partial_records", "Partial records", null, "Not exposed by current overview API"],
    ["records_with_quality_warnings", "Data-quality warnings", null, "Not exposed by current overview API"]
  ];
  const serverSearchTabs = new Set(["pathways", "treatments", "cases"]);
  const pageSize = {pathways: 10, treatments: 100, cases: 100};
  const resourceFor = {patterns: "patterns", pathways: "pathways", treatments: "interventions", safety: "safety", cases: "cases", knowledge: "knowledge", runs: "runs"};
  const state = {
    activeTab: "patterns", runId: "", vbId: "", overview: null,
    cache: new Map(), controllers: new Map(), searches: {}, offsets: {}, detailRows: [], detailSearch: ""
  };
  tabs.forEach((tab) => { state.searches[tab] = ""; state.offsets[tab] = 0; });

  const escapeHTML = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
  const words = (value) => String(value ?? "").replaceAll("_", " ");
  const alphabetical = (rows, key) => [...rows].sort((a, b) => String(a?.[key] ?? "").localeCompare(String(b?.[key] ?? ""), undefined, {sensitivity: "base"}));
  const array = (value) => Array.isArray(value) ? value : [];
  const compact = (value) => value === null || value === undefined || value === "" ? "—" : String(value);
  const badge = (value) => `<span class="badge">${escapeHTML(words(compact(value)))}</span>`;
  const listText = (value) => array(value).map((item) => typeof item === "object" ? compact(item.label || item.name || item.category || item.status) : compact(item)).join(", ") || "—";
  const scopeQuery = () => ({run_id: state.runId, vb_id: state.vbId});

  async function fetchJSON(path, params = {}, requestKey = path) {
    const previous = state.controllers.get(requestKey);
    if (previous) previous.abort();
    const controller = new AbortController();
    state.controllers.set(requestKey, controller);
    const query = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => { if (value !== "" && value !== null && value !== undefined) query.set(key, value); });
    const response = await fetch(`${path}${query.size ? `?${query}` : ""}`, {credentials: "same-origin", signal: controller.signal, headers: {Accept: "application/json"}});
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.error || `Clinical Insights request failed (${response.status}).`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function setTabState(tab, message, kind = "") {
    const node = document.querySelector(`[data-tab-state="${tab}"]`);
    node.textContent = message;
    node.classList.toggle("insights-error", kind === "error");
  }

  function currentItems(payload) { return array(payload?.items ?? payload?.rows ?? payload); }

  function populateFilters(overview) {
    const runSelect = document.getElementById("insights-run");
    const vbSelect = document.getElementById("insights-vb");
    const run = overview.run;
    if (run?.id && !runSelect.querySelector(`option[value="${CSS.escape(String(run.id))}"]`)) {
      runSelect.insertAdjacentHTML("beforeend", `<option value="${escapeHTML(run.id)}">${escapeHTML(run.external_run_id || run.id)}</option>`);
    }
    const vbs = array(overview.source_vbs || overview.vbs || overview.filters?.source_vbs);
    vbs.forEach((vb) => {
      const id = vb.id || vb.vb_id;
      if (id && !vbSelect.querySelector(`option[value="${CSS.escape(String(id))}"]`)) {
        vbSelect.insertAdjacentHTML("beforeend", `<option value="${escapeHTML(id)}">${escapeHTML(vb.display_name || vb.name || id)}</option>`);
      }
    });
  }

  function renderOverview(payload) {
    state.overview = payload;
    const totals = payload.totals || {};
    root.innerHTML = metricDefinitions.map(([field, label, detail, hint]) => {
      const available = Object.prototype.hasOwnProperty.call(totals, field);
      const value = available ? Number(totals[field] || 0).toLocaleString() : "Not exposed";
      return `<button type="button" class="metric-card" ${detail && available ? `data-metric="${detail}"` : "disabled"}>
        <span class="metric-label">${escapeHTML(label)}</span><span class="metric-value">${escapeHTML(value)}</span><small>${escapeHTML(hint)}</small></button>`;
    }).join("");
    const run = payload.run || {};
    const metadata = [
      ["Run", run.external_run_id || run.id], ["Source commit", run.source_commit],
      ["Schema", run.extraction_schema_version || run.schema_version], ["Miner", run.miner_version],
      ["Prompt", run.prompt_version], ["Model", run.model], ["Source cases", run.source_case_count],
      ["Source VBs", run.source_vb_count]
    ].filter(([, value]) => value !== undefined && value !== null && value !== "");
    document.getElementById("overview-metadata").innerHTML = metadata.map(([label, value]) => `<span><strong>${escapeHTML(label)}:</strong> ${escapeHTML(value)}</span>`).join("") || "<span>Run metadata was not supplied.</span>";
    document.getElementById("overview-status").textContent = "Loaded";
    populateFilters(payload);
    document.querySelectorAll("[data-metric]").forEach((button) => button.addEventListener("click", () => openMetric(button.dataset.metric)));
  }

  async function loadOverview() {
    document.getElementById("overview-status").textContent = "Loading…";
    document.getElementById("overview-error").hidden = true;
    try {
      renderOverview(await fetchJSON("/clinical-insights/api/overview", scopeQuery(), "overview"));
    } catch (error) {
      if (error.name === "AbortError") return;
      document.getElementById("overview-status").textContent = "Unavailable";
      const errorNode = document.getElementById("overview-error");
      errorNode.textContent = error.message;
      errorNode.hidden = false;
      root.innerHTML = "";
    }
  }

  function filterLocal(rows, term) {
    const query = term.trim().toLowerCase();
    return query ? rows.filter((row) => JSON.stringify(row).toLowerCase().includes(query)) : rows;
  }

  function table(headers, rows) {
    return `<div class="insights-table-wrap"><table class="insights-table"><thead><tr>${headers.map((header) => `<th>${escapeHTML(header)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
  }

  function renderPatterns(rows) {
    return table(["Canonical label", "Concept", "Status", "Cases", "Prevalence", "Evidence"], alphabetical(rows, "label").map((row) => `<tr data-detail-id="${escapeHTML(row.id)}"><td><strong>${escapeHTML(row.label)}</strong><br><small>${escapeHTML(row.canonical_key || row.pattern_id || "")}</small></td><td>${escapeHTML(words(row.concept_type))}</td><td>${badge(row.status)}</td><td>${escapeHTML(row.case_count)}</td><td>${escapeHTML(row.case_percentage ?? row.prevalence ?? "—")}${row.case_percentage !== undefined || row.prevalence !== undefined ? "%" : ""}</td><td>${escapeHTML(row.evidence_count)}</td></tr>`));
  }

  function renderPathways(rows) {
    return `<div class="insight-card-grid">${alphabetical(rows, "diagnosis_or_pattern").map((row) => `<article class="insight-card"><h3>${escapeHTML(row.diagnosis_or_pattern || row.label)}</h3><p>${badge(row.concept_type)} · ${escapeHTML(row.case_count || 0)} linked case(s)</p>${array(row.variants).map((variant) => `<section><strong>${escapeHTML(variant.treatment || variant.name || variant.strategy_label || variant.strategy_key)}</strong><p>${escapeHTML(words(variant.family || variant.treatment_family || ""))} · ${badge(variant.status)}</p><p>${escapeHTML(variant.rationale || variant.documented_rationale || "No documented rationale")}</p><small>${escapeHTML(variant.comparison_summary || "Insufficient follow-up for comparison")}</small></section>`).join("") || "<p>No linked treatments.</p>"}</article>`).join("")}</div>`;
  }

  function renderTreatments(rows) {
    return table(["Intervention", "Family", "Status", "Cases", "Episodes", "Linked outcomes"], alphabetical(rows, "name").map((row) => `<tr data-detail-id="${escapeHTML(row.id || row.name)}"><td><strong>${escapeHTML(row.name)}</strong></td><td>${escapeHTML(words(row.family))}</td><td>${badge(row.status)}</td><td>${escapeHTML(row.case_count)}</td><td>${escapeHTML(row.episode_count)}</td><td>${escapeHTML(listText(row.outcomes))}</td></tr>`));
  }

  function renderSafety(rows) {
    return table(["Criterion", "Status", "Cases", "Prevalence", "Evidence"], alphabetical(rows, "label").map((row) => `<tr data-detail-id="${escapeHTML(row.id)}"><td><strong>${escapeHTML(row.label)}</strong><br><small>${escapeHTML(row.pattern_id || "")}</small></td><td>${badge(row.status)}</td><td>${escapeHTML(row.case_count)}</td><td>${escapeHTML(row.case_percentage ?? "—")}${row.case_percentage !== undefined ? "%" : ""}</td><td>${escapeHTML(row.evidence_count)}</td></tr>`));
  }

  function renderCases(rows) {
    return table(["Case", "Source VB", "Species / signalment", "Visits / communications", "Date range", "Review", "Evidence"], alphabetical(rows, "external_case_id").map((row) => `<tr data-case-id="${escapeHTML(row.id)}"><td><strong>${escapeHTML(row.external_case_id)}</strong>${row.partial ? "<br><span class=\"badge\">partial record</span>" : ""}<br><small>${escapeHTML(row.missingness_summary || "")}</small></td><td>${escapeHTML(row.source_vb?.display_name || row.source_vb_name || "—")}</td><td>${escapeHTML(row.species || "—")}<br><small>${escapeHTML(row.signalment || "—")}</small></td><td>${escapeHTML(row.visit_count || 0)} / ${escapeHTML(row.communication_count || 0)}</td><td>${escapeHTML(row.first_event_date || "—")} → ${escapeHTML(row.last_event_date || "—")}</td><td>${badge(row.review_status)}</td><td>${row.evidence_available === false ? "No evidence" : "<button type=\"button\" class=\"secondary\">View evidence</button>"}</td></tr>`));
  }

  function renderKnowledge(rows) {
    return `<div class="insight-card-grid">${alphabetical(rows, "term").map((row) => `<article class="insight-card" data-detail-id="${escapeHTML(row.id)}"><h3>${escapeHTML(row.term)}</h3><p>${badge(row.category)} ${row.review_state ? badge(row.review_state) : ""}</p><p>${escapeHTML(row.definition || "No definition supplied.")}</p><p><strong>Aliases:</strong> ${escapeHTML(listText(row.aliases))}</p><p><strong>Guidance:</strong> ${escapeHTML(row.guidance || "—")}</p><p><strong>Formulary:</strong> ${escapeHTML(row.formulary_details || row.formulary || "—")}</p><p><strong>Warnings:</strong> ${escapeHTML(listText(row.warnings))}</p></article>`).join("")}</div>`;
  }

  function renderRuns(rows) {
    return table(["Run", "Source commit", "Schema", "Miner / prompt / model", "Cases", "VBs", "Status"], alphabetical(rows, "external_run_id").map((row) => `<tr><td><strong>${escapeHTML(row.external_run_id)}</strong>${row.is_active ? "<br><span class=\"badge approved\">active</span>" : ""}<br><small>${escapeHTML(row.created_at || "")}</small></td><td><code>${escapeHTML(row.source_commit)}</code></td><td>${escapeHTML(row.extraction_schema_version || row.schema_version || "—")}</td><td>${escapeHTML(row.miner_version || "—")} / ${escapeHTML(row.prompt_version || "—")} / ${escapeHTML(row.model || "deterministic")}</td><td>${escapeHTML(row.source_case_count)}</td><td>${escapeHTML(row.source_vb_count)}</td><td>${badge(row.status)}</td></tr>`));
  }

  function renderMedications(rows) {
    return `<div class="insight-card-grid">${alphabetical(rows, "medication_name").map((row) => `<article class="insight-card"><h3>${escapeHTML(row.medication_name || row.name || row.term)}</h3><p><strong>Aliases:</strong> ${escapeHTML(listText(row.aliases))}</p><p>${badge(row.status)} · ${escapeHTML(row.case_count || 0)} case(s)</p><p><strong>Dose / route / frequency:</strong> ${escapeHTML([row.dose, row.route, row.frequency].filter(Boolean).join(" · ") || "Not documented")}</p><p><strong>Rationale:</strong> ${escapeHTML(row.rationale || row.treatment_rationale || "—")}</p><p><strong>Adverse effects:</strong> ${escapeHTML(listText(row.adverse_effects))}</p><p><strong>Outcomes:</strong> ${escapeHTML(listText(row.outcomes))}</p><small>${escapeHTML(row.attribution || row.association || "No linked follow-up")}</small></article>`).join("")}</div>`;
  }

  const renderers = {patterns: renderPatterns, pathways: renderPathways, treatments: renderTreatments, medications: renderMedications, safety: renderSafety, cases: renderCases, knowledge: renderKnowledge, runs: renderRuns};

  function bindDetails(tab, rows) {
    document.querySelectorAll(`[data-tab-results="${tab}"] [data-case-id]`).forEach((node) => node.addEventListener("click", () => openCase(node.dataset.caseId)));
    document.querySelectorAll(`[data-tab-results="${tab}"] [data-detail-id]`).forEach((node) => node.addEventListener("click", () => openLocalDetail(tab, rows.find((row) => String(row.id || row.name) === node.dataset.detailId) || {})));
  }

  function updatePagination(tab, payload, rows) {
    const node = document.querySelector(`[data-pagination="${tab}"]`);
    if (!node) return;
    const total = Number(payload.total ?? rows.length);
    const offset = state.offsets[tab];
    node.hidden = false;
    node.querySelector(`[data-page-summary="${tab}"]`).textContent = total ? `Showing ${offset + 1}–${Math.min(offset + rows.length, total)} of ${total.toLocaleString()}` : "No data extracted";
    node.querySelector(`[data-page-prev="${tab}"]`).disabled = offset === 0;
    node.querySelector(`[data-page-next="${tab}"]`).disabled = offset + rows.length >= total;
  }

  async function loadTab(tab, force = false) {
    if (tab === "graph") {
      window.CollabVetClinicalGraph?.activate();
      return;
    }
    const search = state.searches[tab];
    const offset = state.offsets[tab];
    const cacheKey = JSON.stringify([tab, state.runId, state.vbId, serverSearchTabs.has(tab) ? search : "", offset]);
    setTabState(tab, "Loading…");
    try {
      let payload = force ? null : state.cache.get(cacheKey);
      if (!payload) {
        const params = {...scopeQuery()};
        if (serverSearchTabs.has(tab)) Object.assign(params, {q: search, offset, limit: pageSize[tab]});
        const path = tab === "medications" ? "/clinical-insights/api/metrics/medications" : `/clinical-insights/api/${resourceFor[tab]}`;
        payload = await fetchJSON(path, params, `tab:${tab}`);
        state.cache.set(cacheKey, payload);
      }
      const allRows = currentItems(payload);
      const rows = serverSearchTabs.has(tab) ? allRows : filterLocal(allRows, search);
      const results = document.querySelector(`[data-tab-results="${tab}"]`);
      results.innerHTML = rows.length ? renderers[tab](rows) : "";
      if (!allRows.length) setTabState(tab, "No data extracted for this run and VB selection.");
      else if (!rows.length) setTabState(tab, "No matching results.");
      else setTabState(tab, "");
      if (serverSearchTabs.has(tab)) updatePagination(tab, payload, rows);
      bindDetails(tab, rows);
    } catch (error) {
      if (error.name === "AbortError") return;
      setTabState(tab, error.message, "error");
      document.querySelector(`[data-tab-results="${tab}"]`).innerHTML = "";
    }
  }

  function activateTab(tab) {
    state.activeTab = tab;
    document.querySelectorAll("[data-tab]").forEach((button) => { const active = button.dataset.tab === tab; button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active)); });
    document.querySelectorAll("[data-tab-panel]").forEach((panel) => { panel.hidden = panel.dataset.tabPanel !== tab; });
    loadTab(tab);
  }

  function detailHTML(value, key = "") {
    if (Array.isArray(value)) return value.length ? `<ul>${value.map((item) => `<li>${typeof item === "object" ? detailHTML(item) : escapeHTML(item)}</li>`).join("")}</ul>` : "—";
    if (value && typeof value === "object") return `<div class="detail-object">${Object.entries(value).map(([childKey, child]) => `<div class="detail-key">${escapeHTML(words(childKey))}</div><div class="detail-value">${detailHTML(child, childKey)}</div>`).join("")}</div>`;
    return escapeHTML(compact(value));
  }

  function showDetail(title, subtitle, rows) {
    state.detailRows = Array.isArray(rows) ? rows : [rows];
    state.detailSearch = "";
    document.getElementById("detail-search").value = "";
    document.getElementById("detail-title").textContent = title;
    document.getElementById("detail-subtitle").textContent = subtitle;
    renderDetail();
    document.getElementById("insights-detail").showModal();
  }

  function renderDetail() {
    const rows = filterLocal(state.detailRows, state.detailSearch);
    document.getElementById("detail-state").textContent = rows.length ? "" : (state.detailRows.length ? "No matching results." : "No linked evidence was supplied.");
    document.getElementById("detail-results").innerHTML = rows.map((row) => detailHTML(row)).join("");
  }

  function openLocalDetail(tab, row) { showDetail(words(tab), "API-provided aggregate details.", row); }
  async function openMetric(metric) {
    showDetail("Metric details", "Loading supporting cases and evidence…", []);
    try { const payload = await fetchJSON(`/clinical-insights/api/metrics/${encodeURIComponent(metric)}`, scopeQuery(), "detail"); showDetail(words(metric), "Supporting cases, evidence dates, observation windows, and known limitations supplied by the API.", currentItems(payload)); }
    catch (error) { document.getElementById("detail-state").textContent = error.message; }
  }
  async function openCase(caseId) {
    showDetail("Case evidence", "Loading de-identified derived evidence…", []);
    try { const payload = await fetchJSON(`/clinical-insights/api/cases/${encodeURIComponent(caseId)}`, scopeQuery(), "detail"); showDetail("Case evidence", "De-identified derived evidence permitted by the API.", payload); }
    catch (error) { document.getElementById("detail-state").textContent = error.message; }
  }

  function resetForScope() {
    state.cache.clear();
    tabs.forEach((tab) => { state.offsets[tab] = 0; });
    loadOverview();
    loadTab(state.activeTab, true);
  }

  document.querySelectorAll("[data-description]").forEach((node) => { node.textContent = descriptions[node.dataset.description]; });
  document.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => activateTab(button.dataset.tab)));
  document.querySelectorAll("[data-search-tab]").forEach((input) => {
    let timer;
    input.addEventListener("input", () => { const tab = input.dataset.searchTab; state.searches[tab] = input.value; state.offsets[tab] = 0; clearTimeout(timer); timer = setTimeout(() => loadTab(tab, serverSearchTabs.has(tab)), serverSearchTabs.has(tab) ? 300 : 0); });
  });
  document.querySelectorAll("[data-clear-tab]").forEach((button) => button.addEventListener("click", () => { const tab = button.dataset.clearTab; const input = document.querySelector(`[data-search-tab="${tab}"]`); input.value = ""; state.searches[tab] = ""; state.offsets[tab] = 0; loadTab(tab, serverSearchTabs.has(tab)); input.focus(); }));
  document.querySelectorAll("[data-page-prev]").forEach((button) => button.addEventListener("click", () => { const tab = button.dataset.pagePrev; state.offsets[tab] = Math.max(0, state.offsets[tab] - pageSize[tab]); loadTab(tab, true); }));
  document.querySelectorAll("[data-page-next]").forEach((button) => button.addEventListener("click", () => { const tab = button.dataset.pageNext; state.offsets[tab] += pageSize[tab]; loadTab(tab, true); }));
  document.getElementById("insights-run").addEventListener("change", (event) => { state.runId = event.target.value; resetForScope(); });
  document.getElementById("insights-vb").addEventListener("change", (event) => { state.vbId = event.target.value; resetForScope(); });
  document.getElementById("detail-search").addEventListener("input", (event) => { state.detailSearch = event.target.value; renderDetail(); });
  document.getElementById("detail-clear").addEventListener("click", () => { state.detailSearch = ""; document.getElementById("detail-search").value = ""; renderDetail(); });

  loadOverview();
})();
