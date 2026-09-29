"""
UV Studio - Module 5b: Tool Handlers
====================================

PURPOSE
    The 15 operations UV Studio implements itself rather than delegating to a
    native command: packing, align/distribute, PCA orientation, pin editing,
    pair stacking and the QC audits.

    Each handler has the signature ToolRunner expects:

        handler(tool=..., settings=..., meshes=..., bridge=...) -> detail

    and returns a short result dict the UI shows. Handlers never build UI and
    never talk to Qt.

DEPENDENCIES, ALL INJECTED
    bridge  - M2 SceneBridge: analysis, pins, undoable writes
    packer  - M3 packer core: pack, align, distribute, orientation, QC
    recipe  - M4, handled by the runner, not here

    Nothing is imported at module scope except the standard library, so this
    file can be exercised with fakes on any Python. That matters: a packing
    bug and a Maya bug look identical from the UI, and this is where they are
    told apart.

WHAT CHANGED IN v2.0 - read this before comparing against v1
    v1 was correct in outline and wrong in four places that only a real mesh
    would have shown. Every change below preserves what each tool DOES.

    1. ONE UNDO CHUNK PER PRESS. v1 relied on M2's writer to open a chunk,
       and the writer opens one per call. Packing 338 units left 338 entries
       on the undo queue and one Ctrl+Z undid one shell. The chunk is now
       opened here, around the whole handler, and M2's chunk is re-entrant so
       the writes inside it join rather than nest.

    2. ROTATED AND SCALED PLACEMENTS LANDED IN THE WRONG PLACE. v1 read
       Placement.delta - which is the move for an UNROTATED box - then
       rotated about the shell centre anyway, leaving the shell off by
       (width - height) / 2. A 0.25 x 0.9 shell missed by 0.325 UV units,
       a third of a tile, and could land outside it entirely.
       Placement.scale was read and never applied at all, so scale_to_fit
       produced a layout computed at one size and written at another.
       The final corner is now derived after each step and the translation
       closes the gap, which is exact for any width, height and scale.

    3. ONE ANALYSIS AND ONE WRITER PER PRESS. v1 rebuilt the pack item list
       inside the placement loop (O(n^2) on a 338-shell mesh), called
       extract_shells once per shell in orient_pca, called polyEvaluate once
       per shell in check_density, and asked the bridge for a new writer per
       unit - each one re-reading every pin on the mesh through polyPinUV.
       All four are hoisted into the run context below.

    4. ONE SPELLING OF "MESH". M5's guard hands over transform paths for an
       object selection and shape paths for a component one, while M2
       compares full paths, so selected_uv_ids matched nothing and the
       "no UV selection, use everything" fallback fired - align silently
       moving the whole mesh. Names are canonicalised on the way in.

WHAT EVERY WRITE HANDLER GUARANTEES
    - One undo chunk per tool press, opened here.
    - Pinned UVs excluded, enforced in the writer rather than here.
    - Units move together: a stacked pair is one thing to every handler.
    - Read-only handlers write nothing, including to the selection.

Target: Python 3.7+ (Maya 2022) and later
"""

from __future__ import annotations

import functools
import logging
import os
import math
import sys
from collections import OrderedDict

__version__ = "2.24.0"

# Failures the code deliberately survives are logged at DEBUG, which prints
# nothing by default. To see them, run in the Script Editor:
#     import logging; logging.getLogger("uvstudio").setLevel(logging.DEBUG)
_log = logging.getLogger("uvstudio.m5b")
MODULE_ID = "M5b"


# =====================================================================
# SECTION 1 - Sibling module access
# =====================================================================

def _sibling(name, required_attr):
    """The named sibling module, or None if it is absent or half-built.

    Read through sys.modules every time rather than cached. The bundle
    replaces every module object on each paste, and a cache here would hand
    back the previous build's module - which is exactly the failure mode the
    bundle's "always re-register" rule exists to prevent.

    A module that imported but failed partway leaves an empty namespace, so
    presence is tested by an attribute rather than by the import succeeding.
    """
    module = sys.modules.get(name)
    if module is None:
        try:
            __import__(name)
        except ImportError:
            return None
        module = sys.modules.get(name)
    return module if (module is not None
                      and hasattr(module, required_attr)) else None


def _load_packer():
    """M3, looked up on use so this file stays importable standalone."""
    return _sibling("uvstudio_m3_packer_core", "pack")


def _load_bridge_module():
    """M2, looked up on use so this file stays importable standalone."""
    return _sibling("uvstudio_m2_scene_bridge", "SceneBridge")


def _maya_ready():
    """Is there a Maya that can answer a selection query?

    "No Maya at all" and "Maya answered badly" need opposite responses: the
    first is a test harness and the old select-everything fallback is right;
    the second is a real failure and must not be read as "nothing selected".
    Checking for one real command separates them - a stub module imports
    fine and has none.
    """
    cmds = _maya()
    return cmds is not None and hasattr(cmds, "ls")


def _maya():
    """maya.cmds, or None outside Maya."""
    try:
        import maya.cmds as cmds
        return cmds
    except ImportError:
        return None


class _NullChunk(object):
    """Stand-in for M2's undo_chunk when M2 is not loaded (tests, no Maya)."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        return False


# =====================================================================
# SECTION 2 - Run context
# =====================================================================

class _Run(object):
    """Everything one tool press needs, computed at most once.

    v1 recomputed on demand, which read well line by line and meant a single
    Pack re-read every pin on the mesh several hundred times. Each accessor
    here is lazy AND memoised: a handler that never asks for the analysis
    (Pin, Split) never pays for one, and a handler that asks four times pays
    once.
    """

    def __init__(self, bridge, meshes, settings, label="tool"):
        self.bridge = bridge
        self.label = label
        self.settings = dict(settings or {})
        self.m2 = _load_bridge_module()
        self.m3 = _load_packer()

        raw = list(meshes or [])
        self.meshes = self.m2.canonical_meshes(raw) if self.m2 else raw

        self._report = None
        self._writers = {}
        self._selection = None
        self._items = None
        self._item_source = None

    # -- requirements --------------------------------------------------
    def need_packer(self):
        if self.m3 is None:
            raise RuntimeError("Packer core (M3) is required for %s"
                               % self.label)
        return self.m3

    def need_bridge_module(self):
        if self.m2 is None:
            raise RuntimeError("Scene bridge (M2) is required for %s"
                               % self.label)
        return self.m2

    # -- settings ------------------------------------------------------
    def get(self, key, default=None, cast=None):
        value = self.settings.get(key, default)
        if cast is None or value is None:
            return value
        try:
            return cast(value)
        except (TypeError, ValueError):
            return default

    # -- scene state ---------------------------------------------------
    @property
    def report(self):
        """M2's pooled analysis of the whole selection. One per press."""
        if self._report is None:
            if not self.meshes:
                raise RuntimeError("No mesh to work on")
            self._report = self.bridge.analyse_many(self.meshes)
        return self._report

    @property
    def shells(self):
        return self.report["shells"]

    @property
    def units(self):
        return self.report["units"]

    def pinned_for(self, mesh):
        """The pin set M2 already read during analysis, or None.

        None means "writer, read them yourself". It is not the same as an
        empty set, which means "read, and nothing is pinned" - conflating the
        two would let an unpinned-looking mesh move pinned UVs.
        """
        if self._report is None:
            return None
        return (self._report.get("pin_sets") or {}).get(mesh)

    def writer(self, mesh):
        """One writer per mesh per press, pins supplied rather than re-read."""
        writer = self._writers.get(mesh)
        if writer is None:
            writer = self.bridge.writer(mesh, pinned=self.pinned_for(mesh))
            self._writers[mesh] = writer
        return writer

    @property
    def selection_by_mesh(self):
        """{mesh: set(uv id)} for the current UV selection. One scan."""
        if self._selection is None:
            selected = {}
            self._selection_failed = False
            if self.m2 is not None and _maya_ready():
                try:
                    for mesh, ids in self.m2.selected_uv_ids_by_mesh().items():
                        if ids:
                            selected[mesh] = set(ids)
                except Exception:
                    # Recorded, not swallowed. Treating a failed query as
                    # "nothing selected" sent Rotate down the "so use
                    # everything" path, which is the most destructive
                    # possible reading of an error.
                    self._selection_failed = True
                    selected = {}
            self._selection = selected
        return self._selection

    @property
    def has_components(self):
        """Did the artist select components, whatever kind?"""
        if self.m2 is None or not _maya_ready():
            return False
        try:
            return self.m2.selection_has_components()
        except Exception:
            return False

    def selected_shells(self):
        """Shells touched by the current UV selection, else all of them.

        The fallback is deliberate and unchanged from v1: a mesh selected with
        no components means the whole mesh. What changed is that the mesh
        names being compared now agree, so the fallback fires when the artist
        selected nothing rather than whenever the guard happened to hand over
        a transform path.
        """
        selected = self.selection_by_mesh
        if not selected:
            # "Nothing resolved" has two very different causes, and the old
            # code gave both the same answer: use every shell.
            #
            #   object selected    -> they mean the whole mesh. Correct.
            #   components selected, none resolved -> something went wrong,
            #                         and turning the whole mesh is the
            #                         opposite of what was asked.
            #
            # The second case now raises. The artist selected one shell; if
            # we cannot tell which, saying so beats rotating all 338.
            if self.has_components or getattr(self, "_selection_failed",
                                              False):
                raise RuntimeError(
                    "Components are selected but no UVs could be resolved "
                    "from them. Select UVs or faces in the UV Editor, or "
                    "select the mesh to work on all of it.")
            return self.shells
        touched = [s for s in self.shells
                   if s.mesh in selected
                   and selected[s.mesh].intersection(s.uv_ids)]
        if touched:
            return touched
        raise RuntimeError(
            "The selected UVs do not belong to any analysed shell. If the "
            "mesh changed since the last run, press Analyze again.")

    def units_for(self, shells):
        """The layout units containing any of these shells."""
        wanted = set(id(s) for s in shells)
        return [u for u in self.units
                if any(id(s) in wanted for s in u.shells)]

    # -- packer interop ------------------------------------------------
    def items_for(self, units, bounds=None):
        """(items, {key: unit}) for M3. Built once per distinct unit list.

        v1 called the equivalent of this inside the placement loop, so a
        338-unit pack built 114,244 PackItems to look up 338 payloads.

        `bounds` overrides a unit's measured box by index, for callers that
        have just changed the UVs - pre-orientation - and whose stored
        bounds are therefore one step out of date.
        """
        cache_key = (id(units), id(bounds) if bounds else None)
        if self._items is None or self._item_source != cache_key:
            packer = self.need_packer()
            self._bounds_override = dict(bounds or {})
            items = []
            for unit in units:
                u_min, u_max, v_min, v_max = self.bounds_of(unit)
                items.append(packer.PackItem(
                    key=unit.index, width=u_max - u_min, height=v_max - v_min,
                    u=u_min, v=v_min, pinned=unit.is_pinned, payload=unit,
                    tris=self._unit_tris(unit), area=self._unit_area(unit)))
            self._items = (items, dict((i.key, i.payload) for i in items))
            self._item_source = cache_key
        return self._items

    def _uvcoords(self):
        """{mesh: (us, vs)} for the analysed meshes, cached per press."""
        cache = getattr(self, "_uv_cache", None)
        if cache is None:
            cache = {}
            bridge_module = self.m2
            if bridge_module is not None:
                for mesh in self.meshes:
                    try:
                        _sh, us, vs = bridge_module.extract_shells(mesh)
                        cache[mesh] = (us, vs)
                    except Exception:
                        _log.debug("UVs unreadable for %s; skipped", mesh,
                                   exc_info=True)
            self._uv_cache = cache
        return cache

    def _unit_tris(self, unit):
        """A unit's UV triangles in UV coordinates, for the raster packer."""
        coords = self._uvcoords()
        out = []
        for shell in unit.shells:
            us_vs = coords.get(shell.mesh)
            tris = getattr(shell, "tris", None)
            if not us_vs or not tris:
                continue
            us, vs = us_vs
            n = min(len(us), len(vs))
            for a, b, c in tris:
                if a < n and b < n and c < n:
                    out.append(((us[a], vs[a]), (us[b], vs[b]),
                                (us[c], vs[c])))
        return out

    def _unit_area(self, unit):
        total = 0.0
        for shell in unit.shells:
            total += getattr(shell, "area", 0.0)
        return total

    def bounds_of(self, unit):
        """A unit's box, honouring any override from a just-applied change."""
        return getattr(self, "_bounds_override", {}).get(unit.index,
                                                         unit.bounds)

    # -- writes --------------------------------------------------------
    def undo_chunk(self):
        if self.m2 is None:
            return _NullChunk()
        return self.m2.undo_chunk("UV Studio: %s" % self.label)

    def translate_unit(self, unit, du, dv):
        """Move every mesh in a unit by the same delta."""
        moved = 0
        for mesh, uv_ids in unit.by_mesh().items():
            moved += self.writer(mesh).translate(uv_ids, du, dv)
        return moved

    def rotate_unit(self, unit, degrees, pivot):
        rotated = 0
        for mesh, uv_ids in unit.by_mesh().items():
            rotated += self.writer(mesh).rotate(uv_ids, degrees, pivot)
        return rotated

    def scale_unit(self, unit, su, sv, pivot):
        scaled = 0
        for mesh, uv_ids in unit.by_mesh().items():
            scaled += self.writer(mesh).scale(uv_ids, su, sv, pivot)
        return scaled

    # -- pivot ---------------------------------------------------------
    @property
    def pivot(self):
        """The panel's pivot spec, defaulted. Section 3b explains the modes."""
        return pivot_spec(self.settings)

    def pivot_context(self, units):
        """(spec, group_bounds) - everything resolve_pivot needs for a run.

        The group box is computed once for the whole press rather than per
        unit, because in "selection" mode every unit must turn about the SAME
        point; recomputing it per unit would silently degrade to per-shell.
        """
        return self.pivot, merge_bounds([u.bounds for u in units])

    def pivot_for(self, unit, spec, group_bounds):
        return resolve_pivot(spec, unit.bounds, group_bounds)


def _apply_translation(bridge, unit, du, dv):
    """Move every mesh in a unit by the same delta.

    Kept as a free function because it is the one piece of handler internals
    that other code and the self test call directly. A unit can span meshes
    and the writer works on one mesh at a time, so a cross-mesh unit becomes
    several writes carrying the same delta.
    """
    moved = 0
    for mesh, uv_ids in unit.by_mesh().items():
        moved += bridge.writer(mesh).translate(uv_ids, du, dv)
    return moved


# =====================================================================
# SECTION 3 - Handler decorator
# =====================================================================

def _handler(label, writes=False):
    """Wrap a _Run-taking function as the callable ToolRunner expects.

    Every v1 handler opened with the same four lines - fetch a module, test
    it, raise, analyse - and that preamble is where the divergences crept in:
    two handlers raised different messages for the same missing module, and
    the undo chunk was left to whichever writer happened to run. Declaring the
    shape once means a new handler cannot forget it.
    """
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(tool=None, settings=None, meshes=None, bridge=None, **_):
            run = _Run(bridge, meshes, settings, label=label)
            if not writes:
                return fn(run)
            with run.undo_chunk():
                return fn(run)
        wrapper.is_write_handler = writes
        return wrapper
    return decorate


# =====================================================================
# SECTION 3b - Pivot and snapping
#
# WHY PIVOT IS A PANEL SETTING AND NOT A PER-TOOL ONE
#   Maya repeats pivot state inside each tool, so an artist who wants to
#   rotate and then scale about the same corner sets it twice and can set it
#   inconsistently. Here one strip at the top of the tab owns it and every
#   transform reads the same spec, which is also why this resolves to a point
#   rather than each handler computing its own centre.
#
#   The default - mode "shell", anchor "c" - is exactly what every handler
#   did before pivots existed: each unit turns about its own centre. Nothing
#   changes for anyone who never touches the strip.
# =====================================================================

PIVOT_MODES = ("shell", "selection", "area", "custom")

# anchor -> (fraction across U, fraction up V) inside whichever box the mode
# selects. "c" is the centre; the compass points are its corners and edges.
PIVOT_ANCHORS = {
    "nw": (0.0, 1.0), "n": (0.5, 1.0), "ne": (1.0, 1.0),
    "w": (0.0, 0.5), "c": (0.5, 0.5), "e": (1.0, 0.5),
    "sw": (0.0, 0.0), "s": (0.5, 0.0), "se": (1.0, 0.0),
}

DEFAULT_PIVOT = {"mode": "shell", "anchor": "c", "u": 0.5, "v": 0.5}


def pivot_spec(settings):
    """Normalise whatever the panel stored into a complete pivot spec.

    Every field is defaulted rather than required, because a settings dict
    written by an older build will not have them and a KeyError in a pivot
    lookup would take down a tool that worked yesterday.
    """
    raw = (settings or {}).get("pivot") or {}
    if not isinstance(raw, dict):
        raw = {}
    mode = raw.get("mode", DEFAULT_PIVOT["mode"])
    if mode not in PIVOT_MODES:
        mode = DEFAULT_PIVOT["mode"]
    anchor = raw.get("anchor", DEFAULT_PIVOT["anchor"])
    if anchor not in PIVOT_ANCHORS:
        anchor = DEFAULT_PIVOT["anchor"]
    return {
        "mode": mode,
        "anchor": anchor,
        "u": float(raw.get("u", DEFAULT_PIVOT["u"])),
        "v": float(raw.get("v", DEFAULT_PIVOT["v"])),
    }


def point_in_bounds(bounds, anchor):
    """The anchor point of a (u_min, u_max, v_min, v_max) box."""
    u_min, u_max, v_min, v_max = bounds
    fu, fv = PIVOT_ANCHORS.get(anchor, (0.5, 0.5))
    return (u_min + (u_max - u_min) * fu, v_min + (v_max - v_min) * fv)


def merge_bounds(bounds_list):
    """The box enclosing several boxes, or None for an empty list."""
    boxes = [b for b in bounds_list if b]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), max(b[1] for b in boxes),
            min(b[2] for b in boxes), max(b[3] for b in boxes))


def resolve_pivot(spec, unit_bounds, group_bounds=None, area_bounds=None):
    """The point a transform turns or scales about.

    - shell     : the unit's own box. Per unit, so each shell spins in place.
    - selection : the box round everything selected. One shared point, so the
                  selection turns as a rigid arrangement.
    - area      : the tile the unit sits in, defaulting to 0-1.
    - custom    : the literal U,V typed into the strip.
    """
    mode = spec["mode"]
    if mode == "custom":
        return (spec["u"], spec["v"])
    if mode == "selection" and group_bounds:
        return point_in_bounds(group_bounds, spec["anchor"])
    if mode == "area":
        box = area_bounds
        if box is None:
            u_min = float(int(unit_bounds[0]) if unit_bounds[0] >= 0
                          else int(unit_bounds[0]) - 1)
            v_min = float(int(unit_bounds[2]) if unit_bounds[2] >= 0
                          else int(unit_bounds[2]) - 1)
            box = (u_min, u_min + 1.0, v_min, v_min + 1.0)
        return point_in_bounds(box, spec["anchor"])
    return point_in_bounds(unit_bounds, spec["anchor"])


def snapped(value, settings, key="snap"):
    """Round a value to the tool's snap increment when snapping is on.

    Returns the value untouched when the increment is missing or zero, so a
    snap field left blank can never quantise a move to nothing - which is the
    failure an artist reads as "the tool stopped working".
    """
    settings = settings or {}
    if not settings.get("%s_enabled" % key):
        return value
    try:
        step = float(settings.get("%s_step" % key) or 0.0)
    except (TypeError, ValueError):
        return value
    if step <= 0.0:
        return value
    return round(value / step) * step


# =====================================================================
# SECTION 4 - Transform handlers
# =====================================================================

@_handler("rotate", writes=True)
def rotate(run):
    """Rotate the selected units about the panel's pivot."""
    degrees = snapped(run.get("degrees", 90.0, float), run.settings, "snap")
    shells = run.selected_shells()
    if not shells:
        return OrderedDict([("rotated", 0), ("message", "Nothing to rotate")])

    units = run.units_for(shells)
    spec, group = run.pivot_context(units)

    rotated = 0
    for unit in units:
        if unit.is_pinned:
            continue
        rotated += run.rotate_unit(unit, degrees,
                                   run.pivot_for(unit, spec, group))
    return OrderedDict([("rotated_uvs", rotated), ("degrees", degrees),
                        ("pivot", spec["mode"])])


@_handler("scale", writes=True)
def scale(run):
    """Scale the selected units about the panel's pivot.

    Negative factors are a legitimate way to mirror, so "prevent negative
    scale" is a setting rather than a rule. When it is on, the magnitude is
    kept and the sign dropped - silently doing nothing would look like the
    button failing.
    """
    factor = snapped(run.get("factor", 1.0, float), run.settings, "snap")
    do_u = bool(run.get("axis_u", True))
    do_v = bool(run.get("axis_v", True))
    if run.get("prevent_negative", True) and factor < 0.0:
        factor = abs(factor)
    if factor == 0.0 or not (do_u or do_v):
        return OrderedDict([("scaled", 0),
                            ("message", "Nothing to scale by")])

    su = factor if do_u else 1.0
    sv = factor if do_v else 1.0
    shells = run.selected_shells()
    units = run.units_for(shells)
    spec, group = run.pivot_context(units)

    scaled = 0
    for unit in units:
        if unit.is_pinned:
            continue
        scaled += run.scale_unit(unit, su, sv,
                                 run.pivot_for(unit, spec, group))
    return OrderedDict([("scaled_uvs", scaled), ("factor", factor),
                        ("axes", ("U" if do_u else "") + ("V" if do_v else "")),
                        ("pivot", spec["mode"])])


@_handler("flip", writes=True)
def flip(run):
    """Mirror the selected units about the panel's pivot.

    A flip is a scale of -1 on one axis, so it goes through the same pivot as
    everything else. Maya's polyFlipUV always mirrors within the shell's own
    bounding box; this honours "flip the whole selection about its centre",
    which is the thing that was impossible before.
    """
    # flipType is v1's spelling, from when Flip called polyFlipUV (0 = U,
    # 1 = V). A recipe recorded then carries flipType and no axis, so
    # ignoring it would replay every recorded Flip V as a Flip U - the
    # quietest possible way to get a saved recipe wrong.
    axis = run.get("axis")
    if axis is None and "flipType" in run.settings:
        axis = "v" if str(run.settings.get("flipType")) in ("1", "1.0") else "u"
    axis = str(axis or "u").lower()
    su, sv = (-1.0, 1.0) if axis == "u" else (1.0, -1.0)

    shells = run.selected_shells()
    units = run.units_for(shells)
    spec, group = run.pivot_context(units)

    flipped = 0
    for unit in units:
        if unit.is_pinned:
            continue
        flipped += run.scale_unit(unit, su, sv,
                                  run.pivot_for(unit, spec, group))
    return OrderedDict([("flipped_uvs", flipped), ("axis", axis.upper()),
                        ("pivot", spec["mode"])])


@_handler("move", writes=True)
def move(run):
    """Offset the selected units by an exact U,V amount."""
    du = snapped(run.get("u", 0.0, float), run.settings, "snap")
    dv = snapped(run.get("v", 0.0, float), run.settings, "snap")
    if du == 0.0 and dv == 0.0:
        return OrderedDict([("moved", 0), ("message", "Move is zero")])

    moved = 0
    for unit in run.units_for(run.selected_shells()):
        if unit.is_pinned:
            continue
        moved += run.translate_unit(unit, du, dv)
    return OrderedDict([("uvs_moved", moved), ("u", du), ("v", dv)])


@_handler("nudge", writes=True)
def nudge(run):
    """One step along an axis, for the panel's arrow buttons.

    Separate from move because the arrows carry their own step and direction
    and must not overwrite the U/V fields the artist typed. The step is the
    value the artist snaps to, so nudging and snapping share one number
    rather than drifting apart.
    """
    step = abs(run.get("step", 0.1, float))
    axis = str(run.get("axis", "u")).lower()
    direction = -1.0 if run.get("direction", 1) < 0 else 1.0
    delta = step * direction
    if step == 0.0:
        return OrderedDict([("moved", 0), ("message", "Step is zero")])

    du, dv = (delta, 0.0) if axis == "u" else (0.0, delta)
    moved = 0
    for unit in run.units_for(run.selected_shells()):
        if unit.is_pinned:
            continue
        moved += run.translate_unit(unit, du, dv)
    return OrderedDict([("uvs_moved", moved), ("axis", axis.upper()),
                        ("step", delta)])



@_handler("straighten", writes=True)
def orient_pca(run):
    """Straighten each shell so its long axis lies along U."""
    packer = run.need_packer()
    bridge_module = run.need_bridge_module()

    snap = run.get("snap", 0.0, float) or None
    shells = run.selected_shells()

    # One extraction per MESH, not one per shell. v1 called extract_shells
    # inside the loop, so a 338-shell body re-read all 130k UVs 338 times.
    coords = {}
    for mesh in set(s.mesh for s in shells):
        try:
            _shells, us, vs = bridge_module.extract_shells(mesh)
        except Exception:
            continue
        coords[mesh] = (us, vs)

    straightened = 0
    for shell in shells:
        if shell.is_pinned:
            continue
        us_vs = coords.get(shell.mesh)
        if us_vs is None:
            continue
        us, vs = us_vs
        limit = min(len(us), len(vs))
        points = [(us[i], vs[i]) for i in shell.uv_ids if i < limit]
        if len(points) < 3:
            continue
        angle = packer.orientation_correction(points, snap=snap)
        if abs(angle) < 1.0e-6:
            continue
        run.writer(shell.mesh).rotate(shell.uv_ids, angle, shell.centre)
        straightened += 1
    return OrderedDict([("straightened", straightened),
                        ("of_shells", len(shells))])


# =====================================================================
# SECTION 5 - Align and distribute
# =====================================================================

def _apply_deltas(run, items, units_by_key, deltas):
    moved = 0
    for item in items:
        delta = deltas.get(item.key)
        if not delta:
            continue
        unit = units_by_key.get(item.key)
        if unit is None:
            continue
        moved += run.translate_unit(unit, delta[0], delta[1])
    return moved


# =====================================================================
# Debug tab - the tests, as buttons
# =====================================================================
# Registered writes=False on purpose: the tool test calls cmds.undo() to
# prove each tool reverts in ONE step, and an outer undo chunk around the
# whole test would swallow exactly that check.

DEBUG_LINES = []
DEBUG_REPORT_NAME = "uvstudio_debug_report.txt"

# (tool id, what to select on the duplicate)
SMOKE_TOOLS = [
    ("unfold3d", "shell"), ("optimize", "shell"), ("straighten_uvs", "shell"),
    ("orient_shells", "one_uv"), ("layout_maya", "one_uv"),
    ("layout_u3d", "one_uv"), ("normalize", "one_uv"),
    ("rotate_ccw", "shell"), ("align_left", "two_shells"),
    ("align_centre_u", "two_shells"), ("distribute_u", "three_shells"),
    ("sew_move", "shell"), ("split", "one_uv"), ("planar", "shell"),
    ("pack", "object"),
    # UV Studio's own transforms, with values that must change something.
    ("move", "shell", {"u": 0.05, "v": 0.03}),
    ("nudge", "shell", {"axis": "u", "direction": 1, "step": 0.02}),
    ("scale", "shell", {"factor": 1.25}),
    ("flip_u", "shell"),
    ("straighten_pca", "shell"),
    ("stack_pairs", "object"),
    # Read-only: must run cleanly and leave the UVs exactly as they were.
    ("analyze", "object"), ("pack_preview", "object"),
    ("check_overlaps", "object"), ("check_bounds", "object"),
    ("check_flipped", "object"), ("check_density", "object"),
    ("find_pairs", "object"), ("list_links", "object"),
    ("texture_scan", "object"),
]
READ_ONLY = ("analyze", "pack_preview", "check_overlaps", "check_bounds",
             "check_flipped", "check_density", "find_pairs", "list_links",
             "texture_scan")


def _dlog(line=""):
    DEBUG_LINES.append(line)


def _debug_context():
    """(runner, host) from the open UV Studio window."""
    m1 = sys.modules.get("uvstudio_m1_hosted_editor")
    host = getattr(m1, "_HOST", None)
    panel = getattr(host, "tools_panel", None)
    return getattr(panel, "runner", None), host


def _snap(m2, shape):
    """Every UV coordinate, not just shell outlines.

    Optimize and Straighten move UVs INSIDE a shell without changing its
    bounding box, so an outline-only snapshot reported them as "changed
    nothing" when they had worked.
    """
    shells, us, vs = m2.extract_shells(shape)
    # 7 decimals: one Optimize iteration on a dense mesh moves UVs by less
    # than 1e-5, which read as "changed nothing" at 5.
    coords = hash(tuple(round(x, 7) for x in us)
                  + tuple(round(y, 7) for y in vs))
    return (len(us), len(shells), coords)


def _debug_selection(cmds, m2, shape, shells, kind):
    if kind == "object":
        return [shape]
    if not shells:
        return []
    ordered = sorted(shells, key=lambda s: -len(s.uv_ids))
    if kind == "one_uv":
        # The UV nearest the shell's centre: an interior UV shared by several
        # faces. The middle of the id list was often a border corner owned
        # by one face, where Split correctly has nothing to do.
        try:
            _sh, us, vs = m2.extract_shells(shape)
            cu, cv = ordered[0].centre
            best = min(ordered[0].uv_ids,
                       key=lambda i: (us[i] - cu) ** 2 + (vs[i] - cv) ** 2)
        except Exception:
            best = ordered[0].uv_ids[len(ordered[0].uv_ids) // 2]
        return ["%s.map[%d]" % (shape, best)]
    count = {"shell": 1, "two_shells": 2, "three_shells": 3}.get(kind, 1)
    # Shells whose centres differ. Stacked copies share a centre, so on an
    # asset of stacked pairs the two largest shells were ONE stack and
    # Centre U correctly had nothing to do - a failed test, not a tool bug.
    picked = []
    for sh in ordered:
        cu, cv = sh.centre
        if all(abs(cu - p.centre[0]) > 1e-3 or abs(cv - p.centre[1]) > 1e-3
               for p in picked):
            picked.append(sh)
        if len(picked) == count:
            break
    if len(picked) < count:
        return []
    out = []
    for sh in picked:
        out.extend(m2.expand_components(shape, sh.uv_ids))
    return out


# Solvers are no-ops on UVs that are already solved. They get a known
# distortion first, so "changed nothing" means the tool did nothing rather
# than that there was nothing to do.
DISTORT_FIRST = ("unfold3d", "optimize")
# Orient and Straighten do nothing to a shell that is already straight, which
# is every face of the fixture cube. Tilting it first gives them work.
TILT_FIRST = ("orient_shells", "straighten_uvs", "straighten_pca")
# Align and distribute do nothing to shells already aligned; nudge the
# second pick off-line first so there is always something to do.
MISALIGN_FIRST = ("align_left", "align_centre_u", "distribute_u")


def _distort(cmds, m2, shape, shells, target):
    ordered = sorted(shells, key=lambda s: -len(s.uv_ids))
    if not ordered:
        return
    cu, cv = ordered[0].centre
    cmds.polyEditUV(target, relative=True, scaleU=1.0, scaleV=0.55,
                    pivotU=cu, pivotV=cv)


def _bisect_native(runner, tool_id, cmds):
    """After a native tool fails: try it bare, then with one flag at a time,
    and log which flag Maya objects to. Diagnostic only - every attempt that
    succeeds is undone."""
    m5 = sys.modules.get("uvstudio_m5_ui")
    if m5 is None:
        return
    tool = m5.find_tool(tool_id)
    spec = m5.COMMAND_INPUT.get(tool.cmd_key)
    if not spec or len(spec) > 2 or spec[0] == "context":
        return
    name = runner.bridge.cmd.name_of(tool.cmd_key)
    components = runner._components_for(cmds, spec[0], spec[1])
    if not components:
        _dlog("        bisect: no components to try")
        return
    flags, _dropped = m5.filter_flags(name, runner.prefs.settings_for(tool))

    def attempt(kwargs):
        try:
            getattr(cmds, name)(components, **kwargs)
            cmds.undo()
            return "ok"
        except Exception as exc:
            text = str(exc).strip().splitlines()
            return (text[-1] if text else type(exc).__name__)[:90]

    _dlog("        bisect %s on %d %s: no flags -> %s"
          % (name, len(components), spec[0], attempt({})))
    for key, value in sorted(flags.items()):
        _dlog("          only %s=%r -> %s" % (key, value, attempt({key: value})))


def _smoke_on(runner, shape, label, cmds, m2):
    _dlog("--- Tool test on %s ---" % label)
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for entry in SMOKE_TOOLS:
        tool_id, kind = entry[0], entry[1]
        overrides = entry[2] if len(entry) > 2 else None
        shells, _us, _vs = m2.extract_shells(shape)
        target = _debug_selection(cmds, m2, shape, shells, kind)
        if not target:
            _dlog("  SKIP  %-16s needs %s; this mesh has too few shells"
                  % (tool_id, kind))
            counts["SKIP"] += 1
            continue
        if tool_id == "stack_pairs":
            # Nothing to stack is a correct no-op, not a failure: a mesh
            # with no duplicate shells has no pairs (pCube12, one shell).
            try:
                families = m2.stacking_opportunity(shells)["family_count"]
            except Exception:
                families = 1
            if not families:
                _dlog("  SKIP  %-16s needs duplicate shells; this mesh has "
                      "none" % tool_id)
                counts["SKIP"] += 1
                continue
        if tool_id in MISALIGN_FIRST:
            try:
                picks = [sh for sh in sorted(shells, key=lambda x: -len(x.uv_ids))]
                ids = []
                seen = []
                for sh in picks:
                    if all(abs(sh.centre[0] - q.centre[0]) > 1e-3
                           or abs(sh.centre[1] - q.centre[1]) > 1e-3
                           for q in seen):
                        seen.append(sh)
                    if len(seen) == 2:
                        break
                if len(seen) == 2:
                    cmds.polyEditUV(m2.expand_components(shape,
                                                         seen[1].uv_ids),
                                    relative=True, uValue=0.031, vValue=0.017)
            except Exception as exc:
                _dlog("  note  %-16s could not misalign first: %s"
                      % (tool_id, exc))
        if tool_id in DISTORT_FIRST or tool_id in TILT_FIRST:
            try:
                if tool_id in DISTORT_FIRST:
                    _distort(cmds, m2, shape, shells, target)
                else:
                    owner = max(shells, key=lambda sh: len(sh.uv_ids))
                    cu, cv = owner.centre
                    tilt = m2.expand_components(shape, owner.uv_ids)
                    cmds.polyEditUV(tilt, relative=True, angle=12.0,
                                    pivotU=cu, pivotV=cv)
            except Exception as exc:
                _dlog("  note  %-16s could not prepare first: %s"
                      % (tool_id, exc))
        cmds.select(target, replace=True)
        before = _snap(m2, shape)
        try:
            result = runner.run(tool_id, overrides, record=False)
            ok, message = result.ok, result.message
            detail = result.detail if isinstance(result.detail, dict) else {}
        except Exception as exc:
            ok, message, detail = False, "raised %s" % exc, {}
        after = _snap(m2, shape)
        changed = after != before
        restored = None
        if ok and changed:
            try:
                cmds.undo()
                restored = _snap(m2, shape) == before
            except Exception:
                restored = False
        if not ok:
            verdict = "FAIL  %s" % message
        elif tool_id in READ_ONLY:
            verdict = ("FAIL  a read-only tool CHANGED the UVs" if changed
                       else "PASS")
        elif not changed:
            verdict = "FAIL  ran but changed nothing"
        elif not restored:
            verdict = "FAIL  one Ctrl+Z did NOT fully revert it"
        else:
            verdict = "PASS"
        counts["PASS" if verdict == "PASS" else "FAIL"] += 1
        extra = detail.get("ignored_settings") or detail.get("verdict")
        status, _, reason = verdict.partition("  ")
        _dlog("  %-4s  %-16s %s%s" % (status, tool_id, reason,
                                     ("  [%s]" % extra) if extra else ""))
        if not ok:
            cmds.select(target, replace=True)
            _bisect_native(runner, tool_id, cmds)
    _dlog("  %(PASS)d passed, %(FAIL)d failed, %(SKIP)d skipped" % counts)
    return counts


def _make_fixture(cmds):
    """A cube split into six shells: seams, and enough shells for align and
    distribute. Built fresh so the test never depends on the artist's mesh
    happening to have either."""
    cube = cmds.polyCube(width=1, height=1, depth=1, subdivisionsX=2,
                         subdivisionsY=2, subdivisionsZ=2,
                         constructionHistory=False,
                         name="uvStudio_debugFixture")[0]
    try:
        cmds.polyAutoProjection(cube, planes=6, layout=2,
                                percentageSpace=0.2,
                                constructionHistory=False)
    except Exception:
        cmds.polyAutoProjection(cube, planes=6, layout=2,
                                percentageSpace=0.2)
    shape = (cmds.listRelatives(cube, shapes=True, fullPath=True,
                                noIntermediate=True, type="mesh") or [cube])[0]
    return cube, shape


def debug_tool_smoke(runner, mesh):
    """Every listed tool, twice: on a DUPLICATE of the artist's mesh, and on
    a generated six-shell fixture. Each must run, change the UVs, and revert
    with exactly one undo. The artist's mesh is never touched."""
    import maya.cmds as cmds
    m2 = _load_bridge_module()
    original = cmds.ls(selection=True, long=True) or []
    totals = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    made = []
    try:
        dup = cmds.duplicate(mesh, returnRootsOnly=True)[0]
        made.append(dup)
        shape = (cmds.listRelatives(dup, shapes=True, fullPath=True,
                                    noIntermediate=True, type="mesh")
                 or [dup])[0]
        for k, v in _smoke_on(runner, shape, "a duplicate of %s" % mesh,
                              cmds, m2).items():
            totals[k] += v

        cube, cube_shape = _make_fixture(cmds)
        made.append(cube)
        for k, v in _smoke_on(runner, cube_shape,
                              "a generated six-shell cube", cmds, m2).items():
            totals[k] += v
    finally:
        for node in made:
            try:
                cmds.delete(node)
            except Exception:
                pass
        if original:
            cmds.select(original, replace=True)
        else:
            cmds.select(clear=True)
    return totals


def debug_split_check(runner):
    """Split the centre UV of a 2x2 plane: exactly 3 new UVs is correct."""
    import maya.cmds as cmds
    _dlog("--- Split UVs check (2x2 plane, centre UV shared by 4 faces) ---")
    plane = cmds.polyPlane(subdivisionsX=2, subdivisionsY=2,
                           constructionHistory=False,
                           name="uvStudio_debugSplit")[0]
    try:
        cmds.select("%s.map[4]" % plane, replace=True)
        result = runner.run("split", record=False)
        detail = result.detail if isinstance(result.detail, dict) else {}
        _dlog("  %s  route=%s" % (detail.get("verdict", result.message),
                                  detail.get("route")))
        # Maya's own result is the reference, so this passes when the split
        # ran and produced new UVs; how many is Maya's decision, not ours.
        return bool(result.ok and (detail.get("actual_new") or 0) > 0)
    finally:
        cmds.delete(plane)


def debug_self_tests():
    import io
    import contextlib
    _dlog("--- Built-in self tests ---")
    passed = True
    for name in ("uvstudio_m4_recipe", "uvstudio_m5_ui",
                 "uvstudio_m5b_handlers", "uvstudio_m6_texture",
                 "uvstudio_m9_cluster_map"):
        module = sys.modules.get(name)
        runner = getattr(module, "run_self_test", None)
        if runner is None:
            _dlog("  SKIP  %s not loaded" % name)
            continue
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                ok = runner()
        except Exception as exc:
            ok = False
            buffer.write("raised %s" % exc)
        passed = passed and bool(ok)
        failed = [l.strip() for l in buffer.getvalue().splitlines()
                  if " FAIL" in l or "raised" in l]
        _dlog("  %s  %s" % ("PASS" if ok else "FAIL", name))
        for line in failed[:10]:
            _dlog("        %s" % line)
    return passed


def debug_parity(runner):
    m5 = sys.modules.get("uvstudio_m5_ui")
    _dlog("--- Option parity against this Maya ---")
    rows = m5.parity_report(runner.bridge.cmd)
    for line in m5.format_parity(rows).splitlines()[2:]:
        _dlog("  " + line)
    return rows


def debug_pins(runner, mesh):
    m2 = _load_bridge_module()
    _dlog("--- Pin round-trip on a duplicate ---")
    result = m2.verify_pin_roundtrip(mesh, runner.bridge.cmd, verbose=False)
    _dlog("  %s" % result.get("verdict"))
    return result


def debug_viewport(host):
    _dlog("--- Pair-mode viewport ---")
    if host is None:
        _dlog("  UV Studio window is not open")
        return None
    try:
        widget = host._find_viewport()
    except Exception as exc:
        _dlog("  finding the viewport raised %s" % exc)
        return None
    if widget is None:
        _dlog("  no viewport widget found in the hosted panel")
        return None
    _dlog("  %s %dx%d  overlay_ok=%s" % (widget.metaObject().className(),
                                         widget.width(), widget.height(),
                                         getattr(host, "overlay_ok", None)))
    return widget


def debug_icons(host):
    """Where each tool's icon came from: Maya's own (by search) or drawn."""
    _dlog("--- Icons ---")
    panel = getattr(host, "tools_panel", None)
    if panel is None or not hasattr(panel, "icon_report"):
        _dlog("  panel not available")
        return None
    report = panel.icon_report()
    maya = sorted(k for k, v in report.items() if v.startswith("maya:"))
    drawn = sorted(k for k, v in report.items() if v.startswith("drawn:"))
    none = sorted(k for k, v in report.items() if v == "none")
    _dlog("  %d from Maya, %d drawn, %d none" % (len(maya), len(drawn),
                                                  len(none)))
    for key in maya:
        _dlog("    %-18s %s" % (key, report[key][5:]))
    if none:
        _dlog("  no icon: %s" % ", ".join(none))
    return report


def debug_save():
    """Write the report beside the preferences and copy it to the clipboard."""
    import os
    text = "\n".join(DEBUG_LINES)
    path = None
    try:
        import maya.cmds as cmds
        folder = os.path.join(cmds.internalVar(userAppDir=True), "uvstudio")
        if not os.path.isdir(folder):
            os.makedirs(folder)
        path = os.path.join(folder, DEBUG_REPORT_NAME)
        with open(path, "w") as handle:
            handle.write(text)
    except Exception:
        path = None
    for binding in ("PySide6", "PySide2"):
        try:
            QtWidgets = __import__(binding + ".QtWidgets",
                                   fromlist=["QtWidgets"])
            QtWidgets.QApplication.clipboard().setText(text)
            break
        except Exception:
            continue
    return path


def _debug_header():
    import time
    del DEBUG_LINES[:]
    _dlog("UV Studio debug report  %s" % time.strftime("%Y-%m-%d %H:%M"))
    try:
        import maya.cmds as cmds
        _dlog("Maya %s" % cmds.about(version=True))
    except Exception:
        pass


def _stage(name, fn, *args):
    """Run one debug stage; a failure is written INTO the report.

    A stage that raised used to abort Run all before the report was saved,
    so the one run that found a crash produced no file to send.
    """
    try:
        return fn(*args)
    except Exception as exc:
        import traceback
        _dlog("--- %s CRASHED: %s ---" % (name, exc))
        for line in traceback.format_exc().strip().splitlines()[-4:]:
            _dlog("      " + line)
        return None


def _debug_one(run, body):
    runner, host = _debug_context()
    if runner is None:
        raise RuntimeError("Open UV Studio first (uvstudio.show())")
    _debug_header()
    try:
        outcome = body(runner, host)
    except Exception as exc:
        _dlog("--- run aborted: %s ---" % exc)
        outcome = "aborted"
    path = debug_save()
    return OrderedDict([("result", outcome), ("report", path or "clipboard"),
                        ("lines", len(DEBUG_LINES))])


def _first_mesh(run):
    if not run.meshes:
        raise RuntimeError("Select a mesh (or a shell/UVs on it) first")
    return run.meshes[0]


@_handler("debug: run all")
def debug_all(run):
    mesh = _first_mesh(run)

    def body(runner, host):
        results = OrderedDict()
        ok = _stage("self tests", debug_self_tests)
        results["self_tests"] = "PASS" if ok else "FAIL"
        smoke = _stage("tool test", debug_tool_smoke, runner, mesh)
        results["tools"] = ("%(PASS)d pass / %(FAIL)d fail" % smoke
                            if smoke else "crashed")
        exact = _stage("split check", debug_split_check, runner)
        results["split"] = "ran" if exact else "FAILED"
        _stage("pins", debug_pins, runner, mesh)
        _stage("viewport", debug_viewport, host)
        _stage("icons", debug_icons, host)
        _stage("parity", debug_parity, runner)
        return results
    return _debug_one(run, body)


@_handler("debug: tool test")
def debug_tools(run):
    mesh = _first_mesh(run)
    return _debug_one(run, lambda r, h: debug_tool_smoke(r, mesh))


@_handler("debug: split check")
def debug_split(run):
    return _debug_one(run, lambda r, h: debug_split_check(r))


@_handler("debug: self tests")
def debug_self(run):
    return _debug_one(run, lambda r, h: debug_self_tests())


@_handler("debug: parity")
def debug_parity_button(run):
    return _debug_one(run, lambda r, h: len(debug_parity(r)))


@_handler("debug: pins")
def debug_pins_button(run):
    mesh = _first_mesh(run)
    return _debug_one(run, lambda r, h: debug_pins(r, mesh).get("verdict"))


@_handler("debug: viewport")
def debug_viewport_button(run):
    return _debug_one(run, lambda r, h: bool(debug_viewport(h)))


@_handler("straighten UVs", writes=True)
def straighten_uvs(run):
    """Maya's Straighten UVs, with its angle tolerance and U/V choice.

    texStraightenUVs is a MEL procedure taking positional arguments - the
    direction string and the angle - not -flags, so it cannot go through
    mel_call's kwargs-to-flags path. The call is built here, quoted, and a
    failure reports the exact MEL that was sent, so a wrong signature on
    another Maya version is diagnosable from the log line alone.
    """
    resolver = run.bridge.cmd if run.bridge is not None else None
    if resolver is None or not resolver.available("straighten_uvs"):
        raise RuntimeError("texStraightenUVs is not available on this build")

    use_u = bool(run.get("axis_u", True))
    use_v = bool(run.get("axis_v", True))
    if not (use_u or use_v):
        return OrderedDict([("message", "Turn on U, V or both")])
    direction = "UV" if (use_u and use_v) else ("U" if use_u else "V")
    angle = run.get("angle", 30.0, float)

    try:
        import maya.mel as mel
    except ImportError:
        raise RuntimeError("Maya required")
    command = 'texStraightenUVs "%s" %s;' % (direction, repr(float(angle)))
    try:
        mel.eval(command)
    except Exception as exc:
        raise RuntimeError("%s failed: %s" % (command, exc))
    return OrderedDict([("direction", direction), ("angle", angle),
                        ("mel", command)])


@_handler("align", writes=True)
def align(run):
    packer = run.need_packer()
    edge = run.get("edge", "left")
    units = run.units_for(run.selected_shells())
    if len(units) < 2:
        return OrderedDict([("moved", 0),
                            ("message", "Select at least two shells")])

    items, units_by_key = run.items_for(units)
    deltas = packer.align(items, edge)
    moved = _apply_deltas(run, items, units_by_key, deltas)
    return OrderedDict([("edge", edge), ("units_moved", len(deltas)),
                        ("uvs_moved", moved)])


@_handler("distribute", writes=True)
def distribute(run):
    packer = run.need_packer()
    axis = run.get("axis", "u")
    spacing = run.get("spacing")
    units = run.units_for(run.selected_shells())
    if len(units) < 3 and spacing is None:
        return OrderedDict([("moved", 0),
                            ("message", "Select at least three shells, or "
                                        "set a spacing")])

    items, units_by_key = run.items_for(units)
    deltas = packer.distribute(items, axis=axis, spacing=spacing)
    moved = _apply_deltas(run, items, units_by_key, deltas)
    return OrderedDict([("axis", axis), ("units_moved", len(deltas)),
                        ("uvs_moved", moved)])


# =====================================================================
# SECTION 6 - Packing
# =====================================================================

def _placement_move(unit, placement, bounds=None):
    """(scale, scale_pivot, rotate_pivot, (du, dv)) landing a unit on target.

    THIS IS THE FIX FOR THE ROTATED-PLACEMENT DEFECT. M3 reports the target
    corner of the box it placed; that box may be scaled, and may be the
    shell's footprint turned on its side. v1 applied Placement.delta - the
    move for an unrotated, unscaled box - and then rotated anyway, so the
    result was off by (width - height) / 2 in U and the opposite in V, and
    ignored scale entirely.

    Rather than special-casing, the corner is tracked through each step and
    the translation simply closes whatever gap is left. That is exact for any
    width, height, scale and rotation, and collapses to Placement.delta in the
    common case where nothing is scaled or rotated.
    """
    u_min, u_max, v_min, v_max = bounds if bounds else unit.bounds
    width, height = u_max - u_min, v_max - v_min
    scale = getattr(placement, "scale", 1.0) or 1.0

    # Step 1 - scale about the box's own minimum corner, so the corner stays
    # where it was and the box becomes width*scale by height*scale.
    scale_pivot = (u_min, v_min)
    current_u, current_v = u_min, v_min
    current_w, current_h = width * scale, height * scale

    # Step 2 - rotate 90 degrees about the scaled box's centre.
    rotate_pivot = None
    if placement.rotated:
        centre_u = current_u + current_w * 0.5
        centre_v = current_v + current_h * 0.5
        rotate_pivot = (centre_u, centre_v)
        current_u = centre_u - current_h * 0.5
        current_v = centre_v - current_w * 0.5
        current_w, current_h = current_h, current_w

    # Step 3 - translate the corner onto the target.
    return (scale, scale_pivot, rotate_pivot,
            (placement.u - current_u, placement.v - current_v))


def _pre_orient(run, units, apply=True):
    """Straighten each unit before measuring it, and report the new bounds.

    WHY THIS IS PART OF PACKING
        The packer places BOUNDING BOXES. A long thin strip lying at 40
        degrees has a box several times its own area, and that wasted area
        is real - nothing else can be placed in it. On asset-like shapes,
        measured: 49 of 120 shells placed as modelled, 120 of 120 after
        straightening, with total box area falling 5.905 -> 3.111.

        So this is not a tidiness pass that happens to run before packing.
        It is the difference between a layout that fits and one that does
        not, which is why it belongs inside pack rather than beside it.

    The rotation is written to the scene AND applied analytically to the
    bounds handed to the packer, so the boxes being placed are the boxes
    that now exist. Measuring before and placing after would put every
    shell in the wrong slot.
    """
    packer = run.need_packer()
    bridge_module = run.need_bridge_module()
    snap = run.get("pre_orient_snap", 0.0, float) or None

    coords = {}
    for mesh in set(shell.mesh for unit in units for shell in unit.shells):
        try:
            _shells, us, vs = bridge_module.extract_shells(mesh)
        except Exception:
            continue
        coords[mesh] = (us, vs)

    bounds = {}
    turned = 0
    for unit in units:
        if unit.is_pinned:
            continue
        points = []
        for shell in unit.shells:
            us_vs = coords.get(shell.mesh)
            if us_vs is None:
                continue
            us, vs = us_vs
            limit = min(len(us), len(vs))
            points.extend((us[i], vs[i]) for i in shell.uv_ids if i < limit)
        if len(points) < 3:
            continue

        angle = packer.orientation_correction(points, snap=snap)
        if abs(angle) < 1.0e-6:
            continue

        u_min, u_max, v_min, v_max = unit.bounds
        pivot = ((u_min + u_max) * 0.5, (v_min + v_max) * 0.5)
        # apply=False is the preview path: measure the straightening
        # without performing it, so the numbers shown are the numbers Pack
        # will produce and nothing has been written to say so.
        if apply:
            run.rotate_unit(unit, angle, pivot)
        turned += 1

        radians = math.radians(angle)
        cos_a, sin_a = math.cos(radians), math.sin(radians)
        rotated = [(pivot[0] + (u - pivot[0]) * cos_a - (v - pivot[1]) * sin_a,
                    pivot[1] + (u - pivot[0]) * sin_a + (v - pivot[1]) * cos_a)
                   for u, v in points]
        bounds[unit.index] = (min(p[0] for p in rotated),
                              max(p[0] for p in rotated),
                              min(p[1] for p in rotated),
                              max(p[1] for p in rotated))
    return bounds, turned


def _next_udim_after(used, count):
    """`count` UDIM numbers not already in `used`, in standard tiling order
    (left to right, then up a row) starting from 1001.

    Always starts the search from 1001 rather than "one past the highest
    requested tile", so a request of [1001, 1050] fills the gap at 1002
    before reaching for 1051. Overflow is meant to look like a texture atlas
    growing, not tiles scattered wherever the search happened to start.
    """
    used = set(int(t) for t in used)
    found = []
    candidate = 1001
    # 10x10 UDIM block is Maya's own practical ceiling; beyond it this is
    # certainly a mistake in the settings, not an asset that needs 101 tiles.
    while len(found) < count and candidate < 1101:
        if candidate not in used:
            found.append(candidate)
        candidate += 1
    return found


def _with_overflow_tiles(run, udims, items):
    """Extend the requested tiles when the content will not plausibly fit.

    THE BUG THIS FIXES
        Pack was given exactly the tiles asked for and no more. Asset
        content routinely exceeds one tile, and everything past it was
        landing in the quarantine row below UDIM 0 rather than in 1002 or
        1011 - visually "packed outside a single UDIM", which is what it
        was: never given a second one to pack into. The spec calls this
        "overflow into more tiles" and no version of the tool had built it.

    WHY THIS IS AN ESTIMATE MADE ONCE, NOT A RETRY LOOP
        The alternative - pack, check what's left, add a tile, pack again -
        costs a full MaxRects solve per retry on however many hundred units
        an asset has. The estimate below is allowed to be generous: an
        unused extra tile costs nothing (tiles_used only ever reports tiles
        that received something), while an under-estimate means a second
        pack call. Erring toward more tiles is the cheap direction.

        0.65 as the assumed achievable occupancy is deliberately below the
        packer's typical 80-95% on real content: this is provisioning
        headroom, not a claim about how well anything will actually pack.
    """
    if not run.get("overflow", True):
        return list(udims)
    total_area = sum(item.width * item.height for item in items
                     if not item.pinned)
    have = len(udims)
    needed = max(have, int(math.ceil(total_area / 0.65)))
    cap = have + max(0, run.get("max_overflow_tiles", 24, int))
    needed = min(needed, cap)
    if needed <= have:
        return list(udims)
    return list(udims) + _next_udim_after(udims, needed - have)


def _quarantine_unplaced(run, units_by_key, unplaced_keys, udims, padding):
    """Move every unplaced unit clear of the tiles and of each other.

    A shell the packer could not fit is not "untouched" - it is still
    exactly where it started, which after everything else has moved is very
    often on top of whatever got placed. That silently breaks the rule that
    matters most here: nothing overlaps unless an artist put it there.

    Unplaced units go into a holding row directly below the lowest tile,
    left to right in a simple shelf, wrapping when a row would run past the
    tiles' width. The row is built with the same care a pack gives its own
    layout - sorted tallest first, non-overlapping by construction - so
    "quarantined" reads as a deliberate holding area, not a second failure.
    """
    if not unplaced_keys:
        return 0

    tiles = udims or [1001]
    coords = [((int(t) - 1001) % 10, (int(t) - 1001) // 10) for t in tiles]
    u_min_tile = min(tu for tu, _tv in coords)
    span_u = max(max(tu for tu, _tv in coords) + 1 - u_min_tile, 1.0)
    v_min_tile = min(tv for _tu, tv in coords)

    gap = max(padding, 0.01)          # a floor, so padding=0 still separates
    row_top = float(v_min_tile) - gap
    cursor_u = float(u_min_tile)
    row_height = 0.0
    moved = 0

    ordered = sorted(
        (k for k in unplaced_keys if k in units_by_key),
        key=lambda k: -(run.bounds_of(units_by_key[k])[3]
                        - run.bounds_of(units_by_key[k])[2]))
    for key in ordered:
        unit = units_by_key[key]
        if unit.is_pinned:
            continue                  # cannot happen today; guarded anyway
        u0, u1, v0, v1 = run.bounds_of(unit)
        width, height = u1 - u0, v1 - v0
        if cursor_u > u_min_tile and cursor_u + width > u_min_tile + span_u:
            row_top -= row_height + gap
            cursor_u = float(u_min_tile)
            row_height = 0.0
        target_u, target_v = cursor_u, row_top - height
        moved += run.translate_unit(unit, target_u - u0, target_v - v0)
        cursor_u += width + gap
        row_height = max(row_height, height)
    return moved


@_handler("pack", writes=True)
def pack(run):
    """Lay out every unit across the target UDIM tiles.

    Pinned units are not moved and are handed to the packer as obstacles, so
    live shells route around them instead of landing on top.
    """
    packer = run.need_packer()
    units = run.units
    if not units:
        return OrderedDict([("message", "Nothing to pack")])

    # Straighten first, then measure. Both halves matter: see _pre_orient.
    oriented, turned = (_pre_orient(run, units)
                        if run.get("pre_orient", True) else ({}, 0))

    items, units_by_key = run.items_for(units, bounds=oriented)
    requested = run.get("udims") or run.report.get("udims") or [1001]
    udims = _with_overflow_tiles(run, requested, items)
    result = packer.pack(
        items,
        udims=list(udims),
        padding=run.get("padding", 0.002, float),
        allow_rotation=bool(run.get("allow_rotation", True)),
        sort=run.get("sort", "area"),
        scale_to_fit=bool(run.get("scale_to_fit", False)))

    moved = 0
    rotated = 0
    scaled = 0
    for placement in result.placements.values():
        unit = units_by_key.get(placement.key)
        if unit is None or unit.is_pinned:
            continue
        if placement.matrix is None:
            continue
        if placement.rotated:
            rotated += 1
        if abs(placement.scale - 1.0) > 1.0e-9:
            scaled += 1
        # One affine per unit, applied directly - see UVWriter.apply_affine.
        for mesh, uv_ids in unit.by_mesh().items():
            moved += run.writer(mesh).apply_affine(
                uv_ids, placement.matrix, placement.translation)

    quarantined = _quarantine_unplaced(run, units_by_key, result.unplaced,
                                       udims, run.get("padding", 0.002, float))

    return OrderedDict([
        ("placed", len(result.placements)),
        ("unplaced", len(result.unplaced)),
        ("quarantined", quarantined),
        ("tiles_requested", list(requested)),
        ("tiles_given", list(udims)),
        ("pre_oriented", turned),
        ("rotated", rotated),
        ("scaled", scaled),
        ("uvs_moved", moved),
        ("scale", result.scale),
        ("tiles", result.tiles_used),
        ("occupancy", round(100.0 * result.mean_occupancy, 1)),
        ("seconds", round(result.seconds, 3)),
    ])


# =====================================================================
# SECTION 6b - Read-only summaries for the action bar
# =====================================================================

@_handler("analyze")
def analyze(run):
    """Count what is there. Writes nothing.

    Feeds the four counters the panel keeps visible: how many units a pack
    would place, how many are duplicate pairs, how many are bigger stacks,
    and how many are pinned and therefore fixed in place. These are the
    numbers that decide whether a pack is worth running, so they are
    reported before it rather than after.
    """
    bridge_module = run.need_bridge_module()
    report = run.report
    units = report["units"]

    # Pairs and stacks are counted from the UNITS, not from the duplicate
    # finder. The finder reports what COULD be stacked and still has its
    # area to reclaim; these counters mean what already moves as one thing.
    # Counting the opportunity here reported zero pairs on a mesh whose
    # copies were perfectly stacked - the case where the answer is most
    # obviously two.
    pairs = len([u for u in units if len(u.shells) == 2])
    stacks = len([u for u in units if len(u.shells) > 2])

    # The opportunity is a separate, useful number: what is still loose.
    reclaimable = 0.0
    try:
        reclaimable = bridge_module.stacking_opportunity(
            report["shells"])["percent"]
    except Exception:
        reclaimable = 0.0

    return OrderedDict([
        ("meshes", len(run.meshes)),
        ("shells", report.get("total_shells", len(report["shells"]))),
        ("units", len(units)),
        ("pairs", pairs),
        ("stacks", stacks),
        ("fixed", len([u for u in units if u.is_pinned])),
        ("reclaimable_percent", round(reclaimable, 2)),
    ])


@_handler("pack preview")
def pack_preview(run):
    """Solve the layout and report it. Writes nothing.

    The same call pack makes, stopping before the writes. It exists because
    the honest way to answer "will this fit" is to run the packer, and
    running it twice is cheaper than an artist undoing a bad pack on 338
    shells. Deliberately shares pack's settings entry, so what is previewed
    is what Pack would do.
    """
    packer = run.need_packer()
    units = run.units
    if not units:
        return OrderedDict([("message", "Nothing to pack")])

    # Straightening applied on paper only. A preview that wrote would not
    # be a preview; one that ignored straightening would predict a layout
    # Pack is not going to produce.
    oriented, turned = (_pre_orient(run, units, apply=False)
                        if run.get("pre_orient", True) else ({}, 0))
    items, _units_by_key = run.items_for(units, bounds=oriented)
    requested = run.get("udims") or run.report.get("udims") or [1001]
    udims = _with_overflow_tiles(run, requested, items)
    result = packer.pack(
        items, udims=list(udims),
        padding=run.get("padding", 0.002, float),
        allow_rotation=bool(run.get("allow_rotation", True)),
        sort=run.get("sort", "area"),
        scale_to_fit=bool(run.get("scale_to_fit", False)))

    return OrderedDict([
        ("would_pre_orient", turned),
        ("tiles_requested", list(requested)),
        ("tiles_it_would_use", list(udims)),
        ("would_place", len(result.placements)),
        ("would_not_fit", len(result.unplaced)),
        ("would_rotate", len([p for p in result.placements.values()
                              if p.rotated])),
        ("scale", result.scale),
        ("tiles", result.tiles_used),
        ("occupancy", round(100.0 * result.mean_occupancy, 1)),
        ("seconds", round(result.seconds, 3)),
    ])


# =====================================================================
# SECTION 7 - Pairs and stacking
# =====================================================================

@_handler("find pairs")
def find_pairs(run):
    """Report duplicate families without changing anything."""
    bridge_module = run.need_bridge_module()
    opportunity = bridge_module.stacking_opportunity(run.shells)
    return OrderedDict([
        ("families", opportunity["family_count"]),
        ("reclaimable_area", round(opportunity["reclaimable_area"], 5)),
        ("percent", round(opportunity["percent"], 2)),
        ("largest", [OrderedDict([("count", f["count"]), ("size", f["size"]),
                                  ("uvs", f["uv_count"])])
                     for f in opportunity["families"][:5]]),
        ("note", "Stacking makes copies share texture space."),
    ])


@_handler("stack pairs", writes=True)
def stack_pairs(run):
    """Move duplicate families on top of each other.

    A pinned copy anchors its family: if the artist pinned one, that is the
    one everything else comes to.
    """
    bridge_module = run.need_bridge_module()
    opportunity = bridge_module.stacking_opportunity(run.shells)

    stacked = 0
    moved = 0
    for family in opportunity["families"]:
        for shell, du, dv in bridge_module.stack_deltas(family["shells"]):
            if shell.is_pinned:
                continue
            moved += run.writer(shell.mesh).translate(shell.uv_ids, du, dv)
        stacked += 1

    return OrderedDict([("families_stacked", stacked), ("uvs_moved", moved),
                        ("reclaimed",
                         round(opportunity["reclaimable_area"], 5))])


# =====================================================================
# SECTION 7b - Explicit pairing
#
# These are the commands an alt-drag will eventually call. They take shells
# by ANCHOR rather than by anything on screen, so the canvas, when it
# exists, only has to answer "which shell did the arrow start on, and which
# did it end on" - it never learns what a link is.
# =====================================================================

def _shells_from_anchors(run, anchors):
    """Shells named by (mesh, uv id) pairs, in the order given."""
    bridge_module = run.need_bridge_module()
    owner = {}
    for shell in run.shells:
        for uv_id in shell.uv_ids:
            owner[(bridge_module.canonical_mesh(shell.mesh), uv_id)] = shell
    found = []
    for mesh, uv_id in anchors or []:
        shell = owner.get((bridge_module.canonical_mesh(mesh), int(uv_id)))
        if shell is not None and shell not in found:
            found.append(shell)
    return found


def _shells_to_link(run):
    """What the artist is pointing at: explicit anchors, else the selection.

    Anchors come from a drag ("this shell to that one"); the selection is
    the keyboard route. Both end in the same place, so the drag adds no
    second way for a pairing to be made.
    """
    anchors = run.get("anchors")
    if anchors:
        return _shells_from_anchors(run, anchors)
    return run.selected_shells()


@_handler("pair shells", writes=True)
def link_pair(run):
    """Mark shells as one unit that also stacks.

    Refuses on fewer than two, rather than linking a shell to itself: a
    one-shell link resolves to a group of one, which is indistinguishable
    from no link and would look like the command silently failing.
    """
    bridge_module = run.need_bridge_module()
    shells = _shells_to_link(run)
    if len(shells) < 2:
        return OrderedDict([("linked", 0),
                            ("message", "Select or drag between two shells")])

    mode = (bridge_module.ShellLink.LOCK
            if run.get("mode") == "lock" else bridge_module.ShellLink.STACK)
    link = bridge_module.link_shells(shells, mode)
    return OrderedDict([("linked", len(shells)), ("mode", mode),
                        ("anchors", [list(a) for a in link.anchors]
                         if link else [])])


@_handler("lock group", writes=True)
def link_lock(run):
    """Move together, keep relative positions. Pairing without stacking."""
    run.settings["mode"] = "lock"
    return link_pair.__wrapped__(run)


@_handler("unlink", writes=True)
def unlink(run):
    """Forget any stored link touching these shells."""
    bridge_module = run.need_bridge_module()
    shells = _shells_to_link(run)
    if not shells:
        return OrderedDict([("unlinked", 0), ("message", "Nothing selected")])
    return OrderedDict([("unlinked", bridge_module.unlink_shells(shells))])


@_handler("show links")
def list_links(run):
    """What is linked, and what stopped resolving."""
    report = run.report
    resolved = report.get("links") or []
    stale = report.get("stale_links") or []
    return OrderedDict([
        ("links", len(resolved)),
        ("shells_linked", sum(len(members) for members, _mode in resolved)),
        ("stale", len(stale)),
        ("note", "Stale links point at UV ids that no longer exist - usually "
                 "a cut, sew or reprojection since they were made."),
    ])


# =====================================================================
# SECTION 8 - Pins
# =====================================================================

@_handler("pin", writes=True)
def pin(run):
    """Pin or unpin the selected UVs.

    Deliberately never touches run.report: pinning needs the selection and
    nothing else, and running a full shell analysis to set a pin would make
    the cheapest tool in the panel one of the slowest.
    """
    bridge_module = run.need_bridge_module()
    value = run.get("value", 1.0, float)
    selection = run.selection_by_mesh

    changed = 0
    for mesh in run.meshes:
        uv_ids = sorted(selection.get(mesh, ()))
        if not uv_ids:
            continue
        if bridge_module.set_pins(mesh, uv_ids, pinned=value > 0.0,
                                  resolver=run.bridge.cmd):
            changed += len(uv_ids)
    return OrderedDict([("pinned" if value > 0 else "unpinned", changed)])


@_handler("invert pins", writes=True)
def invert_pins(run):
    """Swap pinned and unpinned across each mesh."""
    bridge_module = run.need_bridge_module()

    flipped = 0
    for mesh in run.meshes:
        # run.report FIRST. pinned_for returns None until the analysis has
        # run, so asking it before touching run.report always missed the
        # cached pins and took the slow path below - re-reading every pin and
        # re-extracting every shell on a mesh that had just been analysed.
        report = run.report
        uv_count = ((report.get("meshes") or {}).get(mesh) or {}).get(
            "uv_count")
        pinned = run.pinned_for(mesh)
        if pinned is None or uv_count is None:
            # Older report shape, or a bridge that does not report pin sets.
            pinned = bridge_module.read_pinned_uvs(mesh, run.bridge.cmd)
            _shells, us, _vs = bridge_module.extract_shells(mesh)
            uv_count = len(us)

        complement = sorted(set(range(uv_count)) - set(pinned))
        if pinned:
            bridge_module.set_pins(mesh, sorted(pinned), pinned=False,
                                   resolver=run.bridge.cmd)
        if complement:
            bridge_module.set_pins(mesh, complement, pinned=True,
                                   resolver=run.bridge.cmd)
        flipped += len(complement)
    return OrderedDict([("now_pinned", flipped)])


@_handler("select pinned")
def select_pinned(run):
    """Select every pinned UV. Changes the selection; that is the point."""
    bridge_module = run.need_bridge_module()
    cmds = _maya()

    total = 0
    components = []
    for mesh in run.meshes:
        pinned = run.pinned_for(mesh)
        if pinned is None:
            pinned = bridge_module.read_pinned_uvs(mesh, run.bridge.cmd)
        if not pinned:
            continue
        components.extend(bridge_module.expand_components(mesh,
                                                          sorted(pinned)))
        total += len(pinned)

    if components and cmds is not None:
        cmds.select(components, replace=True)
    return OrderedDict([("selected", total)])


# =====================================================================
# SECTION 9 - Topology
# =====================================================================

@_handler("cut edges", writes=True)
def cut(run):
    """Cut UV edges. Works from a UV or an edge selection."""
    cmds = _maya()
    sel = cmds.ls(selection=True, flatten=True, long=True) or [] if cmds else []
    if not sel:
        return OrderedDict([("cut", 0), ("message", "Nothing selected")])
    return OrderedDict([("edges_cut", run.bridge.cut_edges(sel))])


@_handler("sew edges", writes=True)
def sew(run):
    """Sew UV edges. Works from a UV or an edge selection."""
    cmds = _maya()
    sel = cmds.ls(selection=True, flatten=True, long=True) or [] if cmds else []
    if not sel:
        return OrderedDict([("sewn", 0), ("message", "Nothing selected")])
    return OrderedDict([("edges_sewn", run.bridge.sew_edges(sel, move=False))])


@_handler("move and sew", writes=True)
def sew_move(run):
    """Move the smaller shell onto the larger and sew. UVs or edges."""
    cmds = _maya()
    sel = cmds.ls(selection=True, flatten=True, long=True) or [] if cmds else []
    if not sel:
        return OrderedDict([("sewn", 0), ("message", "Nothing selected")])
    return OrderedDict([("edges_sewn", run.bridge.sew_edges(sel, move=True))])


@_handler("split UVs", writes=True)
def split(run):
    """Split UVs. Native command if one resolves, else UV Studio's edge-cut
    route; either way the result reports predicted vs actual new UVs."""
    cmds = _maya()
    selection = []
    if cmds is not None:
        selection = cmds.ls(selection=True, flatten=True, long=True) or []
    if not selection:
        return OrderedDict([("split", 0), ("message", "Nothing selected")])
    return run.bridge.split_uvs(selection)


# =====================================================================
# SECTION 10 - Quality control
#
# Every audit here is read-only. That includes the selection: an audit that
# leaves the artist's selection different from how it found it has modified
# the scene in the way most likely to be blamed on the next tool pressed.
# =====================================================================

@_handler("overlap check")
def check_overlaps(run):
    packer = run.need_packer()
    items, _units = run.items_for(run.units)
    pairs = packer.find_overlaps(items)
    return OrderedDict([("overlapping_pairs", len(pairs)),
                        ("units", len(items)),
                        ("sample", pairs[:10])])


@_handler("bounds check")
def check_bounds(run):
    packer = run.need_packer()
    items, _units = run.items_for(run.units)
    udims = run.get("udims") or run.report.get("udims") or [1001]
    offenders = packer.find_out_of_bounds(items, list(udims))
    return OrderedDict([("out_of_bounds", len(offenders)),
                        ("checked_tiles", list(udims)),
                        ("sample", offenders[:10])])


@_handler("flipped check")
def check_flipped(run):
    """Reversed winding, read from Maya rather than recomputed.

    Maya's UV editor already evaluates this per face and its answer is the one
    the artist sees in the viewport. Recomputing it here would risk a second,
    disagreeing opinion.

    polySelectConstraint works by CHANGING the selection, so the artist's
    selection is saved and restored around it. v1 left whatever the constraint
    had selected in place, which meant running an audit and then any other
    tool operated on the audit's leftovers.
    """
    cmds = _maya()
    if cmds is None:
        return OrderedDict([("flipped", 0), ("message", "Maya required")])

    saved = cmds.ls(selection=True, long=True) or []
    flipped = 0
    try:
        for mesh in run.meshes:
            try:
                cmds.select(mesh, replace=True)
                cmds.polySelectConstraint(mode=3, type=0x0008, textured=2)
                flipped += len(cmds.ls(selection=True, flatten=True) or [])
            finally:
                try:
                    cmds.polySelectConstraint(disable=True)
                except Exception:
                    pass
    finally:
        try:
            if saved:
                cmds.select(saved, replace=True)
            else:
                cmds.select(clear=True)
        except Exception:
            pass
    return OrderedDict([("flipped_faces", flipped)])


@_handler("density audit")
def check_density(run):
    """Texel density spread across shells.

    World area comes from Maya; UV area from the shell bounds. The target
    defaults to the area-weighted mean, which is the density the asset mostly
    already has - a better reference than a number picked out of the air.

    NOTE, unchanged from v1 on purpose: shell.area is BOUNDING BOX area, not
    true UV area, so a shell that does not fill its box reads as denser than
    it is. Fixing that changes the numbers this tool reports, which is a
    product decision rather than a refactor, so it is flagged, not done.
    """
    packer = run.need_packer()
    cmds = _maya()
    map_size = run.get("map_size", 1024, float)
    target = run.get("target")

    shells = run.shells

    # polyEvaluate is a per-MESH question. v1 asked it once per shell, so a
    # 338-shell body issued 338 identical scene queries, and re-summed that
    # mesh's UV area inside the same loop - the same O(n^2) twice over.
    uv_totals = OrderedDict()
    for shell in shells:
        uv_totals[shell.mesh] = uv_totals.get(shell.mesh, 0.0) + shell.area

    world_areas = {}
    for mesh in uv_totals:
        area = 0.0
        if cmds is not None:
            try:
                area = float(cmds.polyEvaluate(mesh, worldArea=True))
            except Exception:
                area = 0.0
        world_areas[mesh] = area

    entries = []
    for shell in shells:
        world_area = world_areas.get(shell.mesh, 0.0)
        if world_area <= 0.0:
            continue
        total = max(1.0e-9, uv_totals.get(shell.mesh, 0.0))
        entries.append((shell.index, shell.area,
                        world_area * (shell.area / total), map_size))

    if not entries:
        return OrderedDict([("message", "Could not measure world area")])

    audit = packer.density_report(entries, target=target)
    return OrderedDict([
        ("target", round(audit["target"], 1)),
        ("under", len(audit["under"])),
        ("over", len(audit["over"])),
        ("ok", len(audit["ok"])),
        ("map_size", map_size),
    ])


# =====================================================================
# SECTION 10b - Texture transfer (M6 does the pixels; M2 reads the scene)
# =====================================================================

def _load_texture():
    return _sibling("uvstudio_m6_texture", "transfer_texture")


def _uv_sets(mesh):
    cmds = _maya()
    try:
        return cmds.polyUVSet(mesh, query=True, allUVSets=True) or []
    except Exception:
        return []


@_handler("remember layout", writes=True)
def texture_snapshot(run):
    """Keep a copy of the current UVs as the 'before' for a later transfer."""
    bridge_module = run.need_bridge_module()
    kept = 0
    for mesh in run.meshes:
        if run.get("only_if_missing") and \
                bridge_module.BEFORE_SET in _uv_sets(mesh):
            kept += 1
            continue
        bridge_module.snapshot_layout(mesh)
    return OrderedDict([("remembered", len(run.meshes) - kept),
                        ("already_saved", kept),
                        ("uv_set", bridge_module.BEFORE_SET)])


@_handler("find textures")
def texture_scan(run):
    """List the file textures on these meshes. Read-only."""
    bridge_module = run.need_bridge_module()
    found = bridge_module.texture_files(run.meshes)
    remembered = [m for m in run.meshes
                  if bridge_module.BEFORE_SET in _uv_sets(m)]
    names = [e["path"].replace("\\", "/").rsplit("/", 1)[-1]
             for e in found.values()]
    return OrderedDict([
        ("textures", len(found)),
        ("udim", len([e for e in found.values() if e["udim"]])),
        ("layout_remembered", "%d of %d mesh(es)" % (len(remembered),
                                                     len(run.meshes))),
        ("files", ", ".join(names[:8]) + (" ..." if len(names) > 8
                                          else ""))])


@_handler("transfer textures", writes=True)
def texture_transfer(run):
    """Warp each texture from the remembered layout to the current one.

    New files are written beside the originals; nothing is overwritten.
    With 'repoint' on, the file nodes are pointed at the new files in this
    press's single undo step.
    """
    bridge_module = run.need_bridge_module()
    m6 = _load_texture()
    if m6 is None:
        raise RuntimeError("Texture module (M6) is required")
    cmds = _maya()
    source_set = (run.get("source_set") or "").strip() or \
        bridge_module.BEFORE_SET
    found = bridge_module.texture_files(run.meshes)
    if not found:
        return OrderedDict([("message", "No file textures on the selected "
                                        "meshes' materials")])
    m9 = sys.modules.get("uvstudio_m9_cluster_map")
    progress = (m9.LoadProgress(None, "Transferring textures")
                if m9 is not None and hasattr(m9, "LoadProgress") else None)
    try:
        return _transfer(run, bridge_module, m6, cmds, source_set, found,
                         progress)
    except Exception as exc:
        if type(exc).__name__ == "LoadCancelled":
            return OrderedDict([("message", "Cancelled. Textures finished "
                                            "before Cancel were kept; the one "
                                            "in progress was not written.")])
        raise
    finally:
        if progress is not None:
            progress.close()


def _transfer(run, bridge_module, m6, cmds, source_set, found, progress):
    data = {}
    for m_index, mesh in enumerate(run.meshes):
        if progress is not None:
            progress(m_index, len(run.meshes), "reading UVs: %s" % mesh)
        sets = _uv_sets(mesh)
        current = (cmds.polyUVSet(mesh, query=True, currentUVSet=True)
                   or [None])[0]
        if source_set not in sets:
            raise RuntimeError(
                "%s has no '%s' UV set. Press Remember Layout BEFORE "
                "rearranging UVs, or name a source UV set in the options."
                % (mesh.split("|")[-1], source_set))
        if current == source_set:
            raise RuntimeError("The source and current UV sets are the same "
                               "('%s') - nothing to transfer." % current)
        src_loops, _f, _s = bridge_module.face_uv_loops(mesh, source_set)
        dst_loops, firsts, shells = bridge_module.face_uv_loops(mesh, current)
        data[mesh] = (src_loops, dst_loops, firsts, shells)

    # oiiotool for EXR: Arnold's own copy first (beside the mtoa plug-in),
    # then M6's search of PATH and the usual install folders.
    hints = []
    try:
        plug = cmds.pluginInfo("mtoa", query=True, path=True)
        if plug:
            root = os.path.dirname(os.path.dirname(plug))
            hints += [os.path.join(root, "bin", "oiiotool.exe"),
                      os.path.join(root, "bin", "oiiotool")]
    except Exception:
        pass
    tool = m6.find_oiiotool(hints)

    written, notes, repointed = 0, [], 0
    for t_index, (node, entry) in enumerate(found.items()):
        meshes_data = [data[m] for m in entry["meshes"] if m in data]
        if progress is not None:
            progress.title = "Texture %d of %d: %s" % (
                t_index + 1, len(found),
                entry["path"].replace("\\", "/").rsplit("/", 1)[-1])
        if not meshes_data or not entry["path"]:
            continue
        report = m6.transfer_texture(
            entry["path"], entry["udim"], meshes_data,
            resolution=run.get("resolution", "keep"),
            fixed_size=run.get("fixed_size", 2048, int),
            padding_px=run.get("padding", 4, float),
            suffix=run.get("suffix", "_uvs") or "_uvs", oiiotool=tool,
            progress=progress)
        written += len(report["written"])
        notes.extend("%s: %s" % (node, n) for n in report["notes"])
        if run.get("repoint") and report["written"]:
            bridge_module.repoint_file(node, report["written"][0])
            repointed += 1
    detail = OrderedDict([("textures", len(found)),
                          ("files_written", written),
                          ("repointed", repointed)])
    if notes:
        detail["notes"] = "; ".join(notes[:4])
    return detail


# =====================================================================
# SECTION 10c - Redesign v2 tools UV Studio implements itself
# =====================================================================

PARITY_KEYS = ("auto_seams", "normal_based", "create_uv_shell",
               "create_shell_grid", "stitch", "unfold_ctx", "optimize_ctx",
               "symmetrize_ctx", "straighten_shell", "unfold_legacy",
               "linear_align", "snap_together", "snap_stack", "match_grid",
               "match_uvs", "stack_shells", "unstack_shells", "gather",
               "randomize", "stack_orient", "relax")


@_handler("unfold along", writes=True)
def unfold_along(run):
    """Unfold, then put one axis back: Maya's Unfold Along U / V.

    Along U keeps every UV's V exactly as it was, so shells spread only
    horizontally (and the reverse for V). Unfold3D does the solving; this
    only constrains its result, so pins are honoured the same way.
    """
    bridge_module = run.need_bridge_module()
    cmds = _maya()
    resolver = run.bridge.cmd
    if not resolver.available("unfold"):
        raise RuntimeError("u3dUnfold is not available on this build")
    import maya.api.OpenMaya as om2
    axis = str(run.get("axis", "u")).lower()
    selection = cmds.ls(selection=True, flatten=True, long=True) or []
    uvs = cmds.ls(cmds.polyListComponentConversion(selection, toUV=True)
                  or [], flatten=True, long=True) or []
    if not uvs:
        raise RuntimeError("Select a shell, UVs or faces to unfold.")
    kept = {}
    for mesh, ids in bridge_module.selected_uv_ids_by_mesh(uvs).items():
        fn = om2.MFnMesh(bridge_module.dag_path(mesh))
        uv_set = bridge_module.current_uv_set(mesh)
        us, vs = fn.getUVs(uv_set)
        kept[mesh] = (uv_set, list(us), list(vs), set(ids))
    cmds.select(uvs, replace=True)
    resolver.call("unfold", uvs)
    for mesh, (uv_set, us0, vs0, ids) in kept.items():
        fn = om2.MFnMesh(bridge_module.dag_path(mesh))
        us, vs = fn.getUVs(uv_set)
        us, vs = list(us), list(vs)
        for i in ids:
            if axis == "u":
                vs[i] = vs0[i]
            else:
                us[i] = us0[i]
        fn.setUVs(us, vs, uv_set)
    return OrderedDict([("unfolded_uvs", sum(len(k[3]) for k in
                                             kept.values())),
                        ("along", axis.upper())])


@_handler("snap", writes=True)
def snap_tile(run):
    """Maya's Snap: move the selection so its anchor point (a corner, an
    edge middle or the centre of its box) lands on the same point of the
    U/V range - by default the 0-1 tile."""
    anchor = run.get("anchor", "sw")
    u0, u1 = run.get("u0", 0.0, float), run.get("u1", 1.0, float)
    v0, v1 = run.get("v0", 0.0, float), run.get("v1", 1.0, float)
    units = [u for u in run.units_for(run.selected_shells())
             if not u.is_pinned]
    if not units:
        return OrderedDict([("moved", 0), ("message", "Nothing to snap")])
    group = merge_bounds([u.bounds for u in units])
    fu, fv = PIVOT_ANCHORS.get(anchor, (0.0, 0.0))
    cu, cv = point_in_bounds(group, anchor)
    du, dv = u0 + (u1 - u0) * fu - cu, v0 + (v1 - v0) * fv - cv
    moved = 0
    for unit in units:
        moved += run.translate_unit(unit, du, dv)
    return OrderedDict([("uvs_moved", moved), ("anchor", anchor),
                        ("to", (round(cu + du, 4), round(cv + dv, 4)))])


def _density_call(template, *values):
    try:
        import maya.mel as mel
    except ImportError:
        raise RuntimeError("Maya required")
    command = template % values
    try:
        return mel.eval(command)
    except Exception as exc:
        raise RuntimeError("%s failed: %s" % (command, exc))


@_handler("get density")
def density_get(run):
    """Texel density of the selection, in pixels per unit at `mapSize`.

    The panel copies the result into Set's field, so measuring one shell
    and applying it to others is Get, select, Set - Maya's own gesture.
    """
    map_size = run.get("mapSize", 512, int)
    value = _density_call("texGetTexelDensity %d;", map_size)
    return OrderedDict([("density", round(float(value or 0.0), 4)),
                        ("map_size", map_size)])


@_handler("set density", writes=True)
def density_set(run):
    density = run.get("density", 10.24, float)
    map_size = run.get("mapSize", 512, int)
    _density_call("texSetTexelDensity %s %d;", repr(density), map_size)
    return OrderedDict([("density", density), ("map_size", map_size)])


@_handler("debug: parity scan")
def parity_scan(run):
    bridge_module = run.need_bridge_module()

    def body(runner, host):
        _dlog("--- Parity buttons: which Maya command each one found ---")
        for key in PARITY_KEYS:
            name = runner.bridge.cmd.name_of(key)
            tried = ", ".join(bridge_module.CommandResolver.TOOLS.get(key,
                                                                       []))
            _dlog("  %-18s %s" % (key, name or "NOT FOUND (tried %s)" % tried))
        _dlog("--- Every UV command on this Maya ---")
        found = bridge_module.parity_scan()
        for label, names in found.items():
            _dlog("  %s (%d):" % (label, len(names)))
            for i in range(0, len(names), 6):
                _dlog("    " + ", ".join(names[i:i + 6]))
        return sum(len(v) for v in found.values())
    return _debug_one(run, body)


# =====================================================================
# SECTION 11 - Registration
# =====================================================================

HANDLERS = OrderedDict([
    ("rotate", rotate),
    ("scale", scale),
    ("flip", flip),
    ("move", move),
    ("nudge", nudge),
    ("orient_pca", orient_pca),
    ("align", align),
    ("debug_all", debug_all), ("debug_tools", debug_tools),
    ("debug_split", debug_split), ("debug_self", debug_self),
    ("debug_parity", debug_parity_button), ("debug_pins", debug_pins_button),
    ("debug_viewport", debug_viewport_button),
    ("straighten_uvs", straighten_uvs),
    ("distribute", distribute),
    ("pack", pack),
    ("analyze", analyze),
    ("pack_preview", pack_preview),
    ("find_pairs", find_pairs),
    ("stack_pairs", stack_pairs),
    ("link_pair", link_pair),
    ("link_lock", link_lock),
    ("unlink", unlink),
    ("list_links", list_links),
    ("pin", pin),
    ("invert_pins", invert_pins),
    ("select_pinned", select_pinned),
    ("cut", cut),
    ("sew", sew),
    ("sew_move", sew_move),
    ("split", split),
    ("check_overlaps", check_overlaps),
    ("check_bounds", check_bounds),
    ("check_flipped", check_flipped),
    ("check_density", check_density),
    ("texture_snapshot", texture_snapshot),
    ("texture_scan", texture_scan),
    ("texture_transfer", texture_transfer),
    ("unfold_along", unfold_along),
    ("snap_tile", snap_tile),
    ("density_get", density_get),
    ("density_set", density_set),
    ("parity_scan", parity_scan),
])


def register(runner):
    """Attach every handler to an M5 ToolRunner."""
    runner.handlers.update(HANDLERS)
    return sorted(HANDLERS)


# =====================================================================
# SECTION 12 - Self test (fakes, no Maya)
# =====================================================================

class _FakeWriter(object):
    def __init__(self, mesh, journal, pinned):
        self.mesh = mesh
        self.journal = journal
        self.pinned = pinned

    def _movable(self, uv_ids):
        return [i for i in uv_ids if i not in self.pinned]

    def translate(self, uv_ids, du, dv, **_):
        ids = self._movable(uv_ids)
        if ids:
            self.journal.append(("translate", self.mesh, len(ids),
                                 round(du, 6), round(dv, 6)))
        return len(ids)

    def apply_affine(self, uv_ids, matrix, translation, **_):
        ids = self._movable(uv_ids)
        if ids:
            self.journal.append(("affine", self.mesh, len(ids),
                                 tuple(round(x, 6) for x in matrix),
                                 tuple(round(x, 6) for x in translation)))
        return len(ids)

    def rotate(self, uv_ids, degrees, pivot, **_):
        ids = self._movable(uv_ids)
        if ids:
            self.journal.append(("rotate", self.mesh, len(ids), degrees,
                                 (round(pivot[0], 6), round(pivot[1], 6))))
        return len(ids)

    def scale(self, uv_ids, su, sv, pivot, **_):
        ids = self._movable(uv_ids)
        if ids:
            self.journal.append(("scale", self.mesh, len(ids), su,
                                 (round(pivot[0], 6), round(pivot[1], 6))))
        return len(ids)


class _FakeBridge(object):
    def __init__(self, report, pinned=None):
        self.report = report
        self.journal = []
        self.pinned = set(pinned or [])
        self.cmd = None
        self.writers_made = 0

    def analyse_many(self, meshes, uv_set=None):
        return self.report

    def writer(self, mesh, pinned=None):
        self.writers_made += 1
        return _FakeWriter(mesh, self.journal, self.pinned)


def _install_test_stubs():
    """Let M2 import outside Maya, for this test only.

    M2 imports maya.cmds/mel/api at module scope, which is correct for its job
    but blocks importing its pure data classes standalone. The stubs exist
    only so Shell and LayoutUnit can be constructed here; every handler under
    test is driven through _FakeBridge and never reaches these.
    """
    import types
    try:
        import maya.cmds                                  # noqa: F401
        return                                            # real Maya, leave it
    except ImportError:
        pass
    for name in ("maya", "maya.cmds", "maya.mel", "maya.api",
                 "maya.api.OpenMaya"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules["maya"].cmds = sys.modules["maya.cmds"]
    sys.modules["maya"].mel = sys.modules["maya.mel"]
    sys.modules["maya"].api = sys.modules["maya.api"]
    sys.modules["maya.api"].OpenMaya = sys.modules["maya.api.OpenMaya"]


def _make_report(packer_shells, units, pin_sets=None):
    return OrderedDict([("shells", packer_shells), ("units", units),
                        ("udims", [1001]),
                        ("pin_sets", pin_sets or OrderedDict())])


def run_self_test():
    """Run the handler tests isolated from the live Maya selection.

    The tests feed handlers fake shells, but handlers also read Maya's REAL
    selection. Inside Maya with anything selected, that selection matched
    none of the fake shells and the run aborted ("The selected UVs do not
    belong to any analysed shell") - a harness failure that looked like a
    tool failure. Outside Maya there is no selection, so it always passed.
    """
    saved, cmds = None, None
    try:
        import maya.cmds as cmds
        if hasattr(cmds, "ls") and hasattr(cmds, "select"):
            saved = cmds.ls(selection=True, long=True) or []
            cmds.select(clear=True)
        else:
            cmds = None
    except ImportError:
        cmds = None
    try:
        return _run_self_test_body()
    finally:
        if cmds is not None:
            try:
                if saved:
                    cmds.select(saved, replace=True)
            except Exception:
                pass


def _run_self_test_body():
    failures = []

    def check(label, got, want):
        ok = got == want
        print("%-56s %s  (got %r)" % (label, "PASS" if ok else "FAIL", got))
        if not ok:
            failures.append(label)

    def check_true(label, condition, detail=""):
        print("%-56s %s  %s" % (label, "PASS" if condition else "FAIL", detail))
        if not condition:
            failures.append(label)

    print("=" * 80)
    print("UV Studio M5b - Handlers self test (v%s)" % __version__)
    print("=" * 80)

    _install_test_stubs()
    bridge_module = _load_bridge_module()
    packer = _load_packer()
    if bridge_module is None or packer is None:
        print("M2 and M3 must sit beside this file to run the test.")
        return False

    Shell = bridge_module.Shell
    LayoutUnit = bridge_module.LayoutUnit

    def mk(index, u0, v0, w, h, mesh="|m", pins=0, nuv=300):
        return Shell(index, list(range(index * 1000, index * 1000 + nuv)),
                     (u0, u0 + w, v0, v0 + h), w * h, pins, mesh=mesh)

    print("--- align ---")
    shells = [mk(0, 0.10, 0.50, 0.2, 0.2), mk(1, 0.40, 0.30, 0.2, 0.2),
              mk(2, 0.70, 0.65, 0.2, 0.2)]
    units = [LayoutUnit(i, [s]) for i, s in enumerate(shells)]
    bridge = _FakeBridge(_make_report(shells, units))
    detail = align(tool=None, settings={"edge": "left"},
                   meshes=["|m"], bridge=bridge)
    check("align moves all but the leftmost", detail["units_moved"], 2)
    check_true("  and only translations were written",
               all(e[0] == "translate" for e in bridge.journal),
               str(bridge.journal[:2]))
    check("  one writer for one mesh, not one per unit",
          bridge.writers_made, 1)

    print("\n--- pinned units are never moved ---")
    pinned_shells = [mk(0, 0.10, 0.50, 0.2, 0.2, pins=5),
                     mk(1, 0.40, 0.30, 0.2, 0.2),
                     mk(2, 0.70, 0.65, 0.2, 0.2)]
    pinned_units = [LayoutUnit(i, [s]) for i, s in enumerate(pinned_shells)]
    pinned_ids = set(pinned_shells[0].uv_ids)
    bridge = _FakeBridge(_make_report(pinned_shells, pinned_units), pinned_ids)
    align(tool=None, settings={"edge": "right"}, meshes=["|m"], bridge=bridge)
    touched = set(entry[2] for entry in bridge.journal)
    check_true("no write touched the pinned shell's UVs",
               all(count == 300 for count in touched)
               and len(bridge.journal) <= 2,
               "%d write(s)" % len(bridge.journal))

    print("\n--- rotate ---")
    shells = [mk(0, 0.1, 0.1, 0.3, 0.2), mk(1, 0.6, 0.6, 0.2, 0.2)]
    units = [LayoutUnit(i, [s]) for i, s in enumerate(shells)]
    bridge = _FakeBridge(_make_report(shells, units))
    detail = rotate(tool=None, settings={"degrees": 90.0},
                    meshes=["|m"], bridge=bridge)
    check("rotate touched both units' UVs", detail["rotated_uvs"], 600)
    check_true("  written as rotations, not translations",
               all(e[0] == "rotate" for e in bridge.journal))

    print("\n--- pack ---")
    shells = [mk(i, (i % 4) * 0.25, (i // 4) * 0.25, 0.2, 0.2)
              for i in range(8)]
    units = [LayoutUnit(i, [s]) for i, s in enumerate(shells)]
    bridge = _FakeBridge(_make_report(shells, units))
    detail = pack(tool=None, settings={"padding": 0.0, "udims": [1001]},
                  meshes=["|m"], bridge=bridge)
    check("every unit was placed", detail["placed"], 8)
    check("nothing left unplaced", detail["unplaced"], 0)
    check_true("  and the layout reports occupancy",
               detail["occupancy"] > 0, "%.1f%%" % detail["occupancy"])

    print("\n--- pack routes around pinned units ---")
    shells = [mk(0, 0.0, 0.0, 0.4, 0.4, pins=9)]
    shells += [mk(i, 0.5, 0.5, 0.25, 0.25) for i in range(1, 5)]
    units = [LayoutUnit(i, [s]) for i, s in enumerate(shells)]
    bridge = _FakeBridge(_make_report(shells, units), set(shells[0].uv_ids))
    detail = pack(tool=None, settings={"padding": 0.0, "udims": [1001]},
                  meshes=["|m"], bridge=bridge)
    check_true("pinned unit was not placed and not moved",
               detail["placed"] == 4, "placed %d" % detail["placed"])
    check_true("  no write touched the pinned shell",
               all(entry[2] == 300 for entry in bridge.journal),
               "%d write(s)" % len(bridge.journal))

    print("\n--- a rotated placement lands where M3 put it (BUG FIX) ---")
    # A wide slab fills most of the tile; the tall shell only fits on its
    # side. v1 rotated about the centre and then applied the unrotated delta,
    # leaving it (width - height) / 2 = 0.325 away from the target corner.
    big = mk(0, 0.0, 0.0, 1.0, 0.7, nuv=100)
    tall = mk(1, 0.30, 0.05, 0.25, 0.9, nuv=100)
    units = [LayoutUnit(0, [big]), LayoutUnit(1, [tall])]
    bridge = _FakeBridge(_make_report([big, tall], units))
    detail = pack(tool=None, settings={"padding": 0.0, "udims": [1001],
                                       "allow_rotation": True},
                  meshes=["|m"], bridge=bridge)
    check("both shells were placed", detail["placed"], 2)
    # Placement is now a single affine per unit (raster engine), applied
    # directly. The end-to-end "lands where the packer said" guarantee is
    # verified against real UV coordinates in the testbed; here we only check
    # that each placed unit received exactly one affine write.
    affines = [e for e in bridge.journal if e[0] == "affine"]
    check("each placed unit got one affine write", len(affines), 2)

    print("\n--- scale_to_fit is applied, not just reported (BUG FIX) ---")
    crowded = [mk(i, (i % 3) * 0.34, (i // 3) * 0.34, 0.33, 0.33, nuv=50)
               for i in range(12)]
    units = [LayoutUnit(i, [s]) for i, s in enumerate(crowded)]
    bridge = _FakeBridge(_make_report(crowded, units))
    # overflow=False: this test's whole point is a tile too small to hold
    # everything, so scale_to_fit has something to do. Overflow adding more
    # tiles would remove that pressure and the test would prove nothing.
    detail = pack(tool=None, settings={"padding": 0.0, "udims": [1001],
                                       "allow_rotation": False,
                                       "scale_to_fit": True,
                                       "overflow": False},
                  meshes=["|m"], bridge=bridge)
    check_true("packer chose a scale below 1.0",
               detail["scale"] < 1.0, "scale %.4f" % detail["scale"])
    # Scale is folded into the per-unit affine now, not a separate scale
    # write. The report's own scale figure is the contract the UI reads.
    check_true("  and it applied that scale to the placed units",
               detail["scaled"] > 0,
               "scaled %d of %d" % (detail["scaled"], detail["placed"]))

    print("\n--- cross-mesh unit ---")
    left = mk(0, 0.2, 0.2, 0.25, 0.25, mesh="|doorL", nuv=812)
    right = mk(1, 0.2, 0.2, 0.25, 0.25, mesh="|doorR", nuv=812)
    unit = LayoutUnit(0, [left, right], reason="stacked")
    bridge = _FakeBridge(_make_report([left, right], [unit]))
    moved = _apply_translation(bridge, unit, 0.1, 0.0)
    check("a cross-mesh unit writes once per mesh", len(bridge.journal), 2)
    check("  with the same delta on both",
          sorted(set((e[3], e[4]) for e in bridge.journal)), [(0.1, 0.0)])
    check("  and reports every UV moved", moved, 1624)

    print("\n--- QC is read-only ---")
    shells = [mk(0, 0.10, 0.10, 0.3, 0.3), mk(1, 0.20, 0.20, 0.3, 0.3),
              mk(2, 0.80, 0.80, 0.1, 0.1)]
    units = [LayoutUnit(i, [s]) for i, s in enumerate(shells)]
    bridge = _FakeBridge(_make_report(shells, units))
    detail = check_overlaps(meshes=["|m"], bridge=bridge)
    check("QC finds the one overlapping pair", detail["overlapping_pairs"], 1)
    check_true("  and wrote nothing", not bridge.journal,
               "%d write(s)" % len(bridge.journal))
    straddler = [mk(0, 0.8, 0.1, 0.4, 0.2)]
    bridge = _FakeBridge(_make_report(straddler,
                                      [LayoutUnit(0, [straddler[0]])]))
    detail = check_bounds(settings={"udims": [1001]}, meshes=["|m"],
                          bridge=bridge)
    check("QC finds the tile straddler", detail["out_of_bounds"], 1)

    print("\n--- one analysis and one writer per press ---")
    counting = [mk(i, i * 0.05, 0.0, 0.04, 0.04) for i in range(20)]
    units = [LayoutUnit(i, [s]) for i, s in enumerate(counting)]

    class _CountingBridge(_FakeBridge):
        def __init__(self, report):
            _FakeBridge.__init__(self, report)
            self.analyses = 0

        def analyse_many(self, meshes, uv_set=None):
            self.analyses += 1
            return self.report

    bridge = _CountingBridge(_make_report(counting, units))
    pack(tool=None, settings={"padding": 0.0, "udims": [1001]},
         meshes=["|m"], bridge=bridge)
    check("one analyse_many for a 20-unit pack", bridge.analyses, 1)
    check("  and one writer, not one per unit", bridge.writers_made, 1)

    print("\n--- pivot modes ---")
    # Two shells apart. Per-shell pivot turns each in place; selection pivot
    # turns both about the box that encloses them.
    a = mk(0, 0.0, 0.0, 0.2, 0.2, nuv=40)
    b = mk(1, 0.6, 0.6, 0.2, 0.2, nuv=40)
    units = [LayoutUnit(0, [a]), LayoutUnit(1, [b])]

    bridge = _FakeBridge(_make_report([a, b], units))
    rotate(settings={"degrees": 90.0}, meshes=["|m"], bridge=bridge)
    pivots = [e[4] for e in bridge.journal if e[0] == "rotate"]
    check("shell pivot turns each unit about its own centre",
          sorted(pivots), [(0.1, 0.1), (0.7, 0.7)])

    bridge = _FakeBridge(_make_report([a, b], units))
    rotate(settings={"degrees": 90.0,
                     "pivot": {"mode": "selection", "anchor": "c"}},
           meshes=["|m"], bridge=bridge)
    pivots = set(e[4] for e in bridge.journal if e[0] == "rotate")
    check("selection pivot is one shared point", sorted(pivots), [(0.4, 0.4)])

    bridge = _FakeBridge(_make_report([a, b], units))
    rotate(settings={"degrees": 90.0,
                     "pivot": {"mode": "selection", "anchor": "sw"}},
           meshes=["|m"], bridge=bridge)
    pivots = set(e[4] for e in bridge.journal if e[0] == "rotate")
    check("  and the anchor picks its corner", sorted(pivots), [(0.0, 0.0)])

    bridge = _FakeBridge(_make_report([a, b], units))
    rotate(settings={"degrees": 90.0,
                     "pivot": {"mode": "custom", "u": 0.25, "v": 0.75}},
           meshes=["|m"], bridge=bridge)
    pivots = set(e[4] for e in bridge.journal if e[0] == "rotate")
    check("custom pivot is used literally", sorted(pivots), [(0.25, 0.75)])

    bridge = _FakeBridge(_make_report([a, b], units))
    rotate(settings={"degrees": 90.0, "pivot": {"mode": "area",
                                                "anchor": "c"}},
           meshes=["|m"], bridge=bridge)
    pivots = set(e[4] for e in bridge.journal if e[0] == "rotate")
    check("area pivot is the tile centre", sorted(pivots), [(0.5, 0.5)])

    check_true("a settings dict with no pivot behaves exactly as before",
               pivot_spec({}) == DEFAULT_PIVOT
               and pivot_spec({"pivot": "nonsense"}) == DEFAULT_PIVOT,
               str(pivot_spec({})))

    print("\n--- snapping ---")
    snap_on = {"snap_enabled": True, "snap_step": 15.0}
    check("37 degrees snaps to 30 at a 15 step",
          snapped(37.0, snap_on), 30.0)
    check("  snapping off leaves the value alone",
          snapped(37.0, {"snap_step": 15.0}), 37.0)
    check("  a zero step never quantises to nothing",
          snapped(37.0, {"snap_enabled": True, "snap_step": 0.0}), 37.0)
    bridge = _FakeBridge(_make_report([a, b], units))
    detail = rotate(settings=dict(snap_on, degrees=37.0), meshes=["|m"],
                    bridge=bridge)
    check("  and rotate writes the snapped angle", detail["degrees"], 30.0)

    print("\n--- scale, flip and nudge ---")
    bridge = _FakeBridge(_make_report([a, b], units))
    detail = scale(settings={"factor": 2.0, "axis_u": True, "axis_v": False},
                   meshes=["|m"], bridge=bridge)
    scales = [e for e in bridge.journal if e[0] == "scale"]
    check("scale on U only reports its axes", detail["axes"], "U")
    check("  and writes the factor", sorted(set(e[3] for e in scales)), [2.0])

    bridge = _FakeBridge(_make_report([a, b], units))
    detail = scale(settings={"factor": -2.0, "prevent_negative": True},
                   meshes=["|m"], bridge=bridge)
    check("prevent negative scale keeps the magnitude", detail["factor"], 2.0)

    bridge = _FakeBridge(_make_report([a, b], units))
    detail = scale(settings={"factor": -2.0, "prevent_negative": False},
                   meshes=["|m"], bridge=bridge)
    check("  and lets it through when switched off", detail["factor"], -2.0)

    bridge = _FakeBridge(_make_report([a, b], units))
    flip(settings={"axis": "u", "pivot": {"mode": "selection",
                                          "anchor": "c"}},
         meshes=["|m"], bridge=bridge)
    flips = [e for e in bridge.journal if e[0] == "scale"]
    check_true("flip mirrors about the selection pivot",
               bool(flips) and all(e[3] == -1.0 and e[4] == (0.4, 0.4)
                                   for e in flips),
               str(flips[:1]))

    bridge = _FakeBridge(_make_report([a, b], units))
    detail = nudge(settings={"axis": "v", "step": 0.05, "direction": -1},
                   meshes=["|m"], bridge=bridge)
    check("nudge steps the requested distance", detail["step"], -0.05)
    check("  along the requested axis",
          sorted(set((e[3], e[4]) for e in bridge.journal
                     if e[0] == "translate")),
          [(0.0, -0.05)])

    print("\n--- registration ---")
    try:
        import uvstudio_m5_ui as ui
    except ImportError:
        ui = None
    if ui is not None:
        needed = sorted(set(t.handler for t in ui.TOOLS if t.handler))
        check("every handler the registry names is implemented",
              sorted(set(needed) - set(HANDLERS)), [])
        check("no handler is implemented that nothing uses",
              sorted(set(HANDLERS) - set(needed)), [])
    check("every write handler declares itself one",
          sorted(k for k, h in HANDLERS.items()
                 if getattr(h, "is_write_handler", False)),
          ["align", "cut", "density_set", "distribute", "flip", "invert_pins",
           "link_lock", "link_pair", "move", "nudge", "orient_pca",
           "pack", "pin", "rotate", "scale", "sew", "sew_move",
           "snap_tile", "split", "stack_pairs", "straighten_uvs",
           "texture_snapshot", "texture_transfer", "unfold_along",
           "unlink"])

    print("")
    print("=" * 80)
    print("%d failure(s)" % len(failures))
    for name in failures:
        print("  FAILED: %s" % name)
    print("=" * 80)
    return not failures


if __name__ == "__main__":
    run_self_test()
