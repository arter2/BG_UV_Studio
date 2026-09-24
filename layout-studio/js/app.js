import {
  ICON_CATEGORIES,
  MENU_LAYOUTS,
  iconById,
  allIconsFlat,
} from "./icons.js";

const STORAGE_KEY = "layout-studio-doc-v1";

let idCounter = 1;
function nextId() {
  return `el-${idCounter++}`;
}

/** @typedef {{ id: string, type: string, x: number, y: number, w: number, h: number, label?: string, color?: string, fill?: string, locked?: boolean, groupId?: string|null, iconId?: string, zIndex?: number, children?: string[] }} DocNode */

/** @type {{ nodes: DocNode[], selection: string[], clipboard: DocNode[]|null, pan: {x:number,y:number}, zoom: number }} */
const state = {
  nodes: [],
  selection: [],
  clipboard: null,
  pan: { x: 40, y: 40 },
  zoom: 1,
  tool: "select",
  drag: null,
  marquee: null,
};

const canvas = document.getElementById("canvas");
const canvasInner = document.getElementById("canvas-inner");
const layersList = document.getElementById("layers-list");
const propsPanel = document.getElementById("props-panel");
const iconLibrary = document.getElementById("icon-library");
const importLayoutSelect = document.getElementById("import-layout");
const statusEl = document.getElementById("status");

const DEFAULT_PANEL_COLOR = "#e8edf5";
const DEFAULT_BLOCK_COLOR = "#ffffff";

function status(msg) {
  if (statusEl) statusEl.textContent = msg;
}

function serializeDoc() {
  return JSON.stringify({
    nodes: state.nodes,
    idCounter,
    pan: state.pan,
    zoom: state.zoom,
  });
}

function loadDoc(raw) {
  try {
    const data = JSON.parse(raw);
    state.nodes = data.nodes || [];
    idCounter = data.idCounter || 1;
    state.pan = data.pan || { x: 40, y: 40 };
    state.zoom = data.zoom || 1;
    state.selection = [];
    renderAll();
    status("Document loaded");
  } catch (e) {
    status("Could not load document");
  }
}

function saveToStorage() {
  localStorage.setItem(STORAGE_KEY, serializeDoc());
  status("Saved locally");
}

function loadFromStorage() {
  const raw = localStorage.getItem(STORAGE_KEY);
  if (raw) loadDoc(raw);
}

function maxZ() {
  return state.nodes.reduce((m, n) => Math.max(m, n.zIndex ?? 0), 0);
}

function nodeById(id) {
  return state.nodes.find((n) => n.id === id);
}

function selectedNodes() {
  return state.selection.map(nodeById).filter(Boolean);
}

function setSelection(ids, additive = false) {
  if (additive) {
    const set = new Set(state.selection);
    for (const id of ids) {
      if (set.has(id)) set.delete(id);
      else set.add(id);
    }
    state.selection = [...set];
  } else {
    state.selection = [...ids];
  }
  renderSelection();
  renderProps();
  renderLayers();
}

function createNode(partial) {
  const node = {
    id: nextId(),
    type: "panel",
    x: 80,
    y: 80,
    w: 160,
    h: 100,
    label: "Panel",
    fill: DEFAULT_PANEL_COLOR,
    locked: false,
    groupId: null,
    zIndex: maxZ() + 1,
    ...partial,
  };
  state.nodes.push(node);
  return node;
}

function duplicateNodes(nodes, offsetX = 20, offsetY = 20) {
  const idMap = new Map();
  const copies = nodes.map((n) => {
    const copy = {
      ...JSON.parse(JSON.stringify(n)),
      id: nextId(),
      x: n.x + offsetX,
      y: n.y + offsetY,
      locked: false,
      zIndex: maxZ() + 1 + idMap.size,
    };
    idMap.set(n.id, copy.id);
    return copy;
  });
  for (const c of copies) {
    if (c.groupId && idMap.has(c.groupId)) {
      c.groupId = idMap.get(c.groupId);
    }
  }
  state.nodes.push(...copies);
  return copies.map((c) => c.id);
}

function deleteSelection() {
  const sel = new Set(state.selection);
  state.nodes = state.nodes.filter((n) => !sel.has(n.id));
  for (const n of state.nodes) {
    if (n.type === "group" && n.children) {
      n.children = n.children.filter((cid) => !sel.has(cid));
    }
  }
  state.selection = [];
  renderAll();
}

function groupSelection() {
  const nodes = selectedNodes().filter((n) => n.type !== "group" && !n.locked);
  if (nodes.length < 2) {
    status("Select 2+ unlocked items to group");
    return;
  }
  const xs = nodes.map((n) => n.x);
  const ys = nodes.map((n) => n.y);
  const xe = nodes.map((n) => n.x + n.w);
  const ye = nodes.map((n) => n.y + n.h);
  const pad = 8;
  const gx = Math.min(...xs) - pad;
  const gy = Math.min(...ys) - pad;
  const gw = Math.max(...xe) - gx + pad;
  const gh = Math.max(...ye) - gy + pad;
  const group = createNode({
    type: "group",
    x: gx,
    y: gy,
    w: gw,
    h: gh,
    label: "Group",
    fill: "rgba(99,102,241,0.08)",
    children: nodes.map((n) => n.id),
  });
  for (const n of nodes) {
    n.groupId = group.id;
  }
  setSelection([group.id]);
  renderAll();
  status("Grouped selection");
}

function ungroupSelection() {
  const groups = selectedNodes().filter((n) => n.type === "group");
  if (!groups.length) {
    status("Select a group to ungroup");
    return;
  }
  for (const g of groups) {
    for (const cid of g.children || []) {
      const child = nodeById(cid);
      if (child) child.groupId = null;
    }
    state.nodes = state.nodes.filter((n) => n.id !== g.id);
  }
  state.selection = [];
  renderAll();
  status("Ungrouped");
}

function toggleLock(ids = state.selection) {
  for (const id of ids) {
    const n = nodeById(id);
    if (n) n.locked = !n.locked;
  }
  renderAll();
}

function reorderZ(ids, delta) {
  const sorted = [...state.nodes].sort(
    (a, b) => (a.zIndex ?? 0) - (b.zIndex ?? 0)
  );
  const idSet = new Set(ids);
  const moving = sorted.filter((n) => idSet.has(n.id));
  const rest = sorted.filter((n) => !idSet.has(n.id));
  let merged;
  if (delta > 0) {
    merged = [...rest, ...moving];
  } else {
    merged = [...moving, ...rest];
  }
  merged.forEach((n, i) => {
    n.zIndex = i + 1;
  });
  renderAll();
}

function svgIcon(path, size = 20) {
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="${path}"/></svg>`;
}

function renderNodeEl(node) {
  const el = document.createElement("div");
  el.className = `node node-${node.type}${node.type === "group" ? " node-group" : ""}${node.locked ? " is-locked" : ""}`;
  el.dataset.id = node.id;
  el.style.left = `${node.x}px`;
  el.style.top = `${node.y}px`;
  el.style.width = `${node.w}px`;
  el.style.height = `${node.h}px`;
  el.style.zIndex = String(node.zIndex ?? 1);
  if (node.fill && node.type !== "linebreak") {
    el.style.background = node.fill;
  }

  if (node.type === "panel" || node.type === "group" || node.type === "block") {
    const label = document.createElement("span");
    label.className = "node-label";
    label.textContent = node.label || node.type;
    el.appendChild(label);
  } else if (node.type === "text") {
    el.classList.add("node-text");
    el.textContent = node.label || "Text";
  } else if (node.type === "textfield") {
    el.classList.add("node-textfield");
    const input = document.createElement("span");
    input.className = "tf-placeholder";
    input.textContent = node.label || "Text field";
    el.appendChild(input);
  } else if (node.type === "icon") {
    el.classList.add("node-icon");
    const icon = iconById(node.iconId || "star");
    el.innerHTML = svgIcon(icon.path, Math.min(node.w, node.h) - 8);
    if (node.label) {
      const cap = document.createElement("span");
      cap.className = "icon-caption";
      cap.textContent = node.label;
      el.appendChild(cap);
    }
  } else if (node.type === "linebreak") {
    el.classList.add("node-linebreak");
    el.innerHTML = '<span class="break-line"></span>';
  }

  if (node.locked) {
    const badge = document.createElement("span");
    badge.className = "lock-badge";
    badge.title = "Locked";
    badge.innerHTML = svgIcon(iconById("lock").path, 12);
    el.appendChild(badge);
  }

  el.addEventListener("mousedown", (e) => onNodeMouseDown(e, node));
  return el;
}

function renderCanvas() {
  canvasInner.style.transform = `translate(${state.pan.x}px, ${state.pan.y}px) scale(${state.zoom})`;
  canvasInner.innerHTML = "";
  const sorted = [...state.nodes].sort(
    (a, b) => (a.zIndex ?? 0) - (b.zIndex ?? 0)
  );
  for (const node of sorted) {
    canvasInner.appendChild(renderNodeEl(node));
  }
  renderSelection();
}

function renderSelection() {
  canvasInner.querySelectorAll(".node.is-selected").forEach((el) => {
    el.classList.remove("is-selected");
  });
  for (const id of state.selection) {
    const el = canvasInner.querySelector(`[data-id="${id}"]`);
    if (el) el.classList.add("is-selected");
  }
}

function renderLayers() {
  if (!layersList) return;
  layersList.innerHTML = "";
  const sorted = [...state.nodes].sort(
    (a, b) => (b.zIndex ?? 0) - (a.zIndex ?? 0)
  );
  for (const node of sorted) {
    const row = document.createElement("button");
    row.type = "button";
    row.className =
      "layer-row" + (state.selection.includes(node.id) ? " active" : "");
    row.innerHTML = `<span>${node.type}</span><span class="layer-name">${escapeHtml(node.label || node.iconId || node.id)}</span>${node.locked ? " 🔒" : ""}`;
    row.addEventListener("click", (e) => {
      setSelection([node.id], e.shiftKey);
    });
    layersList.appendChild(row);
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function renderProps() {
  if (!propsPanel) return;
  const nodes = selectedNodes();
  if (!nodes.length) {
    propsPanel.innerHTML =
      '<p class="muted">Select an object to edit color, size, and lock.</p>';
    return;
  }
  const n = nodes[0];
  const sameFill = nodes.every((x) => x.fill === n.fill);
  const fill = sameFill ? n.fill || DEFAULT_PANEL_COLOR : "";

  propsPanel.innerHTML = `
    <label>Label<input id="prop-label" type="text" value="${escapeAttr(n.label || "")}" /></label>
    <label>Width<input id="prop-w" type="number" min="8" value="${n.w}" /></label>
    <label>Height<input id="prop-h" type="number" min="4" value="${n.h}" /></label>
    <label>Fill color<input id="prop-fill" type="color" value="${toHexColor(fill || DEFAULT_PANEL_COLOR)}" /></label>
    <div class="prop-actions">
      <button type="button" id="prop-lock">${nodes.every((x) => x.locked) ? "Unlock" : "Lock"}</button>
      <button type="button" id="prop-forward">Bring forward</button>
      <button type="button" id="prop-back">Send back</button>
    </div>
    ${nodes.length > 1 ? `<p class="muted">${nodes.length} items selected</p>` : ""}
  `;

  propsPanel.querySelector("#prop-label")?.addEventListener("change", (e) => {
    const v = e.target.value;
    for (const node of nodes) node.label = v;
    renderCanvas();
    renderLayers();
  });
  propsPanel.querySelector("#prop-w")?.addEventListener("change", (e) => {
    const v = +e.target.value;
    for (const node of nodes) node.w = Math.max(8, v);
    renderCanvas();
  });
  propsPanel.querySelector("#prop-h")?.addEventListener("change", (e) => {
    const v = +e.target.value;
    for (const node of nodes) node.h = Math.max(4, v);
    renderCanvas();
  });
  propsPanel.querySelector("#prop-fill")?.addEventListener("input", (e) => {
    const v = e.target.value;
    for (const node of nodes) {
      if (node.type !== "linebreak") node.fill = v;
    }
    renderCanvas();
  });
  propsPanel.querySelector("#prop-lock")?.addEventListener("click", () => {
    toggleLock(nodes.map((x) => x.id));
  });
  propsPanel.querySelector("#prop-forward")?.addEventListener("click", () => {
    reorderZ(
      nodes.map((x) => x.id),
      1
    );
  });
  propsPanel.querySelector("#prop-back")?.addEventListener("click", () => {
    reorderZ(
      nodes.map((x) => x.id),
      -1
    );
  });
}

function escapeAttr(s) {
  return escapeHtml(s).replace(/"/g, "&quot;");
}

function toHexColor(c) {
  if (!c || c.startsWith("rgba")) return DEFAULT_PANEL_COLOR;
  if (c.startsWith("#") && c.length >= 7) return c.slice(0, 7);
  return DEFAULT_PANEL_COLOR;
}

function renderIconLibrary(filter = "") {
  if (!iconLibrary) return;
  iconLibrary.innerHTML = "";
  const q = filter.toLowerCase();
  for (const [cat, items] of Object.entries(ICON_CATEGORIES)) {
    const section = document.createElement("div");
    section.className = "icon-section";
    section.innerHTML = `<h4>${cat}</h4>`;
    const grid = document.createElement("div");
    grid.className = "icon-grid";
    for (const item of items) {
      if (q && !item.label.toLowerCase().includes(q) && !item.id.includes(q))
        continue;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "icon-pick";
      btn.title = item.label;
      btn.innerHTML = svgIcon(item.path, 22);
      btn.addEventListener("click", () => {
        const node = createNode({
          type: "icon",
          iconId: item.id,
          w: 40,
          h: 40,
          label: "",
          fill: DEFAULT_BLOCK_COLOR,
        });
        setSelection([node.id]);
        renderAll();
        status(`Added ${item.label} icon`);
      });
      grid.appendChild(btn);
    }
    if (grid.children.length) {
      section.appendChild(grid);
      iconLibrary.appendChild(section);
    }
  }
}

function renderAll() {
  renderCanvas();
  renderLayers();
  renderProps();
}

function onNodeMouseDown(e, node) {
  if (node.locked) {
    status("Object is locked — unlock in the inspector or press L");
    e.stopPropagation();
    return;
  }
  e.stopPropagation();
  if (e.shiftKey) {
    setSelection([node.id], true);
  } else if (!state.selection.includes(node.id)) {
    setSelection([node.id]);
  }

  let ids = state.selection.includes(node.id)
    ? [...state.selection]
    : [node.id];
  if (node.type === "group" && node.children?.length) {
    ids = [...new Set([node.id, ...node.children])];
  }
  const startPositions = new Map();
  for (const id of ids) {
    const n = nodeById(id);
    if (n && !n.locked) startPositions.set(id, { x: n.x, y: n.y });
  }

  state.drag = {
    mode: "move",
    startX: e.clientX,
    startY: e.clientY,
    startPositions,
    zoom: state.zoom,
  };
}

function onCanvasMouseDown(e) {
  if (e.target !== canvas && e.target !== canvasInner) return;
  if (e.button === 1 || (e.button === 0 && e.altKey)) {
    state.drag = {
      mode: "pan",
      startX: e.clientX,
      startY: e.clientY,
      panStart: { ...state.pan },
    };
    return;
  }
  setSelection([]);
  const rect = canvasInner.getBoundingClientRect();
  state.marquee = {
    startX: (e.clientX - rect.left) / state.zoom,
    startY: (e.clientY - rect.top) / state.zoom,
    el: document.createElement("div"),
  };
  state.marquee.el.className = "marquee";
  canvasInner.appendChild(state.marquee.el);
}

function onMouseMove(e) {
  if (state.drag?.mode === "pan") {
    const dx = e.clientX - state.drag.startX;
    const dy = e.clientY - state.drag.startY;
    state.pan.x = state.drag.panStart.x + dx;
    state.pan.y = state.drag.panStart.y + dy;
    renderCanvas();
    return;
  }
  if (state.drag?.mode === "move") {
    const dx = (e.clientX - state.drag.startX) / state.drag.zoom;
    const dy = (e.clientY - state.drag.startY) / state.drag.zoom;
    for (const [id, pos] of state.drag.startPositions) {
      const n = nodeById(id);
      if (n) {
        n.x = pos.x + dx;
        n.y = pos.y + dy;
      }
    }
    renderCanvas();
    return;
  }
  if (state.marquee) {
    const rect = canvasInner.getBoundingClientRect();
    const x = (e.clientX - rect.left) / state.zoom;
    const y = (e.clientY - rect.top) / state.zoom;
    const x0 = state.marquee.startX;
    const y0 = state.marquee.startY;
    const left = Math.min(x0, x);
    const top = Math.min(y0, y);
    const w = Math.abs(x - x0);
    const h = Math.abs(y - y0);
    const box = state.marquee.el;
    box.style.left = `${left}px`;
    box.style.top = `${top}px`;
    box.style.width = `${w}px`;
    box.style.height = `${h}px`;
  }
}

function onMouseUp() {
  if (state.marquee) {
    const box = state.marquee.el.getBoundingClientRect();
    const inner = canvasInner.getBoundingClientRect();
    const scale = state.zoom;
    const mx0 = (box.left - inner.left) / scale;
    const my0 = (box.top - inner.top) / scale;
    const mx1 = mx0 + box.width / scale;
    const my1 = my0 + box.height / scale;
    const hits = state.nodes
      .filter((n) => {
        const nx1 = n.x + n.w;
        const ny1 = n.y + n.h;
        return n.x < mx1 && nx1 > mx0 && n.y < my1 && ny1 > my0;
      })
      .map((n) => n.id);
    if (hits.length) setSelection(hits);
    state.marquee.el.remove();
    state.marquee = null;
  }
  state.drag = null;
}

function copySelection() {
  const nodes = selectedNodes();
  if (!nodes.length) return;
  state.clipboard = nodes.map((n) => JSON.parse(JSON.stringify(n)));
  status(`Copied ${nodes.length} item(s)`);
}

function pasteClipboard() {
  if (!state.clipboard?.length) {
    status("Nothing to paste");
    return;
  }
  const ids = duplicateNodes(state.clipboard, 24, 24);
  setSelection(ids);
  renderAll();
  status(`Pasted ${ids.length} item(s)`);
}

function addToolType(type) {
  const presets = {
    panel: { type: "panel", w: 180, h: 120, label: "Panel", fill: DEFAULT_PANEL_COLOR },
    block: { type: "block", w: 240, h: 80, label: "Content block", fill: DEFAULT_BLOCK_COLOR },
    text: { type: "text", w: 100, h: 32, label: "Label", fill: "transparent" },
    textfield: { type: "textfield", w: 200, h: 36, label: "Placeholder…", fill: "#fff" },
    linebreak: { type: "linebreak", w: 240, h: 16, label: "", fill: "transparent" },
  };
  const p = presets[type];
  if (!p) return;
  const node = createNode(p);
  setSelection([node.id]);
  renderAll();
}

function importLayout(layoutName) {
  const items = MENU_LAYOUTS[layoutName];
  if (!items) return;
  let x = 80;
  let y = 380;
  const gap = 10;
  const maxRow = 720;
  const newIds = [];
  let rowHeight = 0;

  const place = (node) => {
    newIds.push(node.id);
    rowHeight = Math.max(rowHeight, node.h);
  };

  for (const item of items) {
    if (item.kind === "linebreak") {
      x = 80;
      y += rowHeight + gap + 8;
      rowHeight = 0;
      const node = createNode({
        type: "linebreak",
        x: 80,
        y,
        w: item.w || maxRow - 80,
        h: item.h || 16,
        fill: "transparent",
      });
      place(node);
      y += (item.h || 16) + gap;
      continue;
    }

    let node;
    if (item.kind === "icon") {
      node = createNode({
        type: "icon",
        iconId: item.iconId,
        x,
        y,
        w: item.w,
        h: item.h,
        label: item.label || "",
        fill: item.color || DEFAULT_BLOCK_COLOR,
      });
    } else if (item.kind === "textfield") {
      node = createNode({
        type: "textfield",
        x,
        y,
        w: item.w,
        h: item.h,
        label: item.label,
        fill: "#fff",
      });
    } else if (item.kind === "text") {
      node = createNode({
        type: "text",
        x,
        y,
        w: item.w,
        h: item.h,
        label: item.label,
        fill: "transparent",
      });
    } else {
      node = createNode({
        type: "panel",
        x,
        y,
        w: item.w,
        h: item.h,
        label: item.label,
        fill: item.color || DEFAULT_PANEL_COLOR,
      });
    }

    if (x + node.w > maxRow && x > 80) {
      x = 80;
      y += rowHeight + gap;
      rowHeight = 0;
      node.x = x;
      node.y = y;
    }

    place(node);
    x += node.w + gap;
  }

  setSelection(newIds);
  renderAll();
  status(`Imported “${layoutName}”`);
}

function initImportDropdown() {
  if (!importLayoutSelect) return;
  importLayoutSelect.innerHTML = '<option value="">Import icons from layout…</option>';
  for (const name of Object.keys(MENU_LAYOUTS)) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    importLayoutSelect.appendChild(opt);
  }
  importLayoutSelect.addEventListener("change", () => {
    const v = importLayoutSelect.value;
    if (v) {
      importLayout(v);
      importLayoutSelect.value = "";
    }
  });
}

function initToolbar() {
  document.querySelectorAll("[data-add]").forEach((btn) => {
    btn.addEventListener("click", () => addToolType(btn.dataset.add));
  });
  document.getElementById("btn-group")?.addEventListener("click", groupSelection);
  document.getElementById("btn-ungroup")?.addEventListener("click", ungroupSelection);
  document.getElementById("btn-delete")?.addEventListener("click", deleteSelection);
  document.getElementById("btn-save")?.addEventListener("click", saveToStorage);
  document.getElementById("btn-export")?.addEventListener("click", () => {
    const blob = new Blob([serializeDoc()], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "layout-studio.json";
    a.click();
    URL.revokeObjectURL(a.href);
  });
  document.getElementById("icon-search")?.addEventListener("input", (e) => {
    renderIconLibrary(e.target.value);
  });
}

function onKeyDown(e) {
  const tag = e.target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;

  const mod = e.metaKey || e.ctrlKey;
  if (mod && e.key === "c") {
    e.preventDefault();
    copySelection();
  } else if (mod && e.key === "v") {
    e.preventDefault();
    pasteClipboard();
  } else if (mod && e.key === "d") {
    e.preventDefault();
    const ids = duplicateNodes(selectedNodes());
    setSelection(ids);
    renderAll();
  } else if (e.key === "Delete" || e.key === "Backspace") {
    deleteSelection();
  } else if (mod && e.key === "g" && !e.shiftKey) {
    e.preventDefault();
    groupSelection();
  } else if (mod && e.shiftKey && e.key === "G") {
    e.preventDefault();
    ungroupSelection();
  } else if (e.key === "l" || e.key === "L") {
    toggleLock();
  } else if (e.key === "]") {
    reorderZ(state.selection, 1);
  } else if (e.key === "[") {
    reorderZ(state.selection, -1);
  }
}

function seedDemo() {
  if (state.nodes.length) return;
  createNode({
    type: "panel",
    x: 40,
    y: 40,
    w: 320,
    h: 480,
    label: "Mobile frame",
    fill: "#f8fafc",
  });
  importLayout("Mobile bottom nav");
  status("Welcome — drag panels, ⌘C/⌘V, group with ⌘G");
}

canvas?.addEventListener("mousedown", onCanvasMouseDown);
window.addEventListener("mousemove", onMouseMove);
window.addEventListener("mouseup", onMouseUp);
window.addEventListener("keydown", onKeyDown);

canvas?.addEventListener(
  "wheel",
  (e) => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    state.zoom = Math.min(2.5, Math.max(0.4, state.zoom + (e.deltaY > 0 ? -0.05 : 0.05)));
    renderCanvas();
  },
  { passive: false }
);

initImportDropdown();
initToolbar();
renderIconLibrary();
loadFromStorage();
seedDemo();
renderAll();

export { state };
