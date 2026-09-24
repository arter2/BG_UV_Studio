# Layout Studio

Lightweight browser wireframe canvas (Figma / Canva–style parity for UI layout planning). No build step — open in a browser or serve statically.

## Quick start

```bash
cd layout-studio
python3 -m http.server 8765
```

Open http://localhost:8765

## Features

| Feature | How |
|--------|-----|
| Copy / paste | ⌘/Ctrl+C, ⌘/Ctrl+V (also duplicate with ⌘/Ctrl+D) |
| Import menu layouts | Top dropdown — paste icons/panels from preset menus into the canvas |
| Group / ungroup | Toolbar or ⌘G / ⌘⇧G |
| Lock | Inspector, **L**, or lock badge on object |
| Bring forward / back | Inspector or **]** / **[** |
| Panel colors | Inspector color picker |
| Line breaks | **Line break** tool |
| Text & blocks | **Text label**, **Text field**, **Block**, **Panel** |
| Icon library | Left sidebar — 50+ common UI icons by category |

Documents auto-save to `localStorage` via **Save**. **Export JSON** downloads the layout file.

## Note

The Claude artifact link could not be fetched from this environment (bot protection). This app implements the requested canvas workflow as a standalone project in this repo.
