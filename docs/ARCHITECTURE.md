# UV Studio — Architecture, Risks, Refactoring

Senior-engineer read of the current single-file build (`BG_UV_Studio.py`) against the Long-Term Plan. Functionality described here is what exists today; package names below are the plan target.

## Architecture summary

UV Studio hosts Maya’s real UV Editor and layers studio tools on top: layout units, packing, pins/links, a replayable recipe, and (planned) textures that follow shells.

**Shipped shape today:** one generated file embedding seven logical modules in separate module objects (not one flattened namespace). Entry points: `uvstudio.show()` / `close()` / `self_test()`.

| Logical module | Job |
|---|---|
| M1 Host | Workspace control, hosted UV Editor, Qt/Maya insert, env probe |
| M2 Scene Bridge | Only Maya I/O: UV sets, shells, pins, units, undoable writes |
| M3 Packer | Pure Python: MaxRects, UDIM, align/orient, density, QC |
| M4 Recipe | Steps, snapshots, reorder/replay |
| M5 Registry | Tool definitions, prefs, selection guard, `ToolRunner` |
| M5b Handlers | Pack / transform / stack / pin / QC implementations |
| M5c Panel | Tabs, sections, option boxes, recipe UI, action bar |
| M9 Cluster Map | Interactive map (present; not the long-term left-pane switch yet) |

**Data flow**

```
Selection → SceneBridge.analyse → plain Shell/Unit data
         → Handler / Packer / Recipe step
         → UVWriter (cmds.polyEditUV in undo chunk)
         → optional Recipe append + UI status/counters
```

UI and packer never talk to Maya directly. Original UV set stays untouched; edits go to a working set (`uvStudio_work`).

**Plan target packages:** `compat/`, `core/`, `maya/`, `recipe/`, `texture/`, `ui/`, `build/` — same jobs, installable as `.mod` with single-file fallback from one source tree.

## Problem areas

1. **Monolith maintainability** — ~12k-line generated file is the only source in git. Header says “edit modules and rebuild,” but modules are not in the repo. Edits to the build are fragile and fight the plan’s bundler story.
2. **Duplicated concepts** — layout/align/rect helpers appear in both M3 (`layout_align`, `Rect`) and M5b/M9 paths; `Shell`/`LayoutUnit` vs `LUnit`/`MapUnit` overlap. Risk of divergent packing rules.
3. **Dual self-test / helper names** — multiple `run_self_test` and `_check` per module object (intentional), but easy to call the wrong one from the Script Editor.
4. **Unverified Maya flags** — Create/Unfold/Layout kwargs marked `verified=False`; wrong flag → TypeError on first press.
5. **UI density / discoverability** — many tools lacked tooltips; option-box workflow exists but needs consistent icons, action-bar access for Pack, and clearer Maya-style chrome.
6. **Dead / unreachable UI code** — e.g. duplicate body after `return` in `ToolRow._row_icons`.
7. **Embedding risk (plan M1)** — single `polyTexturePlacementPanel` per session; hosting vs native UV Editor focus/hotkeys remain the biggest product risk.
8. **Recipe fidelity** — Maya unfold/optimize may not replay identically; snapshots are required (plan M2) and only partially productized in UI.
9. **Missing plan packages** — `texture/`, map profiles, bake, preview material are not started (M3–M7).
10. **Bottlenecks** — dense-mesh extract is OM2 (good); writes are cmds (undo-correct, slower). Large UDIM bake will need M9 compiled backend; pure-Qt preview is fine for now.

## Refactoring strategy (functionality unchanged)

Order matches the Long-Term Plan; each step must keep `show()` / tools / undo behavior identical.

1. **Extract real packages from the bundle** — split embedded sources into `compat`, `maya` (M2), `core` (M3), `recipe` (M4), `ui` (M5/M5b/M5c/M9), keep `build/` emitting `BG_UV_Studio.py`. Stop editing the generated file as source of truth.
2. **UI presentation pass (this milestone)** — Maya toolkit chrome, tooltips, option-box Apply / Accept / Close, inline fields kept, full settings in floating window. No handler changes.
3. **Command verification matrix** — resolve every `verified=False` flag on 2022 and 2025; keep button labels stable (plan M1).
4. **Collapse duplicate layout math** — one rect/align/orient API in `core/`; UI and map import it.
5. **Recipe as first-class session** — every mutating tool records a step; topology barriers enforced in UI (already sketched in RecipePanel).
6. **Texture engine behind a narrow interface** — stub then Qt bake (M3–M5), then C++/GPU (M9) without touching UI/recipe.
7. **Shipping** — `.mod` + shelf/menu; single-file fallback from the same tree; docs/hotkeys at M10.

## UI decisions locked for the current pass

| Topic | Decision |
|---|---|
| Option-box Close | Label stays **Close** (Maya-honest after Apply) |
| Inline vs sheet | Keep inline quick fields; floating window = full settings |
| Visual base | Maya UV Toolkit palette already in `COL` / `PANEL_QSS` |
| Editable mock | Deferred until explicitly requested |

## Improved code (intent)

Consumer-visible work lands in M5c / tool registry:

- Fix dead `_row_icons` code
- Tooltips on every visible tool
- Option-box icon on every tool with settings; Pack reachable from action bar with options
- Stronger tab/section/action-bar/option-dialog QSS
- Version / `BUILD_STAMP` bump

Behavior of Apply / Accept / Close remains:

- **Apply** — write settings, run tool, leave window open  
- **Accept** — write settings, run tool, close window  
- **Close** — close without applying edits since last Apply  
