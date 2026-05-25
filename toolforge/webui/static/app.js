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
};

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

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) {
    const txt = await r.text().catch(() => r.statusText);
    throw new Error(`${r.status} ${r.statusText}: ${txt.slice(0, 200)}`);
  }
  return r.json();
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
    const tool = JSON.parse(raw);
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
  });

  $("#zoom-in").addEventListener("click", () => editor.zoom_in());
  $("#zoom-out").addEventListener("click", () => editor.zoom_out());
  $("#zoom-reset").addEventListener("click", () => editor.zoom_reset());
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
}

function clearInspector() {
  state.selectedNodeId = null;
  $("#inspector-sub").textContent = "Click a node";
  setSafeHTML($("#inspector-body"),
    `<div class="empty-state"><div class="empty-glyph">◌</div><p>Select a node to set the prompt / annotation for that step.</p></div>`
  );
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
  try {
    await saveFlow();
    const flow = collectFlow();
    const r = await api("/api/export", { method: "POST", body: flow });
    toast("Skill registered", `${r.skill_slug} · ${r.steps} steps`, "success", 5000);
    toast("Trigger ready", `Type /${r.trigger} in Claude Code (after restart)`, "info", 6000);
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
  toast("New flow", "Canvas cleared", "info", 2000);
}

async function loadSavedFlows() {
  try {
    const r = await api("/api/flows");
    state.savedFlows = r.flows || [];
    renderSavedFlows();
  } catch (e) { /* silent */ }
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
      try { state.editor.addConnection(a, b, "output_1", "input_1"); } catch { /* ignore dup */ }
    }
  }
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
  });
  $("#flow-trigger").addEventListener("input", e => { e.target.dataset.touched = "1"; });
  document.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault(); saveFlow();
    }
  });
}

async function boot() {
  initEditor();
  wireAppBar();
  await Promise.all([loadInventory(), loadSavedFlows()]);
}

document.addEventListener("DOMContentLoaded", boot);
