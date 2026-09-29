"""
UV Studio - Module 9: Cluster Map
=================================

PURPOSE
    UV Studio's own UV view. Shells drawn as units, every UDIM visible at
    once, with drag to move, corner handles to scale, a rotate handle,
    smart guides, selection-order badges, and alt-drag to pair one shell
    with another.

THE SPLIT THAT MATTERS
    This file is in two halves, and the line between them is deliberate.

    SECTIONS 1-5 are pure: coordinate transforms, hit testing, the drag
    state machine, guides. No Qt, no Maya, no painting. Every rule an
    interaction follows lives here, so all of it is testable on any Python
    and all of it IS tested in uvstudio_testbed.py.

    SECTION 6 is the Qt canvas: painting and event plumbing, which turns
    mouse events into calls on the pure half. It is NOT tested, and cannot
    be here - painting, focus, cursor shape and mouse capture need a real
    Qt event loop and a real window. Treat Section 6 as unverified.

    The split exists because "the drag moved the wrong shell" and "the drag
    painted in the wrong place" are different bugs, and only the second one
    needs a machine to find.

A DRAG NEVER WRITES
    The state machine's commit() returns a COMMAND - a tool id and its
    settings - which the caller hands to M5's ToolRunner. The canvas has no
    write path of its own. So a drag is undone by the same Ctrl+Z as the
    button, recorded in the same recipe, and guarded by the same selection
    rules. A canvas that edited UVs directly would be a second way to
    change the scene, with its own bugs and its own undo behaviour.

ALT-DRAG TO PAIR
    Hold Alt, press on one shell, release on another: the map emits
    link_pair with the two shells' anchors. The gesture knows nothing about
    what a link is - M2 owns that - and the command it emits is the same
    one the Groups tab button sends.

Target: Python 3.7+ (Maya 2022), PySide2 and PySide6
"""

from __future__ import annotations

import math
import sys
from collections import OrderedDict

__version__ = "1.15.0"
MODULE_ID = "M9"

QT_BINDING = None
QtCore = QtGui = QtWidgets = None
try:
    from PySide6 import QtCore, QtGui, QtWidgets          # noqa: F401
    QT_BINDING = "PySide6"
except ImportError:
    try:
        from PySide2 import QtCore, QtGui, QtWidgets      # noqa: F401
        QT_BINDING = "PySide2"
    except ImportError:
        pass


# =====================================================================
# SECTION 1 - Coordinate space
# =====================================================================

class MapView(object):
    """UV space to pixels and back.

    V POINTS UP, Y POINTS DOWN. Every bug in a UV canvas that renders
    everything upside down, or makes a drag move the wrong way vertically,
    is this one flip applied an even or odd number of times. It is applied
    exactly once, here, and nothing else in this module touches it.
    """

    def __init__(self, width=800, height=800, centre=(0.5, 0.5), scale=600.0):
        self.width = float(width)
        self.height = float(height)
        self.centre_u, self.centre_v = centre
        self.scale = float(scale)        # pixels per UV unit

    def to_pixels(self, u, v):
        return ((u - self.centre_u) * self.scale + self.width * 0.5,
                self.height * 0.5 - (v - self.centre_v) * self.scale)

    def to_uv(self, x, y):
        return (self.centre_u + (x - self.width * 0.5) / self.scale,
                self.centre_v + (self.height * 0.5 - y) / self.scale)

    def rect_pixels(self, bounds):
        """(x, y, w, h) for a (u_min, u_max, v_min, v_max) box.

        Built from the box's TOP-LEFT in pixels, which is (u_min, v_max) in
        UV, because of the flip above.
        """
        u_min, u_max, v_min, v_max = bounds
        x, y = self.to_pixels(u_min, v_max)
        return (x, y, (u_max - u_min) * self.scale,
                (v_max - v_min) * self.scale)

    def pan_pixels(self, dx, dy):
        self.centre_u -= dx / self.scale
        self.centre_v += dy / self.scale

    def zoom_at(self, x, y, factor, minimum=20.0, maximum=40000.0):
        """Zoom keeping the UV point under the cursor under the cursor.

        Zooming about the view centre instead is the thing that makes a
        canvas feel broken: the point being examined slides away exactly
        when it is being examined.
        """
        anchor_u, anchor_v = self.to_uv(x, y)
        self.scale = max(minimum, min(maximum, self.scale * factor))
        new_u, new_v = self.to_uv(x, y)
        self.centre_u += anchor_u - new_u
        self.centre_v += anchor_v - new_v

    def fit(self, bounds, margin=0.08):
        """Frame a UV box. A degenerate box still yields a usable view."""
        u_min, u_max, v_min, v_max = bounds
        span_u = max(u_max - u_min, 1.0e-4)
        span_v = max(v_max - v_min, 1.0e-4)
        self.centre_u = (u_min + u_max) * 0.5
        self.centre_v = (v_min + v_max) * 0.5
        self.scale = min(self.width / (span_u * (1.0 + margin * 2)),
                         self.height / (span_v * (1.0 + margin * 2)))
        return self


def tiles_in_view(view):
    """The UDIM tiles the view currently covers, as (u, v) integer corners.

    Capped: a zoomed-out view can span thousands of tiles, and drawing all
    of them costs more than it tells anyone.
    """
    u0, v1 = view.to_uv(0, 0)
    u1, v0 = view.to_uv(view.width, view.height)
    tiles = []
    for tv in range(int(math.floor(v0)), int(math.floor(v1)) + 1):
        for tu in range(int(math.floor(u0)), int(math.floor(u1)) + 1):
            tiles.append((tu, tv))
            if len(tiles) >= 400:
                return tiles
    return tiles


def udim_number(tile_u, tile_v):
    return 1001 + tile_u + tile_v * 10


# =====================================================================
# SECTION 2 - What is drawn
# =====================================================================

class MapUnit(object):
    """One layout unit as the map sees it: a box, a name, and some flags.

    Deliberately NOT a Shell or a LayoutUnit. The map needs a rectangle and
    an identity; giving it the real object would let painting code reach
    into UV arrays, and the first time someone did that the canvas would
    own scene state it has no business owning.
    """

    __slots__ = ("key", "bounds", "label", "pinned", "reason", "anchor",
                 "shell_count", "polys", "family", "parts")

    def __init__(self, key, bounds, label="", pinned=False, reason="single",
                 anchor=None, shell_count=1, polys=None, family=None):
        # polys: the unit's real UV face loops, [((u,v), (u,v), ...), ...].
        # Drawn instead of the box, because a box cannot show which shells
        # are copies of each other - the thing the map exists to show.
        # family: 1-based duplicate-family number (P1, P2...), or None.
        self.polys = polys or []
        self.family = family
        # [(shell key, loops)] - the same faces as `polys`, split by shell,
        # so the canvas can cache a drawn outline PER SHELL and reuse it
        # across reloads. The key includes the shell's bounds, so a shell
        # that moved gets a new key and is rebuilt; everything else is not.
        self.parts = []
        self.key = key
        self.bounds = tuple(bounds)
        self.label = label
        self.pinned = bool(pinned)
        self.reason = reason
        self.anchor = anchor            # (mesh, uv id), for links
        self.shell_count = shell_count

    @property
    def area(self):
        return ((self.bounds[1] - self.bounds[0])
                * (self.bounds[3] - self.bounds[2]))

    @property
    def centre(self):
        return ((self.bounds[0] + self.bounds[1]) * 0.5,
                (self.bounds[2] + self.bounds[3]) * 0.5)

    def contains(self, u, v):
        return (self.bounds[0] <= u <= self.bounds[1]
                and self.bounds[2] <= v <= self.bounds[3])


def duplicate_families(shells, bridge_module):
    """{id(shell): family number} for mirrored/repeated shells sitting apart.

    Uses M2's own detector, so the map labels exactly the pairs Stack Pairs
    would stack - one definition of "pair", not a second guess in the map.
    """
    out = {}
    if bridge_module is None:
        return out
    try:
        _stacked, paired = bridge_module.classify_duplicates(shells)
    except Exception:
        return out
    for number, family in enumerate(paired, 1):
        for shell in family:
            out[id(shell)] = number
    return out


def unit_polys(unit, coords):
    """A unit's UV faces from {mesh: (us, vs)}, as drawn loops.

    The mesh's own face loops (quads, n-gons) when M2 supplies them, so the
    map shows the artist's edges. Triangles only as a fallback: fan
    diagonals are the packer's working data, not something to look at.
    """
    polys = []
    for shell in unit.shells:
        uv = coords.get(shell.mesh)
        loops = getattr(shell, "faces", None) or getattr(shell, "tris", None)
        if not uv or not loops:
            continue
        us, vs = uv
        n = min(len(us), len(vs))
        for loop in loops:
            if all(i < n for i in loop):
                polys.append(tuple((us[i], vs[i]) for i in loop))
    return polys


def shell_part(shell, coords):
    """(key, loops) for one shell. The key changes when the shell moves."""
    uv = coords.get(shell.mesh)
    loops_ids = getattr(shell, "faces", None) or getattr(shell, "tris", None)
    loops = []
    if uv and loops_ids:
        us, vs = uv
        n = min(len(us), len(vs))
        for loop in loops_ids:
            if all(i < n for i in loop):
                loops.append(tuple((us[i], vs[i]) for i in loop))
    key = (shell.mesh, min(shell.uv_ids) if shell.uv_ids else -1,
           round(shell.u_min, 6), round(shell.u_max, 6),
           round(shell.v_min, 6), round(shell.v_max, 6), len(loops))
    return key, loops


def units_from_report(report, bridge_module=None, coords=None, families=None):
    """Build the map's units from M2's analysis.

    The anchor carried here is the same one M2 uses to name a shell in a
    link, so an alt-drag can emit a link without the canvas knowing how
    shells are identified.
    """
    units = []
    for unit in report.get("units", []):
        shells = unit.shells
        anchor = None
        if bridge_module is not None and shells:
            try:
                anchor = bridge_module.shell_anchor(shells[0])
            except Exception:
                anchor = None
        family = None
        for shell in shells:
            family = (families or {}).get(id(shell))
            if family:
                break
        parts = [shell_part(shell, coords or {}) for shell in shells]
        map_unit = MapUnit(
            key=unit.index, bounds=unit.bounds,
            label=str(unit.index), pinned=unit.is_pinned,
            reason=unit.reason, anchor=anchor, shell_count=len(shells),
            polys=[loop for _k, loops in parts for loop in loops],
            family=family)
        map_unit.parts = parts
        units.append(map_unit)
    return units


def point_in_loop(u, v, loop):
    """Even-odd ray test: is (u, v) inside the polygon `loop`?"""
    inside = False
    j = len(loop) - 1
    for i in range(len(loop)):
        ui, vi = loop[i]
        uj, vj = loop[j]
        if (vi > v) != (vj > v):
            cross = ui + (v - vi) * (uj - ui) / ((vj - vi) or 1e-30)
            if u < cross:
                inside = not inside
        j = i
    return inside


def unit_contains_point(unit, u, v):
    """Is (u, v) on one of the unit's actual FACES - not just in its box?

    THE BUG THIS FIXES
        Hit-testing by bounding box picked whichever shell's BOX covered the
        click, and on a packed sheet boxes overlap everywhere: a thin
        diagonal strip's box covers half a tile. Clicking one shell selected
        another, and the drag moved it - "random UVs on other shells". The
        box is now only a cheap pre-check; the faces decide.
    """
    if not unit.contains(u, v):
        return False
    if not unit.polys:
        return True             # no geometry supplied: the box is all we have
    for loop in unit.polys:
        if len(loop) < 3:
            continue
        us = [p[0] for p in loop]
        if u < min(us) or u > max(us):
            continue
        vs = [p[1] for p in loop]
        if v < min(vs) or v > max(vs):
            continue
        if point_in_loop(u, v, loop):
            return True
    return False


def hit_test(units, u, v):
    """The unit whose FACES are under a UV point; smallest first.

    Smallest-first still matters where shells genuinely overlap (a stacked
    pair, a trim laid over a panel): the thing you can see on top is the
    thing you get.
    """
    candidates = [unit for unit in units if unit_contains_point(unit, u, v)]
    if not candidates:
        return None
    return min(candidates, key=lambda unit: unit.area)


def marquee_hits(units, u0, v0, u1, v1, enclose=False):
    """Units a rubber band touches, or encloses when `enclose` is set.

    "Touches" means the band covers some of the unit's actual UVs (or the
    band sits inside one of its faces) - not merely that the two boxes
    overlap, which grabbed shells nowhere near the band.
    """
    lo_u, hi_u = min(u0, u1), max(u0, u1)
    lo_v, hi_v = min(v0, v1), max(v0, v1)
    found = []
    for unit in units:
        a_u, b_u, a_v, b_v = unit.bounds
        if enclose:
            if a_u >= lo_u and b_u <= hi_u and a_v >= lo_v and b_v <= hi_v:
                found.append(unit)
            continue
        if b_u < lo_u or a_u > hi_u or b_v < lo_v or a_v > hi_v:
            continue                    # boxes apart: cannot touch
        if not unit.polys:
            found.append(unit)
            continue
        if any(lo_u <= pu <= hi_u and lo_v <= pv <= hi_v
               for loop in unit.polys for pu, pv in loop):
            found.append(unit)
        elif unit_contains_point(unit, (lo_u + hi_u) * 0.5,
                                 (lo_v + hi_v) * 0.5):
            found.append(unit)          # a small band inside one big face
    return found


HANDLES = ("sw", "se", "nw", "ne")


def handle_at(unit, view, x, y, tolerance=9.0):
    """Which corner handle, or "rotate", is under a pixel. None otherwise.

    Tested in PIXELS, not UV. A tolerance in UV units would make handles
    enormous when zoomed out and unclickable when zoomed in, which is the
    opposite of what a handle is for.
    """
    u_min, u_max, v_min, v_max = unit.bounds
    corners = {"sw": (u_min, v_min), "se": (u_max, v_min),
               "nw": (u_min, v_max), "ne": (u_max, v_max)}
    for name, (cu, cv) in corners.items():
        px, py = view.to_pixels(cu, cv)
        if abs(px - x) <= tolerance and abs(py - y) <= tolerance:
            return name
    # The rotate handle sits above the top edge, at a fixed PIXEL offset, so
    # it never collides with the corners however small the unit is drawn.
    mid_u = (u_min + u_max) * 0.5
    px, py = view.to_pixels(mid_u, v_max)
    if abs(px - x) <= tolerance and abs((py - 22.0) - y) <= tolerance:
        return "rotate"
    return None


# =====================================================================
# SECTION 3 - Smart guides
# =====================================================================

def guides_for(moving, others, threshold_uv):
    """Alignment lines a dragged unit is close to, and the snap it implies.

    Returns (vertical lines, horizontal lines, (du, dv)). The snap is the
    SMALLEST correction on each axis, so a unit near two guides takes the
    nearer one rather than the last one checked.
    """
    m_u0, m_u1, m_v0, m_v1 = moving
    m_cu, m_cv = (m_u0 + m_u1) * 0.5, (m_v0 + m_v1) * 0.5

    vertical, horizontal = [], []
    best_du = best_dv = None

    for other in others:
        o_u0, o_u1, o_v0, o_v1 = other
        o_cu, o_cv = (o_u0 + o_u1) * 0.5, (o_v0 + o_v1) * 0.5
        for mine, theirs in ((m_u0, o_u0), (m_u1, o_u1), (m_cu, o_cu),
                             (m_u0, o_u1), (m_u1, o_u0)):
            delta = theirs - mine
            if abs(delta) <= threshold_uv:
                vertical.append(theirs)
                if best_du is None or abs(delta) < abs(best_du):
                    best_du = delta
        for mine, theirs in ((m_v0, o_v0), (m_v1, o_v1), (m_cv, o_cv),
                             (m_v0, o_v1), (m_v1, o_v0)):
            delta = theirs - mine
            if abs(delta) <= threshold_uv:
                horizontal.append(theirs)
                if best_dv is None or abs(delta) < abs(best_dv):
                    best_dv = delta

    return (sorted(set(vertical)), sorted(set(horizontal)),
            (best_du or 0.0, best_dv or 0.0))


# =====================================================================
# SECTION 4 - The drag state machine
# =====================================================================

MOVE, SCALE, ROTATE, MARQUEE, LINK, PAN = ("move", "scale", "rotate",
                                           "marquee", "link", "pan")


class Drag(object):
    """One gesture in progress, from press to release.

    The rules an interaction follows all live here rather than in the
    canvas's event handlers, because that is what makes them testable: a
    press, two moves and a release is three method calls and an assertion,
    with no window involved.
    """

    def __init__(self, mode, unit=None, handle=None, start_uv=(0.0, 0.0),
                 start_bounds=None):
        self.mode = mode
        self.unit = unit
        self.handle = handle
        self.start_u, self.start_v = start_uv
        self.start_bounds = start_bounds
        self.current_u, self.current_v = start_uv
        self.guides = ([], [])
        self.snap = (0.0, 0.0)
        self.target = None              # for LINK: the unit under the cursor
        self.cancelled = False

    # -- geometry ------------------------------------------------------
    @property
    def delta(self):
        return (self.current_u - self.start_u + self.snap[0],
                self.current_v - self.start_v + self.snap[1])

    @property
    def raw_delta(self):
        return (self.current_u - self.start_u, self.current_v - self.start_v)

    def scale_factor(self, uniform=True):
        """Factor implied by dragging a corner, measured against the
        OPPOSITE corner, which is what stays put while a corner is pulled."""
        if self.start_bounds is None or not self.handle:
            return (1.0, 1.0)
        u_min, u_max, v_min, v_max = self.start_bounds
        pivot_u = u_max if "w" in self.handle else u_min
        pivot_v = v_max if self.handle.startswith("s") else v_min
        span_u = (self.start_u - pivot_u) or 1.0e-9
        span_v = (self.start_v - pivot_v) or 1.0e-9
        su = (self.current_u - pivot_u) / span_u
        sv = (self.current_v - pivot_v) / span_v
        if uniform:
            # Sign from the larger movement, magnitude shared, so a corner
            # drag cannot mirror on one axis only and silently flip a shell.
            factor = su if abs(su) > abs(sv) else sv
            return (factor, factor)
        return (su, sv)

    def angle(self, pivot):
        """Degrees turned about a pivot since the press."""
        a0 = math.atan2(self.start_v - pivot[1], self.start_u - pivot[0])
        a1 = math.atan2(self.current_v - pivot[1], self.current_u - pivot[0])
        return math.degrees(a1 - a0)


class MapController(object):
    """Turns presses, moves and releases into commands for M5's runner."""

    SNAP_PIXELS = 7.0

    def __init__(self, view=None, units=None):
        self.view = view or MapView()
        self.units = list(units or [])
        self.selection = []             # unit keys, in the order picked
        self.drag = None
        self.snapping = True

    # -- selection ------------------------------------------------------
    def select(self, keys, add=False):
        if add:
            for key in keys:
                if key not in self.selection:
                    self.selection.append(key)
        else:
            self.selection = list(keys)
        return self.selection

    def badge_of(self, key):
        """1-based pick order, or None. Shown on the unit so an artist can
        see which shell an order-sensitive tool will treat as the key."""
        return (self.selection.index(key) + 1
                if key in self.selection else None)

    def unit_by_key(self, key):
        for unit in self.units:
            if unit.key == key:
                return unit
        return None

    def selected_units(self):
        return [u for u in self.units if u.key in self.selection]

    # -- gesture --------------------------------------------------------
    def press(self, x, y, alt=False, shift=False, middle=False):
        """Decide what this gesture is. Order matters and is not arbitrary:

        middle      pan, always, whatever is underneath
        alt on unit link - the pairing arrow
        handle      scale or rotate, before move, or a handle on a selected
                    unit could never be grabbed
        unit        move
        empty       marquee
        """
        u, v = self.view.to_uv(x, y)

        if middle:
            self.drag = Drag(PAN, start_uv=(u, v))
            return self.drag

        hit = hit_test(self.units, u, v)

        if alt:
            # The arrow needs somewhere to start. Alt on empty space is not
            # a link with no source; it is nothing, and doing nothing is the
            # honest response.
            self.drag = Drag(LINK, unit=hit, start_uv=(u, v)) if hit else None
            return self.drag

        for unit in self.selected_units():
            handle = handle_at(unit, self.view, x, y)
            if handle == "rotate":
                self.drag = Drag(ROTATE, unit=unit, start_uv=(u, v),
                                 start_bounds=unit.bounds)
                return self.drag
            if handle:
                self.drag = Drag(SCALE, unit=unit, handle=handle,
                                 start_uv=(u, v), start_bounds=unit.bounds)
                return self.drag

        if hit is not None:
            if hit.key not in self.selection:
                self.select([hit.key], add=shift)
            self.drag = Drag(MOVE, unit=hit, start_uv=(u, v),
                             start_bounds=hit.bounds)
            return self.drag

        if not shift:
            self.select([])
        self.drag = Drag(MARQUEE, start_uv=(u, v))
        return self.drag

    def move(self, x, y):
        if self.drag is None:
            return None
        u, v = self.view.to_uv(x, y)

        if self.drag.mode == PAN:
            self.view.centre_u -= u - self.drag.start_u
            self.view.centre_v -= v - self.drag.start_v
            return self.drag

        self.drag.current_u, self.drag.current_v = u, v

        if self.drag.mode == LINK:
            # The arrow's head is whatever is under the cursor, excluding
            # its own tail: a link from a shell to itself is not a pair.
            target = hit_test(self.units, u, v)
            self.drag.target = (target if (target is not None
                                           and self.drag.unit is not None
                                           and target.key != self.drag.unit.key)
                                else None)
            return self.drag

        if self.drag.mode == MOVE and self.snapping and self.drag.unit:
            threshold = self.SNAP_PIXELS / self.view.scale
            du, dv = self.drag.raw_delta
            u0, u1, v0, v1 = self.drag.start_bounds
            moving = (u0 + du, u1 + du, v0 + dv, v1 + dv)
            others = [x.bounds for x in self.units
                      if x.key != self.drag.unit.key]
            vertical, horizontal, snap = guides_for(moving, others, threshold)
            self.drag.guides = (vertical, horizontal)
            self.drag.snap = snap
        return self.drag

    def cancel(self):
        if self.drag is not None:
            self.drag.cancelled = True
        drag, self.drag = self.drag, None
        return drag

    def release(self, x=None, y=None):
        """End the gesture and return the command it means, or None.

        A command is {"tool": id, "settings": {...}}. The controller never
        touches UVs: the caller hands this to M5's ToolRunner, so a drag is
        undone, recorded and guarded exactly like a button press.
        """
        drag, self.drag = self.drag, None
        if drag is None or drag.cancelled:
            return None
        if x is not None and y is not None and drag.mode != PAN:
            u, v = self.view.to_uv(x, y)
            drag.current_u, drag.current_v = u, v

        if drag.mode == PAN:
            return None

        if drag.mode == MARQUEE:
            hits = marquee_hits(self.units, drag.start_u, drag.start_v,
                                drag.current_u, drag.current_v)
            self.select([unit.key for unit in hits], add=True)
            return None

        if drag.mode == LINK:
            target = hit_test(self.units, drag.current_u, drag.current_v)
            if (target is None or drag.unit is None
                    or target.key == drag.unit.key):
                return None
            if drag.unit.anchor is None or target.anchor is None:
                return None
            return {"tool": "link_pair",
                    "settings": {"anchors": [list(drag.unit.anchor),
                                             list(target.anchor)]}}

        if drag.mode == MOVE:
            du, dv = drag.delta
            if abs(du) < 1.0e-9 and abs(dv) < 1.0e-9:
                return None             # a click, not a drag
            return {"tool": "move", "settings": {"u": du, "v": dv}}

        if drag.mode == SCALE:
            su, _sv = drag.scale_factor(uniform=True)
            if abs(su - 1.0) < 1.0e-9:
                return None
            return {"tool": "scale",
                    "settings": {"factor": su, "axis_u": True,
                                 "axis_v": True}}

        if drag.mode == ROTATE and drag.unit is not None:
            degrees = drag.angle(drag.unit.centre)
            if abs(degrees) < 1.0e-6:
                return None
            return {"tool": "rotate", "settings": {"degrees": degrees}}

        return None


# =====================================================================
# SECTION 5 - Colours
# =====================================================================

COLOURS = OrderedDict([
    ("background", "#171717"),
    ("grid", "#2a2a2a"),
    ("tile", "#3a3a3a"),
    ("label", "#5f5f5f"),
    ("unit", "#3f6d8a"),
    ("unit_selected", "#6fa8cc"),
    ("pinned", "#c98b3a"),
    ("linked", "#7a5ea8"),
    ("guide", "#c25b8a"),
    ("arrow", "#e0b050"),
])


# Distinct hues, one per duplicate family (and per unit when unpaired), so the
# two halves of a pair read as the same colour at a glance.
FAMILY_HUES = ["#7fbf3f", "#b04fb8", "#d13fa0", "#2bc4a0", "#4fb4e0",
               "#a07a4a", "#3f9f6f", "#b8e04a", "#3a6fe0", "#e0903f",
               "#5fa0a0", "#c05060"]


UNPAIRED_HUE = "#5b7a90"


def hue_for(unit):
    """A pair's colour, or neutral slate for anything unpaired.

    Colour means "these belong together" and nothing else. Giving singles
    colours from the same palette made an unpaired shell the same green as
    pair P1 - a picture that asserts a pairing that does not exist.
    """
    if not unit.family:
        return UNPAIRED_HUE
    return FAMILY_HUES[(unit.family - 1) % len(FAMILY_HUES)]


def colour_for(unit, selected=False):
    """Pinned beats linked beats selected beats plain.

    Pinned first because it is the only one of the four that changes what a
    tool will DO to the unit; the others only say how it was picked.
    """
    if unit.pinned:
        return COLOURS["pinned"]
    if unit.reason in ("linked", "locked"):
        return COLOURS["linked"]
    return COLOURS["unit_selected"] if selected else COLOURS["unit"]


# =====================================================================
# SECTION 5b - Pivot placement (pure; the Edit Pivot widget builds on this)
# =====================================================================

class PivotPlacement(object):
    """The interaction behind Edit Pivot: click in a small UV view to set a
    custom pivot point, drag to nudge it, snap to shell features.

    Pure, so the hit-testing and snapping are tested off Qt. The widget in
    M5c owns only painting and mouse events; every rule an artist can feel -
    where the pivot lands, what it snaps to - lives here.

    Snap targets are the corners, edge midpoints and centre of the selection
    bounds, because those are the points an artist actually wants a pivot on
    and typing them by hand is exactly what Edit Pivot exists to avoid.
    """

    def __init__(self, view=None, bounds=None):
        self.view = view or MapView(220, 220, (0.5, 0.5), 180.0)
        self.bounds = bounds            # (u_min, u_max, v_min, v_max) or None
        if bounds:
            self.view.fit(bounds, margin=0.25)
        self.u, self.v = self._default_point()

    def _default_point(self):
        if self.bounds:
            return ((self.bounds[0] + self.bounds[1]) * 0.5,
                    (self.bounds[2] + self.bounds[3]) * 0.5)
        return (0.5, 0.5)

    def snap_targets(self):
        """The nine handle points of the selection box, if there is one."""
        if not self.bounds:
            return []
        u0, u1, v0, v1 = self.bounds
        mu, mv = (u0 + u1) * 0.5, (v0 + v1) * 0.5
        return [(u0, v0), (mu, v0), (u1, v0),
                (u0, mv), (mu, mv), (u1, mv),
                (u0, v1), (mu, v1), (u1, v1)]

    def set_from_pixel(self, x, y, snap_px=10.0):
        """Place the pivot at a pixel, snapping to a nearby handle point.

        Snap distance is in PIXELS so it feels the same at every zoom - a UV
        threshold would make handles uncatchable when zoomed out and sticky
        when zoomed in.
        """
        u, v = self.view.to_uv(x, y)
        best = None
        for tu, tv in self.snap_targets():
            px, py = self.view.to_pixels(tu, tv)
            d = ((px - x) ** 2 + (py - y) ** 2) ** 0.5
            if d <= snap_px and (best is None or d < best[0]):
                best = (d, tu, tv)
        self.u, self.v = (best[1], best[2]) if best else (u, v)
        return (self.u, self.v)

    def point(self):
        return (self.u, self.v)


# =====================================================================
# SECTION 6 - The Qt canvas  (NOT TESTED - needs a real event loop)
# =====================================================================

_Widget = QtWidgets.QWidget if QtWidgets is not None else object


class LoadCancelled(Exception):
    """Raised from a progress callback when the artist presses Cancel."""


class LoadProgress(object):
    """A progress bar for loading, shown only if loading is slow.

    Callable as progress(index, total, text). It appears after ~250 ms, so
    quick loads never flash a dialog, and it keeps Maya's UI responsive by
    processing events between meshes. Pressing Cancel raises LoadCancelled
    at the next mesh boundary - before anything has been written.
    """

    def __init__(self, parent, title="Loading UVs"):
        self.parent = parent
        self.title = title
        self.dialog = None

    def __call__(self, index, total, text=""):
        if QtWidgets is None:
            return
        if self.dialog is None:
            dialog = QtWidgets.QProgressDialog(self.title, "Cancel", 0,
                                               max(total, 1), self.parent)
            dialog.setWindowTitle("UV Studio")
            dialog.setMinimumDuration(250)
            dialog.setAutoClose(False)
            dialog.setAutoReset(False)
            self.dialog = dialog
        self.dialog.setMaximum(max(total, 1))
        self.dialog.setValue(index)
        short = str(text).split("|")[-1]
        self.dialog.setLabelText("%s\n%d of %d  %s"
                                 % (self.title, index + 1, max(total, 1),
                                    short))
        QtWidgets.QApplication.processEvents()
        if self.dialog.wasCanceled():
            raise LoadCancelled()

    def close(self):
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None


class ClusterMap(_Widget):
    """Paints the map and forwards mouse events to MapController.

    UNVERIFIED. Everything below needs a Qt event loop and a window: paint
    order, cursor shape, focus, wheel deltas and modifier reporting all
    differ between PySide2 and PySide6 and between platforms. The rules the
    interactions follow are in Sections 1-5 and are tested; this is the
    plumbing that reaches them, and it is the part most likely to be wrong
    on first run in Maya.
    """

    def __init__(self, runner=None, bridge=None, parent=None):
        super(ClusterMap, self).__init__(parent)
        self.runner = runner
        self.bridge = bridge
        self.controller = MapController()
        self.last_error = None
        self.pair_mode = False
        self.texture_mode = "off"
        self._tex_tiles = {}
        self.setMouseTracking(True)
        self.setMinimumSize(360, 360)
        self.setFocusPolicy(
            self._enum(QtCore.Qt, "FocusPolicy.StrongFocus", "StrongFocus"))

    @staticmethod
    def _enum(owner, *names):
        for dotted in names:
            obj = owner
            for part in dotted.split("."):
                if not hasattr(obj, part):
                    obj = None
                    break
                obj = getattr(obj, part)
            if obj is not None:
                return obj
        raise AttributeError(str(names))

    # -- data ------------------------------------------------------------
    def load(self, report, bridge_module=None, keep_view=False, progress=None):
        # A reload after an edit must keep the bridge module it was first
        # given. Reloading without it lost the UV coordinates (shells fell
        # back to boxes) and the shell identities (the next drag could not
        # select anything and stuck).
        if bridge_module is None:
            bridge_module = getattr(self, "bridge_module", None)
        self._meshes = sorted(set(sh.mesh for u in report.get("units", [])
                                  for sh in u.shells))
        # Real UVs, read once per mesh per load. Drawing needs coordinates the
        # analysis report does not carry.
        # The analysis already read every mesh's UVs; reuse them. Reading
        # them again here doubled the load time on big groups.
        coords = dict(report.get("coords") or {})
        missing = sorted(set(sh.mesh for u in report.get("units", [])
                             for sh in u.shells) - set(coords))
        if bridge_module is not None:
            for index, mesh in enumerate(missing):
                if progress is not None:
                    progress(index, len(missing), mesh)
                try:
                    _sh, us, vs = bridge_module.extract_shells(mesh)
                    coords[mesh] = (us, vs)
                except Exception:
                    pass
        families = duplicate_families(report.get("shells", []), bridge_module)
        # Remember the selection by SHELL IDENTITY (anchor), not by unit
        # index. Indices are rebuilt on every load and shift whenever the
        # grouping changes (a pair, an unlink); a selection kept by index
        # then pointed at other shells, and the next drag moved them.
        kept = [u.anchor for u in self.controller.selected_units()
                if u.anchor is not None]
        self.controller.units = units_from_report(report, bridge_module,
                                                  coords, families)
        self.controller.selection = [u.key for u in self.controller.units
                                     if u.anchor is not None
                                     and u.anchor in kept]
        self._hover_key = None
        self._paths = {}
        # Keep per-shell paths that are still in use; drop the rest.
        live = set(k for u in self.controller.units for k, _l in u.parts)
        cache = getattr(self, "_part_paths", {}) or {}
        self._part_paths = dict((k, v) for k, v in cache.items() if k in live)
        self.bridge_module = bridge_module
        # UVs per map unit, per mesh. The drag commands carry only an offset
        # and the handlers act on MAYA's selection, so before a drag's
        # command runs, Maya's selection is set to exactly these UVs.
        self._unit_uvs = {}
        for unit in report.get("units", []):
            try:
                self._unit_uvs[unit.index] = unit.by_mesh()
            except Exception:
                pass
        if self.texture_mode != "off":
            # Re-warp after every load, so an edit on the map is reflected
            # in the "after transfer" preview straight away.
            self._build_texture()
        if not keep_view:
            self.frame()
        self.update()

    # -- texture preview ---------------------------------------------------
    TEXTURE_MODES = ("off", "asis", "after")

    @staticmethod
    def _texture_module():
        for name in ("uvstudio_m6_texture", "uvslab_m6"):
            module = sys.modules.get(name)
            if module is not None and hasattr(module, "preview_image"):
                return module
        return None

    def set_texture_mode(self, mode):
        """off | asis | after. Returns a one-line note for the log."""
        self.texture_mode = mode if mode in self.TEXTURE_MODES else "off"
        note = self._build_texture()
        self.update()
        return note

    def _build_texture(self):
        """Fill self._tex_tiles {udim: QImage} for the current mode.

        as is  - the texture file(s) as they are, under the current shells;
                 after a repack this SHOWS the mismatch.
        after  - what Transfer Textures would write, warped in memory at
                 preview size from the remembered layout. Nothing is saved.
        """
        self._tex_tiles = {}
        if self.texture_mode == "off":
            return ""
        bm = getattr(self, "bridge_module", None)
        m6 = self._texture_module()
        meshes = getattr(self, "_meshes", None) or []
        if bm is None or m6 is None or not meshes \
                or not hasattr(bm, "texture_files"):
            return "Texture preview needs a loaded mesh and the texture module."
        try:
            found = bm.texture_files(meshes)
        except Exception as exc:
            return "Could not list textures: %s" % exc
        if not found:
            return "No file textures on these meshes."
        node, entry = max(found.items(), key=lambda kv: len(kv[1]["meshes"]))
        tool = m6.find_oiiotool()
        template = m6.udim_template(entry["path"], entry["udim"])
        udim = "<UDIM>" in template
        cache = {}

        def load(key):
            if key not in cache:
                path = m6.tile_path(template, key) if udim else entry["path"]
                cache[key] = m6.preview_image(path, 1024, tool)
            return cache[key]

        name = entry["path"].replace("\\", "/").rsplit("/", 1)[-1]
        if self.texture_mode == "after":
            data = []
            for mesh in entry["meshes"]:
                try:
                    src, _f, _s = bm.face_uv_loops(mesh, bm.BEFORE_SET)
                    cur = bm.current_uv_set(mesh)
                    dst, firsts, shells = bm.face_uv_loops(mesh, cur)
                    data.append((src, dst, firsts, shells))
                except Exception:
                    continue
            if not data:
                self.texture_mode = "asis"
                note = ("No remembered layout - press Remember Layout "
                        "before rearranging. Showing %s as is." % name)
            else:
                # A PREVIEW: one transform per shell (tolerance infinite), so
                # unfolded shells are drawn once instead of triangle by
                # triangle - seconds instead of minutes on a 120k-face car.
                # Transfer Textures keeps the exact per-triangle path.
                progress = LoadProgress(self, "Building texture preview")
                try:
                    tiles = m6.plans_by_tile(data, 1024,
                                             tolerance_px=float("inf"),
                                             progress=progress)
                    self._warp_preview(tiles, udim, load, m6, progress)
                except LoadCancelled:
                    self._tex_tiles = {}
                    self.texture_mode = "off"
                    return "Texture preview cancelled."
                finally:
                    progress.close()
                return "Previewing %s after transfer (%s, not saved)." % (
                    name, node)
        occupied = sorted(set(1001 + int(math.floor(u.centre[0]))
                              + 10 * int(math.floor(u.centre[1]))
                              for u in self.controller.units)) or [1001]
        for key in (occupied if udim else [None]):
            img = load(key)
            if img is not None:
                self._tex_tiles[key or 1001] = img
        if not self._tex_tiles:
            return "Could not read %s for preview." % name
        return locals().get("note") or "Showing %s as is." % name

    def _warp_preview(self, tiles, udim, load, m6, progress):
        for d_udim, pairs in tiles.items():
            if not udim and d_udim != 1001:
                continue
            plans = []
            for s_udim, plan in pairs:
                img = load(s_udim if udim else None)
                if img is None:
                    continue
                plan.source = img
                plans.append(plan)
            if plans:
                size = plans[0].source.width()
                self._tex_tiles[d_udim] = m6.warp_texture(
                    plans[0].source, plans, (size, size), padding_px=2.0,
                    progress=progress)

    def frame(self, include_unit_tile=True):
        """Fit the view to the content AND the 0-1 tile.

        Content-only framing zoomed straight into wherever the shells sat,
        so the view rarely started on UDIM 1001 the way Maya's editor does.
        """
        boxes = [u.bounds for u in self.controller.units]
        if include_unit_tile or not boxes:
            boxes.append((0.0, 1.0, 0.0, 1.0))
        self.controller.view.fit((min(b[0] for b in boxes),
                                  max(b[1] for b in boxes),
                                  min(b[2] for b in boxes),
                                  max(b[3] for b in boxes)))
        self.update()

    def resizeEvent(self, event):
        self.controller.view.width = float(self.width())
        self.controller.view.height = float(self.height())
        super(ClusterMap, self).resizeEvent(event)

    # -- events ----------------------------------------------------------
    def _position(self, event):
        try:
            point = event.position()            # PySide6
            return point.x(), point.y()
        except AttributeError:
            return float(event.x()), float(event.y())

    def mousePressEvent(self, event):
        x, y = self._position(event)
        mods = event.modifiers()
        alt = bool(mods & self._enum(QtCore.Qt, "KeyboardModifier.AltModifier",
                                     "AltModifier"))
        shift = bool(mods & self._enum(QtCore.Qt,
                                       "KeyboardModifier.ShiftModifier",
                                       "ShiftModifier"))
        middle = event.button() == self._enum(QtCore.Qt,
                                              "MouseButton.MiddleButton",
                                              "MiddleButton")
        # In pair mode a plain drag pairs - the mode exists for nothing else,
        # so making the artist hold Alt as well would be a second switch for
        # the same intent. Middle-drag still pans.
        if getattr(self, "pair_mode", False) and not middle:
            alt = True
        self.controller.press(x, y, alt=alt, shift=shift, middle=middle)
        self.update()

    def mouseMoveEvent(self, event):
        x, y = self._position(event)
        if self.controller.drag is not None:
            self.controller.move(x, y)
            self.update()
            return
        # Hover: tags are shown only for the shell under the cursor and the
        # selected ones - drawing every tag on a dense sheet made them
        # overlap into noise.
        hit = hit_test(self.controller.units, *self.controller.view.to_uv(x, y))
        key = hit.key if hit is not None else None
        if key != getattr(self, "_hover_key", None):
            self._hover_key = key
            self.update()

    def leaveEvent(self, event):
        if getattr(self, "_hover_key", None) is not None:
            self._hover_key = None
            self.update()
        super(ClusterMap, self).leaveEvent(event)

    def mouseReleaseEvent(self, event):
        x, y = self._position(event)
        command = self.controller.release(x, y)
        self.update()
        if command and self.runner is not None:
            if command["tool"] != "link_pair":
                if not self._select_for_command():
                    return
            self.runner.run(command["tool"], command.get("settings"))
            if self.bridge is not None and getattr(self, "_meshes", None):
                try:
                    # Same meshes, same bridge module, same view: the edit
                    # should look like the shell moved, not like the map
                    # reloaded and jumped.
                    self.load(self.bridge.analyse_many(self._meshes),
                              keep_view=True)
                except Exception:
                    pass

    def _select_for_command(self):
        """Make Maya's selection the map's selected units' UVs.

        Without this a drag on the map moved whatever was selected in Maya -
        usually the whole mesh, since a mesh is what gets selected to load
        the map. Returns False (and runs nothing) if the UVs can't be
        selected, rather than falling through to the wrong target.
        """
        module = getattr(self, "bridge_module", None)
        uvs = getattr(self, "_unit_uvs", {})
        components = []
        for unit in self.controller.selected_units():
            for mesh, ids in (uvs.get(unit.key) or {}).items():
                if module is not None and ids:
                    components.extend(module.expand_components(mesh, ids))
        if not components:
            return False
        try:
            import maya.cmds as cmds
            cmds.select(components, replace=True)
        except Exception:
            return False
        return True

    def keyPressEvent(self, event):
        if event.key() == self._enum(QtCore.Qt, "Key.Key_Escape",
                                     "Key_Escape"):
            self.controller.cancel()
            self.update()

    def wheelEvent(self, event):
        x, y = self._position(event)
        steps = event.angleDelta().y() / 120.0
        self.controller.view.zoom_at(x, y, 1.15 ** steps)
        self.update()

    # -- painting ----------------------------------------------------------
    def paintEvent(self, _event):
        """Paint, and if anything fails, SAY SO on the canvas.

        An exception in paintEvent leaves Qt with a blank widget and the
        error in a console nobody is watching - a black map with no clue.
        The error is now painted in red where the shells should be, and kept
        in self.last_error for the lab script to report.
        """
        painter = QtGui.QPainter(self)
        try:
            self._paint_into(painter)
            self.last_error = None
        except Exception:
            import traceback
            self.last_error = traceback.format_exc()
            painter.resetTransform()
            painter.fillRect(self.rect(), QtGui.QColor("#1a1010"))
            painter.setPen(QtGui.QColor("#ff6b6b"))
            painter.drawText(self.rect().adjusted(12, 12, -12, -12),
                             self._enum(QtCore.Qt, "TextFlag.TextWordWrap",
                                        "TextWordWrap"),
                             "Cluster Map paint error:\n\n"
                             + self.last_error[-1200:])
        finally:
            painter.end()

    def _paint_into(self, painter):
        painter.setRenderHint(
            self._enum(QtGui.QPainter, "RenderHint.Antialiasing",
                       "Antialiasing"))
        view = self.controller.view
        painter.fillRect(self.rect(), QtGui.QColor(COLOURS["background"]))

        # Tiles holding shells are drawn bright; empty neighbours fade.
        # With every tile labelled alike, the neighbours' numbers (1011
        # above, 1002 right) sat on 1001's edges and read as the shells'
        # own tile.
        occupied = set()
        for unit in self.controller.units:
            cu, cv = unit.centre
            occupied.add((int(math.floor(cu)), int(math.floor(cv))))
        for tile_u, tile_v in tiles_in_view(view):
            x, y, w, h = view.rect_pixels((tile_u, tile_u + 1.0,
                                           tile_v, tile_v + 1.0))
            used = (tile_u, tile_v) in occupied
            painter.setPen(QtGui.QPen(QtGui.QColor(
                "#6f8fa8" if used else COLOURS["tile"]), 2 if used else 1))
            painter.drawRect(int(x), int(y), int(w), int(h))
            painter.setPen(QtGui.QColor("#cfe3f2" if used else "#3c3c3c"))
            # Bottom-left INSIDE the tile, as Maya's UV editor labels it.
            painter.drawText(int(x) + 4, int(y + h) - 5,
                             str(udim_number(tile_u, tile_v)))

        for udim_key, image in sorted((self._tex_tiles or {}).items()):
            n = udim_key - 1001
            tu, tv = n % 10, n // 10
            x, y, w, h = view.rect_pixels((tu, tu + 1.0, tv, tv + 1.0))
            painter.drawImage(QtCore.QRectF(x, y, w, h), image)

        # UV -> pixel as one transform, so each unit's cached UV-space path
        # is drawn without rebuilding it. V up / Y down is the m22 = -scale.
        to_px = QtGui.QTransform(view.scale, 0.0, 0.0, -view.scale,
                                 view.width * 0.5 - view.centre_u * view.scale,
                                 view.height * 0.5 + view.centre_v * view.scale)
        drag = self.controller.drag
        live_du = live_dv = 0.0
        if drag is not None and drag.mode == MOVE:
            live_du, live_dv = drag.delta
        for unit in self.controller.units:
            selected = unit.key in self.controller.selection
            moving = selected and (live_du or live_dv)
            bounds = unit.bounds
            if moving:
                bounds = (bounds[0] + live_du, bounds[1] + live_du,
                          bounds[2] + live_dv, bounds[3] + live_dv)
            x, y, w, h = view.rect_pixels(bounds)
            if unit.pinned:
                base = QtGui.QColor(COLOURS["pinned"])
            else:
                base = QtGui.QColor(hue_for(unit))
            if selected:
                base = base.lighter(135)
            fill = QtGui.QColor(base)
            if self._tex_tiles:
                fill.setAlpha(70 if selected else 25)   # let the texture show
            else:
                fill.setAlpha(170 if selected else 120)

            if unit.polys and (w > 4 or h > 4):
                path = self._path_for(unit)
                painter.save()
                if moving:
                    painter.setTransform(QtGui.QTransform().translate(
                        live_du, live_dv) * to_px)
                else:
                    painter.setTransform(to_px)
                pen = QtGui.QPen(base.lighter(125), 0)      # cosmetic 1px
                pen.setCosmetic(True)
                painter.setPen(pen)
                painter.setBrush(fill)
                painter.drawPath(path)
                painter.restore()
            else:
                painter.fillRect(int(x), int(y), max(1, int(w)),
                                 max(1, int(h)), fill)
            if selected:
                painter.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1,
                                          self._enum(QtCore.Qt,
                                                     "PenStyle.DashLine",
                                                     "DashLine")))
                painter.setBrush(self._enum(QtCore.Qt, "BrushStyle.NoBrush",
                                            "NoBrush"))
                painter.drawRect(int(x), int(y), int(w), int(h))

            if selected or unit.key == getattr(self, "_hover_key", None):
                self._draw_tags(painter, unit, x, y, w, h)

            badge = self.controller.badge_of(unit.key)
            if badge is not None:
                painter.setPen(QtGui.QColor("#ffffff"))
                painter.drawText(int(x) + 4, int(y) + 14, str(badge))
            if selected:
                # `bounds`, not unit.bounds: mid-drag the handles travel
                # with the shell instead of staying at its old position.
                for name in HANDLES:
                    cu = bounds[1] if "e" in name else bounds[0]
                    cv = bounds[3] if name.startswith("n") else bounds[2]
                    hx, hy = view.to_pixels(cu, cv)
                    painter.fillRect(int(hx) - 3, int(hy) - 3, 6, 6,
                                     QtGui.QColor("#ffffff"))
                mid_u = (bounds[0] + bounds[1]) * 0.5
                rx, ry = view.to_pixels(mid_u, bounds[3])
                painter.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1))
                painter.drawLine(int(rx), int(ry), int(rx), int(ry) - 22)
                painter.drawEllipse(int(rx) - 4, int(ry) - 26, 8, 8)

        drag = self.controller.drag
        if drag is not None and drag.mode == MOVE:
            painter.setPen(QtGui.QPen(QtGui.QColor(COLOURS["guide"]), 1,
                                      self._enum(QtCore.Qt, "PenStyle.DashLine",
                                                 "DashLine")))
            for line_u in drag.guides[0]:
                px, _ = view.to_pixels(line_u, 0)
                painter.drawLine(int(px), 0, int(px), int(view.height))
            for line_v in drag.guides[1]:
                _, py = view.to_pixels(0, line_v)
                painter.drawLine(0, int(py), int(view.width), int(py))

        if drag is not None and drag.mode == LINK and drag.unit is not None:
            self._draw_arrow(painter, drag)

    def _shell_path(self, key, loops):
        """One shell's faces as a QPainterPath in UV space, cached by key.

        This cache survives reloads: after a move only the moved shells have
        new keys, so only they are rebuilt. Rebuilding all 15k faces after
        every drag or pair was the stall.
        """
        cache = getattr(self, "_part_paths", None)
        if cache is None:
            cache = self._part_paths = {}
        path = cache.get(key)
        if path is None:
            path = QtGui.QPainterPath()
            path.setFillRule(self._enum(QtCore.Qt, "FillRule.WindingFill",
                                        "WindingFill"))
            for loop in loops:
                if len(loop) < 3:
                    continue
                path.moveTo(loop[0][0], loop[0][1])
                for u, v in loop[1:]:
                    path.lineTo(u, v)
                path.closeSubpath()
            cache[key] = path
        return path

    def _path_for(self, unit):
        """The unit's outline: its shells' cached paths joined (C++-side)."""
        cache = getattr(self, "_paths", None)
        if cache is None:
            cache = self._paths = {}
        path = cache.get(unit.key)
        if path is None:
            path = QtGui.QPainterPath()
            path.setFillRule(self._enum(QtCore.Qt, "FillRule.WindingFill",
                                        "WindingFill"))
            for key, loops in (unit.parts or [(("unit", unit.key),
                                               unit.polys)]):
                path.addPath(self._shell_path(key, loops))
            cache[unit.key] = path
        return path

    def _draw_tags(self, painter, unit, x, y, w, h):
        """P# for a duplicate family, xN for a stack, centred on the unit."""
        tags = []
        if unit.family:
            tags.append("P%d" % unit.family)
        if unit.shell_count > 1:
            tags.append("x%d" % unit.shell_count)
        if not tags or (w < 14 and h < 14):
            return
        cx, cy = x + w * 0.5, y + h * 0.5
        painter.save()          # the tag's dark fill must not leak out
        for i, tag in enumerate(tags):
            ty = cy + (i - (len(tags) - 1) * 0.5) * 16
            rect = QtCore.QRectF(cx - 15, ty - 7, 30, 14)
            painter.setPen(self._enum(QtCore.Qt, "PenStyle.NoPen", "NoPen"))
            painter.setBrush(QtGui.QColor(20, 20, 20, 210))
            painter.drawRoundedRect(rect, 3, 3)
            painter.setPen(QtGui.QColor("#ffffff"))
            painter.drawText(rect, self._enum(QtCore.Qt,
                                              "AlignmentFlag.AlignCenter",
                                              "AlignCenter"), tag)
        painter.restore()

    def _draw_arrow(self, painter, drag):
        view = self.controller.view
        start = view.to_pixels(*drag.unit.centre)
        end = (view.to_pixels(*drag.target.centre) if drag.target
               else view.to_pixels(drag.current_u, drag.current_v))
        colour = QtGui.QColor(COLOURS["arrow"])
        painter.setPen(QtGui.QPen(colour, 2))
        painter.drawLine(int(start[0]), int(start[1]),
                         int(end[0]), int(end[1]))
        angle = math.atan2(end[1] - start[1], end[0] - start[0])
        for side in (2.6, -2.6):
            painter.drawLine(
                int(end[0]), int(end[1]),
                int(end[0] + 12 * math.cos(angle + side)),
                int(end[1] + 12 * math.sin(angle + side)))
        # Both ends outlined by their REAL shapes, no fill, no box. The box
        # (drawn with a leaked dark brush) hid the shell being paired to.
        to_px = QtGui.QTransform(view.scale, 0.0, 0.0, -view.scale,
                                 view.width * 0.5 - view.centre_u * view.scale,
                                 view.height * 0.5 + view.centre_v * view.scale)
        pen = QtGui.QPen(colour, 2)
        pen.setCosmetic(True)
        for end in (drag.unit, drag.target):
            if end is None or not end.polys:
                continue
            painter.save()
            painter.setTransform(to_px)
            painter.setPen(pen)
            painter.setBrush(self._enum(QtCore.Qt, "BrushStyle.NoBrush",
                                        "NoBrush"))
            painter.drawPath(self._path_for(end))
            painter.restore()


# =====================================================================
# SECTION 7 - Self test (pure half only)
# =====================================================================

def run_self_test():
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
    print("UV Studio M9 - Cluster Map self test (v%s)" % __version__)
    print("=" * 80)

    print("--- coordinates ---")
    view = MapView(width=800, height=800, centre=(0.5, 0.5), scale=600.0)
    check("the view centre is the widget centre", view.to_pixels(0.5, 0.5),
          (400.0, 400.0))
    check_true("V up, Y down", view.to_pixels(0.5, 1.0)[1] < 400.0,
               str(view.to_pixels(0.5, 1.0)))
    u, v = view.to_uv(*view.to_pixels(0.31, 0.77))
    check_true("round trip", abs(u - 0.31) < 1e-9 and abs(v - 0.77) < 1e-9,
               "(%.6f, %.6f)" % (u, v))
    x, y, w, h = view.rect_pixels((0.2, 0.4, 0.1, 0.5))
    check_true("a box is drawn from its top-left in pixels",
               w == 0.2 * 600 and h == 0.4 * 600, "%s x %s" % (w, h))

    before = view.to_uv(120, 640)
    view.zoom_at(120, 640, 2.0)
    after = view.to_uv(120, 640)
    check_true("zoom keeps the point under the cursor",
               abs(after[0] - before[0]) < 1e-9
               and abs(after[1] - before[1]) < 1e-9, str(after))

    print("\n--- hit testing ---")
    big = MapUnit(0, (0.0, 0.9, 0.0, 0.9))
    small = MapUnit(1, (0.3, 0.4, 0.3, 0.4))
    far = MapUnit(2, (1.2, 1.4, 0.1, 0.3))
    units = [big, small, far]
    check("the small shell on top of the big one wins",
          hit_test(units, 0.35, 0.35).key, 1)
    check("  and the big one elsewhere", hit_test(units, 0.8, 0.8).key, 0)
    check("empty space hits nothing", hit_test(units, 5.0, 5.0), None)
    check("a marquee touches what it crosses",
          sorted(u.key for u in marquee_hits(units, 0.0, 0.0, 0.5, 0.5)),
          [0, 1])
    check("  and enclose-mode is stricter",
          sorted(u.key for u in marquee_hits(units, 0.0, 0.0, 0.5, 0.5,
                                             enclose=True)), [1])

    print("\n--- picking uses faces, not boxes (bug fix) ---")
    # A thin diagonal strip whose BOX covers the whole tile, and a panel in
    # the corner. Clicking the panel must pick the panel, even though the
    # strip's box also covers the click and is smaller than... it is not;
    # make the strip's box the SMALLER one to reproduce the original bug.
    strip = MapUnit(10, (0.2, 0.8, 0.2, 0.8),
                    polys=[((0.2, 0.2), (0.25, 0.2), (0.8, 0.75),
                            (0.8, 0.8), (0.75, 0.8), (0.2, 0.25))])
    panel = MapUnit(11, (0.0, 1.0, 0.0, 1.0),
                    polys=[((0.55, 0.05), (0.95, 0.05), (0.95, 0.45),
                            (0.55, 0.45)),
                           ((0.0, 0.0), (0.001, 0.0), (0.001, 1.0),
                            (0.0, 1.0)),
                           ((0.999, 0.0), (1.0, 0.0), (1.0, 1.0),
                            (0.999, 1.0))])
    check("click on the panel's face picks the panel, not the strip",
          hit_test([strip, panel], 0.7, 0.3).key, 11)
    check("click on the strip picks the strip",
          hit_test([strip, panel], 0.5, 0.5).key, 10)
    check("click on empty space inside both boxes picks nothing",
          hit_test([strip, panel], 0.3, 0.6), None)
    check("a marquee over the panel's face does not grab the strip",
          sorted(x.key for x in marquee_hits([strip, panel],
                                             0.6, 0.1, 0.9, 0.4)), [11])

    print("\n--- alt-drag makes a pair ---")
    a = MapUnit(0, (0.0, 0.2, 0.0, 0.2), anchor=("|m|mShape", 0))
    b = MapUnit(1, (0.6, 0.8, 0.6, 0.8), anchor=("|m|mShape", 400))
    controller = MapController(MapView(400, 400, (0.5, 0.5), 400.0),
                               [a, b])
    start = controller.view.to_pixels(0.1, 0.1)
    end = controller.view.to_pixels(0.7, 0.7)
    drag = controller.press(start[0], start[1], alt=True)
    check("alt on a shell starts a link", drag.mode, LINK)
    controller.move(end[0], end[1])
    check_true("  and the arrow finds its target",
               controller.drag.target is not None
               and controller.drag.target.key == 1, "")
    command = controller.release(end[0], end[1])
    check("releasing emits link_pair", command["tool"], "link_pair")
    check("  with both anchors",
          command["settings"]["anchors"],
          [["|m|mShape", 0], ["|m|mShape", 400]])

    controller = MapController(MapView(400, 400, (0.5, 0.5), 400.0), [a, b])
    controller.press(start[0], start[1], alt=True)
    check("a link that ends where it began is not a pair",
          controller.release(start[0], start[1]), None)
    controller = MapController(MapView(400, 400, (0.5, 0.5), 400.0), [a, b])
    empty = controller.view.to_pixels(0.45, 0.45)
    check("alt on empty space starts nothing",
          controller.press(empty[0], empty[1], alt=True), None)

    print("\n--- drag emits commands, never writes ---")
    controller = MapController(MapView(400, 400, (0.5, 0.5), 400.0), [a, b])
    controller.snapping = False
    p0 = controller.view.to_pixels(0.1, 0.1)
    controller.press(p0[0], p0[1])
    check("pressing a shell selects it", controller.selection, [0])
    p1 = controller.view.to_pixels(0.3, 0.25)
    controller.move(p1[0], p1[1])
    command = controller.release(p1[0], p1[1])
    check("a move emits the move tool", command["tool"], "move")
    check_true("  with the delta dragged",
               abs(command["settings"]["u"] - 0.2) < 1e-9
               and abs(command["settings"]["v"] - 0.15) < 1e-9,
               str(command["settings"]))

    controller = MapController(MapView(400, 400, (0.5, 0.5), 400.0), [a, b])
    controller.press(p0[0], p0[1])
    check("a click that does not move emits nothing",
          controller.release(p0[0], p0[1]), None)

    print("\n--- corner scale is measured from the opposite corner ---")
    controller = MapController(MapView(400, 400, (0.5, 0.5), 400.0), [a, b])
    controller.select([0])
    corner = controller.view.to_pixels(0.2, 0.2)          # the "ne" handle
    drag = controller.press(corner[0], corner[1])
    check("a handle on a selected unit starts a scale", drag.mode, SCALE)
    check("  and knows which corner", drag.handle, "ne")
    pulled = controller.view.to_pixels(0.4, 0.4)
    controller.move(pulled[0], pulled[1])
    command = controller.release(pulled[0], pulled[1])
    check("scale emits the scale tool", command["tool"], "scale")
    check_true("  doubling when the corner is pulled to twice the span",
               abs(command["settings"]["factor"] - 2.0) < 1e-9,
               str(command["settings"]["factor"]))

    print("\n--- rotate ---")
    controller = MapController(MapView(400, 400, (0.5, 0.5), 400.0), [a, b])
    controller.select([0])
    handle = controller.view.to_pixels(0.1, 0.2)
    handle = (handle[0], handle[1] - 22.0)
    drag = controller.press(handle[0], handle[1])
    check("the rotate handle sits above the top edge", drag.mode, ROTATE)
    controller.move(*controller.view.to_pixels(0.2, 0.1))
    command = controller.release(*controller.view.to_pixels(0.2, 0.1))
    check_true("rotate emits an angle",
               command["tool"] == "rotate"
               and abs(command["settings"]["degrees"]) > 1.0,
               str(command["settings"]))

    print("\n--- guides and snapping ---")
    vertical, horizontal, snap = guides_for(
        (0.301, 0.401, 0.0, 0.1), [(0.3, 0.4, 0.5, 0.6)], 0.01)
    check_true("a near edge produces a guide", 0.3 in vertical, str(vertical))
    check_true("  and a snap that closes the gap",
               abs(snap[0] + 0.001) < 1e-9, str(snap))
    _v, _h, snap = guides_for((0.5, 0.6, 0.0, 0.1), [(0.9, 1.0, 0.5, 0.6)],
                              0.01)
    check("a far edge produces no snap", snap, (0.0, 0.0))

    print("\n--- marquee and badges ---")
    controller = MapController(MapView(400, 400, (0.5, 0.5), 400.0), [a, b])
    empty = controller.view.to_pixels(0.45, 0.02)
    controller.press(empty[0], empty[1])
    controller.move(*controller.view.to_pixels(0.9, 0.9))
    controller.release(*controller.view.to_pixels(0.9, 0.9))
    check("a marquee selects what it crossed", sorted(controller.selection),
          [1])
    controller.select([0], add=True)
    check("badges number the pick order", controller.badge_of(0), 2)
    check("  and unpicked units have none", controller.badge_of(99), None)

    print("\n--- escape abandons a gesture ---")
    controller = MapController(MapView(400, 400, (0.5, 0.5), 400.0), [a, b])
    controller.press(p0[0], p0[1])
    controller.move(p1[0], p1[1])
    controller.cancel()
    check("nothing is emitted after cancel", controller.release(), None)

    print("\n--- pivot placement (Edit Pivot) ---")
    pp = PivotPlacement(bounds=(0.2, 0.6, 0.3, 0.7))
    check("a fresh pivot sits at the selection centre", pp.point(),
          (0.4, 0.5))
    check("nine snap targets for a box", len(pp.snap_targets()), 9)
    # click near the top-right corner (0.6, 0.7) -> snaps to it
    px, py = pp.view.to_pixels(0.6, 0.7)
    got = pp.set_from_pixel(px + 3, py - 3, snap_px=12.0)
    check("a click near a corner snaps to it",
          (round(got[0], 4), round(got[1], 4)), (0.6, 0.7))
    # click in open space -> no snap, lands where clicked
    px, py = pp.view.to_pixels(0.45, 0.55)
    got = pp.set_from_pixel(px, py, snap_px=6.0)
    check_true("a click in open space lands where clicked, unsnapped",
               abs(got[0] - 0.45) < 1e-6 and abs(got[1] - 0.55) < 1e-6,
               str(got))

    print("\n--- colours say what matters ---")
    check("pinned beats selected",
          colour_for(MapUnit(0, (0, 1, 0, 1), pinned=True), selected=True),
          COLOURS["pinned"])
    check("linked reads as linked",
          colour_for(MapUnit(0, (0, 1, 0, 1), reason="linked")),
          COLOURS["linked"])

    print("\n--- tiles ---")
    check("a unit tile is 1001", udim_number(0, 0), 1001)
    check("  one right is 1002", udim_number(1, 0), 1002)
    check("  one up is 1011", udim_number(0, 1), 1011)
    wide = MapView(800, 800, (1.0, 1.0), 200.0)
    check_true("a zoomed-out view lists several tiles",
               len(tiles_in_view(wide)) >= 9, str(len(tiles_in_view(wide))))

    print("")
    print("=" * 80)
    print("%d failure(s)" % len(failures))
    for name in failures:
        print("  FAILED: %s" % name)
    print("=" * 80)
    return not failures


if __name__ == "__main__":
    run_self_test()
