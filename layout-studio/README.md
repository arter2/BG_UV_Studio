# UV Studio — Layout Designer

Browser-based panel layout editor for **BG UV Studio**. Blocks match the real tool registry in `uvstudio_v5_1.py` (67 tools across Edit, Layout, Pack, Groups, Density, Checks, Debug).

## Quick start

```bash
cd layout-studio
python3 -m http.server 8765
```

Open http://localhost:8765

## Workflow

1. Pick a **panel tab** on the left — the canvas shows that tab’s sections and tool buttons plus shared viewport / action bar.
2. **Drag** blocks to move; drag the **bottom-right corner** to resize.
3. **Double-click** a label to rename in place.
4. Toggle **Snap to grid** and **Zoom** in the header.
5. Layout **autosaves** in your browser (`localStorage`).
6. Click **Export layout** and send the JSON — it can be turned into real Qt panel code.

## Selected item inspector

Label, X, Y, W, H (and color). **Load a file…** restores a prior export.

## Extra tools

Icons, lock, and z-order live under **Extra tools** in the left sidebar. ⌘/Ctrl+C and ⌘/Ctrl+V copy and paste blocks.

## Files

| File | Role |
|------|------|
| `js/uv-spec.json` | Tool/tab data extracted from UV Studio |
| `js/uv-designer.js` | Layout designer app |
| `js/icons.js` | Optional common UI icons |
