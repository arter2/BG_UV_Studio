import { ICON_CATEGORIES, iconById } from "./icons.js";

const STORAGE_KEY = "uv-studio-layout-designer-v1";
const GRID = 8;
const PANEL_W = 300;

let spec = null;
let idCounter = 1;
function nextId() {
  return `blk-${idCounter++}`;
}

const state = {
  snap: true,
  zoom: 1,
  activeTab: "Edit",
  nodes: [],
  selection: [],
  clipboard: null,
  pan: { x: 48, y: 32 },
  drag: null,
  spec: null,
};

const canvas = document.getElementById("canvas");
const canvasInner = document.getElementById("canvas-inner");
const propsPanel = document.getElementById("props-panel");
const tabList = document.getElementById("tab-list");
const statusEl = document.getElementById("status");
const jsonPreview = document.getElementById("json-preview");
const snapCheckbox = document.getElementById("snap-grid");
const zoomRange = document.getElementById("zoom-range");
const zoomLabel = document.getElementById("zoom-label");

const MAYA = {
  panel: "#3a3a3a",
  control: "#4a4a4a",
  viewport: "#2a2a2a",
  accent: "#5b8def",
  section: "#454545",
};

function status(msg) {
  if (statusEl) statusEl.textContent = msg;
}

function snap(v) {
  if (!state.snap) return Math.round(v);
  return Math.round(v / GRID) * GRID;
}

function nodeById(id) {
  return state.nodes.find((n) => n.id === id);
}

function visibleNodes() {
  return state.nodes.filter(
    (n) => n.tab == null || n.tab === state.activeTab || n.type === "tab"
  );
}

function maxZ() {
  return state.nodes.reduce((m, n) => Math.max(m, n.zIndex ?? 0), 0);
}

function setSelection(ids, additive = false) {
  if (additive) {
    const s = new Set(state.selection);
    for (const id of ids) (s.has(id) ? s.delete(id) : s.add(id));
    state.selection = [...s];
  } else {
    state.selection = [...ids];
  }
  renderSelection();
  renderProps();
}

function selectedNodes() {
  return state.selection.map(nodeById).filter(Boolean);
}

function buildDefaultLayout(uvSpec) {
  const nodes = [];
  const tabs = uvSpec.TAB_ORDER;
  let tabX = 8;
  const tabY = 8;
  const tabH = 26;
  for (const name of tabs) {
    const w = Math.max(52, name.length * 8 + 16);
    nodes.push({
      id: nextId(),
      type: "tab",
      tab: null,
      tabName: name,
      label: name,
      x: tabX,
      y: tabY,
      w,
      h: tabH,
      fill: MAYA.control,
      zIndex: 10,
    });
    tabX += w + 2;
  }

  nodes.push({
    id: nextId(),
    type: "viewport",
    tab: null,
    label: "UV viewport",
    x: 8,
    y: 40,
    w: PANEL_W - 16,
    h: 200,
    fill: MAYA.viewport,
    zIndex: 5,
  });

  let ax = 8;
  const ay = 248;
  for (const action of uvSpec.ACTION_BAR) {
    nodes.push({
      id: nextId(),
      type: "action",
      tab: null,
      toolId: action.id,
      label: action.label,
      x: ax,
      y: ay,
      w: 68,
      h: 28,
      fill: MAYA.control,
      zIndex: 8,
    });
    ax += 72;
  }

  for (const tabName of tabs) {
    const sectionMap = uvSpec.sections[tabName] || {};
    let sy = 288;
    for (const [sectionName, tools] of Object.entries(sectionMap)) {
      const toolRows = Math.ceil(tools.length / 2);
      const sh = 28 + toolRows * 34 + 12;
      const sectionId = nextId();
      nodes.push({
        id: sectionId,
        type: "section",
        tab: tabName,
        label: sectionName,
        x: 8,
        y: sy,
        w: PANEL_W - 16,
        h: sh,
        fill: MAYA.section,
        zIndex: 2,
      });
      let tx = 16;
      let ty = sy + 32;
      tools.forEach((tool, i) => {
        if (i > 0 && i % 2 === 0) {
          tx = 16;
          ty += 34;
        }
        nodes.push({
          id: nextId(),
          type: "tool",
          tab: tabName,
          toolId: tool.id,
          label: tool.label,
          x: tx,
          y: ty,
          w: tool.label.length > 10 ? 118 : 96,
          h: 28,
          fill: MAYA.control,
          zIndex: 3,
        });
        tx += 100;
      });
      sy += sh + 10;
    }
  }

  return nodes;
}

function exportDocument() {
  const tabStrip = state.nodes
    .filter((n) => n.type === "tab")
    .sort((a, b) => a.x - b.x)
    .map((n) => ({
      tab: n.tabName,
      label: n.label,
      x: n.x,
      y: n.y,
      w: n.w,
      h: n.h,
    }));

  const byTab = {};
  for (const t of spec.TAB_ORDER) {
    byTab[t] = state.nodes
      .filter((n) => n.tab === t)
      .map(({ id, type, toolId, label, x, y, w, h, fill, locked }) => ({
        id,
        type,
        toolId,
        label,
        x,
        y,
        w,
        h,
        fill,
        locked: !!locked,
      }));
  }

  return {
    format: "uvstudio-panel-layout",
    version: 1,
    snapGrid: GRID,
    activeTab: state.activeTab,
    tabStrip,
    global: state.nodes
      .filter((n) => n.tab == null && n.type !== "tab")
      .map(({ id, type, toolId, label, x, y, w, h, fill }) => ({
        id,
        type,
        toolId,
        label,
        x,
        y,
        w,
        h,
        fill,
      })),
    tabs: byTab,
  };
}

function refreshJsonPreview() {
  if (jsonPreview) {
    jsonPreview.value = JSON.stringify(exportDocument(), null, 2);
  }
}

let saveTimer;
function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        idCounter,
        snap: state.snap,
        zoom: state.zoom,
        activeTab: state.activeTab,
        nodes: state.nodes,
        pan: state.pan,
      })
    );
    refreshJsonPreview();
  }, 200);
}

function loadStored() {
  const raw = localStorage.getItem(STORAGE_KEY);
  if (!raw) return false;
  try {
    const data = JSON.parse(raw);
    state.nodes = data.nodes || [];
    idCounter = data.idCounter || 1;
    state.snap = data.snap ?? true;
    state.zoom = data.zoom ?? 1;
    state.activeTab = data.activeTab || "Edit";
    state.pan = data.pan || { x: 48, y: 32 };
    return true;
  } catch {
    return false;
  }
}

function loadFromFile(obj) {
  if (obj.format === "uvstudio-panel-layout" && obj.tabs) {
    const nodes = [];
    for (const t of obj.tabStrip || []) {
      nodes.push({
        id: nextId(),
        type: "tab",
        tab: null,
        tabName: t.tab,
        label: t.label || t.tab,
        x: t.x,
        y: t.y,
        w: t.w,
        h: t.h,
        fill: MAYA.control,
        zIndex: 10,
      });
    }
    for (const g of obj.global || []) {
      nodes.push({ ...g, tab: null, zIndex: g.zIndex ?? 5 });
    }
    for (const [tabName, blocks] of Object.entries(obj.tabs)) {
      for (const b of blocks) {
        nodes.push({ ...b, tab: tabName, zIndex: b.zIndex ?? 2 });
      }
    }
    state.nodes = nodes;
    state.activeTab = obj.activeTab || spec.TAB_ORDER[0];
    setSelection([]);
    renderAll();
    status("Layout JSON loaded");
    return;
  }
  if (obj.nodes) {
    state.nodes = obj.nodes;
    idCounter = obj.idCounter || idCounter;
    renderAll();
  }
}

function svgIcon(path, size = 18) {
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2"><path d="${path}"/></svg>`;
}

function wireInlineRename(label, node) {
  label.addEventListener("dblclick", (e) => {
    e.stopPropagation();
    if (node.locked) return;
    label.contentEditable = "true";
    label.focus();
  });
  label.addEventListener("blur", () => {
    label.contentEditable = "false";
    node.label = label.textContent.trim() || node.label;
    scheduleSave();
    renderProps();
  });
  label.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      label.blur();
    }
  });
}

function renderNode(node) {
  const el = document.createElement("div");
  const isActiveTab =
    node.type === "tab" && node.tabName === state.activeTab;
  el.className = `node node-${node.type}${node.locked ? " is-locked" : ""}${isActiveTab ? " is-active-tab" : ""}`;
  el.dataset.id = node.id;
  el.style.left = `${node.x}px`;
  el.style.top = `${node.y}px`;
  el.style.width = `${node.w}px`;
  el.style.height = `${node.h}px`;
  el.style.zIndex = String(node.zIndex ?? 1);
  if (node.fill) el.style.background = node.fill;

  const label = document.createElement("span");
  label.className = "node-label";
  label.textContent = node.label || node.type;
  label.contentEditable = "false";
  wireInlineRename(label, node);

  if (node.type === "viewport") {
    el.appendChild(label);
    el.classList.add("node-viewport");
  } else if (node.type === "tab") {
    el.appendChild(label);
    el.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      state.activeTab = node.tabName;
      renderTabList();
      renderCanvas();
      scheduleSave();
    });
  } else if (node.type === "section") {
    el.appendChild(label);
  } else if (node.type === "tool" || node.type === "action") {
    const cap = document.createElement("span");
    cap.className = "tool-caption node-label";
    cap.textContent = node.label;
    wireInlineRename(cap, node);
    el.appendChild(cap);
  } else if (node.type === "icon") {
    const ic = iconById(node.iconId || "settings");
    el.innerHTML = svgIcon(ic.path, Math.min(node.w, node.h) - 10);
  }

  if (!node.locked) {
    const handle = document.createElement("span");
    handle.className = "resize-handle";
    handle.addEventListener("mousedown", (e) => onResizeStart(e, node));
    el.appendChild(handle);
  }

  el.addEventListener("mousedown", (e) => onNodeDown(e, node));
  return el;
}

function renderCanvas() {
  canvasInner.style.transform = `translate(${state.pan.x}px, ${state.pan.y}px) scale(${state.zoom})`;
  canvasInner.innerHTML = "";

  const frame = document.createElement("div");
  frame.className = "panel-frame";
  frame.style.width = `${PANEL_W}px`;
  frame.style.minHeight = "640px";
  canvasInner.appendChild(frame);

  const sorted = [...visibleNodes()].sort(
    (a, b) => (a.zIndex ?? 0) - (b.zIndex ?? 0)
  );
  for (const node of sorted) {
    frame.appendChild(renderNode(node));
  }
  renderSelection();
}

function renderSelection() {
  canvasInner.querySelectorAll(".node.is-selected").forEach((n) => {
    n.classList.remove("is-selected");
  });
  for (const id of state.selection) {
    canvasInner.querySelector(`[data-id="${id}"]`)?.classList.add("is-selected");
  }
}

function renderTabList() {
  if (!tabList || !spec) return;
  tabList.innerHTML = "";
  for (const name of spec.TAB_ORDER) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "tab-pick" + (name === state.activeTab ? " active" : "");
    btn.textContent = name;
    btn.addEventListener("click", () => {
      state.activeTab = name;
      renderTabList();
      renderCanvas();
      scheduleSave();
    });
    tabList.appendChild(btn);
  }
}

function renderProps() {
  const nodes = selectedNodes();
  if (!nodes.length) {
    propsPanel.innerHTML = '<p class="muted">Nothing selected.</p>';
    return;
  }
  const n = nodes[0];
  propsPanel.innerHTML = `
    <label>Label<input id="p-label" type="text" value="${esc(n.label || "")}" /></label>
    <label>X<input id="p-x" type="number" value="${n.x}" /></label>
    <label>Y<input id="p-y" type="number" value="${n.y}" /></label>
    <label>W<input id="p-w" type="number" min="8" value="${n.w}" /></label>
    <label>H<input id="p-h" type="number" min="8" value="${n.h}" /></label>
    <label>Color<input id="p-fill" type="color" value="${toHex(n.fill || MAYA.control)}" /></label>
  `;
  const apply = () => {
    for (const node of nodes) {
      node.label = propsPanel.querySelector("#p-label").value;
      node.x = snap(+propsPanel.querySelector("#p-x").value);
      node.y = snap(+propsPanel.querySelector("#p-y").value);
      node.w = Math.max(8, +propsPanel.querySelector("#p-w").value);
      node.h = Math.max(8, +propsPanel.querySelector("#p-h").value);
      node.fill = propsPanel.querySelector("#p-fill").value;
    }
    renderCanvas();
    scheduleSave();
  };
  propsPanel.querySelectorAll("input").forEach((inp) => {
    inp.addEventListener("change", apply);
    inp.addEventListener("input", () => {
      if (inp.type === "color") apply();
    });
  });
}

function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;");
}

function toHex(c) {
  if (c?.startsWith("#") && c.length >= 7) return c.slice(0, 7);
  return "#4a4a4a";
}

function onNodeDown(e, node) {
  if (node.locked) {
    status("Locked — unlock from Extra tools");
    return;
  }
  e.stopPropagation();
  if (e.target.classList.contains("resize-handle")) return;

  if (node.type === "tab" && e.detail === 1) {
    state.activeTab = node.tabName;
    renderTabList();
  }

  if (e.shiftKey) setSelection([node.id], true);
  else if (!state.selection.includes(node.id)) setSelection([node.id]);

  const ids = [...state.selection];
  const starts = new Map();
  for (const id of ids) {
    const n = nodeById(id);
    if (n && !n.locked) starts.set(id, { x: n.x, y: n.y });
  }
  state.drag = {
    kind: "move",
    sx: e.clientX,
    sy: e.clientY,
    starts,
    zoom: state.zoom,
  };
}

function onResizeStart(e, node) {
  e.stopPropagation();
  if (node.locked) return;
  setSelection([node.id]);
  state.drag = {
    kind: "resize",
    nodeId: node.id,
    sx: e.clientX,
    sy: e.clientY,
    ow: node.w,
    oh: node.h,
    zoom: state.zoom,
  };
}

function onCanvasDown(e) {
  if (e.target.closest(".node")) return;
  setSelection([]);
  if (e.altKey) {
    state.drag = {
      kind: "pan",
      sx: e.clientX,
      sy: e.clientY,
      pan: { ...state.pan },
    };
  }
}

function onMove(e) {
  const d = state.drag;
  if (!d) return;
  if (d.kind === "pan") {
    state.pan.x = d.pan.x + (e.clientX - d.sx);
    state.pan.y = d.pan.y + (e.clientY - d.sy);
    renderCanvas();
    return;
  }
  const dx = (e.clientX - d.sx) / d.zoom;
  const dy = (e.clientY - d.sy) / d.zoom;
  if (d.kind === "move") {
    for (const [id, start] of d.starts) {
      const n = nodeById(id);
      if (n) {
        n.x = snap(start.x + dx);
        n.y = snap(start.y + dy);
      }
    }
    renderCanvas();
    renderProps();
  } else if (d.kind === "resize") {
    const n = nodeById(d.nodeId);
    if (n) {
      n.w = Math.max(GRID, snap(d.ow + dx));
      n.h = Math.max(GRID, snap(d.oh + dy));
      renderCanvas();
      renderProps();
    }
  }
}

function onUp() {
  if (state.drag) scheduleSave();
  state.drag = null;
}

function copySel() {
  const nodes = selectedNodes();
  if (!nodes.length) return;
  state.clipboard = nodes.map((n) => JSON.parse(JSON.stringify(n)));
  status(`Copied ${nodes.length}`);
}

function pasteSel() {
  if (!state.clipboard?.length) return;
  const copies = state.clipboard.map((n) => ({
    ...n,
    id: nextId(),
    x: n.x + GRID * 2,
    y: n.y + GRID * 2,
    zIndex: maxZ() + 1,
  }));
  state.nodes.push(...copies);
  setSelection(copies.map((c) => c.id));
  renderAll();
  status("Pasted");
}

function toggleLock() {
  for (const id of state.selection) {
    const n = nodeById(id);
    if (n) n.locked = !n.locked;
  }
  renderAll();
  scheduleSave();
}

function reorderZ(delta) {
  const ids = new Set(state.selection);
  const sorted = [...state.nodes].sort(
    (a, b) => (a.zIndex ?? 0) - (b.zIndex ?? 0)
  );
  const moving = sorted.filter((n) => ids.has(n.id));
  const rest = sorted.filter((n) => !ids.has(n.id));
  const merged = delta > 0 ? [...rest, ...moving] : [...moving, ...rest];
  merged.forEach((n, i) => {
    n.zIndex = i;
  });
  renderAll();
  scheduleSave();
}

function renderIconLibrary(q = "") {
  const lib = document.getElementById("icon-library");
  if (!lib) return;
  lib.innerHTML = "";
  const query = q.toLowerCase();
  for (const [cat, items] of Object.entries(ICON_CATEGORIES)) {
    const grid = document.createElement("div");
    grid.className = "icon-grid";
    for (const item of items) {
      if (query && !item.label.toLowerCase().includes(query)) continue;
      const b = document.createElement("button");
      b.type = "button";
      b.className = "icon-pick";
      b.innerHTML = svgIcon(item.path, 20);
      b.title = item.label;
      b.addEventListener("click", () => {
        const node = {
          id: nextId(),
          type: "icon",
          tab: state.activeTab,
          iconId: item.id,
          label: item.label,
          x: 16,
          y: 320,
          w: 36,
          h: 36,
          fill: MAYA.control,
          zIndex: maxZ() + 1,
        };
        state.nodes.push(node);
        setSelection([node.id]);
        renderAll();
        scheduleSave();
      });
      grid.appendChild(b);
    }
    if (grid.children.length) {
      const h = document.createElement("h4");
      h.textContent = cat;
      lib.appendChild(h);
      lib.appendChild(grid);
    }
  }
}

function renderAll() {
  renderCanvas();
  renderProps();
  refreshJsonPreview();
}

function wireUi() {
  snapCheckbox?.addEventListener("change", () => {
    state.snap = snapCheckbox.checked;
    scheduleSave();
  });
  zoomRange?.addEventListener("input", () => {
    state.zoom = +zoomRange.value / 100;
    if (zoomLabel) zoomLabel.textContent = `${zoomRange.value}%`;
    renderCanvas();
    scheduleSave();
  });
  document.getElementById("btn-export")?.addEventListener("click", () => {
    const blob = new Blob([JSON.stringify(exportDocument(), null, 2)], {
      type: "application/json",
    });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "uv-studio-layout.json";
    a.click();
    URL.revokeObjectURL(a.href);
    status("Exported layout JSON");
  });
  document.getElementById("load-json")?.addEventListener("change", (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        loadFromFile(JSON.parse(reader.result));
        scheduleSave();
      } catch {
        status("Invalid JSON file");
      }
    };
    reader.readAsText(file);
    e.target.value = "";
  });
  document.getElementById("btn-group")?.addEventListener("click", () => {
    const nodes = selectedNodes().filter((n) => !n.locked && n.type !== "viewport");
    if (nodes.length < 2) {
      status("Shift+click to select 2+ items, then Group");
      return;
    }
    const xs = nodes.map((n) => n.x);
    const ys = nodes.map((n) => n.y);
    const pad = 6;
    const gx = Math.min(...xs) - pad;
    const gy = Math.min(...ys) - pad;
    const gw = Math.max(...nodes.map((n) => n.x + n.w)) - gx + pad;
    const gh = Math.max(...nodes.map((n) => n.y + n.h)) - gy + pad;
    state.nodes.push({
      id: nextId(),
      type: "section",
      tab: state.activeTab,
      label: "Group",
      x: gx,
      y: gy,
      w: gw,
      h: gh,
      fill: "rgba(91,141,239,0.12)",
      zIndex: maxZ() + 1,
      locked: false,
    });
    renderAll();
    scheduleSave();
    status("Group frame added");
  });
  document.getElementById("btn-lock")?.addEventListener("click", toggleLock);
  document.getElementById("btn-forward")?.addEventListener("click", () =>
    reorderZ(1)
  );
  document.getElementById("btn-back")?.addEventListener("click", () =>
    reorderZ(-1)
  );
  document.getElementById("icon-search")?.addEventListener("input", (e) => {
    renderIconLibrary(e.target.value);
  });

  canvas?.addEventListener("mousedown", onCanvasDown);
  window.addEventListener("mousemove", onMove);
  window.addEventListener("mouseup", onUp);
  window.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, [contenteditable=true]")) return;
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key === "c") {
      e.preventDefault();
      copySel();
    } else if (mod && e.key === "v") {
      e.preventDefault();
      pasteSel();
    } else if (e.key === "Delete" || e.key === "Backspace") {
      const sel = new Set(state.selection);
      state.nodes = state.nodes.filter((n) => !sel.has(n.id));
      setSelection([]);
      renderAll();
      scheduleSave();
    }
  });
}

async function init() {
  spec = await fetch("js/uv-spec.json").then((r) => r.json());
  state.spec = spec;
  wireUi();
  snapCheckbox.checked = state.snap;
  if (!loadStored()) {
    state.nodes = buildDefaultLayout(spec);
    state.activeTab = "Edit";
  }
  renderTabList();
  renderIconLibrary();
  if (zoomRange) {
    zoomRange.value = String(Math.round(state.zoom * 100));
    zoomLabel.textContent = `${zoomRange.value}%`;
  }
  renderAll();
  status("Autosaving in this browser — export JSON when ready");
}

init();
