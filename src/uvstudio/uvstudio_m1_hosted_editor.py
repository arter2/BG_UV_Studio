\
"""
UV Studio - Module 1 v2: Compatibility Layer + Hosted UV Editor
===============================================================

WHY THIS IS A REWRITE, NOT A PATCH
    v1 tried three times to make Maya build its UI inside a Qt hierarchy:
    reparenting a Maya layout into a QLayout, then registering a QWidget with
    cmds.setParent. That direction is not supported by Maya's API and the
    failures were not incidental:

      attempt 1  crashed Maya on close - Qt destroyed a widget Maya owned
      attempt 2  malformed control path - unnamed widgets in the Qt chain
      attempt 3  path resolved, Maya still would not build into it

    Maya provides exactly one sanctioned crossing: MQtUtil.addWidgetToMayaLayout,
    which puts a QWidget INTO a Maya layout. There is no reverse. So the
    hierarchy is inverted here:

        workspaceControl            <- Maya owns the window
          paneLayout                <- Maya owns the split
            polyTexturePlacementPanel   <- Maya's real UV Editor, untouched
            formLayout              <- Maya owns the container
              [our Qt tools widget] <- inserted via the supported call

    Teardown is now one deleteUI on a Maya control. Nothing Qt-owned is ever
    deleted by Maya and nothing Maya-owned is ever deleted by Qt, which is what
    removes the crash surface rather than merely guarding it.

BONUS PROPERTIES OF workspaceControl
    Dockable into Maya's layout, remembers size and position, survives
    workspace switches, and is the standard host for Maya 2017+ tools.

USAGE (Script Editor, Python tab)
    Paste and run. Or:
        import uvstudio_m1_hosted_editor as m1
        m1.show()
        m1.close()

Target: Maya 2022 - 2025+ (PySide2 and PySide6)
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from collections import OrderedDict

import maya.cmds as cmds
import maya.mel as mel
import maya.OpenMayaUI as omui

__version__ = "2.13.0"
MODULE_ID = "M1"

CONTROL_NAME = "uvStudioWorkspaceControl"
CONTROL_LABEL = "UV Studio"
PANEL_TYPE = "polyTexturePlacementPanel"


# =====================================================================
# SECTION 1 - Qt compatibility shim
# =====================================================================

QT_BINDING = None
QtCore = QtGui = QtWidgets = None
wrapInstance = getCppPointer = None
_QT_IMPORT_ERROR = None

try:
    from PySide6 import QtCore, QtGui, QtWidgets           # noqa: F401
    from shiboken6 import wrapInstance, getCppPointer      # noqa: F401
    QT_BINDING = "PySide6"
except ImportError:
    try:
        from PySide2 import QtCore, QtGui, QtWidgets       # noqa: F401
        from shiboken2 import wrapInstance, getCppPointer  # noqa: F401
        QT_BINDING = "PySide2"
    except ImportError as exc:                              # pragma: no cover
        _QT_IMPORT_ERROR = exc


# @include _shared/qt_enum.py


def maya_layout_to_qwidget(layout_name):
    """Wrap a Maya layout as a QWidget. Maya keeps ownership."""
    for finder in ("findLayout", "findControl"):
        fn = getattr(omui.MQtUtil, finder, None)
        if fn is None:
            continue
        try:
            ptr = fn(layout_name)
        except Exception:
            ptr = None
        if ptr:
            return wrapInstance(int(ptr), QtWidgets.QWidget)
    return None


def add_widget_to_maya_layout(widget, layout_name):
    """Insert a QWidget into a Maya layout - the one supported crossing.

    Two routes, in order of preference:
      1. MQtUtil.addWidgetToMayaLayout, the sanctioned API.
      2. Wrap the Maya layout and give it a QLayout holding our widget.

    Route 2 is used when route 1 is absent (it is not present on every build)
    and is still safe, because the widget becomes a CHILD of a Maya-owned
    parent rather than the other way round. Maya deletes it with its parent;
    Qt never deletes anything Maya owns.
    """
    adder = getattr(omui.MQtUtil, "addWidgetToMayaLayout", None)
    if adder is not None:
        try:
            widget_ptr = int(getCppPointer(widget)[0])
            layout_ptr = omui.MQtUtil.findLayout(layout_name)
            if layout_ptr:
                adder(widget_ptr, int(layout_ptr))
                return "addWidgetToMayaLayout"
        except Exception:
            pass

    host = maya_layout_to_qwidget(layout_name)
    if host is None:
        return None
    try:
        existing = host.layout()
        if existing is None:
            existing = QtWidgets.QVBoxLayout(host)
            existing.setContentsMargins(0, 0, 0, 0)
            existing.setSpacing(0)
        existing.addWidget(widget)
        return "QLayout on wrapped Maya layout"
    except Exception:
        return None


# =====================================================================
# SECTION 2 - Environment probe
# =====================================================================

class Environment(object):
    def __init__(self):
        self.maya_version = self._about("version", "unknown")
        self.api_version = self._about("apiVersion", 0)
        self.product = self._about("product", "unknown")
        self.cut_id = self._about("cutIdentifier", "unknown")
        self.qt_version = self._about("qtVersion", "unknown")
        self.os_name = self._about("operatingSystem", sys.platform)
        self.batch = bool(self._about("batch", False))
        self.qt_binding = QT_BINDING
        self.python_version = "%d.%d.%d" % sys.version_info[:3]
        self.numpy = self._probe_numpy()
        self.user_app_dir = cmds.internalVar(userAppDir=True)

    @staticmethod
    def _about(flag, default):
        try:
            return cmds.about(**{flag: True})
        except Exception:
            return default

    @staticmethod
    def _probe_numpy():
        try:
            import numpy
            return numpy.__version__
        except Exception:
            return None

    @property
    def doc_help_version(self):
        digits = "".join(c for c in str(self.maya_version) if c.isdigit())[:4]
        return digits if len(digits) == 4 else "2024"

    def as_dict(self):
        return OrderedDict([
            ("maya_version", self.maya_version),
            ("api_version", self.api_version),
            ("product", self.product),
            ("cut_id", self.cut_id),
            ("qt_binding", self.qt_binding),
            ("qt_version", self.qt_version),
            ("python_version", self.python_version),
            ("numpy", self.numpy or "NOT AVAILABLE"),
            ("os", self.os_name),
            ("batch_mode", self.batch),
            ("user_app_dir", self.user_app_dir),
        ])


# =====================================================================
# SECTION 3 - Native command registry
# =====================================================================

def _bridge():
    """M2, if it loaded. The command catalog lives there now."""
    return sys.modules.get("uvstudio_m2_scene_bridge")



def doc_url(command, help_version):
    return ("https://help.autodesk.com/cloudhelp/%s/ENU/"
            "Maya-Tech-Docs/CommandsPython/%s.html" % (help_version, command))


def resolve_commands(env):
    """Ask M2 what resolved. M1 no longer keeps its own candidate list.

    It used to, and the two lists could disagree: the Cmds tab resolved
    `polySplitUV` through one table while the bridge refused to call it
    through another, so the tab said available and the button said missing.
    A second table is not redundancy, it is a second answer.
    """
    bridge = _bridge()
    if bridge is None:
        return OrderedDict()
    try:
        return bridge.default_resolver().report(
            doc_version=env.doc_help_version)
    except Exception:
        return OrderedDict()


def resolution_summary(resolved):
    total = sum(len(rows) for rows in resolved.values())
    ok = sum(1 for rows in resolved.values() for r in rows if r["resolved"])
    return ok, total


# =====================================================================
# SECTION 4 - Tools panel (pure Qt, owned by Maya once inserted)
# =====================================================================

WARN_COLOUR = "#b8860b"
FAIL_COLOUR = "#c0392b"
# Deliberately a fixed mid grey rather than a palette role. QPalette.Mid sits
# almost exactly on Maya's dark background, which rendered the whole label
# column invisible. This reads on both dark and light schemes.
MUTED_COLOUR = "#8f8f8f"


class ToolsPanel(QtWidgets.QWidget):
    """Fallback panel: shown only when M5c did not load.

    This used to be a second, lesser copy of the real panel - its own
    environment table, its own command tree, its own log view, ~180 lines
    that the bundle overwrote with M5c's panel on every successful start.
    Dead code that renders is worse than dead code that does not: it made
    "the panel module failed" look like "the panel works, oddly".

    So it now says what went wrong and nothing else. If you are reading this
    on screen, M5c raised during import and the Log tab of the real panel is
    not there to tell you.
    """

    def __init__(self, env, resolved, parent=None):
        super(ToolsPanel, self).__init__(parent)
        self.setObjectName("uvStudioToolsPanel")
        self.embed_log = []

        layout = QtWidgets.QVBoxLayout(self)
        headline = QtWidgets.QLabel("UV Studio: the tools panel did not load.")
        headline.setStyleSheet("color: %s; font-weight: 600;" % FAIL_COLOUR)
        headline.setWordWrap(True)
        layout.addWidget(headline)

        self.detail = QtWidgets.QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlainText(
            "M5c (panel widgets) is not available, so the buttons, fields "
            "and recipe view cannot be built.\n\n"
            "Environment and command resolution still worked, so the bridge "
            "is probably fine; re-paste uvstudio.py and check the bootstrap "
            "errors it prints.")
        layout.addWidget(self.detail, 1)

    # The host and the bundle call these on whatever panel is in place.
    def log(self, level, message):
        line = "[%s] %s" % (level, message)
        self.embed_log.append(line)
        self.detail.appendPlainText(line)

    def set_verdict(self, ok, headline, detail=""):
        self.detail.appendPlainText("%s %s\n%s"
                                    % ("OK" if ok else "FAIL", headline,
                                       detail))

    def set_log(self, entries):
        for entry in entries or []:
            self.detail.appendPlainText(str(entry))


# Set by the bundle to a callable returning the Cluster Map widget. None when
# M9 did not load; the switch bar then offers the Maya editor only.
MapFactory = None
# Set by the bundle: callable(tools_panel) -> widget for the RIGHT end of
# the viewport bar (the selection modes, per the artist's layout).
ViewBarFactory = None


class ViewSwitch(QtWidgets.QWidget):
    """Maya UV Editor | Cluster Map, above the left pane."""

    def __init__(self, host, parent=None):
        super(ViewSwitch, self).__init__(parent)
        self.host = host
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(4)
        self.group = QtWidgets.QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = {}
        for key, label in (("editor", "Maya UV Editor"), ("map", "Cluster Map")):
            b = QtWidgets.QPushButton(label)
            b.setCheckable(True)
            b.setFixedHeight(22)
            b.clicked.connect(lambda _c=False, k=key: self.host.set_view(k))
            self.group.addButton(b)
            row.addWidget(b)
            self.buttons[key] = b
        self.reload = QtWidgets.QPushButton("Reload map")
        self.reload.setFixedHeight(22)
        self.reload.setToolTip("Re-read the selected meshes into the map.")
        self.reload.clicked.connect(
            lambda _c=False: self.host.reload_map(force=True))
        row.addWidget(self.reload)
        self.texture = QtWidgets.QComboBox()
        self.texture.addItems(["Texture: off", "Texture: as is",
                               "Texture: after transfer"])
        self.texture.setFixedHeight(22)
        self.texture.setToolTip("Show the texture under the shells: as it "
                                "is now, or as Transfer Textures would make "
                                "it (previewed in memory, nothing saved).")
        self.texture.currentIndexChanged.connect(
            lambda i: self.host.set_texture_mode(("off", "asis", "after")[i]))
        row.addWidget(self.texture)
        row.addStretch(1)
        self.pair = QtWidgets.QPushButton("Pair mode")
        self.pair.setCheckable(True)
        self.pair.setFixedHeight(22)
        self.pair.setToolTip("Opens the Cluster Map; drag from one shell to "
                             "another to pair them. Turn off to return to "
                             "Maya's UV Editor.")
        self.pair.toggled.connect(lambda on: self.host.set_pair_mode(on))
        row.addWidget(self.pair)
        self._row = row
        self.buttons["editor"].setChecked(True)
        self.reload.setVisible(False)
        self.texture.setVisible(False)

    def add_extra(self, widget):
        """A widget at the right end of the bar, after Pair mode."""
        self._row.addSpacing(8)
        self._row.addWidget(widget)

    def show_state(self, view, map_available):
        self.buttons["map"].setEnabled(map_available)
        if not map_available:
            self.buttons["map"].setToolTip("Cluster Map did not load; see Log.")
        self.buttons[view].setChecked(True)
        self.reload.setVisible(view == "map")
        self.texture.setVisible(view == "map")


class UVStudioHost(object):
    """Builds the workspaceControl and everything inside it.

    Maya creates and owns every control here. Our only Qt object is the tools
    panel, and it is inserted into a Maya layout through the supported call,
    so it is destroyed as that layout's child and never independently.
    """

    def __init__(self):
        self.env = Environment()
        self.resolved = resolve_commands(self.env)
        self.control = None
        self.pane = None
        self.panel = None
        self.tools_form = None
        self.tools_panel = None
        self.adopted = False
        self.log = []

    # -- logging -----------------------------------------------------
    def _log(self, level, message):
        self.log.append((level, message))

    def _log_exc(self, context):
        self._log("ERROR", "%s\n%s" % (context, traceback.format_exc()))

    # -- build -------------------------------------------------------
    def build(self):
        self._destroy_existing()

        try:
            self.control = cmds.workspaceControl(
                CONTROL_NAME, label=CONTROL_LABEL, retain=False,
                floating=True, initialWidth=1500, initialHeight=860)
            self._log("OK", "workspaceControl created: %s" % self.control)
            # Maya remembers a workspaceControl's docked state in the user's
            # workspace prefs, keyed by NAME. A control left collapsed,
            # tabbed behind something, or docked off a monitor that is no
            # longer attached comes back the same way - created successfully,
            # visible nowhere. Restoring and raising explicitly is the
            # difference between "no window" and "no window and no idea why".
            try:
                cmds.workspaceControl(self.control, edit=True, restore=True,
                                      visible=True)
                cmds.workspaceControl(self.control, edit=True,
                                      floating=True)
                visible = cmds.workspaceControl(self.control, query=True,
                                                visible=True)
                self._log("OK" if visible else "ERROR",
                          "control visible: %s" % visible)
            except Exception:
                self._log_exc("Could not restore or raise the control")
        except Exception:
            self._log_exc("Creating the workspaceControl failed")
            return False

        try:
            cmds.setParent(self.control)
            self.pane = cmds.paneLayout(configuration="vertical2")
            self._log("OK", "paneLayout created: %s" % self.pane)
        except Exception:
            self._log_exc("Creating the pane layout failed")
            return False

        self.editor_parent = self.pane
        self._build_left()
        hosted = self._host_uv_editor()

        try:
            cmds.setParent(self.pane)
            self.tools_form = cmds.formLayout("uvStudioToolsForm")
            cmds.setParent("..")
        except Exception:
            self._log_exc("Creating the tools container failed")
            return False

        try:
            self.tools_panel = ToolsPanel(self.env, self.resolved)
        except Exception:
            # Unguarded, this threw straight out of build() and out of
            # show(), past the log that would have said how far we got.
            self._log_exc("Building the tools panel failed")
            return False
        route = add_widget_to_maya_layout(self.tools_panel, self.tools_form)
        if route:
            self._log("OK", "Tools panel inserted via %s" % route)
        else:
            self._log("ERROR", "Could not insert the tools panel into %s"
                      % self.tools_form)
            return False

        # A formLayout child with no attachments sits at its size hint and
        # never resizes - the panel appears as a small box in the corner while
        # the rest of the pane stays empty. Inserting is only half the job.
        if route == "addWidgetToMayaLayout":
            self._stretch_tools_panel()

        self._mount_map()
        if ViewBarFactory is not None and self.view_switch is not None:
            try:
                self.view_switch.add_extra(ViewBarFactory(self.tools_panel))
                self._log("OK", "Selection modes placed in the viewport bar")
            except Exception:
                self._log_exc("Placing the selection modes failed")

        try:
            cmds.paneLayout(self.pane, edit=True, paneSize=[1, 62, 100])
        except Exception:
            self._log("WARN", "Could not set the initial split ratio.")

        self._publish_verdict(hosted)
        if hosted:
            self._refresh_editor()
        return True

    def _refresh_editor(self):
        """Nudge the hosted panel into drawing the current selection.

        The UV Editor redraws on selection change. Reparenting it does not
        raise one, so a panel adopted after the artist made their selection
        comes up empty and looks broken. Re-applying the existing selection
        one idle cycle later gives it the event it missed, without changing
        what is selected.
        """
        def _nudge():
            try:
                selection = cmds.ls(selection=True, long=True) or []
                if selection:
                    cmds.select(selection, replace=True)
                cmds.refresh()
            except Exception:
                pass
        try:
            cmds.evalDeferred(_nudge, lowestPriority=True)
        except Exception:
            _nudge()

    # -- left pane: switch, editor, map --------------------------------
    def _build_left(self):
        """Left pane = formLayout[switch bar / editor pane | map form].

        All three are Maya layouts; the switch bar and the map are Qt
        widgets inserted INTO them via addWidgetToMayaLayout - the one
        supported crossing. Switching views toggles `manage` on Maya's side,
        so nothing is ever reparented. On any failure the editor goes
        straight into the pane as before and the map is simply unavailable.
        """
        self.left_form = self.switch_form = None
        self.editor_pane = self.map_form = None
        self.view_switch = self.map_widget = None
        self.view = "editor"
        self.map_available = False
        self.pair_mode = False
        self._view_before_pair = None
        try:
            cmds.setParent(self.pane)
            left = cmds.formLayout()
            switch = cmds.formLayout(parent=left, height=28)
            editor = cmds.paneLayout(configuration="single", parent=left)
            mapf = cmds.formLayout(parent=left, manage=False)
            cmds.formLayout(left, edit=True, attachForm=[
                (switch, "top", 0), (switch, "left", 0), (switch, "right", 0),
                (editor, "left", 0), (editor, "right", 0), (editor, "bottom", 0),
                (mapf, "left", 0), (mapf, "right", 0), (mapf, "bottom", 0)],
                attachControl=[(editor, "top", 0, switch),
                               (mapf, "top", 0, switch)])
            cmds.setParent(self.pane)
        except Exception:
            self._log_exc("Building the left pane failed; editor only")
            return False
        self.left_form, self.switch_form = left, switch
        self.editor_pane, self.map_form = editor, mapf
        self.editor_parent = editor

        try:
            self.view_switch = ViewSwitch(self)
            if add_widget_to_maya_layout(self.view_switch, switch):
                self._attach_fill(switch, "view switch")
        except Exception:
            self._log_exc("Building the view switch failed")
        return True

    def _mount_map(self):
        available = False
        if self.map_form is not None and MapFactory is not None:
            try:
                self.map_widget = MapFactory()
                if add_widget_to_maya_layout(self.map_widget, self.map_form):
                    self._attach_fill(self.map_form, "cluster map")
                    available = True
                    self._log("OK", "Cluster Map mounted")
            except Exception:
                self._log_exc("Mounting the Cluster Map failed")
        elif MapFactory is None:
            self._log("WARN", "Cluster Map not loaded (M9 missing)")
        self.map_available = available
        if self.view_switch is not None:
            self.view_switch.show_state("editor", available)

    def set_view(self, view):
        if view == "map" and not getattr(self, "map_available", False):
            view = "editor"
        try:
            cmds.layout(self.editor_pane, edit=True, manage=(view == "editor"))
            cmds.layout(self.map_form, edit=True, manage=(view == "map"))
        except Exception:
            self._log_exc("Switching view failed")
            return
        self.view = view
        if self.view_switch is not None:
            self.view_switch.show_state(view, self.map_available)
        if view == "map":
            self.reload_map()
        else:
            self._refresh_editor()

    # -- pair mode ----------------------------------------------------
    def set_pair_mode(self, on):
        """Pair mode = the Cluster Map, with a plain drag making pairs.

        WHY NOT MAYA'S EDITOR
            An overlay on Maya's UV Editor was tried and fails on Maya 2022 /
            Windows: a transparent Qt layer cannot composite over the
            OpenGL viewport, so it painted stale pixels - a ghost copy of the
            tools panel - over the UVs. Per the agreed fallback, pairing
            happens on UV Studio's own map, and only while pair mode is on;
            leaving pair mode returns to the Maya editor.
        """
        on = bool(on)
        if on == self.pair_mode:
            return
        if on and not self.map_available:
            self._say("ERROR", "Pair mode needs the Cluster Map, which did "
                               "not load. See the Log tab.")
            if self.view_switch is not None:
                self.view_switch.pair.setChecked(False)
            return
        self.pair_mode = on
        if self.map_widget is not None:
            self.map_widget.pair_mode = on
        if on:
            if self.view != "map":
                self._view_before_pair = self.view
                self.set_view("map")
            self._say("OK", "Pair mode: drag from one shell to another to "
                            "pair them.")
        else:
            if self._view_before_pair is not None:
                self.set_view(self._view_before_pair)
                self._view_before_pair = None
            self._say("OK", "Pair mode off.")

    def _find_viewport(self):
        """Largest visible drawing child of the hosted panel (diagnostics)."""
        root = maya_layout_to_qwidget(self.panel) if self.panel else None
        if root is None:
            return None
        self._viewport_root = root
        skip = ("ToolBar", "MenuBar", "Button", "LineEdit", "Label",
                "ComboBox", "Slider", "Menu", "ScrollBar", "Frame")
        best, area = None, 0
        try:
            children = root.findChildren(QtWidgets.QWidget)
        except RuntimeError:
            return None
        for child in children:
            try:
                name = child.metaObject().className()
                if any(k in name for k in skip) or not child.isVisible():
                    continue
                a = child.width() * child.height()
            except RuntimeError:
                continue
            if a > area:
                best, area = child, a
        return best

    def set_texture_mode(self, mode):
        if self.map_widget is None:
            return
        try:
            note = self.map_widget.set_texture_mode(mode)
        except Exception:
            import traceback
            self._say("ERROR", "Texture preview failed:\n%s"
                      % traceback.format_exc())
            return
        if note:
            self._say("INFO", note)

    def reload_map(self, force=False):
        """Load the selected meshes into the map. Warns on empty selection.

        Skipped when the map already shows the same meshes: analysing a
        130k-UV mesh on every view switch was the long load when leaving
        pair mode. The Reload button forces it.
        """
        if self.map_widget is None:
            return
        bridge = getattr(self.map_widget, "bridge", None)
        m2 = sys.modules.get("uvstudio_m2_scene_bridge")
        if bridge is None or m2 is None:
            self._log("ERROR", "Cluster Map has no scene bridge")
            return
        try:
            meshes = m2.selected_meshes()
        except Exception:
            meshes = []
        if not meshes:
            self._log("WARN", "Select a mesh, shell or UVs to show it in the "
                              "Cluster Map.")
            return
        key = tuple(sorted(meshes))
        if not force and key == getattr(self, "_map_loaded_for", None):
            return                  # already showing these; Reload forces
        m9 = sys.modules.get("uvstudio_m9_cluster_map")
        progress = (m9.LoadProgress(self.map_widget,
                                    "Loading %d mesh(es)" % len(meshes))
                    if m9 is not None and hasattr(m9, "LoadProgress")
                    else None)
        try:
            self.map_widget.load(bridge.analyse_many(meshes, progress=progress),
                                 m2, progress=progress)
            # Marked loaded only AFTER it worked. Marking first meant one
            # failed load left the map permanently blank - every later
            # switch saw "already loaded" and skipped.
            self._map_loaded_for = key
            units = len(self.map_widget.controller.units)
            self._say("OK", "Cluster Map: %d mesh(es), %d unit(s)"
                      % (len(meshes), units))
        except Exception as exc:
            if type(exc).__name__ == "LoadCancelled":
                self._say("INFO", "Cluster Map load cancelled.")
            else:
                import traceback
                self._say("ERROR", "Loading the Cluster Map failed:\n%s"
                          % traceback.format_exc())
        finally:
            if progress is not None:
                progress.close()

    def _say(self, level, message):
        """Log to the host AND the panel's visible Log tab. The host log
        alone is invisible after startup, which is how the map failed
        silently."""
        self._log(level, message)
        panel = getattr(self, "tools_panel", None)
        if panel is not None and hasattr(panel, "log"):
            try:
                panel.log(level, message)
            except Exception:
                pass

    def _attach_fill(self, form, label):
        try:
            children = cmds.formLayout(form, query=True, childArray=True) or []
            if children:
                c = children[-1]
                cmds.formLayout(form, edit=True, attachForm=[
                    (c, "top", 0), (c, "bottom", 0),
                    (c, "left", 0), (c, "right", 0)])
                return True
        except Exception:
            self._log_exc("Attaching the %s failed" % label)
        return False

    def _stretch_tools_panel(self):
        """Attach the inserted widget to all four edges of the formLayout.

        The child's Maya control name is queried from the form rather than
        derived from the QWidget: after insertion Maya owns it, and childArray
        is the authoritative answer regardless of how the widget was named.
        """
        try:
            children = cmds.formLayout(self.tools_form, query=True,
                                       childArray=True) or []
        except Exception:
            self._log_exc("Querying the tools form children failed")
            return False

        if not children:
            self._log("WARN", "The tools form reports no children; the panel "
                              "will not resize with the window.")
            return False

        child = children[-1]
        try:
            cmds.formLayout(self.tools_form, edit=True, attachForm=[
                (child, "top", 0), (child, "bottom", 0),
                (child, "left", 0), (child, "right", 0)])
            self._log("OK", "Tools panel attached to all four edges (%s)" % child)
            return True
        except Exception:
            self._log_exc("Attaching the tools panel to the form failed")
            return False

    def _close_native_editor_window(self):
        """Close Maya's own UV Editor window if it is open.

        Maya 2022 allows one polyTexturePlacementPanel per session. With the
        native window open it owns that panel, so UV Studio adopts a panel
        that is still parented elsewhere and displays nothing - the mesh is
        loaded, the shell count is right, and the view is blank. Closing the
        native window first is the only way to get the panel cleanly, and
        while UV Studio is open it IS the UV Editor, so nothing is lost.
        """
        closed = []
        try:
            windows = cmds.lsUI(windows=True) or []
        except Exception:
            return closed

        for window in windows:
            if "polyTexturePlacementPanel" not in window \
                    and "textureView" not in window.lower():
                continue
            try:
                if cmds.window(window, exists=True):
                    cmds.deleteUI(window, window=True)
                    closed.append(window)
            except Exception:
                pass

        if closed:
            self._log("WARN", "Closed Maya's own UV Editor window (%s) so the "
                              "session panel could be hosted here. Maya 2022 "
                              "allows only one." % ", ".join(closed))
        return closed

    def _host_uv_editor(self):
        """Put Maya's UV Editor into the left pane. Returns True on success."""
        self._close_native_editor_window()
        panel = None
        try:
            panel = cmds.scriptedPanel(type=PANEL_TYPE, unParent=True,
                                       label="UV Studio Editor")
            self._log("OK", "Created a dedicated panel: %s" % panel)
        except Exception:
            self._log("WARN", "Dedicated panel unavailable (%s); adopting the "
                              "session panel."
                      % traceback.format_exc().strip().splitlines()[-1])

        if panel is None:
            try:
                panels = cmds.getPanel(scriptType=PANEL_TYPE) or []
                if not panels:
                    panels = self._spawn_panel()
                if panels:
                    panel = panels[0]
                    self.adopted = True
                    cmds.scriptedPanel(panel, edit=True, unParent=True)
                    self._log("WARN", "Adopted the session panel %s." % panel)
            except Exception:
                self._log_exc("Adopting the session panel failed")

        if panel is None:
            self._log("ERROR", "No %s available to host." % PANEL_TYPE)
            return False

        self.panel = panel
        try:
            cmds.scriptedPanel(panel, edit=True, parent=self.editor_parent)
            self._log("OK", "UV Editor hosted in %s" % self.pane)
            return True
        except Exception:
            self._log_exc("Parenting the panel into the pane layout failed")
            return False

    def _spawn_panel(self):
        """Open Maya's UV Editor to force the panel into existence."""
        try:
            if hasattr(cmds, "TextureViewWindow"):
                cmds.TextureViewWindow()
            else:
                mel.eval("TextureViewWindow;")
        except Exception:
            self._log_exc("Could not spawn the UV Editor panel")
            return []
        return cmds.getPanel(scriptType=PANEL_TYPE) or []

    def _publish_verdict(self, hosted):
        if hosted:
            headline = "UV EDITOR HOSTED"
            detail = ("Maya %s | %s | Maya owns the window, the split and the "
                      "panel; the tools panel is a Qt child of a Maya layout. "
                      "Close with the window's X or uvstudio.close()."
                      % (self.env.maya_version, self.env.qt_binding))
        else:
            headline = "UV EDITOR NOT HOSTED"
            detail = ("Maya %s | %s | The tools panel is live but the editor "
                      "could not be hosted. See the Host log tab."
                      % (self.env.maya_version, self.env.qt_binding))
        self.tools_panel.set_verdict(hosted, headline, detail)
        self.tools_panel.set_log(self.log)

    # -- teardown ----------------------------------------------------
    def destroy(self):
        """Return the panel to Maya, then delete the control Maya owns.

        Order matters. The panel must be unparented BEFORE the control is
        deleted, or Maya deletes the session's only UV Editor along with it.
        Nothing Qt-side is deleted here at all - the tools panel dies as a
        child of the Maya layout it was inserted into.
        """
        try:
            if getattr(self, "pair_mode", False):
                self.set_pair_mode(False)
        except Exception:
            pass
        try:
            if self.panel and cmds.scriptedPanel(self.panel, exists=True):
                cmds.scriptedPanel(self.panel, edit=True, unParent=True)
        except Exception:
            cmds.warning("UV Studio: returning the panel to Maya failed:\n%s"
                         % traceback.format_exc())

        try:
            if self.control and cmds.workspaceControl(self.control, exists=True):
                cmds.deleteUI(self.control)
        except Exception:
            cmds.warning("UV Studio: deleting the workspace control failed:\n%s"
                         % traceback.format_exc())

        self.control = self.pane = self.panel = None
        self.tools_form = self.tools_panel = None
        self.view_switch = self.map_widget = None

    @staticmethod
    def _destroy_existing():
        try:
            if cmds.workspaceControl(CONTROL_NAME, exists=True):
                cmds.deleteUI(CONTROL_NAME)
        except Exception:
            pass


# =====================================================================
# SECTION 6 - Entry points
# =====================================================================

_HOST = None


def _looks_headless():
    """True only when there is genuinely no GUI to build into.

    `cmds.about(batch=True)` alone was not enough. It reported batch inside a
    running interactive Maya - it can during early startup, from a deferred
    evaluation, and in some embedded contexts - and show() then refused to
    open with a message about batch mode on a machine plainly showing the
    Maya UI.

    So the batch flag is now one of two signals, and it only wins if the
    other agrees. A live QApplication is positive proof of a GUI: Maya
    cannot have made one without a UI to put it in. Requiring both to say
    headless means the failure mode is "tries to build a window in batch and
    raises a Qt error", which is loud and diagnosable, rather than "refuses
    to open and blames batch mode", which is neither.
    """
    try:
        batch = bool(cmds.about(batch=True))
    except Exception:
        batch = False
    if not batch:
        return False, ""

    try:
        if QtWidgets is not None and QtWidgets.QApplication.instance():
            return False, ""            # Qt disagrees; trust Qt
    except Exception:
        pass
    return True, "cmds.about(batch=True) is True and no QApplication exists"


def show():
    """Open UV Studio. Safe to call repeatedly."""
    if _QT_IMPORT_ERROR is not None:
        raise RuntimeError("UV Studio needs PySide2 or PySide6: %s"
                           % _QT_IMPORT_ERROR)
    headless, why = _looks_headless()
    if headless:
        raise RuntimeError(
            "UV Studio needs an interactive Maya: %s. "
            "Use run_headless_probe() for the environment and command report "
            "without a UI." % why)

    global _HOST
    close()
    _HOST = UVStudioHost()
    if not _HOST.build():
        # A warning scrolls past and show() returned a host anyway, so a
        # failed build looked exactly like a successful one that happened to
        # open nothing. Raising puts the log where it cannot be missed.
        raise RuntimeError(
            "UV Studio could not build its window. What happened:\n  %s"
            % "\n  ".join("[%s] %s" % entry for entry in _HOST.log))
    return _HOST


def close():
    """Tear down cleanly. Safe when nothing is open."""
    global _HOST
    if _HOST is not None:
        try:
            _HOST.destroy()
        except Exception:
            cmds.warning("UV Studio: teardown raised:\n%s"
                         % traceback.format_exc())
        _HOST = None
    else:
        UVStudioHost._destroy_existing()


def run_headless_probe():
    """Environment and command probe with no UI. Safe in mayapy/batch."""
    env = Environment()
    resolved = resolve_commands(env)
    ok, total = resolution_summary(resolved)
    return OrderedDict([
        ("module", MODULE_ID),
        ("environment", env.as_dict()),
        ("commands_resolved", "%d/%d" % (ok, total)),
        ("missing", [r["label"] for rows in resolved.values()
                     for r in rows if not r["resolved"]]),
    ])


if __name__ == "__main__":
    show()
