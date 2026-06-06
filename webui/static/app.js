import './sidebar-resize.js';
/* ToolForge Flow Studio — vanilla module, no bundler. */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const state = {
  inventory: { items: [], counts: {} },
  filter: { q: "", types: new Set(), categories: new Set() },
  editor: null,
  selectedNodeId: null,
  nodeMeta: new Map(),
  savedFlows: [],
  activeTab: "node",       // "node" | "preview"
  preview: { markdown: "", cycle: null, dirty: false },
  previewTimer: null,
};

const PREVIEW_DEBOUNCE_MS = 250;

let csrfToken = null;

const TYPE_ORDER = ["skill", "command", "mcp", "plugin", "agent", "repo"];

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function setSafeHTML(el, html) {
  // Centralized DOM write. Callers pass strings whose interpolations are
  // escapeHtml()-escaped; only static authored markup is unescaped.
  const frag = document.createRange().createContextualFragment(html);
  el.replaceChildren(frag);
}

function toast(title, body = "", kind = "info", ttl = 3200) {
  const stack = $("#toast-stack");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  const t = document.createElement("div"); t.className = "t-title"; t.textContent = title;
  el.appendChild(t);
  if (body) {
    const b = document.createElement("div"); b.className = "t-body"; b.textContent = body;
    el.appendChild(b);
  }
  stack.appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity 200ms";
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 220);
  }, ttl);
}

// FIX_CONVENTIONS §9 — adapter over toast() so F04/F05 callers can use the
// (msg, level) shape from the playbook without forking a parallel system.
function showToast(msg, level = "info") {
  const title = level === "error" ? "Error" : level === "warn" ? "Warning" : "Notice";
  toast(title, msg, level, level === "error" || level === "warn" ? 6000 : 3200);
}

async function api(path, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (method !== "GET" && method !== "HEAD" && csrfToken) {
    headers["X-CSRF-Token"] = csrfToken;
  }
  const r = await fetch(path, {
    ...opts,
    headers,
    credentials: "same-origin",
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) {
    const txt = await r.text().catch(() => r.statusText);
    throw new Error(`${r.status} ${r.statusText}: ${txt.slice(0, 200)}`);
  }
  return r.json();
}

async function loadCsrfToken() {
  const r = await fetch("/api/csrf", { credentials: "same-origin" });
  if (!r.ok) throw new Error(`csrf bootstrap failed: ${r.status}`);
  const j = await r.json();
  csrfToken = j.token;
}

async function loadInventory() {
  try {
    const data = await api("/api/inventory");
    state.inventory = data;
    renderBarStats();
    renderFilters();
    renderInventory();
    toast("Inventory loaded", `${data.counts.total} tools indexed`, "success");
  } catch (e) {
    toast("Inventory failed", e.message, "error", 6000);
  }
}

function renderBarStats() {
  const c = state.inventory.counts || {};
  const by = c.by_type || {};
  const parts = [`<span class="pill">${c.total || 0} total</span>`];
  for (const t of TYPE_ORDER) {
    if (by[t]) parts.push(`<span class="pill">${escapeHtml(t)} · ${by[t]}</span>`);
  }
  setSafeHTML($("#bar-stats"), parts.join(""));
}

function renderFilters() {
  const by_type = state.inventory.counts.by_type || {};
  setSafeHTML($("#type-chips"),
    TYPE_ORDER.filter(t => by_type[t])
      .map(t => `<button class="chip" data-type="${escapeHtml(t)}">${escapeHtml(t)} (${by_type[t]})</button>`)
      .join("")
  );
  const cats = Object.entries(state.inventory.counts.by_category || {})
    .sort((a, b) => b[1] - a[1]).slice(0, 24);
  setSafeHTML($("#cat-chips"),
    cats.map(([c, n]) => `<button class="chip muted" data-cat="${escapeHtml(c)}">${escapeHtml(c)} (${n})</button>`).join("")
  );

  $$("#type-chips .chip").forEach(b => b.addEventListener("click", () => {
    const t = b.dataset.type;
    state.filter.types.has(t) ? state.filter.types.delete(t) : state.filter.types.add(t);
    b.classList.toggle("active");
    renderInventory();
  }));
  $$("#cat-chips .chip").forEach(b => b.addEventListener("click", () => {
    const c = b.dataset.cat;
    state.filter.categories.has(c) ? state.filter.categories.delete(c) : state.filter.categories.add(c);
    b.classList.toggle("active");
    renderInventory();
  }));
}

function matchesFilter(item) {
  const f = state.filter;
  if (f.types.size && !f.types.has(item.type)) return false;
  if (f.categories.size && !item.categories.some(c => f.categories.has(c))) return false;
  if (f.q) {
    const q = f.q.toLowerCase();
    const hay = `${item.name} ${item.description} ${item.categories.join(" ")} ${item.source}`.toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}

function renderInventory() {
  const list = $("#inventory-list");
  const items = state.inventory.items.filter(matchesFilter);
  if (!items.length) {
    setSafeHTML(list, `<div class="empty-state"><div class="empty-glyph">∅</div><p>No tools match.</p></div>`);
    return;
  }
  setSafeHTML(list, items.map(it => {
    const r = it.rating || {};
    const score = (r.score != null)
      ? `<span class="score">${r.score.toFixed(2)}</span><span class="muted small"> · ${r.n}</span>`
      : `<span class="muted small">unrated</span>`;
    const cats = (it.categories || []).slice(0, 3).map(c => `<span class="tc-cat">${escapeHtml(c)}</span>`).join("");
    return `<div class="tool-card" draggable="true" data-id="${escapeHtml(it.id)}">
      <div class="tc-top">
        <span class="tc-type ${escapeHtml(it.type)}">${escapeHtml(it.type)}</span>
        <span class="tc-name">${escapeHtml(it.name)}</span>
        <span class="tc-rating">${score}</span>
      </div>
      <div class="tc-desc">${escapeHtml(it.description || "(no description)")}</div>
      <div class="tc-cats">${cats}<span class="tc-cat" title="source">${escapeHtml(it.source || "")}</span></div>
    </div>`;
  }).join(""));

  $$(".tool-card", list).forEach(card => {
    card.addEventListener("dragstart", e => {
      const id = card.dataset.id;
      const tool = state.inventory.items.find(x => x.id === id);
      if (!tool) return;
      e.dataTransfer.effectAllowed = "copy";
      e.dataTransfer.setData("application/x-toolforge-tool", JSON.stringify(tool));
      e.dataTransfer.setData("text/plain", tool.name);
    });
  });
}

function initEditor() {
  const host = $("#drawflow");
  const editor = new window.Drawflow(host);
  editor.reroute = true;
  editor.curvature = 0.5;
  editor.reroute_fix_curvature = true;
  editor.editor_mode = "edit";
  editor.start();
  state.editor = editor;

  host.addEventListener("dragover", e => {
    if (e.dataTransfer.types.includes("application/x-toolforge-tool")) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
    }
  });
  host.addEventListener("drop", e => {
    e.preventDefault();
    const raw = e.dataTransfer.getData("application/x-toolforge-tool");
    if (!raw) return;
    // FIXED in F34 (see SKETCHY_CODE_AUDIT.md#s4-8) — surface malformed drop payload via toast.
    let tool;
    try {
      tool = JSON.parse(raw);
    } catch (err) {
      showToast("Drop payload invalid JSON: " + err.message, "warn");
      return;
    }
    const rect = host.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    addNode(tool, x, y);
  });

  editor.on("nodeSelected", id => selectNode(id));
  editor.on("nodeUnselected", () => clearInspector());
  editor.on("nodeRemoved", id => {
    state.nodeMeta.delete(String(id));
    if (String(state.selectedNodeId) === String(id)) clearInspector();
    schedulePreviewRefresh();
  });
  editor.on("nodeCreated", () => schedulePreviewRefresh());
  // Drawflow v0.0.59 fires connectionCreated/connectionRemoved with
  // {output_id, input_id, output_class, input_class}. Either way we just
  // need a redraw trigger — refresh path highlight + preview.
  editor.on("connectionCreated", () => {
    refreshPathHighlight();
    schedulePreviewRefresh();
  });
  editor.on("connectionRemoved", () => {
    refreshPathHighlight();
    schedulePreviewRefresh();
  });

  $("#zoom-in").addEventListener("click", () => editor.zoom_in());
  $("#zoom-out").addEventListener("click", () => editor.zoom_out());
  $("#zoom-reset").addEventListener("click", () => editor.zoom_reset());
}

/* ── path tracing + preview ───────────────────────────────────
 * Walks the current drawflow graph to compute upstream / downstream
 * sets relative to the selected node, then toggles CSS classes on
 * the drawflow node DIVs and the connection SVGs. CSS does the paint
 * — JS stays simple. The same adjacency feeds the live preview pane
 * via /api/preview, which round-trips through render_skill_md so the
 * markdown matches what Export will write.
 */

function buildAdjacency() {
  // Returns { fwd: Map<id,Set<id>>, rev: Map<id,Set<id>>, nodeIds: Set<id>, edges: Array<[s,t]> }
  const editor = state.editor;
  if (!editor) return { fwd: new Map(), rev: new Map(), nodeIds: new Set(), edges: [] };
  const data = editor.export().drawflow.Home.data || {};
  const fwd = new Map();
  const rev = new Map();
  const nodeIds = new Set();
  const edges = [];
  for (const id of Object.keys(data)) {
    const k = String(id);
    nodeIds.add(k);
    fwd.set(k, new Set());
    rev.set(k, new Set());
  }
  for (const [id, n] of Object.entries(data)) {
    const sid = String(id);
    for (const out of Object.values(n.outputs || {})) {
      for (const conn of (out.connections || [])) {
        const tid = String(conn.node);
        if (!nodeIds.has(tid)) continue;
        fwd.get(sid).add(tid);
        rev.get(tid).add(sid);
        edges.push([sid, tid]);
      }
    }
  }
  return { fwd, rev, nodeIds, edges };
}

function bfsReach(start, adj) {
  const reached = new Set();
  const q = [start];
  while (q.length) {
    const x = q.shift();
    for (const nxt of (adj.get(x) || [])) {
      if (!reached.has(nxt)) { reached.add(nxt); q.push(nxt); }
    }
  }
  return reached;
}

function clearPathClasses() {
  document.querySelectorAll(".drawflow .drawflow-node")
    .forEach(el => el.classList.remove("path-current", "path-up", "path-down", "path-dim", "cycle"));
  document.querySelectorAll(".drawflow .connection")
    .forEach(el => el.classList.remove("edge-up", "edge-down", "edge-dim", "edge-cycle"));
}

function refreshPathHighlight() {
  clearPathClasses();
  applyCycleHighlight();  // cycle decoration persists across selection changes
  const id = state.selectedNodeId;
  if (id == null) return;
  const sid = String(id);
  const { fwd, rev, nodeIds } = buildAdjacency();
  if (!nodeIds.has(sid)) return;

  const up = bfsReach(sid, rev);
  const down = bfsReach(sid, fwd);
  const lit = new Set([sid, ...up, ...down]);

  for (const nid of nodeIds) {
    const el = document.getElementById(`node-${nid}`);
    if (!el) continue;
    if (nid === sid) el.classList.add("path-current");
    else if (up.has(nid)) el.classList.add("path-up");
    else if (down.has(nid)) el.classList.add("path-down");
    else el.classList.add("path-dim");
  }

  // Drawflow tags connection <svg>s with `node_in_node-T node_out_node-S`.
  document.querySelectorAll(".drawflow .connection").forEach(svg => {
    const cls = svg.className.baseVal || svg.getAttribute("class") || "";
    const inMatch = cls.match(/node_in_node-(\S+)/);
    const outMatch = cls.match(/node_out_node-(\S+)/);
    if (!inMatch || !outMatch) return;
    const t = inMatch[1], s = outMatch[1];
    if (!lit.has(s) || !lit.has(t)) {
      svg.classList.add("edge-dim");
      return;
    }
    // Direction relative to selected: edges going *into* selected (or its ancestors) = up,
    // edges going *out of* selected (or its descendants) = down.
    if (down.has(t) || (s === sid && down.has(t))) svg.classList.add("edge-down");
    else if (up.has(s) || (t === sid && up.has(s))) svg.classList.add("edge-up");
  });
}

function applyCycleHighlight() {
  const cycle = state.preview.cycle;
  if (!cycle || cycle.length < 2) return;
  const ring = new Set(cycle.map(String));
  for (const nid of ring) {
    const el = document.getElementById(`node-${nid}`);
    if (el) el.classList.add("cycle");
  }
  // Edges where both endpoints sit on the ring and the edge follows ring order.
  // Cheap heuristic: any edge with both endpoints in ring is suspect — mark it.
  document.querySelectorAll(".drawflow .connection").forEach(svg => {
    const cls = svg.className.baseVal || svg.getAttribute("class") || "";
    const inMatch = cls.match(/node_in_node-(\S+)/);
    const outMatch = cls.match(/node_out_node-(\S+)/);
    if (!inMatch || !outMatch) return;
    if (ring.has(inMatch[1]) && ring.has(outMatch[1])) {
      svg.classList.add("edge-cycle");
    }
  });
}

function schedulePreviewRefresh() {
  state.preview.dirty = true;
  if (state.previewTimer) clearTimeout(state.previewTimer);
  state.previewTimer = setTimeout(refreshPreview, PREVIEW_DEBOUNCE_MS);
}

async function refreshPreview() {
  state.previewTimer = null;
  if (!state.editor) return;
  const flow = collectFlow();
  const statusEl = $("#preview-status");
  const warnEl = $("#preview-warn");
  const bodyEl = $("#preview-body");
  if (!flow.nodes.length) {
    state.preview = { markdown: "", cycle: null, dirty: false };
    if (statusEl) statusEl.textContent = "";
    if (warnEl) { warnEl.classList.add("hidden"); warnEl.textContent = ""; }
    if (bodyEl) { bodyEl.textContent = "Drop a node to start."; bodyEl.className = "muted small"; }
    clearPathClasses();
    if (state.selectedNodeId != null) refreshPathHighlight();
    return;
  }
  try {
    const r = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken || "" },
      credentials: "same-origin",
      body: JSON.stringify(flow),
    });
    // FIXED in F33 (was silent .catch(() => ({})) — see SKETCHY_CODE_AUDIT.md#s4-7)
    let j = {};
    try {
      j = await r.json();
    } catch {
      const body = await r.text().catch(() => "");
      showToast(`Preview error: ${r.status} ${r.statusText} — ${body.slice(0, 200)}`, "error");
      return;
    }
    if (r.status === 409 && j.error === "cycle") {
      state.preview = { markdown: "", cycle: j.cycle || [], dirty: false };
      if (statusEl) statusEl.textContent = "cycle";
      if (warnEl) {
        warnEl.classList.remove("hidden");
        warnEl.textContent = `Cycle detected: ${(j.cycle || []).join(" → ")}. Export blocked — break the cycle to continue.`;
      }
      if (bodyEl) { bodyEl.textContent = "(no preview while graph has a cycle)"; bodyEl.className = "muted small"; }
    } else if (r.ok && j.ok) {
      state.preview = { markdown: j.markdown, cycle: null, dirty: false };
      if (statusEl) statusEl.textContent = `${j.steps} step${j.steps === 1 ? "" : "s"}`;
      if (warnEl) { warnEl.classList.add("hidden"); warnEl.textContent = ""; }
      if (bodyEl) { bodyEl.textContent = j.markdown; bodyEl.className = ""; }
    } else {
      state.preview = { markdown: "", cycle: null, dirty: false };
      if (statusEl) statusEl.textContent = "error";
      if (bodyEl) { bodyEl.textContent = `Preview error: ${j.message || r.statusText}`; bodyEl.className = "muted small"; }
    }
  } catch (e) {
    if (bodyEl) { bodyEl.textContent = `Preview error: ${e.message}`; bodyEl.className = "muted small"; }
  }
  refreshPathHighlight();
}

function switchTab(tab) {
  state.activeTab = tab;
  const isNode = tab === "node";
  $("#tab-node").classList.toggle("active", isNode);
  $("#tab-node").setAttribute("aria-selected", isNode ? "true" : "false");
  $("#tab-preview").classList.toggle("active", !isNode);
  $("#tab-preview").setAttribute("aria-selected", !isNode ? "true" : "false");
  $("#panel-node").classList.toggle("hidden", !isNode);
  $("#panel-preview").classList.toggle("hidden", isNode);
  if (!isNode && state.preview.dirty) refreshPreview();
}

function nodeHtml(tool, annotation = "") {
  return `<div class="node-inner">
    <span class="node-type">${escapeHtml(tool.type)}</span>
    <span class="node-name">${escapeHtml(tool.name)}</span>
    <span class="node-anno" data-role="anno">${escapeHtml(annotation || "click to add prompt…")}</span>
  </div>`;
}

function addNode(tool, x, y) {
  const editor = state.editor;
  const data = { tool, annotation: "" };
  const id = editor.addNode(
    tool.name.slice(0, 64),
    1, 1,
    Math.max(20, x - 40), Math.max(20, y - 30),
    `t-${tool.type}`,
    data,
    nodeHtml(tool, ""),
  );
  state.nodeMeta.set(String(id), data);
  selectNode(id);
}

function selectNode(id) {
  state.selectedNodeId = id;
  const meta = state.nodeMeta.get(String(id)) || state.editor.getNodeFromId(id)?.data;
  if (!meta || !meta.tool) return;
  state.nodeMeta.set(String(id), meta);
  renderInspector(id, meta);
  if (state.activeTab !== "node") switchTab("node");
  refreshPathHighlight();
}

function clearInspector() {
  state.selectedNodeId = null;
  $("#inspector-sub").textContent = "Click a node";
  setSafeHTML($("#inspector-body"),
    `<div class="empty-state"><div class="empty-glyph">◌</div><p>Select a node to set the prompt / annotation for that step.</p><p class="muted small">Tip: click a node to highlight its upstream and downstream paths.</p></div>`
  );
  clearPathClasses();
  applyCycleHighlight();
}

function renderInspector(id, meta) {
  const t = meta.tool;
  $("#inspector-sub").textContent = `node #${id} · ${t.type}`;
  setSafeHTML($("#inspector-body"), `
    <div>
      <label>Name</label>
      <div class="field">${escapeHtml(t.name)}</div>
    </div>
    <div>
      <label>Invoke</label>
      <div class="field code">${escapeHtml(t.invoke || t.name)}</div>
    </div>
    <div>
      <label>Source</label>
      <div class="field code">${escapeHtml(t.source || "")} · ${escapeHtml(t.path || "")}</div>
    </div>
    <div>
      <label>Categories</label>
      <div class="field">${(t.categories || []).map(c => `<span class="tc-cat">${escapeHtml(c)}</span>`).join(" ") || "—"}</div>
    </div>
    <div>
      <label>Annotation / prompt for this step</label>
      <textarea class="field" id="anno" placeholder="What should Claude do at this step?"></textarea>
    </div>
    <div class="row">
      <button class="danger" id="del-node">✕ Remove node</button>
      <button class="btn" id="reveal-node">Open source ↗</button>
    </div>
  `);
  const annoEl = $("#anno");
  annoEl.value = meta.annotation || "";
  annoEl.addEventListener("input", e => {
    meta.annotation = e.target.value;
    const node = state.editor.getNodeFromId(id);
    if (node) {
      node.data = meta;
      const el = document.getElementById(`node-${id}`);
      if (el) {
        const anno = el.querySelector('[data-role="anno"]');
        if (anno) anno.textContent = meta.annotation || "click to add prompt…";
      }
    }
    schedulePreviewRefresh();
  });
  $("#del-node").addEventListener("click", () => state.editor.removeNodeId(`node-${id}`));
  $("#reveal-node").addEventListener("click", async () => {
    try {
      await api("/api/open", { method: "POST", body: { path: t.path } });
      toast("Opened", t.path, "success");
    } catch (e) { toast("Open failed", e.message, "error"); }
  });
}

function collectFlow() {
  const dfData = state.editor.export();
  const home = dfData.drawflow.Home.data || {};
  const nodes = [];
  for (const [id, n] of Object.entries(home)) {
    const meta = state.nodeMeta.get(String(id)) || n.data || {};
    nodes.push({
      id: String(id),
      tool: meta.tool || n.data?.tool || { name: n.name, type: "skill" },
      annotation: meta.annotation || n.data?.annotation || "",
      pos_x: n.pos_x, pos_y: n.pos_y,
    });
  }
  const edges = [];
  for (const [id, n] of Object.entries(home)) {
    const outs = n.outputs || {};
    for (const out of Object.values(outs)) {
      for (const conn of (out.connections || [])) {
        edges.push({ source: String(id), target: String(conn.node) });
      }
    }
  }
  return {
    name: $("#flow-name").value.trim() || "Untitled flow",
    trigger: $("#flow-trigger").value.trim() || "untitled-flow",
    description: $("#flow-desc").value.trim() || "",
    nodes, edges,
    saved_at: new Date().toISOString(),
  };
}

async function saveFlow() {
  if (!state.nodeMeta.size) { toast("Empty flow", "Drop at least one tool first", "error"); return; }
  try {
    const flow = collectFlow();
    const r = await api("/api/flows", { method: "POST", body: flow });
    toast("Flow saved", `trigger: /${r.trigger}`, "success");
    await loadSavedFlows();
  } catch (e) { toast("Save failed", e.message, "error", 6000); }
}

async function exportFlow() {
  if (!state.nodeMeta.size) { toast("Empty flow", "Drop at least one tool first", "error"); return; }
  if (state.preview.cycle && state.preview.cycle.length) {
    toast("Cycle blocks export",
          `Break the cycle: ${state.preview.cycle.join(" → ")}`,
          "error", 6000);
    switchTab("preview");
    return;
  }
  try {
    await saveFlow();
    const flow = collectFlow();
    // /api/export inherits the same FlowCycleError → 409 path. Treat 409 specially
    // so the message names the ring instead of dumping a raw 409 status.
    const r = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken || "" },
      credentials: "same-origin",
      body: JSON.stringify(flow),
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 409 && j.error === "cycle") {
      state.preview.cycle = j.cycle || [];
      applyCycleHighlight();
      toast("Cycle blocks export",
            `Break the cycle: ${(j.cycle || []).join(" → ")}`,
            "error", 6000);
      switchTab("preview");
      return;
    }
    if (!r.ok || !j.ok) throw new Error(j.message || r.statusText);
    toast("Skill registered", `${j.skill_slug} · ${j.steps} steps`, "success", 5000);
    toast("Trigger ready", `Type /${j.trigger} in Claude Code (after restart)`, "info", 6000);
  } catch (e) { toast("Export failed", e.message, "error", 6000); }
}

function newFlow() {
  if (state.nodeMeta.size && !confirm("Discard current flow?")) return;
  state.editor.clear();
  state.nodeMeta.clear();
  $("#flow-name").value = "";
  $("#flow-trigger").value = "";
  $("#flow-desc").value = "";
  clearInspector();
  state.preview = { markdown: "", cycle: null, dirty: false };
  refreshPreview();
  toast("New flow", "Canvas cleared", "info", 2000);
}

async function loadSavedFlows() {
  try {
    const r = await api("/api/flows");
    state.savedFlows = r.flows || [];
    renderSavedFlows();
  } catch (e) {
    showToast("Failed to load saved flows: " + e.message, "error");
    console.error(e);
  }
}

function renderSavedFlows() {
  const host = $("#saved-flows");
  if (!state.savedFlows.length) {
    setSafeHTML(host, `<div class="empty-state small"><p class="muted small">No saved flows yet. Build one and hit Export.</p></div>`);
    return;
  }
  setSafeHTML(host, state.savedFlows.map(f => `
    <div class="saved-flow" data-trigger="${escapeHtml(f.trigger)}">
      <div class="sf-top">
        <span class="sf-name">${escapeHtml(f.name || f.trigger)}</span>
        <button class="sf-del" data-trigger="${escapeHtml(f.trigger)}" title="Delete">✕</button>
      </div>
      <span class="sf-trigger">/${escapeHtml(f.trigger)}</span>
      <span class="sf-desc">${escapeHtml(f.description || "")}</span>
      <span class="sf-meta">${(f.nodes || []).length} nodes · ${(f.edges || []).length} edges</span>
    </div>
  `).join(""));

  $$(".saved-flow", host).forEach(el => {
    el.addEventListener("click", e => {
      if (e.target.classList.contains("sf-del")) return;
      const trig = el.dataset.trigger;
      const flow = state.savedFlows.find(f => f.trigger === trig);
      if (flow) loadFlowIntoEditor(flow);
    });
  });
  $$(".sf-del", host).forEach(b => {
    b.addEventListener("click", async e => {
      e.stopPropagation();
      const trig = b.dataset.trigger;
      if (!confirm(`Delete /${trig} (also removes the exported skill)?`)) return;
      try {
        await api(`/api/flows/${encodeURIComponent(trig)}`, { method: "DELETE" });
        toast("Deleted", `/${trig}`, "success");
        loadSavedFlows();
      } catch (err) { toast("Delete failed", err.message, "error"); }
    });
  });
}

function loadFlowIntoEditor(flow) {
  state.editor.clear();
  state.nodeMeta.clear();
  $("#flow-name").value = flow.name || "";
  $("#flow-trigger").value = flow.trigger || "";
  $("#flow-desc").value = flow.description || "";

  const idMap = new Map();
  for (const n of (flow.nodes || [])) {
    const newId = state.editor.addNode(
      (n.tool?.name || "node").slice(0, 64),
      1, 1, n.pos_x || 100, n.pos_y || 100,
      `t-${n.tool?.type || "skill"}`,
      { tool: n.tool, annotation: n.annotation || "" },
      nodeHtml(n.tool || { type: "skill", name: n.tool?.name || "?" }, n.annotation || ""),
    );
    idMap.set(String(n.id), String(newId));
    state.nodeMeta.set(String(newId), { tool: n.tool, annotation: n.annotation || "" });
  }
  for (const e of (flow.edges || [])) {
    const a = idMap.get(String(e.source));
    const b = idMap.get(String(e.target));
    if (a && b) {
      try {
        state.editor.addConnection(a, b, "output_1", "input_1");
      } catch (e) {
        const isDup = /already exists|duplicate/i.test(e?.message || "");
        if (!isDup) { showToast("Edge add failed: " + e.message, "warn"); console.error(e); }
      }
    }
  }
  schedulePreviewRefresh();
  toast("Loaded", `/${flow.trigger}`, "info", 2000);
}

function wireAppBar() {
  $("#btn-refresh").addEventListener("click", loadInventory);
  $("#btn-new").addEventListener("click", newFlow);
  $("#btn-export").addEventListener("click", exportFlow);
  $("#search").addEventListener("input", e => {
    state.filter.q = e.target.value;
    renderInventory();
  });
  $("#flow-name").addEventListener("input", e => {
    const trig = $("#flow-trigger");
    if (!trig.dataset.touched) {
      trig.value = "toolforge-" + e.target.value.toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "");
    }
    schedulePreviewRefresh();
  });
  $("#flow-trigger").addEventListener("input", e => {
    e.target.dataset.touched = "1";
    schedulePreviewRefresh();
  });
  $("#flow-desc").addEventListener("input", () => schedulePreviewRefresh());
  $("#tab-node").addEventListener("click", () => switchTab("node"));
  $("#tab-preview").addEventListener("click", () => switchTab("preview"));
  document.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault(); saveFlow();
    }
  });
}

async function boot() {
  initEditor();
  wireAppBar();
  try {
    await loadCsrfToken();
  } catch (e) {
    toast("CSRF bootstrap failed", e.message, "error", 6000);
    return;
  }
  await Promise.all([loadInventory(), loadSavedFlows()]);
}

document.addEventListener("DOMContentLoaded", boot);
