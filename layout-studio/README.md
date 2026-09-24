# UV Studio — Layout Designer

Single-file layout editor for the UV Studio panel. No build step.

## Open it

Double-click `index.html` (or `UV-Studio-Layout-Designer.html`, the same file), or serve the folder:

```bash
cd layout-studio
python3 -m http.server 8765
```

and go to http://localhost:8765.

## What it does

Every tool button, tab, section, action button and the viewport starts out as a block from the UV Studio spec. Arrange each tab separately; the layout autosaves in the browser. **Export layout** produces the JSON to turn into panel code.

### Editing

| Feature | How |
|---|---|
| Copy / cut / paste / duplicate | Ctrl/⌘+C, X, V, D, or right-click. The clipboard carries across tabs and browser reloads. "Paste here" in the right-click menu drops at the cursor. |
| Add tools from other menus | **＋ Add from another menu…** lists every tool on every tab's current layout (and the action bar); pick one to place a copy on this tab. |
| Copy / move selection to a tab | **⇢ Send selection to tab…** |
| Multi-select | Shift-click, or drag a box on empty canvas. Dragging on an unselected viewport / content area / action bar also box-selects, so they don't get grabbed by accident. |
| Group / ungroup | Ctrl/⌘+G, Ctrl/⌘+Shift+G. Clicking one member selects the group; Ctrl/⌘-click selects a single member. |
| Lock | Ctrl/⌘+Shift+L, the Lock button, or the padlock in **Layers**. Locked items ignore clicks, which pass through to whatever is underneath. **Lock frames** locks the viewport, content area and action bar in one go. |
| Bring forward / send back | `]` / `[`, Ctrl/⌘+`]` / `[` for front/back, or the Arrange buttons. |
| Colors | Fill and text color pickers plus swatches in **Selected item**. |
| Text, text fields, blocks, line breaks | **+ Insert…** → Text label, Text field, Block, Line break, Vertical divider, Icon. |
| Common UI icons | **UI icons** panel: ~100 searchable icons. Click to add one as an icon block; Shift-click (or tick the checkbox) to set it on the selected item. |
| Layers | Stacking-order list with show/hide and lock per item. |
| Undo / redo | Ctrl/⌘+Z, Ctrl/⌘+Shift+Z (or Ctrl+Y). |
| Nudge | Arrow keys (Shift for bigger steps). |
| Zoom | Slider, or Ctrl/⌘+scroll. |

### Export format

The export is the full designer state. Items can carry these optional fields in addition to `x`, `y`, `w`, `h`, `label`, `icon`:

- `kind`: on free-form items in each tab's `shapes` list (`tool`, `section`, `note`, `text`, `textfield`, `block`, `divider`, `iconbox`, or a copied `actionbtn` / `tabbtn`)
- `fill`, `color`, `fontSize`: appearance overrides
- `z`: stacking order
- `group`: shared id for grouped items
- `locked`, `hidden`

Older exports without these fields still import.
