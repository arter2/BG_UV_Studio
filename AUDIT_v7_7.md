# UV Studio v7.7 audit

Scope: `uvstudio_v7_7.py` (17.6k lines, 719 KB). Analysis was static plus a headless run (no Maya). Nothing was tested inside Maya.

## 1. Architecture

The file is a **generated bundle**. Its header says "edit the modules and rebuild", but the module sources are not in this repo. Nine module sources sit inside `r'''...'''` strings, and `_bootstrap()` `exec`s each into its own `types.ModuleType`.

| Module | Lines | Role |
|---|---|---|
| M2 scene_bridge | 2.4k | Only code that touches Maya: `CommandResolver`, `UVSetManager`, `Shell`, `ShellLink`, `UVWriter`, `SceneBridge`, `undo_chunk` |
| M3 packer_core | 1.5k | Pure-Python packing (MaxRects, UDIM, align, orient, QC). No Maya |
| M4 recipe | 0.8k | `Step`, `Recipe`, `Snapshot` (replay and undo history) |
| M6 texture | 0.9k | Texture transfer (`ShellWarp`, oiiotool/EXR) |
| M5 ui | 2.6k | Tool registry, `Preferences`, `SelectionGuard`, `ToolRunner` |
| M5b handlers | 2.9k | Tool implementations (pack, align, stack, pins, QC) |
| M5c panel | 3.5k | Qt widgets |
| M9 cluster_map | 1.7k | Interactive map view |
| M1 hosted_editor | 1.0k | Hosts the panel in Maya's UV Editor; Qt shim |

Data flow: Qt widget → `ToolRunner` (M5) → handler (M5b) → `SceneBridge` (M2) reads Maya into plain `Shell`/`LayoutUnit` data → `Packer` (M3) → results written back through `UVWriter` inside an `undo_chunk` → `Recipe` (M4) records the step.

The layering is good. M3, M4 and M6 have no Maya dependency, so they are testable headless.

`Session` wires the modules together by monkeypatching M1 at runtime (`m1.ToolsPanel = _panel_factory`, `m1.MapFactory`, `m1.ViewBarFactory`). The wiring lives in the bundle tail, not in the modules.

## 2. Problem areas (measured)

**Correctness and tooling**
1. **The bundle's self-tests don't work headless.**
   - `uvstudio.self_test()` gives `m2/m3: unavailable` and `m5b: FAIL`.
   - M2 does `import maya.cmds` at module scope, so bootstrap fails and M2 is popped.
   - M5b's test installs Maya stubs only after that, then can't find M2 ("M2 and M3 must sit beside this file").
   - It is a harness ordering bug. It says nothing about handler correctness.
2. **Docs don't match the code.**
   - The header says "7 modules" (there are 9).
   - It advertises `uvstudio.m3.run_self_test()`, but M3 defines none.
   - The bootstrap comment says "five define run_self_test", but there are seven.
3. **No source lines in tracebacks.** Modules are exec'd from `<uvstudio:name>` strings, so field bug reports show no source. This is fixed below.
4. **The source of truth is missing.** Only generated output is versioned. There are also three older full copies (`uvstudio_v4_7.py`, `v5_1.py`, `BG_UV_Studio.py`; about 1.5 MB combined) and no history-friendly diffs.

**Duplication**
- `_enum` is byte-identical in M1 and M5c (19 lines). The Qt compat shim should live in one place.
- `_NullChunk` is defined in both M5 and M5b, and M2 has the real `undo_chunk`. The three variants can drift.
- `_maya()` / `_sibling()` loader helpers are reimplemented per module.
- Each module carries its own `__version__` and `MODULE_ID`, plus a separate bundle version (`2.36.0`).

**Maintainability**
- Test code is 6 of the 25 longest functions. `_run_self_test_body` is 305 lines with complexity 45, and `run_self_test` in M5 is 263 lines. Tests are embedded in production modules, which is about 1.3k lines of shipped test code.
- Long UI methods:
  - `_build_cell`: 147 lines, complexity 35
  - `_paint_into`: 124 lines, complexity 37
  - Panel `__init__`s: 121, 95, 92 and 89 lines
  - `_smoke_on`: complexity 34
  - `transfer_texture`: complexity 32
- Error handling:
  - There are 155 `except Exception` handlers and 35 that are `except Exception: pass`.
  - Some are justified for Maya and Qt version differences. Others silently hide failures. There are no bare `except:`.
- Module-level global `_SESSION`, plus the runtime monkeypatching of M1 noted above.

**Bottlenecks (to profile in Maya; not measured here)**
- M3 `Packer` / `MultiPacker` are pure Python. That is fine for hundreds of shells, but MaxRects can go roughly O(n²) on very large counts.
- M9 `ClusterMap.load` and `_build_texture` are ones to check, along with M6 per-triangle warp fallback.
- M2 reads UVs per shell through `cmds`. Batching through `om2` / `polyEvaluate` is normally the win.

## 3. Refactoring strategy (ordered, each step behavior-neutral)

1. **Restore the source tree.** *Done (see section 4).*
2. **Fix the headless test entry.** *Done for M5b.* Still to do: give M3 (the packer, the most testable module) a `run_self_test`, or better, move the embedded tests into `tests/` and run them with pytest.
3. **Share duplicated helpers.** *Done for `_enum`*, using a build-time `# @include`. `_NullChunk` (8 trivial lines, two copies) is not worth sharing.
4. **Replace runtime monkeypatching** with constructor injection: `m1.show(panel_factory=..., map_factory=...)`.
5. **Split the large modules along seams that already exist.** M5c becomes `panel/{icons,sections,tool_row,recipe_panel}`. M5b becomes one file per tool family. `_build_cell` and `_paint_into` become small helpers. Each split needs an in-Maya smoke test, because the Qt code has no headless coverage.
6. **Tighten error handling.** Keep `except Exception` at Maya and Qt boundaries. Route the 35 `pass` handlers through one `_swallow(log, where)` helper so failures at least reach the log.
7. **Profile in Maya** (`cProfile` around pack, load and transfer) before optimizing anything in section 2's bottleneck list.
8. **Delete the old copies** (`uvstudio_v4_7.py`, `uvstudio_v5_1.py`, `BG_UV_Studio.py`); git history already keeps them. Not done here, pending your OK.

## 4. Improved code (applied)

**Source tree and build (steps 1 and 3).**
- `src/uvstudio/*.py` are the nine modules, extracted from the bundle.
- `tools/bundle_head.py.in` and `tools/bundle_tail.py.in` hold the loader and wiring.
- `tools/build.py` regenerates `uvstudio_v7_7.py`. `--check` fails if the bundle is out of date.
- The rebuild was verified byte-identical to the shipped bundle before any edit.
- `_enum` now lives once, in `src/uvstudio/_shared/qt_enum.py`. The build inlines it into M1 and M5c. A runtime import between modules would couple their load failures.

**Headless self test (step 2).**
- `self_test()` now loads M2 against stub `maya` modules just for M5b's test, then removes them.
- M5b's full handler suite now runs and passes: 0 failures, including the registration checks.
- A module that loaded but has no test (M3) now reports "no self test" instead of "unavailable".

**Traceback source lines.** `_register_source()` puts each embedded source into `linecache`.

**Docs.** The header now says 9 modules, lists M9, points to `m4.run_self_test()` (M3 has none) and gives the correct test-suite count.

**CI.** `.github/workflows/check.yml` runs `build.py --check` and the headless self tests on every push.

**Verification (headless, Python 3):**
- All nine embedded module sources are byte-identical before and after (`_SOURCES` compared), so the tool's runtime behavior is unchanged.
- Only the bundle header docstring and `self_test()` changed.
- `self_test()`: m4, m5, m5b and m6 PASS; m2 unavailable (needs Maya); m3 no self test.
- `sys.modules` and `status()` are unchanged after the test run.

Nothing was run inside Maya.

## Workflow from now on

Edit `src/uvstudio/...`, run `python tools/build.py`, then commit both the sources and the rebuilt bundle.
