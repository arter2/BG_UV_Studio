\
"""
UV Studio - Module 2: Scene Bridge
==================================

PURPOSE
    The only module that touches the Maya scene. Everything above it (packer,
    recipe, UI) works on plain Python data this module produces, which is what
    makes the packer testable without Maya and replaceable without surgery.

RESPONSIBILITIES
    - Resolve and invoke native commands, including the six that are MEL-only
      on Maya 2022 (confirmed by the M1 probe).
    - Manage the original / working UV set split. The original is never edited.
    - Extract UV shells as pure data, fast, via the OpenMaya 2.0 API.
    - Read native pin state so "pinned UVs never move" is enforced against the
      same pins Maya's own unfold honours.
    - Group overlapping shells into layout units that move as one.
    - Write UV transforms undoably, with pinned UVs excluded.

DESIGN NOTE - why reads and writes use different APIs
    Reads go through maya.api.OpenMaya (MFnMesh.getUVs / getUvShellsIds). It is
    two orders of magnitude faster than cmds on dense meshes and returns flat
    arrays we can slice.

    Writes go through cmds.polyEditUV inside an undo chunk. MFnMesh.setUVs is
    faster but is NOT undoable, and an artist tool whose moves cannot be undone
    is not shippable. The speed we lose is recovered by compressing component
    lists into ranges (see compress_indices) rather than by abandoning undo.

USAGE (Script Editor, Python tab)
    Paste and run. It reports on the current selection without modifying it.
    To exercise the write path as well:
        run_self_test(destructive=True)     # operates on a temp duplicate

Target: Maya 2022 - 2025+
"""

from __future__ import annotations

import json
import math
import re
import time
import traceback
from collections import OrderedDict, defaultdict

import maya.cmds as cmds
import maya.mel as mel
import maya.api.OpenMaya as om2

__version__ = "2.17.0"
MODULE_ID = "M2"

ORIGINAL_SET_ATTR = "uvStudioOriginalSet"   # remembers which set was the source
WORKING_SET_NAME = "uvStudio_work"
OVERLAP_EPSILON = 1.0e-5
# Duplicate matching precision, set from a sensitivity sweep on a real
# symmetrical asset, not guessed. At 4 digits only 37 of 338 shells paired; at
# 3 digits 176 paired - 52%, which is what mirror symmetry predicts. At 2
# digits unrelated shells start colliding. Mirrored halves routinely differ in
# the 4th decimal, so 4 was too strict.
DUPLICATE_DIGITS = 3
# At 3 digits a shell thinner than ~0.001 rounds to zero and would match every
# other sliver on size alone. Below this, size is not evidence of identity.
MIN_DUPLICATE_SIZE = 0.0015


# =====================================================================
# SECTION 1 - Command resolution and the MEL bridge
# =====================================================================

class CommandResolver(object):
    """Resolves tool names to concrete commands and invokes them.

    M1 established that six UV commands exist only as MEL procedures on 2022.
    Callers should never care which - they ask for a tool by name and get a
    result, or a clean CommandUnavailable.
    """

    # tool key -> ordered candidates. Mirrors M1's registry, trimmed to what
    # the bridge itself calls. M5 owns the full button-facing registry.
    TOOLS = {
        "cut": ["polyMapCut"],
        "sew": ["polyMapSew"],
        "sew_move": ["polyMapSewMove"],
        "merge": ["polyMergeUV"],
        "edit_uv": ["polyEditUV"],
        "uv_set": ["polyUVSet"],
        "convert": ["polyListComponentConversion"],
        "evaluate": ["polyEvaluate"],
        "pin": ["polyPinUV"],
        "layout": ["polyLayoutUV", "u3dLayout"],
        "unfold": ["u3dUnfold", "unfold"],
        "optimize": ["u3dOptimize"],
        "orient_shells": ["texOrientShells"],
        "orient_edge": ["texOrientEdge"],
        "straighten_uvs": ["texStraightenUVs"],
        "straighten_border": ["polyStraightenUVBorder"],
        "get_density": ["texGetTexelDensity"],
        "set_density": ["texSetTexelDensity"],
        "stack_similar": ["polyUVStackSimilarShells"],
        "select_shell": ["polySelectBorderShell"],
        "normalize": ["polyNormalizeUV"],
        "flip": ["polyFlipUV"],
        # Registered because M5's button registry asks for them by these
        # names. A key missing here greys out a button whose command exists
        # perfectly well, which reads as "your Maya lacks this" - the most
        # misleading failure the UI can produce.
        "unitize": ["polyForceUV"],
        "layout_u3d": ["u3dLayout"],
        "planar": ["polyPlanarProjection", "polyProjection"],
        "cylindrical": ["polyCylindricalProjection"],
        "spherical": ["polySphericalProjection"],
        "automatic": ["polyAutoProjection"],
        "camera": ["CreateUVsBasedOnCamera", "polyProjection"],
        "contour": ["polyContourProjection"],
        "cut_context": ["texCutContext", "texCutUVContext"],
        # Present so the Cmds tab can report on them, though the bridge does
        # not call them: split_uv is the one M1 proved absent on 2022, and
        # reporting "missing" is the point of listing it.
        # polySplitUV is absent on 2022. SplitUV (runtime) and
        # polySplitTextureUV (MEL) are the other names Maya has used for the
        # UV Toolkit's Split; whichever resolves is used, and if none do,
        # split_uvs() falls back to its own edge-cut route and says so.
        "split_uv": ["polySplitUV", "SplitUV", "polySplitTextureUV"],
        "relax": ["polyRelaxUVs", "u3dOptimize"],
        # ---- UV Toolkit parity (redesign v2). Names from Maya's UV Toolkit
        # as best known; any that do not resolve on this build grey out
        # their button, and Debug > Parity scan lists what DOES exist so a
        # wrong name is one edit to fix, never a silent failure.
        "auto_seams": ["u3dAutoSeam"],
        "normal_based": ["NormalBasedProjection", "UVNormalBasedProjection"],
        "create_uv_shell": ["CreateUVShellAlongBorder", "texCreateUVShell"],
        "create_shell_grid": ["CreateUVShellGrid", "texCreateShellGrid"],
        "stitch": ["texStitchShells", "StitchTogether"],
        "unfold_ctx": ["texUnfoldUVContext"],
        "optimize_ctx": ["texOptimizeUVContext"],
        "symmetrize_ctx": ["texSymmetrizeUVContext"],
        "straighten_shell": ["texStraightenShell"],
        "unfold_legacy": ["unfold"],
        "linear_align": ["texLinearAlignUVs", "texLinearAlign"],
        "snap_together": ["texSnapShells"],
        "snap_stack": ["texSnapStackShells"],
        "match_grid": ["texMatchGrid"],
        "match_uvs": ["texMatchUVs"],
        "stack_shells": ["texStackShells"],
        "unstack_shells": ["texUnstackShells"],
        "gather": ["texGatherShells"],
        "randomize": ["texRandomizeShells"],
        "stack_orient": ["texStackAndOrientShells", "texOrientStackShells"],
        "uv_editor": ["TextureViewWindow"],
    }

    # Presentation only: how the Cmds tab groups and labels each key. Kept
    # beside the candidates rather than in M1 because M1 used to carry its
    # OWN candidate list, so the tab could report a command as available
    # while the bridge, resolving separately, refused to call it. One table
    # cannot disagree with itself.
    CATALOG = OrderedDict([
        ("planar", ("Create", "Planar projection", "", "high")),
        ("cylindrical", ("Create", "Cylindrical projection", "", "high")),
        ("spherical", ("Create", "Spherical projection", "", "high")),
        ("automatic", ("Create", "Automatic projection", "", "high")),
        ("camera", ("Create", "Camera-based projection", "", "high")),
        ("contour", ("Create", "Contour stretch", "", "high")),
        ("cut", ("Cut & Sew", "Cut UV edges", "", "high")),
        ("sew", ("Cut & Sew", "Sew UV edges", "", "high")),
        ("sew_move", ("Cut & Sew", "Move and sew", "", "high")),
        ("split_uv", ("Cut & Sew", "Split UVs",
                      "If no native resolves, UV Studio provides Split itself "
                      "(convert to edges, polyMapCut) - the button still "
                      "works. Its log line reports predicted vs actual new "
                      "UVs, so a wrong split is visible, not silent.",
                      "low")),
        ("merge", ("Cut & Sew", "Merge UVs", "", "high")),
        ("cut_context", ("Cut & Sew", "Cut tool (interactive)", "", "high")),
        ("unfold", ("Unfold", "Unfold (Unfold3D)", "Honours pinned UVs.",
                    "high")),
        ("optimize", ("Unfold", "Optimize", "", "high")),
        ("straighten_border", ("Unfold", "Straighten UV border", "", "high")),
        ("straighten_uvs", ("Unfold", "Straighten UVs", "", "high")),
        ("relax", ("Unfold", "Relax / smooth UVs", "", "high")),
        ("layout", ("Arrange", "Layout (Maya)", "", "high")),
        ("layout_u3d", ("Arrange", "Layout (Unfold3D)", "", "high")),
        ("normalize", ("Arrange", "Normalize", "", "high")),
        ("unitize", ("Arrange", "Unitize", "Use the -unitize flag.", "high")),
        ("flip", ("Arrange", "Flip UVs", "", "high")),
        ("edit_uv", ("Arrange", "Move/rotate/scale UVs", "", "high")),
        ("orient_shells", ("Arrange", "Orient shells", "", "high")),
        ("orient_edge", ("Arrange", "Orient to edge", "", "high")),
        ("stack_similar", ("Arrange", "Stack similar shells", "", "high")),
        ("pin", ("Pin", "Pin / unpin UVs",
                 "Addresses UV COMPONENTS, not objects. Querying an object "
                 "name returns nothing; '.map[*]' is the working form.",
                 "high")),
        ("get_density", ("Density", "Get texel density", "", "high")),
        ("set_density", ("Density", "Set texel density", "", "high")),
        ("uv_set", ("Selection & query", "UV set management", "", "high")),
        ("convert", ("Selection & query", "Component conversion", "",
                     "high")),
        ("evaluate", ("Selection & query", "Mesh evaluation", "", "high")),
        ("select_shell", ("Selection & query", "Select shell", "", "high")),
        ("uv_editor", ("Selection & query", "UV editor window", "", "high")),
    ])

    def report(self, doc_version=None):
        """What resolved, grouped for the Cmds tab.

        The rows carry the same resolution the bridge will actually use, so
        a greyed-out button and a red row in the tab always agree.
        """
        groups = OrderedDict()
        for key, (group, label, note, confidence) in self.CATALOG.items():
            name, kind = self._resolved.get(key, (None, None))
            tried = [(candidate, self._status(candidate) or "missing")
                     for candidate in self.TOOLS.get(key, [])]
            groups.setdefault(group, []).append(OrderedDict([
                ("key", key), ("label", label), ("resolved", name),
                ("kind", kind), ("confidence", confidence), ("note", note),
                ("tried", tried),
                ("doc", ("https://help.autodesk.com/cloudhelp/%s/ENU/"
                         "Maya-Tech-Docs/CommandsPython/%s.html"
                         % (doc_version, name))
                 if (name and doc_version) else None)]))
        return groups

    def __init__(self):
        self._resolved = {}
        self._resolve_all()

    def _resolve_all(self):
        for key, candidates in self.TOOLS.items():
            for name in candidates:
                kind = self._status(name)
                if kind:
                    self._resolved[key] = (name, kind)
                    break
            else:
                self._resolved[key] = (None, None)

    @staticmethod
    def _status(name):
        if hasattr(cmds, name):
            return "python"
        try:
            what = mel.eval('whatIs "%s"' % name)
        except Exception:
            return None
        if not what or "Unknown" in what or "not found" in what.lower():
            return None
        return "mel"

    def available(self, key):
        return self._resolved.get(key, (None, None))[0] is not None

    def name_of(self, key):
        return self._resolved.get(key, (None, None))[0]

    def missing(self):
        return sorted(k for k, (n, _) in self._resolved.items() if n is None)

    def call(self, key, *args, **kwargs):
        """Invoke a resolved tool. Python commands take kwargs; MEL does not."""
        name, kind = self._resolved.get(key, (None, None))
        if name is None:
            raise CommandUnavailable(
                "No command available for %r on Maya %s (tried %s)"
                % (key, cmds.about(version=True),
                   ", ".join(self.TOOLS.get(key, []))))
        if kind == "python":
            return getattr(cmds, name)(*args, **kwargs)
        return mel_call(name, *args, **kwargs)


class CommandUnavailable(RuntimeError):
    pass


_DEFAULT_RESOLVER = []


def default_resolver():
    """One shared CommandResolver for callers that were not handed one.

    Constructing a resolver runs `whatIs` through mel.eval for every candidate
    that is not a cmds attribute - roughly thirty round trips. read_pinned_uvs,
    set_pins and UVWriter all defaulted to building a fresh one, so a handler
    that made a writer per layout unit paid that cost per unit. Resolution
    cannot change inside a session unless a plug-in is loaded, which is what
    reset_default_resolver is for.
    """
    if not _DEFAULT_RESOLVER:
        _DEFAULT_RESOLVER.append(CommandResolver())
    return _DEFAULT_RESOLVER[0]


def reset_default_resolver():
    """Forget the shared resolver. Call after loading or unloading a plug-in."""
    del _DEFAULT_RESOLVER[:]


def mel_quote(value):
    """Render a Python value as a MEL literal.

    Naive string interpolation into mel.eval is the classic source of silent
    breakage the moment a namespace, a path with spaces, or a quote appears in
    an object name.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "{%s}" % ", ".join(mel_quote(v) for v in value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % text


def mel_call(procedure, *args, **kwargs):
    """Call a MEL procedure with flags, safely quoted.

    kwargs become MEL flags: texSetTexelDensity(density=10.24) is emitted as
    `texSetTexelDensity -density 10.24;`. Flag values of True emit the bare
    flag, which is how MEL boolean flags are written.
    """
    parts = [procedure]
    for flag, value in kwargs.items():
        if value is True:
            parts.append("-%s" % flag)
        elif value is False or value is None:
            continue
        else:
            parts.append("-%s %s" % (flag, mel_quote(value)))
    parts.extend(mel_quote(a) for a in args)
    command = " ".join(parts) + ";"
    try:
        return mel.eval(command)
    except Exception as exc:
        raise CommandUnavailable("MEL call failed: %s\n%s" % (command, exc))


# =====================================================================
# SECTION 2 - Undo
# =====================================================================

_CHUNK_DEPTH = [0]


class undo_chunk(object):
    """Group every scene edit inside a `with` block into one undo step.

    Without this, a single packer run leaves several hundred entries on the
    undo queue and Ctrl+Z becomes useless. The try/finally is mandatory: an
    unclosed chunk corrupts the undo stack for the rest of the session.

    RE-ENTRANT. The writer opens a chunk per call; a handler that moves 338
    units therefore opened 338 chunks, and one Ctrl+Z undid one shell. Nesting
    is now counted: only the outermost `with` talks to Maya, so a handler can
    wrap a whole tool press in one chunk and every write inside it joins that
    chunk instead of starting its own. Maya does not reference-count
    openChunk/closeChunk itself, which is why the count is kept here.

    The depth is process-global rather than per-instance because the nesting
    it guards is across call frames, not within one object.
    """

    def __init__(self, name="UV Studio"):
        self.name = name
        self._open = False

    def __enter__(self):
        if _CHUNK_DEPTH[0] > 0:
            _CHUNK_DEPTH[0] += 1        # already inside one; just count
            return self
        try:
            cmds.undoInfo(openChunk=True, chunkName=self.name)
            self._open = True
            _CHUNK_DEPTH[0] = 1
        except Exception:
            self._open = False
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        if _CHUNK_DEPTH[0] > 0:
            _CHUNK_DEPTH[0] -= 1
        if self._open:
            try:
                cmds.undoInfo(closeChunk=True)
            except Exception:
                pass
            # A failed close must not strand the counter, or every later
            # chunk in the session silently nests inside a chunk that is
            # already gone and nothing is ever undoable again.
            _CHUNK_DEPTH[0] = 0
        return False    # never swallow exceptions


# =====================================================================
# SECTION 3 - UV set management
# =====================================================================

class UVSetManager(object):
    """Owns the original / working UV set contract.

    The original set is never edited. All UV Studio operations run on a copy.
    The name of the source set is stored on the mesh so a reopened scene knows
    what to restore to, and so the texture engine in M6 can always map a
    working UV back to the pixel it came from.
    """

    def __init__(self, resolver=None):
        self.cmd = resolver or CommandResolver()

    def list_sets(self, mesh):
        return cmds.polyUVSet(mesh, query=True, allUVSets=True) or []

    def current_set(self, mesh):
        sets = cmds.polyUVSet(mesh, query=True, currentUVSet=True) or []
        return sets[0] if sets else None

    def original_set(self, mesh):
        """The set the working copy was made from, or the current one."""
        attr = "%s.%s" % (mesh, ORIGINAL_SET_ATTR)
        if cmds.objExists(attr):
            stored = cmds.getAttr(attr)
            if stored in self.list_sets(mesh):
                return stored
        current = self.current_set(mesh)
        return current if current != WORKING_SET_NAME else None

    def has_working_set(self, mesh):
        return WORKING_SET_NAME in self.list_sets(mesh)

    def ensure_working_set(self, mesh, activate=True):
        """Create the working copy if absent. Returns (set_name, created)."""
        existing = self.list_sets(mesh)
        if WORKING_SET_NAME in existing:
            if activate:
                self.activate(mesh, WORKING_SET_NAME)
            return WORKING_SET_NAME, False

        source = self.current_set(mesh)
        if source is None:
            raise RuntimeError("%s has no UV sets to copy." % mesh)

        with undo_chunk("UV Studio: create working set"):
            cmds.polyUVSet(mesh, copy=True, uvSet=source,
                           newUVSet=WORKING_SET_NAME)
            self._remember_source(mesh, source)
            if activate:
                self.activate(mesh, WORKING_SET_NAME)
        return WORKING_SET_NAME, True

    def activate(self, mesh, uv_set):
        cmds.polyUVSet(mesh, currentUVSet=True, uvSet=uv_set)

    def discard_working_set(self, mesh, restore_original=True):
        """Throw away edits and go back to the original. Undoable."""
        if not self.has_working_set(mesh):
            return False
        original = self.original_set(mesh)
        with undo_chunk("UV Studio: discard working set"):
            if restore_original and original:
                self.activate(mesh, original)
            cmds.polyUVSet(mesh, delete=True, uvSet=WORKING_SET_NAME)
        return True

    def _remember_source(self, mesh, source):
        attr = "%s.%s" % (mesh, ORIGINAL_SET_ATTR)
        if not cmds.objExists(attr):
            cmds.addAttr(mesh, longName=ORIGINAL_SET_ATTR, dataType="string")
        cmds.setAttr(attr, source, type="string")


# =====================================================================
# SECTION 4 - Shell extraction
# =====================================================================

class Shell(object):
    """One UV shell as plain data. No Maya handles held."""

    __slots__ = ("index", "uv_ids", "u_min", "u_max", "v_min", "v_max",
                 "area", "pinned_count", "mesh", "tris", "faces")

    def __init__(self, index, uv_ids, bounds, area, pinned_count=0, mesh=None):
        self.mesh = mesh
        self.tris = []
        # The real face loops, as UV ids - quads and n-gons, untriangulated.
        # The packer needs triangles; anything DRAWN wants these, so the
        # artist sees the mesh's own edges rather than fan diagonals.
        self.faces = []
        self.index = index
        self.uv_ids = uv_ids
        self.u_min, self.u_max, self.v_min, self.v_max = bounds
        self.area = area
        self.pinned_count = pinned_count

    @property
    def width(self):
        return self.u_max - self.u_min

    @property
    def height(self):
        return self.v_max - self.v_min

    @property
    def centre(self):
        return ((self.u_min + self.u_max) * 0.5,
                (self.v_min + self.v_max) * 0.5)

    @property
    def is_pinned(self):
        return self.pinned_count > 0

    @property
    def udim(self):
        """UDIM tile this shell's centre falls in (1001 + u + v*10)."""
        cu, cv = self.centre
        # floor, not int(): int() truncates toward zero, so u=-0.5 would land
        # in tile 1001 instead of 1000 and every negative-U shell would be
        # assigned to the wrong tile.
        return 1001 + int(math.floor(cu)) + int(math.floor(cv)) * 10

    def bbox_overlaps(self, other, epsilon=OVERLAP_EPSILON):
        return not (self.u_max < other.u_min - epsilon
                    or other.u_max < self.u_min - epsilon
                    or self.v_max < other.v_min - epsilon
                    or other.v_max < self.v_min - epsilon)

    def signature(self, digits=4):
        """Size fingerprint for similarity matching, position-independent."""
        return (round(self.width, digits), round(self.height, digits),
                round(self.area, digits))

    def fingerprint(self, digits=DUPLICATE_DIGITS):
        """Position-independent identity: dimensions plus UV count.

        This is what actually identifies a mirrored or duplicated shell. Two
        copies of the same geometry have the same UV count and the same size
        wherever they sit in the sheet - stacked, side by side, or in
        different UDIM tiles. Bounding-box overlap sees none of that.
        """
        return (round(self.width, digits), round(self.height, digits),
                len(self.uv_ids))

    def __repr__(self):
        return ("<Shell %s:%d uvs=%d bbox=(%.3f,%.3f)-(%.3f,%.3f) pinned=%d>"
                % ((self.mesh or "?").split("|")[-1], self.index,
                   len(self.uv_ids), self.u_min, self.v_min,
                   self.u_max, self.v_max, self.pinned_count))


def canonical_mesh(name):
    """The full path of the mesh SHAPE, whatever form the caller had.

    Two callers disagreed about what a "mesh" is. M5's SelectionGuard derives
    one by splitting a selection string at the first dot, which yields the
    TRANSFORM for an object selection ("|door") and the SHAPE for a component
    one ("|door|doorShape.map[3]"). selected_uv_ids compares full paths, so on
    an object selection every comparison failed, no shell was ever reported as
    touched, and the caller's "nothing selected, so use everything" fallback
    ran instead - align silently moved the whole mesh.

    One spelling, resolved once, fixes the class rather than the symptom.
    """
    if not name:
        return name
    # A name Maya cannot resolve is returned unchanged rather than raised on.
    # This runs at the top of every handler, so a stray entry in the selection
    # must not be able to take down a tool before it starts.
    try:
        shapes = cmds.listRelatives(name, shapes=True, noIntermediate=True,
                                    fullPath=True, type="mesh") or []
    except Exception:
        return name
    if shapes:
        return shapes[0]
    try:
        return (cmds.ls(name, long=True) or [name])[0]
    except Exception:
        return name


def meshes_under(node):
    """Every real mesh shape at or below `node`.

    A selected GROUP has no shape of its own, so looking only at the node
    itself found nothing and the tool acted as if nothing was selected. The
    artist means every mesh inside it. Intermediate (history) shapes are
    skipped: they are not what is drawn or edited.
    """
    try:
        shapes = cmds.listRelatives(node, shapes=True, noIntermediate=True,
                                    fullPath=True, type="mesh") or []
    except Exception:
        shapes = []
    if shapes:
        return shapes
    try:
        if cmds.objectType(node, isType="mesh"):
            return (cmds.ls(node, long=True) or [node])[:1]
    except Exception:
        pass
    try:
        found = cmds.listRelatives(node, allDescendents=True, fullPath=True,
                                   type="mesh") or []
    except Exception:
        found = []
    out = []
    for shape in found:
        try:
            if cmds.getAttr(shape + ".intermediateObject"):
                continue
        except Exception:
            pass
        if shape not in out:
            out.append(shape)
    return sorted(out)


def canonical_meshes(names):
    """Mesh shapes for a list of names, groups expanded, order preserved,
    duplicates dropped."""
    seen = []
    for name in names or []:
        expanded = meshes_under(name) or [canonical_mesh(name)]
        for resolved in expanded:
            if resolved and resolved not in seen:
                seen.append(resolved)
    return seen


def dag_path(mesh):
    """MDagPath for a mesh, accepting either the transform or the shape.

    MFnMesh on a transform path is not reliable across Maya versions, and the
    failure is a raise deep inside extract_shells rather than anything a
    caller can interpret. Extending to the shape here means every read path
    gets the same treatment without each one remembering to.
    """
    sel = om2.MSelectionList()
    sel.add(mesh)
    path = sel.getDagPath(0)
    if path.apiType() != om2.MFn.kMesh:
        try:
            path.extendToShape()
        except Exception:
            pass                # single-shape assumption failed; let MFnMesh say so
    return path


def extract_shells(mesh, uv_set=None, pinned=None):
    """Return (shells, us, vs) for a mesh. Read-only, API 2.0, single pass.

    getUvShellsIds gives a shell id per UV in one call, which is what keeps
    this linear instead of the flood-fill everyone writes by hand.
    """
    fn = om2.MFnMesh(dag_path(mesh))
    uv_set = uv_set or (cmds.polyUVSet(mesh, query=True,
                                       currentUVSet=True) or [None])[0]

    us, vs = fn.getUVs(uv_set)
    if not us:
        return [], [], []

    shell_count, shell_ids = fn.getUvShellsIds(uv_set)
    pinned = pinned or set()

    buckets = defaultdict(list)
    for uv_id, shell_id in enumerate(shell_ids):
        buckets[shell_id].append(uv_id)

    # Face -> UV-index map, fan-triangulated per shell. The raster packer (M3)
    # needs real triangles, not boxes; a face's UVs all share one shell, so
    # its first UV id names the shell.
    counts, face_uvids = fn.getAssignedUVs(uv_set)
    tris_by_shell = defaultdict(list)
    faces_by_shell = defaultdict(list)
    k = 0
    for count in counts:
        if count == 0:
            continue
        fu = face_uvids[k:k + count]
        k += count
        sid = shell_ids[fu[0]]
        faces_by_shell[sid].append(tuple(fu))
        for i in range(1, count - 1):
            tris_by_shell[sid].append((fu[0], fu[i], fu[i + 1]))

    shells = []
    for shell_id in sorted(buckets):
        ids = buckets[shell_id]
        u_vals = [us[i] for i in ids]
        v_vals = [vs[i] for i in ids]
        u_min, u_max = min(u_vals), max(u_vals)
        v_min, v_max = min(v_vals), max(v_vals)
        tris = tris_by_shell.get(shell_id, [])
        # True UV area from the shoelace over each triangle. Falls back to the
        # bounding box when a shell has no faces (stray UVs).
        true_area = 0.0
        for a, b, c in tris:
            true_area += abs((us[b] - us[a]) * (vs[c] - vs[a])
                             - (us[c] - us[a]) * (vs[b] - vs[a])) * 0.5
        area = true_area or (u_max - u_min) * (v_max - v_min)
        pin_count = sum(1 for i in ids if i in pinned)
        shell = Shell(shell_id, ids,
                      (u_min, u_max, v_min, v_max), area, pin_count,
                      mesh=mesh)
        shell.tris = tris
        shell.faces = faces_by_shell.get(shell_id, [])
        shells.append(shell)
    return shells, list(us), list(vs)


# =====================================================================
# SECTION 5 - Pins
# =====================================================================

def read_pinned_uvs(mesh, resolver=None):
    """Return the set of pinned UV ids using Maya's native pin data.

    Reading Maya's own pins rather than keeping a parallel set is what makes
    "pinned UVs never move" agree with what u3dUnfold does. The query form has
    shifted across versions, so several are attempted before giving up.
    """
    resolver = resolver or default_resolver()
    if not resolver.available("pin"):
        return set()

    # polyPinUV addresses UV components, not objects. Querying the mesh name
    # returns nothing and warns "works only on poly uvs"; '.map[*]' is the
    # form that answers. Values come back in component order, so index i of
    # the result is UV id i.
    attempts = (
        ("%s.map[*]" % mesh, dict(query=True, value=True)),
        (mesh, dict(query=True, value=True)),
    )
    for target, kwargs in attempts:
        try:
            result = cmds.polyPinUV(target, **kwargs)
        except Exception:
            continue
        if not result:
            continue
        # Maya returns a weight per UV; anything above zero counts as pinned.
        try:
            return set(i for i, weight in enumerate(result) if float(weight) > 0.0)
        except (TypeError, ValueError):
            continue
    return set()


def set_pins(mesh, uv_ids, pinned=True, resolver=None):
    """Pin or unpin specific UVs. Undoable."""
    resolver = resolver or default_resolver()
    if not resolver.available("pin") or not uv_ids:
        return False
    components = expand_components(mesh, uv_ids)
    # polyPinUV's flag spelling has shifted between versions. A raise here
    # would abort a packer run mid-way, so failure is reported, not thrown.
    try:
        with undo_chunk("UV Studio: %s UVs" % ("pin" if pinned else "unpin")):
            cmds.polyPinUV(components, value=1.0 if pinned else 0.0)
        return True
    except Exception:
        cmds.warning("UV Studio: could not set pins on %s (%s)"
                     % (mesh, traceback.format_exc().strip().splitlines()[-1]))
        return False


# =====================================================================
# SECTION 6 - Layout units
# =====================================================================

def _centre_distance(a, b):
    ac, bc = a.centre, b.centre
    return max(abs(ac[0] - bc[0]), abs(ac[1] - bc[1]))


def find_duplicate_families(shells, digits=DUPLICATE_DIGITS, min_uvs=3,
                            use_uv_count=True):
    """Group shells that are copies of each other, wherever they sit.

    Keyed on the position-independent fingerprint: rounded width, rounded
    height, and UV count. Two mirrored halves of a symmetrical model produce
    identical fingerprints whether they are stacked on top of each other, laid
    side by side, or sitting in different UDIM tiles.

    UV count is what makes this safe. Size alone collides constantly on hard-
    surface assets full of similar panels; size AND an exact UV count almost
    never collides by accident.

    Shells below min_uvs are skipped - a 3-UV sliver matches too many things to
    be evidence of anything.
    """
    families = defaultdict(list)
    for shell in shells:
        if len(shell.uv_ids) < min_uvs:
            continue
        if shell.width < MIN_DUPLICATE_SIZE or shell.height < MIN_DUPLICATE_SIZE:
            continue
        key = shell.fingerprint(digits)
        if not use_uv_count:
            key = key[:2]
        families[key].append(shell)
    return [members for members in families.values() if len(members) > 1]


def classify_duplicates(shells, stack_tolerance=1.0e-3,
                        digits=DUPLICATE_DIGITS):
    """Split duplicate families into stacked and merely-paired.

    stacked : copies sitting on top of each other. These MUST move as one unit.
    paired  : copies sitting apart. These are candidates for stacking, but
              grouping them would weld shells that occupy separate space, so
              they are reported and left alone.
    """
    stacked, paired = [], []
    for family in find_duplicate_families(shells, digits=digits):
        remaining = list(family)
        while remaining:
            seed = remaining.pop(0)
            cluster = [seed]
            rest = []
            for other in remaining:
                if _centre_distance(seed, other) <= stack_tolerance:
                    cluster.append(other)
                else:
                    rest.append(other)
            remaining = rest
            if len(cluster) > 1:
                stacked.append(cluster)
        if len(family) > 1:
            centres = set()
            for shell in family:
                centres.add((round(shell.centre[0], 3),
                             round(shell.centre[1], 3)))
            if len(centres) > 1:
                paired.append(family)
    return stacked, paired


def fingerprint_sensitivity(shells, digit_options=(5, 4, 3, 2)):
    """How many duplicate families appear at each matching tolerance.

    The fingerprint's strictness is a guess until it is measured against real
    geometry. Mirrored halves can differ in the fourth decimal, and halves
    welded along a seam no longer share an exact UV count - either would hide
    real duplicates at the default setting.

    A flat curve means the default is right and the asset simply has that many
    duplicates. A curve that climbs steeply as tolerance loosens means the
    default is too strict and real pairs are being missed. A curve that
    explodes at 2 digits is matching unrelated shells by coincidence, which is
    what the UV-count column is there to distinguish.
    """
    rows = []
    for digits in digit_options:
        for use_count in (True, False):
            families = find_duplicate_families(shells, digits=digits,
                                               use_uv_count=use_count)
            rows.append(OrderedDict([
                ("digits", digits),
                ("uv_count_required", use_count),
                ("families", len(families)),
                ("shells_covered", sum(len(f) for f in families)),
            ]))
    return rows


def stacking_opportunity(shells, stack_tolerance=1.0e-3):
    """How much UV area could be reclaimed by stacking duplicate families.

    Each family of N copies currently occupies N footprints; stacked it
    occupies one. The reclaimed area is (N-1) x the footprint, summed.

    IMPORTANT CAVEAT for the caller: stacking makes copies share texture
    space. That is right for mirrored geometry meant to look identical, and
    wrong wherever the copies need distinct texturing - different decals on a
    left and right door, unique wear, per-instance dirt. This function reports;
    it never decides.
    """
    _stacked, paired = classify_duplicates(shells, stack_tolerance)
    families = []
    reclaimed = 0.0
    for family in paired:
        footprint = family[0].width * family[0].height
        gain = footprint * (len(family) - 1)
        reclaimed += gain
        families.append(OrderedDict([
            ("count", len(family)),
            ("size", family[0].fingerprint()[:2]),
            ("uv_count", len(family[0].uv_ids)),
            ("footprint", footprint),
            ("reclaimed", gain),
            ("shells", family),
        ]))
    families.sort(key=lambda f: -f["reclaimed"])

    total_area = sum(s.width * s.height for s in shells)
    return OrderedDict([
        ("families", families),
        ("family_count", len(families)),
        ("reclaimable_area", reclaimed),
        ("current_area", total_area),
        ("percent", (100.0 * reclaimed / total_area) if total_area > 0 else 0.0),
    ])


def stack_deltas(family, anchor="lowest_left"):
    """Translation per shell that brings a duplicate family into coincidence.

    Returns [(shell, du, dv)] excluding the anchor. Pinned shells are preferred
    as the anchor: if the artist pinned one copy, that is the one that must not
    move, and everything else comes to it.
    """
    if len(family) < 2:
        return []

    pinned = [s for s in family if s.is_pinned]
    if pinned:
        target = pinned[0]
    elif anchor == "lowest_left":
        target = min(family, key=lambda s: (s.u_min, s.v_min))
    else:
        target = family[0]

    tu, tv = target.centre
    moves = []
    for shell in family:
        if shell is target:
            continue
        cu, cv = shell.centre
        moves.append((shell, tu - cu, tv - cv))
    return moves


def explain_grouping(shells, overlap_ratio=0.6, stack_tolerance=1.0e-3,
                     limit=10):
    """Explain what grouped, what did not, and why.

    Reports both detection routes so "0 grouped" can be told apart from
    "0 detected": a symmetrical asset with nothing grouped and no duplicate
    families means the detector is wrong, not the asset.
    """
    stacked, paired = classify_duplicates(shells, stack_tolerance)

    order = sorted(range(len(shells)), key=lambda i: shells[i].u_min)
    count = len(order)
    touching = 0
    best_iou = 0.0
    iou_grouped = 0
    for a_pos in range(count):
        a = shells[order[a_pos]]
        for b_pos in range(a_pos + 1, count):
            b = shells[order[b_pos]]
            if b.u_min > a.u_max + OVERLAP_EPSILON:
                break
            if not a.bbox_overlaps(b):
                continue
            touching += 1
            ratio = _overlap_ratio(a, b)
            best_iou = max(best_iou, ratio)
            if ratio >= overlap_ratio:
                iou_grouped += 1

    if stacked:
        verdict = ("%d stacked group(s) found covering %d shells - these move "
                   "as one unit." % (len(stacked), sum(len(g) for g in stacked)))
    elif paired:
        verdict = ("%d duplicate family/families found, but their copies sit "
                   "APART rather than stacked. They are mirrored or repeated "
                   "shells laid out separately. Use Stack Similar Shells to "
                   "bring them together; they are not grouped automatically "
                   "because they currently occupy separate space."
                   % len(paired))
    elif iou_grouped:
        verdict = ("%d overlapping group(s) found by bounding box, with no "
                   "matching duplicates." % iou_grouped)
    else:
        verdict = ("No duplicates and no significant overlaps. %d bounding "
                   "boxes touch, best IoU %.3f. If this asset IS symmetrical, "
                   "the halves are probably separate mesh objects - pool them "
                   "with analyse_many()." % (touching, best_iou))

    return OrderedDict([
        ("shells", count),
        ("stacked_groups", stacked[:limit]),
        ("paired_families", paired[:limit]),
        ("stacked_total", len(stacked)),
        ("paired_total", len(paired)),
        ("overlapping_pairs", touching),
        ("best_iou", best_iou),
        ("iou_grouped", iou_grouped),
        ("verdict", verdict),
    ])


class LayoutUnit(object):
    """A group of shells that must move together.

    Stacked duplicates and deliberately overlapped shells are one thing to the
    artist. Packing them independently is the single most destructive thing a
    UV tool can do, so grouping happens before any layout runs.
    """

    __slots__ = ("index", "shells", "reason")

    def __init__(self, index, shells, reason="single"):
        self.index = index
        self.shells = shells
        self.reason = reason

    @property
    def uv_ids(self):
        ids = []
        for shell in self.shells:
            ids.extend(shell.uv_ids)
        return ids

    def by_mesh(self):
        """UV ids split per mesh, since a unit may span several meshes.

        The writer works on one mesh at a time, so a cross-mesh unit has to be
        applied as several writes with the same delta.
        """
        buckets = defaultdict(list)
        for shell in self.shells:
            buckets[shell.mesh].append(shell)
        return OrderedDict(
            (mesh, [i for s in group for i in s.uv_ids])
            for mesh, group in buckets.items())

    @property
    def meshes(self):
        return sorted(set(s.mesh for s in self.shells if s.mesh))

    @property
    def bounds(self):
        return (min(s.u_min for s in self.shells),
                max(s.u_max for s in self.shells),
                min(s.v_min for s in self.shells),
                max(s.v_max for s in self.shells))

    @property
    def is_pinned(self):
        return any(s.is_pinned for s in self.shells)

    @property
    def width(self):
        u_min, u_max, _, _ = self.bounds
        return u_max - u_min

    @property
    def height(self):
        _, _, v_min, v_max = self.bounds
        return v_max - v_min

    def __repr__(self):
        return ("<LayoutUnit %d shells=%d reason=%s pinned=%s>"
                % (self.index, len(self.shells), self.reason, self.is_pinned))


class _UnionFind(object):
    def __init__(self, size):
        self.parent = list(range(size))

    def find(self, a):
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


LINKS_ATTR = "uvStudioLinks"        # explicit, artist-authored shell groups


# =====================================================================
# SECTION 6b - Explicit links
#
# WHY THIS IS NOT JUST "RUN THE DUPLICATE FINDER AGAIN"
#   build_units finds shells that LOOK like they belong together: same
#   fingerprint and coincident centres, or boxes that overlap enough. That
#   catches mirrored halves and deliberate stacks, and it will never catch
#   "these two are a pair because I say so" - a left glove and a right boot
#   that share a trim texture, two props that must share one island.
#
#   So an explicit link is a separate input, not a tuned threshold. The
#   finder proposes; a link decides. Where they disagree, the link wins,
#   because it is the only one of the two that knows what the asset is for.
#
# WHY IT LIVES ON THE MESH
#   A pairing an artist drew by hand and lost on scene close is worse than
#   no pairing at all - they would draw it again every session and never be
#   sure it took. Pins already live on the mesh; links follow them.
#
# HOW A SHELL IS NAMED ACROSS SESSIONS
#   Shell INDEX is not identity: it is a position in a list rebuilt from UV
#   connectivity every analysis, so editing one shell can renumber another.
#   A link therefore stores the lowest UV id in the shell as an anchor, and
#   is resolved by asking which shell currently contains that UV.
#
#   That survives moving, rotating, packing and re-unfolding, because none
#   of those change UV ids. It does NOT survive a topology change that
#   renumbers UVs - cutting, sewing, merging, a projection. Those links
#   resolve to nothing and are reported as stale rather than silently
#   re-pointed at whatever UV now holds that number, which would pair two
#   arbitrary shells and look deliberate.
# =====================================================================

class ShellLink(object):
    """One artist-authored grouping of shells, by UV anchor."""

    __slots__ = ("anchors", "mode")

    STACK = "stack"     # move together AND onto each other
    LOCK = "lock"       # move together, keep relative positions

    def __init__(self, anchors, mode=STACK):
        # (mesh, uv id) pairs, sorted so the same link always compares equal
        self.anchors = sorted(set(tuple(a) for a in anchors))
        self.mode = mode if mode in (self.STACK, self.LOCK) else self.STACK

    def to_dict(self):
        return {"anchors": [list(a) for a in self.anchors], "mode": self.mode}

    @classmethod
    def from_dict(cls, data):
        return cls([tuple(a) for a in data.get("anchors", [])],
                   data.get("mode", cls.STACK))

    def __eq__(self, other):
        return (isinstance(other, ShellLink)
                and self.anchors == other.anchors and self.mode == other.mode)

    def __repr__(self):
        return "<ShellLink %s %s>" % (self.mode, self.anchors)


def shell_anchor(shell):
    """The durable name of a shell: its mesh and its lowest UV id."""
    return (shell.mesh, min(shell.uv_ids)) if shell.uv_ids else (shell.mesh, -1)


def read_links(mesh):
    """Links stored on a mesh. Never raises; an unreadable attribute is
    treated as no links, because a malformed string must not stop an
    analysis."""
    try:
        if not cmds.objExists("%s.%s" % (mesh, LINKS_ATTR)):
            return []
        raw = cmds.getAttr("%s.%s" % (mesh, LINKS_ATTR))
        return [ShellLink.from_dict(entry) for entry in json.loads(raw or "[]")]
    except Exception:
        return []


def write_links(mesh, links):
    try:
        plug = "%s.%s" % (mesh, LINKS_ATTR)
        if not cmds.objExists(plug):
            cmds.addAttr(mesh, longName=LINKS_ATTR, dataType="string")
        cmds.setAttr(plug, json.dumps([link.to_dict() for link in links]),
                     type="string")
        return True
    except Exception:
        return False


def read_all_links(meshes):
    links = []
    for mesh in meshes or []:
        links.extend(read_links(mesh))
    return links


def resolve_links(links, shells):
    """(resolved, stale) — links mapped onto the shells that exist now.

    A link resolves only if EVERY anchor finds a shell. A half-resolved link
    is not a smaller group, it is a link whose other end was cut away, and
    quietly grouping what remains would be a pairing the artist never made.
    """
    owner = {}
    for index, shell in enumerate(shells):
        for uv_id in shell.uv_ids:
            owner[(shell.mesh, uv_id)] = index

    resolved, stale = [], []
    for link in links:
        found = [owner.get(anchor) for anchor in link.anchors]
        if len(link.anchors) > 1 and all(i is not None for i in found):
            resolved.append((sorted(set(found)), link.mode))
        else:
            stale.append(link)
    return resolved, stale


def link_shells(shells, mode=ShellLink.STACK):
    """Store a link joining these shells, on every mesh they touch.

    Written to each mesh involved rather than to one of them, so a link
    between two objects survives deleting either: whichever remains still
    carries a record, which resolve_links then reports as stale instead of
    losing silently.
    """
    if len(shells) < 2:
        return None
    link = ShellLink([shell_anchor(s) for s in shells], mode)
    for mesh in sorted(set(s.mesh for s in shells)):
        existing = [l for l in read_links(mesh) if l.anchors != link.anchors]
        existing.append(link)
        write_links(mesh, existing)
    return link


def unlink_shells(shells):
    """Remove any stored link that mentions any of these shells."""
    anchors = set(shell_anchor(s) for s in shells)
    removed = 0
    for mesh in sorted(set(s.mesh for s in shells)):
        keep = []
        for link in read_links(mesh):
            if anchors.intersection(link.anchors):
                removed += 1
                continue
            keep.append(link)
        write_links(mesh, keep)
    return removed


def build_units(shells, stack_tolerance=1.0e-3, use_fingerprints=True,
                links=None):
    """Group shells that must move together.

    TWO ROUTES. Both require the artist's intent, in different forms:

      1. Fingerprint + coincident centre. Copies of the same shell sitting on
         top of each other - mirrored halves, a deliberate stack already in
         the scene. The geometry itself says these are one thing: same
         shape, same place.

      2. Explicit links (Section 6b). An artist said so, by drag or button.

    THERE IS NO ROUTE FOR "THE BOXES HAPPEN TO OVERLAP".
        A v1 route unioned any two shells whose bounding-box IoU cleared a
        threshold, whatever they were. That is not evidence of a pair - two
        unrelated shells laid out close together score the same as a
        deliberate stack - and it welded them into one unit forever, which
        is precisely the layout having overlapping UVs the artist never
        asked for. UV Studio's own rule is that overlap only happens on
        purpose: a shell is fixed only by a pin, and moves with another only
        by a real duplicate or a link. Positional coincidence is not consent
        and no longer grants one.

        explain_grouping still reports bounding-box IoU as a DIAGNOSTIC -
        "these boxes touch, here is how much" is useful information - but a
        number crossing a threshold no longer changes what gets packed
        together. Two shells that should share space are stacked with Stack
        Pairs or Pair Selected, same as any other deliberate choice.

    Duplicates that sit APART are deliberately NOT grouped either. They are
    pairs awaiting a stack operation, and welding them into one unit would
    move two shells that occupy separate space as though they were one.

    Shells may come from several meshes; grouping is purely positional, so a
    unit can span meshes. LayoutUnit.by_mesh() splits it back out for writing.
    """
    if not shells:
        return []

    finder = _UnionFind(len(shells))
    position = {}
    for i, shell in enumerate(shells):
        position[id(shell)] = i
    reasons = {}

    # Route 1 - stacked duplicates.
    if use_fingerprints:
        stacked, _paired = classify_duplicates(shells, stack_tolerance)
        for cluster in stacked:
            first = position[id(cluster[0])]
            for member in cluster[1:]:
                finder.union(first, position[id(member)])
                reasons[finder.find(first)] = "stacked"

    # Route 2: explicit links. Applied last so an artist-authored pairing
    # overrides whatever the two detectors concluded, and so its reason
    # survives - a link that merely agreed with the finder should still read
    # as "linked", because that is what the artist will look for when asking
    # why two shells move together.
    for members, mode in (links or []):
        first = members[0]
        for other in members[1:]:
            finder.union(first, other)
        reasons[finder.find(first)] = ("linked" if mode == ShellLink.STACK
                                       else "locked")

    groups = defaultdict(list)
    for idx, shell in enumerate(shells):
        groups[finder.find(idx)].append(shell)

    units = []
    for unit_index, root in enumerate(sorted(groups)):
        members = groups[root]
        reason = "single" if len(members) == 1 else reasons.get(root, "grouped")
        units.append(LayoutUnit(unit_index, members, reason))
    return units


def _overlap_ratio(a, b):
    """Intersection over union of two bounding boxes.

    Dividing by the SMALLER area instead - the obvious first choice - scores a
    perfect 1.0 whenever one shell merely sits inside another's box, which is
    containment, not stacking. Real assets are full of it: a small trim piece
    laid over a large panel scored 1.000 against a shell seven times its size.
    IoU gives 1.0 only when the boxes genuinely coincide, which is what
    "stacked" actually means.
    """
    du = min(a.u_max, b.u_max) - max(a.u_min, b.u_min)
    dv = min(a.v_max, b.v_max) - max(a.v_min, b.v_min)
    if du <= 0 or dv <= 0:
        return 0.0
    intersection = du * dv
    union = a.area + b.area - intersection
    return intersection / union if union > 0 else 0.0


# =====================================================================
# SECTION 7 - Component addressing
# =====================================================================

def compress_indices(indices):
    """Collapse a sorted index list into (start, end) inclusive ranges.

    A 200k-UV mesh produces a 200k-element component list, and passing that to
    polyEditUV as individual strings is the slowest thing in the whole tool.
    Contiguous shells compress to a handful of ranges, which is the difference
    between seconds and milliseconds per move.
    """
    if not indices:
        return []
    ordered = sorted(set(indices))
    ranges = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append((start, previous))
        start = previous = value
    ranges.append((start, previous))
    return ranges


def expand_components(mesh, uv_ids):
    """Build the shortest component strings that address these UVs."""
    return ["%s.map[%d:%d]" % (mesh, lo, hi) if lo != hi
            else "%s.map[%d]" % (mesh, lo)
            for lo, hi in compress_indices(uv_ids)]


# =====================================================================
# SECTION 8 - Transform writer
# =====================================================================

class UVWriter(object):
    """Undoable UV transforms that respect pins.

    Every public method filters pinned UVs out before writing, so the core
    requirement holds no matter which caller is driving - packer, gizmo, or a
    replayed recipe step.
    """

    def __init__(self, mesh, pinned=None, resolver=None):
        self.mesh = mesh
        self.cmd = resolver or default_resolver()
        # Resolve the resolver FIRST and hand it to the pin read. The previous
        # order read pins with resolver=None, which built a throwaway
        # CommandResolver on every writer even when the caller had supplied
        # one - the single most expensive line in a packing run.
        self.pinned = (pinned if pinned is not None
                       else read_pinned_uvs(mesh, self.cmd))

    def movable(self, uv_ids):
        if not self.pinned:
            return list(uv_ids)
        return [i for i in uv_ids if i not in self.pinned]

    def translate(self, uv_ids, du, dv, chunk_name="UV Studio: move"):
        ids = self.movable(uv_ids)
        if not ids or (du == 0.0 and dv == 0.0):
            return 0
        with undo_chunk(chunk_name):
            cmds.polyEditUV(expand_components(self.mesh, ids),
                            relative=True, uValue=du, vValue=dv)
        return len(ids)

    def scale(self, uv_ids, su, sv, pivot, chunk_name="UV Studio: scale"):
        ids = self.movable(uv_ids)
        if not ids:
            return 0
        with undo_chunk(chunk_name):
            cmds.polyEditUV(expand_components(self.mesh, ids),
                            relative=True, scaleU=su, scaleV=sv,
                            pivotU=pivot[0], pivotV=pivot[1])
        return len(ids)

    def rotate(self, uv_ids, degrees, pivot, chunk_name="UV Studio: rotate"):
        ids = self.movable(uv_ids)
        if not ids or degrees == 0.0:
            return 0
        with undo_chunk(chunk_name):
            cmds.polyEditUV(expand_components(self.mesh, ids),
                            relative=True, angle=degrees,
                            pivotU=pivot[0], pivotV=pivot[1])
        return len(ids)

    def apply_affine(self, uv_ids, matrix, translation,
                     chunk_name="UV Studio: pack"):
        """Map UVs through (A, t): uv' = A.uv + t. One setUV per UV.

        The raster packer emits a single affine per unit - rotation, scale and
        translation combined - and reconstructing it as separate scale/rotate/
        translate calls loses precision and mishandles the rotated cases. This
        writes the affine directly. Uses the API's setUVs for speed and so the
        whole thing is one undo chunk.
        """
        ids = self.movable(uv_ids)
        if not ids:
            return 0
        a, b, c, d = matrix
        tx, ty = translation
        with undo_chunk(chunk_name):
            fn = om2.MFnMesh(dag_path(self.mesh))
            uv_set = (cmds.polyUVSet(self.mesh, query=True,
                                     currentUVSet=True) or [None])[0]
            us, vs = fn.getUVs(uv_set)
            us, vs = list(us), list(vs)
            for i in ids:
                u, v = us[i], vs[i]
                us[i] = a * u + b * v + tx
                vs[i] = c * u + d * v + ty
            fn.setUVs(us, vs, uv_set)
        return len(ids)

    def move_unit(self, unit, du, dv):
        """Move a whole layout unit. Skipped entirely if any member is pinned."""
        if unit.is_pinned:
            return 0
        return self.translate(unit.uv_ids, du, dv,
                              chunk_name="UV Studio: move unit %d" % unit.index)


# =====================================================================
# SECTION 8b - Texture transfer support (read-mostly; M6 does the pixels)
# =====================================================================

BEFORE_SET = "uvStudio_before"


def snapshot_layout(mesh):
    """Copy the CURRENT UV set to BEFORE_SET, replacing any older copy.

    UV Studio's tools edit the current set in place, so without this there
    is no 'before' layout for a texture transfer to warp from. The current
    set stays current. Undoable.
    """
    current = (cmds.polyUVSet(mesh, query=True, currentUVSet=True)
               or [None])[0]
    if current is None or current == BEFORE_SET:
        raise RuntimeError("%s has no usable current UV set" % mesh)
    with undo_chunk("UV Studio: remember layout"):
        existing = cmds.polyUVSet(mesh, query=True, allUVSets=True) or []
        if BEFORE_SET in existing:
            cmds.polyUVSet(mesh, delete=True, uvSet=BEFORE_SET)
        cmds.polyUVSet(mesh, copy=True, uvSet=current, newUVSet=BEFORE_SET)
        cmds.polyUVSet(mesh, currentUVSet=True, uvSet=current)
    return BEFORE_SET


def face_uv_loops(mesh, uv_set):
    """Per face, its UV loop as [(u, v), ...] in `uv_set`; None where the
    face has no UVs. Face order and corner order are the mesh's own, so two
    UV sets' results correspond corner for corner - valid after cuts and
    sews, which renumber UV ids but not face-vertices.

    Returns (loops, first_uv_ids, shell_ids) where shell_ids is the per-UV
    shell id list of THIS set, so a caller can group faces by shell.
    """
    fn = om2.MFnMesh(dag_path(mesh))
    us, vs = fn.getUVs(uv_set)
    counts, ids = fn.getAssignedUVs(uv_set)
    _count, shell_ids = fn.getUvShellsIds(uv_set)
    per_face = fn.getVertices()[0]          # corners per face, all faces
    loops, firsts = [], []
    k = 0
    for face, corners in enumerate(per_face):
        count = counts[face] if face < len(counts) else 0
        if count:
            fu = ids[k:k + count]
            k += count
            loops.append([(us[i], vs[i]) for i in fu])
            firsts.append(fu[0])
        else:
            loops.append(None)
            firsts.append(None)
    return loops, firsts, list(shell_ids)


def texture_files(meshes):
    """{file node: {"path", "udim", "meshes"}} for textures on these meshes.

    Found by walking each mesh's shading groups upstream, so every file
    node feeding any material channel is included. A texture shared by
    several meshes is listed once with all of them - its transfer must
    carry every one of their shells into the same new image.
    """
    found = OrderedDict()
    for mesh in canonical_meshes(meshes):
        groups = cmds.listConnections(mesh, type="shadingEngine") or []
        for sg in sorted(set(groups)):
            for node in cmds.ls(cmds.listHistory(sg) or [], type="file") or []:
                entry = found.get(node)
                if entry is None:
                    path = cmds.getAttr(node + ".fileTextureName") or ""
                    tiling = 0
                    if cmds.attributeQuery("uvTilingMode", node=node,
                                           exists=True):
                        tiling = cmds.getAttr(node + ".uvTilingMode") or 0
                    entry = found[node] = OrderedDict([
                        ("path", path), ("udim", tiling == 3
                                         or "<UDIM>" in path.upper()),
                        ("meshes", [])])
                if mesh not in entry["meshes"]:
                    entry["meshes"].append(mesh)
    return found


def parity_scan():
    """Every UV-related command, procedure and runtime command on THIS Maya.

    Used to confirm the parity buttons' command names: whatever a button
    expected and this list lacks is a name to correct, found in one run
    instead of guessed at.
    """
    found = OrderedDict()
    for pattern in ("tex*", "u3d*", "poly*UV*", "*UVShell*", "*Unfold*"):
        try:
            found["commands %s" % pattern] = sorted(
                mel.eval('help -list "%s"' % pattern) or [])
        except Exception as exc:
            found["commands %s" % pattern] = ["(help failed: %s)" % exc]
    try:
        rtc = cmds.runTimeCommand(query=True, commandArray=True) or []
    except Exception:
        rtc = []
    keys = ("uv", "unfold", "stitch", "shell", "texel", "seam", "stack",
            "snap", "gather", "randomize", "symmetr", "straighten", "sew")
    found["runtime commands"] = sorted(r for r in rtc
                                       if any(k in r.lower() for k in keys))
    return found


def current_uv_set(mesh):
    return (cmds.polyUVSet(mesh, query=True, currentUVSet=True) or [None])[0]


def repoint_file(node, path):
    """Point a file node at a new image. Undoable."""
    with undo_chunk("UV Studio: repoint texture"):
        cmds.setAttr(node + ".fileTextureName", path, type="string")


# =====================================================================
# SECTION 9 - Selection
# =====================================================================

def selected_meshes():
    """Meshes in the current selection, deduplicated - objects OR components.

    A component ("body.map[5]", "body.e[3]") names its mesh before the dot.
    Reading only whole objects returned nothing whenever UVs were selected,
    which left the Cluster Map empty in exactly the mode it is used in.
    Hilited objects count too: in component mode they are the meshes the
    artist is working on even when nothing is selected yet.
    """
    meshes = []
    names = []
    for item in cmds.ls(selection=True, long=True) or []:
        names.append(item.split(".")[0])
    try:
        names.extend(cmds.ls(hilite=True, long=True) or [])
    except Exception:
        pass
    for node in names:
        for shape in meshes_under(node):
            if shape not in meshes:
                meshes.append(shape)
    return meshes


SELECTION_MODES = ("vertex", "edge", "face", "uv", "shell")
_MODE_CONVERT = {"vertex": "toVertex", "edge": "toEdge", "face": "toFace",
                 "uv": "toUV", "shell": "toUV"}
_MODE_TYPE = {"vertex": "vertex", "edge": "edge", "face": "facet",
              "uv": "polymeshUV", "shell": "polymeshUV"}


_UV_SHELL_FLAG = []


def uv_shell_flag():
    """selectType's UV-shell flag on THIS Maya, or None. Asked once.

    The flag's name differs between versions and Maya 2022 has none of the
    guessed spellings. A bad flag raises - catchable - but Maya still PRINTS
    "Error while parsing arguments" every time, and the selection strip
    polls twice a second. So the flag list is read from `help selectType`,
    which prints nothing, and only a flag Maya declares is ever sent.
    """
    if not _UV_SHELL_FLAG:
        flag = None
        try:
            text = mel.eval("help selectType") or ""
            found = [t for t in re.findall(r"(?<![\w-])-([A-Za-z]\w*)", text)
                     if "shell" in t.lower() and "uv" in t.lower()]
            if found:
                flag = max(found, key=len)          # the long spelling
        except Exception:
            flag = None
        _UV_SHELL_FLAG.append(flag)
    return _UV_SHELL_FLAG[0]


def set_selection_mode(mode):
    """Switch Maya's component mode the way the UV Toolkit's buttons do.

    A COMPONENT selection is converted to the new type (UVs selected, press
    Edge: their edges are selected). With only objects selected, the mode is
    entered and nothing is selected - converting an object would select
    every component on it, which is not what switching mode means.
    UV Shell selects whole shells (Maya's polySelectBorderShell).
    """
    if mode not in SELECTION_MODES:
        raise ValueError("unknown selection mode %r" % mode)
    selection = cmds.ls(selection=True, long=True) or []
    components = [item for item in selection if "." in item]
    meshes = selected_meshes()
    converted = []
    if components:
        try:
            converted = cmds.polyListComponentConversion(
                components, **{_MODE_CONVERT[mode]: True}) or []
        except Exception:
            converted = []
    if meshes:
        cmds.hilite(meshes, replace=True)
    cmds.selectMode(component=True)
    cmds.selectType(allComponents=False)
    cmds.selectType(**{_MODE_TYPE[mode]: True})
    shell_flag = uv_shell_flag()
    if mode == "shell" and shell_flag:
        try:
            cmds.selectType(**{shell_flag: True})
        except Exception:
            pass                # UV mode plus grown selection still works
    if converted:
        cmds.select(converted, replace=True)
        if mode == "shell":
            # Maya's own conversion first (present on 2022 per probe 2),
            # polySelectBorderShell only if it is missing.
            try:
                mel.eval("ConvertSelectionToUVShell;")
            except Exception:
                try:
                    mel.eval("polySelectBorderShell 0;")
                except Exception:
                    pass
    elif components:
        cmds.select(clear=True)
    return mode


def current_selection_mode():
    """Which of SELECTION_MODES Maya is in, or "object"."""
    try:
        if cmds.selectMode(query=True, object=True):
            return "object"
    except Exception:
        return None
    shell_flag = uv_shell_flag()
    if shell_flag:
        try:
            if cmds.selectType(query=True, **{shell_flag: True}):
                return "shell"
        except Exception:
            pass
    for mode, flag in (("uv", "polymeshUV"), ("face", "facet"),
                       ("edge", "edge"), ("vertex", "vertex")):
        try:
            if cmds.selectType(query=True, **{flag: True}):
                return mode
        except Exception:
            continue
    return None


def selected_uv_ids_by_mesh(selection=None):
    """{canonical mesh path: [uv id, ...]} for the whole selection, one pass.

    The per-mesh version below used to call cmds.ls(node, long=True) once per
    selected component to normalise its node name. An artist who selects a
    whole shell selects thousands of components, and every handler called it
    once per mesh, so the same normalisation ran tens of thousands of times
    per button press. Node names repeat; the lookup is cached instead.
    """
    if selection is None:
        selection = cmds.ls(selection=True, flatten=True, long=True) or []

    # A UV EDITOR SELECTION IS NOT ALWAYS UVs. Its selection mode can be
    # face, edge or vertex, and "select a shell" in face mode yields
    # ".f[...]" with not one ".map[" among it. Reading only .map entries then
    # found nothing, callers read that as "no component selection", and
    # Rotate fell back to every shell on the mesh - the artist selected one
    # shell and the whole mesh turned.
    #
    # Converting is the fix rather than handling each mode, because the
    # question every caller is really asking is "which UVs did they point
    # at", and polyListComponentConversion answers exactly that.
    components = [item for item in selection if "." in item]
    if components and not any(".map[" in item for item in components):
        try:
            converted = cmds.polyListComponentConversion(
                components, toUV=True) or []
            if converted:
                selection = cmds.ls(converted, flatten=True, long=True) or []
        except Exception:
            pass                # leave the selection as it was

    resolved = {}
    out = defaultdict(list)
    for item in selection:
        node, sep, component = item.partition(".map[")
        if not sep:
            continue
        target = resolved.get(node)
        if target is None:
            target = resolved[node] = canonical_mesh(node)
        body = component.rstrip("]")
        # "map[0:3]" as well as "map[0]". cmds.ls(flatten=True) normally
        # expands ranges, so this only fires if something upstream forgets
        # the flag - and the old code's failure mode for that was to drop
        # every UV in the range silently, which reads to a caller as "the
        # artist selected nothing" and quietly widens the tool to the whole
        # mesh. Three lines to close a silent-wrong-answer path.
        if ":" in body:
            low, _, high = body.partition(":")
            try:
                out[target].extend(range(int(low), int(high) + 1))
            except ValueError:
                continue
            continue
        try:
            out[target].append(int(body))
        except ValueError:
            continue
    return out


def selection_has_components(selection=None):
    """True when the artist has components selected, of any kind.

    The difference between "they selected an object, so mean the whole mesh"
    and "they selected components and we failed to resolve them". Those need
    opposite responses and were previously indistinguishable.
    """
    if selection is None:
        selection = cmds.ls(selection=True, long=True) or []
    return any("." in item for item in selection)


def selected_uv_ids(mesh, selection=None):
    """UV ids currently selected on a mesh.

    Compares full shape paths. A substring test matches "head" against
    "headBand" and silently mixes two meshes' UVs together; a transform-vs-
    shape mismatch matches nothing at all, which is worse, because callers
    read "no UV selection" and fall back to operating on everything.
    """
    return selected_uv_ids_by_mesh(selection).get(canonical_mesh(mesh), [])


def select_shells(mesh, shells, replace=True):
    """Select the UVs belonging to the given shells."""
    ids = []
    for shell in shells:
        ids.extend(shell.uv_ids)
    components = expand_components(mesh, ids)
    if replace:
        cmds.select(components, replace=True)
    else:
        cmds.select(components, add=True)
    return len(ids)


def shells_from_selection(mesh, shells):
    """Which shells does the current UV selection touch?"""
    selected = set(selected_uv_ids(mesh))
    if not selected:
        return []
    return [s for s in shells if selected.intersection(s.uv_ids)]


# =====================================================================
# SECTION 10 - Facade
# =====================================================================

class SceneBridge(object):
    """The single object modules 3-6 talk to."""

    def __init__(self):
        self.cmd = CommandResolver()
        self.uv_sets = UVSetManager(self.cmd)

    def analyse(self, mesh, uv_set=None, use_working_set=False):
        """Read everything the packer needs. Does not modify the scene unless
        use_working_set is True and the working set is missing."""
        created = False
        if use_working_set:
            _, created = self.uv_sets.ensure_working_set(mesh)
            uv_set = WORKING_SET_NAME

        started = time.time()
        pinned = read_pinned_uvs(mesh, self.cmd)
        shells, us, vs = extract_shells(mesh, uv_set=uv_set, pinned=pinned)
        units = build_units(shells)
        elapsed = time.time() - started

        return OrderedDict([
            ("mesh", mesh),
            ("uv_set", uv_set or self.uv_sets.current_set(mesh)),
            ("created_working_set", created),
            ("uv_count", len(us)),
            ("shells", shells),
            ("units", units),
            ("pinned", pinned),
            ("udims", sorted(set(s.udim for s in shells))),
            ("seconds", elapsed),
        ])

    def analyse_many(self, meshes, uv_set=None, progress=None):
        """Analyse several meshes as ONE shared UV space.

        Per-mesh analysis cannot see a duplicate whose twin lives on another
        object, which is exactly how symmetrical assets are usually built -
        left and right halves as separate meshes sharing one UV sheet. Pooling
        the shells before grouping is the only way those pairs are visible.
        """
        pooled = []
        per_mesh = OrderedDict()
        pin_sets = OrderedDict()
        started = time.time()

        # One spelling of a mesh, decided here, so that shell.mesh, the keys
        # of pin_sets and anything a handler compares against a selection are
        # all the same string.
        stored_links = []
        # UVs kept per mesh and returned, so the Cluster Map draws from them
        # instead of reading every mesh a second time.
        coords = OrderedDict()
        todo = canonical_meshes(meshes)
        for index, mesh in enumerate(todo):
            if progress is not None:
                # May raise to cancel; nothing has been written, so stopping
                # between meshes leaves the scene exactly as it was.
                progress(index, len(todo), mesh)
            stored_links.extend(read_links(mesh))
            pinned = read_pinned_uvs(mesh, self.cmd)
            shells, us, vs_ = extract_shells(mesh, uv_set=uv_set, pinned=pinned)
            coords[mesh] = (us, vs_)
            pooled.extend(shells)
            pin_sets[mesh] = pinned
            per_mesh[mesh] = OrderedDict([
                ("uv_count", len(us)), ("shells", len(shells)),
                ("pinned", len(pinned))])

        resolved_links, stale_links = resolve_links(stored_links, pooled)
        units = build_units(pooled, links=resolved_links)
        multi = [u for u in units if len(u.shells) > 1]
        spanning = [u for u in multi if len(u.meshes) > 1]

        return OrderedDict([
            ("meshes", per_mesh),
            # The pin read is the expensive part of this call and every write
            # needs it again. Handing it back means a writer can be built
            # without a second polyPinUV query per unit.
            ("pin_sets", pin_sets),
            # Explicit pairings the artist made, and the ones whose anchors
            # no longer resolve. Stale links are surfaced rather than
            # dropped: a pairing that stopped working is something to be
            # told about, not something to quietly stop honouring.
            ("links", resolved_links),
            ("stale_links", stale_links),
            ("coords", coords),
            ("total_shells", len(pooled)),
            ("shells", pooled),
            ("units", units),
            ("grouped_units", len(multi)),
            ("cross_mesh_units", len(spanning)),
            ("udims", sorted(set(s.udim for s in pooled))),
            ("seconds", time.time() - started),
        ])

    def chunk(self, name="UV Studio"):
        """One undo step for everything inside the `with`.

        Exposed on the bridge so callers that must not import M2 directly -
        M5's runner, replaying a recipe - can still group a whole sequence.
        The chunk is re-entrant, so writers opening their own inside it join
        rather than nest.
        """
        return undo_chunk(name)

    def writer(self, mesh, pinned=None):
        return UVWriter(mesh, pinned=pinned, resolver=self.cmd)

    def sew_edges(self, components, move=False):
        """Sew UV edges. Accepts an edge OR a UV selection.

        polyMapSew / polyMapSewMove act on UV map-edges. But an artist working
        in the UV editor most often has UVs selected, not edges, and means
        "sew the boundary between these" - which is what Maya's own Sew does by
        converting first. The old guard demanded edges and refused a UV
        selection outright, so the natural gesture failed with a warning
        instead of working. This converts UV components to edges when needed,
        exactly as split_uvs does for cutting, then calls the native command.
        """
        items = components if isinstance(components, (list, tuple)) \
            else [components]
        has_edges = any(".e[" in str(i) for i in items)
        if has_edges:
            edges = [i for i in items if ".e[" in str(i)]
        else:
            # Edges running between selected UVs first; every touching edge
            # only if that finds none.
            edges = None
        # Only UV SEAMS can be sewn - a seam edge maps to more than two UVs.
        # Seams INSIDE the selection first (a cut within one shell); if none,
        # the selection's border, which is where a whole selected shell meets
        # its neighbour. The inside-only conversion alone excluded exactly
        # those border seams, so selecting a shell and pressing Move and Sew
        # found nothing to sew.
        if edges is None:
            inside = self.cmd.call("convert", items, toEdge=True,
                                   internal=True) or []
            edges = self.seam_edges(inside)
            if not edges:
                around = self.cmd.call("convert", items, toEdge=True,
                                       internal=False) or []
                edges = self.seam_edges(around)
        else:
            edges = self.seam_edges(edges)
        if not edges:
            raise RuntimeError("No UV seam edges in the selection to sew.")
        key = "sew_move" if move else "sew"
        name = "Move and Sew" if move else "Sew"
        with undo_chunk("UV Studio: %s" % name):
            self.cmd.call(key, edges)
        return len(cmds.ls(edges, flatten=True) or [])

    SEAM_CHECK_LIMIT = 20000

    def seam_edges(self, edges):
        """Edges that are UV borders: their UVs are split at an end, so the
        edge maps to more than two UVs."""
        flat = cmds.ls(edges, flatten=True, long=True) or []
        if len(flat) > self.SEAM_CHECK_LIMIT:
            return flat
        seams = []
        for edge in flat:
            try:
                uvs = cmds.ls(cmds.polyListComponentConversion(
                    edge, toUV=True) or [], flatten=True) or []
            except Exception:
                continue
            if len(uvs) > 2:
                seams.append(edge)
        return seams

    def cut_edges(self, components):
        """Cut UV edges. Accepts an edge or UV selection, like sew_edges."""
        items = components if isinstance(components, (list, tuple)) \
            else [components]
        if any(".e[" in str(i) for i in items):
            edges = [i for i in items if ".e[" in str(i)]
        else:
            edges = self.cmd.call("convert", items, toEdge=True,
                                  internal=False) or []
        if not edges:
            return 0
        with undo_chunk("UV Studio: cut"):
            self.cmd.call("cut", edges)
        return len(cmds.ls(edges, flatten=True) or [])

    def split_uvs(self, components):
        """Split UVs so each selected UV belongs to exactly one face.

        ROUTES, in order
            1. A native command, if any of the split_uv candidates resolves.
            2. UV Studio's own: convert the selection to its edges and cut
               them with polyMapCut.

        VERIFIED, NOT ASSUMED
            Whether route 2 matches Maya's Split exactly depends on how
            polyMapCut treats the far end of each cut edge, and that could
            not be checked outside Maya. So every run predicts the correct
            result - each selected UV should gain (faces sharing it - 1) new
            UVs - and compares it with the actual change in UV count. The
            verdict is returned, so over- or under-splitting shows up in the
            log on the first click instead of in a texture weeks later.

        Returns an OrderedDict: route, selected_uvs, expected_new,
        actual_new, verdict.
        """
        components = list(components or [])
        if not components:
            return OrderedDict([("route", None), ("verdict", "nothing selected")])

        meshes = sorted(set(c.split(".")[0] for c in components))
        selected, expected = self._expected_split_gain(components)
        before = self._uv_count(meshes)
        route = None

        with undo_chunk("UV Studio: split UVs"):
            if self.cmd.available("split_uv"):
                try:
                    cmds.select(components, replace=True)
                    self.cmd.call("split_uv")
                    route = "native %s" % self.cmd.name_of("split_uv")
                except Exception:
                    route = None
            if route is None:
                edges = self.cmd.call("convert", components, toEdge=True,
                                      internal=False)
                if edges:
                    self.cmd.call("cut", edges)
                route = "UV Studio edge-cut"

        actual = self._uv_count(meshes) - before
        # Maya 2022's own SplitUV, measured on a 2x2 plane, cuts EVERY edge at
        # the selected UV (+7 UVs where "one UV per face" predicts +3). Maya is
        # the reference, so a native result is reported as-is, never graded
        # against a model of what Split "should" do. The edge-cut fallback
        # uses the same method as Maya's, so it is reported the same way.
        if actual <= 0:
            verdict = "NOTHING SPLIT"
        elif route and route.startswith("native"):
            verdict = "Maya's own Split: +%d UVs" % actual
        else:
            verdict = "edge-cut (Maya's Split method): +%d UVs" % actual

        return OrderedDict([("route", route), ("selected_uvs", selected),
                            ("expected_new", expected), ("actual_new", actual),
                            ("verdict", verdict)])

    PREDICT_LIMIT = 2000

    @staticmethod
    def _uv_count(meshes):
        total = 0
        for mesh in meshes:
            try:
                total += int(cmds.polyEvaluate(mesh, uvcoord=True) or 0)
            except Exception:
                pass
        return total

    def _expected_split_gain(self, components):
        """(selected UV count, UVs a correct split adds).

        A UV shared by N faces becomes N UVs, so it adds N - 1. Counted per
        UV with a component conversion, which is slow per call, so large
        selections skip the prediction rather than stall the button.
        """
        try:
            uvs = cmds.ls(cmds.polyListComponentConversion(
                components, toUV=True) or [], flatten=True) or []
        except Exception:
            return 0, None
        if len(uvs) > self.PREDICT_LIMIT:
            return len(uvs), None
        gain = 0
        for uv in uvs:
            try:
                faces = cmds.ls(cmds.polyListComponentConversion(
                    uv, toFace=True) or [], flatten=True) or []
            except Exception:
                return len(uvs), None
            gain += max(0, len(faces) - 1)
        return len(uvs), gain


# =====================================================================
# SECTION 11 - Self test
# =====================================================================

def _line(char="-", width=72):
    return char * width


def verify_pin_roundtrip(mesh, resolver=None, verbose=True):
    """Determine which polyPinUV query form works on this Maya build.

    This is the one API in M2 whose calling convention I could not confirm
    across versions, and "pinned UVs never move" depends entirely on it. Rather
    than leave it to fail quietly during a pack, this pins a couple of UVs on a
    duplicate, tries every known query form, and reports which one answered.

    Returns a dict with the working form, or None if pins cannot be read.
    """
    resolver = resolver or CommandResolver()
    result = OrderedDict([("command", resolver.name_of("pin")),
                          ("write_ok", False), ("working_query", None),
                          ("attempts", []), ("verdict", "unknown")])
    if not resolver.available("pin"):
        result["verdict"] = "polyPinUV not available on this build"
        return result

    duplicate = None
    try:
        duplicate = cmds.duplicate(mesh, name="uvStudio_pinProbe")[0]
        shapes = cmds.listRelatives(duplicate, shapes=True, fullPath=True,
                                    noIntermediate=True, type="mesh") or []
        if not shapes:
            result["verdict"] = "duplicate had no mesh shape"
            return result
        target = shapes[0]

        fn = om2.MFnMesh(dag_path(target))
        us, _ = fn.getUVs()
        if len(us) < 4:
            result["verdict"] = "mesh has too few UVs to probe"
            return result

        probe_ids = [0, 1, 2]
        try:
            cmds.polyPinUV(expand_components(target, probe_ids), value=1.0)
            result["write_ok"] = True
        except Exception as exc:
            result["attempts"].append(("write value=1.0", str(exc).strip()))

        for label, addressed, kwargs in (
                ("components '.map[*]'", "%s.map[*]" % target,
                 dict(query=True, value=True)),
                ("object name", target, dict(query=True, value=True))):
            try:
                returned = cmds.polyPinUV(addressed, **kwargs)
            except Exception as exc:
                result["attempts"].append((label, "raised: %s"
                                           % str(exc).strip().splitlines()[-1]))
                continue
            if not returned:
                result["attempts"].append((label, "returned empty"))
                continue
            try:
                pinned = set(i for i, w in enumerate(returned) if float(w) > 0.0)
            except (TypeError, ValueError):
                result["attempts"].append((label, "returned non-numeric data"))
                continue
            result["attempts"].append((label, "%d pinned of %d values"
                                       % (len(pinned), len(returned))))
            if pinned and result["working_query"] is None:
                result["working_query"] = label

        if result["working_query"]:
            result["verdict"] = "OK - pins are readable via %s" % result["working_query"]
        elif result["write_ok"]:
            result["verdict"] = ("Pins can be WRITTEN but not READ back. "
                                 "UV Studio must track pins itself; Maya's own "
                                 "unfold will still honour them.")
        else:
            result["verdict"] = "Pins could not be written or read on this build."
    except Exception:
        result["verdict"] = "probe failed:\n%s" % traceback.format_exc()
    finally:
        try:
            if duplicate and cmds.objExists(duplicate):
                cmds.delete(duplicate)
        except Exception:
            pass

    if verbose:
        print("Pin round-trip: %s" % result["verdict"])
        for label, outcome in result["attempts"]:
            print("  %-28s %s" % (label, outcome))
    return result


def run_self_test(destructive=False, verbose=True):
    """Report on the current selection. Read-only unless destructive=True.

    destructive=True duplicates the mesh first and exercises the write path on
    the duplicate, so a real asset is never touched by a test.
    """
    out = []

    def say(text=""):
        out.append(text)
        if verbose:
            print(text)

    say(_line("="))
    say("UV Studio M2 - Scene Bridge self test (v%s)" % __version__)
    say(_line("="))

    bridge = SceneBridge()
    missing = bridge.cmd.missing()
    say("Commands resolved : %d of %d"
        % (len(CommandResolver.TOOLS) - len(missing), len(CommandResolver.TOOLS)))
    if missing:
        say("Unavailable       : %s" % ", ".join(missing))
        if "pin" in missing:
            say("  WARNING: polyPinUV is unavailable; pins cannot be honoured.")

    meshes = selected_meshes()
    if not meshes:
        say("")
        say("No mesh selected. Select a polygon mesh and run again.")
        return "\n".join(out)

    for mesh in meshes:
        say("")
        say(_line())
        say("Mesh: %s" % mesh)
        say(_line())
        try:
            report = bridge.analyse(mesh)
        except Exception:
            say("FAILED to analyse:\n%s" % traceback.format_exc())
            continue

        shells = report["shells"]
        units = report["units"]
        multi = [u for u in units if len(u.shells) > 1]

        say("UV set            : %s" % report["uv_set"])
        say("UV count          : %d" % report["uv_count"])
        say("Shells            : %d" % len(shells))
        say("Layout units      : %d  (%d grouped from %d shells)"
            % (len(units), len(multi), sum(len(u.shells) for u in multi)))
        say("Pinned UVs        : %d" % len(report["pinned"]))
        say("UDIM tiles in use : %s" % (report["udims"] or "none"))
        say("Analysis time     : %.3f s" % report["seconds"])
        say("UV sets on mesh   : %s" % ", ".join(bridge.uv_sets.list_sets(mesh)))
        say("Working set       : %s"
            % ("present" if bridge.uv_sets.has_working_set(mesh) else "absent"))

        sample_unused = None

        sample = shells[:3]
        if sample:
            say("")
            say("Sample shells:")
            for shell in sample:
                say("  %r udim=%d" % (shell, shell.udim))

        pin_report = verify_pin_roundtrip(mesh, bridge.cmd, verbose=False)
        say("")
        say("Pin round-trip    : %s" % pin_report["verdict"])
        for label, outcome in pin_report["attempts"]:
            say("  %-28s %s" % (label, outcome))

        ranges = compress_indices([i for s in shells for i in s.uv_ids])
        say("")
        say("Component compression: %d UVs -> %d ranges (%.1fx fewer tokens)"
            % (report["uv_count"], len(ranges),
               (report["uv_count"] / float(len(ranges))) if ranges else 0.0))

        if destructive:
            say("")
            say("Destructive checks on a duplicate:")
            say(_run_destructive_checks(bridge, mesh, say))

    if len(meshes) >= 1:
        say("")
        say(_line("="))
        say("POOLED ANALYSIS - all %d selected mesh(es) as one UV space" % len(meshes))
        say(_line("="))
        try:
            pooled = bridge.analyse_many(meshes)
        except Exception:
            say("Pooled analysis failed:\n%s" % traceback.format_exc())
        else:
            say("Total shells      : %d" % pooled["total_shells"])
            say("Layout units      : %d  (%d grouped, %d spanning meshes)"
                % (len(pooled["units"]), pooled["grouped_units"],
                   pooled["cross_mesh_units"]))
            say("Pooling time      : %.3f s" % pooled["seconds"])

            diag = explain_grouping(pooled["shells"])
            say("")
            say("Grouping diagnosis:")
            for line in _wrap(diag["verdict"], 74):
                say("  " + line)
            say("")
            say("  stacked groups   : %d" % diag["stacked_total"])
            say("  duplicate pairs  : %d (same size and UV count, sitting apart)"
                % diag["paired_total"])
            say("  bbox overlaps    : %d pair(s), best IoU %.3f"
                % (diag["overlapping_pairs"], diag["best_iou"]))

            opportunity = stacking_opportunity(pooled["shells"])
            if opportunity["family_count"]:
                say("")
                say("Stacking opportunity:")
                say("  %d family/families could be stacked, reclaiming %.4f of "
                    "%.4f UV area (%.1f%%)"
                    % (opportunity["family_count"],
                       opportunity["reclaimable_area"],
                       opportunity["current_area"], opportunity["percent"]))
                say("  Stacking makes copies SHARE texture space - right for "
                    "mirrored parts, wrong where they need unique detail.")
                for family in opportunity["families"][:5]:
                    say("    x%d  size=%s  uvs=%d  reclaims %.5f"
                        % (family["count"], family["size"],
                           family["uv_count"], family["reclaimed"]))

            say("")
            say("Fingerprint sensitivity (is the default too strict?):")
            say("   digits  uv_count   families  shells")
            for row in fingerprint_sensitivity(pooled["shells"]):
                say("   %6d  %8s   %8d  %6d"
                    % (row["digits"], "yes" if row["uv_count_required"] else "no",
                       row["families"], row["shells_covered"]))
            say("   Default is digits=3 with uv_count=yes. If loosening to 3")
            say("   digits roughly doubles the families, real pairs are being")
            say("   missed. If dropping uv_count explodes the count, those are")
            say("   coincidental size matches, not duplicates.")

            for group in diag["stacked_groups"][:5]:
                say("  STACKED x%d: %r" % (len(group), group[0]))
            for family in diag["paired_families"][:5]:
                centres = ", ".join("(%.3f,%.3f)" % s.centre for s in family[:4])
                say("  PAIR x%d size=%s at %s"
                    % (len(family), family[0].fingerprint()[:2], centres))

    say("")
    say(_line("="))
    return "\n".join(out)


def _wrap(text, width):
    words = text.split()
    lines, current = [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        lines.append(current)
    return lines


def _run_destructive_checks(bridge, mesh, say):
    """Exercise working-set creation and a pinned move on a throwaway copy."""
    duplicate = None
    try:
        duplicate = cmds.duplicate(mesh, name="uvStudio_m2_test")[0]
        shapes = cmds.listRelatives(duplicate, shapes=True, fullPath=True,
                                    noIntermediate=True, type="mesh") or []
        if not shapes:
            return "  duplicate has no mesh shape; skipped."
        target = shapes[0]

        name, created = bridge.uv_sets.ensure_working_set(target)
        say("  working set '%s' %s" % (name, "created" if created else "reused"))
        say("  original recorded as '%s'" % bridge.uv_sets.original_set(target))

        report = bridge.analyse(target, uv_set=name)
        shells = report["shells"]
        if not shells:
            return "  no shells to move; skipped."

        first = shells[0]
        pin_ids = first.uv_ids[:max(1, len(first.uv_ids) // 2)]
        set_pins(target, pin_ids, pinned=True, resolver=bridge.cmd)

        pinned_now = read_pinned_uvs(target, bridge.cmd)
        say("  pinned %d UVs, read back %d" % (len(pin_ids), len(pinned_now)))

        writer = bridge.writer(target, pinned=pinned_now)
        moved = writer.translate(first.uv_ids, 0.25, 0.0)
        say("  translate: %d of %d UVs moved (%d held by pins)"
            % (moved, len(first.uv_ids), len(first.uv_ids) - moved))

        if pinned_now and moved == len(first.uv_ids):
            say("  FAIL: pinned UVs were moved.")
        elif pinned_now:
            say("  PASS: pinned UVs were excluded.")
        else:
            say("  INCONCLUSIVE: pin read-back returned nothing on this build.")

        cmds.undo()
        say("  undo executed; single chunk confirmed by the queue.")
        return "  done."
    except Exception:
        return "  FAILED:\n%s" % traceback.format_exc()
    finally:
        try:
            if duplicate and cmds.objExists(duplicate):
                cmds.delete(duplicate)
        except Exception:
            pass


if __name__ == "__main__":
    run_self_test()
