# UV Studio – Long-Term Plan

UV Studio – Long-Term Plan

Sep 17, 2026 · @Someone

Overview

UV Studio hosts Maya's real UV Editor inside our window and layers the Cluster Packer, a replayable layout history and texture-follows-shells baking on top. The first milestone is the hosted editor with Maya's UV tools mapped to our buttons.

Decision

Choice

Maya versions

2022 to 2025+ (PySide2 and PySide6)

Textures that follow shells

Any file texture on the material, with a rule per map

Dependencies

Pure Maya + Qt now; a compiled C++/GPU plugin later for advanced baking

Ideas to borrow

Blender (sync selection, island tools, live unwrap), Houdini (replayable history), C4D BodyPaint (paint-aware tools, projection preview)

Protecting originals

Original UV set stays untouched; all editing happens on a working copy

Bake output

New versioned files plus a duplicated material

First milestone

Hosted UV Editor + mapped Maya buttons

Up-res

Two options: resample with an external AI upscaler hook, or re-bake from high-poly

Shipping

Maya module (.mod) now, single-script fallback if needed, compiled plugin as the final form

Guiding principles

Maya does the editing, we add the layer on top. The real UV Editor is hosted; our tools add units, clustering, history and textures that move with their shells.

The original is never touched. Each mesh keeps its original UV set untouched, and all edits happen on a working copy. The texture engine always maps from working UVs back to original UVs.

Everything is a recipe. Every operation is a recorded step that can be disabled, reordered or replayed from the original.

Replaceable engines. The texture engine sits behind a narrow interface. Pure Qt ships first; a compiled C++/GPU version can replace it later without touching the rest.

Version-proof. One compatibility layer covers PySide2/PySide6 and version-specific details of Maya 2022 to 2025+.

Architecture

UV Studio ships as a Maya module (.mod) with a shelf and menu, split into packages with one job each.

Package

Holds

compat/

Qt shim (PySide2/6), Maya-version helpers

core/

Pure Python: packer, layout math, alignment, recipe model

maya/

Scene bridge: meshes, UV sets, pins, selection sync, commands

recipe/

History steps, replay, snapshots

texture/

Map discovery, map profiles, preview, bake (backend interface)

ui/

Main window, hosted UV Editor, tool strips, Cluster Map, panels

build/

Bundler that produces the single-file fallback from the same source

flowchart LR  UI[ui] --> Recipe[recipe]  UI --> Core[core]  Recipe --> Bridge[maya bridge]  Recipe --> Tex[texture]  Tex --> Backend[bake backend<br/>Qt now, C++/GPU later]  Bridge --> Maya[(Maya scene)]

The single-file fallback is generated from the same source, so a failed .mod install never means maintaining two copies.

Texture follows shells

Every face corner has an original UV and a working UV, so each triangle maps exactly from its new place back to its source pixels. That covers rigid moves as well as unfold, relax and sew.

Preview. Rigid units draw as clipped, transformed image patches; reshaped units draw triangle by triangle at preview resolution. A low-resolution preview texture is written to a temporary folder and shown in the Maya viewport on a preview material.

Final bake:

Plan the output resolution per UDIM from the target texel density.

Resample each triangle from the original texture into the new one.

Pad shell edges so borders don't bleed.

Write versioned files, e.g. sourceimages/uvstudio/<asset>/v003/<map>.<UDIM>.png.

Duplicate the material and repoint the copy at the new files; the original material stays as it is.

Map profiles (guessed from name and colorspace, editable per map):

Profile

Resampling

Extra handling

Color (sRGB)

Smooth, prefiltered when shrinking

None

Data (roughness, metal, AO)

Smooth, linear

None

Mask / ID

Nearest neighbour

No blending across edges

Tangent normal

Smooth, linear

X/Y rotated with the shell, flipped when mirrored

Vector / flow

Smooth, linear

Vectors rotated with the shell

Height / displacement

Smooth

Optional intensity change when a shell is scaled

Up-res options:

Resample, flag shells stretched beyond the source detail, with an optional hook for an external AI upscaler.

Re-bake from high-poly with Maya Transfer Maps / Arnold when a high-poly mesh exists.

Qt's smooth scaling is roughly bilinear: fine for previews and moderate resizing. High-quality up-res waits for the compiled backend (M9).

Hosted UV Editor and mapped tools

The left side switches between Maya UV Editor (the real panel) and Cluster Map (our map); the tools panel stays shared.

Group

Tools

Create

Planar, cylindrical, spherical, automatic, camera projections

Cut & Sew

Cut, sew, move and sew, split, merge

Unfold

Unfold (keeping pins), optimize, straighten UVs and borders

Arrange

Maya layout, our pack, stack/unstack, orient shells and edges, flip, rotate

Pin

Pin, unpin, invert pins

Density

Get, set and check texel density

Some Maya commands differ between versions, so M1 starts by checking each on 2022 and 2025; the buttons stay the same whatever runs underneath.

Borrowed from Blender:

Sync selection keeps the viewport, the hosted editor and our map in agreement.

Live unwrap re-unfolds a shell after pinned UVs move, with a short delay.

Island tools: select similar shells, match them, average their scale.

Milestones

M0 and M1 come first; each milestone ends with something testable in Maya.

#

Milestone

Delivers

Notes

M0

Skeleton

.mod install, shelf and menu, package split, Qt compatibility layer, bundler, test harness

Proves the install on 2022 and 2025

M1

Hosted editor

Maya UV Editor in our window, view switch, mapped tool strips, sync selection

First milestone; starts with an embedding feasibility test

M2

Working UV sets and recipe

Original/working UV sets, recipe panel (toggle, reorder, replay), snapshots for Maya operations

Replay applies stored results where Maya can't re-run identically

M3

Texture discovery

Material network, file nodes, <UDIM> sequences, colorspaces, map profile editor

M4

Preview

Textured Cluster Map, viewport preview material, detail-loss flags

M5

Bake v1

Per-triangle resampling, edge padding, resolution planner, versioned output + duplicated material

Pure Qt

M6

Map rules

Normal/vector rotation, masks, height intensity

M7

Up-res options

External upscaler hook, high-poly re-bake via Transfer Maps/Arnold

M8

Blender and BodyPaint extras

Live unwrap, island tools, texture-aware seam hints

M9

Compiled backend

C++ command for fast undoable UV writes, GPU/OIIO resampling, high-quality up-res

Final form

M10

Ship

Docs, presets, hotkey editor, version testing

Risks

The riskiest item is embedding Maya's panel, so M1 tests it before anything is built on it.

Risk

Impact

Mitigation

Embedding Maya's UV Editor panel

Focus, hotkeys and redraw differ between versions

Short test at the start of M1; fallback is docking our tools beside Maya's editor

Replaying Maya operations

Unfold/optimize may not give identical results twice

Recipe stores their results as snapshots

Pure-Qt engine speed

4K-8K UDIM bakes are slow

Correct but slow until the M9 compiled backend

Normal maps after unfold

Reshaped shells shift tangent directions slightly

Rigid moves are exact; reshaped shells get flagged for a high-poly re-bake

.mod install fails in a studio setup

Tool can't load

Generated single-script fallback from the same source
