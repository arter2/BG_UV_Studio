# UV Studio — Editable UX Mock

Open `index.html` in any browser (no build step).

## What you can edit

| Control | Effect |
|---|---|
| **Tab strip** | East (Maya default) / West / North |
| **UV Editor width** | Left/right split |
| **Density** | Comfortable vs compact rows |
| **Icon-dense buttons** | Show/hide glyphs on tool buttons |
| **Inline fields** | Keep quick fields on the row, or hide them (options-only feel) |
| **Pivot / Counters / Action bar / Recipe** | Toggle chrome on or off |
| **Palette** | Maya-base colors live |
| **Click any label** | Rename tools/sections (`contenteditable`) |
| **Option-box icons** | Opens floating **Apply / Accept / Close** window |
| **Export / Import JSON** | Hand layout decisions back to engineering |
| **UX notes** | Freeform decisions; Copy notes |

## Option box behavior (Maya)

- **Apply** — write settings, run tool, leave window open  
- **Accept** — tool-named primary; write, run, close  
- **Close** — discard edits since last Apply  

Settings persist in `localStorage` under `uvstudio.mock.design`.
