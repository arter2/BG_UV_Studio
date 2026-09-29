"""
UV Studio - Module 5c: Panel Widgets
====================================

PURPOSE
    The tools panel M1 hosts. Collapsible, reorderable sections; one row per
    tool; the row carries the tool's own controls.

LAYOUT (the "B" layout, chosen over a Maya UV Toolkit recreation)
    Tab
      Pivot strip          one per tab, owns the pivot for every transform in
                           it; Maya repeats pivot state inside each tool and
                           lets the copies disagree
      Section  [header ^ v]
        [ Tool ][ field ][ field ]        <- the button and its values on one
        [ Tool ][ field ]                    row, so "what it does" and "what
        [ Tool ]                             it does it with" read together

    Left-click runs the tool on its current values. Right-click opens the
    settings sheet, which holds the rest.

TWO ROUTES, ONE STORED VALUE
    An inline field and its entry in the settings sheet are two controls over
    the SAME key in the SAME prefs entry. The panel never keeps a second copy:
    a widget writes straight through to prefs.remember on edit, and every
    other view of that key is refreshed from prefs afterwards. Two ways to
    reach a setting is a feature; two settings behind one label is a bug you
    lose an afternoon to.

WHAT THE PANEL DOES NOT KNOW
    Which fields a tool has, what they are called, what type they are, or
    which tools exist. All of that is declared in M5's registry. Adding a tool
    means adding a row to that table, never editing this file - which is the
    same reason section ORDER is data rather than code.

Target: Maya 2022 - 2025+ (PySide2 and PySide6)
"""

from __future__ import annotations

import ast
import logging
import sys
import traceback
from collections import OrderedDict

__version__ = "4.13.0"
MODULE_ID = "M5c"

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


# @include _shared/qt_enum.py


# Without PySide the widget classes below still need a base to be DEFINED,
# so the pure value-parsing helpers in Section 1 can be imported and tested
# on any Python. They are not usable without Qt; nothing in this module tries
# to be. The bundle treats a Qt-less load as a failed module and says so,
# which is the honest outcome - a panel that cannot draw is not a panel.
_Widget = QtWidgets.QWidget if QtWidgets is not None else object
_Dialog = QtWidgets.QDialog if QtWidgets is not None else object

MUTED = "#8f8f8f"
OK_COLOUR = "#2d6a4f"
WARN_COLOUR = "#b8860b"
FAIL_COLOUR = "#c0392b"

ROW_HEIGHT = 26
BUTTON_WIDTH = 96

# --- palette, matched to Maya 2022's UV Toolkit -----------------------
# Maya's toolkit is not flat: controls sit a shade lighter than the panel,
# fields a shade darker, and the one accent is the teal used for an active
# axis toggle. Hard-coding these rather than reading the Maya palette keeps
# the panel identical across the 2022-2025 skins, which drift year to year.
COL = {
    "panel":    "#3a3a3a",
    "section":  "#333333",
    "control":  "#5a5a5a",
    "control_hi": "#666666",
    "control_lo": "#4c4c4c",
    "field":    "#2b2b2b",
    "field_bd": "#232323",
    "text":     "#d8d8d8",
    "muted":    "#8f8f8f",
    "border":   "#2a2a2a",
    "accent":   "#5285a6",   # Maya UV toolkit teal-blue
    "accent_lo": "#456f8a",
}

PANEL_QSS = """
QWidget { color: %(text)s; font-size: 11px; }
QLabel { color: %(muted)s; background: transparent; }
QPushButton {
    background: %(control)s; border: 1px solid %(control_lo)s;
    border-radius: 2px; color: %(text)s; padding: 2px 8px;
}
QPushButton:hover { background: %(control_hi)s; }
QPushButton:pressed { background: %(control_lo)s; }
QPushButton:checked { background: %(accent)s; border-color: %(accent_lo)s; }
QPushButton:disabled { color: #6a6a6a; background: %(control_lo)s; }
QToolButton { background: transparent; border: none; color: %(muted)s; }
QToolButton:hover { color: %(text)s; }
QLineEdit, QDoubleSpinBox, QSpinBox {
    background: %(field)s; border: 1px solid %(field_bd)s;
    border-radius: 2px; color: %(text)s; padding: 1px 4px;
    selection-background-color: %(accent)s;
}
QLineEdit:disabled, QDoubleSpinBox:disabled, QSpinBox:disabled {
    color: #6a6a6a; background: #333333;
}
QComboBox {
    background: %(control)s; border: 1px solid %(control_lo)s;
    border-radius: 2px; color: %(text)s; padding: 1px 6px;
}
QComboBox::drop-down { border: none; width: 14px; }
QComboBox QAbstractItemView {
    background: %(section)s; color: %(text)s;
    selection-background-color: %(accent)s;
}
QCheckBox { color: %(text)s; spacing: 5px; }
QCheckBox::indicator, QRadioButton::indicator {
    width: 12px; height: 12px;
    border: 1px solid #7a7a7a; background: %(field)s;
}
QCheckBox::indicator { border-radius: 2px; }
QRadioButton::indicator { border-radius: 7px; }
QCheckBox::indicator:checked {
    background: %(accent)s; border-color: %(accent_lo)s;
}
QRadioButton::indicator:checked {
    background: %(accent)s; border-color: %(accent_lo)s;
}
QScrollArea { border: none; background: %(panel)s; }
QTreeWidget, QPlainTextEdit, QTextBrowser {
    background: %(field)s; border: 1px solid %(border)s; color: %(text)s;
}
""" % COL


def _draw_layout_glyph(p, kind, size, colour):
    """Align and distribute glyphs: grey bars against a blue guide line.

    The guide is the edge or centre line the shells snap to, drawn in the
    panel accent so the eye reads WHERE they go before WHAT moves.
    """
    fill = QtGui.QColor(colour)
    guide = QtGui.QPen(QtGui.QColor(COL["accent"]))
    guide.setWidthF(1.6)
    s = float(size)
    lo, hi, mid = s * 0.14, s * 0.86, s * 0.5
    long_, short, th = s * 0.56, s * 0.34, s * 0.20

    def bar(x, y, w, h):
        p.fillRect(QtCore.QRectF(x, y, w, h), fill)

    def vline(x):
        p.setPen(guide)
        p.drawLine(QtCore.QPointF(x, lo), QtCore.QPointF(x, hi))

    def hline(y):
        p.setPen(guide)
        p.drawLine(QtCore.QPointF(lo, y), QtCore.QPointF(hi, y))

    y1, y2 = s * 0.24, s * 0.56
    x1, x2 = s * 0.24, s * 0.56
    if kind == "align_left":
        bar(lo + 1.5, y1, long_, th); bar(lo + 1.5, y2, short, th); vline(lo)
    elif kind == "align_right":
        bar(hi - 1.5 - long_, y1, long_, th)
        bar(hi - 1.5 - short, y2, short, th); vline(hi)
    elif kind == "align_top":
        bar(x1, lo + 1.5, th, long_); bar(x2, lo + 1.5, th, short); hline(lo)
    elif kind == "align_bottom":
        bar(x1, hi - 1.5 - long_, th, long_)
        bar(x2, hi - 1.5 - short, th, short); hline(hi)
    elif kind == "align_centre_u":
        bar(mid - long_ / 2, y1, long_, th)
        bar(mid - short / 2, y2, short, th); vline(mid)
    elif kind == "align_centre_v":
        bar(x1, mid - long_ / 2, th, long_)
        bar(x2, mid - short / 2, th, short); hline(mid)
    elif kind == "distribute_u":
        for x in (lo + 1, mid - th / 2, hi - 1 - th):
            bar(x, s * 0.26, th, s * 0.48)
        hline(hi)
    elif kind == "distribute_v":
        for y in (lo + 1, mid - th / 2, hi - 1 - th):
            bar(s * 0.26, y, s * 0.48, th)
        vline(lo)


_SECTION_GLYPH_KINDS = ("project", "cut", "unfold", "grid", "pin", "select",
                        "orient", "link", "checker", "check", "move")


def _draw_section_glyph(p, kind, size, colour):
    """Simple drawn icons, one per kind of tool, used when Maya has none."""
    s = float(size)
    c = s * 0.5
    col = QtGui.QColor(colour)
    accent = QtGui.QColor(COL["accent"])
    pen = QtGui.QPen(col)
    pen.setWidthF(1.3)
    p.setPen(pen)
    P = QtCore.QPointF
    R = QtCore.QRectF
    if kind == "project":            # a face and the light hitting it
        p.drawRect(R(s * .15, s * .45, s * .5, s * .4))
        p.setPen(QtGui.QPen(accent, 1.3))
        for x in (.55, .75, .95):
            p.drawLine(P(s * x, s * .1), P(s * (x - .3), s * .42))
    elif kind == "cut":              # a seam with a gap
        p.drawLine(P(s * .15, s * .2), P(s * .45, s * .8))
        p.drawLine(P(s * .55, s * .2), P(s * .85, s * .8))
        p.setPen(QtGui.QPen(accent, 1.3, _enum(QtCore.Qt, "PenStyle.DashLine",
                                                "DashLine")))
        p.drawLine(P(c, s * .1), P(c, s * .9))
    elif kind == "unfold":           # a folded sheet opening flat
        p.drawPolyline(QtGui.QPolygonF([P(s * .1, s * .7), P(s * .35, s * .35),
                                        P(s * .6, s * .7), P(s * .9, s * .7)]))
        p.setPen(QtGui.QPen(accent, 1.3))
        p.drawLine(P(s * .1, s * .85), P(s * .9, s * .85))
    elif kind == "grid":             # packed boxes
        for x, y, w, h in ((.12, .12, .45, .35), (.62, .12, .26, .35),
                           (.12, .52, .3, .36), (.47, .52, .41, .36)):
            p.drawRect(R(s * x, s * y, s * w, s * h))
    elif kind == "pin":
        p.drawEllipse(R(s * .32, s * .1, s * .36, s * .36))
        p.drawLine(P(c, s * .46), P(c, s * .9))
        p.setBrush(accent)
        p.drawEllipse(R(s * .41, s * .19, s * .18, s * .18))
    elif kind == "select":           # a pointer
        p.drawPolygon(QtGui.QPolygonF([P(s * .25, s * .12), P(s * .25, s * .8),
                                       P(s * .42, s * .62), P(s * .55, s * .88),
                                       P(s * .64, s * .82), P(s * .52, s * .58),
                                       P(s * .75, s * .55)]))
    elif kind == "orient":           # a tilted bar straightening
        p.drawLine(P(s * .15, s * .75), P(s * .8, s * .3))
        p.setPen(QtGui.QPen(accent, 1.3))
        p.drawLine(P(s * .12, s * .85), P(s * .88, s * .85))
    elif kind == "link":             # two shells joined
        p.drawRect(R(s * .08, s * .3, s * .32, s * .4))
        p.drawRect(R(s * .6, s * .3, s * .32, s * .4))
        p.setPen(QtGui.QPen(accent, 1.5))
        p.drawLine(P(s * .4, c), P(s * .6, c))
    elif kind == "checker":
        for i in range(3):
            for j in range(3):
                if (i + j) % 2 == 0:
                    p.fillRect(R(s * (.14 + i * .24), s * (.14 + j * .24),
                                 s * .24, s * .24), col)
    elif kind == "check":
        p.setPen(QtGui.QPen(accent, 2.0))
        p.drawPolyline(QtGui.QPolygonF([P(s * .18, s * .52), P(s * .4, s * .75),
                                        P(s * .82, s * .25)]))
    elif kind == "move":             # four-way arrows
        p.drawLine(P(c, s * .12), P(c, s * .88))
        p.drawLine(P(s * .12, c), P(s * .88, c))
        for a, b, d in (((c, s * .12), (c - 3, s * .12 + 3), (c + 3, s * .12 + 3)),
                        ((c, s * .88), (c - 3, s * .88 - 3), (c + 3, s * .88 - 3)),
                        ((s * .12, c), (s * .12 + 3, c - 3), (s * .12 + 3, c + 3)),
                        ((s * .88, c), (s * .88 - 3, c - 3), (s * .88 - 3, c + 3))):
            p.drawLine(P(*a), P(*b))
            p.drawLine(P(*a), P(*d))


_MAYA_ICON_INDEX = []


def maya_icon_names():
    """Every icon in the running Maya's Qt resources, listed once.

    Maya ships its UI icons as ':/name.png' resources. Searching the real
    list is what makes icons correct on any Maya version: file names drift
    between releases, and guessed names fail silently as blank buttons.
    """
    if _MAYA_ICON_INDEX:
        return _MAYA_ICON_INDEX[0]
    names = []
    try:
        flag = _enum(QtCore.QDirIterator, "IteratorFlag.Subdirectories",
                     "Subdirectories")
        it = QtCore.QDirIterator(":", flag)
        while it.hasNext():
            path = it.next()
            if path.lower().endswith((".png", ".svg")):
                names.append(path)
    except Exception:
        names = []
    _MAYA_ICON_INDEX.append(names)
    return names


def find_maya_icon(groups):
    """The best Maya icon matching any keyword group, or None.

    A name matches a group when it contains every keyword (case-folded).
    Among matches, 'poly' UV-editor icons and shorter names win - the plain
    tool icon rather than a variant like '..._hover' or '..._200'.
    """
    names = maya_icon_names()
    for group in groups or []:
        hits = [n for n in names
                if all(k in n.lower().rsplit("/", 1)[-1] for k in group)]
        if not hits:
            continue
        hits.sort(key=lambda n: (0 if "poly" in n.lower() else 1,
                                 any(t in n.lower() for t in
                                     ("hover", "pressed", "disabled", "_200",
                                      "_150")),
                                 len(n)))
        return hits[0]
    return None


def _icon(kind, size=16, colour="#d8d8d8"):
    """A crisp vector glyph for a tool button. Drawn, not fonted, so it is
    identical on every platform and does not depend on an icon font being
    installed. Returns a QIcon, or None off Qt."""
    if QtGui is None or not hasattr(QtGui, "QPixmap"):
        return None
    pix = QtGui.QPixmap(size, size)
    pix.fill(_enum(QtCore.Qt, "GlobalColor.transparent", "transparent"))
    p = QtGui.QPainter(pix)
    p.setRenderHint(_enum(QtGui.QPainter, "RenderHint.Antialiasing",
                          "Antialiasing"))
    pen = QtGui.QPen(QtGui.QColor(colour))
    pen.setWidthF(1.4)
    pen.setCapStyle(_enum(QtCore.Qt, "PenCapStyle.RoundCap", "RoundCap"))
    p.setPen(pen)
    c = size / 2.0
    if kind in ("rot_ccw", "rot_cw"):
        r = size * 0.30
        rect = QtCore.QRectF(c - r, c - r, r * 2, r * 2)
        # 270-degree arc, arrowhead on the open end
        if kind == "rot_ccw":
            p.drawArc(rect, 60 * 16, 250 * 16)
            ax, ay = c + r * 0.5, c - r * 0.86
            p.drawLine(QtCore.QPointF(ax, ay),
                       QtCore.QPointF(ax - 4, ay - 1))
            p.drawLine(QtCore.QPointF(ax, ay),
                       QtCore.QPointF(ax + 1, ay - 4))
        else:
            p.drawArc(rect, 240 * 16, -250 * 16)
            ax, ay = c - r * 0.5, c - r * 0.86
            p.drawLine(QtCore.QPointF(ax, ay),
                       QtCore.QPointF(ax + 4, ay - 1))
            p.drawLine(QtCore.QPointF(ax, ay),
                       QtCore.QPointF(ax - 1, ay - 4))
    elif kind == "scale":
        m = size * 0.22
        p.drawRect(QtCore.QRectF(m, m, size - 2 * m, size - 2 * m))
        p.fillRect(QtCore.QRectF(c, c, size - m - c, size - m - c),
                   QtGui.QColor(colour))
    elif kind in ("left", "right", "up", "down"):
        d = size * 0.24
        pts = {
            "left":  [(c + d, c - d), (c - d, c), (c + d, c + d)],
            "right": [(c - d, c - d), (c + d, c), (c - d, c + d)],
            "up":    [(c - d, c + d), (c, c - d), (c + d, c + d)],
            "down":  [(c - d, c - d), (c, c + d), (c + d, c - d)],
        }[kind]
        path = QtGui.QPainterPath()
        path.moveTo(*pts[0])
        for x, y in pts[1:]:
            path.lineTo(x, y)
        p.drawPath(path)
    elif kind == "optionbox":
        # Maya's option-box mark: a small square with a dividing tick, the
        # long-standing "this tool has options" affordance every Maya artist
        # already reads without a tooltip.
        m = size * 0.24
        p.drawRect(QtCore.QRectF(m, m, size - 2 * m, size - 2 * m))
        p.drawLine(QtCore.QPointF(m, c), QtCore.QPointF(size - m, c))
    elif kind.startswith("align_") or kind.startswith("distribute_"):
        _draw_layout_glyph(p, kind, size, colour)
    elif kind in _SECTION_GLYPH_KINDS:
        _draw_section_glyph(p, kind, size, colour)
    elif kind == "flip":
        m2 = size * 0.18
        p.drawLine(QtCore.QPointF(c, m2),
                   QtCore.QPointF(c, size - m2))
        p.setPen(_enum(QtCore.Qt, "PenStyle.NoPen", "NoPen"))
        p.setBrush(QtGui.QColor(colour))
        tri = QtGui.QPolygonF([QtCore.QPointF(c - 2, c - 4),
                               QtCore.QPointF(c - 7, c),
                               QtCore.QPointF(c - 2, c + 4)])
        p.drawPolygon(tri)
    p.end()
    return QtGui.QIcon(pix)


# Qt.UserRole, resolved once. The step id is carried on the item rather than
# looked up by row, so reordering cannot mis-address a step.
_ROLE_ID = (_enum(QtCore.Qt, "ItemDataRole.UserRole", "UserRole")
            if QtCore is not None else 32)


# =====================================================================
# SECTION 1 - Value parsing
# =====================================================================

def parse_tiles(text, fallback=None):
    """"1001-1004, 1011" -> [1001, 1002, 1003, 1004, 1011].

    Artists write tile ranges the way they say them out loud. Anything
    unparseable returns the fallback rather than an empty list, because an
    empty tile list silently means "pack into 1001" and that is a wrong
    answer delivered confidently.
    """
    tiles = []
    for chunk in str(text).replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk[1:]:
            lo, _, hi = chunk.partition("-")
            try:
                lo, hi = int(lo), int(hi)
            except ValueError:
                return fallback if fallback is not None else []
            if hi < lo or hi - lo > 63:     # 64 tiles is the confirmed ceiling
                return fallback if fallback is not None else []
            tiles.extend(range(lo, hi + 1))
            continue
        try:
            tiles.append(int(chunk))
        except ValueError:
            return fallback if fallback is not None else []
    return tiles or (fallback if fallback is not None else [])


def format_tiles(values):
    """The inverse, collapsing runs back into ranges."""
    numbers = sorted(set(int(v) for v in (values or [])))
    if not numbers:
        return ""
    parts = []
    start = previous = numbers[0]
    for value in numbers[1:]:
        if value == previous + 1:
            previous = value
            continue
        parts.append(str(start) if start == previous
                     else "%d-%d" % (start, previous))
        start = previous = value
    parts.append(str(start) if start == previous
                 else "%d-%d" % (start, previous))
    return ", ".join(parts)


def parse_literal(text, kind):
    """Read free text back into a value.

    ast.literal_eval, never eval. The old dialog used eval on every text
    field, which made a settings box a place to run arbitrary Python - fine
    on a personal tool right up until the prefs file comes off a shared
    drive with somebody else's project settings in it.
    """
    text = str(text).strip()
    if kind == "text" or not text:
        return text
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text


# =====================================================================
# SECTION 2 - One control for one Field
# =====================================================================

class FieldWidget(_Widget):
    """The widgets for one registry Field, and the values they hold.

    Built from the Field's declared kind rather than from the type of
    whatever happens to be stored, so a setting whose value is currently
    None still gets the right editor instead of degrading to a text box.
    """

    def __init__(self, field, values, on_change, parent=None,
                 show_label=True):
        super(FieldWidget, self).__init__(parent)
        self.field = field
        self._on_change = on_change
        self._editors = OrderedDict()
        self._nudge_buttons = []
        self._building = True

        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)

        # The option window draws its own right-aligned caption, so it asks
        # for the field without one - otherwise every label appeared twice.
        if (show_label and field.label
                and field.kind not in ("bool", "axes", "nudge")):
            caption = QtWidgets.QLabel(field.label)
            caption.setStyleSheet("color: %s;" % MUTED)
            row.addWidget(caption)

        builder = getattr(self, "_build_%s" % field.kind, self._build_text)
        builder(row, values)

        if field.unit:
            unit = QtWidgets.QLabel(field.unit)
            unit.setStyleSheet("color: %s;" % MUTED)
            row.addWidget(unit)

        if field.tooltip:
            self.setToolTip(field.tooltip)
        self._building = False

    # -- builders -------------------------------------------------------
    def _build_float(self, row, values):
        box = QtWidgets.QDoubleSpinBox()
        box.setDecimals(self.field.decimals)
        low = -1.0e6 if self.field.minimum is None else self.field.minimum
        high = 1.0e6 if self.field.maximum is None else self.field.maximum
        if self.field.optional:
            # Qt shows specialValueText AT the minimum, so the minimum is
            # spent representing None and the usable range starts one step
            # above it. Without this an optional setting reads back as 0.0,
            # which for Distribute is a different operation, not a default.
            low = low - (self.field.step or 0.1)
            box.setSpecialValueText("auto")
        box.setRange(low, high)
        box.setSingleStep(self.field.step or 0.1)
        box.setButtonSymbols(
            _enum(QtWidgets.QAbstractSpinBox, "ButtonSymbols.NoButtons",
                  "NoButtons"))
        value = values.get(self.field.key)
        if value is None:
            box.setValue(box.minimum() if self.field.optional else 0.0)
        else:
            box.setValue(float(value))
        box.setFixedWidth(self.field.width or 72)
        box.setFixedHeight(ROW_HEIGHT)
        box.valueChanged.connect(self._changed)
        self._editors[self.field.key] = box
        row.addWidget(box)

    def _build_int(self, row, values):
        box = QtWidgets.QSpinBox()
        box.setRange(int(self.field.minimum if self.field.minimum is not None
                         else -1000000),
                     int(self.field.maximum if self.field.maximum is not None
                         else 1000000))
        box.setButtonSymbols(
            _enum(QtWidgets.QAbstractSpinBox, "ButtonSymbols.NoButtons",
                  "NoButtons"))
        if self.field.optional:
            box.setMinimum(box.minimum() - 1)
            box.setSpecialValueText("auto")
        value = values.get(self.field.key)
        if value is None:
            box.setValue(box.minimum() if self.field.optional else 0)
        else:
            box.setValue(int(value))
        box.setFixedWidth(self.field.width or 72)
        box.setFixedHeight(ROW_HEIGHT)
        box.valueChanged.connect(self._changed)
        self._editors[self.field.key] = box
        row.addWidget(box)

    def _build_text(self, row, values):
        edit = QtWidgets.QLineEdit(str(values.get(self.field.key, "") or ""))
        edit.setFixedHeight(ROW_HEIGHT)
        if self.field.width:
            edit.setFixedWidth(self.field.width)
        edit.editingFinished.connect(self._changed)
        self._editors[self.field.key] = edit
        row.addWidget(edit, 1)

    def _build_bool(self, row, values):
        box = QtWidgets.QCheckBox(self.field.label or self.field.key)
        box.setChecked(bool(values.get(self.field.key)))
        box.toggled.connect(self._changed)
        self._editors[self.field.key] = box
        row.addWidget(box)

    def _build_enum(self, row, values):
        combo = QtWidgets.QComboBox()
        combo.addItems([str(o) for o in self.field.options])
        current = str(values.get(self.field.key, ""))
        index = combo.findText(current)
        if index >= 0:
            combo.setCurrentIndex(index)
        combo.setFixedHeight(ROW_HEIGHT)
        if self.field.width:
            combo.setFixedWidth(self.field.width)
        combo.currentIndexChanged.connect(self._changed)
        self._editors[self.field.key] = combo
        row.addWidget(combo)

    TRISTATE = (("Maya default", None), ("On", True), ("Off", False))

    def _build_tristate(self, row, values):
        """Maya default / On / Off.

        A plain checkbox cannot say "leave it to Maya": it is always on or
        off, so exposing one would override Maya's default the moment the
        option window opened. The third state is what makes exposing an
        option safe.
        """
        combo = QtWidgets.QComboBox()
        for text, _value in self.TRISTATE:
            combo.addItem(text)
        current = values.get(self.field.key)
        for index, (_text, value) in enumerate(self.TRISTATE):
            if value is current or (value is not None and value == current):
                combo.setCurrentIndex(index)
                break
        combo.setFixedHeight(ROW_HEIGHT)
        combo.setFixedWidth(self.field.width or 110)
        combo.currentIndexChanged.connect(self._changed)
        self._editors[self.field.key] = combo
        row.addWidget(combo)

    def _build_axes(self, row, values):
        for key, label in (("axis_u", "U"), ("axis_v", "V")):
            button = QtWidgets.QPushButton(label)
            button.setCheckable(True)
            button.setChecked(bool(values.get(key, True)))
            button.setFixedSize(30, ROW_HEIGHT)
            button.toggled.connect(self._changed)
            self._editors[key] = button
            row.addWidget(button)

    def _build_tiles(self, row, values):
        edit = QtWidgets.QLineEdit(format_tiles(values.get(self.field.key)))
        edit.setPlaceholderText("1001-1004")
        edit.setFixedHeight(ROW_HEIGHT)
        if self.field.width:
            edit.setFixedWidth(self.field.width)
        edit.editingFinished.connect(self._changed)
        self._editors[self.field.key] = edit
        row.addWidget(edit, 1)

    def _build_snap(self, row, values):
        """Reference shows 'Step Snap: [Off/On] [step]'. A short label, a
        toggle sized to its text, and the increment beside it."""
        enabled_key = "%s_enabled" % self.field.key
        step_key = "%s_step" % self.field.key

        cap = QtWidgets.QLabel("Step Snap:")
        cap.setStyleSheet("color: %s;" % MUTED)
        row.addWidget(cap)

        toggle = QtWidgets.QPushButton("Off")
        toggle.setCheckable(True)
        toggle.setChecked(bool(values.get(enabled_key)))
        toggle.setText("On" if toggle.isChecked() else "Off")
        toggle.setFixedSize(34, ROW_HEIGHT)
        toggle.setToolTip("Round the value to the increment beside this.")
        toggle.toggled.connect(
            lambda on, b=toggle: (b.setText("On" if on else "Off"),
                                  self._changed()))
        self._editors[enabled_key] = toggle
        row.addWidget(toggle)

        box = QtWidgets.QDoubleSpinBox()
        box.setDecimals(4)
        box.setRange(0.0, 1.0e6)
        box.setButtonSymbols(
            _enum(QtWidgets.QAbstractSpinBox, "ButtonSymbols.NoButtons",
                  "NoButtons"))
        box.setValue(float(values.get(step_key) or self.field.step or 0.0))
        box.setFixedWidth(56)
        box.setFixedHeight(ROW_HEIGHT)
        box.setEnabled(toggle.isChecked())
        toggle.toggled.connect(box.setEnabled)
        box.valueChanged.connect(self._changed)
        self._editors[step_key] = box
        row.addWidget(box)

    def _build_nudge(self, row, values):
        """A tight cross of icon arrows that run the tool named by `runs`.

        They deliberately do not write into this tool's U/V boxes: an artist
        who typed an exact offset and then tapped an arrow should still have
        the offset they typed.
        """
        for icon, axis, direction, tip in (
                ("left", "u", -1, "Nudge \u2212U"),
                ("right", "u", 1, "Nudge +U"),
                ("down", "v", -1, "Nudge \u2212V"),
                ("up", "v", 1, "Nudge +V")):
            button = QtWidgets.QPushButton()
            ico = _icon(icon)
            if ico is not None:
                button.setIcon(ico)
            button.setFixedSize(22, ROW_HEIGHT)
            button.setToolTip(tip)
            button.setProperty("nudge", (self.field.runs or "nudge",
                                         axis, direction))
            self._nudge_buttons.append(button)
            row.addWidget(button)

    # -- values ---------------------------------------------------------
    def _changed(self, *_):
        if not self._building and self._on_change:
            self._on_change()

    def values(self):
        """{settings key: value} for everything this control owns."""
        out = {}
        for key, editor in self._editors.items():
            if isinstance(editor, QtWidgets.QCheckBox):
                out[key] = editor.isChecked()
            elif isinstance(editor, QtWidgets.QPushButton):
                out[key] = editor.isChecked()
            elif isinstance(editor, (QtWidgets.QSpinBox,
                                     QtWidgets.QDoubleSpinBox)):
                out[key] = (None if (self.field.optional
                                     and editor.value() == editor.minimum())
                            else editor.value())
            elif (isinstance(editor, QtWidgets.QComboBox)
                  and self.field.kind == "tristate"):
                out[key] = self.TRISTATE[max(0, editor.currentIndex())][1]
            elif isinstance(editor, QtWidgets.QComboBox):
                out[key] = editor.currentText()
            elif self.field.kind == "tiles":
                out[key] = parse_tiles(editor.text(), fallback=[1001])
            else:
                out[key] = parse_literal(editor.text(), self.field.kind)
        return out

    def nudge_buttons(self):
        return self._nudge_buttons

    def reset_to(self, values):
        """Push new values into this control's editors, e.g. after Reset.

        Sets each editor to the given value without firing on_change per
        keystroke - the caller (Reset) has already written prefs, and a
        cascade of change callbacks here would just re-write them.
        """
        self._building = True
        try:
            for key, editor in self._editors.items():
                v = values.get(key)
                if isinstance(editor, QtWidgets.QCheckBox):
                    editor.setChecked(bool(v))
                elif isinstance(editor, QtWidgets.QPushButton):
                    if editor.isCheckable():
                        editor.setChecked(bool(v))
                elif isinstance(editor, (QtWidgets.QSpinBox,
                                         QtWidgets.QDoubleSpinBox)):
                    if v is None and self.field.optional:
                        editor.setValue(editor.minimum())
                    else:
                        editor.setValue(v or 0)
                elif (isinstance(editor, QtWidgets.QComboBox)
                      and self.field.kind == "tristate"):
                    for i, (_t, value) in enumerate(self.TRISTATE):
                        if value is v or (value is not None and value == v):
                            editor.setCurrentIndex(i)
                            break
                elif isinstance(editor, QtWidgets.QComboBox):
                    idx = editor.findText(str(v))
                    if idx >= 0:
                        editor.setCurrentIndex(idx)
                elif self.field.kind == "tiles":
                    editor.setText(format_tiles(v))
                else:
                    editor.setText("" if v is None else str(v))
        finally:
            self._building = False


# =====================================================================
# SECTION 3 - Pivot strip
# =====================================================================

class PivotEditDialog(_Dialog):
    """Edit Pivot: a small UV view where the artist clicks to place a custom
    pivot, snapping to the selection's handle points. Backed by M9's pure
    PivotPlacement, so the placement rules are tested; this is only paint and
    mouse plumbing.
    """

    def __init__(self, bounds, start_uv, parent=None):
        super(PivotEditDialog, self).__init__(parent)
        self.setWindowTitle("Edit Pivot")
        self.setModal(True)
        m9 = sys.modules.get("uvstudio_m9_cluster_map")
        self.placement = m9.PivotPlacement(bounds=bounds) if m9 else None
        if self.placement and start_uv:
            self.placement.u, self.placement.v = start_uv

        outer = QtWidgets.QVBoxLayout(self)
        hint = QtWidgets.QLabel("Click to place the pivot. It snaps to shell "
                                "corners, edges and centre.")
        hint.setStyleSheet("color: %s;" % MUTED)
        hint.setWordWrap(True)
        outer.addWidget(hint)

        self.canvas = _PivotCanvas(self.placement)
        self.canvas.setMinimumSize(240, 240)
        outer.addWidget(self.canvas, 1)

        self.readout = QtWidgets.QLabel("")
        self.readout.setStyleSheet("color: %s; font-family: monospace;" % MUTED)
        outer.addWidget(self.readout)
        self.canvas.moved = self._update_readout
        self._update_readout()

        buttons = QtWidgets.QDialogButtonBox(
            _enum(QtWidgets.QDialogButtonBox, "StandardButton.Ok", "Ok")
            | _enum(QtWidgets.QDialogButtonBox, "StandardButton.Cancel",
                    "Cancel"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _update_readout(self):
        if self.placement:
            u, v = self.placement.point()
            self.readout.setText("U %.4f   V %.4f" % (u, v))

    def result_uv(self):
        return self.placement.point() if self.placement else (0.5, 0.5)


class _PivotCanvas(_Widget):
    """The clickable UV view inside Edit Pivot."""

    def __init__(self, placement, parent=None):
        super(_PivotCanvas, self).__init__(parent)
        self.placement = placement
        self.moved = None
        self.setMouseTracking(True)

    def resizeEvent(self, event):
        if self.placement:
            self.placement.view.width = float(self.width())
            self.placement.view.height = float(self.height())
            if self.placement.bounds:
                self.placement.view.fit(self.placement.bounds, margin=0.25)
        super(_PivotCanvas, self).resizeEvent(event)

    def _pos(self, event):
        try:
            p = event.position(); return p.x(), p.y()
        except AttributeError:
            return float(event.x()), float(event.y())

    def mousePressEvent(self, event):
        if self.placement:
            self.placement.set_from_pixel(*self._pos(event))
            if self.moved:
                self.moved()
            self.update()

    def mouseMoveEvent(self, event):
        buttons = event.buttons()
        if self.placement and buttons:
            self.placement.set_from_pixel(*self._pos(event))
            if self.moved:
                self.moved()
            self.update()

    def paintEvent(self, _event):
        if self.placement is None:
            return
        painter = QtGui.QPainter(self)
        view = self.placement.view
        painter.fillRect(self.rect(), QtGui.QColor("#1b1b1b"))
        b = self.placement.bounds
        if b:
            x, y, w, h = view.rect_pixels(b)
            painter.setPen(QtGui.QPen(QtGui.QColor("#5f7f9f"), 1))
            painter.drawRect(int(x), int(y), int(w), int(h))
            painter.setBrush(QtGui.QColor("#8fbfe0"))
            for tu, tv in self.placement.snap_targets():
                px, py = view.to_pixels(tu, tv)
                painter.drawEllipse(int(px) - 2, int(py) - 2, 4, 4)
        pu, pv = self.placement.point()
        px, py = view.to_pixels(pu, pv)
        painter.setPen(QtGui.QPen(QtGui.QColor("#e0b050"), 2))
        painter.drawLine(int(px) - 7, int(py), int(px) + 7, int(py))
        painter.drawLine(int(px), int(py) - 7, int(px), int(py) + 7)
        painter.drawEllipse(int(px) - 5, int(py) - 5, 10, 10)
        painter.end()


class PivotStrip(_Widget):
    """The pivot for every transform in one tab, styled to the UV Studio
    mockup: a 3x3 dot grid for the anchor, mode buttons, a numeric U/V, and
    an Edit Pivot button that opens an interactive placement view.

    Four modes are kept (Shell / Selection / UV area / Custom) - Custom is
    what Edit Pivot writes to. The default, Shell + centre, reproduces the
    pre-pivot behaviour exactly.
    """

    MODES = (("shell", "Shell", "Each shell turns about its own centre."),
             ("selection", "Selection",
              "The whole selection turns as one arrangement."),
             ("area", "UV area", "About the tile the shell sits in."),
             ("custom", "Custom", "About a placed or typed point."))

    ANCHOR_GRID = (("nw", "n", "ne"), ("w", "c", "e"), ("sw", "s", "se"))

    def __init__(self, tab, prefs, on_change=None, panel=None, parent=None):
        super(PivotStrip, self).__init__(parent)
        self.tab = tab
        self.prefs = prefs
        self.panel = panel
        self._on_change = on_change
        self._mode_buttons = OrderedDict()
        self._anchor_buttons = OrderedDict()
        self._building = True

        spec = (prefs.pivot(tab) if prefs is not None
                else {"mode": "shell", "anchor": "c", "u": 0.5, "v": 0.5})

        outer = QtWidgets.QHBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(8)

        label = QtWidgets.QLabel("Pivot")
        label.setStyleSheet("color: %s;" % MUTED)
        label.setFixedWidth(38)
        outer.addWidget(label)

        # 3x3 dot grid - round radio-style dots, as the mockup shows.
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(5)
        for r, names in enumerate(self.ANCHOR_GRID):
            for c, name in enumerate(names):
                dot = QtWidgets.QRadioButton("")
                dot.setChecked(spec["anchor"] == name)
                dot.setFixedSize(14, 14)
                dot.setToolTip(name.upper())
                dot.toggled.connect(self._anchor_toggled)
                self._anchor_buttons[name] = dot
                grid.addWidget(dot, r, c)
        outer.addLayout(grid)

        right = QtWidgets.QVBoxLayout()
        right.setSpacing(4)

        modes = QtWidgets.QHBoxLayout()
        modes.setSpacing(4)
        for key, text, tip in self.MODES:
            btn = QtWidgets.QPushButton(text)
            btn.setCheckable(True)
            btn.setChecked(spec["mode"] == key)
            btn.setToolTip(tip)
            btn.setFixedHeight(ROW_HEIGHT)
            btn.clicked.connect(self._mode_clicked)
            self._mode_buttons[key] = btn
            modes.addWidget(btn)
        modes.addStretch(1)
        right.addLayout(modes)

        self.custom_row = QtWidgets.QWidget()
        custom = QtWidgets.QHBoxLayout(self.custom_row)
        custom.setContentsMargins(0, 0, 0, 0)
        custom.setSpacing(4)
        for key in ("u", "v"):
            cap = QtWidgets.QLabel(key.upper())
            cap.setStyleSheet("color: %s;" % MUTED)
            custom.addWidget(cap)
            box = QtWidgets.QDoubleSpinBox()
            box.setDecimals(4)
            box.setRange(-1000.0, 1000.0)
            box.setValue(float(spec.get(key, 0.5)))
            box.setFixedWidth(64)
            box.setFixedHeight(ROW_HEIGHT)
            box.setButtonSymbols(
                _enum(QtWidgets.QAbstractSpinBox, "ButtonSymbols.NoButtons",
                      "NoButtons"))
            box.valueChanged.connect(self._commit)
            setattr(self, "_%s_box" % key, box)
            custom.addWidget(box)
        edit = QtWidgets.QPushButton("Edit Pivot")
        edit.setFixedHeight(ROW_HEIGHT)
        edit.setToolTip("Click to place the pivot in a UV view.")
        edit.clicked.connect(self._edit_pivot)
        custom.addWidget(edit)
        reset = QtWidgets.QPushButton("Reset")
        reset.setFixedHeight(ROW_HEIGHT)
        reset.setToolTip("Back to the shell centre.")
        reset.clicked.connect(self._reset)
        custom.addWidget(reset)
        custom.addStretch(1)
        right.addWidget(self.custom_row)

        outer.addLayout(right, 1)
        self._building = False
        self._sync_enabled()

    # -- interaction ----------------------------------------------------
    def _mode_clicked(self):
        sender = self.sender()
        for btn in self._mode_buttons.values():
            btn.setChecked(btn is sender)
        self._sync_enabled()
        self._commit()

    def _anchor_toggled(self, on):
        if on and not self._building:
            self._commit()

    def _reset(self):
        for name, dot in self._anchor_buttons.items():
            dot.setChecked(name == "c")
        for key, btn in self._mode_buttons.items():
            btn.setChecked(key == "shell")
        self._sync_enabled()
        self._commit()

    def _edit_pivot(self):
        bounds = None
        if self.panel is not None:
            bounds = self.panel.selection_bounds()
        bounds = bounds or (0.0, 1.0, 0.0, 1.0)
        dialog = PivotEditDialog(bounds, (self._u_box.value(),
                                          self._v_box.value()), self)
        if dialog.exec_():
            u, v = dialog.result_uv()
            for key, btn in self._mode_buttons.items():
                btn.setChecked(key == "custom")
            self._u_box.setValue(u)
            self._v_box.setValue(v)
            self._sync_enabled()
            self._commit()

    def _sync_enabled(self):
        custom = self.mode() == "custom"
        for dot in self._anchor_buttons.values():
            dot.setEnabled(not custom)
        self.custom_row.setEnabled(True)
        self._u_box.setEnabled(custom)
        self._v_box.setEnabled(custom)

    def mode(self):
        for key, btn in self._mode_buttons.items():
            if btn.isChecked():
                return key
        return "shell"

    def anchor(self):
        for name, dot in self._anchor_buttons.items():
            if dot.isChecked():
                return name
        return "c"

    def _commit(self, *_):
        if self._building or self.prefs is None:
            return
        self.prefs.set_pivot(self.tab, mode=self.mode(), anchor=self.anchor(),
                             u=self._u_box.value(), v=self._v_box.value())
        if self._on_change:
            self._on_change()


# =====================================================================
# SECTION 4 - One tool's row
# =====================================================================

class ToolRow(_Widget):
    """The button and, beside it, the controls the registry declared for it."""

    def __init__(self, tool, panel, parent=None):
        super(ToolRow, self).__init__(parent)
        self.tool = tool
        self.panel = panel
        self.widgets = []

        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self.button = QtWidgets.QPushButton(tool.label)
        self.button.setFixedHeight(ROW_HEIGHT + 6)
        glyph = panel.glyph_for(tool)
        if glyph is not None:
            self.button.setIcon(glyph)
            self.button.setIconSize(QtCore.QSize(18, 18))
        if glyph is not None and panel.icon_only:
            # Icons only: the name moves to the tooltip. A tool whose icon is
            # a shared fallback keeps a two-letter code, or Cut, Sew and
            # Split would be identical squares.
            code = panel.short_label(tool)
            self.button.setText(code)
            self.button.setFixedWidth(ROW_HEIGHT + (34 if code else 12))
        elif tool.width:
            # MINIMUM, not fixed. A fixed width clipped "Layout (Maya)" to
            # "ayout (Maya" in Maya 2022 - Qt centres and clips from both
            # ends, so the label lost a character at each edge and read as a
            # different word. The width now aligns the short labels and
            # long ones grow past it.
            self.button.setMinimumWidth(tool.width)
            self.button.setSizePolicy(
                _enum(QtWidgets.QSizePolicy, "Policy.Preferred", "Preferred"),
                _enum(QtWidgets.QSizePolicy, "Policy.Fixed", "Fixed"))
        self.button.setContextMenuPolicy(
            _enum(QtCore.Qt, "ContextMenuPolicy.CustomContextMenu",
                  "CustomContextMenu"))
        self.button.customContextMenuRequested.connect(self._context_menu)
        self.button.clicked.connect(self._run)

        tip = ([tool.label] if panel.icon_only else []) + (
            [tool.tooltip] if tool.tooltip else [])
        available, reason = panel.availability(tool)
        if not available:
            self.button.setEnabled(False)
            tip.append("Unavailable: %s" % reason)
        resolved = panel.resolved_name(tool)
        if resolved:
            tip.append("Runs %s" % resolved)
        tip.append("Right-click for more settings.")
        self.button.setToolTip("\n".join(tip))
        row.addWidget(self.button)

        values = panel.settings_for(tool)
        if not tool.options_only:
            for field in tool.fields:
                widget = FieldWidget(field, values, self._field_changed)
                self.widgets.append(widget)
                row.addWidget(widget)
                for button in widget.nudge_buttons():
                    button.clicked.connect(self._nudge_clicked)

        # Icon affordances the reference shows beside particular tools. These
        # run other tools (or this one with an override) rather than editing a
        # setting, so they live on the row, not in a field.
        for icon, run_id, overrides, tip in self._row_icons():
            btn = QtWidgets.QPushButton()
            ico = _icon(icon)
            if ico is not None:
                btn.setIcon(ico)
            btn.setFixedSize(ROW_HEIGHT, ROW_HEIGHT)
            btn.setToolTip(tip)
            btn.clicked.connect(
                lambda _c=False, r=run_id, o=overrides:
                self.panel.run_tool_id(r, o))
            row.addWidget(btn)

        # The option box: every tool with a tweakable setting gets Maya's
        # option-box mark, which opens the floating Apply/Accept/Close window.
        # A tool with no settings gets none - an option box with nothing in it
        # is a promise the tool cannot keep.
        if tool.fields or tool.sheet_fields():
            opt = QtWidgets.QPushButton()
            ico = _icon("optionbox")
            if ico is not None:
                opt.setIcon(ico)
            opt.setFixedSize(ROW_HEIGHT, ROW_HEIGHT)
            opt.setToolTip("%s options\u2026" % tool.label)
            opt.clicked.connect(lambda _c=False: self.panel.open_options(
                self.tool))
            row.addWidget(opt)

        row.addStretch(1)

    def _row_icons(self):
        """(icon, tool id, overrides, tooltip) buttons for this tool's row."""
        if self.tool.tool_id == "rotate":
            return [("rot_ccw", "rotate", {"degrees": 90.0},
                     "Rotate +90\u00b0"),
                    ("rot_cw", "rotate", {"degrees": -90.0},
                     "Rotate \u221290\u00b0")]
        if self.tool.tool_id == "scale":
            return [("scale", "scale", None, "Apply scale")]
        return []
        """(icon, tool id, overrides, tooltip) buttons for this tool's row."""
        if self.tool.tool_id == "rotate":
            return [("rot_ccw", "rotate", {"degrees": 90.0},
                     "Rotate +90\u00b0"),
                    ("rot_cw", "rotate", {"degrees": -90.0},
                     "Rotate \u221290\u00b0")]
        if self.tool.tool_id == "scale":
            return [("scale", "scale", None, "Apply scale")]
        return []

    # -- values ---------------------------------------------------------
    def values(self):
        merged = {}
        for widget in self.widgets:
            merged.update(widget.values())
        return merged

    def _field_changed(self):
        """Write straight through to prefs, so the settings sheet and the
        next run both see what was just typed."""
        self.panel.remember(self.tool, self.values())

    def _run(self):
        self._field_changed()
        self.panel.run_tool(self.tool)

    def _nudge_clicked(self):
        pair = self.sender().property("nudge")
        if not pair:
            return
        tool_id, axis, direction = pair
        self.panel.run_tool_id(tool_id, {"axis": axis,
                                         "direction": direction})

    def _context_menu(self, point):
        menu = QtWidgets.QMenu(self)
        menu.addAction("Settings\u2026",
                       lambda: self.panel.open_settings(self.tool, self))
        menu.addSeparator()
        menu.addAction("Reset to defaults",
                       lambda: self.panel.reset_tool(self.tool, self))
        menu.addAction("Copy settings",
                       lambda: self.panel.copy_settings(self.tool))
        paste = menu.addAction("Paste settings",
                               lambda: self.panel.paste_settings(self.tool,
                                                                 self))
        paste.setEnabled(bool(self.panel.clipboard))
        menu.exec_(self.button.mapToGlobal(point))

    def refresh(self):
        """Rebuild the inline controls from prefs after an edit elsewhere."""
        values = self.panel.settings_for(self.tool)
        for widget in list(self.widgets):
            index = self.layout().indexOf(widget)
            replacement = FieldWidget(widget.field, values,
                                      self._field_changed)
            self.layout().insertWidget(index, replacement)
            for button in replacement.nudge_buttons():
                button.clicked.connect(self._nudge_clicked)
            self.layout().removeWidget(widget)
            widget.setParent(None)
            self.widgets[self.widgets.index(widget)] = replacement


# =====================================================================
# SECTION 5 - Collapsible, reorderable section
# =====================================================================

class ToolSection(_Widget):
    """A named group of tool rows, collapsible and movable."""

    def __init__(self, tab, name, tools, panel, parent=None):
        super(ToolSection, self).__init__(parent)
        self.tab = tab
        self.name = name
        self.panel = panel
        self.rows = []

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())

        self.body = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(self.body)
        body.setContentsMargins(6, 6, 6, 8)
        body.setSpacing(5)
        spec = panel.section_layout(tab, name)
        placed = set()
        if spec:
            placed = self._build_split_layout(body, tools, spec)
        # Icons only: tools with no inline fields sit side by side, several
        # to a line - one icon per full-width row wasted the space that
        # icon-only mode exists to save. Tools with fields keep their row.
        flow = None
        flow_count = 0
        per_line = 4
        for tool in tools:
            if tool.hidden or tool.tool_id in placed:
                continue            # hidden, or already placed by the layout
            row = ToolRow(tool, panel)
            self.rows.append(row)
            compact = (panel.icon_only and not (tool.fields
                                                and not tool.options_only))
            if compact:
                if flow is None:
                    flow = QtWidgets.QGridLayout()
                    flow.setHorizontalSpacing(4)
                    flow.setVerticalSpacing(4)
                    flow.setAlignment(_enum(QtCore.Qt,
                                            "AlignmentFlag.AlignLeft",
                                            "AlignLeft"))
                    body.addLayout(flow)
                flow.addWidget(row, flow_count // per_line,
                               flow_count % per_line)
                flow_count += 1
            else:
                flow = None
                flow_count = 0
                body.addWidget(row)
        outer.addWidget(self.body)

        if panel.prefs is not None:
            self.set_collapsed(panel.prefs.is_collapsed(tab, name))

    def _build_split_layout(self, body, tools, spec):
        """Grid of buttons on the left, a divider, ordinary rows on the right.

        Driven by SECTION_LAYOUTS in the registry, so the arrangement is data:
        which tools sit in the grid and which run down the side is edited
        there, not here. Any tool the spec does not mention still appears
        below as a normal row, so a new tool can never vanish because the
        layout predates it. Returns the ids it placed.
        """
        by_id = OrderedDict((t.tool_id, t) for t in tools if not t.hidden)
        placed = set()

        wrap = QtWidgets.QHBoxLayout()
        wrap.setSpacing(10)

        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(4)
        for r, row_ids in enumerate(spec.get("grid") or []):
            for c, tool_id in enumerate(row_ids):
                tool = by_id.get(tool_id)
                if tool is None:
                    continue
                grid.addWidget(self._grid_button(tool), r, c)
                placed.add(tool_id)
        wrap.addLayout(grid)

        side_ids = [t for t in (spec.get("side") or []) if t in by_id]
        if side_ids:
            divider = QtWidgets.QFrame()
            divider.setFixedWidth(2)
            divider.setStyleSheet("background: %s;" % COL["accent"])
            wrap.addWidget(divider)

            side = QtWidgets.QVBoxLayout()
            side.setSpacing(4)
            for tool_id in side_ids:
                row = ToolRow(by_id[tool_id], self.panel)
                self.rows.append(row)
                side.addWidget(row)
                placed.add(tool_id)
            side.addStretch(1)
            wrap.addLayout(side, 1)
        else:
            wrap.addStretch(1)

        body.addLayout(wrap)
        return placed

    def _grid_button(self, tool):
        button = QtWidgets.QPushButton(tool.label)
        button.setFixedHeight(ROW_HEIGHT + 6)
        glyph = self.panel.glyph_for(tool)
        if glyph is not None:
            button.setIcon(glyph)
        if glyph is not None and self.panel.icon_only:
            code = self.panel.short_label(tool)
            button.setText(code)
            button.setFixedWidth(ROW_HEIGHT + (34 if code else 12))
        else:
            button.setMinimumWidth(86)
        tip = [tool.tooltip] if tool.tooltip else []
        available, reason = self.panel.availability(tool)
        if not available:
            button.setEnabled(False)
            tip.append("Unavailable: %s" % reason)
        button.setToolTip("\n".join(tip) or tool.label)
        button.clicked.connect(lambda _c=False, t=tool: self.panel.run_tool(t))
        return button

    def _build_header(self):
        header = QtWidgets.QWidget()
        header.setFixedHeight(22)
        header.setStyleSheet(
            "background: %s; border-top: 1px solid #444;"
            " border-bottom: 1px solid %s;" % (COL["section"], COL["border"]))
        row = QtWidgets.QHBoxLayout(header)
        row.setContentsMargins(6, 0, 4, 0)
        row.setSpacing(4)

        # The arrow opens/closes on a single click; the title bar itself on a
        # double-click (eventFilter), so both ways work.
        self.toggle = QtWidgets.QToolButton()
        self.toggle.setAutoRaise(True)
        self.toggle.setCheckable(True)
        self.toggle.setText("\u25be")
        self.toggle.setToolTip("Click to open or close")
        self.toggle.clicked.connect(self._on_toggle)
        row.addWidget(self.toggle)

        label = QtWidgets.QLabel(self.name)
        label.setStyleSheet("font-weight: 600;")
        label.installEventFilter(self)
        row.addWidget(label)
        header.setToolTip("Double-click to open or close")
        header.installEventFilter(self)
        row.addStretch(1)

        for text, delta, tip in (("\u25b2", -1, "Move section up"),
                                 ("\u25bc", 1, "Move section down")):
            button = QtWidgets.QToolButton()
            button.setAutoRaise(True)
            button.setText(text)
            button.setToolTip(tip)
            button.clicked.connect(
                lambda _checked=False, d=delta:
                self.panel.move_section(self.tab, self.name, d))
            row.addWidget(button)
        return header

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == _enum(QtCore.QEvent, "Type.MouseButtonDblClick",
                         "MouseButtonDblClick"):
            self.toggle.setChecked(not self.toggle.isChecked())
            self._on_toggle()
            return True
        return False

    def _on_toggle(self):
        collapsed = self.toggle.isChecked()
        self.body.setVisible(not collapsed)
        self.toggle.setText("\u25b8" if collapsed else "\u25be")
        if self.panel.prefs is not None:
            self.panel.prefs.set_collapsed(self.tab, self.name, collapsed)
            self.panel.save_prefs()

    def set_collapsed(self, collapsed):
        self.toggle.setChecked(bool(collapsed))
        self.body.setVisible(not collapsed)
        self.toggle.setText("\u25b8" if collapsed else "\u25be")

    def refresh(self):
        for row in self.rows:
            row.refresh()


# =====================================================================
# SECTION 6 - Settings sheet
# =====================================================================

class ToolOptionBox(_Dialog):
    """A Maya-style option box: a floating window with every tweakable setting
    for one tool, and the three buttons Maya artists reach for without
    thinking.

        Apply   - write the settings, run the tool, LEAVE THE WINDOW OPEN.
                  For iterating: nudge a value, Apply, look, nudge again.
        Accept  - write the settings, run the tool, CLOSE.
        Close   - shut the window, changing nothing since it opened.

    "Close" rather than "Cancel" because settings written by an earlier Apply
    are real and stay; only settings edited AFTER the last Apply are dropped,
    which is exactly Maya's behaviour and the only honest label for it.

    Non-modal, so the artist can select different UVs between Applies without
    the window blocking the viewport - the whole point of Apply.
    """

    def __init__(self, tool, panel, parent=None):
        super(ToolOptionBox, self).__init__(parent)
        self.tool = tool
        self.panel = panel
        self.widgets = []
        self.setWindowTitle("%s Options" % tool.label)
        self.setModal(False)
        self.setMinimumWidth(300)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        head = QtWidgets.QLabel(tool.label)
        head.setStyleSheet("font-weight: 600; font-size: 12px;")
        outer.addWidget(head)
        if tool.tooltip:
            blurb = QtWidgets.QLabel(tool.tooltip)
            blurb.setStyleSheet("color: %s;" % MUTED)
            blurb.setWordWrap(True)
            outer.addWidget(blurb)

        line = QtWidgets.QFrame()
        line.setFrameShape(_enum(QtWidgets.QFrame, "Shape.HLine", "HLine"))
        line.setStyleSheet("color: %s;" % COL["border"])
        outer.addWidget(line)

        values = panel.settings_for(tool)

        # Maya groups an option box's fields under section header bars, with
        # right-aligned labels in a fixed gutter and the controls to their
        # right. Each field names its `group`; fields with no group sit in a
        # leading ungrouped block, exactly as Maya's own dialogs do.
        fields = list(tool.fields) + [f for f in tool.sheet_fields()
                                      if f.key not in
                                      set(x.key for x in tool.fields)]
        if not fields:
            empty = QtWidgets.QLabel("This tool has no options.")
            empty.setStyleSheet("color: %s;" % MUTED)
            outer.addWidget(empty)

        groups = OrderedDict()
        for field in fields:
            groups.setdefault(field.group or "", []).append(field)

        for group_name, group_fields in groups.items():
            if group_name:
                header = QtWidgets.QLabel(group_name)
                header.setStyleSheet(
                    "background: %s; color: %s; padding: 3px 6px;"
                    " font-weight: 600; border-top: 1px solid #444;"
                    % (COL["section"], COL["text"]))
                outer.addWidget(header)

            block = QtWidgets.QWidget()
            grid = QtWidgets.QGridLayout(block)
            grid.setContentsMargins(0, 4, 0, 4)
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(6)
            grid.setColumnMinimumWidth(0, 130)
            grid.setColumnStretch(1, 1)

            for row_index, field in enumerate(group_fields):
                widget = FieldWidget(field, values, None,
                                     show_label=(field.kind == "bool"))
                self.widgets.append(widget)
                if field.kind == "bool":
                    # A checkbox carries its own label; Maya still right-aligns
                    # the caption gutter, so the box sits in the value column.
                    grid.addWidget(widget, row_index, 1)
                else:
                    cap = QtWidgets.QLabel((field.label or field.key) + ":")
                    cap.setStyleSheet("color: %s;" % COL["text"])
                    cap.setAlignment(
                        _enum(QtCore.Qt, "AlignmentFlag.AlignRight",
                              "AlignRight")
                        | _enum(QtCore.Qt, "AlignmentFlag.AlignVCenter",
                                "AlignVCenter"))
                    grid.addWidget(cap, row_index, 0)
                    grid.addWidget(widget, row_index, 1)
                if field.tooltip:
                    widget.setToolTip(field.tooltip)
            outer.addWidget(block)

        outer.addStretch(1)

        # Maya's three-button footer: the PRIMARY button is named after the
        # tool ("Layout UVs", "Unfold UVs") and does Accept - apply and close.
        # Apply runs and stays open; Close discards edits since the last Apply.
        # Reset sits apart on the left.
        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(6)
        reset = QtWidgets.QPushButton("Reset")
        reset.setToolTip("Restore this tool's default settings.")
        reset.setMinimumWidth(64)
        reset.clicked.connect(self._reset)
        bar.addWidget(reset)
        bar.addStretch(1)
        for text, slot, tip, primary in (
                (tool.label, self._accept,
                 "Apply the settings, run %s, and close." % tool.label,
                 True),
                ("Apply", self._apply,
                 "Apply the settings and run the tool. Leaves this open.",
                 False),
                ("Close", self.close,
                 "Close without applying anything edited since the last "
                 "Apply.", False)):
            btn = QtWidgets.QPushButton(text)
            btn.setToolTip(tip)
            # Wide enough for its own label: the primary button carries the
            # tool's name and "Layout (Unfold3D)" was clipped at 96px.
            btn.setMinimumWidth(max(80, btn.fontMetrics().horizontalAdvance(
                text) + 24) if hasattr(btn.fontMetrics(), "horizontalAdvance")
                else max(80, btn.fontMetrics().width(text) + 24))
            btn.setDefault(primary)
            if primary:
                btn.setStyleSheet("background: %s;" % COL["accent"])
            btn.clicked.connect(slot)
            bar.addWidget(btn)
        outer.addLayout(bar)

    def _collect(self):
        merged = {}
        for widget in self.widgets:
            merged.update(widget.values())
        return merged

    def _write(self):
        self.panel.remember(self.tool, self._collect())

    def _apply(self):
        self._write()
        self.panel.run_tool(self.tool)

    def _accept(self):
        self._write()
        self.panel.run_tool(self.tool)
        self.close()

    def _reset(self):
        if self.panel.runner is not None:
            self.panel.runner.prefs.tool_settings.pop(self.tool.tool_id, None)
            self.panel.save_prefs()
        # Rebuild the widgets from the freshly-defaulted values.
        values = self.panel.settings_for(self.tool)
        for widget in self.widgets:
            widget.reset_to(values)


def _mode_icon(mode, size=22):
    """Selection-mode glyphs in the style of Maya's UV Toolkit strip."""
    pix = QtGui.QPixmap(size, size)
    pix.fill(_enum(QtCore.Qt, "GlobalColor.transparent", "transparent"))
    p = QtGui.QPainter(pix)
    p.setRenderHint(_enum(QtGui.QPainter, "RenderHint.Antialiasing",
                          "Antialiasing"))
    s = float(size)
    grey = QtGui.QColor("#cfcfcf")
    green = QtGui.QColor("#6fcf97")
    P, R = QtCore.QPointF, QtCore.QRectF
    pen = QtGui.QPen(grey, 1.4)
    p.setPen(pen)
    dot = s * 0.16

    def handle(x, y, colour):
        p.fillRect(R(x - dot / 2, y - dot / 2, dot, dot), colour)

    if mode == "vertex":
        p.drawRect(R(s * .2, s * .2, s * .6, s * .6))
        for x, y in ((.2, .2), (.8, .2), (.2, .8), (.8, .8)):
            handle(s * x, s * y, green)
    elif mode == "edge":
        p.setPen(QtGui.QPen(grey, 1.4))
        p.drawPolygon(QtGui.QPolygonF([P(s * .5, s * .1), P(s * .9, s * .5),
                                       P(s * .5, s * .9), P(s * .1, s * .5)]))
        p.setPen(QtGui.QPen(green, 2.2))
        p.drawLine(P(s * .5, s * .1), P(s * .9, s * .5))
    elif mode == "face":
        top = QtGui.QPolygonF([P(s * .5, s * .1), P(s * .9, s * .3),
                               P(s * .5, s * .5), P(s * .1, s * .3)])
        left = QtGui.QPolygonF([P(s * .1, s * .3), P(s * .5, s * .5),
                                P(s * .5, s * .92), P(s * .1, s * .72)])
        right = QtGui.QPolygonF([P(s * .9, s * .3), P(s * .5, s * .5),
                                 P(s * .5, s * .92), P(s * .9, s * .72)])
        p.setPen(_enum(QtCore.Qt, "PenStyle.NoPen", "NoPen"))
        p.setBrush(QtGui.QColor("#e6e6e6")); p.drawPolygon(top)
        p.setBrush(QtGui.QColor("#b8b8b8")); p.drawPolygon(left)
        p.setBrush(green); p.drawPolygon(right)
    elif mode == "uv":
        p.drawRect(R(s * .15, s * .15, s * .7, s * .7))
        p.drawRect(R(s * .32, s * .32, s * .36, s * .36))
        for x, y in ((.15, .15), (.85, .15), (.15, .85), (.85, .85)):
            handle(s * x, s * y, grey)
        handle(s * .5, s * .5, green)
    elif mode == "shell":
        p.setPen(QtGui.QPen(green, 1.6))
        p.drawRect(R(s * .12, s * .12, s * .76, s * .76))
        p.drawLine(P(s * .5, s * .12), P(s * .5, s * .88))
        p.drawLine(P(s * .12, s * .5), P(s * .88, s * .5))
    p.end()
    return QtGui.QIcon(pix)


class SelectionModeStrip(_Widget):
    """Vertex / Edge / Face | UV / UV Shell, above the tabs.

    Lives OUTSIDE the tab widget, so it is the same strip whichever tab is
    open - selection mode is a property of the scene, not of a tab. Kept in
    sync with Maya: switching mode anywhere else (F9, the marking menu)
    updates the highlighted button within half a second.
    """

    MODES = (("vertex", "Vertex"), ("edge", "Edge"), ("face", "Face"),
             None, ("uv", "UV"), "pin", ("shell", "UV Shell"))

    def __init__(self, panel, parent=None):
        super(SelectionModeStrip, self).__init__(parent)
        self.panel = panel
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(2, 2, 2, 2)
        row.setSpacing(3)
        self.group = QtWidgets.QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = OrderedDict()
        for entry in self.MODES:
            if entry is None:
                divider = QtWidgets.QFrame()
                divider.setFixedWidth(1)
                divider.setStyleSheet("background: %s;" % COL["border"])
                row.addSpacing(4)
                row.addWidget(divider)
                row.addSpacing(4)
                continue
            if entry == "pin":
                pin = QtWidgets.QPushButton()
                pin.setIcon(_icon("pin", 20))
                pin.setIconSize(QtCore.QSize(20, 20))
                pin.setFixedSize(34, 30)
                pin.setToolTip("Pin the selected UVs\nRight-click to unpin")
                pin.clicked.connect(lambda _c=False: self.panel.run_tool_id("pin"))
                pin.setContextMenuPolicy(_enum(
                    QtCore.Qt, "ContextMenuPolicy.CustomContextMenu",
                    "CustomContextMenu"))
                pin.customContextMenuRequested.connect(
                    lambda _p: self.panel.run_tool_id("unpin"))
                self.pin_button = pin
                row.addWidget(pin)
                continue
            mode, label = entry
            button = QtWidgets.QPushButton()
            button.setIcon(_mode_icon(mode))
            button.setIconSize(QtCore.QSize(22, 22))
            button.setFixedSize(34, 30)
            button.setCheckable(True)
            button.setToolTip("%s selection mode\nConverts the current "
                              "component selection." % label)
            button.setProperty("uvs_mode", mode)
            button.clicked.connect(self._clicked)
            self.group.addButton(button)
            self.buttons[mode] = button
            row.addWidget(button)
        row.addStretch(1)

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.sync)
        self._timer.start(500)

    @staticmethod
    def _bridge():
        return sys.modules.get("uvstudio_m2_scene_bridge")

    def _clicked(self):
        mode = self.sender().property("uvs_mode")
        m2 = self._bridge()
        if m2 is None or not hasattr(m2, "set_selection_mode"):
            self.panel.log("ERROR", "Selection modes need the scene bridge.")
            return
        try:
            m2.set_selection_mode(mode)
        except Exception as exc:
            self.panel.log("ERROR", "Switching to %s mode failed: %s"
                           % (mode, exc))
        self.sync()

    def sync(self):
        """Light the button for Maya's current mode; none in object mode."""
        if not self.isVisible():
            return
        m2 = self._bridge()
        mode = None
        if m2 is not None and hasattr(m2, "current_selection_mode"):
            try:
                mode = m2.current_selection_mode()
            except Exception:
                mode = None
        self.group.setExclusive(False)
        for key, button in self.buttons.items():
            button.setChecked(key == mode)
        self.group.setExclusive(True)


class RecipePanel(_Widget):
    """The recipe: what has been done, and what to do with it again.

    The recipe has existed since M4 was written and had no way in or out of
    a session (D-06). It now saves, loads and replays. This is the surface
    for that, and it is deliberately a LIST rather than a graph: the recipe
    is an ordered history, and showing it as anything cleverer would imply
    a freedom M4 does not give - steps cannot cross a topology barrier.

    Nothing here recomputes state. The list is rebuilt from the recipe after
    every edit, so what is on screen is what M4 holds, not a parallel model
    that can drift from it.
    """

    def __init__(self, panel, parent=None):
        super(RecipePanel, self).__init__(parent)
        self.panel = panel
        self._library = None

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)

        self.summary = QtWidgets.QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color: %s;" % MUTED)
        outer.addWidget(self.summary)

        self.steps = QtWidgets.QTreeWidget()
        self.steps.setHeaderLabels(["Step", "Settings"])
        self.steps.setRootIsDecorated(False)
        self.steps.setAlternatingRowColors(True)
        self.steps.itemChanged.connect(self._item_changed)
        outer.addWidget(self.steps, 1)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        for text, slot, tip in (
                ("\u25b2", self._move_up, "Move this step earlier"),
                ("\u25bc", self._move_down, "Move this step later"),
                ("Remove", self._remove, "Delete this step"),
                ("Clear", self._clear, "Forget every step")):
            button = QtWidgets.QPushButton(text)
            button.setToolTip(tip)
            if text in ("\u25b2", "\u25bc"):
                button.setFixedWidth(28)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch(1)
        outer.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        self.saved = QtWidgets.QComboBox()
        self.saved.setMinimumWidth(120)
        row.addWidget(self.saved, 1)
        for text, slot, tip in (
                ("Load", self._load, "Replace the current recipe with this one"),
                ("Delete", self._delete, "Remove this saved recipe")):
            button = QtWidgets.QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
        outer.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setPlaceholderText("recipe name")
        row.addWidget(self.name_edit, 1)
        save = QtWidgets.QPushButton("Save")
        save.setToolTip("Write this recipe to disk.\n"
                        "Right-click to include snapshots (much larger).")
        save.clicked.connect(lambda: self._save(include_snapshots=False))
        save.setContextMenuPolicy(
            _enum(QtCore.Qt, "ContextMenuPolicy.CustomContextMenu",
                  "CustomContextMenu"))
        save.customContextMenuRequested.connect(self._save_menu)
        self.save_button = save
        row.addWidget(save)
        outer.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        dry = QtWidgets.QPushButton("Dry run")
        dry.setToolTip("Report what would run, without touching the scene.")
        dry.clicked.connect(lambda: self._replay(dry_run=True))
        row.addWidget(dry)
        replay = QtWidgets.QPushButton("Replay")
        replay.setToolTip("Re-run every enabled step on the CURRENT "
                          "selection, as one undo step.\n"
                          "Right-click to run on the recorded meshes instead.")
        replay.clicked.connect(lambda: self._replay())
        replay.setContextMenuPolicy(
            _enum(QtCore.Qt, "ContextMenuPolicy.CustomContextMenu",
                  "CustomContextMenu"))
        replay.customContextMenuRequested.connect(self._replay_menu)
        self.replay_button = replay
        row.addWidget(replay, 1)
        outer.addLayout(row)

        self.refresh()

    # -- state -----------------------------------------------------------
    @property
    def recipe(self):
        return getattr(self.panel.runner, "recipe", None)

    @property
    def library(self):
        if self._library is None:
            module = sys.modules.get("uvstudio_m4_recipe")
            if module is None:
                return None
            self._library = module.RecipeLibrary()
        return self._library

    def _selected_step(self):
        items = self.steps.selectedItems()
        return items[0].data(0, _ROLE_ID) if items else None

    # -- display -----------------------------------------------------------
    def refresh(self):
        recipe = self.recipe
        self.steps.blockSignals(True)
        self.steps.clear()

        if recipe is None:
            self.summary.setText("No recipe attached.")
            self.steps.blockSignals(False)
            return

        degraded = set(s.id for s in recipe.degraded_steps())
        for step in recipe.steps:
            item = QtWidgets.QTreeWidgetItem(
                self.steps, [step.label, self._describe(step.params)])
            item.setData(0, _ROLE_ID, step.id)
            item.setFlags(item.flags()
                          | _enum(QtCore.Qt, "ItemFlag.ItemIsUserCheckable",
                                  "ItemIsUserCheckable"))
            item.setCheckState(
                0, _enum(QtCore.Qt, "CheckState.Checked", "Checked")
                if step.enabled
                else _enum(QtCore.Qt, "CheckState.Unchecked", "Unchecked"))
            if step.is_barrier:
                item.setToolTip(0, "Changes topology. Steps cannot be "
                                   "reordered across it.")
                font = item.font(0)
                font.setItalic(True)
                item.setFont(0, font)
            if step.id in degraded:
                item.setForeground(
                    0, QtGui.QBrush(QtGui.QColor(WARN_COLOUR)))
                item.setToolTip(0, "Its stored result was evicted; replaying "
                                   "re-runs the command and may differ.")
        self.steps.blockSignals(False)
        self.steps.resizeColumnToContents(0)

        info = recipe.summary()
        self.summary.setText(
            "%s — %d step(s), %d enabled, %d barrier(s)"
            % (info["name"], info["steps"], info["enabled"],
               info["barriers"]))
        if not self.name_edit.text():
            self.name_edit.setText(recipe.name)
        self._refresh_library()

    def _refresh_library(self):
        library = self.library
        self.saved.clear()
        if library is None:
            return
        for record in library.names():
            label = record["name"]
            if not record["readable"]:
                label += "  (unreadable)"
            elif record["steps"] is not None:
                label += "  (%d)" % record["steps"]
            self.saved.addItem(label, record["path"])

    @staticmethod
    def _describe(params):
        """A one-line rendering of a step's settings.

        The pivot is spelled out rather than shown as a dict: "pivot
        selection/c" is the thing an artist would check when a replay looks
        wrong, and {'mode': 'selection', ...} buries it.
        """
        if not params:
            return ""
        parts = []
        for key in sorted(params):
            value = params[key]
            if key == "pivot" and isinstance(value, dict):
                parts.append("pivot %s/%s" % (value.get("mode"),
                                              value.get("anchor")))
            elif isinstance(value, float):
                parts.append("%s %g" % (key, value))
            elif isinstance(value, (list, tuple)):
                parts.append("%s %d item(s)" % (key, len(value)))
            elif isinstance(value, bool):
                if value:
                    parts.append(key)
            else:
                parts.append("%s %s" % (key, value))
        return ", ".join(parts)

    # -- editing -------------------------------------------------------------
    def _item_changed(self, item, column):
        recipe = self.recipe
        if recipe is None or column != 0:
            return
        checked = item.checkState(0) == _enum(
            QtCore.Qt, "CheckState.Checked", "Checked")
        recipe.set_enabled(item.data(0, _ROLE_ID), checked)
        self.refresh()

    def _shift(self, delta):
        recipe = self.recipe
        step_id = self._selected_step()
        if recipe is None or step_id is None:
            return
        target = recipe.index_of(step_id) + delta
        if target < 0 or target >= len(recipe.steps):
            return
        try:
            recipe.move(step_id, target)
        except Exception as exc:
            # M4 refuses to move a step across a topology barrier. That
            # refusal is the point; surfacing it beats silently not moving.
            self.panel.log("WARN", str(exc))
            return
        self.refresh()

    def _move_up(self):
        self._shift(-1)

    def _move_down(self):
        self._shift(1)

    def _remove(self):
        recipe = self.recipe
        step_id = self._selected_step()
        if recipe is not None and step_id is not None:
            recipe.remove(step_id)
            self.refresh()

    def _clear(self):
        recipe = self.recipe
        if recipe is None or not recipe.steps:
            return
        if not self.panel.confirm("Forget all %d step(s)?"
                                  % len(recipe.steps)):
            return
        for step in list(recipe.steps):
            recipe.remove(step.id)
        self.refresh()

    # -- disk -----------------------------------------------------------------
    def _save(self, include_snapshots=False):
        recipe, library = self.recipe, self.library
        if recipe is None or library is None:
            return
        name = self.name_edit.text().strip() or recipe.name
        try:
            path = library.save(recipe, name=name,
                                include_snapshots=include_snapshots)
        except Exception as exc:
            self.panel.log("ERROR", "Could not save: %s" % exc)
            return
        self.panel.log("OK", "Saved %s to %s" % (name, path))
        self.refresh()

    def _save_menu(self, point):
        menu = QtWidgets.QMenu(self)
        menu.addAction("Save with snapshots (much larger)",
                       lambda: self._save(include_snapshots=True))
        menu.exec_(self.save_button.mapToGlobal(point))

    def _load(self):
        library = self.library
        if library is None or self.saved.count() == 0:
            return
        path = self.saved.currentData()
        try:
            loaded = library.load(path)
        except Exception as exc:
            self.panel.log("ERROR", "Could not load: %s" % exc)
            return
        self.panel.runner.recipe = loaded
        self.name_edit.setText(loaded.name)
        self.panel.log("OK", "Loaded %s (%d step(s))"
                       % (loaded.name, len(loaded.steps)))
        self.refresh()

    def _delete(self):
        library = self.library
        if library is None or self.saved.count() == 0:
            return
        label = self.saved.currentText()
        if not self.panel.confirm("Delete saved recipe %s?" % label):
            return
        library.delete(self.saved.currentData())
        self.panel.log("INFO", "Deleted %s" % label)
        self._refresh_library()

    # -- replay -----------------------------------------------------------------
    def _replay(self, dry_run=False, use_recorded_targets=False):
        if self.panel.runner is None or self.recipe is None:
            return
        try:
            records = self.panel.runner.replay(
                use_recorded_targets=use_recorded_targets, dry_run=dry_run)
        except Exception as exc:
            self.panel.log("ERROR", "Replay failed: %s" % exc)
            return

        tally = {}
        for record in records:
            tally[record["status"]] = tally.get(record["status"], 0) + 1
        self.panel.log("OK" if not tally.get("failed") else "WARN",
                       "%s: %s" % ("Dry run" if dry_run else "Replay",
                                   ", ".join("%d %s" % (count, status)
                                             for status, count
                                             in sorted(tally.items()))))
        # Anything degraded or unknown is named individually. A count alone
        # would let "3 ran, 1 degraded" pass for success.
        for record in records:
            if record["degraded"] or record["status"] in ("unknown", "failed"):
                self.panel.log("WARN", "  %s: %s"
                               % (record["kind"], record["message"]))
        self.refresh()

    def _replay_menu(self, point):
        menu = QtWidgets.QMenu(self)
        menu.addAction("Replay on the recorded meshes",
                       lambda: self._replay(use_recorded_targets=True))
        menu.exec_(self.replay_button.mapToGlobal(point))


# =====================================================================
# SECTION 7 - The panel
# =====================================================================

class UVStudioToolsPanel(_Widget):
    """Tool tabs plus the environment/commands/log tabs from M1."""

    def __init__(self, env=None, resolved=None, runner=None, registry=None,
                 parent=None):
        super(UVStudioToolsPanel, self).__init__(parent)
        self.setObjectName("uvStudioToolsPanel")
        self.setStyleSheet(PANEL_QSS)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(_enum(QtGui.QPalette, "ColorRole.Window", "Window"),
                     QtGui.QColor(COL["panel"]))
        self.setPalette(pal)
        self.setSizePolicy(
            _enum(QtWidgets.QSizePolicy, "Policy.Expanding", "Expanding"),
            _enum(QtWidgets.QSizePolicy, "Policy.Expanding", "Expanding"))

        self.env = env
        self.resolved = resolved or {}
        self.runner = runner
        self.registry = registry
        self.prefs = runner.prefs if runner is not None else None
        self.embed_log = []
        self.clipboard = None
        self._sections = {}
        self._pivot_strips = {}
        # Declared before anything is built. Construction order decides when
        # each of these becomes a real widget, and a method that runs early -
        # the action bar logging its wiring - used to hit AttributeError and
        # take the whole panel down. Naming them here means "not built yet"
        # is None, which reads as a state rather than a crash.
        self.status = None
        self.log_view = None
        self.verdict = None
        self.env_view = None
        self.cmd_tree = None
        self.recipe_panel = None
        self.counters = OrderedDict()
        self._action_buttons = []

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        self.verdict = QtWidgets.QLabel("")
        self.verdict.setWordWrap(True)
        outer.addWidget(self.verdict, 0)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setTabPosition(
            _enum(QtWidgets.QTabWidget, "TabPosition.East", "East"))
        self.tabs.setMinimumWidth(320)
        self.selection_strip = None
        self.health = None
        self.suggest_buttons = []

        if registry is not None and hasattr(registry, "PANEL_LAYOUT"):
            # Redesign v2: search and suggestions on top, tabs drawn from
            # the layout table, health strip at the bottom. The selection
            # modes live in the viewport bar (the artist's edit).
            outer.addLayout(self.build_search(), 0)
            outer.addLayout(self.build_suggestions(), 0)
            outer.addWidget(self.tabs, 1)
            self.build_layout_tabs()
            self.health = HealthStrip(self)
            outer.addWidget(self.health, 0)
        else:
            # Above the tabs, not in one: it never changes with the tab.
            self.selection_strip = SelectionModeStrip(self)
            outer.addWidget(self.selection_strip, 0)
            outer.addWidget(self.tabs, 1)
            self._build_tool_tabs()
            self.recipe_panel = RecipePanel(self)
            self.tabs.addTab(self.recipe_panel, "Recipe")
            self._build_info_tabs()
            outer.addWidget(self._build_counters(), 0)
            outer.addWidget(self._build_action_bar(), 0)

        row = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        self.icons_toggle = QtWidgets.QPushButton("Icons only")
        self.icons_toggle.setCheckable(True)
        self.icons_toggle.setChecked(self.icon_only)
        self.icons_toggle.setToolTip("Show tool buttons as icons only. "
                                     "Hover a button for its name.")
        self.icons_toggle.toggled.connect(self.set_icon_only)
        if self.health is None:
            row.addWidget(self.icons_toggle)
        else:
            self.icons_toggle.setVisible(False)
        for text, slot in (("Reset order", self.reset_order),
                           ("Copy report", self._copy_report)):
            button = QtWidgets.QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)
        outer.addLayout(row)

    # -- tabs ------------------------------------------------------------
    def _build_tool_tabs(self):
        if self.registry is None:
            return
        for tab_name, sections in self.registry.tools_by_tab().items():
            if not sections:
                continue
            page = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(page)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)

            # The strip appears only where something obeys it. A pivot
            # control above a tab of audits would be a promise nothing keeps.
            if any(t.uses_pivot for group in sections.values() for t in group):
                strip = PivotStrip(tab_name, self.prefs,
                                   on_change=self.save_prefs, panel=self)
                self._pivot_strips[tab_name] = strip
                layout.addWidget(strip)

            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(
                _enum(QtWidgets.QFrame, "Shape.NoFrame", "NoFrame"))
            holder = QtWidgets.QWidget()
            holder_layout = QtWidgets.QVBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.setSpacing(4)
            holder_layout.addStretch(1)
            scroll.setWidget(holder)
            layout.addWidget(scroll, 1)

            self._sections[tab_name] = (holder_layout, sections)
            self._populate(tab_name)
            self.tabs.addTab(page, tab_name)

    def _populate(self, tab_name):
        holder_layout, sections = self._sections[tab_name]
        while holder_layout.count() > 1:
            item = holder_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)

        order = (self.prefs.sections_for(tab_name, list(sections))
                 if self.prefs is not None else list(sections))
        for index, name in enumerate(order):
            tools = sections.get(name)
            if not tools:
                continue
            holder_layout.insertWidget(index,
                                       ToolSection(tab_name, name, tools,
                                                   self))

    def _build_info_tabs(self):
        """Environment, commands and log in ONE tab, as resizable panes.

        Three tabs for what is one question - "what is going on?" - meant
        switching tabs to read a log line against the command it was about.
        The log gets the most room because it is read the most.
        """
        split = QtWidgets.QSplitter(
            _enum(QtCore.Qt, "Orientation.Vertical", "Vertical"))

        def pane(title, widget):
            box = QtWidgets.QWidget()
            lay = QtWidgets.QVBoxLayout(box)
            lay.setContentsMargins(2, 2, 2, 2)
            lay.setSpacing(2)
            head = QtWidgets.QLabel(title)
            head.setStyleSheet("background: %s; color: %s; padding: 2px 6px;"
                               " font-weight: 600;"
                               % (COL["section"], COL["text"]))
            lay.addWidget(head)
            lay.addWidget(widget, 1)
            split.addWidget(box)

        self.env_view = QtWidgets.QTextBrowser()
        pane("Environment", self.env_view)
        self._fill_environment()

        self.cmd_tree = QtWidgets.QTreeWidget()
        self.cmd_tree.setHeaderLabels(["Tool", "Resolved", "Kind"])
        pane("Commands", self.cmd_tree)
        self._fill_commands()

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        pane("Log", self.log_view)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        split.setStretchFactor(2, 4)
        split.setSizes([110, 200, 420])
        # Anything logged before this tab existed - the action bar wiring,
        # any early failure - would otherwise be invisible in the one place
        # built to show it.
        for line in self.embed_log:
            self.log_view.appendPlainText(line)
        self.tabs.addTab(split, "Info")

    def _fill_environment(self):
        if self.env is None:
            self.env_view.setPlainText("No environment probe available.")
            return
        body = self.palette().color(
            _enum(QtGui.QPalette, "ColorRole.WindowText", "WindowText")).name()
        rows = ["<table cellspacing='0' cellpadding='4'>"]
        for key, value in self.env.as_dict().items():
            colour = WARN_COLOUR if (key == "numpy" and not self.env.numpy) else body
            rows.append("<tr><td style='color:%s'>%s</td>"
                        "<td style='color:%s'><b>%s</b></td></tr>"
                        % (MUTED, key.replace("_", " "), colour, value))
        rows.append("</table>")
        self.env_view.setHtml("".join(rows))

    def _fill_commands(self):
        self.cmd_tree.clear()
        for group, rows in self.resolved.items():
            found = sum(1 for r in rows if r["resolved"])
            parent = QtWidgets.QTreeWidgetItem(
                self.cmd_tree, ["%s  (%d/%d)" % (group, found, len(rows)),
                                "", ""])
            font = parent.font(0)
            font.setBold(True)
            parent.setFont(0, font)
            for record in rows:
                child = QtWidgets.QTreeWidgetItem(
                    parent, [record["label"],
                             record["resolved"] or "-- not available --",
                             record["kind"] or ""])
                if not record["resolved"]:
                    child.setForeground(
                        1, QtGui.QBrush(QtGui.QColor(FAIL_COLOUR)))
            parent.setExpanded(True)

    def _build_counters(self):
        """Units, pairs, stacks, fixed — kept visible whatever tab is open.

        They are the numbers that decide whether a pack is worth running, so
        they sit outside the tabs. They read "—" until Analyze has run: a
        counter showing 0 before anything was measured is a claim, and a
        wrong one.
        """
        holder = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(holder)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(4)
        self.counters = OrderedDict()
        for key, label in (("units", "units"), ("pairs", "pairs"),
                           ("stacks", "stacks"), ("fixed", "fixed")):
            cell = QtWidgets.QLabel("\u2014\n%s" % label)
            cell.setAlignment(
                _enum(QtCore.Qt, "AlignmentFlag.AlignCenter", "AlignCenter"))
            cell.setStyleSheet(
                "background: rgba(0,0,0,50); border-radius: 3px;"
                " padding: 4px; color: %s;" % MUTED)
            self.counters[key] = cell
            row.addWidget(cell, 1)
        return holder

    def _set_counters(self, detail):
        if not isinstance(detail, dict):
            return
        for key, cell in self.counters.items():
            value = detail.get(key)
            cell.setText("%s\n%s" % ("\u2014" if value is None else value,
                                     key))
            cell.setStyleSheet(
                "background: rgba(0,0,0,50); border-radius: 3px;"
                " padding: 4px; color: %s;"
                % (MUTED if value is None else "#e0e0e0"))

    def _build_action_bar(self):
        """Analyze, Preview, Pack, Check — reachable from every tab.

        Pack is the only one that writes, and is the only one styled as a
        primary action. Giving the read-only three the same weight would
        make the destructive one no easier to pick out than the safe ones.
        """
        holder = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        # Kept on the panel so the buttons cannot be collected while the
        # layout still shows them.
        self._action_buttons = []
        wired = []
        if self.registry is None:
            return holder
        for tool_id in getattr(self.registry, "ACTION_BAR", []):
            try:
                tool = self.registry.find_tool(tool_id)
            except Exception:
                continue
            button = QtWidgets.QPushButton(tool.label)
            button.setFixedHeight(ROW_HEIGHT + 6)
            button.setToolTip(tool.tooltip or tool.label)
            writes = tool.fidelity != "none"
            if writes:
                button.setStyleSheet(
                    "background: #3f6d8a; color: #ffffff; font-weight: 500;")
            available, reason = self.availability(tool)
            if not available:
                button.setEnabled(False)
                button.setToolTip("Unavailable: %s" % reason)
            # The tool id rides on the button and is read back from the
            # sender, rather than captured in a lambda. The section buttons
            # connect to a bound method and demonstrably fire in Maya; the
            # lambda here did not, and a connection that silently never
            # fires is not worth diagnosing when the working pattern is
            # three lines away.
            button.setProperty("uvs_tool", tool_id)
            button.clicked.connect(self._action_clicked)
            self._action_buttons.append(button)
            wired.append(tool_id)
            row.addWidget(button, 2 if writes else 1)
        self.log("INFO", "Action bar wired: %s" % (", ".join(wired) or "none"))
        return holder

    def _action_clicked(self):
        """One slot for every action-bar button."""
        sender = self.sender()
        tool_id = sender.property("uvs_tool") if sender is not None else None
        if not tool_id:
            self.log("ERROR", "An action button fired with no tool attached.")
            return
        self.run_tool_id(tool_id)

    # -- tool plumbing ----------------------------------------------------
    def glyph_for(self, tool):
        """The icon for a tool, never None when icons are available.

        Order: an explicit drawn glyph (align/distribute), then Maya's own
        icon found by keyword search, then a drawn glyph for the tool or its
        section. The choice is recorded for the Debug report.
        """
        reg = self.registry
        cache = self.__dict__.setdefault("_icon_cache", {})
        if tool.tool_id in cache:
            return cache[tool.tool_id][0]
        icon, source = None, "none"
        kind = (getattr(reg, "TOOL_GLYPHS", None) or {}).get(tool.tool_id)
        if kind:
            icon, source = _icon(kind), "drawn:%s" % kind
        if icon is None:
            groups = (getattr(reg, "TOOL_ICON_SEARCH", None) or {}).get(
                tool.tool_id)
            name = find_maya_icon(groups) if groups else None
            if name:
                # Test the IMAGE, not the QIcon: under PySide2 (Maya 2022)
                # QIcon("missing.png").isNull() is False, so a bad path gave
                # a blank button instead of the drawn fallback.
                pixmap = QtGui.QPixmap(name)
                if not pixmap.isNull():
                    icon, source = QtGui.QIcon(pixmap), "maya:%s" % name
        if icon is None:
            kind = ((getattr(reg, "TOOL_GLYPH_FALLBACK", None) or {}).get(
                tool.tool_id)
                or (getattr(reg, "SECTION_GLYPHS", None) or {}).get(
                    (tool.tab, tool.section)))
            if kind:
                icon, source = _icon(kind), "drawn:%s" % kind
        cache[tool.tool_id] = (icon, source)
        return icon

    def short_label(self, tool):
        """Two-letter code shown in icon-only mode when a tool's icon is a
        shared fallback - otherwise Cut, Sew and Split are three identical
        buttons. Initials of the meaningful words, else the first letters."""
        self.glyph_for(tool)
        source = self._icon_cache.get(tool.tool_id, (None, ""))[1]
        registry_kind = (getattr(self.registry, "TOOL_GLYPHS", None)
                         or {}).get(tool.tool_id)
        specific = (getattr(self.registry, "TOOL_GLYPH_FALLBACK", None)
                    or {}).get(tool.tool_id)
        # A code is needed whenever another visible tool on the same tab
        # shows the SAME icon - drawn fallback or Maya's (Pin, Invert Pins
        # and Select Pinned all use polyPinUV.png).
        shared = [t for t in self.registry.TOOLS
                  if not t.hidden and t.tab == tool.tab
                  and t.tool_id != tool.tool_id
                  and (self.glyph_for(t) is not None)
                  and self._icon_cache.get(t.tool_id, (None, ""))[1] == source]
        if registry_kind or not shared:
            return ""
        words = [w for w in tool.label.replace("-", " ").replace("(", " ")
                 .replace(")", " ").split()
                 if w.lower() not in ("and", "uvs", "uv", "the", "to")]
        if len(words) >= 2:
            return (words[0][0] + words[1][0]).upper()
        word = words[0] if words else tool.label
        return word[:2].capitalize()

    def icon_report(self):
        """{tool id: where its icon came from} for the Debug report."""
        if self.registry is None:
            return {}
        out = OrderedDict()
        for tool in self.registry.TOOLS:
            if tool.hidden:
                continue
            self.glyph_for(tool)
            out[tool.tool_id] = self._icon_cache.get(tool.tool_id,
                                                     (None, "none"))[1]
        return out

    @property
    def icon_only(self):
        return bool(self.prefs is not None and self.prefs.icon_only)

    def set_icon_only(self, on):
        """Icons only, or icon + name. Rebuilds the tabs; remembered."""
        if self.prefs is None:
            return
        self.prefs.icon_only = bool(on)
        self.save_prefs()
        for tab in list(self._sections):
            self._populate(tab)

    def section_layout(self, tab, name):
        layouts = getattr(self.registry, "SECTION_LAYOUTS", None) or {}
        return layouts.get((tab, name))

    def availability(self, tool):
        if tool.handler:
            if self.runner is None or tool.handler not in self.runner.handlers:
                return False, "handler %r not registered" % tool.handler
            return True, ""
        if self.runner is None or self.runner.bridge is None:
            return False, "no scene bridge"
        try:
            if not self.runner.bridge.cmd.available(tool.cmd_key):
                return False, "%s not on this Maya build" % tool.cmd_key
        except Exception:
            return False, "could not resolve %s" % tool.cmd_key
        return True, ""

    def resolved_name(self, tool):
        try:
            return self.runner.bridge.cmd.name_of(tool.cmd_key)
        except Exception:
            return None

    def settings_for(self, tool):
        if self.runner is None:
            return dict(tool.defaults)
        return self.runner.prefs.settings_for(tool)

    def remember(self, tool, values):
        """The one place a control's value reaches storage."""
        if self.runner is None:
            return
        merged = self.runner.prefs.settings_for(tool)
        merged.update(values)
        merged.pop("pivot", None)
        self.runner.prefs.remember(tool.tool_id, merged)
        self.save_prefs()

    def run_tool(self, tool):
        self.run_tool_id(tool.tool_id)

    def run_tool_id(self, tool_id, overrides=None):
        if self.runner is None:
            self.log("ERROR", "No runner attached")
            return
        # A pressed button that produces no log line is indistinguishable
        # from a button that was never connected. This makes that difference
        # visible without needing to reproduce it.
        self.log("INFO", "Running %s%s" % (tool_id,
                                           " with overrides" if overrides
                                           else ""))
        if not self._push_map_selection():
            self.log("WARN", "%s: the shells selected on the Cluster Map "
                             "could not be selected in Maya, so nothing was "
                             "run." % tool_id)
            return
        try:
            result = self.runner.run(tool_id, overrides)
        except Exception:
            self.log("ERROR", "%s raised:\n%s" % (tool_id,
                                                  traceback.format_exc()))
            return
        if result.ok:
            self.log("OK", "%s  %s" % (result.message,
                                       self._format_detail(result.detail)))
            for line in detail_lines(result.detail):
                self.log("DETAIL", line)
            if tool_id == "analyze":
                self._set_counters(result.detail)
            self._refresh_map_after(tool_id)
            if hasattr(self, "after_run"):
                self.after_run(tool_id, result)
            if (getattr(result, "step", None) is not None
                    and self.recipe_panel is not None):
                self.recipe_panel.refresh()
        else:
            self.log("WARN", result.message)

    @staticmethod
    def _push_map_selection():
        """Make the Cluster Map's selected shells Maya's selection.

        The map keeps its own selection and only handed it to Maya on a drag
        on the map. A panel button pressed while the map was showing ran
        against Maya's selection instead - the meshes that were selected to
        load the map - so Rotate turned every shell on them. Returns False
        only when the map has a selection that could not be pushed, so the
        caller runs nothing rather than the wrong target.
        """
        host = getattr(sys.modules.get("uvstudio_m1_hosted_editor"),
                       "_HOST", None)
        if host is None or getattr(host, "view", None) != "map":
            return True
        view = getattr(host, "map_widget", None)
        controller = getattr(view, "controller", None)
        if controller is None or not controller.selected_units():
            return True
        return bool(view._select_for_command())

    def _refresh_map_after(self, tool_id):
        """A tool that changed UVs makes the map's picture stale. Redraw it
        if the map is the view showing; otherwise just mark it stale, so the
        next switch to the map reloads instead of showing old positions."""
        try:
            tool = self.registry.find_tool(tool_id)
            if tool.fidelity == "none":
                return
            host = getattr(sys.modules.get("uvstudio_m1_hosted_editor"),
                           "_HOST", None)
            if host is None:
                return
            host._map_loaded_for = None
            if getattr(host, "view", None) == "map":
                host.reload_map(force=True)
        except Exception:
            pass

    @staticmethod
    def _format_detail(detail):
        if isinstance(detail, dict):
            return "  ".join("%s=%s" % (k, v) for k, v in detail.items()
                             if not isinstance(v, (list, dict)))
        return "" if detail is None else str(detail)[:120]

    # -- settings sheet ---------------------------------------------------
    def open_options(self, tool):
        """Open (or raise) the floating option box for a tool.

        One box per tool, remembered, so repeated clicks raise the existing
        window instead of stacking copies - Maya keeps a single option box
        per tool and artists expect the same.
        """
        cache = getattr(self, "_option_boxes", None)
        if cache is None:
            cache = self._option_boxes = {}
        box = cache.get(tool.tool_id)
        if box is None:
            box = cache[tool.tool_id] = ToolOptionBox(tool, self, self)
        box.show()
        box.raise_()
        box.activateWindow()

    def open_settings(self, tool, row=None):
        # The right-click "Settings..." entry now opens the same floating
        # option box the row's option-box icon opens - one settings surface,
        # not two that could drift.
        self.open_options(tool)

    def reset_tool(self, tool, row=None):
        if self.runner is None:
            return
        self.runner.prefs.tool_settings.pop(tool.tool_id, None)
        self.save_prefs()
        if row is not None:
            row.refresh()
        self.log("INFO", "%s reset to defaults." % tool.label)

    def copy_settings(self, tool):
        if self.runner is None:
            return
        self.clipboard = self.runner.prefs.settings_for(tool)
        self.log("INFO", "Copied %s settings." % tool.label)

    def paste_settings(self, tool, row=None):
        if not self.clipboard:
            return
        # Only keys this tool actually has. Pasting a packer's tile list onto
        # a rotate would store a setting nothing reads and quietly grow the
        # prefs file with junk.
        shared = dict((k, v) for k, v in self.clipboard.items()
                      if k in tool.defaults)
        if not shared:
            self.log("WARN", "Nothing in common with %s." % tool.label)
            return
        self.remember(tool, shared)
        if row is not None:
            row.refresh()
        self.log("INFO", "Pasted %d setting(s) into %s."
                 % (len(shared), tool.label))

    # -- sections ----------------------------------------------------------
    def move_section(self, tab, section, delta):
        if self.prefs is None:
            return
        self.prefs.move_section(tab, section, delta)
        self._populate(tab)
        self.save_prefs()

    def reset_order(self):
        if self.prefs is None:
            return
        self.prefs.reset_sections()
        for tab in list(self._sections):
            self._populate(tab)
        self.save_prefs()
        self.log("INFO", "Section order reset.")

    def save_prefs(self):
        if self.prefs is not None:
            self.prefs.save()

    def selection_bounds(self):
        """The UV bounding box of the current selection, for Edit Pivot.

        Best-effort: returns None when there is no runner, no analysis or
        nothing selected, and the dialog falls back to the 0-1 tile. It never
        raises - a pivot editor that dies because nothing is selected would
        be worse than one that opens on the whole tile.
        """
        m2 = sys.modules.get("uvstudio_m2_scene_bridge")
        if self.runner is None or self.runner.bridge is None or m2 is None:
            return None
        try:
            meshes = m2.selected_meshes()
            if not meshes:
                return None
            report = self.runner.bridge.analyse_many(meshes)
            shells = report.get("shells") or []
            if not shells:
                return None
            return (min(s.u_min for s in shells),
                    max(s.u_max for s in shells),
                    min(s.v_min for s in shells),
                    max(s.v_max for s in shells))
        except Exception:
            return None

    def confirm(self, question):
        """Ask before anything irreversible. Clearing a recipe and deleting a
        saved one are the only two; everything else is undoable."""
        answer = QtWidgets.QMessageBox.question(
            self, "UV Studio", question,
            _enum(QtWidgets.QMessageBox, "StandardButton.Yes", "Yes")
            | _enum(QtWidgets.QMessageBox, "StandardButton.No", "No"))
        return answer == _enum(QtWidgets.QMessageBox, "StandardButton.Yes",
                               "Yes")

    # -- log ---------------------------------------------------------------
    def log(self, level, message):
        """Record a line, whether or not the widgets exist yet.

        Construction order is not a contract anything else should have to
        know. The action bar is built before the Log tab, so logging from it
        raised AttributeError and took the whole panel down - a diagnostic
        that destroyed the thing it was diagnosing. The text is always kept;
        the widgets get it if they are there, and _build_info_tabs replays
        the backlog when the Log tab appears.
        """
        line = "[%s] %s" % (level, message)
        self.embed_log.append(line)
        view = getattr(self, "log_view", None)
        if view is not None:
            view.appendPlainText(line)
        for box in getattr(self, "_log_boxes", ()):
            try:
                box.append(line)
            except RuntimeError:
                pass                # the box's widget was deleted
        status = getattr(self, "status", None)
        if status is not None:
            status.setText(line)

    def set_verdict(self, ok, headline, detail=""):
        if self.verdict is None:
            return
        colour = OK_COLOUR if ok else FAIL_COLOUR
        self.verdict.setText("<b style='color:%s'>%s</b><br>%s"
                             % (colour, headline, detail))

    def set_log(self, entries):
        for entry in entries or []:
            self.log_view.appendPlainText(str(entry))

    def _copy_report(self):
        clip = QtWidgets.QApplication.clipboard()
        clip.setText("\n".join(self.embed_log))
        self.log("INFO", "Log copied to the clipboard.")


# =====================================================================
# SECTION 6b - Redesign v2: tabs drawn from M5's PANEL_LAYOUT
# =====================================================================

GUTTER = 64


class LayoutSection(_Widget):
    """One bordered, collapsible section: a header bar and its rows.

    Double-click the header or single-click the arrow to open and close;
    the state is remembered per tab and section, as before.
    """

    def __init__(self, panel, tab, title, rows, parent=None):
        super(LayoutSection, self).__init__(parent)
        self.panel, self.tab, self.title = panel, tab, title
        self.setObjectName("uvsSection")
        self.setStyleSheet("#uvsSection { border: 1px solid %s; border-radius: 3px;"
                           " background: %s; }" % (COL["border"], "#363636"))
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QtWidgets.QWidget()
        header.setFixedHeight(24)
        header.setStyleSheet("background: %s; border-bottom: 1px solid %s;"
                             % (COL["section"], COL["border"]))
        row = QtWidgets.QHBoxLayout(header)
        row.setContentsMargins(6, 0, 6, 0)
        self.arrow = QtWidgets.QToolButton()
        self.arrow.setAutoRaise(True)
        self.arrow.setText("\u25be")
        self.arrow.clicked.connect(self.toggle)
        row.addWidget(self.arrow)
        name = QtWidgets.QLabel(title)
        name.setStyleSheet("font-weight: 600; color: %s;" % COL["text"])
        row.addWidget(name)
        row.addStretch(1)
        header.installEventFilter(self)
        name.installEventFilter(self)
        outer.addWidget(header)
        self.body = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(self.body)
        body.setContentsMargins(8, 8, 8, 8)
        body.setSpacing(6)
        for kind, a, cells in rows:
            body.addLayout(panel.build_row(kind, a, cells))
        outer.addWidget(self.body)
        if panel.prefs is not None and panel.prefs.is_collapsed(tab, title):
            self.body.setVisible(False)
            self.arrow.setText("\u25b8")

    def eventFilter(self, obj, event):
        if event.type() == _enum(QtCore.QEvent, "Type.MouseButtonDblClick",
                                 "MouseButtonDblClick"):
            self.toggle()
            return True
        return False

    def toggle(self):
        show = not self.body.isVisible()
        self.body.setVisible(show)
        self.arrow.setText("\u25be" if show else "\u25b8")
        if self.panel.prefs is not None:
            self.panel.prefs.set_collapsed(self.tab, self.title, not show)
            self.panel.save_prefs()


class _PanelLogHandler(logging.Handler):
    """Forwards the "uvstudio" logger into the panel log while Details is on.

    The modules log what they deliberately skip - a mesh whose UVs could not
    be read, a map reload that failed, a press with nothing selected - at
    DEBUG, which prints nothing by default. This is how those lines reach
    the panel without the artist typing into the Script Editor.
    """

    MARKER = "_uvstudio_panel_handler"

    def __init__(self):
        super(_PanelLogHandler, self).__init__(logging.DEBUG)
        setattr(self, self.MARKER, True)
        self.panel = None

    def emit(self, record):
        panel = self.panel
        if panel is None:
            return
        try:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + "".join(
                    traceback.format_exception(*record.exc_info)).rstrip()
            panel.log("DEBUG", text)
        except Exception:
            pass            # a deleted panel must not break the tool logging


def set_debug_target(panel):
    """Send "uvstudio" DEBUG records to `panel`, or stop (panel=None).

    The handler is found by a marker attribute rather than by class, because
    each paste of the bundle defines the class afresh and an old handler
    from the previous paste would otherwise stay attached. Propagation is
    switched off while it is on so Maya's own root handler does not echo
    every line into the Script Editor.
    """
    logger = logging.getLogger("uvstudio")
    handlers = [h for h in logger.handlers
                if getattr(h, _PanelLogHandler.MARKER, False)]
    if panel is None:
        for handler in handlers:
            logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)
        logger.propagate = True
        return
    for handler in handlers[1:]:
        logger.removeHandler(handler)
    handler = handlers[0] if handlers else None
    if handler is None:
        handler = _PanelLogHandler()
        logger.addHandler(handler)
    handler.panel = panel
    logger.setLevel(logging.DEBUG)
    logger.propagate = False


def detail_lines(detail, limit=12):
    """Readable lines for the list and dict values in a tool's result.

    The one-line summary after a press leaves lists out, which for a check
    is the whole answer: which shells overlap, which are flipped. These go
    to the log under it, first `limit` entries each.
    """
    lines = []
    if not isinstance(detail, dict):
        return lines
    for key, value in detail.items():
        if isinstance(value, dict):
            entries = ["%s=%s" % (k, v) for k, v in value.items()]
        elif isinstance(value, (list, tuple, set)):
            entries = [str(v) for v in value]
        else:
            continue
        if not entries:
            lines.append("%s: none" % key)
            continue
        shown = ", ".join(entries[:limit])
        more = len(entries) - limit
        lines.append("%s: %d - %s%s" % (key, len(entries), shown[:400],
                                        " (+%d more)" % more if more > 0
                                        else ""))
    return lines


class LogBox(_Widget):
    """The panel log, on the Checks tab where audits are run.

    Shows the same lines as Info > Log - every press, its result and
    findings, every warning - in the tab the artist is looking at. Warnings
    only filters to problems; Details adds the DEBUG lines the modules log
    about what they skipped.
    """

    MIN_HEIGHT = 240

    def __init__(self, panel, parent=None):
        super(LogBox, self).__init__(parent)
        self.panel = panel
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.view = QtWidgets.QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMinimumHeight(self.MIN_HEIGHT)
        lay.addWidget(self.view, 1)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        self.problems_only = QtWidgets.QCheckBox("Warnings only")
        self.problems_only.toggled.connect(self.refill)
        self.details = QtWidgets.QCheckBox("Details")
        self.details.setToolTip("Also show what the tools skipped and why "
                                "(unreadable meshes, empty selections).")
        self.details.toggled.connect(self._set_details)
        copy = QtWidgets.QPushButton("Copy")
        copy.setToolTip("Copy the whole log to the clipboard.")
        copy.clicked.connect(self._copy)
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(self._clear)
        for widget in (self.problems_only, self.details):
            row.addWidget(widget)
        row.addStretch(1)
        for button in (copy, clear):
            button.setFixedHeight(ROW_HEIGHT)
            row.addWidget(button)
        lay.addLayout(row)
        self.setMinimumHeight(self.MIN_HEIGHT + ROW_HEIGHT + 8)
        self.refill()

    def accepts(self, line):
        if not self.problems_only.isChecked():
            return True
        return line.startswith(("[WARN]", "[ERROR]"))

    def append(self, line):
        if self.accepts(line):
            self.view.appendPlainText(line)

    def refill(self, *_):
        self.view.clear()
        for line in self.panel.embed_log:
            self.append(line)

    def _copy(self):
        QtWidgets.QApplication.clipboard().setText(
            "\n".join(self.panel.embed_log))

    def _clear(self):
        del self.panel.embed_log[:]
        self.view.clear()
        info = getattr(self.panel, "log_view", None)
        if info is not None:
            info.clear()

    def _set_details(self, on):
        set_debug_target(self.panel if on else None)
        self.panel.log("INFO", "Details %s" % ("on" if on else "off"))


class HealthStrip(_Widget):
    """Units, pairs, stacks, pins and the audits, on every tab.

    The grouping engine (Analyze) runs by itself when the selected meshes
    change and after any tool that edits UVs, so the numbers are never
    stale; Re-analyze forces it. Each chip runs its own check on click.
    """

    CHIPS = (("units", "Units", "analyze"), ("pairs", "Pairs", "analyze"),
             ("stacks", "Stacks", "analyze"), ("fixed", "Pinned", "analyze"),
             ("overlaps", "Overlaps", "check_overlaps"),
             ("outside", "Outside", "check_bounds"),
             ("flipped", "Flipped", "check_flipped"),
             ("density", "Density", "check_density"))

    def __init__(self, panel, parent=None):
        super(HealthStrip, self).__init__(parent)
        self.panel = panel
        self.values = {}
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(0, 4, 0, 0)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(4)
        self.buttons = OrderedDict()
        for i, (key, label, tool_id) in enumerate(self.CHIPS):
            button = QtWidgets.QPushButton("%s \u2014" % label)
            button.setToolTip("Runs %s" % tool_id)
            button.clicked.connect(lambda _c=False, t=tool_id: panel.run_tool_id(t))
            grid.addWidget(button, i // 4, i % 4)
            self.buttons[key] = (button, label)
        again = QtWidgets.QPushButton("Re-analyze")
        again.setToolTip("Run the grouping engine and the audits now.")
        again.clicked.connect(lambda: self.refresh(force=True))
        grid.addWidget(again, 2, 0, 1, 4)
        self._meshes = None
        self.dirty = True
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1500)

    def _tick(self):
        if not self.isVisible():
            return
        m2 = sys.modules.get("uvstudio_m2_scene_bridge")
        try:
            meshes = tuple(sorted(m2.selected_meshes())) if m2 else ()
        except Exception:
            meshes = ()
        if meshes and (meshes != self._meshes or self.dirty):
            self._meshes = meshes
            self.refresh()

    def refresh(self, force=False):
        runner = self.panel.runner
        if runner is None:
            return
        self.dirty = False
        found = {}
        for tool_id, keys in (("analyze", ("units", "pairs", "stacks",
                                           "fixed")),
                              ("check_overlaps", ("overlapping_pairs",)),
                              ("check_bounds", ("out_of_bounds",))):
            try:
                result = runner.run(tool_id, record=False, quiet=True)
            except Exception:
                continue
            if result.ok and isinstance(result.detail, dict):
                for key in keys:
                    found[key] = result.detail.get(key)
        found["overlaps"] = found.pop("overlapping_pairs", None)
        found["outside"] = found.pop("out_of_bounds", None)
        self.values.update(found)
        self.show_values()
        self.panel.refresh_suggestions()

    def show_values(self):
        for key, (button, label) in self.buttons.items():
            value = self.values.get(key)
            bad = key in ("overlaps", "outside", "flipped") and value
            button.setText("%s %s" % (label, "\u2014" if value is None
                                      else value))
            button.setStyleSheet("color: %s;" % (WARN_COLOUR if bad
                                                 else COL["text"]))


def _build_layout_tabs(self):
    """Draw every tab from registry.PANEL_LAYOUT (redesign v2)."""
    layout = self.registry.PANEL_LAYOUT
    self._bound = {}
    self._layout_pages = {}
    order = [t for t in layout if t != "Debug"]
    for tab in order + ["Recipe", "Info", "Debug"]:
        if tab == "Recipe":
            self.recipe_panel = RecipePanel(self)
            self.tabs.addTab(self.recipe_panel, "Recipe")
            continue
        if tab == "Info":
            self._build_info_tabs()
            continue
        if tab not in layout:
            continue
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(_enum(QtWidgets.QFrame, "Shape.NoFrame",
                                   "NoFrame"))
        holder = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(holder)
        column.setContentsMargins(4, 4, 4, 4)
        column.setSpacing(8)
        for title, rows in layout[tab]:
            column.addWidget(LayoutSection(self, tab, title, rows))
        column.addStretch(1)
        scroll.setWidget(holder)
        self._layout_pages[tab] = scroll
        self.tabs.addTab(scroll, tab)
    self.tabs.currentChanged.connect(self._tab_changed)


def _build_row(self, kind, a, cells):
    """One row: a gutter label and weighted cells; or a grid; or a note."""
    if kind == "note":
        box = QtWidgets.QHBoxLayout()
        note = QtWidgets.QLabel(a)
        note.setWordWrap(True)
        note.setStyleSheet("color: %s; border: 1px solid %s; padding: 5px;"
                           " border-radius: 3px;" % (MUTED, COL["border"]))
        box.addWidget(note)
        return box
    if kind == "grid":
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        for i, cell in enumerate(cells):
            grid.addWidget(_shrinkable(self.build_cell(cell, grid=True)),
                           i // a, i % a)
        for c in range(a):
            grid.setColumnStretch(c, 1)
        return grid
    row = QtWidgets.QHBoxLayout()
    row.setSpacing(6)
    gutter = QtWidgets.QLabel(a or "")
    gutter.setFixedWidth(GUTTER)
    gutter.setAlignment(_enum(QtCore.Qt, "AlignmentFlag.AlignRight",
                              "AlignRight")
                        | _enum(QtCore.Qt, "AlignmentFlag.AlignVCenter",
                                "AlignVCenter"))
    gutter.setStyleSheet("color: %s;" % COL["text"])
    row.addWidget(gutter)
    for cell in cells:
        row.addWidget(_shrinkable(self.build_cell(cell)),
                      max(1, int(cell["w"] * 10)))
    return row


def _compact_pivot(strip):
    """Re-seat the pivot strip's own widgets for a narrow panel.

    The strip lays its four modes, U/V fields and Edit/Reset in one line,
    which clipped every label at panel width. Same widgets, same stored
    pivot, new arrangement:
        Shell    Selection  Edit Pivot
        UV area  Custom     Reset
        U [0.5000]   V [0.5000]
    """
    buttons = list(strip._mode_buttons.values())
    extra = [b for b in strip.custom_row.findChildren(QtWidgets.QPushButton)]
    grid = QtWidgets.QGridLayout()
    grid.setHorizontalSpacing(4)
    grid.setVerticalSpacing(4)
    cells = [buttons[0], buttons[1]] + extra[:1] + [buttons[2], buttons[3]] \
        + extra[1:2]
    for i, w in enumerate(cells):
        w.setParent(None)
        _shrinkable(w)
        grid.addWidget(w, i // 3, i % 3)
    for c in range(3):
        grid.setColumnStretch(c, 1)
    right = strip.custom_row.parentWidget().layout() if False else None
    uv_row = strip.custom_row.layout()
    for i in reversed(range(uv_row.count())):
        item = uv_row.itemAt(i)
        if item.widget() is None:
            uv_row.takeAt(i)                # drop the old trailing stretch
    for box in (strip._u_box, strip._v_box):
        _shrinkable(box)
        uv_row.setStretch(uv_row.indexOf(box), 1)
    column = None
    for lay in strip.findChildren(QtWidgets.QVBoxLayout):
        if lay.indexOf(strip.custom_row) >= 0:
            column = lay
    if column is not None:
        column.insertLayout(0, grid)
    return strip


def _shrinkable(widget):
    """Let a cell take its weighted share even when narrower than its text.

    Qt sizes buttons, spin boxes and combos to their contents by default,
    so a row of them overflowed a 400 px panel and the right edge was cut
    off. Ignored lets the row's stretch weights decide.
    """
    if not isinstance(widget, (QtWidgets.QLabel, PivotStrip, LogBox)):
        widget.setSizePolicy(
            _enum(QtWidgets.QSizePolicy, "Policy.Ignored", "Ignored"),
            _enum(QtWidgets.QSizePolicy, "Policy.Fixed", "Fixed"))
        widget.setMinimumWidth(22)
    return widget


def _bind(self, tool_id, setter):
    self._bound.setdefault(tool_id, []).append(setter)


def _refresh_bound(self, tool_id):
    values = self.runner.prefs.settings_for(self.registry.find_tool(tool_id))
    for setter in self._bound.get(tool_id, []):
        try:
            setter(values)
        except RuntimeError:
            pass                    # widget already deleted


def _pick_value(self, pick):
    options = self.registry.PICKS.get(pick) or []
    stored = (self.prefs.tool_settings.get("__panel__", {}) if self.prefs
              else {}).get(pick)
    tools = [t for _l, t in options]
    return stored if stored in tools else (tools[0] if tools else None)


def _set_pick(self, pick, tool_id):
    if self.prefs is not None:
        self.prefs.tool_settings.setdefault("__panel__", {})[pick] = tool_id
        self.save_prefs()
    for setter in self._bound.get("__pick__" + pick, []):
        try:
            setter(tool_id)
        except RuntimeError:
            pass


def _build_cell(self, cell, grid=False):
    t = cell["t"]
    reg = self.registry
    h = ROW_HEIGHT
    if t == "label":
        label = QtWidgets.QLabel(cell["text"])
        label.setStyleSheet("color: %s;" % MUTED)
        label.setAlignment(_enum(QtCore.Qt, "AlignmentFlag.AlignCenter",
                                 "AlignCenter"))
        return label
    if t == "widget" and cell["name"] == "log":
        box = LogBox(self)
        self._log_boxes = list(getattr(self, "_log_boxes", [])) + [box]
        return box
    if t == "widget" and cell["name"] == "pivot":
        strip = PivotStrip("Layout", self.prefs, on_change=self.save_prefs,
                           panel=self)
        caption = strip.findChild(QtWidgets.QLabel)
        if caption is not None and caption.text() == "Pivot":
            caption.setVisible(False)       # the row's gutter already says it
        strip.layout().setContentsMargins(0, 0, 0, 0)
        _compact_pivot(strip)
        return strip
    if t in ("run", "runpick"):
        tool = reg.find_tool(cell["tool"]) if t == "run" else None
        text = cell.get("label")
        if tool is not None and text is None:
            text = tool.label
        button = QtWidgets.QPushButton(text or "")
        button.setFixedHeight(h + 2)
        icon = None
        if cell.get("icon"):
            icon = _icon(cell["icon"])
        elif tool is not None and grid:
            icon = self.glyph_for(tool)
        if icon is not None:
            button.setIcon(icon)
            button.setIconSize(QtCore.QSize(16, 16))
        if cell.get("primary"):
            button.setStyleSheet("background: %s; border-color: %s; color: #ffffff;"
                                 " font-weight: 600;" % (COL["accent"],
                                                         COL["accent_lo"]))
        if t == "run":
            available, reason = self.availability(tool)
            tip = [tool.label] + ([tool.tooltip] if tool.tooltip else [])
            if not available:
                button.setEnabled(False)
                tip.append("Unavailable: %s" % reason)
            tip.append("Right-click for options.")
            button.setToolTip("\n".join(tip))
            over = cell.get("over") or None
            button.clicked.connect(lambda _c=False, i=tool.tool_id, o=over:
                                   self.run_tool_id(i, o))
            button.setContextMenuPolicy(_enum(
                QtCore.Qt, "ContextMenuPolicy.CustomContextMenu",
                "CustomContextMenu"))
            button.customContextMenuRequested.connect(
                lambda _p, tl=tool: self.open_options(tl))
        else:
            pick = cell["pick"]

            def fire(_c=False, p=pick, opts=cell.get("options")):
                tool_id = self.pick_value(p)
                if tool_id is None:
                    return
                if opts:
                    self.open_options(reg.find_tool(tool_id))
                else:
                    self.run_tool_id(tool_id)
            button.clicked.connect(fire)
        return button
    if t == "pick":
        combo = QtWidgets.QComboBox()
        combo.setFixedHeight(h)
        for label, tool_id in cell["options"]:
            combo.addItem(label, tool_id)
        current = self.pick_value(cell["pick"])
        index = max(0, combo.findData(current))
        combo.setCurrentIndex(index)
        combo.currentIndexChanged.connect(
            lambda i, c=combo, p=cell["pick"]: self.set_pick(p, c.itemData(i)))
        self.bind("__pick__" + cell["pick"],
                  lambda tool_id, c=combo: c.setCurrentIndex(
                      max(0, c.findData(tool_id))))
        return combo
    tool = reg.find_tool(cell["tool"])
    key = cell["key"]
    values = self.settings_for(tool)

    def store(value, tl=tool, k=key):
        self.remember(tl, {k: value})
    if t == "num":
        box = QtWidgets.QDoubleSpinBox()
        box.setDecimals(cell.get("dec", 4))
        box.setRange(-1.0e6, 1.0e6)
        box.setButtonSymbols(_enum(QtWidgets.QAbstractSpinBox,
                                   "ButtonSymbols.NoButtons", "NoButtons"))
        box.setFixedHeight(h)
        whole = cell.get("dec", 4) == 0

        def load(vals, b=box, k=key):
            b.blockSignals(True)
            b.setValue(float(vals.get(k) or 0.0))
            b.blockSignals(False)
        load(values)
        box.editingFinished.connect(
            lambda b=box: store(int(b.value()) if whole else b.value()))
        self.bind(tool.tool_id, load)
        return box
    if t == "text":
        edit = QtWidgets.QLineEdit()
        edit.setFixedHeight(h)
        tiles = isinstance(tool.defaults.get(key), list)

        def load(vals, e=edit, k=key):
            v = vals.get(k)
            e.setText(format_tiles(v) if tiles else ("" if v is None else str(v)))
        load(values)
        edit.editingFinished.connect(
            lambda e=edit: store(parse_tiles(e.text(), fallback=[1001])
                                 if tiles else e.text()))
        self.bind(tool.tool_id, load)
        return edit
    if t == "tog":
        button = QtWidgets.QPushButton(cell["label"])
        button.setCheckable(True)
        button.setFixedHeight(h)
        button.setChecked(bool(values.get(key)))
        button.toggled.connect(lambda on: store(bool(on)))
        self.bind(tool.tool_id, lambda vals, b=button, k=key: (
            b.blockSignals(True), b.setChecked(bool(vals.get(k))),
            b.blockSignals(False)))
        return button
    if t == "check":
        box = QtWidgets.QCheckBox(cell["label"])
        box.setChecked(bool(values.get(key)))
        box.toggled.connect(lambda on: store(bool(on)))
        self.bind(tool.tool_id, lambda vals, b=box, k=key: (
            b.blockSignals(True), b.setChecked(bool(vals.get(k))),
            b.blockSignals(False)))
        return box
    if t == "choice":
        combo = QtWidgets.QComboBox()
        combo.setFixedHeight(h)
        for label, value in cell["options"]:
            combo.addItem(label, value)
        combo.setCurrentIndex(max(0, combo.findData(values.get(key))))
        combo.currentIndexChanged.connect(
            lambda i, c=combo: store(c.itemData(i)))
        return combo
    return QtWidgets.QLabel("?")


def _build_search(self):
    """Search every command by name - UV Studio's or Maya's."""
    row = QtWidgets.QHBoxLayout()
    row.setSpacing(4)
    self.search = QtWidgets.QLineEdit()
    self.search.setPlaceholderText("Search commands (Enter runs, "
                                   "Shift+Enter opens options)")
    self.search.setFixedHeight(ROW_HEIGHT + 2)
    self._search_index = OrderedDict()
    for tool in self.registry.TOOLS:
        name = None
        if tool.cmd_key:
            try:
                name = self.runner.bridge.cmd.name_of(tool.cmd_key)
            except Exception:
                name = None
        text = "%s  (%s%s)" % (tool.label, tool.tab,
                               ", " + name if name else "")
        self._search_index[text] = tool.tool_id
    completer = QtWidgets.QCompleter(list(self._search_index))
    completer.setCaseSensitivity(_enum(QtCore.Qt,
                                       "CaseSensitivity.CaseInsensitive",
                                       "CaseInsensitive"))
    try:
        completer.setFilterMode(_enum(QtCore.Qt, "MatchFlag.MatchContains",
                                      "MatchContains"))
    except Exception:
        pass
    completer.activated.connect(self._search_pick)
    self.search.setCompleter(completer)
    self.search.returnPressed.connect(
        lambda: self._search_pick(self.search.text()))
    row.addWidget(self.search, 1)
    return row


def _search_pick(self, text):
    tool_id = self._search_index.get(text)
    if tool_id is None:
        # Typed text rather than a picked completion: the part before any
        # "(Tab, command)" suffix, matched against names - exact first,
        # then a name that starts with it, then one that contains it.
        needle = text.split("  (")[0].strip().lower()
        entries = [(label.split("  (")[0].lower(), label.lower(), tid)
                   for label, tid in self._search_index.items()]
        for test in (lambda n, full: n == needle,
                     lambda n, full: n.startswith(needle),
                     lambda n, full: needle in full):
            hit = [tid for n, full, tid in entries if needle and test(n, full)]
            if hit:
                tool_id = hit[0]
                break
    if tool_id is None:
        self.log("WARN", "No command matches %r" % text)
        return
    shift = bool(QtWidgets.QApplication.keyboardModifiers()
                 & _enum(QtCore.Qt, "KeyboardModifier.ShiftModifier",
                         "ShiftModifier"))
    QtCore.QTimer.singleShot(0, self.search.clear)
    if shift:
        self.open_options(self.registry.find_tool(tool_id))
    else:
        self.run_tool_id(tool_id)


def _build_suggestions(self):
    row = QtWidgets.QHBoxLayout()
    row.setSpacing(4)
    self.suggest_label = QtWidgets.QLabel("Next")
    self.suggest_label.setStyleSheet("color: %s;" % MUTED)
    row.addWidget(self.suggest_label)
    self.suggest_buttons = []
    for _i in range(4):
        button = QtWidgets.QPushButton("")
        button.setFixedHeight(ROW_HEIGHT)
        button.setVisible(False)
        button.clicked.connect(self._suggestion_clicked)
        self.suggest_buttons.append(button)
        row.addWidget(button, 1)
    self._suggest_timer = QtCore.QTimer(self)
    self._suggest_timer.timeout.connect(self.refresh_suggestions)
    self._suggest_timer.start(1200)
    return row


def _suggestion_clicked(self):
    tool_id = self.sender().property("uvs_tool")
    if tool_id:
        self.run_tool_id(tool_id)


def _refresh_suggestions(self):
    if not self.isVisible() or not self.suggest_buttons:
        return
    try:
        kinds, _m = self.runner.guard.classify(self.runner.guard.provider())
    except Exception:
        kinds = set()
    health = getattr(self, "health", None)
    ids = self.registry.suggest(kinds, getattr(self, "_last_tool", None),
                                health.values if health else {})
    for i, button in enumerate(self.suggest_buttons):
        if i < len(ids):
            tool = self.registry.find_tool(ids[i])
            button.setText(tool.label)
            button.setProperty("uvs_tool", tool.tool_id)
            button.setToolTip(tool.tooltip or tool.label)
            button.setVisible(True)
        else:
            button.setVisible(False)


def _tab_changed(self, index):
    """Opening Groups or Texture saves the original layout, once."""
    name = self.tabs.tabText(index)
    if name not in ("Groups", "Texture") or self.runner is None:
        return
    m2 = sys.modules.get("uvstudio_m2_scene_bridge")
    try:
        if m2 is None or not m2.selected_meshes():
            return
    except Exception:
        return
    result = self.runner.run("texture_snapshot", {"only_if_missing": True},
                             record=False, quiet=True)
    detail = result.detail if isinstance(result.detail, dict) else {}
    if result.ok and detail.get("remembered"):
        self.log("OK", "Saved the original layout of %d mesh(es) for texture "
                       "transfer." % detail["remembered"])


def _after_run(self, tool_id, result):
    """Follow-ups that need the panel: Get fills Set, and freshness."""
    self._last_tool = tool_id
    detail = result.detail if isinstance(result.detail, dict) else {}
    if tool_id in ("density_get", "get_density") and "density" in detail:
        tool = self.registry.find_tool("set_density")
        self.remember(tool, {"density": detail["density"],
                             "mapSize": detail.get("map_size", 512)})
        self.refresh_bound("set_density")
    try:
        if self.registry.find_tool(tool_id).fidelity != "none" and \
                getattr(self, "health", None) is not None:
            self.health.dirty = True
    except Exception:
        pass
    self.refresh_suggestions()


for _name, _fn in (("build_layout_tabs", _build_layout_tabs),
                   ("build_row", _build_row), ("build_cell", _build_cell),
                   ("bind", _bind), ("refresh_bound", _refresh_bound),
                   ("pick_value", _pick_value), ("set_pick", _set_pick),
                   ("build_search", _build_search),
                   ("_search_pick", _search_pick),
                   ("build_suggestions", _build_suggestions),
                   ("_suggestion_clicked", _suggestion_clicked),
                   ("refresh_suggestions", _refresh_suggestions),
                   ("_tab_changed", _tab_changed),
                   ("after_run", _after_run)):
    setattr(UVStudioToolsPanel, _name, _fn)


# =====================================================================
# SECTION 8 - Self test (pure logic only; no Qt required)
# =====================================================================

def run_self_test():
    failures = []

    def check(label, got, want):
        ok = got == want
        print("%-56s %s  (got %r)" % (label, "PASS" if ok else "FAIL", got))
        if not ok:
            failures.append(label)

    print("=" * 80)
    print("UV Studio M5c - Panel self test (v%s)" % __version__)
    print("=" * 80)
    print("--- tile parsing ---")
    check("a range expands", parse_tiles("1001-1004"),
          [1001, 1002, 1003, 1004])
    check("mixed ranges and singles",
          parse_tiles("1001-1002, 1011"), [1001, 1002, 1011])
    check("round trip", format_tiles(parse_tiles("1001-1004, 1011")),
          "1001-1004, 1011")
    check("junk falls back rather than silently meaning 1001",
          parse_tiles("wat", fallback=[1001]), [1001])
    check("a backwards range is junk", parse_tiles("1004-1001",
                                                   fallback=[1001]), [1001])
    check("more than 64 tiles is junk",
          parse_tiles("1001-1200", fallback=[1001]), [1001])

    print("\n--- literal parsing ---")
    check("a list parses", parse_literal("[1, 2]", "text"), "[1, 2]")
    check("  when the field is not text", parse_literal("[1, 2]", "float"),
          [1, 2])
    check("text stays text", parse_literal("area", "float"), "area")
    unsafe = parse_literal("__import__('os').getcwd()", "float")
    check("code is NOT executed", unsafe, "__import__('os').getcwd()")

    print("")
    print("=" * 80)
    print("%d failure(s)" % len(failures))
    for name in failures:
        print("  FAILED: %s" % name)
    print("=" * 80)
    return not failures


if __name__ == "__main__":
    run_self_test()
