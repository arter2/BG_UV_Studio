"""
UV Studio - Module 6: Texture transfer (PROTOTYPE)
==================================================

PURPOSE
    When shells are moved, rotated, flipped or rescaled by the packer, the
    texture painted for the ORIGINAL layout no longer lines up. This module
    re-lays the texture's pixels to follow the shells into the new layout.

WHY A 2D WARP AND NOT MAYA'S TRANSFER MAPS
    Maya's native route (Transfer Maps / surfaceSampler) is a 3D render bake:
    two meshes, a lighting-neutral material, a render pass. Our case is
    exact and 2D - every shell underwent a known transform of its UVs - so
    its pixels can be moved by the same transform, losslessly, with no
    rendering and no lighting contamination.

HOW (all heavy work in C++, via Qt - numpy is not available in Maya 2022)
    1. Per shell, fit the transform old-UV -> new-UV from corresponding
       face-vertices (valid even after cuts, because both UV sets index the
       same face-vertices). A full 2x3 affine covers move, rotate, uniform
       and non-uniform scale, and flips.
    2. If one affine explains the shell to within `tolerance_px`, the shell
       is drawn with ONE QPainter.drawImage through that transform, clipped
       to the shell's new outline. Shells that were unfolded or relaxed are
       not affine; they fall back to one draw per triangle (slower, exact).
    3. Clipping uses the outline grown by `padding_px`, so the original
       bleed around each shell travels with it - no hard edges when the
       texture is mip-mapped.

STATUS
    Pure-Qt core, tested offscreen under PySide2 5.15.2 (Maya 2022's Qt) and
    PySide6. Not yet wired into Maya: file-node discovery, UDIM tiles and
    image I/O beyond Qt's formats are designed but unbuilt.
"""

from __future__ import division

import glob
import math
import os
import re
import shutil
import subprocess
import tempfile

__version__ = "0.5.0"
MODULE_ID = "M6"

try:
    from PySide6 import QtCore, QtGui
except ImportError:
    try:
        from PySide2 import QtCore, QtGui
    except ImportError:
        QtCore = QtGui = None


# =====================================================================
# SECTION 1 - Transform fitting (pure Python)
# =====================================================================

def fit_affine(src, dst):
    """Least-squares 2x3 affine mapping src points onto dst points.

    Returns ((a, b, c, d), (tx, ty), rms_error) where
        x' = a*x + b*y + tx,   y' = c*x + d*y + ty.
    None when the points are degenerate (fewer than 3, or collinear).
    """
    n = len(src)
    if n < 3 or n != len(dst):
        return None
    mx = sum(p[0] for p in src) / n
    my = sum(p[1] for p in src) / n
    nx = sum(q[0] for q in dst) / n
    ny = sum(q[1] for q in dst) / n
    sxx = sxy = syy = 0.0
    bx_x = bx_y = by_x = by_y = 0.0
    for (x, y), (u, v) in zip(src, dst):
        x -= mx
        y -= my
        u -= nx
        v -= ny
        sxx += x * x
        sxy += x * y
        syy += y * y
        bx_x += x * u
        bx_y += y * u
        by_x += x * v
        by_y += y * v
    det = sxx * syy - sxy * sxy
    if abs(det) < 1e-18 * max(1.0, sxx * syy):
        return None
    inv = (syy / det, -sxy / det, -sxy / det, sxx / det)
    a = inv[0] * bx_x + inv[1] * bx_y
    b = inv[2] * bx_x + inv[3] * bx_y
    c = inv[0] * by_x + inv[1] * by_y
    d = inv[2] * by_x + inv[3] * by_y
    tx = nx - (a * mx + b * my)
    ty = ny - (c * mx + d * my)
    err = 0.0
    for (x, y), (u, v) in zip(src, dst):
        ex = a * x + b * y + tx - u
        ey = c * x + d * y + ty - v
        err += ex * ex + ey * ey
    return (a, b, c, d), (tx, ty), math.sqrt(err / n)


def affine_scale(matrix):
    """Area scale of an affine's linear part (1 = same size)."""
    a, b, c, d = matrix
    return abs(a * d - b * c)


# =====================================================================
# SECTION 2 - Planning
# =====================================================================

class ShellWarp(object):
    """How one shell's pixels get from the old layout to the new one."""

    __slots__ = ("key", "mode", "matrix", "translation", "error_px",
                 "dst_faces", "src_faces", "order", "source")

    def __init__(self, key, mode, dst_faces, src_faces, matrix=None,
                 translation=None, error_px=0.0, order=0):
        self.key = key
        self.mode = mode                  # "affine" | "triangles"
        self.dst_faces = dst_faces        # [[(u, v), ...], ...] new UVs
        self.src_faces = src_faces        # same faces, old UVs
        self.matrix = matrix
        self.translation = translation
        self.error_px = error_px
        self.order = order                # draw order; higher wins overlaps
        self.source = None                # per-plan QImage (UDIM tiles)


def plan_shell(key, src_faces, dst_faces, size_px, tolerance_px=0.35,
               order=0):
    """One shell's plan: a single affine if it fits, else per-triangle.

    Tolerance is in OUTPUT pixels, so the same shell may be 'affine' at 1K
    and 'triangles' at 8K - which is correct: at 8K a sub-pixel UV wobble
    becomes a visible one.
    """
    src = [p for face in src_faces for p in face]
    dst = [p for face in dst_faces for p in face]
    fit = fit_affine(src, dst)
    if fit is not None:
        matrix, translation, rms_uv = fit
        err_px = rms_uv * size_px
        if err_px <= tolerance_px:
            return ShellWarp(key, "affine", dst_faces, src_faces, matrix,
                             translation, err_px, order)
    return ShellWarp(key, "triangles", dst_faces, src_faces,
                     error_px=float("inf") if fit is None else fit[2] * size_px,
                     order=order)


def output_size(source_size, plans, keep_density=True, cap=8192):
    """Output resolution that preserves texel density, if asked.

    If the layout shrank shells to half size, keeping the same pixel detail
    needs twice the resolution. The factor is the area-weighted median
    linear scale across affine shells, rounded to a power of two and capped
    - so one tiny shrunken shell cannot demand a 64K texture.
    """
    if not keep_density:
        return source_size
    scales = []
    for plan in plans:
        if plan.mode == "affine":
            s = math.sqrt(max(affine_scale(plan.matrix), 1e-12))
            scales.append(s)
    if not scales:
        return source_size
    scales.sort()
    median = scales[len(scales) // 2]
    want = source_size / max(median, 1e-6)
    power = 2 ** int(round(math.log(max(want, 1.0), 2)))
    return int(min(max(power, 16), cap))


# =====================================================================
# SECTION 3 - The warp (Qt, C++-side)
# =====================================================================

def _uv_to_px(size):
    """QTransform taking UV (V up, 0-1) to pixels (Y down)."""
    w, h = size
    return QtGui.QTransform(w, 0.0, 0.0, -h, 0.0, h)


def _faces_path(faces, to_px, grow_px=0.0):
    path = QtGui.QPainterPath()
    path.setFillRule(QtCore.Qt.WindingFill)
    for face in faces:
        if len(face) < 3:
            continue
        pts = [to_px.map(QtCore.QPointF(u, v)) for u, v in face]
        path.moveTo(pts[0])
        for p in pts[1:]:
            path.lineTo(p)
        path.closeSubpath()
    if grow_px > 0.0:
        stroker = QtGui.QPainterPathStroker()
        stroker.setWidth(grow_px * 2.0)
        stroker.setJoinStyle(QtCore.Qt.RoundJoin)
        path = path.united(stroker.createStroke(path))
    return path


def _pixel_transform(matrix, translation, src_size, dst_size):
    """QTransform mapping SOURCE pixels to DESTINATION pixels.

    uv_dst = A * uv_src + t, wrapped in the two UV<->pixel conversions.
    QTransform maps (x, y) -> (m11 x + m21 y + dx, m12 x + m22 y + dy).
    """
    a, b, c, d = matrix
    tx, ty = translation
    uv = QtGui.QTransform(a, c, b, d, tx, ty)
    to_uv = _uv_to_px(src_size).inverted()[0]
    return to_uv * uv * _uv_to_px(dst_size)


def warp_texture(source, plans, out_size=None, padding_px=4.0,
                 background=None, progress=None):
    """Re-lay `source` (QImage) so each shell's pixels follow its UVs.

    plans      ShellWarp list. Drawn in `order`, so where shells overlap in
               the new layout (a stacked pair) the higher order wins.
    padding_px Bleed carried around each shell.
    Returns a new QImage; the source is never modified.
    """
    src_size = (source.width(), source.height())
    dst_size = out_size or src_size
    # 16-bit in, 16-bit out. The prototype always made an 8-bit image, so a
    # 16-bit texture silently lost precision on the way through.
    deep = source.depth() >= 64 and hasattr(QtGui.QImage,
                                            "Format_RGBA64_Premultiplied")
    out = QtGui.QImage(dst_size[0], dst_size[1],
                       QtGui.QImage.Format_RGBA64_Premultiplied if deep
                       else QtGui.QImage.Format_ARGB32_Premultiplied)
    out.fill(background if background is not None
             else QtGui.QColor(0, 0, 0, 0))
    to_px = _uv_to_px(dst_size)
    full = QtCore.QRect(0, 0, dst_size[0], dst_size[1])
    layer_fmt = out.format()
    painter = QtGui.QPainter(out)
    ordered = sorted(plans, key=lambda p: p.order)
    try:
        for index, plan in enumerate(ordered):
            if progress is not None:
                progress(index, len(ordered), plan.key)
            image = plan.source if plan.source is not None else source
            _draw_shell(painter, plan, image, to_px, dst_size, full,
                        layer_fmt, padding_px, progress, index, len(ordered))
    finally:
        painter.end()
    return out


def _draw_shell(painter, plan, image, to_px, dst_size, full, layer_fmt,
                padding_px, progress=None, index=0, total=1):
    """Warp one shell into its own small layer, cut it out with a painted
    mask (faces + padding stroke), and composite it onto the output.

    WHY A PAINTED MASK
        The first version clipped with the outline grown by a vector union
        (QPainterPath.united). On a 355-face shell that union dominated
        everything: 170 s for a 120k-face car preview. Painting the faces
        and a thick stroke is raster work, and the layer is cropped to the
        shell, so each shell costs milliseconds.
    """
    d_path = _faces_path(plan.dst_faces, to_px)
    grow = int(math.ceil(padding_px)) + 2
    rect = d_path.boundingRect().toAlignedRect().adjusted(
        -grow, -grow, grow, grow).intersected(full)
    if rect.isEmpty():
        return
    shift = QtGui.QTransform.fromTranslate(-rect.x(), -rect.y())
    isize = (image.width(), image.height())

    layer = QtGui.QImage(rect.width(), rect.height(), layer_fmt)
    layer.fill(QtGui.QColor(0, 0, 0, 0))
    lp = QtGui.QPainter(layer)
    lp.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
    matrix, translation = plan.matrix, plan.translation
    if plan.mode != "affine":
        # Best single transform underneath: it supplies the padding bleed an
        # exact triangle-by-triangle warp has no pixels for.
        src = [p for f in plan.src_faces for p in f]
        dst = [p for f in plan.dst_faces for p in f]
        fit = fit_affine(src, dst)
        matrix, translation = (fit[0], fit[1]) if fit else (None, None)
    if matrix is not None:
        lp.setTransform(_pixel_transform(matrix, translation, isize,
                                         dst_size) * shift)
        lp.drawImage(0, 0, image)
    if plan.mode != "affine":
        count = 0
        for sface, dface in zip(plan.src_faces, plan.dst_faces):
            for i in range(1, len(dface) - 1):
                count += 1
                if progress is not None and count % 400 == 0:
                    progress(index, total, plan.key)
                s_tri = [sface[0], sface[i], sface[i + 1]]
                d_tri = [dface[0], dface[i], dface[i + 1]]
                fit = fit_affine(s_tri, d_tri)
                if fit is None:
                    continue
                lp.setTransform(shift)
                # No anti-aliasing on triangle clips: neighbouring triangles
                # then tile without seams (pixel-centre rule).
                lp.setClipPath(_faces_path([d_tri], to_px))
                lp.setTransform(_pixel_transform(fit[0], fit[1], isize,
                                                 dst_size) * shift)
                lp.drawImage(0, 0, image)
        lp.setClipping(False)

    mask = QtGui.QImage(rect.width(), rect.height(),
                        QtGui.QImage.Format_ARGB32_Premultiplied)
    mask.fill(QtGui.QColor(0, 0, 0, 0))
    mp = QtGui.QPainter(mask)
    mp.setRenderHint(QtGui.QPainter.Antialiasing, True)
    mp.setTransform(shift)
    white = QtGui.QColor(255, 255, 255)
    mp.fillPath(d_path, white)
    if padding_px > 0:
        mp.strokePath(d_path, QtGui.QPen(white, padding_px * 2.0,
                                         QtCore.Qt.SolidLine,
                                         QtCore.Qt.RoundCap,
                                         QtCore.Qt.RoundJoin))
    mp.end()
    lp.resetTransform()
    lp.setCompositionMode(QtGui.QPainter.CompositionMode_DestinationIn)
    lp.drawImage(0, 0, mask)
    lp.end()
    painter.drawImage(rect.topLeft(), layer)


# =====================================================================
# SECTION 3b - Files, UDIM tiles, and the whole transfer (no Maya here;
# the handler passes in what M2 read from the scene)
# =====================================================================

_UDIM_NUMBER = re.compile(r"(?<!\d)(1\d{3})(?!\d)")


def udim_template(path, is_udim):
    """'wood.1001.png' -> 'wood.<UDIM>.png' when the texture is UDIM."""
    if "<UDIM>" in path.upper():
        return re.sub("(?i)<udim>", "<UDIM>", path)
    if not is_udim:
        return path
    folder, name = os.path.split(path)
    hits = list(_UDIM_NUMBER.finditer(name))
    if not hits:
        return path
    last = hits[-1]
    return os.path.join(folder, name[:last.start()] + "<UDIM>"
                        + name[last.end():])


def tile_path(template, udim):
    return template.replace("<UDIM>", str(udim))


def output_path(template, suffix):
    """Suffix before the UDIM token / extension: wood_uvs.<UDIM>.png."""
    folder, name = os.path.split(template)
    stem, ext = os.path.splitext(name)
    if "<UDIM>" in stem:
        i = stem.index("<UDIM>")
        sep = stem[i - 1] if i and stem[i - 1] in "._" else ""
        head = stem[:i - len(sep)]
        stem = head + suffix + sep + stem[i:]
    else:
        stem = stem + suffix
    return os.path.join(folder, stem + ext)


def _tile(loop):
    cu = sum(p[0] for p in loop) / float(len(loop))
    cv = sum(p[1] for p in loop) / float(len(loop))
    return int(math.floor(cu)), int(math.floor(cv))


def _udim(tile):
    return 1001 + tile[0] + 10 * tile[1]


def plans_by_tile(meshes_data, size_px, tolerance_px=0.35, progress=None):
    """{dst udim: [(src udim, ShellWarp in tile-local UVs)]}.

    meshes_data: [(src_loops, dst_loops, dst_firsts, dst_shell_ids)] - one
    entry per mesh sharing the texture. Faces are grouped by their shell in
    the NEW layout, then by source and destination tile, so a shell that
    moved from 1001 to 1002 is read from the 1001 image and drawn into the
    1002 one.
    """
    out = {}
    for m_index, (src_loops, dst_loops, firsts, shells) in enumerate(
            meshes_data):
        if progress is not None:
            progress(m_index, len(meshes_data), "planning mesh %d" % (m_index + 1))
        groups = {}
        for f, (s_loop, d_loop) in enumerate(zip(src_loops, dst_loops)):
            if not s_loop or not d_loop or len(s_loop) != len(d_loop):
                continue
            shell = shells[firsts[f]] if firsts[f] is not None else -1
            key = (shell, _tile(s_loop), _tile(d_loop))
            groups.setdefault(key, ([], []))
            groups[key][0].append(s_loop)
            groups[key][1].append(d_loop)
        for (shell, s_tile, d_tile), (s_faces, d_faces) in groups.items():
            s_local = [[(u - s_tile[0], v - s_tile[1]) for u, v in f]
                       for f in s_faces]
            d_local = [[(u - d_tile[0], v - d_tile[1]) for u, v in f]
                       for f in d_faces]
            plan = plan_shell((m_index, shell), s_local, d_local, size_px,
                              tolerance_px)
            out.setdefault(_udim(d_tile), []).append((_udim(s_tile), plan))
    return out


def transfer_texture(path, is_udim, meshes_data, resolution="keep",
                     fixed_size=2048, padding_px=4.0, suffix="_uvs",
                     progress=None, oiiotool=None):
    """Warp one texture (single image or UDIM set) to the new layout.

    Writes new files beside the originals and returns a report. Never
    overwrites a source: an output path equal to its input is refused.
    EXR goes through oiiotool (float kept); everything else through Qt.
    """
    if path.lower().endswith(".exr"):
        tool = oiiotool or find_oiiotool()
        if tool is None:
            raise IOError("EXR textures need oiiotool, which ships with "
                          "Arnold (MtoA). It was not found.")
        return transfer_texture_exr(path, is_udim, meshes_data, tool,
                                    resolution, fixed_size, padding_px,
                                    suffix, progress=progress)
    template = udim_template(path, is_udim)
    out_template = output_path(template, suffix)
    if os.path.normcase(out_template) == os.path.normcase(template):
        raise ValueError("suffix must be non-empty; refusing to overwrite "
                         "the source texture")
    cache = {}

    def load(udim):
        if udim not in cache:
            p = tile_path(template, udim) if "<UDIM>" in template else path
            img = QtGui.QImage(p)
            cache[udim] = None if img.isNull() else img
        return cache[udim]

    probe_img = load(1001) if "<UDIM>" in template else load(None)
    if probe_img is None:
        for udim in range(1001, 1101):
            probe_img = load(udim)
            if probe_img is not None:
                break
    if probe_img is None:
        raise IOError("could not read %s" % path)
    base = probe_img.width()
    tiles = plans_by_tile(meshes_data, base, progress=progress)
    written, notes = [], []
    for d_udim, pairs in sorted(tiles.items()):
        plans = []
        for s_udim, plan in pairs:
            src = load(s_udim if "<UDIM>" in template else None)
            if src is None:
                notes.append("missing source tile %s" % s_udim)
                continue
            plan.source = src
            plans.append(plan)
        if not plans:
            continue
        if resolution == "fixed":
            size = int(fixed_size)
        elif resolution == "follow":
            size = output_size(base, [p for p in plans])
        else:
            size = base
        if "<UDIM>" not in template and d_udim != 1001:
            notes.append("shells moved to tile %d; a single-image texture "
                         "only covers 1001 - they were skipped" % d_udim)
            continue
        image = warp_texture(probe_img, plans, (size, size), padding_px,
                             progress=progress)
        if not probe_img.hasAlphaChannel():
            # A colour map without alpha must not gain one.
            flat = (QtGui.QImage.Format_RGBX64 if image.depth() >= 64
                    and hasattr(QtGui.QImage, "Format_RGBX64")
                    else QtGui.QImage.Format_RGB32)
            image = image.convertToFormat(flat)
        target = (tile_path(out_template, d_udim) if "<UDIM>" in out_template
                  else out_template)
        if not image.save(target):
            stem, _ext = os.path.splitext(target)
            target = stem + ".png"
            if not image.save(target):
                raise IOError("could not write %s" % target)
            notes.append("wrote PNG: this Qt cannot write %s" % _ext)
        written.append(target)
    return {"source": path, "output": (out_template if "<UDIM>" in
                                       out_template else
                                       (written[0] if written else None)),
            "written": written, "notes": notes,
            "shells": sum(len(v) for v in tiles.values())}


# =====================================================================
# SECTION 3c - Float / HDR EXR through oiiotool
#
# Maya 2022's Qt has no float image format and neither MImage nor
# convertSolidTx can write a float EXR (probes 1-3). Arnold ships oiiotool,
# which keeps everything float. Per shell: crop the source canvas, --warp it
# by the shell's exact pixel transform, attach a Qt-drawn outline mask as
# alpha, and composite it --over the accumulating result.
#
# Measured, not assumed (OIIO 3.1 here; the same ops exist in Arnold's 2.x):
#   --warp's 3x3 matrix is row-major, row-vector, source -> destination:
#       [x y 1] . [[a c 0], [b d 0], [tx ty 1]] - QTransform's own layout.
#   --over composites the SECOND-from-top image over the top one, so each
#       shell is pushed, --swap'ped under the accumulator, then --over'ed.
# =====================================================================

OIIO_BATCH = 40          # shells per oiiotool call (Windows 32K cmd limit)


def find_oiiotool(extra=None):
    """Path to oiiotool: caller hints, PATH, then Arnold's usual homes."""
    candidates = list(extra or [])
    found = shutil.which("oiiotool")
    if found:
        candidates.append(found)
    for pattern in ("C:/Program Files/Autodesk/Arnold/maya*/bin/oiiotool.exe",
                    "/usr/autodesk/arnold/maya*/bin/oiiotool",
                    "/Applications/Autodesk/Arnold/mtoa/*/bin/oiiotool"):
        candidates.extend(sorted(glob.glob(pattern), reverse=True))
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _oiio(tool, args):
    out = subprocess.run([tool] + [str(a) for a in args],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         universal_newlines=True, timeout=1800)
    if out.returncode != 0:
        raise RuntimeError("oiiotool failed: %s" % (out.stderr.strip()
                                                    or out.stdout.strip())
                           [-400:])
    return out.stdout


def oiio_info(tool, path):
    """(width, height, channels) of an image, read by oiiotool."""
    text = _oiio(tool, ["--info", path])
    size = re.search(r"(\d+)\s*x\s*(\d+)", text)
    chans = re.search(r"(\d+)\s+channel", text)
    if not size:
        raise IOError("could not read %s" % path)
    return (int(size.group(1)), int(size.group(2)),
            int(chans.group(1)) if chans else 3)


def _oiio_matrix(plan, src_size, dst_size):
    """oiiotool --warp argument for a plan (affine; best fit otherwise)."""
    matrix, translation = plan.matrix, plan.translation
    if plan.mode != "affine":
        src = [p for f in plan.src_faces for p in f]
        dst = [p for f in plan.dst_faces for p in f]
        fit = fit_affine(src, dst)
        if fit is None:
            return None
        matrix, translation = fit[0], fit[1]
    t = _pixel_transform(matrix, translation, src_size, dst_size)
    return ",".join(repr(float(v)) for v in (
        t.m11(), t.m12(), 0.0, t.m21(), t.m22(), 0.0, t.dx(), t.dy(), 1.0))


def _mask(path, plan, size, padding_px):
    """The shell's new outline (grown by padding) as an 8-bit mask PNG."""
    img = QtGui.QImage(size[0], size[1], QtGui.QImage.Format_Grayscale8)
    img.fill(0)
    painter = QtGui.QPainter(img)
    painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
    outline = _faces_path(plan.dst_faces, _uv_to_px(size))
    white = QtGui.QColor(255, 255, 255)
    painter.fillPath(outline, white)
    if padding_px > 0:
        painter.strokePath(outline, QtGui.QPen(white, padding_px * 2.0,
                                            QtCore.Qt.SolidLine,
                                            QtCore.Qt.RoundCap,
                                            QtCore.Qt.RoundJoin))
    painter.end()
    if not img.save(path):
        raise IOError("could not write mask %s" % path)


def warp_exr_tile(tool, pairs, sources, out_path, size, padding_px=4.0,
                  work=None, progress=None):
    """Composite every (src udim, plan) in `pairs` into one float image.

    sources: {src udim or None: (path, (w, h), channels)}.
    Returns a list of notes (e.g. shells approximated by one transform).
    """
    work = work or tempfile.mkdtemp(prefix="uvs_exr_")
    w, h = size
    notes = []
    acc = os.path.join(work, "acc_0.exr")
    _oiio(tool, ["--create", "%dx%d" % (w, h), 4, "-d", "float", "-o", acc])
    step = 0
    for start in range(0, len(pairs), OIIO_BATCH):
        if progress is not None:
            progress(start, len(pairs), "EXR shells %d-%d" % (
                start + 1, min(start + OIIO_BATCH, len(pairs))))
        args = [acc]
        for index, (s_key, plan) in enumerate(pairs[start:start + OIIO_BATCH]):
            src_path, src_size, chans = sources[s_key]
            matrix = _oiio_matrix(plan, src_size, size)
            if matrix is None:
                notes.append("shell %s skipped (degenerate)" % (plan.key,))
                continue
            if plan.mode != "affine":
                notes.append("shell %s was unfolded; EXR uses its best single "
                             "transform (%.1f px off)" % (plan.key,
                                                          plan.error_px))
            mask = os.path.join(work, "mask_%d_%d.png" % (start, index))
            _mask(mask, plan, size, padding_px)
            big = "%dx%d+0+0" % (max(w, src_size[0]), max(h, src_size[1]))
            args += [src_path, "--ch", "0,1,2" if chans >= 3 else "0,0,0",
                     "--crop", big, "--warp:filter=lanczos3", matrix,
                     "--crop", "%dx%d+0+0" % (w, h), mask, "--chappend",
                     "--chnames", "R,G,B,A", "--premult", "--swap", "--over"]
        step += 1
        acc = os.path.join(work, "acc_%d.exr" % step)
        args += ["-d", "float", "-o", acc]
        _oiio(tool, args)
    _oiio(tool, [acc, "--ch", "R,G,B", "-d", "half", "-o", out_path])
    return notes


def transfer_texture_exr(path, is_udim, meshes_data, tool, resolution="keep",
                         fixed_size=2048, padding_px=4.0, suffix="_uvs",
                         progress=None):
    """transfer_texture for float EXR, via oiiotool. Same report shape."""
    template = udim_template(path, is_udim)
    out_template = output_path(template, suffix)
    if os.path.normcase(out_template) == os.path.normcase(template):
        raise ValueError("suffix must be non-empty; refusing to overwrite "
                         "the source texture")
    udim = "<UDIM>" in template
    sources = {}

    def source(key):
        if key not in sources:
            p = tile_path(template, key) if udim else path
            sources[key] = ((p,) + tuple([oiio_info(tool, p)[:2],
                                          oiio_info(tool, p)[2]])
                            if os.path.isfile(p) else None)
        return sources[key]

    first = None
    for key in ([1001] + list(range(1002, 1101))) if udim else [None]:
        if source(key):
            first = sources[key]
            break
    if first is None:
        raise IOError("could not read %s" % path)
    base = first[1][0]
    tiles = plans_by_tile(meshes_data, base, progress=progress)
    written, notes = [], []
    for d_udim, pairs in sorted(tiles.items()):
        if not udim and d_udim != 1001:
            notes.append("shells moved to tile %d; a single-image texture "
                         "only covers 1001 - they were skipped" % d_udim)
            continue
        usable = []
        for s_udim, plan in pairs:
            key = s_udim if udim else None
            if source(key) is None:
                notes.append("missing source tile %s" % s_udim)
                continue
            usable.append((key, plan))
        if not usable:
            continue
        if resolution == "fixed":
            size = int(fixed_size)
        elif resolution == "follow":
            size = output_size(base, [p for _k, p in usable])
        else:
            size = base
        target = tile_path(out_template, d_udim) if udim else out_template
        notes.extend(warp_exr_tile(tool, usable, dict(
            (k, sources[k]) for k, _p in usable), target, (size, size),
            padding_px, progress=progress))
        written.append(target)
    return {"source": path, "output": out_template if udim else
            (written[0] if written else None), "written": written,
            "notes": notes, "shells": sum(len(v) for v in tiles.values())}


def preview_image(path, max_size=1024, tool=None):
    """A QImage of a texture for on-screen preview, at most max_size wide.

    Qt reads 8/16-bit formats itself; anything else (EXR) is converted to a
    temporary 8-bit PNG by oiiotool when available - HDR is clamped, which
    is fine for LOOKING at a layout. None when it cannot be shown.
    """
    img = QtGui.QImage(path)
    if img.isNull() and tool:
        tmp = os.path.join(tempfile.mkdtemp(prefix="uvs_prev_"), "p.png")
        try:
            _oiio(tool, [path, "--ch", "0,1,2", "--resize",
                         "%dx0" % max_size, "-d", "uint8", "-o", tmp])
            img = QtGui.QImage(tmp)
        except Exception:
            return None
    if img.isNull():
        return None
    if img.width() > max_size:
        img = img.scaledToWidth(max_size, QtCore.Qt.SmoothTransformation)
    return img


# =====================================================================
# SECTION 4 - Self test (offscreen Qt, no Maya)
# =====================================================================

def run_self_test():
    failures = []

    def check(label, ok, detail=""):
        print("%-58s %s %s" % (label, "PASS" if ok else "FAIL", detail))
        if not ok:
            failures.append(label)

    print("=" * 80)
    print("UV Studio M6 - Texture transfer self test (v%s)" % __version__)
    print("=" * 80)

    # -- fitting
    src = [(0.1, 0.1), (0.3, 0.1), (0.3, 0.2), (0.1, 0.2)]
    moved = [(u + 0.5, v + 0.25) for u, v in src]
    m, t, err = fit_affine(src, moved)
    check("a move fits exactly", err < 1e-12 and abs(t[0] - 0.5) < 1e-12,
          "t=%s" % (t,))
    rot = [(-v, u) for u, v in src]
    m, t, err = fit_affine(src, rot)
    check("a 90-degree rotation fits exactly", err < 1e-12,
          "m=%s" % (tuple(round(x, 6) for x in m),))
    flip = [(1.0 - u, v) for u, v in src]
    m, t, err = fit_affine(src, flip)
    check("a flip fits exactly (negative determinant)",
          err < 1e-12 and m[0] * m[3] - m[1] * m[2] < 0)
    half = [(u * 0.5, v * 0.5) for u, v in src]
    m, t, err = fit_affine(src, half)
    check("a half-size scale reports area 0.25",
          abs(affine_scale(m) - 0.25) < 1e-12)
    check("collinear points are refused",
          fit_affine([(0, 0), (1, 1), (2, 2)], [(0, 0), (1, 1), (2, 2)])
          is None)

    # -- planning
    face_src = [[(0.1, 0.1), (0.3, 0.1), (0.3, 0.3), (0.1, 0.3)]]
    face_dst = [[(u + 0.4, v + 0.4) for u, v in face_src[0]]]
    plan = plan_shell("a", face_src, face_dst, 1024)
    check("a moved shell plans as ONE affine draw", plan.mode == "affine")
    warped = [[(0.5, 0.5), (0.7, 0.52), (0.72, 0.75), (0.48, 0.7)]]
    plan2 = plan_shell("b", face_src, warped, 1024)
    check("an unfolded (non-affine) shell falls back to triangles",
          plan2.mode == "triangles", "err %.1f px" % plan2.error_px)
    shrunk = [[(u * 0.5, v * 0.5) for u, v in face_src[0]]]
    size = output_size(1024, [plan_shell("c", face_src, shrunk, 1024)])
    check("halving shells doubles resolution to keep density", size == 2048,
          str(size))
    check("resolution is capped",
          output_size(4096, [plan_shell("d", face_src,
                                        [[(u * .1, v * .1)
                                          for u, v in face_src[0]]],
                                        4096)]) == 8192)

    if QtGui is None:
        print("Qt not available; warp tests skipped.")
    else:
        from_colour = QtGui.QColor(220, 40, 40)
        src_img = QtGui.QImage(256, 256, QtGui.QImage.Format_ARGB32)
        src_img.fill(QtGui.QColor(30, 30, 30))
        p = QtGui.QPainter(src_img)
        # the shell occupies UV 0.1-0.3 -> pixels x 25.6-76.8, y 179-230
        p.fillRect(QtCore.QRectF(25.6, 179.2, 51.2, 51.2), from_colour)
        p.fillRect(QtCore.QRectF(25.6, 179.2, 25.6, 25.6),
                   QtGui.QColor(40, 200, 40))       # marks the top-left
        p.end()

        def px(img, u, v):
            c = QtGui.QColor(img.pixel(int(u * img.width()),
                                       int((1 - v) * img.height())))
            return (c.red(), c.green(), c.blue())

        out = warp_texture(src_img, [plan])
        check("moved shell: its pixels arrive at the new place",
              px(out, 0.65, 0.55) == (220, 40, 40), str(px(out, 0.65, 0.55)))
        check("  the top-left marker is still top-left",
              px(out, 0.53, 0.67) == (40, 200, 40), str(px(out, 0.53, 0.67)))
        check("  nothing is drawn outside the shell + padding",
              QtGui.QColor.fromRgba(out.pixel(5, 5)).alpha() == 0)

        rot_dst = [[(0.9 - (v - 0.1), 0.1 + (u - 0.1))
                    for u, v in face_src[0]]]
        out = warp_texture(src_img, [plan_shell("r", face_src, rot_dst,
                                                256)])
        check("rotated shell: the marker rotates with it",
              px(out, 0.75, 0.15) == (40, 200, 40), str(px(out, 0.75, 0.15)))
        # (u, v) -> (1 - v, u): the marker at (0.15, 0.25) lands at
        # (0.75, 0.15), and the far corner of the red body is elsewhere.
        check("  and the body rotates with it",
              px(out, 0.85, 0.25) == (220, 40, 40), str(px(out, 0.85, 0.25)))

        big = warp_texture(src_img, [plan_shell("s", face_src, shrunk, 256)],
                           out_size=(512, 512))
        check("halved shell at doubled resolution keeps its pixels",
              px(big, 0.1, 0.1) == (220, 40, 40), str(px(big, 0.1, 0.1)))

        tri = warp_texture(src_img, [plan2])
        check("per-triangle fallback lands the shell too",
              px(tri, 0.6, 0.6)[0] > 180, str(px(tri, 0.6, 0.6)))

        low = ShellWarp("low", "affine", face_dst, face_src, plan.matrix,
                        plan.translation, order=0)
        other = [[(0.1, 0.6), (0.3, 0.6), (0.3, 0.8), (0.1, 0.8)]]
        high_plan = plan_shell("high", other, face_dst, 256, order=1)
        out = warp_texture(src_img, [high_plan, low])
        check("stacked shells: the higher order wins the overlap",
              px(out, 0.65, 0.55) == (30, 30, 30),
              "(it drew the grey 'high' shell) %s" % (px(out, 0.65, 0.55),))

    # -- files and tiles
    check("UDIM path becomes a template",
          udim_template("C:/t/wood.1001.png", True).endswith("wood.<UDIM>.png"))
    check("non-UDIM path is left alone",
          udim_template("C:/t/wood_1001.png", False).endswith("wood_1001.png"))
    check("suffix goes before the UDIM token",
          output_path("C:/t/wood.<UDIM>.png", "_uvs").endswith(
              "wood_uvs.<UDIM>.png"))
    check("suffix goes before the extension",
          output_path("C:/t/wood.png", "_uvs").endswith("wood_uvs.png"))
    src_l = [[(0.1, 0.1), (0.3, 0.1), (0.3, 0.3), (0.1, 0.3)]]
    dst_l = [[(u + 1.0, v) for u, v in src_l[0]]]
    tiles = plans_by_tile([(src_l, dst_l, [0], [0])], 256)
    check("a shell moved to the next tile reads 1001, draws into 1002",
          list(tiles) == [1002] and tiles[1002][0][0] == 1001, str(tiles))

    if QtGui is not None and hasattr(QtGui.QImage, "Format_RGBA64"):
        import tempfile
        folder = tempfile.mkdtemp()
        deep = QtGui.QImage(64, 64, QtGui.QImage.Format_RGBX64)
        deep.fill(QtGui.QColor.fromRgbF(0.123456, 0.5, 0.25, 1.0))
        src_path = os.path.join(folder, "deep.png")
        deep.save(src_path)
        report = transfer_texture(src_path, False,
                                  [(src_l, [[(u + 0.4, v + 0.4)
                                             for u, v in src_l[0]]],
                                    [0], [0])])
        back = QtGui.QImage(report["written"][0])
        check("16-bit texture stays 16-bit through a transfer",
              back.depth() == 64, "depth %d" % back.depth())
        check("  and does not gain an alpha channel",
              not back.hasAlphaChannel())
        check("  and the source file is untouched",
              QtGui.QImage(src_path).depth() == 64
              and report["written"][0] != src_path)
        try:
            transfer_texture(src_path, False, [(src_l, src_l, [0], [0])],
                             suffix="")
            check("an empty suffix is refused (no overwrite)", False)
        except ValueError:
            check("an empty suffix is refused (no overwrite)", True)

    tool = find_oiiotool()
    if tool is None or QtGui is None:
        print("oiiotool not found; EXR tests skipped (it ships with Arnold).")
    else:
        import tempfile as _tf
        folder = _tf.mkdtemp()
        src_exr = os.path.join(folder, "hdr.exr")
        _oiio(tool, ["--create", "64x64", 3, "--fill:color=2.5,2.5,2.5",
                     "13x13+6+45", "-d", "float", "-o", src_exr])
        report = transfer_texture(src_exr, False,
                                  [(src_l, [[(u + 0.5, v + 0.5)
                                             for u, v in src_l[0]]],
                                    [0], [0])], oiiotool=tool)
        out_exr = report["written"][0]
        stats = _oiio(tool, [out_exr, "--cut", "1x1+46+21", "--printstats"])
        hit = re.search(r"Min:\s*([\d.]+)", stats)
        check("EXR through oiiotool: HDR value 2.5 arrives moved",
              hit is not None and abs(float(hit.group(1)) - 2.5) < 1e-3,
              hit.group(1) if hit else stats[-80:])

    print("")
    print("=" * 80)
    print("%d failure(s)" % len(failures))
    for name in failures:
        print("  FAILED: %s" % name)
    print("=" * 80)
    return not failures


if __name__ == "__main__":
    run_self_test()
