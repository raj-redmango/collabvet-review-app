"use strict";

(() => {
  const root = document.getElementById("clinical-insights-root");
  if (!root) return;

  const esc = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  }[char]));
  const words = (value) => String(value ?? "").replaceAll("_", " ");
  const array = (value) => Array.isArray(value) ? value : [];
  const text = (value, fallback = "Not documented") =>
    value === null || value === undefined || value === "" ? fallback : String(value);
  const truthy = (value) => value === true || value === "true";
  const truncate = (value, length) => {
    const string = String(value ?? "");
    return string.length > length ? `${string.slice(0, length - 1)}…` : string;
  };
  const debounce = (fn, delay = 350) => {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), delay);
    };
  };
  const badge = (value, className = "") =>
    `<span class="badge ${esc(className)}">${esc(words(text(value, "Unknown")))}</span>`;
  const hasDisplayIdentity = (item) => {
    const status = String(item?.identity_mapping_status || "").toLowerCase();
    return Boolean(item?.patient_display_name) &&
      !["unmapped", "unlinked", "not_linked", "not linked", "missing"].includes(status);
  };

  async function requestJSON(path, options = {}) {
    const headers = {Accept: "application/json", ...(options.headers || {})};
    if (options.body) {
      headers["Content-Type"] = "application/json";
      headers["X-CSRFToken"] = root.dataset.csrfToken;
    }
    const response = await fetch(path, {
      credentials: "same-origin",
      ...options,
      headers,
      body: options.body ? JSON.stringify(options.body) : undefined
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.error || `Clinical graph request failed (${response.status}).`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function queryPath(path, params) {
    const query = new URLSearchParams();
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value !== "" && value !== null && value !== undefined && value !== false) {
        query.set(key, value);
      }
    });
    return `${path}${query.size ? `?${query}` : ""}`;
  }

  function projectionLabel(source, corpus = false) {
    if (source === "neo4j") return corpus ? "Live Neo4j corpus projection" : "Live Neo4j projection";
    if (source) return corpus ? "PostgreSQL corpus graph preview" : "PostgreSQL graph preview";
    return "Graph projection source not reported";
  }

  class ClinicalNetwork {
    constructor(host, {nodes, edges, patient, projectionSource, corpus = false, onSelect}) {
      this.host = host;
      this.rawNodes = nodes;
      this.rawEdges = edges;
      this.patient = patient;
      this.projectionSource = projectionSource;
      this.corpus = corpus;
      this.onSelect = onSelect;
      this.width = 1440;
      this.height = 840;
      this.scale = 1;
      this.pan = {x: 0, y: 0};
      this.selected = null;
      this.showEdgeLabels = false;
      this.nodeRefs = new Map();
      this.edgeRefs = new Map();
      this.drag = null;
      this.panDrag = null;
      this.prepare();
      this.mount();
      this.layout();
      this.fit();
    }

    keyOf(node) {
      return node.node_key || node.concept_key || node.key;
    }

    prepare() {
      const stageOrder = ["history", "observations", "assessment", "plan", "monitoring", "outcomes"];
      this.nodes = this.rawNodes.map((raw, index) => {
        const key = this.keyOf(raw);
        const seed = this.hash(key);
        const stageIndex = Math.max(0, stageOrder.indexOf(raw.pathway_stage));
        const angle = (stageIndex / stageOrder.length) * Math.PI * 2 + seed * 0.8;
        const radius = 180 + (index % 4) * 75 + seed * 35;
        return {
          key,
          raw,
          x: this.width / 2 + Math.cos(angle) * radius,
          y: this.height / 2 + Math.sin(angle) * radius,
          radius: raw.is_quarantined ? 30 : 27
        };
      });
      if (this.patient) {
        this.nodes.push({
          key: "__patient__",
          raw: {headline: this.patient.patient_display_name || "Patient name not linked"},
          patient: true,
          fixed: true,
          x: this.width / 2,
          y: this.height / 2,
          radius: 45
        });
      }
      const keys = new Set(this.nodes.map((node) => node.key));
      this.edges = this.rawEdges.map((raw, index) => ({
        key: raw.edge_key || `${raw.source_concept_key}->${raw.target_concept_key}#${index}`,
        source: raw.source_node_key || raw.source_concept_key,
        target: raw.target_node_key || raw.target_concept_key,
        raw
      })).filter((edge) => keys.has(edge.source) && keys.has(edge.target));
      if (this.patient) {
        this.nodes.filter((node) => !node.patient).forEach((node) => {
          this.edges.push({
            key: `patient:${node.key}`,
            source: "__patient__",
            target: node.key,
            patientEdge: true,
            raw: {relationship_label: "HAS_CLAIM"}
          });
        });
      }
    }

    hash(value) {
      let hash = 2166136261;
      for (const char of String(value)) {
        hash ^= char.charCodeAt(0);
        hash = Math.imul(hash, 16777619);
      }
      return (hash >>> 0) / 4294967295;
    }

    mount() {
      this.host.innerHTML = `
        <div class="network-shell">
          <div class="network-toolbar">
            <span class="graph-projection-badge">${esc(projectionLabel(this.projectionSource, this.corpus))}</span>
            <span class="network-count">${this.rawNodes.length.toLocaleString()} clinical ${this.corpus ? "concepts" : "claims"} · ${this.rawEdges.length.toLocaleString()} relationships</span>
            <label class="checkbox"><input type="checkbox" data-network-edge-labels> Relationship labels</label>
            <button type="button" class="secondary" data-network-zoom-out aria-label="Zoom out">−</button>
            <span data-network-zoom class="network-zoom">100%</span>
            <button type="button" class="secondary" data-network-zoom-in aria-label="Zoom in">+</button>
            <button type="button" class="secondary" data-network-fit>Fit</button>
            <button type="button" class="secondary" data-network-reset>Reset</button>
            <button type="button" class="secondary" data-network-fullscreen>Fullscreen</button>
          </div>
          <div class="network-stage" tabindex="0" role="application"
            aria-label="${this.corpus ? "Clinical corpus" : "Patient case"} relationship graph">
            <svg viewBox="0 0 ${this.width} ${this.height}" aria-hidden="true">
              <defs>
                <marker id="review-graph-arrow" viewBox="0 0 10 10" refX="9" refY="5"
                  markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                  <path d="M 0 0 L 10 5 L 0 10 z"></path>
                </marker>
              </defs>
              <g class="network-viewport">
                <g class="network-edges"></g>
                <g class="network-nodes"></g>
              </g>
            </svg>
          </div>
        </div>`;
      this.shell = this.host.querySelector(".network-shell");
      this.stage = this.host.querySelector(".network-stage");
      this.svg = this.host.querySelector("svg");
      this.viewport = this.host.querySelector(".network-viewport");
      this.edgeLayer = this.host.querySelector(".network-edges");
      this.nodeLayer = this.host.querySelector(".network-nodes");
      this.zoomText = this.host.querySelector("[data-network-zoom]");
      this.host.querySelector("[data-network-edge-labels]").addEventListener("change", (event) => {
        this.showEdgeLabels = event.target.checked;
        this.draw();
      });
      this.host.querySelector("[data-network-zoom-in]").addEventListener("click", () => this.zoom(1.18));
      this.host.querySelector("[data-network-zoom-out]").addEventListener("click", () => this.zoom(0.84));
      this.host.querySelector("[data-network-fit]").addEventListener("click", () => this.fit());
      this.host.querySelector("[data-network-reset]").addEventListener("click", () => {
        this.prepare();
        this.nodeRefs.clear();
        this.edgeRefs.clear();
        this.edgeLayer.innerHTML = "";
        this.nodeLayer.innerHTML = "";
        this.layout();
        this.fit();
      });
      this.host.querySelector("[data-network-fullscreen]").addEventListener("click", async () => {
        if (document.fullscreenElement) await document.exitFullscreen();
        else await this.shell.requestFullscreen?.();
      });
      this.stage.addEventListener("wheel", (event) => {
        event.preventDefault();
        this.zoom(event.deltaY < 0 ? 1.1 : 0.9);
      }, {passive: false});
      this.stage.addEventListener("pointerdown", (event) => {
        if (event.target.closest(".network-node")) return;
        this.panDrag = {x: event.clientX, y: event.clientY, pan: {...this.pan}};
        this.stage.setPointerCapture(event.pointerId);
      });
      this.stage.addEventListener("pointermove", (event) => this.pointerMove(event));
      this.stage.addEventListener("pointerup", (event) => {
        this.drag = null;
        this.panDrag = null;
        this.stage.releasePointerCapture?.(event.pointerId);
      });
      this.stage.addEventListener("pointercancel", () => {
        this.drag = null;
        this.panDrag = null;
      });
    }

    layout() {
      const byKey = new Map(this.nodes.map((node) => [node.key, node]));
      for (let tick = 0; tick < 150; tick += 1) {
        const force = new Map(this.nodes.map((node) => [node.key, {x: 0, y: 0}]));
        for (let left = 0; left < this.nodes.length; left += 1) {
          for (let right = left + 1; right < this.nodes.length; right += 1) {
            const a = this.nodes[left];
            const b = this.nodes[right];
            let dx = b.x - a.x;
            let dy = b.y - a.y;
            const distanceSquared = Math.max(dx * dx + dy * dy, 100);
            const distance = Math.sqrt(distanceSquared);
            dx /= distance;
            dy /= distance;
            const strength = Math.min(15, 10500 / distanceSquared);
            if (!a.fixed) {
              force.get(a.key).x -= dx * strength;
              force.get(a.key).y -= dy * strength;
            }
            if (!b.fixed) {
              force.get(b.key).x += dx * strength;
              force.get(b.key).y += dy * strength;
            }
          }
        }
        this.edges.forEach((edge) => {
          const source = byKey.get(edge.source);
          const target = byKey.get(edge.target);
          if (!source || !target) return;
          const dx = target.x - source.x;
          const dy = target.y - source.y;
          const distance = Math.max(Math.hypot(dx, dy), 1);
          const ideal = edge.patientEdge ? 235 : 150;
          const pull = (distance - ideal) * 0.018;
          if (!source.fixed) {
            force.get(source.key).x += (dx / distance) * pull;
            force.get(source.key).y += (dy / distance) * pull;
          }
          if (!target.fixed) {
            force.get(target.key).x -= (dx / distance) * pull;
            force.get(target.key).y -= (dy / distance) * pull;
          }
        });
        this.nodes.forEach((node) => {
          if (node.fixed) return;
          const itemForce = force.get(node.key);
          itemForce.x += (this.width / 2 - node.x) * 0.0025;
          itemForce.y += (this.height / 2 - node.y) * 0.0025;
          node.x = Math.max(60, Math.min(this.width - 60, node.x + itemForce.x * 0.76));
          node.y = Math.max(60, Math.min(this.height - 60, node.y + itemForce.y * 0.76));
        });
      }
      this.buildElements();
      this.draw();
    }

    buildElements() {
      const namespace = "http://www.w3.org/2000/svg";
      this.edges.forEach((edge) => {
        if (this.edgeRefs.has(edge.key)) return;
        const group = document.createElementNS(namespace, "g");
        group.classList.add("network-edge");
        if (edge.patientEdge) group.classList.add("patient-edge");
        const hitLine = document.createElementNS(namespace, "line");
        hitLine.classList.add("network-edge-hit");
        group.append(hitLine);
        const line = document.createElementNS(namespace, "line");
        line.setAttribute("marker-end", "url(#review-graph-arrow)");
        group.append(line);
        const label = document.createElementNS(namespace, "text");
        label.textContent = words(edge.raw.relationship_label || "");
        group.append(label);
        if (!edge.patientEdge) {
          group.setAttribute("tabindex", "0");
          group.setAttribute("role", "button");
          group.setAttribute("aria-label", `Relationship: ${words(edge.raw.relationship_label)}`);
          group.addEventListener("click", () => this.select("edge", edge));
          group.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") this.select("edge", edge);
          });
        }
        this.edgeLayer.append(group);
        this.edgeRefs.set(edge.key, {group, hitLine, line, label});
      });
      this.nodes.forEach((node) => {
        if (this.nodeRefs.has(node.key)) return;
        const group = document.createElementNS(namespace, "g");
        group.classList.add("network-node");
        if (node.patient) group.classList.add("patient-node");
        if (node.raw.is_quarantined) group.classList.add("quarantined-node");
        if (["confirmed", "corrected"].includes(node.raw.review_state)) group.classList.add("reviewed-node");
        if (node.raw.lifecycle_status === "retired") group.classList.add("retired-node");
        group.setAttribute("tabindex", "0");
        group.setAttribute("role", "button");
        group.setAttribute("aria-label", node.patient ? `Patient: ${node.raw.headline}` : `${words(node.raw.clinical_category)}: ${node.raw.headline}`);
        const circle = document.createElementNS(namespace, "circle");
        circle.setAttribute("r", node.radius);
        group.append(circle);
        const shortLabel = document.createElementNS(namespace, "text");
        shortLabel.classList.add("network-node-label");
        shortLabel.setAttribute("text-anchor", "middle");
        shortLabel.setAttribute("y", node.radius + 17);
        shortLabel.textContent = truncate(node.raw.headline, 28);
        group.append(shortLabel);
        group.addEventListener("click", () => {
          if (!node.patient) this.select("node", node);
        });
        group.addEventListener("keydown", (event) => {
          if (!node.patient && (event.key === "Enter" || event.key === " ")) {
            this.select("node", node);
          }
        });
        group.addEventListener("pointerdown", (event) => {
          event.stopPropagation();
          this.drag = {node, pointerId: event.pointerId};
          this.stage.setPointerCapture(event.pointerId);
        });
        this.nodeLayer.append(group);
        this.nodeRefs.set(node.key, {group, circle, label: shortLabel});
      });
    }

    pointerMove(event) {
      if (this.drag) {
        const rect = this.svg.getBoundingClientRect();
        const viewX = ((event.clientX - rect.left) / rect.width) * this.width;
        const viewY = ((event.clientY - rect.top) / rect.height) * this.height;
        this.drag.node.x = (viewX - this.pan.x) / this.scale;
        this.drag.node.y = (viewY - this.pan.y) / this.scale;
        this.draw();
      } else if (this.panDrag) {
        const rect = this.svg.getBoundingClientRect();
        this.pan.x = this.panDrag.pan.x + ((event.clientX - this.panDrag.x) / rect.width) * this.width;
        this.pan.y = this.panDrag.pan.y + ((event.clientY - this.panDrag.y) / rect.height) * this.height;
        this.draw();
      }
    }

    select(kind, item) {
      this.selected = `${kind}:${item.key}`;
      this.draw();
      this.onSelect?.(kind, item.raw);
    }

    draw() {
      const byKey = new Map(this.nodes.map((node) => [node.key, node]));
      this.viewport.setAttribute("transform", `translate(${this.pan.x} ${this.pan.y}) scale(${this.scale})`);
      this.zoomText.textContent = `${Math.round(this.scale * 100)}%`;
      this.edges.forEach((edge) => {
        const source = byKey.get(edge.source);
        const target = byKey.get(edge.target);
        const ref = this.edgeRefs.get(edge.key);
        if (!source || !target || !ref) return;
        const dx = target.x - source.x;
        const dy = target.y - source.y;
        const distance = Math.max(Math.hypot(dx, dy), 1);
        const x1 = source.x + (dx / distance) * source.radius;
        const y1 = source.y + (dy / distance) * source.radius;
        const x2 = target.x - (dx / distance) * (target.radius + 5);
        const y2 = target.y - (dy / distance) * (target.radius + 5);
        [["x1", x1], ["y1", y1], ["x2", x2], ["y2", y2]].forEach(([key, value]) => {
          ref.hitLine.setAttribute(key, value);
          ref.line.setAttribute(key, value);
        });
        ref.label.setAttribute("x", (source.x + target.x) / 2);
        ref.label.setAttribute("y", (source.y + target.y) / 2 - 8);
        ref.label.setAttribute(
          "visibility",
          this.showEdgeLabels && this.scale >= 0.88 && !edge.patientEdge ? "visible" : "hidden"
        );
        ref.group.classList.toggle("temporal-edge", truthy(edge.raw.is_temporal_only));
        ref.group.classList.toggle("selected", this.selected === `edge:${edge.key}`);
      });
      this.nodes.forEach((node) => {
        const ref = this.nodeRefs.get(node.key);
        if (!ref) return;
        ref.group.setAttribute("transform", `translate(${node.x} ${node.y})`);
        ref.group.classList.toggle("selected", this.selected === `node:${node.key}`);
        const visible = node.patient || this.scale >= 0.68 || this.selected === `node:${node.key}`;
        ref.label.setAttribute("visibility", visible ? "visible" : "hidden");
        ref.label.textContent = truncate(node.raw.headline, this.scale >= 1.12 ? 48 : 28);
      });
    }

    zoom(factor) {
      this.scale = Math.max(0.28, Math.min(2.8, this.scale * factor));
      this.draw();
    }

    fit() {
      if (!this.nodes.length) return;
      const minX = Math.min(...this.nodes.map((node) => node.x - node.radius));
      const maxX = Math.max(...this.nodes.map((node) => node.x + node.radius));
      const minY = Math.min(...this.nodes.map((node) => node.y - node.radius));
      const maxY = Math.max(...this.nodes.map((node) => node.y + node.radius + 25));
      const graphWidth = Math.max(maxX - minX, 1);
      const graphHeight = Math.max(maxY - minY, 1);
      this.scale = Math.max(0.28, Math.min(1.5, Math.min((this.width - 130) / graphWidth, (this.height - 110) / graphHeight)));
      this.pan.x = (this.width - graphWidth * this.scale) / 2 - minX * this.scale;
      this.pan.y = (this.height - graphHeight * this.scale) / 2 - minY * this.scale;
      this.draw();
    }
  }

  const state = {
    initialized: false,
    activeMode: "case",
    activeView: "overview",
    summary: null,
    vocabulary: null,
    cases: [],
    total: 0,
    offset: 0,
    limit: 25,
    selectedCase: null,
    relationships: null,
    viewCache: new Map(),
    viewSearch: {pathway: "", timeline: "", review: "", network: ""},
    caseController: null,
    requestedView: "overview"
  };

  function setState(id, message, error = false) {
    const node = document.getElementById(id);
    node.textContent = message;
    node.classList.toggle("insights-error", error);
  }

  async function bootstrap() {
    if (state.initialized) return;
    state.initialized = true;
    setState("graph-case-status", "Loading the complete graph corpus…");
    try {
      const [summary, vocabulary, overview, runs] = await Promise.all([
        requestJSON("/clinical-insights/api/graph/summary"),
        requestJSON("/clinical-insights/api/graph/vocabulary"),
        requestJSON("/clinical-insights/api/overview"),
        requestJSON("/clinical-insights/api/runs")
      ]);
      state.summary = summary;
      state.vocabulary = vocabulary;
      renderSummary();
      populateFilters(overview, runs);
      const pageParams = new URLSearchParams(window.location.search);
      const requestedCase = pageParams.get("patient") || pageParams.get("case");
      const requestedScope = pageParams.get("scope");
      const requestedView = pageParams.get("view");
      if (["overview", "pathway", "timeline", "review", "network"].includes(requestedView)) {
        state.requestedView = requestedView;
      }
      if (requestedCase) {
        document.getElementById("graph-case-search").value = requestedCase;
      }
      if (requestedScope === "corpus") {
        switchMode("corpus");
        return;
      }
      await loadCases();
    } catch (error) {
      setState("graph-case-status", error.message, true);
    }
  }

  function renderSummary() {
    const summary = state.summary || {};
    document.getElementById("graph-summary-badges").innerHTML = [
      `${Number(summary.case_count || 0).toLocaleString()} cases`,
      `${Number(summary.claim_count || 0).toLocaleString()} clinical claims`,
      `${Number(summary.relationship_count || 0).toLocaleString()} relationships`,
      `${Number(summary.held_item_count || 0).toLocaleString()} held`
    ].map((item) => badge(item)).join("");
  }

  function populateFilters(overview, runs) {
    const vbSelect = document.getElementById("graph-vb-filter");
    array(overview.source_vbs).forEach((vb) => {
      const option = document.createElement("option");
      option.value = vb.id || vb.source_vb_id;
      option.textContent = vb.display_name;
      vbSelect.append(option);
    });
    const runSelect = document.getElementById("graph-run-filter");
    array(runs.items || runs).forEach((run) => {
      const option = document.createElement("option");
      option.value = run.id;
      option.textContent = run.external_run_id || run.id;
      runSelect.append(option);
    });
  }

  function caseParams() {
    return {
      q: document.getElementById("graph-case-search").value.trim(),
      source_vb_id: document.getElementById("graph-vb-filter").value,
      mining_run_id: document.getElementById("graph-run-filter").value,
      review_state: document.getElementById("graph-review-filter").value,
      include_quarantined: document.getElementById("graph-held-filter").checked,
      offset: state.offset,
      limit: state.limit
    };
  }

  async function loadCases() {
    state.caseController?.abort();
    state.caseController = new AbortController();
    setState("graph-case-status", "Loading cases…");
    try {
      const payload = await requestJSON(
        queryPath("/clinical-insights/api/graph/cases", caseParams()),
        {signal: state.caseController.signal}
      );
      state.cases = array(payload.items);
      state.total = Number(payload.total || 0);
      renderCases();
      setState(
        "graph-case-status",
        state.total ? `${state.total.toLocaleString()} cases are reachable through server-side search and pagination.` : "No matching cases."
      );
      if (!state.selectedCase && state.cases[0]) selectCase(state.cases[0]);
    } catch (error) {
      if (error.name !== "AbortError") setState("graph-case-status", error.message, true);
    }
  }

  function renderCases() {
    document.getElementById("graph-case-total").textContent = `${state.total.toLocaleString()} total cases`;
    const list = document.getElementById("graph-case-list");
    list.innerHTML = state.cases.map((item) => {
      const linked = hasDisplayIdentity(item);
      return `<button type="button" class="graph-case-item${state.selectedCase?.id === item.id ? " active" : ""}"
        data-graph-case-id="${esc(item.id)}">
        <strong>${esc(linked ? item.patient_display_name : "Patient name not linked")}</strong>
        ${!linked ? badge("Identity not linked", "needs_changes") : ""}
        ${linked ? badge(`${words(item.identity_mapping_status)} identity`) : ""}
        <small>${esc(item.case_id)} · ${esc(item.source_vb?.display_name || "Source VB unavailable")}</small>
        <small>${Number(item.node_count || 0)} claims · ${Number(item.edge_count || 0)} relationships</small>
        <span>${badge(item.review_status)}${item.quarantine_count ? badge(`${item.quarantine_count} held`, "holdout") : ""}</span>
      </button>`;
    }).join("") || `<div class="graph-empty">No matching results.</div>`;
    list.querySelectorAll("[data-graph-case-id]").forEach((button) => {
      button.addEventListener("click", () => {
        const item = state.cases.find((row) => row.id === button.dataset.graphCaseId);
        if (item) selectCase(item);
      });
    });
    const start = state.total ? state.offset + 1 : 0;
    document.getElementById("graph-case-page").textContent =
      `${start}–${Math.min(state.offset + state.cases.length, state.total)} of ${state.total.toLocaleString()}`;
    document.getElementById("graph-case-prev").disabled = state.offset === 0;
    document.getElementById("graph-case-next").disabled = state.offset + state.cases.length >= state.total;
  }

  async function selectCase(item) {
    state.selectedCase = item;
    state.activeView = state.requestedView;
    state.viewCache.clear();
    renderCases();
    document.getElementById("graph-case-empty").hidden = true;
    document.getElementById("graph-case-content").hidden = false;
    document.querySelectorAll("[data-graph-view]").forEach((button) => {
      const active = button.dataset.graphView === state.activeView;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    renderCaseHeader(item);
    setState("graph-view-state", "Loading case relationships…");
    try {
      state.relationships = await requestJSON(queryPath(
        `/clinical-insights/api/graph/cases/${encodeURIComponent(item.id)}/relationships`,
        {include_quarantined: document.getElementById("graph-held-filter").checked}
      ));
      setState("graph-view-state", "");
      renderCurrentView();
    } catch (error) {
      setState("graph-view-state", error.message, true);
    }
  }

  function renderCaseHeader(item) {
    const linked = hasDisplayIdentity(item);
    document.getElementById("graph-case-header").innerHTML = `
      <div>
        <span class="graph-eyebrow">Patient case</span>
        <h3>${esc(linked ? item.patient_display_name : "Patient name not linked")}</h3>
        <p>${esc(item.case_id)} · ${esc(item.species || "")}</p>
      </div>
      <div class="graph-case-metadata">
        ${!linked ? badge("Identity not linked", "needs_changes") : ""}
        ${linked ? badge(`${words(item.identity_mapping_status)} identity`) : ""}
        ${badge(item.source_vb?.display_name || "Source VB unavailable")}
        ${badge(item.review_status)}
        ${item.quarantine_count ? badge(`${item.quarantine_count} held`, "holdout") : ""}
        <span>${esc(item.first_event_date || "Date unknown")} → ${esc(item.last_event_date || "Date unknown")}</span>
      </div>`;
  }

  function nodeMatches(node, query) {
    if (!query) return true;
    return [
      node.headline, node.explanation, node.clinical_category, node.pathway_stage,
      node.stage_label, node.epistemic_kind, node.epistemic_status, node.review_state,
      node.status_note
    ].some((value) => String(value || "").toLowerCase().includes(query));
  }

  function viewSearch(view, placeholder, onChange) {
    return `<div class="graph-view-search">
      <label class="sr-only" for="graph-${view}-search">Search ${esc(view)}</label>
      <input id="graph-${view}-search" type="search" value="${esc(state.viewSearch[view] || "")}"
        placeholder="${esc(placeholder)}">
      <button type="button" class="secondary" id="graph-${view}-clear">Clear</button>
    </div>`;
  }

  function bindViewSearch(view, render) {
    const input = document.getElementById(`graph-${view}-search`);
    const clear = document.getElementById(`graph-${view}-clear`);
    if (!input || !clear) return;
    input.addEventListener("input", debounce(() => {
      state.viewSearch[view] = input.value;
      render();
    }, 180));
    clear.addEventListener("click", () => {
      state.viewSearch[view] = "";
      render();
    });
  }

  function renderCurrentView() {
    if (!state.relationships) return;
    if (state.activeView === "overview") renderCaseOverview();
    else if (state.activeView === "network") renderNetwork();
    else loadAuxiliaryView(state.activeView);
  }

  function renderCaseOverview() {
    const payload = state.relationships;
    const nodes = array(payload.nodes);
    const graph = payload.graph || state.selectedCase;
    const categories = new Set(nodes.map((node) => node.clinical_category).filter(Boolean));
    const documented = nodes.filter((node) => !["inferred", "temporal"].includes(node.epistemic_status)).length;
    document.getElementById("graph-case-view").innerHTML = `
      <div class="graph-projection-line">
        ${badge(projectionLabel(payload.projection_source), payload.projection_source === "neo4j" ? "approved" : "")}
        <span>Projection wording follows the API response; it is never inferred from the visualization.</span>
      </div>
      <div class="graph-overview-grid">
        <article><small>Clinical claims</small><strong>${nodes.length}</strong></article>
        <article><small>Relationships</small><strong>${array(payload.edges).length}</strong></article>
        <article><small>Clinical categories</small><strong>${categories.size}</strong></article>
        <article><small>Documented claims</small><strong>${documented}</strong></article>
        <article><small>Evidence references</small><strong>${nodes.reduce((sum, node) => sum + Number(node.evidence_count || 0), 0)}</strong></article>
        <article><small>Held for review</small><strong>${nodes.filter((node) => node.is_quarantined).length}</strong></article>
      </div>
      <div class="graph-overview-columns">
        <section>
          <h4>Clinical record</h4>
          <dl>
            <dt>Patient</dt><dd>${esc(graph.patient_display_name || "Patient name not linked")}</dd>
            <dt>Identity</dt><dd>${esc(words(graph.identity_mapping_status || "not reported"))}</dd>
            <dt>Source VB</dt><dd>${esc(graph.source_vb?.display_name || "Not reported")}</dd>
            <dt>Date range</dt><dd>${esc(graph.first_event_date || "Unknown")} → ${esc(graph.last_event_date || "Unknown")}</dd>
          </dl>
        </section>
        <section>
          <h4>Interpretation</h4>
          <p>Claims describe what was documented in this case. A relationship may indicate
            sequence or association and must not be interpreted as proof of causality.</p>
          <button type="button" class="secondary" id="overview-open-network">Open graph explorer</button>
        </section>
      </div>`;
    document.getElementById("overview-open-network").addEventListener("click", () => activateView("network"));
  }

  async function loadAuxiliaryView(view, force = false) {
    const caseId = state.selectedCase.id;
    const cacheKey = `${caseId}:${view}`;
    setState("graph-view-state", `Loading ${words(view)}…`);
    try {
      let payload = force ? null : state.viewCache.get(cacheKey);
      if (!payload) {
        const params = view === "review" ? {
          q: state.viewSearch.review,
          limit: 50,
          include_quarantined: document.getElementById("graph-held-filter").checked
        } : {};
        const endpoint = view === "review" ? "review-table" : view;
        payload = await requestJSON(queryPath(
          `/clinical-insights/api/graph/cases/${encodeURIComponent(caseId)}/${endpoint}`,
          params
        ));
        state.viewCache.set(cacheKey, payload);
      }
      setState("graph-view-state", "");
      if (view === "pathway") renderPathway(payload);
      else if (view === "timeline") renderTimeline(payload);
      else renderReviewTable(payload);
    } catch (error) {
      setState("graph-view-state", error.message, true);
    }
  }

  function renderPathway(payload) {
    const query = state.viewSearch.pathway.trim().toLowerCase();
    const relationships = array(payload.relationships);
    const incoming = new Map();
    relationships.forEach((edge) => {
      const values = incoming.get(edge.target_node_key) || [];
      values.push(edge);
      incoming.set(edge.target_node_key, values);
    });
    const stages = array(payload.stages).map((stage) => ({
      ...stage,
      nodes: array(stage.nodes).filter((node) => nodeMatches(node, query))
    }));
    document.getElementById("graph-case-view").innerHTML = `
      ${viewSearch("pathway", "Search claims, categories, stages, status, or evidence…")}
      <div class="clinical-pathway-lanes">
        ${stages.map((stage) => `<section>
          <header><span>${Number(stage.order || 0) + 1}</span><strong>${esc(stage.label)}</strong></header>
          <div>${stage.nodes.map((node) => `<button type="button" class="pathway-claim${node.is_quarantined ? " held" : ""}"
            data-pathway-node="${esc(node.node_key)}">
            <small>${esc(words(node.clinical_category))}</small>
            <strong>${esc(node.headline)}</strong>
            <span>${esc(node.recorded_at || "Not dated")} · ${Number(node.evidence_count || 0)} evidence</span>
            ${array(incoming.get(node.node_key)).map((edge) =>
              `<em>${esc(words(edge.relationship_label))}${edge.is_temporal_only ? " · sequence only" : ""}</em>`
            ).join("")}
          </button>`).join("") || `<p class="muted">No matching claims.</p>`}</div>
        </section>`).join("")}
      </div>
      <p class="graph-truth-note">Arrows and “sequence only” labels describe API-provided relationships.
        Sequence does not prove causation.</p>`;
    document.querySelectorAll("[data-pathway-node]").forEach((button) => {
      button.addEventListener("click", () => {
        const node = array(state.relationships.nodes).find((item) => item.node_key === button.dataset.pathwayNode);
        if (node) showInspector("node", node, document.getElementById("graph-case-view"));
      });
    });
    bindViewSearch("pathway", () => renderPathway(payload));
  }

  function renderTimeline(payload) {
    const query = state.viewSearch.timeline.trim().toLowerCase();
    const entries = array(payload.entries).map((entry) => ({
      ...entry,
      nodes: array(entry.nodes).filter((node) => nodeMatches(node, query))
    }));
    const undated = array(payload.undated_nodes).filter((node) => nodeMatches(node, query));
    document.getElementById("graph-case-view").innerHTML = `
      ${viewSearch("timeline", "Search timeline claims, categories, status, or evidence…")}
      <ol class="clinical-graph-timeline">
        ${entries.map((entry) => `<li>
          <time>${esc(entry.date)}</time>
          <div>${entry.nodes.map((node) => timelineCard(node)).join("") || `<p class="muted">No matching claims.</p>`}</div>
        </li>`).join("")}
        ${undated.length ? `<li><time>Not dated</time><div>${undated.map((node) => timelineCard(node)).join("")}</div></li>` : ""}
      </ol>
      <p class="graph-truth-note">${esc(payload.undated_note || "")}</p>`;
    document.querySelectorAll("[data-timeline-node]").forEach((button) => {
      button.addEventListener("click", () => {
        const node = array(state.relationships.nodes).find((item) => item.node_key === button.dataset.timelineNode);
        if (node) showInspector("node", node, document.getElementById("graph-case-view"));
      });
    });
    bindViewSearch("timeline", () => renderTimeline(payload));
  }

  function timelineCard(node) {
    return `<button type="button" class="timeline-claim" data-timeline-node="${esc(node.node_key)}">
      <span>${badge(node.clinical_category)} ${node.is_quarantined ? badge("Held", "holdout") : ""}</span>
      <strong>${esc(node.headline)}</strong>
      <small>${esc(node.explanation)}</small>
    </button>`;
  }

  function renderReviewTable(payload) {
    const rows = array(payload.items);
    document.getElementById("graph-case-view").innerHTML = `
      ${viewSearch("review", "Search clinical claims across the review queue…")}
      <div class="insights-table-wrap">
        <table class="insights-table graph-review-table">
          <thead><tr><th>Clinical item</th><th>Category</th><th>Review state</th><th>Date</th><th>Evidence</th></tr></thead>
          <tbody>${rows.map((node) => `<tr data-review-node="${esc(node.node_key)}" tabindex="0" role="button">
            <td><strong>${esc(node.headline)}</strong><br><small>${esc(node.explanation)}</small></td>
            <td>${esc(words(node.clinical_category))}</td>
            <td>${badge(node.review_state)}${node.is_quarantined ? badge("Held", "holdout") : ""}</td>
            <td>${esc(node.recorded_at || "Not dated")}</td>
            <td>${Number(node.evidence_count || 0)}</td>
          </tr>`).join("")}</tbody>
        </table>
      </div>
      <p class="muted">Showing ${rows.length.toLocaleString()} of ${Number(payload.total || rows.length).toLocaleString()} items.</p>`;
    document.querySelectorAll("[data-review-node]").forEach((row) => {
      const open = () => {
        const node = rows.find((item) => item.node_key === row.dataset.reviewNode);
        if (node) showInspector("node", node, document.getElementById("graph-case-view"));
      };
      row.addEventListener("click", open);
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") open();
      });
    });
    const input = document.getElementById("graph-review-search");
    const clear = document.getElementById("graph-review-clear");
    input.addEventListener("input", debounce(() => {
      state.viewSearch.review = input.value;
      state.viewCache.delete(`${state.selectedCase.id}:review`);
      loadAuxiliaryView("review", true);
    }));
    clear.addEventListener("click", () => {
      state.viewSearch.review = "";
      state.viewCache.delete(`${state.selectedCase.id}:review`);
      loadAuxiliaryView("review", true);
    });
  }

  function renderNetwork() {
    const payload = state.relationships;
    const query = state.viewSearch.network.trim().toLowerCase();
    const nodes = array(payload.nodes).filter((node) => nodeMatches(node, query));
    const keys = new Set(nodes.map((node) => node.node_key));
    const edges = array(payload.edges).filter((edge) => keys.has(edge.source_node_key) && keys.has(edge.target_node_key));
    document.getElementById("graph-case-view").innerHTML = `
      ${viewSearch("network", "Search every displayed clinical claim…")}
      <div class="network-and-inspector">
        <div id="case-network-host"></div>
        <aside id="graph-inspector" class="graph-inspector">
          <div class="graph-inspector-empty">Select a clinical claim or relationship to inspect its evidence and review state.</div>
        </aside>
      </div>`;
    new ClinicalNetwork(document.getElementById("case-network-host"), {
      nodes,
      edges,
      patient: payload.graph,
      projectionSource: payload.projection_source,
      onSelect: (kind, item) => showInspector(kind, item, document.getElementById("graph-inspector"))
    });
    bindViewSearch("network", renderNetwork);
  }

  async function showInspector(kind, item, host) {
    let inspector = host;
    if (!host.classList.contains("graph-inspector")) {
      let dialog = document.getElementById("graph-item-dialog");
      if (!dialog) {
        dialog = document.createElement("dialog");
        dialog.id = "graph-item-dialog";
        dialog.className = "insights-dialog graph-item-dialog";
        dialog.innerHTML = `<form method="dialog" class="dialog-heading"><h2>Clinical graph detail</h2><button class="secondary">Close</button></form><div class="graph-inspector"></div>`;
        document.body.append(dialog);
      }
      inspector = dialog.querySelector(".graph-inspector");
      dialog.showModal();
    }
    const isNode = kind === "node";
    const nodes = array(state.relationships?.nodes);
    const source = isNode ? null : nodes.find((node) => node.node_key === item.source_node_key);
    const target = isNode ? null : nodes.find((node) => node.node_key === item.target_node_key);
    const review = item.review || {};
    inspector.innerHTML = `
      <div class="inspector-heading">
        <span class="graph-eyebrow">${isNode ? esc(words(item.clinical_category)) : "Clinical relationship"}</span>
        <h3>${esc(isNode ? item.headline : words(item.relationship_label))}</h3>
        <div>${badge(item.review_state)} ${item.is_quarantined ? badge("Held / quarantined", "holdout") : ""}
          ${item.lifecycle_status === "retired" ? badge("Retired", "needs_changes") : ""}</div>
      </div>
      ${isNode ? `
        <p class="inspector-explanation">${esc(item.explanation)}</p>
        ${item.status_note ? `<p class="inspector-note">${esc(item.status_note)}</p>` : ""}
        <dl>
          <dt>Pathway stage</dt><dd>${esc(item.stage_label || words(item.pathway_stage))}</dd>
          <dt>Date</dt><dd>${esc(item.recorded_at || "Not dated in the record")}</dd>
          <dt>Documentation</dt><dd>${esc(words(item.epistemic_kind_label || item.epistemic_kind || item.epistemic_status))}</dd>
          <dt>Certainty</dt><dd>${esc(words(item.certainty || item.confidence || "Not reported"))}</dd>
          <dt>Evidence</dt><dd>${Number(item.evidence_count || 0)} reference(s)</dd>
          <dt>Source VB</dt><dd>${esc(state.relationships?.graph?.source_vb?.display_name || "Not reported")}</dd>
        </dl>
        <section class="inspector-evidence"><h4>Evidence references</h4>
          ${array(item.evidence).map((evidence) => `<article><strong>${esc(evidence.source_description || "Source evidence")}</strong><small>${esc(words(evidence.provenance_kind))}</small></article>`).join("") || `<p class="muted">No evidence references supplied.</p>`}
        </section>` : `
        <div class="edge-statement">
          <strong>${esc(source?.headline || "Source claim unavailable")}</strong>
          <span>→ ${esc(words(item.relationship_label))} →</span>
          <strong>${esc(target?.headline || "Target claim unavailable")}</strong>
        </div>
        ${item.is_temporal_only ? `<p class="inspector-note">Temporal sequence only. This is not evidence that one event caused the other.</p>` : ""}
        <dl>
          <dt>Relationship</dt><dd>${esc(words(item.relationship_label))}</dd>
          <dt>Temporal/inferred</dt><dd>${item.is_temporal_only ? "Temporal sequence only" : "Not marked temporal-only by the API"}</dd>
          <dt>Review state</dt><dd>${esc(words(item.review_state))}</dd>
          <dt>Source VB</dt><dd>${esc(state.relationships?.graph?.source_vb?.display_name || "Not reported")}</dd>
        </dl>`}
      <section class="graph-review-actions">
        <h4>Clinician review</h4>
        <label>Reason for this decision
          <textarea data-review-rationale rows="2" placeholder="Optional clinical rationale"></textarea>
        </label>
        <div class="review-action-buttons">
          <button type="button" data-review-action="confirm">Confirm</button>
          <button type="button" class="secondary" data-review-action="mark_uncertain">Mark uncertain</button>
          <button type="button" class="secondary" data-review-action="request_evidence">Request evidence</button>
          <button type="button" class="secondary" data-review-action="${item.lifecycle_status === "retired" ? "restore" : "retire"}">${item.lifecycle_status === "retired" ? "Restore" : "Retire"}</button>
        </div>
        <label>Annotation
          <textarea data-review-annotation rows="2" placeholder="Add a clinical note without changing the claim"></textarea>
        </label>
        <button type="button" class="secondary" data-review-action="annotate">Save annotation</button>
        <label>Corrected wording
          <input data-review-correction value="" placeholder="${isNode ? "Replacement clinical headline" : "Replacement relationship label"}">
        </label>
        <button type="button" class="secondary" data-review-action="correct">Save correction</button>
        <p class="graph-review-status" aria-live="polite"></p>
      </section>
      <section class="graph-history"><h4>Review history</h4><p class="muted">Loading history…</p></section>
      <details class="advanced-details"><summary>Advanced details</summary>
        <dl>
          <dt>${isNode ? "Node key" : "Edge key"}</dt><dd><code>${esc(isNode ? item.node_key : item.edge_key)}</code></dd>
          <dt>Origin</dt><dd>${esc(item.origin || "Not reported")}</dd>
          <dt>Provenance</dt><dd><pre>${esc(JSON.stringify(item.advanced_provenance || {}, null, 2))}</pre></dd>
        </dl>
      </details>`;
    inspector.querySelectorAll("[data-review-action]").forEach((button) => {
      button.addEventListener("click", () => submitReview(kind, item, button.dataset.reviewAction, inspector, review.payload_hash));
    });
    loadHistory(kind, item, inspector);
  }

  async function loadHistory(kind, item, inspector) {
    const key = kind === "node" ? item.node_key : item.edge_key;
    try {
      const payload = await requestJSON(queryPath(
        `/clinical-insights/api/graph/cases/${encodeURIComponent(state.selectedCase.id)}/history`,
        {target_kind: kind, target_key: key}
      ));
      const history = inspector.querySelector(".graph-history");
      history.innerHTML = `<h4>Review history</h4>${array(payload.items).length ? `<ol>${array(payload.items).map((entry) =>
        `<li><strong>${esc(words(String(entry.event_type).split(".").pop()))}</strong> by ${esc(entry.actor?.display_name || "Reviewer")}
          ${entry.recorded_at ? ` · ${esc(entry.recorded_at.slice(0, 10))}` : ""}
          ${entry.rationale ? `<br><small>${esc(entry.rationale)}</small>` : ""}</li>`
      ).join("")}</ol>` : `<p class="muted">No prior review decisions.</p>`}`;
    } catch (error) {
      inspector.querySelector(".graph-history").innerHTML = `<h4>Review history</h4><p class="insights-error">${esc(error.message)}</p>`;
    }
  }

  async function submitReview(kind, item, action, inspector, expectedHash) {
    const status = inspector.querySelector(".graph-review-status");
    const annotation = inspector.querySelector("[data-review-annotation]").value.trim();
    const correction = inspector.querySelector("[data-review-correction]").value.trim();
    if (action === "annotate" && !annotation) {
      status.textContent = "Enter an annotation first.";
      return;
    }
    if (action === "correct" && !correction) {
      status.textContent = "Enter corrected wording first.";
      return;
    }
    const payload = {
      target_kind: kind,
      target_key: kind === "node" ? item.node_key : item.edge_key,
      action,
      rationale: inspector.querySelector("[data-review-rationale]").value.trim(),
      expected_payload_hash: expectedHash || null
    };
    if (action === "annotate") payload.annotation = annotation;
    if (action === "correct") {
      payload.replacement = kind === "node" ? {headline: correction} : {relationship_label: correction};
    }
    status.textContent = "Saving review decision…";
    inspector.querySelectorAll("[data-review-action]").forEach((button) => { button.disabled = true; });
    try {
      await requestJSON(
        `/clinical-insights/api/graph/cases/${encodeURIComponent(state.selectedCase.id)}/review`,
        {method: "POST", body: payload}
      );
      status.textContent = "Review decision saved.";
      state.relationships = await requestJSON(queryPath(
        `/clinical-insights/api/graph/cases/${encodeURIComponent(state.selectedCase.id)}/relationships`,
        {include_quarantined: document.getElementById("graph-held-filter").checked}
      ));
      state.viewCache.clear();
      renderCurrentView();
    } catch (error) {
      status.textContent = error.message;
      inspector.querySelectorAll("[data-review-action]").forEach((button) => { button.disabled = false; });
    }
  }

  function activateView(view) {
    state.activeView = view;
    state.requestedView = view;
    document.querySelectorAll("[data-graph-view]").forEach((button) => {
      const active = button.dataset.graphView === view;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    renderCurrentView();
  }

  async function loadCorpusGraph() {
    setState("corpus-graph-state", "Loading corpus graph…");
    try {
      const payload = await requestJSON(queryPath("/clinical-insights/api/graph/global-graph", {
        max_nodes: document.getElementById("corpus-node-limit").value,
        include_machine_only: document.getElementById("corpus-machine-only").checked,
        include_quarantined: document.getElementById("corpus-held").checked
      }));
      const host = document.getElementById("corpus-graph-view");
      if (!array(payload.nodes).length) {
        host.innerHTML = "";
        setState(
          "corpus-graph-state",
          array(payload.notes)[0] || "No corpus graph is available for these review filters."
        );
        return;
      }
      setState(
        "corpus-graph-state",
        `${Number(payload.contributing_case_count || 0).toLocaleString()} contributing cases · ${Number(payload.shown_concept_count || payload.nodes.length).toLocaleString()} of ${Number(payload.total_concept_count || payload.nodes.length).toLocaleString()} concepts shown.`
      );
      host.innerHTML = `<div class="network-and-inspector"><div id="corpus-network-host"></div><aside id="corpus-inspector" class="graph-inspector"><div class="graph-inspector-empty">Select a corpus concept or relationship for descriptive details.</div></aside></div>`;
      new ClinicalNetwork(document.getElementById("corpus-network-host"), {
        nodes: payload.nodes,
        edges: payload.edges,
        projectionSource: payload.projection_source,
        corpus: true,
        onSelect: (kind, item) => renderCorpusInspector(kind, item, payload)
      });
    } catch (error) {
      setState("corpus-graph-state", error.message, true);
    }
  }

  function renderCorpusInspector(kind, item, payload) {
    const host = document.getElementById("corpus-inspector");
    if (kind === "node") {
      host.innerHTML = `<div class="inspector-heading"><span class="graph-eyebrow">${esc(words(item.clinical_category))}</span><h3>${esc(item.headline)}</h3></div>
        <p class="inspector-explanation">${esc(item.explanation)}</p>
        <dl><dt>Stage</dt><dd>${esc(item.stage_label || words(item.pathway_stage))}</dd>
          <dt>Cases</dt><dd>${Number(item.case_count || 0).toLocaleString()}</dd>
          <dt>Documented</dt><dd>${Number(item.documented_count || 0).toLocaleString()}</dd>
          <dt>Inferred</dt><dd>${Number(item.inferred_count || 0).toLocaleString()}</dd></dl>
        <p class="inspector-note">Corpus counts are descriptive and do not establish prevalence or causality.</p>`;
      return;
    }
    const source = array(payload.nodes).find((node) => node.concept_key === item.source_concept_key);
    const target = array(payload.nodes).find((node) => node.concept_key === item.target_concept_key);
    host.innerHTML = `<div class="inspector-heading"><span class="graph-eyebrow">Corpus relationship</span><h3>${esc(words(item.relationship_label))}</h3></div>
      <div class="edge-statement"><strong>${esc(source?.headline || "Source concept")}</strong><span>→ ${esc(words(item.relationship_label))} →</span><strong>${esc(target?.headline || "Target concept")}</strong></div>
      <dl><dt>Cases</dt><dd>${Number(item.case_count || 0).toLocaleString()}</dd><dt>Temporal only</dt><dd>${item.is_temporal_only ? "Yes — sequence only" : "No"}</dd></dl>
      ${item.is_temporal_only ? `<p class="inspector-note">Sequence does not prove causation.</p>` : ""}`;
  }

  function switchMode(mode) {
    state.activeMode = mode;
    document.querySelectorAll("[data-graph-mode]").forEach((button) => {
      const active = button.dataset.graphMode === mode;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll("[data-graph-mode-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.graphModePanel !== mode;
    });
    if (mode === "corpus") loadCorpusGraph();
  }

  function bind() {
    const reloadCases = () => {
      state.offset = 0;
      state.selectedCase = null;
      loadCases();
    };
    document.getElementById("graph-case-search").addEventListener("input", debounce(reloadCases));
    ["graph-vb-filter", "graph-run-filter", "graph-review-filter", "graph-held-filter"].forEach((id) => {
      document.getElementById(id).addEventListener("change", reloadCases);
    });
    document.getElementById("graph-page-size").addEventListener("change", (event) => {
      state.limit = Number(event.target.value);
      reloadCases();
    });
    document.getElementById("graph-clear-search").addEventListener("click", () => {
      document.getElementById("graph-case-search").value = "";
      reloadCases();
    });
    document.getElementById("graph-case-prev").addEventListener("click", () => {
      state.offset = Math.max(0, state.offset - state.limit);
      loadCases();
    });
    document.getElementById("graph-case-next").addEventListener("click", () => {
      state.offset += state.limit;
      loadCases();
    });
    document.querySelectorAll("[data-graph-view]").forEach((button) => {
      button.addEventListener("click", () => activateView(button.dataset.graphView));
    });
    document.querySelectorAll("[data-graph-mode]").forEach((button) => {
      button.addEventListener("click", () => switchMode(button.dataset.graphMode));
    });
    document.getElementById("corpus-refresh").addEventListener("click", loadCorpusGraph);
    document.getElementById("corpus-node-limit").addEventListener("change", loadCorpusGraph);
    document.getElementById("corpus-machine-only").addEventListener("change", loadCorpusGraph);
    document.getElementById("corpus-held").addEventListener("change", loadCorpusGraph);
  }

  bind();
  window.CollabVetClinicalGraph = {activate: bootstrap};
  if (document.getElementById("clinical-graph-root")) bootstrap();
})();
