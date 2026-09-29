\
"""
UV Studio - Module 5: UI Shell
==============================

PURPOSE
    Turns the 34 resolved native commands into buttons, records what they did
    into the recipe, and lays the panel out the way the artist wants rather
    than the way Maya happens to.

THREE DECISIONS THIS MODULE IMPLEMENTS
    1. Buttons run immediately using last-used settings. No modal in the way.
       Settings persist per tool between sessions.
    2. Sections are declarative and reorderable. Maya scatters Transform,
       Align & Snap and Arrange & Layout across the toolkit even though they
       are used as one workflow; here they are one tab, and the order is the
       artist's to change and keep.
    3. With nothing selected, a tool warns and does nothing. It never guesses
       at a target.

STRUCTURE
    The registry, preferences and selection guard are pure Python and are
    tested standalone. Only the widgets need Maya, so a layout bug cannot be
    confused with a command bug.

    TOOLS           declarative: id, label, command key, defaults, what it
                    needs selected, and which recipe fidelity it records as
    Preferences     JSON: section order, collapsed state, per-tool settings
    SelectionGuard  decides yes/no and says why, without touching anything
    ToolRunner      resolves, runs inside one undo chunk, records the step
    UI              tabs of collapsible, reorderable sections of buttons

USAGE
    In Maya, with M1/M2/M3/M4 available:
        import uvstudio_m5_ui as m5
        m5.show()
    Standalone, to test the pure parts:
        python uvstudio_m5_ui.py

Target: Maya 2022 - 2025+
"""

from __future__ import annotations

import json
import sys
import re
import tempfile
import os
import traceback
from collections import OrderedDict

__version__ = "3.22.0"
MODULE_ID = "M5"

IN_MAYA = True
try:
    import maya.cmds as cmds
except ImportError:                      # standalone test run
    IN_MAYA = False
    cmds = None


# =====================================================================
# SECTION 1 - Selection requirements
# =====================================================================

SEL_ANY = "any"            # anything selected
SEL_OBJECT = "object"      # a mesh
SEL_UV = "uv"              # UV components
SEL_EDGE = "edge"          # edges
SEL_FACE = "face"          # faces
SEL_NONE = "none"          # needs nothing


# =====================================================================
# SECTION 2 - Tool registry
# =====================================================================

class Field(object):
    """One editable control, declared next to the tool it belongs to.

    WHY THIS IS DATA AND NOT PANEL CODE
        Before this, a tool's values were reachable only through a generated
        settings dialog, because the panel had no way to know that `degrees`
        wants a 62px number box with a degree sign and `sort` wants a menu.
        Hand-building the widgets per section would work once and then rot:
        the section order is already declarative and reorderable, so the
        controls inside a section have to be too, or adding a tool means
        editing the panel.

        A field names a key in the tool's `defaults`. That is the whole
        contract - the inline control and the right-click sheet read and
        write the SAME key in the SAME prefs entry, which is what stops the
        two routes from drifting into two values that disagree.

    kinds
        float, int, text   a number or string box
        bool               a checkbox
        enum               a drop-down over `options`
        axes               the U / V pair, reading keys axis_u and axis_v
        tiles              a UDIM range, "1001-1004", stored as a list
        snap               the on/off chip plus its increment, reading keys
                           <key>_enabled and <key>_step
        nudge              a pair of arrows that run the tool named by `runs`
    """

    KINDS = ("float", "int", "text", "bool", "enum", "axes", "tiles",
             "snap", "nudge", "tristate")

    __slots__ = ("key", "kind", "label", "options", "minimum", "maximum",
                 "step", "decimals", "unit", "width", "tooltip", "runs",
                 "optional", "verified", "group")

    def __init__(self, key, kind="float", label="", options=None,
                 minimum=None, maximum=None, step=None, decimals=4, unit="",
                 width=0, tooltip="", runs=None, optional=False,
                 verified=True, group=""):
        if kind not in self.KINDS:
            raise ValueError("%s: unknown field kind %r" % (key, kind))
        self.key = key
        self.kind = kind
        self.label = label
        self.options = list(options or [])
        self.minimum = minimum
        self.maximum = maximum
        self.step = step
        self.decimals = decimals
        self.unit = unit
        self.width = width
        self.tooltip = tooltip
        self.runs = runs
        # optional means "None is a real value for this setting, distinct
        # from zero" - e.g. Distribute's gap, where None spaces evenly and
        # 0.0 butts shells edge to edge.
        self.optional = bool(optional)
        # group names the option-box SECTION this field sits under, matching
        # Maya's own dialogs (e.g. "Solver Options", "Room Space Options").
        # Empty means the default top section. The inline row ignores it.
        self.group = group
        # verified=False marks a flag name taken from the Maya docs rather
        # than confirmed against a running Maya. A wrong flag name is a
        # TypeError the moment the button is pressed, because settings are
        # passed straight through as kwargs. Marking them means the first
        # session back at a machine has a list to check instead of a
        # scavenger hunt.
        self.verified = bool(verified)

    def keys(self):
        """Every settings key this one control reads and writes."""
        if self.kind == "axes":
            return ["axis_u", "axis_v"]
        if self.kind == "snap":
            return ["%s_enabled" % self.key, "%s_step" % self.key]
        return [self.key]

    def __repr__(self):
        return "<Field %s:%s>" % (self.key, self.kind)


def _f(*args, **kwargs):
    return Field(*args, **kwargs)


class Tool(object):
    """One button, and the controls that sit beside it.

    `cmd_key` names a command in M2's resolver. `handler` names an internal
    operation implemented by UV Studio itself (pack, align, stack) rather than
    by Maya. Exactly one of the two is set.

    `fields` render inline, on the tool's own row: the values an artist
    retypes mid-job. `advanced` render only in the right-click sheet: the
    ones set once and forgotten. Both edit the same stored settings, so which
    list a key sits in is a presentation decision and never a behavioural one.

    `uses_pivot` marks a transform that obeys the tab's pivot strip. The
    pivot is injected at run time and deliberately NOT stored per tool - see
    ToolRunner.run.
    """

    __slots__ = ("tool_id", "label", "tab", "section", "cmd_key", "handler",
                 "defaults", "needs", "fidelity", "tooltip",
                 "fields", "advanced", "uses_pivot", "width", "hidden",
                 "options_only")

    def __init__(self, tool_id, label, tab, section, cmd_key=None, handler=None,
                 defaults=None, needs=SEL_OBJECT, fidelity="exact",
                 tooltip="", fields=None, advanced=None,
                 uses_pivot=False, width=0, hidden=False,
                 options_only=False):
        if bool(cmd_key) == bool(handler):
            raise ValueError("%s: set exactly one of cmd_key or handler"
                             % tool_id)
        self.tool_id = tool_id
        self.label = label
        self.tab = tab
        self.section = section
        self.cmd_key = cmd_key
        self.handler = handler
        self.defaults = dict(defaults or {})
        self.needs = needs
        self.fidelity = fidelity
        self.tooltip = tooltip
        self.fields = list(fields or [])
        self.advanced = list(advanced or [])
        self.uses_pivot = bool(uses_pivot)
        self.width = width
        # hidden: a real, runnable tool with its own settings that gets no
        # row of its own because another tool's control drives it. Nudge is
        # the case: Move's arrows run it. Without this the panel drew a
        # "Nudge" button that, pressed on its own, stepped in whatever
        # direction was last used - a control with no visible state.
        self.hidden = bool(hidden)
        # options_only: the row shows just the button and the option-box icon;
        # all settings live in the floating dialog, never inline. For tools
        # whose settings are a full Maya-style dialog (Pack, Unfold) rather
        # than one or two quick tweaks that belong on the row.
        self.options_only = bool(options_only)

        # A field naming a key the tool has no default for would render as an
        # empty box that writes a setting nothing reads. Caught here, at
        # import, rather than as a button that quietly does nothing.
        for field in self.fields + self.advanced:
            for key in field.keys():
                if key not in self.defaults and field.kind != "nudge":
                    raise ValueError("%s: field %r has no default for %r"
                                     % (tool_id, field.key, key))

    def inline_keys(self):
        keys = []
        for field in self.fields:
            keys.extend(field.keys())
        return keys

    def sheet_fields(self):
        """What the right-click sheet shows: `advanced`, plus any default
        that no inline field covers, so nothing is unreachable."""
        covered = set(self.inline_keys())
        for field in self.advanced:
            covered.update(field.keys())
        extra = [Field(key, _kind_for(self.defaults[key]))
                 for key in sorted(self.defaults) if key not in covered
                 and key != "pivot"]
        return list(self.advanced) + extra

    def __repr__(self):
        return "<Tool %s (%s/%s)>" % (self.tool_id, self.tab, self.section)


def _kind_for(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, (list, tuple)):
        return "tiles"
    return "text"


def _t(*args, **kwargs):
    return Tool(*args, **kwargs)


# Tabs and their default section order. The artist reorders sections at
# runtime; this is only the starting point.
TAB_ORDER = ["Edit", "Layout", "Pack", "Groups", "Density", "Checks",
             "Texture", "Debug"]

DEFAULT_SECTIONS = OrderedDict([
    ("Debug", ["Tests"]),
    # Edit is Maya's own commands, grouped the way Maya groups them, so an
    # artist who knows the UV Toolkit can find things. Everything else is
    # UV Studio's own work and is grouped by what it is FOR.
    # "Interactive" holds tools that ENTER a mode and are then driven by
    # dragging in the viewport - the 3D Cut and Sew brush - as opposed to the
    # selection-and-press commands in Cut & Sew above it. Kept a separate
    # section, not a divider inside Cut & Sew, because "select then press" and
    # "enter a tool then drag" are genuinely different gestures and the header
    # bar is the honest way to say so.
    ("Edit", ["Select", "Create", "Cut & Sew", "Interactive", "Unfold",
              "Arrange", "Pin"]),
    # Align and Distribute are one section: they are used together, in one
    # gesture, and the reference layout puts them side by side.
    ("Layout", ["Transform", "Align & Distribute", "Orient"]),
    ("Pack", ["Pack"]),
    # Stacking gets its own tab rather than a section inside Layout. It is
    # not an arranging operation: it decides which shells ARE one thing, and
    # every layout tool downstream treats that decision as given. Burying it
    # under Layout made the thing that defines a unit look like one more way
    # to move units around.
    ("Groups", ["Pairs", "Stacks"]),
    ("Density", ["Density"]),
    ("Checks", ["Audit"]),
    ("Texture", ["Texture"]),
])


# Sections that are not a plain column of rows. The panel reads this rather
# than special-casing names in widget code: a grid on the left, a divider,
# and ordinary rows (with their inline fields) on the right.
SECTION_LAYOUTS = {
    ("Layout", "Align & Distribute"): OrderedDict([
        ("grid", [["align_left", "align_centre_u", "align_right"],
                  ["align_top", "align_centre_v", "align_bottom"]]),
        ("side", ["distribute_u", "distribute_v"]),
    ]),
}

# Drawn glyphs for tool buttons, by tool id. The panel draws them; a tool
# without an entry keeps a plain text button.
TOOL_GLYPHS = {
    "align_left": "align_left", "align_right": "align_right",
    "align_top": "align_top", "align_bottom": "align_bottom",
    "align_centre_u": "align_centre_u", "align_centre_v": "align_centre_v",
    "distribute_u": "distribute_u", "distribute_v": "distribute_v",
}


# Icons. Maya's own icon for a tool is found by SEARCHING the icons installed
# in the running Maya rather than by exact file name, because those names
# change between versions. Each entry is a list of keyword groups tried in
# order; an icon matches a group when its name contains every keyword.
# Anything not found falls back to a drawn glyph for its section, so no row
# is ever left without an icon.
TOOL_ICON_SEARCH = {
    # Maya abbreviates (polyCylProj), so the short forms come first. The
    # bare ("planar",) and ("spherical",) searches matched planarTrim.png
    # and polySphericalHarmonics.png on 2022 - unrelated tools - and are gone.
    "planar": [("planproj",), ("poly", "planar", "proj")],
    "cylindrical": [("cylproj",), ("poly", "cylindrical")],
    "spherical": [("sphproj",), ("poly", "spherical", "proj")],
    "automatic": [("auto", "proj")],
    "camera": [("camera", "proj"), ("camerabased",)],
    "contour": [("contour",)],
    "cut": [("cut", "uv"), ("mapcut",)],
    "sew": [("sew", "uv"), ("mapsew",)],
    "sew_move": [("move", "sew")],
    "split": [("split", "uv")],
    "merge": [("merge", "uv")],
    "cut_tool": [("3d", "cut"), ("cut", "sew")],
    "unfold3d": [("unfold",)],
    "optimize": [("optimize",)],
    "straighten_uvs": [("straighten", "uv")],
    "straighten_border": [("straighten", "border"), ("straighten",)],
    "flip_native_u": [("flip", "uv")],
    "normalize": [("normalize",)],
    "unitize": [("unitize",)],
    "layout_maya": [("layout", "uv")],
    "layout_u3d": [("layout", "uv")],
    "orient_shells": [("orient", "shell")],
    "orient_edge": [("orient", "edge")],
    "stack_native": [("stack", "shell"), ("stack",)],
    "stack_pairs": [("stack", "shell"), ("stack",)],
    "pin": [("pin", "uv")],
    "unpin": [("unpin",), ("pin", "uv")],
    "invert_pins": [("pin", "uv")],
    "select_pinned": [("pin", "uv")],
    "get_density": [("texel",), ("density",)],
    "set_density": [("texel",), ("density",)],
}

# Drawn fallback: a specific glyph for some tools, else one per section.
TOOL_GLYPH_FALLBACK = {
    "rotate": "rot_ccw", "rotate_ccw": "rot_ccw", "rotate_cw": "rot_cw",
    "scale": "scale", "move": "move", "flip_u": "flip", "flip_v": "flip",
    "flip_native_u": "flip",
}
SECTION_GLYPHS = {
    ("Edit", "Create"): "project", ("Edit", "Cut & Sew"): "cut",
    ("Edit", "Interactive"): "cut", ("Edit", "Unfold"): "unfold",
    ("Edit", "Arrange"): "grid", ("Edit", "Pin"): "pin",
    ("Edit", "Select"): "select", ("Layout", "Orient"): "orient",
    ("Layout", "Transform"): "move", ("Groups", "Pairs"): "link",
    ("Groups", "Stacks"): "link", ("Pack", "Pack"): "grid",
    ("Density", "Density"): "checker", ("Checks", "Audit"): "check",
    ("Debug", "Tests"): "check", ("Texture", "Texture"): "checker",
}


# Always visible, below the tabs, whatever tab is open. The spec calls for
# analyse / preview / pack / check to be reachable without hunting for the
# tab they live on, because they are the four things done repeatedly.
ACTION_BAR = ["analyze", "pack_preview", "pack", "check_overlaps"]


TOOLS = [
    # ---- Layout / Transform -------------------------------------------
    # Transform and layout handlers take SEL_ANY, not SEL_UV.
    # Their handlers document a fallback - "shells touched by the UV selection,
    # else all of them" - which only means anything if an OBJECT selection can
    # reach them. With SEL_UV the guard rejected exactly that case, so selecting
    # a mesh and pressing Rotate warned "Rotate needs uv selected; you have
    # object" while the code behind it was written to handle it. Tools that
    # genuinely need components (pin, merge, the native cut/sew) keep SEL_UV.
    # Rotate, Scale, Move and Flip all obey the tab's pivot strip, which is
    # why Flip goes through polyEditUV (a native command, via the writer)
    # rather than polyFlipUV: polyFlipUV always mirrors inside each shell's
    # own box and cannot honour a shared pivot, so on "selection" it would
    # silently do something different from every other transform.
    _t("rotate", "Rotate", "Layout", "Transform", handler="rotate",
       defaults={"degrees": 90.0, "snap_enabled": False, "snap_step": 15.0},
       needs=SEL_ANY, uses_pivot=True, width=96,
       fields=[_f("degrees", "float", unit="\u00b0", width=62),
               _f("snap", "snap", label="snap", step=15.0)],
       tooltip="Rotate the selection about the pivot set at the top of the "
               "tab."),
    _t("rotate_ccw", "Rotate +90", "Layout", "Transform", handler="rotate",
       defaults={"degrees": 90.0}, needs=SEL_ANY, uses_pivot=True),
    _t("rotate_cw", "Rotate -90", "Layout", "Transform", handler="rotate",
       defaults={"degrees": -90.0}, needs=SEL_ANY, uses_pivot=True),
    _t("scale", "Scale", "Layout", "Transform", handler="scale",
       defaults={"factor": 2.0, "axis_u": True, "axis_v": True,
                 "prevent_negative": True, "snap_enabled": False,
                 "snap_step": 0.1},
       needs=SEL_ANY, uses_pivot=True, width=96,
       fields=[_f("factor", "float", width=62), _f("axes", "axes")],
       advanced=[_f("prevent_negative", "bool", label="Prevent negative "
                                                     "scale"),
                 _f("snap", "snap", label="Snap", step=0.1)],
       tooltip="Scale the selection about the tab's pivot."),
    _t("move", "Move", "Layout", "Transform", handler="move",
       defaults={"u": 0.0, "v": 0.0, "snap_enabled": False,
                 "snap_step": 0.1},
       needs=SEL_ANY, width=96,
       fields=[_f("u", "float", width=62), _f("v", "float", width=62),
               _f("step", "nudge", runs="nudge")],
       advanced=[_f("snap", "snap", label="Snap", step=0.1)],
       tooltip="Offset the selection by an exact amount. The arrows step by "
               "the Nudge amount instead."),
    _t("nudge", "Nudge", "Layout", "Transform", handler="nudge",
       defaults={"axis": "u", "direction": 1, "step": 0.1}, needs=SEL_ANY,
       fidelity="exact", hidden=True,
       tooltip="One step along an axis. Driven by Move's arrow buttons."),
    _t("flip_u", "Flip U", "Layout", "Transform", handler="flip",
       defaults={"axis": "u"}, needs=SEL_ANY, uses_pivot=True,
       tooltip="Mirror horizontally about the tab's pivot."),
    _t("flip_v", "Flip V", "Layout", "Transform", handler="flip",
       defaults={"axis": "v"}, needs=SEL_ANY, uses_pivot=True,
       tooltip="Mirror vertically about the tab's pivot."),
    _t("flip_native_u", "Flip U (Maya)", "Edit", "Arrange",
       cmd_key="flip", defaults={"flipType": 0}, needs=SEL_UV,
       tooltip="Maya's polyFlipUV. Always mirrors inside each shell's own "
               "box, whatever the pivot strip says."),
    _t("normalize", "Normalize", "Edit", "Arrange", cmd_key="normalize",
       defaults={"normalizeType": 1}, needs=SEL_OBJECT,
       tooltip="Fit UVs into 0-1 space."),
    _t("unitize", "Unitize", "Edit", "Arrange", cmd_key="unitize",
       defaults={"unitize": True}, needs=SEL_FACE),

    # ---- Layout / Align & Distribute ----------------------------------
    # ---- Debug -------------------------------------------------------------
    # Each writes a report to Documents/maya/uvstudio/uvstudio_debug_report.txt
    # and copies it to the clipboard. Tool tests run on a duplicate.
    _t("debug_all", "Run all tests", "Debug", "Tests", handler="debug_all",
       needs=SEL_ANY, fidelity="none",
       tooltip="Self tests, every tool on a duplicate (runs, changes UVs, "
               "one Ctrl+Z reverts), Split check, pins, viewport, parity. "
               "Select a mesh first. Report goes to file + clipboard."),
    _t("debug_tools", "Tool test", "Debug", "Tests", handler="debug_tools",
       needs=SEL_ANY, fidelity="none",
       tooltip="Run each native and UV Studio tool on a DUPLICATE of the "
               "selected mesh and check one undo reverts it."),
    _t("debug_split", "Split UVs check", "Debug", "Tests",
       handler="debug_split", needs=SEL_NONE, fidelity="none",
       tooltip="Builds a 2x2 plane, splits its centre UV, expects exactly 3 "
               "new UVs."),
    _t("debug_pins", "Pin round-trip", "Debug", "Tests", handler="debug_pins",
       needs=SEL_ANY, fidelity="none"),
    _t("debug_viewport", "Pair-mode viewport", "Debug", "Tests",
       handler="debug_viewport", needs=SEL_NONE, fidelity="none",
       tooltip="Which widget the pair-mode overlay attaches to."),
    _t("debug_parity", "Option parity", "Debug", "Tests",
       handler="debug_parity", needs=SEL_NONE, fidelity="none"),
    _t("debug_self", "Self tests", "Debug", "Tests", handler="debug_self",
       needs=SEL_NONE, fidelity="none"),

    _t("align_left", "Align L", "Layout", "Align & Distribute",
       handler="align", defaults={"edge": "left"}, needs=SEL_ANY),
    _t("align_right", "Align R", "Layout", "Align & Distribute",
       handler="align", defaults={"edge": "right"}, needs=SEL_ANY),
    _t("align_bottom", "Align B", "Layout", "Align & Distribute",
       handler="align", defaults={"edge": "bottom"}, needs=SEL_ANY),
    _t("align_top", "Align T", "Layout", "Align & Distribute",
       handler="align", defaults={"edge": "top"}, needs=SEL_ANY),
    _t("align_centre_u", "Centre U", "Layout", "Align & Distribute",
       handler="align", defaults={"edge": "centre_u"}, needs=SEL_ANY,
       tooltip="Line the selected units up on a shared vertical centre "
               "line (their horizontal centres match)."),
    _t("align_centre_v", "Centre V", "Layout", "Align & Distribute",
       handler="align", defaults={"edge": "centre_v"}, needs=SEL_ANY,
       tooltip="Line the selected units up on a shared horizontal centre "
               "line (their vertical centres match)."),
    _t("distribute_u", "Distribute U", "Layout", "Align & Distribute",
       handler="distribute", defaults={"axis": "u", "spacing": None},
       needs=SEL_ANY, fields=[_f("spacing", "float", label="gap", width=62,
                  optional=True)]),
    _t("distribute_v", "Distribute V", "Layout", "Align & Distribute",
       handler="distribute", defaults={"axis": "v", "spacing": None},
       needs=SEL_ANY, fields=[_f("spacing", "float", label="gap", width=62,
                                 optional=True)]),

    # ---- Layout / Orient ----------------------------------------------
    _t("orient_shells", "Orient Shells", "Layout", "Orient",
       cmd_key="orient_shells", needs=SEL_UV),
    _t("orient_edge", "Orient to Edge", "Layout", "Orient",
       cmd_key="orient_edge", needs=SEL_EDGE),
    _t("straighten_pca", "Straighten (PCA)", "Layout", "Orient",
       handler="orient_pca", defaults={"snap": 0.0}, needs=SEL_ANY,
       fields=[_f("snap", "float", label="snap", unit="\u00b0", width=62,
                  minimum=0.0, maximum=90.0, step=5.0, decimals=1,
                  tooltip="Round each correction to this many degrees. "
                          "0 rotates by the exact angle.")],
       tooltip="Rotate each shell so its long axis lies along U."),

    # ---- Layout / Stack & Pairs ---------------------------------------
    # The alt-drag on the Cluster Map will call link_pair with the two shells
    # the arrow touched. These buttons are the same command reached from the
    # keyboard, so there is one pairing path, not two.
    _t("link_pair", "Pair Selected", "Groups", "Pairs", handler="link_pair",
       defaults={"mode": "stack", "anchors": None}, needs=SEL_ANY, width=118,
       tooltip="Mark the selected shells as one unit that stacks. Stored on "
               "the mesh, so it survives the session."),
    _t("link_lock", "Lock Selected", "Groups", "Pairs", handler="link_lock",
       defaults={"anchors": None}, needs=SEL_ANY, width=118,
       tooltip="Move together, keep relative positions. Pairing without "
               "stacking."),
    _t("unlink", "Unlink", "Groups", "Pairs", handler="unlink",
       defaults={"anchors": None}, needs=SEL_ANY, width=118,
       tooltip="Forget any stored link touching the selected shells."),
    _t("list_links", "Show Links", "Groups", "Pairs", handler="list_links",
       needs=SEL_OBJECT, fidelity="none", width=118,
       tooltip="How many links resolve, and how many have gone stale."),
    _t("find_pairs", "Find Pairs", "Groups", "Pairs",
       handler="find_pairs", needs=SEL_OBJECT,
       tooltip="Report duplicate shell families and reclaimable area."),
    _t("stack_pairs", "Stack Pairs", "Groups", "Pairs",
       handler="stack_pairs", needs=SEL_OBJECT,
       tooltip="Move duplicate families on top of each other. Copies then "
               "share texture space."),
    _t("stack_native", "Stack Similar (Maya)", "Groups", "Stacks",
       cmd_key="stack_similar", needs=SEL_OBJECT, width=96,
       defaults={"tolerance": 0.001},
       fields=[_f("tolerance", "float", label="Tolerance", width=70,
                  decimals=5, verified=False)]),

    # ---- Layout / Pack -------------------------------------------------
    _t("analyze", "Analyze", "Pack", "Pack", handler="analyze",
       needs=SEL_OBJECT, fidelity="none", hidden=True,
       tooltip="Count units, pairs, stacks and pinned shells. Changes "
               "nothing."),
    _t("pack_preview", "Preview pack", "Pack", "Pack", handler="pack_preview",
       defaults={"padding": 0.002, "allow_rotation": True, "sort": "area",
                 "scale_to_fit": False, "udims": [1001],
                 "pre_orient": True, "overflow": True,
                 "max_overflow_tiles": 24},
       needs=SEL_OBJECT, fidelity="none", hidden=True,
       tooltip="Solve the layout and report it without moving anything."),
    _t("pack", "Pack", "Pack", "Pack", handler="pack", options_only=True,
       defaults={"padding": 0.002, "allow_rotation": True, "sort": "area",
                 "scale_to_fit": False, "udims": [1001],
                 "max_scale_passes": 12, "pre_orient": True,
                 "pre_orient_snap": 0.0, "overflow": True,
                 "max_overflow_tiles": 24},
       needs=SEL_OBJECT, width=96,
       fields=[_f("sort", "enum", label="Shell Distribution",
                  options=["area", "height", "perimeter"],
                  group="Pack Settings"),
               _f("allow_rotation", "bool", label="Rotate Shells",
                  group="Shell Transform Settings"),
               _f("pre_orient", "bool", label="Straighten first",
                  group="Shell Pre-Transform Settings"),
               _f("scale_to_fit", "bool", label="Scale to Fit",
                  group="Layout Settings"),
               _f("udims", "tiles", label="Tiles",
                  group="Layout Settings"),
               _f("padding", "float", label="Shell Padding", width=70,
                  decimals=5, group="Layout Settings"),
               _f("overflow", "bool", label="Overflow to more tiles",
                  group="Layout Settings")],
       advanced=[_f("pre_orient_snap", "float",
                    label="Straighten Snap (degrees)", optional=True,
                    group="Shell Pre-Transform Settings"),
                 _f("max_overflow_tiles", "int",
                    label="Max Overflow Tiles", group="Layout Settings"),
                 _f("max_scale_passes", "int", label="Scale Search Passes",
                    group="Layout Settings")],
       tooltip="UV Studio packer. Honours pins and moves units together."),
    _t("layout_maya", "Layout (Maya)", "Edit", "Arrange", cmd_key="layout",
       defaults={"layout": 2, "separate": 2, "percentageSpace": 0.2,
                 "rotateForBestFit": 1},
       needs=SEL_OBJECT, width=96,
       fields=[_f("percentageSpace", "float", label="Gap %", width=56,
                  decimals=2, verified=False)],
       advanced=[_f("rotateForBestFit", "int", label="Rotate for best fit",
                    verified=False),
                 _f("layout", "int", label="Layout mode", verified=False),
                 _f("separate", "int", label="Separate mode",
                    verified=False)]),
    _t("layout_u3d", "Layout (Unfold3D)", "Edit", "Arrange",
       cmd_key="layout_u3d", needs=SEL_OBJECT, width=96,
       defaults={"res": 256, "spacing": 0.002, "rot": 2},
       fields=[_f("spacing", "float", label="Spacing", width=62, decimals=4,
                  verified=False)],
       advanced=[_f("res", "int", label="Resolution", verified=False),
                 _f("rot", "int", label="Rotation step", verified=False)]),

    # ---- Layout / Density ----------------------------------------------
    _t("get_density", "Get Density", "Density", "Density",
       cmd_key="get_density", defaults={"mapSize": 512}, needs=SEL_FACE,
       fidelity="none", width=62,
       fields=[_f("mapSize", "int", label="Map size", width=70)]),
    _t("set_density", "Set Density", "Density", "Density",
       cmd_key="set_density", defaults={"density": 10.24, "mapSize": 512},
       needs=SEL_FACE, width=62,
       fields=[_f("density", "float", label="px/unit", width=80),
               _f("mapSize", "int", label="Map size", width=70)]),

    # ---- Create ---------------------------------------------------------
    # Flag names below come from the Maya command reference, not from a
    # running Maya. Every one is marked verified=False so the Cmds tab and
    # the first test session can list exactly what to confirm: settings are
    # passed through as kwargs, so a wrong flag name is a TypeError on the
    # first press, not a wrong result.
    _t("planar", "Planar", "Edit", "Create", cmd_key="planar",
       # Maya 2022 polyPlanarProjection: mapDirection x|y|z|c|p|b;
       # insertBeforeDeformers defaults on; createNewMap defaults off.
       # "b" (best plane) by default. "c" projects from the current camera
       # and failed with a bare "Maya command error" when no camera view was
       # in context - the one tool of fifteen that failed the Maya test.
       defaults={"mapDirection": "b", "keepImageRatio": True,
                 "insertBeforeDeformers": True, "createNewMap": False},
       needs=SEL_FACE, fidelity="topology", width=96,
       fields=[_f("mapDirection", "enum", label="Axis",
                  options=["x", "y", "z", "c", "p", "b"], width=56)],
       advanced=[_f("keepImageRatio", "bool", label="Keep image ratio"),
                 _f("insertBeforeDeformers", "bool",
                    label="Insert projection before deformers"),
                 _f("createNewMap", "bool", label="Create new UV set")]),
    _t("cylindrical", "Cylindrical", "Edit", "Create",
       cmd_key="cylindrical", needs=SEL_FACE, fidelity="topology", width=96,
       # Maya 2022 defaults, from the polyCylindricalProjection reference:
       # sweep 0-360 (default 360), scaleV is the map HEIGHT (default 90),
       # not 1.0 - a scaleV of 1 collapsed the cylinder and produced an
       # invalid projection, which is why this one tool failed while the
       # others worked.
       defaults={"projectionHorizontalSweep": 360.0,
                 "projectionScaleV": 90.0, "keepImageRatio": True,
                 "insertBeforeDeformers": True, "createNewMap": False},
       fields=[_f("projectionHorizontalSweep", "float", label="Sweep",
                  unit="\u00b0", width=62, decimals=1)],
       advanced=[_f("projectionScaleV", "float", label="Height"),
                 _f("keepImageRatio", "bool", label="Keep image ratio"),
                 _f("insertBeforeDeformers", "bool",
                    label="Insert projection before deformers"),
                 _f("createNewMap", "bool", label="Create new UV set")]),
    _t("spherical", "Spherical", "Edit", "Create",
       cmd_key="spherical", needs=SEL_FACE, fidelity="topology", width=96,
       defaults={"projectionScaleU": 180.0, "projectionScaleV": 90.0,
                 "insertBeforeDeformers": True, "createNewMap": False},
       fields=[_f("projectionScaleU", "float", label="Sweep U", unit="\u00b0",
                  width=56, decimals=1),
               _f("projectionScaleV", "float", label="V", unit="\u00b0",
                  width=56, decimals=1)],
       advanced=[_f("insertBeforeDeformers", "bool",
                    label="Insert projection before deformers"),
                 _f("createNewMap", "bool", label="Create new UV set")]),
    _t("automatic", "Automatic", "Edit", "Create", cmd_key="automatic",
       defaults={"planes": 6, "percentageSpace": 0.2, "optimize": 1},
       needs=SEL_FACE, fidelity="topology", width=96,
       fields=[_f("planes", "int", label="Planes", width=50,
                  verified=False),
               _f("percentageSpace", "float", label="Gap %", width=56,
                  decimals=2, verified=False)],
       advanced=[_f("optimize", "int", label="Optimize mode",
                    verified=False)]),
    _t("camera", "Camera-based", "Edit", "Create", cmd_key="camera",
       needs=SEL_FACE, fidelity="topology"),
    _t("contour", "Contour Stretch", "Edit", "Create",
       cmd_key="contour", needs=SEL_FACE, fidelity="topology"),

    # ---- Cut & Sew -------------------------------------------------------
    _t("cut", "Cut", "Edit", "Cut & Sew", handler="cut", needs=SEL_ANY,
       fidelity="topology",
       tooltip="Cut the currently selected UV or mesh edges in one press. A "
               "UV selection is converted to its bordering edges. For "
               "free-hand cutting, use the 3D Cut and Sew tool below."),
    _t("cut_tool", "3D Cut and Sew UV Tool", "Edit", "Interactive",
       cmd_key="cut_context", needs=SEL_NONE, fidelity="none",
       options_only=True,
       # symmetry and loopSpeed removed: texCutContext has neither flag on
       # 2022 (parity report). They were controls that did nothing.
       defaults={"steadyStroke": False, "steadyStrokeDistance": 4.0},
       tooltip="Enter the interactive cut-and-sew brush, then drag across "
               "edges to cut and hold to sew - no prior selection needed. "
               "This is a tool mode, unlike the selection-based Cut and Sew "
               "buttons above. Options match Maya's Tool Settings.",
       fields=[_f("steadyStroke", "bool", label="Steady Stroke",
                  group="Brush", verified=False),
               _f("steadyStrokeDistance", "float", label="Distance",
                  width=70, group="Brush", verified=False)]),
    _t("sew", "Sew", "Edit", "Cut & Sew", handler="sew", needs=SEL_ANY,
       fidelity="topology",
       tooltip="Sew the selected UV or mesh edges. A UV selection is "
               "converted to its bordering edges."),
    _t("sew_move", "Move and Sew", "Edit", "Cut & Sew", handler="sew_move",
       needs=SEL_ANY, fidelity="topology",
       tooltip="Move the smaller shell onto the larger and sew. Works from a "
               "UV selection, not only edges."),
    _t("split", "Split UVs", "Edit", "Cut & Sew", handler="split",
       needs=SEL_UV, fidelity="topology",
       tooltip="Separate the selected UVs so each belongs to one face. Uses "
               "Maya's Split if this build has one, else UV Studio's own. "
               "The log reports predicted vs actual new UVs."),
    _t("merge", "Merge UVs", "Edit", "Cut & Sew", cmd_key="merge",
       defaults={"distance": 0.001}, needs=SEL_UV, fidelity="topology",
       width=96,
       fields=[_f("distance", "float", label="Within", width=70,
                  decimals=5)]),

    # ---- Unfold -----------------------------------------------------------
    _t("unfold3d", "Unfold", "Edit", "Unfold", cmd_key="unfold",
       options_only=True,
       # fixNonManifold removed: u3dUnfold has no such flag (parity report),
       # so the checkbox never did anything.
       defaults={"iterations": 1,
                 "layoutUVs": False, "borderIntersection": True,
                 "triangleFlip": True, "mapSize": 1024, "roomSpace": 0},
       needs=SEL_UV, fidelity="snapshot",
       tooltip="Unfold3D solver. Honours pinned UVs.",
       fields=[_f("iterations", "int", label="Iterations", minimum=0,
                  maximum=10000, width=70, group="Solver Options",
                  verified=False),
               _f("layoutUVs", "bool", label="Layout UVs",
                  group="Solver Options", verified=False),
               _f("borderIntersection", "bool",
                  label="Prevent self border intersections",
                  group="Solver Options", verified=False),
               _f("triangleFlip", "bool", label="Prevent triangle flips",
                  group="Solver Options", verified=False),
               _f("mapSize", "int", label="Map size (Pixels)", minimum=2,
                  maximum=16384, width=70, group="Room Space Options",
                  verified=False),
               _f("roomSpace", "int", label="Room space (Pixels)", minimum=0,
                  maximum=1024, width=70, group="Room Space Options",
                  verified=False)]),
    _t("optimize", "Optimize", "Edit", "Unfold", cmd_key="optimize",
       defaults={"iterations": 1}, needs=SEL_UV, fidelity="snapshot",
       advanced=[_f("iterations", "int", label="Iterations", minimum=0,
                    group="Solver Options")]),
    _t("straighten_uvs", "Straighten UVs", "Edit", "Unfold",
       handler="straighten_uvs", needs=SEL_UV, fidelity="snapshot",
       defaults={"angle": 30.0, "axis_u": True, "axis_v": True},
       fields=[_f("angle", "float", label="angle", unit="\u00b0", width=58,
                  minimum=0.0, maximum=90.0, step=5.0, decimals=1,
                  tooltip="Edges within this many degrees of the axis are "
                          "straightened."),
               _f("axes", "axes", label="")],
       tooltip="Straighten runs of UVs along U, V or both. Runs Maya's "
               "texStraightenUVs."),
    _t("straighten_border", "Straighten Border", "Edit", "Unfold",
       cmd_key="straighten_border", needs=SEL_EDGE, fidelity="snapshot"),

    # ---- Pin ---------------------------------------------------------------
    _t("pin", "Pin", "Edit", "Pin", handler="pin", defaults={"value": 1.0},
       needs=SEL_UV),
    _t("unpin", "Unpin", "Edit", "Pin", handler="pin", defaults={"value": 0.0},
       needs=SEL_UV),
    _t("invert_pins", "Invert Pins", "Edit", "Pin", handler="invert_pins",
       needs=SEL_OBJECT),
    _t("select_pinned", "Select Pinned", "Edit", "Select",
       handler="select_pinned", needs=SEL_OBJECT, fidelity="none"),

    # ---- Check --------------------------------------------------------------
    _t("check_overlaps", "Overlaps", "Checks", "Audit", handler="check_overlaps",
       needs=SEL_OBJECT, fidelity="none"),
    _t("check_flipped", "Flipped", "Checks", "Audit", handler="check_flipped",
       needs=SEL_OBJECT, fidelity="none"),
    _t("check_density", "Density Audit", "Checks", "Audit",
       handler="check_density",
       defaults={"map_size": 1024, "target": None}, needs=SEL_OBJECT,
       fidelity="none",
       fields=[_f("map_size", "int", label="Map size", width=70)],
       advanced=[_f("target", "float", label="Target px/unit",
                    optional=True)]),
    # ---- Texture ----------------------------------------------------------
    _t("texture_snapshot", "Remember Layout", "Texture", "Texture",
       handler="texture_snapshot", needs=SEL_OBJECT, fidelity="none",
       tooltip="Keep a copy of the current UVs (UV set 'uvStudio_before') "
               "so textures can later be moved to follow your changes. "
               "Press BEFORE rearranging."),
    _t("texture_scan", "Find Textures", "Texture", "Texture",
       handler="texture_scan", needs=SEL_OBJECT, fidelity="none",
       tooltip="List the file textures on the selected meshes' materials."),
    _t("texture_transfer", "Transfer Textures", "Texture", "Texture",
       handler="texture_transfer", needs=SEL_OBJECT, fidelity="none",
       options_only=True,
       defaults={"resolution": "keep", "fixed_size": 2048, "padding": 4,
                 "suffix": "_uvs", "repoint": False, "source_set": ""},
       fields=[_f("resolution", "enum", label="Resolution",
                  options=["keep", "follow", "fixed"], group="Output",
                  tooltip="keep: same size as the source. follow: grow or "
                          "shrink with the shells to keep texel density. "
                          "fixed: the size below."),
               _f("fixed_size", "int", label="Fixed size (px)", minimum=16,
                  maximum=16384, group="Output"),
               _f("padding", "int", label="Padding (px)", minimum=0,
                  maximum=64, group="Output"),
               _f("suffix", "text", label="File suffix", group="Output"),
               _f("repoint", "bool",
                  label="Point file nodes at the new textures",
                  group="Scene"),
               _f("source_set", "text",
                  label="Source UV set (blank = remembered)",
                  group="Scene")],
       tooltip="Move each texture's pixels to follow the shells from the "
               "remembered layout to the current one. New files are saved "
               "beside the originals; nothing is overwritten."),
    _t("check_bounds", "Out of Bounds", "Checks", "Audit",
       handler="check_bounds", needs=SEL_OBJECT, fidelity="none"),
]


# =====================================================================
# Option-window parity
# =====================================================================
# The options Maya's own option windows offer, added from the uvstudio.parity()
# report run on Maya 2022 - so every NAME below is confirmed against a real
# Maya. Types and value meanings are from Maya's documentation.
#
# EVERY ONE DEFAULTS TO "Maya default" AND IS NOT SENT UNLESS CHANGED.
#   tristate  Maya default / On / Off  (None / True / False)
#   int/float optional; blank ("auto") means None
# filter_flags drops None values, so adding these changes no tool's
# behaviour until an artist sets one. That is the difference between
# exposing an option and silently overriding Maya's default for it.
#
# (tool id, key, kind, label, group, tooltip)
PARITY_OPTIONS = [
    # Layout (Maya) - polyLayoutUV
    ("layout_maya", "flipReversed", "tristate", "Flip reversed shells",
     "Layout Settings", "Flip shells whose UVs are reversed."),
    ("layout_maya", "layoutMethod", "int", "Layout method", "Layout Settings",
     "0 block stacking, 1 shape stacking."),
    ("layout_maya", "scale", "int", "Scale mode", "Layout Settings",
     "0 none, 1 uniform, 2 stretch to fit."),
    # Normalize - polyNormalizeUV
    ("normalize", "preserveAspectRatio", "tristate", "Preserve aspect ratio",
     "Settings", ""),
    ("normalize", "centerOnTile", "tristate", "Center on tile", "Settings", ""),
    ("normalize", "normalizeDirection", "int", "Direction", "Settings",
     "0 both, 1 U only, 2 V only."),
    # Unitize - polyForceUV
    ("unitize", "preserveAspectRatio", "tristate", "Preserve aspect ratio",
     "Settings", ""),
    # Flip (Maya) - polyFlipUV
    ("flip_native_u", "local", "tristate", "Flip each shell locally",
     "Settings", "On: each shell about its own centre. Off: about the "
                 "selection."),
    # Stack Similar - polyUVStackSimilarShells
    ("stack_native", "onlyMatch", "tristate", "Only stack exact matches",
     "Settings", ""),
    # Layout (Unfold3D) - u3dLayout
    ("layout_u3d", "rotateStep", "float", "Rotate step", "Shell Transform",
     "Degrees between rotations tried. Not the same flag as the older "
     "'Rotation step' field (confirmed by the parity report)."),
    ("layout_u3d", "preScaleMode", "int", "Pre-scale mode", "Shell Pre-Transform",
     "0 none, 1 preserve 3D ratio, 2 preserve UV ratio."),
    ("layout_u3d", "preRotateMode", "int", "Pre-rotate mode",
     "Shell Pre-Transform", "0 none, 1 horizontal, 2 vertical, 3 to X/Y."),
    ("layout_u3d", "layoutScaleMode", "int", "Layout scale mode",
     "Layout Settings", "0 none, 1 uniform, 2 non-uniform."),
    ("layout_u3d", "rotateMin", "float", "Rotate min", "Shell Transform", ""),
    ("layout_u3d", "rotateMax", "float", "Rotate max", "Shell Transform", ""),
    ("layout_u3d", "tileMargin", "float", "Tile margin", "Layout Settings",
     "Space kept clear at the tile edge."),
    ("layout_u3d", "mutations", "int", "Mutations", "Packing",
     "More tries a denser layout, slower."),
    ("layout_u3d", "tileAssignMode", "int", "Tile assignment", "Layout Settings",
     "0 current tile, 1 distribute across tiles."),
    # Straighten Border - polyStraightenUVBorder
    ("straighten_border", "curvature", "float", "Curvature", "Settings",
     "0 straight, 1 keep the current curve."),
    ("straighten_border", "preserveLength", "float", "Preserve length",
     "Settings", "0-1."),
    ("straighten_border", "blendOriginal", "float", "Blend original",
     "Settings", "0-1: mix back toward the original shape."),
    ("straighten_border", "gapTolerance", "int", "Gap tolerance", "Settings",
     "Border gaps up to this many edges are bridged."),
    # Contour Stretch - polyContourProjection
    ("contour", "method", "int", "Method", "Settings",
     "0 walk contours, 1 NURBS projection."),
    ("contour", "reduceShear", "float", "Reduce shear", "Settings", "0-1."),
    ("contour", "flipRails", "tristate", "Flip rails", "Settings", ""),
    ("contour", "smoothness0", "float", "Smoothness 1", "Smoothness", ""),
    ("contour", "smoothness1", "float", "Smoothness 2", "Smoothness", ""),
    ("contour", "smoothness2", "float", "Smoothness 3", "Smoothness", ""),
    ("contour", "smoothness3", "float", "Smoothness 4", "Smoothness", ""),
    # Automatic - polyAutoProjection
    ("automatic", "layout", "int", "Layout", "Layout",
     "0 overlap, 1 along U, 2 into square, 3 tile."),
    ("automatic", "scaleMode", "int", "Scale mode", "Layout",
     "0 none, 1 uniform, 2 stretch."),
    ("automatic", "layoutMethod", "int", "Shell layout", "Layout",
     "0 block stacking, 1 shape stacking."),
    ("automatic", "projectBothDirections", "tristate",
     "Project both directions", "Mapping Settings", ""),
    ("automatic", "skipIntersect", "tristate", "Skip intersection check",
     "Mapping Settings", ""),
    ("automatic", "insertBeforeDeformers", "tristate",
     "Insert projection before deformers", "Mapping Settings", ""),
    # Projections - smartFit and rotation, as in Maya's mapping options
    ("planar", "smartFit", "tristate", "Fit to best plane", "Projection", ""),
    ("planar", "rotationAngle", "float", "Image rotation", "Projection",
     "Degrees."),
    ("cylindrical", "smartFit", "tristate", "Fit to bounding box",
     "Projection", ""),
    ("cylindrical", "rotationAngle", "float", "Image rotation", "Projection",
     "Degrees."),
    ("spherical", "smartFit", "tristate", "Fit to bounding box",
     "Projection", ""),
    ("spherical", "rotationAngle", "float", "Image rotation", "Projection",
     "Degrees."),
    # Optimize - u3dOptimize (plug-in; lowercase flags)
    ("optimize", "borderintersection", "tristate",
     "Prevent self border intersections", "Solver Options", ""),
    ("optimize", "triangleflip", "tristate", "Prevent triangle flips",
     "Solver Options", ""),
    ("optimize", "power", "int", "Power", "Solver Options",
     "Strength of each iteration."),
    ("optimize", "surfangle", "float", "Surface angle", "Solver Options",
     "0-1: weight between angle and area."),
    ("optimize", "mapsize", "int", "Map size (pixels)", "Room Space Options",
     ""),
    ("optimize", "roomspace", "int", "Room space (pixels)",
     "Room Space Options", ""),
    # 3D Cut and Sew tool - texCutContext
    ("cut_tool", "touchToSew", "tristate", "Touch to sew", "Brush", ""),
    ("cut_tool", "displayShellBorders", "tristate", "Display shell borders",
     "Display", ""),
    ("cut_tool", "size", "float", "Brush size", "Brush", ""),
    ("cut_tool", "moveRatio", "float", "Move ratio", "Brush", ""),
    ("cut_tool", "edgeSelectSensitive", "float", "Edge select sensitivity",
     "Brush", ""),
]


def _add_parity_options():
    """Attach PARITY_OPTIONS to their tools' option windows, defaulted to
    None so none is sent until changed."""
    by_id = dict((t.tool_id, t) for t in TOOLS)
    for tool_id, key, kind, label, group, tip in PARITY_OPTIONS:
        tool = by_id.get(tool_id)
        if tool is None or key in tool.defaults:
            continue
        tool.defaults[key] = None
        tool.advanced.append(Field(
            key, kind, label=label, group=group, tooltip=tip,
            optional=kind in ("int", "float"),
            minimum=0 if kind == "int" else None,
            decimals=4 if kind == "float" else 4))


_add_parity_options()


# =====================================================================
# Native command inputs
# =====================================================================
# What each native command actually operates on, and at what scope.
#   takes   uv | edge | face | proc | context
#           proc    = a MEL procedure that acts on the current selection and
#                     takes no arguments (the UV Toolkit calls them bare)
#           context = an interactive tool entered with setToolTo
#   scope   as_is  - operate on exactly what is selected
#           shell  - grow the selection to whole UV shells first
# Maya's own UV Toolkit converts whatever the artist has selected into what
# the command needs - a shell, UVs, edges or faces - and so does this. The
# runner reads this table; no tool demands a particular selection mode.
COMMAND_INPUT = {
    "unfold": ("uv", "as_is"),
    "optimize": ("uv", "as_is"),
    "relax": ("uv", "as_is"),
    "layout": ("face", "shell"),
    "layout_u3d": ("uv", "shell"),
    "orient_shells": ("uv", "shell", "proc"),
    "orient_edge": ("edge", "as_is", "proc"),
    "normalize": ("uv", "shell"),
    "unitize": ("face", "as_is"),
    "flip": ("uv", "as_is"),
    "merge": ("uv", "as_is"),
    "stack_similar": ("uv", "shell"),
    "straighten_border": ("uv", "as_is"),
    "planar": ("face", "as_is"),
    "cylindrical": ("face", "as_is"),
    "spherical": ("face", "as_is"),
    "automatic": ("face", "as_is"),
    "camera": ("face", "as_is"),
    "contour": ("face", "as_is"),
    "cut_context": ("context", "as_is"),
}


def _relax_native_guards():
    """Native tools listed above convert selection themselves, so the guard
    only has to insist that SOMETHING is selected."""
    for tool in TOOLS:
        if tool.cmd_key in COMMAND_INPUT and tool.needs not in (SEL_NONE,):
            if COMMAND_INPUT[tool.cmd_key][0] != "context":
                tool.needs = SEL_ANY


_relax_native_guards()

_FLAG_CACHE = {}

# UI names that a command spells differently. Maya's Unfold option window
# says "Layout UVs"; the Unfold3D plug-in's flag for it is -pack.
# All three pairs were found by uvstudio.parity() on Maya 2022, not guessed.
FLAG_ALIASES = {
    "u3dUnfold": {"layoutuvs": "pack"},
    "u3dLayout": {"spacing": "shellspacing"},
    # 2022's cylindrical sweep is projectionScaleU; the docs page that named
    # projectionHorizontalSweep describes a different release.
    "polyCylindricalProjection": {"projectionhorizontalsweep":
                                  "projectionscaleu"},
}


def command_flags(name):
    """{lowercase flag: exact flag} as THIS Maya declares them, via `help`.

    The Unfold3D commands (u3dUnfold, u3dOptimize, u3dLayout) are plug-in
    commands with no documentation page; `help` is the only authority on
    their flags, and they are lowercase where the UI's settings were
    camelCase. Asking Maya, once per command per session, makes the flags
    right on every version without guessing. {} means help gave nothing, and
    the caller then passes settings through unfiltered.
    """
    if name in _FLAG_CACHE:
        return _FLAG_CACHE[name]
    flags = {}
    try:
        import maya.mel as mel
        text = mel.eval("help %s" % name) or ""
        for token in re.findall(r"(?<![\w-])-([A-Za-z][A-Za-z0-9]*)", text):
            flags.setdefault(token.lower(), token)
    except Exception:
        flags = {}
    _FLAG_CACHE[name] = flags
    return flags


def filter_flags(name, settings):
    """(flags Maya accepts, keys dropped). Keys are matched case-insensitively
    and renamed to Maya's spelling, so 'borderIntersection' reaches the
    plug-in as 'borderintersection'."""
    known = command_flags(name)
    if not known:
        return dict(settings), []
    aliases = FLAG_ALIASES.get(name, {})
    kept, dropped = {}, []
    settings = dict(settings)
    # createNewMap=False made polyPlanarProjection fail ("Maya command
    # error"), found by the debug bisect: the flag is only valid when on, and
    # then Maya wants a set name. So: omitted when off, named when on.
    if "createNewMap" in settings:
        if settings.get("createNewMap"):
            settings.setdefault("uvSetName", "uvStudio_projection")
        else:
            settings.pop("createNewMap")
    for key, value in settings.items():
        lookup = aliases.get(str(key).lower(), str(key).lower())
        declared = known.get(lookup)
        if declared is None:
            dropped.append(key)
        elif value is not None:
            kept[declared] = value
    return kept, sorted(dropped)


# Flags every poly/modifier command carries that no option window shows.
_GENERIC_FLAGS = {"caching", "constructionhistory", "name", "nodestate",
                  "edit", "query", "exists", "help", "frozen", "perinstance",
                  "worldspace", "uvsetname", "image", "image1", "image2",
                  "image3", "dumpcommand", "history", "i1", "i2", "i3"}


def command_flag_pairs(name):
    """[(short, long)] from `help <name>` - long names for the report."""
    try:
        import maya.mel as mel
        text = mel.eval("help %s" % name) or ""
    except Exception:
        return []
    return re.findall(r"(?<![\w-])-([A-Za-z]\w*)\s+-([A-Za-z]\w*)", text)


def parity_report(resolver):
    """Every native tool's settings against what this Maya's command accepts.

    For each tool: the command it runs, which of its settings Maya accepts,
    which it rejects (would be dropped at run time), and which of the
    command's own options UV Studio does not expose yet. Built from `help`,
    so it is exact for the running Maya - including the Unfold3D plug-in
    commands, which have no documentation page to check against.
    """
    rows = []
    for tool in TOOLS:
        if not tool.cmd_key:
            continue
        name = resolver.name_of(tool.cmd_key) if resolver.available(
            tool.cmd_key) else None
        keys = set(tool.defaults)
        for f in list(getattr(tool, "fields", None) or []) + list(
                getattr(tool, "advanced", None) or []):
            if getattr(f, "key", None) and f.kind not in ("axes",):
                keys.add(f.key)
        row = OrderedDict([("tool", tool.label), ("command", name)])
        spec = COMMAND_INPUT.get(tool.cmd_key)
        if name is None:
            row["status"] = "command not on this build"
        elif spec and len(spec) > 2 and spec[2] == "proc":
            row["status"] = "MEL procedure - runs on selection, no options"
        else:
            pairs = command_flag_pairs(name)
            known = dict((x.lower(), x) for pair in pairs for x in pair)
            if not known:
                row["status"] = "help returned no flags"
            else:
                alias = FLAG_ALIASES.get(name, {})
                # Short -> long, so a setting stored under a short flag
                # ("rot") counts its long name ("rotateStep") as exposed.
                # Without this the report listed options as missing that
                # were already exposed under their short spelling.
                to_long = dict((s_.lower(), l.lower()) for s_, l in pairs)

                def norm(k, _alias=alias, _long=to_long):
                    key = _alias.get(k.lower(), k.lower())
                    return _long.get(key, key)
                accepted = sorted(k for k in keys if norm(k) in known)
                rejected = sorted(k for k in keys if norm(k) not in known)
                used = set(norm(k) for k in accepted)
                known.update(dict((l.lower(), l) for _s, l in pairs))
                longs = sorted(set(l for _s, l in pairs))
                unexposed = [l for l in longs if l.lower() not in used
                             and l.lower() not in _GENERIC_FLAGS]
                row["status"] = "REJECTED SETTINGS" if rejected else "ok"
                row["accepted"] = accepted
                row["rejected"] = rejected
                row["not_exposed"] = unexposed
        rows.append(row)
    return rows


def format_parity(rows):
    lines = ["UV Studio option parity against this Maya", "=" * 60]
    for row in rows:
        lines.append("%s  [%s]  %s" % (row["tool"], row.get("command") or "-",
                                       row["status"]))
        for key in ("rejected", "not_exposed"):
            if row.get(key):
                lines.append("    %-12s %s" % (key + ":", ", ".join(row[key])))
    bad = [r for r in rows if r["status"] == "REJECTED SETTINGS"]
    lines.append("=" * 60)
    lines.append("%d tools, %d with rejected settings" % (len(rows), len(bad)))
    return "\n".join(lines)


# =====================================================================
# Redesign v2 (+ the artist's edit): new tools, and the panel as DATA
# =====================================================================
# Every tab below is drawn from PANEL_LAYOUT by M5c. A row is a label in
# the left gutter plus cells spread by weight; a grid is N even columns.
# Cells bind to a tool's own stored settings, so a field here and the
# tool's option box edit the same value. Changing the design means
# editing this table, never the panel code.

_PARITY_TOOLS = [
    # (id, label, tab, section, cmd_key or None, handler or None, defaults,
    #  needs, fidelity, tooltip)
    ("best_plane", "Best Plane", "Edit", "Create", "planar", None,
     {"mapDirection": "b", "keepImageRatio": True,
      "insertBeforeDeformers": True, "createNewMap": False},
     SEL_ANY, "topology", "Planar projection fitted to the faces' best plane."),
    ("normal_based", "Normal-Based", "Edit", "Create", "normal_based", None,
     {}, SEL_ANY, "topology", "Project from the average face normal."),
    ("auto_seams", "Auto Seams", "Edit", "Cut & Sew", "auto_seams", None,
     {}, SEL_ANY, "topology", "Unfold3D chooses and cuts seams."),
    ("create_uv_shell", "Create UV Shell", "Edit", "Cut & Sew",
     "create_uv_shell", None, {}, SEL_ANY, "topology",
     "Cut the selected faces out as one new shell."),
    ("create_shell_grid", "Create Shell (Grid)", "Edit", "Cut & Sew",
     "create_shell_grid", None, {}, SEL_ANY, "topology",
     "Cut the selected faces out as a grid-straightened shell."),
    ("stitch", "Stitch Together", "Edit", "Cut & Sew", "stitch", None, {},
     SEL_ANY, "topology", "Stitch the selected shells along their seam."),
    ("unfold_tool", "Unfold Tool", "Edit", "Interactive", "unfold_ctx", None,
     {}, SEL_NONE, "none", "Brush that unfolds where you drag."),
    ("optimize_tool", "Optimize Tool", "Edit", "Interactive", "optimize_ctx",
     None, {}, SEL_NONE, "none", "Brush that relaxes distortion."),
    ("symmetrize", "Symmetrize", "Edit", "Interactive", "symmetrize_ctx",
     None, {}, SEL_NONE, "none", "Mirror one half of a shell onto the other."),
    ("unfold_legacy", "Unfold (Legacy)", "Edit", "Unfold", "unfold_legacy",
     None, {}, SEL_ANY, "snapshot", "Maya's original unfold solver."),
    ("unfold_along_u", "Unfold Along U", "Edit", "Unfold", None,
     "unfold_along", {"axis": "u"}, SEL_ANY, "snapshot",
     "Unfold, keeping every UV's V: shells unfold in U only."),
    ("unfold_along_v", "Unfold Along V", "Edit", "Unfold", None,
     "unfold_along", {"axis": "v"}, SEL_ANY, "snapshot",
     "Unfold, keeping every UV's U: shells unfold in V only."),
    ("straighten_shell", "Straighten Shell", "Edit", "Unfold",
     "straighten_shell", None, {}, SEL_ANY, "snapshot",
     "Straighten the whole shell into a grid."),
    ("relax", "Relax", "Edit", "Unfold", "relax", None, {}, SEL_ANY,
     "snapshot", "Even out UV spacing without unfolding."),
    ("linear_align", "Linear Align", "Layout", "Align & Distribute",
     "linear_align", None, {}, SEL_ANY, "exact",
     "Line the selected UVs up between the two end UVs."),
    ("snap_tile", "Snap", "Layout", "Align & Distribute", None, "snap_tile",
     {"anchor": "sw", "u0": 0.0, "u1": 1.0, "v0": 0.0, "v1": 1.0}, SEL_ANY,
     "exact", "Move the selection so its anchor point lands on the tile "
              "range's matching point."),
    ("snap_together", "Snap Together", "Layout", "Align & Distribute",
     "snap_together", None, {}, SEL_ANY, "exact",
     "Snap one shell to another by their selected UVs."),
    ("snap_stack", "Snap and Stack", "Layout", "Align & Distribute",
     "snap_stack", None, {}, SEL_ANY, "exact", ""),
    ("match_grid", "Match Grid", "Layout", "Align & Distribute",
     "match_grid", None, {}, SEL_ANY, "exact", ""),
    ("match_uvs", "Match UVs", "Layout", "Align & Distribute", "match_uvs",
     None, {}, SEL_ANY, "exact", ""),
    ("stack_shells", "Stack", "Groups", "Stacks", "stack_shells", None, {},
     SEL_ANY, "exact", "Maya's Stack."),
    ("unstack_shells", "Unstack", "Groups", "Stacks", "unstack_shells", None,
     {}, SEL_ANY, "exact", ""),
    ("stack_orient", "Stack & Orient", "Groups", "Stacks", "stack_orient",
     None, {}, SEL_ANY, "exact", ""),
    ("gather", "Gather", "Groups", "Stacks", "gather", None, {}, SEL_ANY,
     "exact", "Pull the selected shells together."),
    ("randomize", "Randomize", "Groups", "Stacks", "randomize", None, {},
     SEL_ANY, "exact", ""),
    ("density_get", "Get", "Density", "Density", None, "density_get",
     {"mapSize": 512}, SEL_ANY, "none",
     "Measure the selection's texel density into the field beside Set."),
    ("parity_scan", "Parity scan", "Debug", "Tests", None, "parity_scan",
     {}, SEL_NONE, "none", "List every UV command this Maya has."),
]

for _p in _PARITY_TOOLS:
    if not any(t.tool_id == _p[0] for t in TOOLS):
        TOOLS.append(Tool(_p[0], _p[1], _p[2], _p[3], cmd_key=_p[4],
                          handler=_p[5], defaults=_p[6], needs=_p[7],
                          fidelity=_p[8], tooltip=_p[9]))
# The Move row's "Retain component spacing" and the texel-density unit.
[t for t in TOOLS if t.tool_id == "move"][0].defaults.setdefault(
    "retain_spacing", True)

# texGetTexelDensity / texSetTexelDensity are MEL procedures taking
# POSITIONAL arguments. Called as commands they received -mapSize flags
# and failed; they run through handlers now.
for _t_ in TOOLS:
    if _t_.tool_id == "get_density":
        _t_.cmd_key, _t_.handler, _t_.needs = None, "density_get", SEL_ANY
    elif _t_.tool_id == "set_density":
        _t_.cmd_key, _t_.handler, _t_.needs = None, "density_set", SEL_ANY

COMMAND_INPUT.update({
    "auto_seams": ("face", "as_is"),
    "normal_based": ("face", "as_is", "proc"),
    "create_uv_shell": ("face", "as_is", "proc"),
    "create_shell_grid": ("face", "as_is", "proc"),
    "stitch": ("uv", "as_is", "proc"),
    "unfold_ctx": ("context", "as_is"),
    "optimize_ctx": ("context", "as_is"),
    "symmetrize_ctx": ("context", "as_is"),
    "unfold_legacy": ("uv", "as_is"),
    "straighten_shell": ("uv", "shell", "proc"),
    "relax": ("uv", "as_is"),
    "linear_align": ("uv", "as_is", "proc"),
    "snap_together": ("uv", "as_is", "proc"),
    "snap_stack": ("uv", "shell", "proc"),
    "match_grid": ("uv", "shell", "proc"),
    "match_uvs": ("uv", "as_is", "proc"),
    "stack_shells": ("uv", "shell", "proc"),
    "unstack_shells": ("uv", "shell", "proc"),
    "gather": ("uv", "shell", "proc"),
    "randomize": ("uv", "shell", "proc"),
    "stack_orient": ("uv", "shell", "proc"),
})


# ---- cell constructors ------------------------------------------------
def c_run(tool, label=None, icon=None, w=1.0, over=None, primary=False):
    if over is not None and not isinstance(over, dict):
        raise TypeError("c_run(%r): overrides must be a dict" % tool)
    return {"t": "run", "tool": tool, "label": label, "icon": icon, "w": w,
            "over": over or {}, "primary": primary}


def c_num(tool, key, w=1.0, decimals=4):
    return {"t": "num", "tool": tool, "key": key, "w": w, "dec": decimals}


def c_text(tool, key, w=1.0):
    return {"t": "text", "tool": tool, "key": key, "w": w}


def c_tog(tool, key, label, w=0.5):
    return {"t": "tog", "tool": tool, "key": key, "label": label, "w": w}


def c_radio(tool, key, value, label, w=0.5):
    return {"t": "radio", "tool": tool, "key": key, "value": value,
            "label": label, "w": w}


def c_check(tool, key, label, w=1.0):
    return {"t": "check", "tool": tool, "key": key, "label": label, "w": w}


def c_choice(tool, key, options, w=1.0):
    return {"t": "choice", "tool": tool, "key": key, "options": options,
            "w": w}


def c_label(text, w=0.3):
    return {"t": "label", "text": text, "w": w}


def c_pick(pick, options, w=1.0):
    """A panel-level choice of WHICH tool a later cell runs (engine,
    method). options: [(label, tool id)]."""
    return {"t": "pick", "pick": pick, "options": options, "w": w}


def c_runpick(pick, label, icon=None, w=1.0, primary=False, options=False):
    return {"t": "runpick", "pick": pick, "label": label, "icon": icon,
            "w": w, "primary": primary, "options": options}


def c_widget(name, w=1.0):
    return {"t": "widget", "name": name, "w": w}


def r_row(label, cells):
    return ("row", label, cells)


def r_grid(cols, cells):
    return ("grid", cols, cells)


def r_note(text):
    return ("note", text, None)


PICKS = {
    "unfold_method": [("Unfold3D", "unfold3d"), ("Legacy", "unfold_legacy")],
    "layout_engine": [("UV Studio", "pack"), ("Unfold3D", "layout_u3d"),
                      ("Maya", "layout_maya")],
}
_SNAP9 = [("sw", "SW"), ("s", "S"), ("se", "SE"), ("w", "W"), ("c", "C"),
          ("e", "E"), ("nw", "NW"), ("n", "N"), ("ne", "NE")]
_MOVE = "move"

PANEL_LAYOUT = OrderedDict([
    ("UV", [
        ("Transform", [
            r_row("Pivot", [c_widget("pivot")]),
            r_row("Move", [c_label("U"), c_num(_MOVE, "u", 1, 3),
                           c_label("V"), c_num(_MOVE, "v", 1, 3),
                           c_run(_MOVE, "Move", "move", 1.1)]),
            r_row("", [c_run("nudge", "", "left", 0.45,
                             {"axis": "u", "direction": -1}),
                       c_num("nudge", "step", 0.8, 3),
                       c_run("nudge", "", "right", 0.45,
                             {"axis": "u", "direction": 1}),
                       c_run("nudge", "", "up", 0.45,
                             {"axis": "v", "direction": 1}),
                       c_run("nudge", "", "down", 0.45,
                             {"axis": "v", "direction": -1}),
                       c_tog(_MOVE, "snap_enabled", "Snap", 0.7),
                       c_num(_MOVE, "snap_step", 0.6, 2)]),
            r_row("", [c_check(_MOVE, "retain_spacing", "Retain spacing",
                               1.5),
                       c_run("distribute_u", "Distribute U", None, 1.1),
                       c_run("distribute_v", "V", None, 0.4)]),
            r_row("Rotate", [c_num("rotate", "degrees", 1.2, 2),
                             c_run("rotate_ccw", "", "rot_ccw", 0.4),
                             c_run("rotate_cw", "", "rot_cw", 0.4),
                             c_tog("rotate", "snap_enabled", "Snap", 0.8),
                             c_num("rotate", "snap_step", 0.6, 1),
                             c_run("rotate", "Apply", None, 0.8)]),
            r_row("Scale", [c_num("scale", "factor", 1.2, 3),
                            c_run("scale", "", "scale", 0.4),
                            c_tog("scale", "axis_u", "U", 0.4),
                            c_tog("scale", "axis_v", "V", 0.4),
                            c_tog("scale", "snap_enabled", "Snap", 0.8),
                            c_num("scale", "snap_step", 0.6, 2)]),
            r_row("", [c_check("scale", "prevent_negative", "No negative",
                               1.6),
                       c_run("flip_u", "Flip U", "flip", 1),
                       c_run("flip_v", "Flip V", "flip", 1)]),
            r_row("Texel\ndensity", [c_run("density_get", "Get", None, 0.7),
                                     c_num("set_density", "density", 1.2, 4),
                                     c_run("set_density", "Set", None, 0.7),
                                     c_label("Map", 0.4),
                                     c_num("set_density", "mapSize", 0.8,
                                           0)]),
        ]),
        ("Create", [
            r_grid(2, [c_run("automatic"), c_run("normal_based"),
                       c_run("cylindrical"), c_run("planar"),
                       c_run("spherical"), c_run("best_plane"),
                       c_run("camera"), c_run("contour")]),
        ]),
        ("Cut and Sew", [
            r_grid(1, [c_run("auto_seams")]),
            r_grid(2, [c_run("cut"), c_run("cut_tool", "Cut / Sew Tool"),
                       c_run("create_uv_shell"), c_run("create_shell_grid"),
                       c_run("sew"), c_run("stitch"),
                       c_run("split"), c_run("sew_move")]),
            r_row("Merge", [c_run("merge", "Merge UVs", None, 1.2),
                            c_label("within", 0.5),
                            c_num("merge", "distance", 1)]),
        ]),
        ("Unfold", [
            r_row("Method", [c_pick("unfold_method",
                                    PICKS["unfold_method"], 1.4),
                             c_runpick("unfold_method", "Unfold", None, 1,
                                       True),
                             c_runpick("unfold_method", "Options", None,
                                       0.8, options=True)]),
            r_grid(2, [c_run("optimize"), c_run("unfold_tool"),
                       c_run("optimize_tool"), c_run("relax")]),
            r_row("Along", [c_run("unfold_along_u", "Unfold along U"),
                            c_run("unfold_along_v", "Unfold along V")]),
            r_row("Straighten", [c_run("straighten_uvs", "Straighten UVs",
                                       None, 1.4),
                                 c_num("straighten_uvs", "angle", 0.7, 1),
                                 c_tog("straighten_uvs", "axis_u", "U", 0.4),
                                 c_tog("straighten_uvs", "axis_v", "V", 0.4)]),
            r_grid(2, [c_run("straighten_shell"),
                       c_run("straighten_border"), c_run("symmetrize"),
                       c_run("straighten_pca", "Straighten (PCA)")]),
            r_row("Pins", [c_run("pin"), c_run("unpin"),
                           c_run("invert_pins", "Invert"),
                           c_run("select_pinned", "Select", None, 0.9)]),
        ]),
        ("Align and Snap", [
            r_row("Align", [c_run("align_left", "", "align_left", 1),
                            c_run("align_centre_u", "", "align_centre_u", 1),
                            c_run("align_right", "", "align_right", 1),
                            c_run("align_top", "", "align_top", 1),
                            c_run("align_centre_v", "", "align_centre_v", 1),
                            c_run("align_bottom", "", "align_bottom", 1),
                            c_run("linear_align", "Linear", None, 2)]),
            r_row("Snap", [c_choice("snap_tile", "anchor", _SNAP9, 1),
                           c_label("U"), c_num("snap_tile", "u0", 0.6, 2),
                           c_num("snap_tile", "u1", 0.6, 2),
                           c_label("V"), c_num("snap_tile", "v0", 0.6, 2),
                           c_num("snap_tile", "v1", 0.6, 2),
                           c_run("snap_tile", "Snap", None, 0.8)]),
            r_grid(2, [c_run("snap_together"), c_run("snap_stack"),
                       c_run("match_grid"), c_run("match_uvs"),
                       c_run("normalize"), c_run("unitize")]),
        ]),
        ("Arrange and Layout", [
            r_grid(4, [c_run("orient_shells", "Orient"),
                       c_run("orient_edge", "To edge"),
                       c_run("stack_orient", "Stack+Orient"),
                       c_run("stack_shells", "Stack"),
                       c_run("unstack_shells", "Unstack"),
                       c_run("stack_native", "Similar"),
                       c_run("gather"), c_run("randomize"),
                       c_run("link_pair", "Pair"),
                       c_run("stack_pairs", "Stack pairs"),
                       c_run("flip_native_u", "Flip (Maya)"),
                       c_run("normalize", "Normalize")]),
            r_row("Layout", [c_pick("layout_engine", PICKS["layout_engine"],
                                    1.4),
                             c_num("pack", "padding", 0.8, 4),
                             c_runpick("layout_engine", "Options", None, 0.8,
                                       options=True)]),
            r_row("", [c_run("pack_preview", "Preview", None, 1),
                       c_runpick("layout_engine", "Layout", None, 1.4,
                                 True)]),
        ]),
    ]),
    ("Pack", [
        ("Pack", [
            r_row("Engine", [c_pick("layout_engine", PICKS["layout_engine"],
                                    1)]),
            r_row("Tiles", [c_text("pack", "udims", 1.4),
                            c_check("pack", "overflow", "Overflow", 1)]),
            r_row("Padding", [c_num("pack", "padding", 1, 4),
                              c_check("pack", "scale_to_fit", "Scale to fit",
                                      1.2)]),
            r_row("Shells", [c_check("pack", "allow_rotation", "Rotate", 1),
                             c_check("pack", "pre_orient", "Straighten first",
                                     1.4)]),
            r_row("", [c_run("pack_preview", "Preview", None, 1),
                       c_runpick("layout_engine", "Pack", None, 1.4, True)]),
        ]),
        ("Texel density", [
            r_row("Density", [c_run("density_get", "Get", None, 0.7),
                              c_num("set_density", "density", 1.2, 4),
                              c_run("set_density", "Set", None, 0.7)]),
            r_row("Map size", [c_num("set_density", "mapSize", 1, 0),
                               c_run("check_density", "Audit", None, 1)]),
        ]),
    ]),
    ("Groups", [
        ("Pairs", [
            r_note("Opening this tab saves the original layout for texture "
                   "transfer, if it has not been saved yet."),
            r_grid(3, [c_run("find_pairs"), c_run("stack_pairs"),
                       c_run("link_pair", "Pair"), c_run("link_lock", "Lock"),
                       c_run("unlink"), c_run("list_links", "Show links")]),
        ]),
        ("Stacks", [
            r_row("Tolerance", [c_num("stack_native", "tolerance", 1, 5),
                                c_run("stack_native", "Stack similar", None,
                                      1.4)]),
            r_grid(3, [c_run("stack_shells", "Stack"),
                       c_run("unstack_shells", "Unstack"),
                       c_run("stack_orient", "Stack+Orient"),
                       c_run("gather"), c_run("randomize")]),
        ]),
    ]),
    ("Checks", [
        ("Audit", [
            r_grid(3, [c_run("check_overlaps"), c_run("check_flipped"),
                       c_run("check_bounds"), c_run("analyze"),
                       c_run("find_pairs"), c_run("list_links", "Links")]),
            r_row("Density", [c_num("check_density", "map_size", 1, 0),
                              c_run("check_density", "Audit", None, 1.2)]),
        ]),
    ]),
    ("Texture", [
        ("Textures", [
            r_note("Opening this tab saves the original layout "
                   "automatically, if it has not been saved yet."),
            r_row("", [c_run("texture_scan", "Find textures", None, 1),
                       c_run("texture_snapshot", "Save current as original",
                             None, 1.4)]),
        ]),
        ("Transfer", [
            r_row("Resolution", [c_choice("texture_transfer", "resolution",
                                          [("Keep size", "keep"),
                                           ("Keep texel density", "follow"),
                                           ("Fixed", "fixed")], 1.4),
                                 c_num("texture_transfer", "fixed_size", 0.7,
                                       0)]),
            r_row("Padding", [c_num("texture_transfer", "padding", 0.6, 0),
                              c_label("Suffix", 0.5),
                              c_text("texture_transfer", "suffix", 0.8)]),
            r_row("Scene", [c_check("texture_transfer", "repoint",
                                    "Point file nodes at new maps", 2)]),
            r_row("", [c_run("texture_transfer", "Transfer textures", None,
                             1, primary=True)]),
        ]),
    ]),
    ("Debug", [
        ("Tests", [
            r_note("Testing only: this tab is removed before shipping."),
            r_grid(2, [c_run("debug_all"), c_run("debug_tools"),
                       c_run("debug_split"), c_run("debug_pins"),
                       c_run("debug_viewport"), c_run("debug_parity"),
                       c_run("debug_self"), c_run("parity_scan")]),
        ]),
    ]),
])


def layout_cells():
    """Every cell in PANEL_LAYOUT, for validation and search."""
    for tab, sections in PANEL_LAYOUT.items():
        for title, rows in sections:
            for kind, a, cells in rows:
                for cell in (cells or []):
                    yield tab, title, cell


def validate_layout():
    """Problems in PANEL_LAYOUT: unknown tools, keys a tool lacks."""
    problems = []
    ids = set(t.tool_id for t in TOOLS)
    for tab, title, cell in layout_cells():
        tool = cell.get("tool")
        if tool and tool not in ids:
            problems.append("%s/%s: unknown tool %r" % (tab, title, tool))
            continue
        key = cell.get("key")
        if tool and key and key not in find_tool(tool).defaults:
            problems.append("%s/%s: %s has no setting %r"
                            % (tab, title, tool, key))
        for _label, opt_tool in (PICKS.get(cell.get("pick")) or []):
            if opt_tool not in ids:
                problems.append("%s/%s: pick names unknown tool %r"
                                % (tab, title, opt_tool))
    return problems


# ---- suggestions ------------------------------------------------------
# What to offer next, from three signals in order: what the health strip
# found, what was just done, and what is selected. A short table the
# artist can tune - not guesswork.
NEXT_AFTER = {
    "cut": ["unfold3d", "sew"], "auto_seams": ["unfold3d", "optimize"],
    "create_uv_shell": ["unfold3d"], "split": ["unfold3d"],
    "unfold3d": ["optimize", "straighten_uvs", "straighten_shell"],
    "unfold_legacy": ["optimize", "straighten_uvs"],
    "optimize": ["straighten_uvs", "orient_shells"],
    "straighten_uvs": ["orient_shells", "pack"],
    "straighten_shell": ["orient_shells", "pack"],
    "orient_shells": ["pack", "stack_pairs"], "stack_pairs": ["pack"],
    "pack": ["check_overlaps", "texture_transfer"],
    "automatic": ["sew_move", "unfold3d"], "planar": ["unfold3d"],
}
BY_SELECTION = {
    "edge": ["cut", "sew", "sew_move", "straighten_border"],
    "face": ["planar", "automatic", "auto_seams", "create_uv_shell"],
    "uv": ["unfold3d", "straighten_uvs", "align_left", "pin"],
    "object": ["auto_seams", "automatic", "pack", "check_overlaps"],
}


def suggest(selection_kinds, last_tool=None, health=None, limit=4):
    out = []
    health = health or {}
    if health.get("overlaps") or health.get("outside"):
        out.append("pack")
    out.extend(NEXT_AFTER.get(last_tool, []))
    for kind in ("edge", "face", "uv", "object"):
        if kind in (selection_kinds or ()):
            out.extend(BY_SELECTION[kind])
            break
    seen, final = set(), []
    for tool_id in out:
        if tool_id not in seen:
            seen.add(tool_id)
            final.append(tool_id)
    return final[:limit]


def tools_by_tab():
    grouped = OrderedDict((tab, OrderedDict()) for tab in TAB_ORDER)
    for tool in TOOLS:
        grouped.setdefault(tool.tab, OrderedDict())
        grouped[tool.tab].setdefault(tool.section, []).append(tool)
    return grouped


def unverified_flags():
    """[(tool id, command key, flag), ...] for every flag taken from the
    docs rather than confirmed against a running Maya.

    Settings are handed to the command as kwargs, so a wrong flag name is a
    TypeError the first time the button is pressed. This turns "test the
    Create tab" into a list of exactly which names to check.
    """
    rows = []
    for tool in TOOLS:
        for field in tool.fields + tool.advanced:
            if field.verified:
                continue
            for key in field.keys():
                rows.append((tool.tool_id, tool.cmd_key or tool.handler, key))
    return sorted(rows)


def find_tool(tool_id):
    for tool in TOOLS:
        if tool.tool_id == tool_id:
            return tool
    raise KeyError("No tool %r" % tool_id)


# =====================================================================
# SECTION 3 - Preferences
# =====================================================================

class Preferences(object):
    """Section order, collapsed state and per-tool settings, on disk.

    Section order is stored as a list of names per tab and reconciled against
    the registry on load: unknown names are dropped and new ones appended. A
    prefs file from an older build therefore never hides a new section, and a
    removed section never leaves a hole.
    """

    FILENAME = "uvstudio_ui_prefs.json"

    def __init__(self, folder=None):
        self.folder = folder or self._default_folder()
        self.section_order = OrderedDict(
            (tab, list(sections)) for tab, sections in DEFAULT_SECTIONS.items())
        self.collapsed = {}
        self.tool_settings = {}
        self.pivots = {}
        self.splitter = None
        self.icon_only = False

    @staticmethod
    def _default_folder():
        if IN_MAYA:
            try:
                return os.path.join(cmds.internalVar(userAppDir=True), "uvstudio")
            except Exception:
                pass
        return os.path.join(os.path.expanduser("~"), ".uvstudio")

    @property
    def path(self):
        return os.path.join(self.folder, self.FILENAME)

    # -- section order ------------------------------------------------
    # Sections that were merged or renamed. A saved order that still uses an
    # old name keeps its position under the new one, so the artist's
    # arrangement survives a build that restructures the tab.
    SECTION_RENAMES = {"Align": "Align & Distribute",
                       "Distribute": "Align & Distribute"}

    def sections_for(self, tab, available):
        """Stored order, reconciled with what actually exists."""
        migrated = []
        for name in self.section_order.get(tab, []):
            name = self.SECTION_RENAMES.get(name, name)
            if name not in migrated:
                migrated.append(name)
        stored = [s for s in migrated if s in available]
        stored += [s for s in available if s not in stored]
        return stored

    def move_section(self, tab, section, delta):
        order = self.section_order.setdefault(tab, list(DEFAULT_SECTIONS.get(tab, [])))
        if section not in order:
            order.append(section)
        index = order.index(section)
        new_index = max(0, min(index + delta, len(order) - 1))
        if new_index == index:
            return index
        order.pop(index)
        order.insert(new_index, section)
        return new_index

    def reset_sections(self):
        self.section_order = OrderedDict(
            (tab, list(sections)) for tab, sections in DEFAULT_SECTIONS.items())

    # -- collapsed ----------------------------------------------------
    def is_collapsed(self, tab, section):
        return bool(self.collapsed.get("%s/%s" % (tab, section), False))

    def set_collapsed(self, tab, section, value):
        self.collapsed["%s/%s" % (tab, section)] = bool(value)

    # -- pivot --------------------------------------------------------
    # Stored per TAB, not per tool. One strip owns it, every transform in
    # that tab reads it, and nothing writes a second copy into a tool's own
    # settings - see ToolRunner.run for why that matters.
    DEFAULT_PIVOT = {"mode": "shell", "anchor": "c", "u": 0.5, "v": 0.5}

    def pivot(self, tab):
        merged = dict(self.DEFAULT_PIVOT)
        merged.update(self.pivots.get(tab) or {})
        return merged

    def set_pivot(self, tab, **changes):
        current = self.pivot(tab)
        current.update(dict((k, v) for k, v in changes.items()
                            if v is not None))
        self.pivots[tab] = current
        return current

    # -- tool settings ------------------------------------------------
    def settings_for(self, tool):
        """Defaults overlaid with whatever was last used.

        Only keys the tool still declares. A setting removed from a tool
        (Unfold's fixNonManifold) lived on in saved preferences and kept
        being sent - and reported as ignored - on every run.
        """
        merged = dict(tool.defaults)
        stored = self.tool_settings.get(tool.tool_id, {})
        merged.update(dict((k, v) for k, v in stored.items()
                           if k in tool.defaults))
        return merged

    def remember(self, tool_id, settings):
        self.tool_settings[tool_id] = dict(settings)

    # -- persistence --------------------------------------------------
    def to_dict(self):
        return OrderedDict([
            ("module", MODULE_ID), ("version", __version__),
            ("section_order", self.section_order),
            ("collapsed", self.collapsed),
            ("tool_settings", self.tool_settings),
            ("pivots", self.pivots),
            ("splitter", self.splitter),
            ("icon_only", self.icon_only),
        ])

    def load(self):
        try:
            with open(self.path, "r") as handle:
                data = json.load(handle)
        except Exception:
            return False
        self.section_order = OrderedDict(data.get("section_order") or {})
        self.collapsed = data.get("collapsed") or {}
        self.tool_settings = data.get("tool_settings") or {}
        self.pivots = data.get("pivots") or {}
        self.splitter = data.get("splitter")
        self.icon_only = bool(data.get("icon_only", False))
        return True

    def save(self):
        try:
            if not os.path.isdir(self.folder):
                os.makedirs(self.folder)
            with open(self.path, "w") as handle:
                json.dump(self.to_dict(), handle, indent=2)
            return True
        except Exception:
            return False


# =====================================================================
# SECTION 4 - Selection guard
# =====================================================================

class SelectionGuard(object):
    """Decides whether a tool may run, and says why not.

    Per the stated preference: with nothing selected, nothing happens and the
    artist is told. The guard never widens the selection or picks a target on
    the artist's behalf, because a tool that silently invents a target is how
    a whole asset gets repacked by accident.
    """

    MESSAGES = {
        SEL_UV: "Select some UVs first.",
        SEL_EDGE: "Select some edges first.",
        SEL_FACE: "Select some faces first.",
        SEL_OBJECT: "Select a mesh first.",
        SEL_ANY: "Select a shell, UVs, edges, faces or a mesh first.",
    }

    def __init__(self, selection_provider=None, hilite_provider=None):
        # Injected so the guard is testable without Maya. A caller that fakes
        # the selection gets NO hilite unless it fakes that too - otherwise a
        # test's "nothing selected" silently consulted the real scene, and in
        # a Maya session with a mesh hilited the test's premise was false.
        self.provider = selection_provider or self._maya_selection
        if hilite_provider is not None:
            self.hilite = hilite_provider
        elif selection_provider is None:
            self.hilite = self._hilited
        else:
            self.hilite = lambda: []

    @staticmethod
    def _maya_selection():
        if not IN_MAYA:
            return []
        try:
            return cmds.ls(selection=True, flatten=True, long=True) or []
        except Exception:
            return []

    @staticmethod
    def _hilited():
        if not IN_MAYA:
            return []
        try:
            return cmds.ls(hilite=True, long=True) or []
        except Exception:
            return []

    @staticmethod
    def classify(selection):
        kinds = set()
        meshes = set()
        for item in selection:
            if ".map[" in item:
                kinds.add(SEL_UV)
            elif ".e[" in item:
                kinds.add(SEL_EDGE)
            elif ".f[" in item:
                kinds.add(SEL_FACE)
            elif ".vtx[" in item:
                kinds.add("vertex")
            else:
                kinds.add(SEL_OBJECT)
            meshes.add(item.split(".")[0])
        return kinds, sorted(meshes)

    def check(self, tool):
        """Return (allowed, message, meshes)."""
        if tool.needs == SEL_NONE:
            return True, "", []

        selection = self.provider()
        if not selection and tool.needs in (SEL_OBJECT, SEL_ANY):
            # In component mode Maya's working object is HILITED, not
            # selected - pair mode lives there, and Preview/Pack were
            # refused with "select a mesh" on a mesh plainly being edited.
            hilited = self.hilite()
            if hilited:
                return True, "", hilited
        if not selection:
            return False, self.MESSAGES.get(tool.needs, "Select something first."), []

        kinds, meshes = self.classify(selection)

        if tool.needs == SEL_ANY or tool.needs == SEL_OBJECT:
            return True, "", meshes

        if tool.needs in kinds:
            return True, "", meshes

        # A component selection of the wrong type is a different problem from
        # an empty one, and saying so saves a guessing round.
        have = ", ".join(sorted(kinds)) or "nothing"
        return False, ("%s needs %s selected; you have %s."
                       % (tool.label, tool.needs, have)), meshes


# =====================================================================
# SECTION 5 - Tool runner
# =====================================================================

class ToolResult(object):
    __slots__ = ("tool_id", "ok", "message", "detail", "step")

    def __init__(self, tool_id, ok, message="", detail=None, step=None):
        self.tool_id = tool_id
        self.ok = ok
        self.message = message
        self.detail = detail
        self.step = step

    def __repr__(self):
        return "<ToolResult %s %s %s>" % (self.tool_id,
                                          "ok" if self.ok else "blocked",
                                          self.message)


class _NullChunk(object):
    """Stands in for an undo chunk when there is nothing to undo."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        return False


class ToolRunner(object):
    """Runs a tool: guard, resolve, execute in one undo chunk, record.

    `bridge` is M2's SceneBridge, `recipe` is M4's Recipe, `handlers` maps
    internal handler names to callables. All injected, so this class has no
    hard dependency on any of them and can be exercised with fakes.
    """

    def __init__(self, bridge=None, recipe=None, prefs=None, guard=None,
                 handlers=None, log=None):
        self.bridge = bridge
        self.recipe = recipe
        self.prefs = prefs or Preferences()
        self.guard = guard or SelectionGuard()
        self.handlers = dict(handlers or {})
        self.log = log or (lambda level, message: None)

    def run(self, tool_id, overrides=None, record=True, quiet=False):
        """Run one tool. `record` is False during replay.

        A replay that recorded its own steps would double the recipe every
        time it ran, and the second replay would double it again.
        """
        tool = find_tool(tool_id)
        if tool.handler and tool.needs in (SEL_UV, SEL_EDGE, SEL_FACE):
            self._coerce_selection(tool.needs)
        allowed, message, meshes = self.guard.check(tool)
        if not allowed:
            self.log("WARN", message)
            return ToolResult(tool_id, False, message)

        settings = self.prefs.settings_for(tool)
        settings.update(overrides or {})
        if tool.uses_pivot:
            # Injected for the run, stripped again before the settings are
            # remembered. A pivot copied into each tool's own entry is two
            # stored values for one user-visible control, and they diverge
            # the first time the strip is changed while a tool is greyed out.
            settings["pivot"] = self.prefs.pivot(tool.tab)

        try:
            if tool.handler:
                detail = self._run_handler(tool, settings, meshes)
            else:
                detail = self._run_command(tool, settings, meshes)
        except Exception as exc:
            self.log("ERROR", "%s failed: %s" % (tool.label, exc))
            return ToolResult(tool_id, False, "%s failed: %s" % (tool.label, exc),
                              detail=traceback.format_exc())

        # Only remember settings once the tool has actually succeeded with
        # them; persisting a set that just errored would reapply it next time.
        self.prefs.remember(tool_id, dict(
            (k, v) for k, v in settings.items() if k != "pivot"))

        step = self._record(tool, settings, meshes) if record else None
        if not quiet:
            self.log("OK", "%s" % tool.label)
        return ToolResult(tool_id, True, tool.label, detail=detail, step=step)

    # -- replay -------------------------------------------------------
    def replay(self, recipe=None, use_recorded_targets=False, dry_run=False):
        """Re-run a recipe's steps, in order, as one undo step.

        WHAT REPLAY IS FOR
            Applying one asset's UV treatment to the next one. That is why it
            runs against the CURRENT selection by default rather than the
            meshes the steps were recorded on - a recipe pinned to the mesh
            that made it can only ever redo work already done.
            `use_recorded_targets` is for the other case: rebuilding this
            asset after someone else's change.

        WHAT IT DOES NOT DO YET
            M4 emits an "apply" action for SNAPSHOT-fidelity steps whose
            stored UVs survived, meaning "write these exact values back". No
            path writes absolute UVs undoably yet, so those steps are
            RE-RUN instead and reported as degraded. Re-running a
            non-deterministic command is not the same as restoring its
            result, and silently pretending otherwise is exactly the kind of
            confident wrong answer this project keeps finding.

        Returns one record per action, so the panel can show what ran, what
        was skipped, and what came back different.
        """
        recipe = recipe or self.recipe
        if recipe is None:
            raise RuntimeError("No recipe to replay")

        actions = recipe.plan()
        records = []
        chunk = (self.bridge.chunk("UV Studio: replay")
                 if (self.bridge is not None and not dry_run)
                 else _NullChunk())

        with chunk:
            for action in actions:
                kind = action.get("kind")
                outcome = OrderedDict([("step", action.get("step")),
                                       ("kind", kind), ("status", ""),
                                       ("message", ""), ("degraded", False)])

                if action["action"] == "skip":
                    outcome["status"] = "skipped"
                    outcome["message"] = action.get("reason", "disabled")
                    records.append(outcome)
                    continue

                try:
                    tool = find_tool(kind)
                except KeyError:
                    # A recipe written by a newer build, or one whose tool was
                    # renamed. Reported rather than raised: one unknown step
                    # should not abandon the twelve after it.
                    outcome["status"] = "unknown"
                    outcome["message"] = "no tool named %r in this build" % kind
                    records.append(outcome)
                    continue

                if action["action"] == "apply":
                    outcome["degraded"] = True
                    outcome["message"] = ("snapshot restore not implemented; "
                                          "re-ran the command instead")
                elif action.get("degraded"):
                    outcome["degraded"] = True
                    outcome["message"] = action.get("reason", "degraded")

                if use_recorded_targets and action.get("targets") and IN_MAYA:
                    try:
                        cmds.select(action["targets"], replace=True)
                    except Exception:
                        outcome["status"] = "failed"
                        outcome["message"] = "recorded targets are gone"
                        records.append(outcome)
                        continue

                if dry_run:
                    outcome["status"] = "would run"
                    records.append(outcome)
                    continue

                result = self.run(kind, overrides=dict(action.get("params")
                                                       or {}), record=False)
                outcome["status"] = "ran" if result.ok else "failed"
                if not result.ok:
                    outcome["message"] = result.message
                records.append(outcome)

        return records

    # -- execution ----------------------------------------------------
    def _run_command(self, tool, settings, meshes):
        """Run a native command the way Maya's UV Toolkit would.

        1. Convert the selection to what the command operates on (COMMAND_
           INPUT), growing it to whole shells where the command works on
           shells. An artist can select a shell, UVs, edges, faces or the
           object; the command gets what it needs.
        2. Keep only the flags this Maya declares for the command (`help`),
           renamed to its exact spelling, and report the rest as dropped.
        3. MEL procedures act on the selection and take no arguments;
           interactive tools are entered as a context.
        """
        if self.bridge is None:
            raise RuntimeError("No scene bridge available")
        resolver = self.bridge.cmd
        if not resolver.available(tool.cmd_key):
            raise RuntimeError("%s is not available on this Maya build"
                               % tool.cmd_key)
        name = resolver.name_of(tool.cmd_key)
        spec = COMMAND_INPUT.get(tool.cmd_key)
        if spec is None:
            selection = self.guard.provider()
            return resolver.call(tool.cmd_key, selection, **settings)

        takes, scope = spec[0], spec[1]
        style = spec[2] if len(spec) > 2 else "command"
        import maya.cmds as cmds

        if takes == "context":
            return self._enter_context(cmds, name, settings)

        components = self._components_for(cmds, takes, scope)
        if not components:
            raise RuntimeError("Nothing to %s - select a shell, UVs, edges, "
                               "faces or the mesh." % tool.label)

        m2 = sys.modules.get("uvstudio_m2_scene_bridge")
        chunk = m2.undo_chunk("UV Studio: %s" % tool.label) if m2 else None
        if chunk is not None:
            chunk.__enter__()
        try:
            cmds.select(components, replace=True)
            if style == "proc":
                import maya.mel as mel
                mel.eval("%s;" % name)
                dropped = sorted(settings)
                route = "MEL %s on selection" % name
            else:
                flags, dropped = filter_flags(name, settings)
                getattr(cmds, name)(components, **flags)
                route = "%s(%d %s)" % (name, len(components), takes)
        finally:
            if chunk is not None:
                chunk.__exit__(None, None, None)
        detail = OrderedDict([("ran", route), ("scope", scope)])
        if dropped:
            detail["ignored_settings"] = ", ".join(dropped)
        return detail

    def _components_for(self, cmds, takes, scope):
        selection = cmds.ls(selection=True, flatten=True, long=True) or []
        if not selection:
            return []
        uvs = cmds.ls(cmds.polyListComponentConversion(
            selection, toUV=True) or [], flatten=True, long=True) or []
        if scope == "shell" and uvs:
            uvs = self._grow_to_shells(uvs) or uvs
        if takes == "uv":
            return uvs
        source = uvs if (scope == "shell" and uvs) else selection
        if takes == "face":
            return cmds.ls(cmds.polyListComponentConversion(
                source, toFace=True) or [], flatten=True, long=True) or []
        if takes == "edge":
            if any(".e[" in c for c in selection) and scope != "shell":
                return [c for c in selection if ".e[" in c]
            edges = cmds.polyListComponentConversion(
                source, toEdge=True, internal=True) or []
            if not edges:
                edges = cmds.polyListComponentConversion(
                    source, toEdge=True) or []
            return cmds.ls(edges, flatten=True, long=True) or []
        return selection

    def _grow_to_shells(self, uv_components):
        """Every UV of every shell the given UVs touch, via M2's analysis."""
        m2 = sys.modules.get("uvstudio_m2_scene_bridge")
        if m2 is None or self.bridge is None:
            return []
        out = []
        try:
            by_mesh = m2.selected_uv_ids_by_mesh(uv_components)
            for mesh, ids in by_mesh.items():
                wanted = set(ids)
                report = self.bridge.analyse_many([mesh])
                for shell in report["shells"]:
                    if wanted.intersection(shell.uv_ids):
                        out.extend(m2.expand_components(shell.mesh,
                                                        shell.uv_ids))
        except Exception:
            return []
        return out

    def _coerce_selection(self, needs):
        """Convert Maya's selection to the component type a handler needs,
        in place, so a shell or object selection reaches a UV tool as UVs."""
        if not IN_MAYA:
            return
        try:
            selection = cmds.ls(selection=True, flatten=True, long=True) or []
            if not selection:
                return
            marker = {SEL_UV: ".map[", SEL_EDGE: ".e[", SEL_FACE: ".f["}[needs]
            if all(marker in item for item in selection):
                return
            key = {SEL_UV: "toUV", SEL_EDGE: "toEdge", SEL_FACE: "toFace"}[needs]
            kwargs = {key: True}
            if needs == SEL_EDGE:
                kwargs["internal"] = True
            converted = cmds.polyListComponentConversion(selection, **kwargs)
            if not converted and needs == SEL_EDGE:
                converted = cmds.polyListComponentConversion(selection,
                                                             toEdge=True)
            if converted:
                cmds.select(converted, replace=True)
        except Exception:
            pass

    @staticmethod
    def _enter_context(cmds, name, settings):
        """Interactive tools are contexts: create once, set flags, enter.

        Calling the context command with a selection and flags, as every
        other command is called, creates a context NAMED after the selection
        and never enters it - which is how the 3D Cut and Sew tool failed.
        """
        ctx = "uvStudio_%s" % name
        make = getattr(cmds, name)
        if not make(ctx, exists=True):
            make(ctx)
        flags, dropped = filter_flags(name, settings)
        applied = []
        for flag, value in flags.items():
            try:
                make(ctx, edit=True, **{flag: value})
                applied.append(flag)
            except Exception:
                dropped.append(flag)
        cmds.setToolTo(ctx)
        detail = OrderedDict([("entered", ctx)])
        if dropped:
            detail["ignored_settings"] = ", ".join(sorted(dropped))
        return detail

    def _run_handler(self, tool, settings, meshes):
        handler = self.handlers.get(tool.handler)
        if handler is None:
            raise RuntimeError("No handler registered for %r" % tool.handler)
        return handler(tool=tool, settings=settings, meshes=meshes,
                       bridge=self.bridge)

    def _record(self, tool, settings, meshes):
        if self.recipe is None or tool.fidelity == "none":
            return None
        try:
            from uvstudio_m4_recipe import Step
        except ImportError:
            return None
        step = Step(kind=tool.tool_id, label=tool.label, params=dict(settings),
                    fidelity=tool.fidelity, targets=list(meshes))
        self.recipe.add(step)
        return step


# =====================================================================
# SECTION 6 - Self test (pure parts only)
# =====================================================================

def _check(label, got, want, failures):
    ok = got == want
    print("%-56s %s  (got %r)" % (label, "PASS" if ok else "FAIL", got))
    if not ok:
        failures.append(label)


def _check_true(label, condition, failures, detail=""):
    print("%-56s %s  %s" % (label, "PASS" if condition else "FAIL", detail))
    if not condition:
        failures.append(label)


def run_self_test():
    failures = []
    print("=" * 80)
    print("UV Studio M5 - UI Shell self test (v%s)" % __version__)
    print("=" * 80)

    print("--- registry ---")
    _check_true("every tool has a unique id",
                len(set(t.tool_id for t in TOOLS)) == len(TOOLS), failures,
                "%d tools" % len(TOOLS))
    _check_true("every tool sets exactly one of cmd_key/handler",
                all(bool(t.cmd_key) != bool(t.handler) for t in TOOLS), failures)
    _check_true("every tool's tab is a known tab",
                all(t.tab in TAB_ORDER for t in TOOLS), failures)
    _check_true("every tool's section is declared for its tab",
                all(t.section in DEFAULT_SECTIONS[t.tab] for t in TOOLS),
                failures)
    # Stacking decides which shells ARE one thing; every layout tool then
    # treats that as given. It gets a tab, not a section inside Layout.
    groups = [t for t in TOOLS if t.tab == "Groups"]
    _check_true("stacking has its own tab", len(groups) >= 2, failures,
                "%d tool(s) in Groups" % len(groups))
    _check_true("no stacking tool is left in Layout",
                not [t for t in TOOLS if t.tab == "Layout"
                     and "tack" in t.section], failures)
    _check_true("every tab declared has at least one tool",
                sorted(set(TAB_ORDER) - set(t.tab for t in TOOLS)) == [],
                failures,
                str(sorted(set(TAB_ORDER) - set(t.tab for t in TOOLS))))
    _check_true("every section declared has at least one tool",
                not [(tab, sec) for tab, secs in DEFAULT_SECTIONS.items()
                     for sec in secs
                     if not [t for t in TOOLS
                             if t.tab == tab and t.section == sec]],
                failures)

    print("\n--- preferences ---")
    legacy = Preferences(folder="/tmp/uvstudio_test_prefs")
    legacy.section_order["Layout"] = ["Orient", "Align", "Transform",
                                      "Distribute"]
    _check("old Align/Distribute order maps onto the merged section",
           legacy.sections_for("Layout", DEFAULT_SECTIONS["Layout"]),
           ["Orient", "Align & Distribute", "Transform"], failures)
    prefs = Preferences(folder="/tmp/uvstudio_test_prefs")
    available = DEFAULT_SECTIONS["Layout"]
    _check("default order matches the registry",
           prefs.sections_for("Layout", available), list(available), failures)
    # Relative to where it starts, so the test does not encode how many
    # sections the tab happens to have (merging Align and Distribute
    # shortened Layout and broke a hardcoded index).
    before = prefs.sections_for("Layout", available).index("Orient")
    prefs.move_section("Layout", "Orient", -1)
    moved = prefs.sections_for("Layout", available)
    _check_true("a section can be moved up",
                moved.index("Orient") == before - 1, failures, str(moved))
    prefs.move_section("Layout", "Transform", -5)
    _check("moving past the top clamps, it does not wrap",
           prefs.sections_for("Layout", available)[0], "Transform", failures)

    stale = Preferences(folder="/tmp/uvstudio_test_prefs")
    stale.section_order["Layout"] = ["Orient", "GhostSection", "Transform"]
    reconciled = stale.sections_for("Layout", available)
    _check_true("a section removed from the build is dropped from prefs",
                "GhostSection" not in reconciled, failures)
    _check_true("a section added by the build appears at the end",
                set(reconciled) == set(available), failures, str(reconciled))
    _check("stored order is honoured for what remains",
           reconciled[:2], ["Orient", "Transform"], failures)

    pack = find_tool("pack")
    _check("settings start at the tool defaults",
           prefs.settings_for(pack)["padding"], 0.002, failures)
    prefs.remember("pack", dict(pack.defaults, padding=0.01))
    _check("last-used settings override defaults",
           prefs.settings_for(pack)["padding"], 0.01, failures)
    _check_true("prefs round-trip through JSON",
                json.loads(json.dumps(prefs.to_dict()))["tool_settings"]["pack"]
                ["padding"] == 0.01, failures)

    print("\n--- selection guard ---")
    empty = SelectionGuard(lambda: [])
    allowed, message, _ = empty.check(find_tool("unfold3d"))
    _check("nothing selected blocks the tool", allowed, False, failures)
    _check("  and the message says what to select",
           message, "Select a shell, UVs, edges, faces or a mesh first.", failures)

    uvs = SelectionGuard(lambda: ["|car|bodyShape.map[0:99]"])
    _check("UV selection allows a UV tool",
           uvs.check(find_tool("unfold3d"))[0], True, failures)
    # Cut / Sew / Move-and-Sew take SEL_ANY: an edge selection is ideal, but a
    # UV selection is converted to its bordering edges rather than refused,
    # which is what an artist working in the UV editor expects.
    _check("UV selection allows Cut (it converts to edges)",
           uvs.check(find_tool("cut"))[0], True, failures)
    _check("UV selection allows Move and Sew",
           uvs.check(find_tool("sew_move"))[0], True, failures)

    edges = SelectionGuard(lambda: ["|car|bodyShape.e[4]", "|car|bodyShape.e[5]"])
    _check("edge selection allows Cut", edges.check(find_tool("cut"))[0],
           True, failures)
    obj = SelectionGuard(lambda: ["|car|bodyShape"])
    _check("object selection allows Pack", obj.check(find_tool("pack"))[0],
           True, failures)
    _check("a context tool needs no selection",
           empty.check(find_tool("cut_tool"))[0], True, failures)
    _, _, meshes = uvs.check(find_tool("unfold3d"))
    _check("the guard reports the mesh it found", meshes,
           ["|car|bodyShape"], failures)

    print("\n--- runner ---")
    calls = []

    def fake_handler(tool=None, settings=None, meshes=None, bridge=None):
        calls.append((tool.tool_id, dict(settings)))
        return "done"

    logged = []
    runner = ToolRunner(prefs=Preferences(folder="/tmp/uvstudio_test_prefs"),
                        guard=SelectionGuard(lambda: []),
                        handlers={"align": fake_handler},
                        log=lambda lvl, msg: logged.append((lvl, msg)))
    result = runner.run("align_left")
    _check("blocked tool returns not-ok", result.ok, False, failures)
    _check("  and nothing was executed", calls, [], failures)
    _check("  and the block was logged as a warning", logged[-1][0], "WARN",
           failures)

    runner.guard = SelectionGuard(lambda: ["|m.map[0]"])
    result = runner.run("align_left")
    _check("allowed tool runs its handler", result.ok, True, failures)
    _check("  with the tool's defaults", calls[-1][1]["edge"], "left", failures)
    result = runner.run("align_left", overrides={"edge": "right"})
    _check("overrides reach the handler", calls[-1][1]["edge"], "right",
           failures)
    _check("and are remembered for next time",
           runner.prefs.settings_for(find_tool("align_left"))["edge"], "right",
           failures)

    def exploding(**kwargs):
        raise RuntimeError("boom")

    runner.handlers["distribute"] = exploding
    before = dict(runner.prefs.tool_settings)
    result = runner.run("distribute_u", overrides={"axis": "v"})
    _check("a failing tool returns not-ok", result.ok, False, failures)
    _check_true("  and its settings are NOT remembered",
                runner.prefs.tool_settings == before, failures)
    _check_true("  and the traceback is kept for the log",
                result.detail is not None and "boom" in result.detail, failures)

    def _check_wrap(label, got, want):
        _check(label, got, want, failures)

    def _check_true_wrap(label, condition, detail=""):
        _check_true(label, condition, failures, detail)

    print("\n--- fields ---")
    # Field/default agreement is enforced in Tool.__init__, so importing this
    # module at all proves it. What is left to check is that the two routes
    # cover everything and never disagree about a key.
    overlaps = []
    unreachable = []
    for tool in TOOLS:
        inline = set(tool.inline_keys())
        sheet = set()
        for field in tool.sheet_fields():
            sheet.update(field.keys())
        if inline & sheet:
            overlaps.append((tool.tool_id, sorted(inline & sheet)))
        missing = set(k for k in tool.defaults if k != "pivot") - inline - sheet
        if missing:
            unreachable.append((tool.tool_id, sorted(missing)))
    _check_wrap("no setting appears both inline and in the sheet", overlaps, [])
    _check_wrap("every setting is reachable from one of the two", unreachable, [])

    # A setting whose default is None means None is a real value. If the
    # control cannot produce None it produces 0.0 instead, and for Distribute
    # that is a different operation rather than a default.
    unmarked = []
    for tool in TOOLS:
        for field in tool.fields + tool.advanced:
            if field.kind not in ("float", "int"):
                continue
            if tool.defaults.get(field.key, "") is None and not field.optional:
                unmarked.append((tool.tool_id, field.key))
    _check_wrap("every None-defaulted number field can express None",
                unmarked, [])

    # A hidden tool has no row, so something else must be able to reach it.
    orphans = [t.tool_id for t in TOOLS if t.hidden
               and t.tool_id not in ACTION_BAR
               and not any(f.runs == t.tool_id
                           for other in TOOLS for f in other.fields)]
    _check_wrap("every hidden tool is driven by some other tool's control",
                orphans, [])

    nudges = [(t.tool_id, f.runs) for t in TOOLS for f in t.fields
              if f.kind == "nudge"]
    _check_wrap("every nudge field names a tool that exists",
           [pair for pair in nudges
            if not any(t.tool_id == pair[1] for t in TOOLS)], [])

    unverified = unverified_flags()
    _check_true("unverified flags are declared, not assumed",
                all(f[2] in find_tool(f[0]).defaults for f in unverified),
                "%d flag(s) to confirm in Maya" % len(unverified))

    _check_wrap("every action-bar tool exists",
                [t for t in ACTION_BAR
                 if not any(x.tool_id == t for x in TOOLS)], [])
    _check_wrap("no action-bar tool writes except Pack",
                [t for t in ACTION_BAR if t != "pack"
                 and find_tool(t).fidelity != "none"], [])

    print("\n--- pivot ---")
    prefs = Preferences(folder=tempfile.mkdtemp())
    _check_wrap("pivot defaults to per-shell centres",
           prefs.pivot("Layout")["mode"], "shell")
    prefs.set_pivot("Layout", mode="selection", anchor="sw")
    _check_wrap("  the strip remembers a change", prefs.pivot("Layout")["anchor"],
           "sw")
    _check_wrap("  and it is scoped to its tab", prefs.pivot("Check")["mode"],
           "shell")

    pivot_runner = ToolRunner(prefs=prefs,
                              guard=SelectionGuard(lambda: ["|m.map[0]"]),
                              handlers={"rotate": lambda **kw: kw["settings"]})
    detail = pivot_runner.run("rotate").detail
    _check_true_wrap("a pivot tool receives the strip's pivot",
                detail.get("pivot", {}).get("mode") == "selection",
                str(detail.get("pivot")))
    _check_true_wrap("  and the pivot is not written into the tool's settings",
                "pivot" not in prefs.tool_settings.get("rotate", {}),
                str(sorted(prefs.tool_settings.get("rotate", {}))))

    print("\n--- panel layout (redesign v2) ---")
    _check("every layout cell names a real tool and setting",
           validate_layout(), [], failures)
    _check("edges selected: Cut and Sew are offered first",
           suggest({SEL_EDGE})[:2], ["cut", "sew"], failures)
    _check("after Unfold, Optimize is offered first",
           suggest({SEL_UV}, "unfold3d")[0], "optimize", failures)
    _check("overlaps found: Pack comes first",
           suggest({SEL_OBJECT}, None, {"overlaps": 3})[0], "pack", failures)
    _check("suggestions never repeat and stop at four",
           len(suggest({SEL_UV}, "unfold3d")), 4, failures)

    print("\n--- coverage ---")
    handlers_needed = sorted(set(t.handler for t in TOOLS if t.handler))
    print("  internal handlers to implement: %s" % ", ".join(handlers_needed))
    cmd_keys = sorted(set(t.cmd_key for t in TOOLS if t.cmd_key))
    print("  resolver keys used: %d" % len(cmd_keys))
    _check_true("no tool records a fidelity the recipe does not know",
                all(t.fidelity in ("exact", "snapshot", "topology", "none")
                    for t in TOOLS), failures)

    print("")
    print("=" * 80)
    print("%d failure(s)" % len(failures))
    for name in failures:
        print("  FAILED: %s" % name)
    print("=" * 80)
    return not failures


if __name__ == "__main__":
    run_self_test()
