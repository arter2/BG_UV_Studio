"""
Smart Mirror & Weld  -  Maya 2022+

Mirrors the selected polygon mesh(es) across a world axis, combines the
original with the mirrored copy and welds the seam vertices.

Usage: paste into a Python tab of the Script Editor and run, or put this file
on your PYTHONPATH and run:

    import smart_mirror_weld
    smart_mirror_weld.show()

Works with Maya 2022's Python 3 (default) and Python 2 (-pythonver 2) modes.
"""
from __future__ import absolute_import, division, print_function

import traceback

from maya import cmds
import maya.api.OpenMaya as om2
from maya.app.general.mayaMixin import MayaQWidgetBaseMixin
from PySide2 import QtCore, QtWidgets


WINDOW_OBJECT_NAME = "SmartMirrorWeldWindow"
OPTION_PREFIX = "smartMirrorWeld_"

AXES = ("x", "y", "z")
PLANE_PIVOT = "Object Pivot"
PLANE_ORIGIN = "World Origin"
PLANE_BBOX_MIN = "Bounding Box Min"
PLANE_BBOX_MAX = "Bounding Box Max"
PLANE_MODES = (PLANE_PIVOT, PLANE_ORIGIN, PLANE_BBOX_MIN, PLANE_BBOX_MAX)

# The merge slider works in steps of 0.0001 units.
SLIDER_SCALE = 10000.0


# -----------------------------------------------------------------------------
# Core mirror logic (no UI dependencies)
# -----------------------------------------------------------------------------
def _short_name(node):
    return node.split("|")[-1]


def _mesh_shape(transform):
    shapes = cmds.listRelatives(
        transform, shapes=True, noIntermediate=True, fullPath=True
    ) or []
    for shape in shapes:
        if cmds.nodeType(shape) == "mesh":
            return shape
    return None


def _mfn_mesh(node):
    sel = om2.MSelectionList()
    sel.add(node)
    return om2.MFnMesh(sel.getDagPath(0))


def _world_points(shape):
    return _mfn_mesh(shape).getPoints(om2.MSpace.kWorld)


def _plane_coordinate(transform, axis_index, plane_mode):
    """World-space position of the mirror plane along the chosen axis."""
    if plane_mode == PLANE_ORIGIN:
        return 0.0
    if plane_mode == PLANE_PIVOT:
        pivot = cmds.xform(transform, query=True, worldSpace=True, rotatePivot=True)
        return pivot[axis_index]
    bbox = cmds.exactWorldBoundingBox(transform)
    if plane_mode == PLANE_BBOX_MIN:
        return bbox[axis_index]
    return bbox[axis_index + 3]


def _clean_duplicate(target):
    """Duplicate target as a bare mesh: no history, children or orig shapes."""
    dup = cmds.duplicate(target, returnRootsOnly=True,
                         name=_short_name(target) + "_Mirrored")[0]
    dup = cmds.ls(dup, long=True)[0]
    cmds.delete(dup, constructionHistory=True)

    for child in cmds.listRelatives(dup, children=True, fullPath=True) or []:
        if cmds.nodeType(child) != "mesh" or cmds.getAttr(child + ".intermediateObject"):
            cmds.delete(child)
    return dup


def mirror_and_weld(target, axis="x", plane_mode=PLANE_PIVOT, combine=True,
                    merge_threshold=0.001, seam_tolerance=0.01, snap_seam=True):
    """Mirror one mesh transform. Returns (result_node, welded_vertex_count)."""
    axis_index = AXES.index(axis)
    plane = _plane_coordinate(target, axis_index, plane_mode)

    # Warn if the mesh straddles the plane: the mirrored half would overlap it.
    points = _world_points(_mesh_shape(target))
    above = any(p[axis_index] > plane + seam_tolerance for p in points)
    below = any(p[axis_index] < plane - seam_tolerance for p in points)
    if above and below:
        cmds.warning(
            "Smart Mirror: '{0}' has geometry on both sides of the mirror "
            "plane; the halves will overlap.".format(_short_name(target))
        )

    # Mirror the duplicate's vertices in world space. Working on the points
    # (not the transform) gives a true world-axis mirror even for rotated
    # objects, and leaves no negative scale behind.
    dup = _clean_duplicate(target)
    dup_shape = _mesh_shape(dup)
    fn = _mfn_mesh(dup_shape)
    mirrored = om2.MPointArray()
    for p in fn.getPoints(om2.MSpace.kWorld):
        coords = [p.x, p.y, p.z]
        coords[axis_index] = 2.0 * plane - coords[axis_index]
        mirrored.append(om2.MPoint(coords[0], coords[1], coords[2]))
    fn.setPoints(mirrored, om2.MSpace.kWorld)
    fn.updateSurface()

    # Mirroring flips the face winding, so reverse normals to point outward.
    cmds.polyNormal(dup, normalMode=0, userNormalMode=0, constructionHistory=False)

    if not combine:
        return dup, 0

    parent = cmds.listRelatives(target, parent=True, fullPath=True)
    pivot = cmds.xform(target, query=True, worldSpace=True, rotatePivot=True)
    base_name = _short_name(target)

    combined = cmds.polyUnite(target, dup, constructionHistory=True,
                              mergeUVSets=1, name=base_name + "_Geo")[0]
    cmds.delete(combined, constructionHistory=True)
    combined = cmds.ls(combined, long=True)[0]

    # polyUnite leaves the source transforms behind; remove the empty ones.
    for leftover in (target, dup):
        if cmds.objExists(leftover) and not cmds.listRelatives(leftover, children=True):
            cmds.delete(leftover)

    # Find vertices on the mirror plane.
    shape = _mesh_shape(combined)
    fn = _mfn_mesh(shape)
    points = fn.getPoints(om2.MSpace.kWorld)
    seam_ids = [i for i, p in enumerate(points)
                if abs(p[axis_index] - plane) <= seam_tolerance]

    welded = 0
    if seam_ids:
        if snap_seam:
            for i in seam_ids:
                p = points[i]
                coords = [p.x, p.y, p.z]
                coords[axis_index] = plane
                points[i] = om2.MPoint(coords[0], coords[1], coords[2])
            fn.setPoints(points, om2.MSpace.kWorld)
            fn.updateSurface()

        before = cmds.polyEvaluate(combined, vertex=True)
        seam_verts = ["{0}.vtx[{1}]".format(combined, i) for i in seam_ids]
        cmds.polyMergeVertex(seam_verts, distance=merge_threshold,
                             constructionHistory=True)
        cmds.delete(combined, constructionHistory=True)
        welded = before - cmds.polyEvaluate(combined, vertex=True)

    # Restore pivot, hierarchy and name of the original object.
    cmds.xform(combined, worldSpace=True, rotatePivot=pivot, scalePivot=pivot)
    if parent:
        combined = cmds.parent(combined, parent[0])[0]
        combined = cmds.ls(combined, long=True)[0]
    if not cmds.objExists(target):
        combined = cmds.rename(combined, base_name)
    return combined, welded


# -----------------------------------------------------------------------------
# UI
# -----------------------------------------------------------------------------
class SmartMirrorTool(MayaQWidgetBaseMixin, QtWidgets.QDialog):

    def __init__(self, parent=None):
        super(SmartMirrorTool, self).__init__(parent)
        self.setObjectName(WINDOW_OBJECT_NAME)
        self.setWindowTitle("Smart Mirror & Weld")
        self.setWindowFlags(QtCore.Qt.Window | QtCore.Qt.Tool)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.setMinimumWidth(320)

        self._build_ui()
        self._load_settings()

    # --- layout --------------------------------------------------------------
    def _build_ui(self):
        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(10)

        # Mirror settings
        mirror_group = QtWidgets.QGroupBox("Mirror")
        mirror_form = QtWidgets.QFormLayout(mirror_group)

        axis_row = QtWidgets.QHBoxLayout()
        self.axis_buttons = QtWidgets.QButtonGroup(self)
        for i, axis in enumerate(AXES):
            radio = QtWidgets.QRadioButton(axis.upper())
            self.axis_buttons.addButton(radio, i)
            axis_row.addWidget(radio)
        self.axis_buttons.button(0).setChecked(True)
        axis_row.addStretch()
        mirror_form.addRow("Axis:", axis_row)

        self.plane_combo = QtWidgets.QComboBox()
        self.plane_combo.addItems(PLANE_MODES)
        self.plane_combo.setToolTip(
            "Where the mirror plane sits along the chosen axis."
        )
        mirror_form.addRow("Mirror plane:", self.plane_combo)
        main_layout.addWidget(mirror_group)

        # Weld settings
        self.weld_group = QtWidgets.QGroupBox("Combine and weld seam")
        self.weld_group.setCheckable(True)
        self.weld_group.setChecked(True)
        weld_form = QtWidgets.QFormLayout(self.weld_group)

        self.thresh_spin = QtWidgets.QDoubleSpinBox()
        self.thresh_spin.setDecimals(4)
        self.thresh_spin.setRange(0.0001, 1.0)
        self.thresh_spin.setSingleStep(0.0005)
        self.thresh_spin.setValue(0.001)
        self.thresh_spin.setToolTip("Vertices closer than this are merged.")

        self.thresh_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.thresh_slider.setRange(1, 100)  # 0.0001 - 0.0100
        self.thresh_slider.setValue(10)

        thresh_row = QtWidgets.QHBoxLayout()
        thresh_row.addWidget(self.thresh_spin)
        thresh_row.addWidget(self.thresh_slider, 1)
        weld_form.addRow("Merge threshold:", thresh_row)

        self.seam_spin = QtWidgets.QDoubleSpinBox()
        self.seam_spin.setDecimals(4)
        self.seam_spin.setRange(0.0001, 10.0)
        self.seam_spin.setSingleStep(0.001)
        self.seam_spin.setValue(0.01)
        self.seam_spin.setToolTip(
            "Vertices within this distance of the mirror plane count as seam "
            "vertices."
        )
        weld_form.addRow("Seam tolerance:", self.seam_spin)

        self.snap_check = QtWidgets.QCheckBox("Snap seam vertices to plane")
        self.snap_check.setChecked(True)
        self.snap_check.setToolTip(
            "Flatten seam vertices onto the mirror plane before welding so "
            "slightly-off vertices still merge cleanly."
        )
        weld_form.addRow(self.snap_check)
        main_layout.addWidget(self.weld_group)

        # Buttons
        button_row = QtWidgets.QHBoxLayout()
        self.btn_execute = QtWidgets.QPushButton("Mirror and Weld")
        self.btn_execute.setStyleSheet(
            "QPushButton {font-weight: bold; padding: 8px; "
            "background-color: #2b7bb9; color: white;}"
            "QPushButton:hover {background-color: #3a8fd0;}"
        )
        self.btn_close = QtWidgets.QPushButton("Close")
        self.btn_close.setStyleSheet("padding: 8px;")
        button_row.addWidget(self.btn_execute, 1)
        button_row.addWidget(self.btn_close)
        main_layout.addLayout(button_row)

        self.status_label = QtWidgets.QLabel("Select a polygon mesh.")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #aaaaaa;")
        main_layout.addWidget(self.status_label)

        # Signals
        self.thresh_slider.valueChanged.connect(self._slider_changed)
        self.thresh_spin.valueChanged.connect(self._spin_changed)
        self.weld_group.toggled.connect(self._update_button_label)
        self.btn_execute.clicked.connect(self.run_mirror)
        self.btn_close.clicked.connect(self.close)

    def _slider_changed(self, value):
        self.thresh_spin.blockSignals(True)
        self.thresh_spin.setValue(value / SLIDER_SCALE)
        self.thresh_spin.blockSignals(False)

    def _spin_changed(self, value):
        self.thresh_slider.blockSignals(True)
        self.thresh_slider.setValue(int(round(value * SLIDER_SCALE)))
        self.thresh_slider.blockSignals(False)

    def _update_button_label(self, weld):
        self.btn_execute.setText("Mirror and Weld" if weld else "Mirror Copy")

    def _set_status(self, text, error=False):
        self.status_label.setStyleSheet(
            "color: #e06c6c;" if error else "color: #8fd18f;"
        )
        self.status_label.setText(text)

    # --- settings (stored as Maya optionVars) --------------------------------
    def _load_settings(self):
        def get(key, default):
            name = OPTION_PREFIX + key
            return cmds.optionVar(query=name) if cmds.optionVar(exists=name) else default

        axis_id = int(get("axis", 0))
        if self.axis_buttons.button(axis_id):
            self.axis_buttons.button(axis_id).setChecked(True)
        index = self.plane_combo.findText(get("plane", PLANE_PIVOT))
        self.plane_combo.setCurrentIndex(max(index, 0))
        self.weld_group.setChecked(bool(get("combine", 1)))
        self.thresh_spin.setValue(float(get("threshold", 0.001)))
        self.seam_spin.setValue(float(get("seamTolerance", 0.01)))
        self.snap_check.setChecked(bool(get("snap", 1)))
        self._spin_changed(self.thresh_spin.value())
        self._update_button_label(self.weld_group.isChecked())

    def _save_settings(self):
        cmds.optionVar(intValue=(OPTION_PREFIX + "axis", self.axis_buttons.checkedId()))
        cmds.optionVar(stringValue=(OPTION_PREFIX + "plane", self.plane_combo.currentText()))
        cmds.optionVar(intValue=(OPTION_PREFIX + "combine", int(self.weld_group.isChecked())))
        cmds.optionVar(floatValue=(OPTION_PREFIX + "threshold", self.thresh_spin.value()))
        cmds.optionVar(floatValue=(OPTION_PREFIX + "seamTolerance", self.seam_spin.value()))
        cmds.optionVar(intValue=(OPTION_PREFIX + "snap", int(self.snap_check.isChecked())))

    def closeEvent(self, event):
        self._save_settings()
        super(SmartMirrorTool, self).closeEvent(event)

    # --- action --------------------------------------------------------------
    def run_mirror(self):
        # objectsOnly also resolves component/shape selections to their objects.
        targets = []
        for node in cmds.ls(selection=True, long=True, objectsOnly=True) or []:
            if cmds.nodeType(node) == "mesh":
                node = cmds.listRelatives(node, parent=True, fullPath=True)[0]
            if node not in targets and _mesh_shape(node):
                targets.append(node)
        if not targets:
            cmds.warning("Smart Mirror: please select one or more polygon meshes.")
            self._set_status("No polygon mesh selected.", error=True)
            return

        axis = AXES[self.axis_buttons.checkedId()]
        options = dict(
            axis=axis,
            plane_mode=self.plane_combo.currentText(),
            combine=self.weld_group.isChecked(),
            merge_threshold=self.thresh_spin.value(),
            seam_tolerance=self.seam_spin.value(),
            snap_seam=self.snap_check.isChecked(),
        )
        self._save_settings()

        results = []
        total_welded = 0
        # One undo chunk so Ctrl+Z reverts the whole operation in one step.
        cmds.undoInfo(openChunk=True, chunkName="smartMirrorWeld")
        try:
            for target in targets:
                result, welded = mirror_and_weld(target, **options)
                results.append(result)
                total_welded += welded
        except Exception as exc:
            traceback.print_exc()
            cmds.warning("Smart Mirror: {0}".format(exc))
            self._set_status("Error: {0}".format(exc), error=True)
            return
        finally:
            cmds.undoInfo(closeChunk=True)

        cmds.select(results, replace=True)
        if options["combine"]:
            message = "Mirrored {0} mesh(es) on {1}, welded {2} vertices.".format(
                len(results), axis.upper(), total_welded)
            if total_welded == 0:
                message += " No seam found - check the mirror plane / tolerance."
        else:
            message = "Created {0} mirrored copy(s) on {1}.".format(
                len(results), axis.upper())
        print("Smart Mirror: " + message)
        self._set_status(message)


def show():
    """Open the tool, replacing any window that is already open."""
    app = QtWidgets.QApplication.instance()
    for widget in app.topLevelWidgets():
        if widget.objectName() == WINDOW_OBJECT_NAME:
            widget.close()
            widget.deleteLater()

    window = SmartMirrorTool()
    window.show()
    return window


if __name__ == "__main__":
    smart_mirror_win = show()
