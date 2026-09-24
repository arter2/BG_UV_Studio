# UV Studio — Layout Designer

Official **single-file** layout editor from the Claude artifact. No build step.

## Quick start

```bash
cd layout-studio
python3 -m http.server 8765
```

Open http://localhost:8765 (serves `index.html`).

Same file is also saved as `UV-Studio-Layout-Designer.html` for download/sharing.

## Features (from the artifact)

- Full UV Studio tool registry (`TOOL_DEFS`) with tab/section layout per **Edit**, **Layout**, **Pack**, **Groups**, **Density**, **Checks**, **Debug**, **Recipe**, **Env**, **Cmds**, **Log**
- **Viewport**, **tools panel content area**, vertical **tab bar**, **action bar**, **pivot strip** (Layout tab)
- Snap to grid, zoom, light/dark/auto **theme**
- Drag, resize (corner handle), double-click rename, per-tab editing
- **Export layout** / **Import** modal with copy and **Load a file…**
- Autosave in `localStorage` (`uvstudio_layout_designer_v1`)
- **+ Icon/Tool**, **+ Section**, **+ Note**, **Clear tab**, **Reset all**

When the layout is final, export JSON and send it to turn into real Qt panel code.