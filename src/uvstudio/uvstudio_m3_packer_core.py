"""
UV Studio - Module 3: Packing engine
====================================

The bitmap packer from UV Cluster Packer v4, adopted whole. Shells are
RASTERISED to their true silhouette on an occupancy grid and packed against
real free space, so an L-shape or a diagonal strip nests into another
shell's negative space. The previous M3 saw only bounding boxes and could
not: on 60 irregular shells it placed 34 and filled 35%% of the real UV area
it used, against this engine's 60 and 48%%.

PURE PYTHON, NO MAYA. Every collision search is shifts and ORs on one big
Python int (Board), so the whole thing runs and is tested off a real Maya.
M2 feeds it UV triangles; M5b drives it. Nothing here imports maya.

CREDIT: this is the original author's engine, moved module-for-module rather
than reimplemented, because it already works on the real asset and a rewrite
would only find new ways to be wrong.

Entry points M5b uses:
    PackItem(key, tris_uv, bbox, area, pscale, ref_res)  - one rigid cluster
    ObstacleSet(tris_uv, tile)                            - pinned geometry
    Packer / MultiPacker                                 - the solver
    QUALITY, ROTATIONS                                   - the settings tables
"""

from __future__ import division

import math
from collections import OrderedDict

__version__ = "3.1.0"
MODULE_ID = "M3"

EPS = 1e-9
OVERLAP_EPSILON = 1.0e-5
ALIGN_EDGES = ("left", "right", "bottom", "top", "centre_u", "centre_v")


def offset_to_udim(u, v):
    """UV coordinate -> the UDIM number of the tile it falls in."""
    return 1001 + int(math.floor(u)) + 10 * int(math.floor(v))


class UserError(Exception):
    """Raised when a layout genuinely cannot be produced, with a message the
    panel shows the artist verbatim."""


def _noop_tick(msg=None):
    pass


def popcount(x):
    return bin(x).count("1")


def bit_runs(row):
    """[(start, length), ...] for every run of set bits in an int."""
    out = []
    shift = 0
    while row:
        tz = (row & -row).bit_length() - 1
        row >>= tz
        shift += tz
        ln = ((~row) & (row + 1)).bit_length() - 1
        out.append((shift, ln))
        row >>= ln
        shift += ln
    return out


def _band_xrange(a, b, c, y0, y1):
    """x-extent of triangle abc clipped to the horizontal band [y0, y1]."""
    xs = []
    pts = (a, b, c)
    for i in range(3):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % 3]
        if y0 <= ay <= y1:
            xs.append(ax)
        if ay != by:
            for yl in (y0, y1):
                if (ay - yl) * (by - yl) < 0.0:
                    t = (yl - ay) / (by - ay)
                    xs.append(ax + t * (bx - ax))
    if not xs:
        return None
    return min(xs), max(xs)


def raster_conservative(tris, width, height, rows=None):
    """Mark every cell a triangle touches. Coordinates are in cell units.
    rows[y] is an int bitmask (bit x = column x)."""
    if rows is None:
        rows = [0] * height
    floor, ceil = math.floor, math.ceil
    for a, b, c in tris:
        ys = (a[1], b[1], c[1])
        xs = (a[0], b[0], c[0])
        ymin, ymax = min(ys), max(ys)
        xmin, xmax = min(xs), max(xs)
        if ymax < 0 or ymin >= height or xmax < 0 or xmin >= width:
            continue
        r0 = int(floor(ymin))
        r1 = int(ceil(ymax)) - 1
        single = r1 <= r0
        if single:
            r1 = r0
        for r in range(max(r0, 0), min(r1, height - 1) + 1):
            if single:
                xa, xb = xmin, xmax
            else:
                res = _band_xrange(a, b, c, r, r + 1)
                if res is None:
                    continue
                xa, xb = res
            c0 = int(floor(xa))
            c1 = int(ceil(xb)) - 1
            if c1 < c0:
                c1 = c0
            if c0 < 0:
                c0 = 0
            if c1 >= width:
                c1 = width - 1
            if c1 < c0:
                continue
            rows[r] |= ((1 << (c1 - c0 + 1)) - 1) << c0
    return rows


def raster_center(tris, width, height, rows=None):
    """Mark cells whose centre lies inside a triangle (for overlap tests,
    so shells that merely touch are not counted as overlapping)."""
    if rows is None:
        rows = [0] * height
    floor, ceil = math.floor, math.ceil
    for a, b, c in tris:
        ys = (a[1], b[1], c[1])
        ymin, ymax = min(ys), max(ys)
        r0 = max(int(ceil(ymin - 0.5)), 0)
        r1 = min(int(floor(ymax - 0.5)), height - 1)
        if r1 < r0:
            continue
        pts = (a, b, c)
        for r in range(r0, r1 + 1):
            yc = r + 0.5
            xs = []
            for i in range(3):
                ax, ay = pts[i]
                bx, by = pts[(i + 1) % 3]
                if (ay - yc) * (by - yc) <= 0.0:
                    if ay == by:
                        xs.append(ax)
                        xs.append(bx)
                    else:
                        xs.append(ax + (yc - ay) / (by - ay) * (bx - ax))
            if not xs:
                continue
            c0 = max(int(ceil(min(xs) - 0.5)), 0)
            c1 = min(int(floor(max(xs) - 0.5)), width - 1)
            if c1 >= c0:
                rows[r] |= ((1 << (c1 - c0 + 1)) - 1) << c0
    return rows


def _hwin(x, length):
    """OR of x >> k for k in [0, length)."""
    res = x
    span = 1
    while span * 2 <= length:
        res |= res >> span
        span *= 2
    if span < length:
        res |= res >> (length - span)
    return res


def _vwin(x, count, width):
    """OR of x >> (t * width) for t in [0, count)."""
    res = x
    span = 1
    while span * 2 <= count:
        res |= res >> (span * width)
        span *= 2
    if span < count:
        res |= res >> ((count - span) * width)
    return res


def dilate_rows(rows, m, size):
    """Chebyshev dilation of a size x size bitmap by m cells."""
    if m <= 0:
        return list(rows)
    full = (1 << size) - 1
    hd = [(_hwin(r << (2 * m), 2 * m + 1) >> m) & full for r in rows]
    out = [0] * size
    for j in range(size):
        acc = 0
        for i in range(max(0, j - m), min(size, j + m + 1)):
            acc |= hd[i]
        out[j] = acc
    return out


def rot90_bitmap(w, h, rows):
    """Rotate a bitmap 90 deg counter-clockwise: cell (x, y) -> (h-1-y, x)."""
    tog = [0] * (w + 1)
    for y, r in enumerate(rows):
        if not r:
            continue
        bit = 1 << (h - 1 - y)
        for a, ln in bit_runs(r):
            tog[a] ^= bit
            tog[a + ln] ^= bit
    out = []
    cur = 0
    for x in range(w):
        cur ^= tog[x]
        out.append(cur)
    return h, w, out


def mask_rects(rows):
    """Decompose a bitmap into (row0, rowcount, col, collength) rectangles."""
    rects = []
    open_ = {}
    for j, r in enumerate(rows):
        cur = set(bit_runs(r))
        for key in list(open_.keys()):
            if key not in cur:
                j0 = open_.pop(key)
                rects.append((j0, j - j0, key[0], key[1]))
        for key in cur:
            if key not in open_:
                open_[key] = j
    n = len(rows)
    for key, j0 in open_.items():
        rects.append((j0, n - j0, key[0], key[1]))
    return rects


def stamp_int(rows, m, width):
    """Dilate a mask by m and pack it into one int (origin at -m, -m)."""
    out = 0
    for i, r in enumerate(rows):
        if r:
            hr = _hwin(r << (2 * m), 2 * m + 1) if m > 0 else r
            out |= hr << (i * width)
    if m > 0:
        out = _vwin(out << (2 * m * width), 2 * m + 1, width)
    return out


_REPUNIT = {}


def _repunit(n, width):
    key = (n, width)
    v = _REPUNIT.get(key)
    if v is None:
        v = ((1 << (width * n)) - 1) // ((1 << width) - 1)
        _REPUNIT[key] = v
    return v


class Board(object):
    """Occupancy grid stored as one big int (2G x 2G bits, padded) so that a
    whole collision search is a handful of shifts and ORs."""

    def __init__(self, size, obstacle_rows, border):
        self.G = size
        self.W = w = 2 * size
        pad = ((1 << size) - 1) << size
        full = (1 << w) - 1
        side = 0
        if border > 0:
            bm = (1 << border) - 1
            side = bm | (bm << (size - border))
        bits = 0
        for y in range(size):
            if y < border or y >= size - border:
                r = full
            else:
                r = obstacle_rows[y] | pad | side
            bits |= r << (y * w)
        bits |= ((1 << (size * w)) - 1) << (size * w)
        self.bits = bits

    def copy(self):
        b = Board.__new__(Board)
        b.G, b.W, b.bits = self.G, self.W, self.bits
        return b

    def find(self, rects, mw, mh, cache):
        G, W = self.G, self.W
        if mw > G or mh > G or mw < 1 or mh < 1:
            return None
        bits = self.bits
        bad = 0
        for j0, k, a, ln in rects:
            key = (ln, k)
            v = cache.get(key)
            if v is None:
                h = cache.get((ln, 1))
                if h is None:
                    h = cache[(ln, 1)] = _hwin(bits, ln)
                v = h if k == 1 else _vwin(h, k, W)
                cache[key] = v
            bad |= v >> (j0 * W + a)
        region = ((1 << (G - mw + 1)) - 1) * _repunit(G - mh + 1, W)
        valid = region & ~bad
        if not valid:
            return None
        idx = (valid & -valid).bit_length() - 1
        return idx % W, idx // W

    def place(self, stamp, x0, y0):
        sh = y0 * self.W + x0
        self.bits |= (stamp << sh) if sh >= 0 else (stamp >> -sh)


_ROT = {0: (1, 0, 0, 1), 1: (0, -1, 1, 0), 2: (-1, 0, 0, -1), 3: (0, 1, -1, 0)}


class RasterItem(object):
    """A rigid cluster, rasterised once at a reference resolution."""

    def __init__(self, key, tris_uv, bbox, area, pscale, ref_res):
        self.key = key
        umin, vmin, umax, vmax = bbox
        bw = max(umax - umin, 1e-9)
        bh = max(vmax - vmin, 1e-9)
        self.bbmin = (umin, vmin)
        self.lmax = max(bw, bh)
        self.cell = self.lmax / float(ref_res)
        self.W = max(1, int(math.ceil(bw / self.cell - 1e-6)))
        self.H = max(1, int(math.ceil(bh / self.cell - 1e-6)))
        self.area = area
        self.pscale = pscale
        inv = 1.0 / self.cell
        ctris = [(((a[0] - umin) * inv, (a[1] - vmin) * inv),
                  ((b[0] - umin) * inv, (b[1] - vmin) * inv),
                  ((c[0] - umin) * inv, (c[1] - vmin) * inv))
                 for a, b, c in tris_uv]
        rows = raster_conservative(ctris, self.W, self.H)
        self._rots = {0: (self.W, self.H, rows,
                          [bit_runs(r) for r in rows])}

    def _rot(self, k):
        r = self._rots.get(k)
        if r is None:
            w, h, rows, _ = self._rot(k - 1)
            w2, h2, rows2 = rot90_bitmap(w, h, rows)
            r = self._rots[k] = (w2, h2, rows2, [bit_runs(x) for x in rows2])
        return r

    def mask(self, k, g):
        """Mask for rotation k at g target cells per UV unit."""
        w, h, _, runrows = self._rot(k)
        inv = self.cell * g
        wt = max(1, int(math.ceil(w * inv - 1e-7)))
        ht = max(1, int(math.ceil(h * inv - 1e-7)))
        out = [0] * ht
        floor, ceil = math.floor, math.ceil
        for s, runs in enumerate(runrows):
            if not runs:
                continue
            bits = 0
            for a, ln in runs:
                t0 = int(floor(a * inv + EPS))
                t1 = min(int(ceil((a + ln) * inv - EPS)) - 1, wt - 1)
                if t1 < t0:
                    t1 = t0
                bits |= ((1 << (t1 - t0 + 1)) - 1) << t0
            y0 = int(floor(s * inv + EPS))
            y1 = min(int(ceil((s + 1) * inv - EPS)) - 1, ht - 1)
            if y1 < y0:
                y1 = y0
            for y in range(y0, y1 + 1):
                out[y] |= bits
        return wt, ht, out

    def transform(self, k, x, y, G, S, tile):
        """Affine (A, t) mapping original UVs to packed UVs."""
        s = S * self.pscale
        a, b, c, d = _ROT[k]
        const = {0: (0, 0), 1: (self.H, 0),
                 2: (self.W, self.H), 3: (0, self.W)}[k]
        bx, by = self.bbmin
        tx = tile[0] + x / float(G) + s * (const[0] * self.cell - (a * bx + b * by))
        ty = tile[1] + y / float(G) + s * (const[1] * self.cell - (c * bx + d * by))
        return (s * a, s * b, s * c, s * d), (tx, ty)


class ObstacleSet(object):
    def __init__(self, tris_uv, tile):
        self.tris = tris_uv
        self.tile = tile
        self._raw = {}
        self._dil = {}

    def raw(self, G):
        r = self._raw.get(G)
        if r is None:
            tu, tv = self.tile
            ct = [(((a[0] - tu) * G, (a[1] - tv) * G),
                   ((b[0] - tu) * G, (b[1] - tv) * G),
                   ((c[0] - tu) * G, (c[1] - tv) * G))
                  for a, b, c in self.tris]
            r = self._raw[G] = raster_conservative(ct, G, G)
        return r

    def rows(self, G, m):
        key = (G, m)
        r = self._dil.get(key)
        if r is None:
            r = self._dil[key] = dilate_rows(self.raw(G), m, G)
        return r

    def free_fraction(self, G):
        used = sum(popcount(r) for r in self.raw(G))
        return 1.0 - used / float(G * G)


def _cells(v, G):
    return max(0, int(math.ceil(v * G - 1e-9)))


class Packer(object):
    def __init__(self, items, obstacles, rotations, border_uv, tile,
                 tick=_noop_tick):
        self.items = sorted(items, key=lambda i: -i.area * i.pscale ** 2)
        self.obst = obstacles
        self.rots = tuple(rotations)
        self.border_uv = border_uv
        self.tile = tile
        self.tick = tick
        self._boards = {}
        self.packs_run = 0

    def _board(self, G, m):
        b = _cells(self.border_uv, G)
        key = (G, m, b)
        bd = self._boards.get(key)
        if bd is None:
            bd = self._boards[key] = Board(G, self.obst.rows(G, m), b)
        return bd.copy()

    def pack(self, G, S, margin_uv, partial=False):
        """Return {key: (rot, x, y)} or None if something doesn't fit.
        With partial=True return (placements, unplaced_items) instead."""
        self.packs_run += 1
        m = _cells(margin_uv, G)
        board = self._board(G, m)
        W = board.W
        out = {}
        unplaced = []
        for it in self.items:
            self.tick()
            g = G * S * it.pscale
            best = None
            cache = {}
            for k in self.rots:
                mw, mh, rows = it.mask(k, g)
                if mw > G or mh > G:
                    continue
                pos = board.find(mask_rects(rows), mw, mh, cache)
                if pos is None:
                    continue
                score = (pos[1] + mh, pos[0])
                if best is None or score < best[0]:
                    best = (score, k, pos, rows)
            if best is None:
                if partial:
                    unplaced.append(it)
                    continue
                return None
            _, k, (x, y), rows = best
            board.place(stamp_int(rows, m, W), x - m, y - m)
            out[it.key] = (k, x, y)
        return (out, unplaced) if partial else out

    # ---- searches ----------------------------------------------------------
    def scale_upper_bound(self, G):
        b = _cells(self.border_uv, G) / float(G)
        free = max(self.obst.free_fraction(G) - (1 - (1 - 2 * b) ** 2), 1e-6)
        area = sum(i.area * i.pscale ** 2 for i in self.items) or 1e-12
        hi = math.sqrt(free / area)
        for i in self.items:
            hi = min(hi, (1 - 2 * b) / (i.lmax * i.pscale))
        return max(hi, 1e-6)

    def max_scale(self, Gs, G, margin_uv, cap=None):
        hi = self.scale_upper_bound(Gs)
        if cap is not None:
            hi = min(hi, cap)
        self.tick("Searching for the largest scale...")
        if self.pack(Gs, hi, margin_uv) is not None:
            s = hi
        else:
            lo = hi * 0.02
            if self.pack(Gs, lo, margin_uv) is None:
                raise UserError("Not enough free space around the fixed "
                                "shells, even at a tiny scale.")
            for _ in range(9):
                mid = math.sqrt(lo * hi)
                if self.pack(Gs, mid, margin_uv) is not None:
                    lo = mid
                else:
                    hi = mid
            s = lo
        if G != Gs:
            self.tick("Confirming scale at final resolution...")
            for _ in range(15):
                if self.pack(G, s, margin_uv) is not None:
                    break
                s *= 0.985
            else:
                raise UserError("Could not confirm the layout at the final "
                                "resolution. Try a lower quality setting.")
            for f in (1.03, 1.015):
                if (cap is None or s * f <= cap) and \
                        self.pack(G, s * f, margin_uv) is not None:
                    s *= f
                    break
        return s

    def max_margin(self, Gs, G, S, base_margin):
        self.tick("Spreading shells evenly...")
        lo, hi = base_margin, 0.25
        for _ in range(9):
            mid = (lo + hi) * 0.5
            if self.pack(Gs, S, mid) is not None:
                lo = mid
            else:
                hi = mid
        m = lo
        for _ in range(10):
            if m <= base_margin or self.pack(G, S, m) is not None:
                break
            m = base_margin + (m - base_margin) * 0.8
        return max(m, base_margin)


def median(vals):
    vals = sorted(vals)
    n = len(vals)
    if not n:
        return 0.0
    return vals[n // 2] if n % 2 else 0.5 * (vals[n // 2 - 1] + vals[n // 2])


# ---- shell alignment (for stacking pairs) -----------------------------------
IDENT = ((1.0, 0.0, 0.0, 1.0), (0.0, 0.0))


def affine_compose(outer, inner):
    """x -> outer(inner(x))"""
    (A, t), (L, u) = outer, inner
    M = (A[0] * L[0] + A[1] * L[2], A[0] * L[1] + A[1] * L[3],
         A[2] * L[0] + A[3] * L[2], A[2] * L[1] + A[3] * L[3])
    v = (A[0] * u[0] + A[1] * u[1] + t[0], A[2] * u[0] + A[3] * u[1] + t[1])
    return M, v


def affine_pt(aff, p):
    L, t = aff
    return (L[0] * p[0] + L[1] * p[1] + t[0], L[2] * p[0] + L[3] * p[1] + t[1])


def affine_tris(aff, tris):
    L, t = aff
    a, b, c, d = L
    return [tuple((a * p[0] + b * p[1] + t[0], c * p[0] + d * p[1] + t[1])
                  for p in tr) for tr in tris]


def is_identity(aff, tol=1e-12):
    return aff is None or (all(abs(x - y) <= tol for x, y in zip(aff[0], IDENT[0]))
                           and abs(aff[1][0]) <= tol and abs(aff[1][1]) <= tol)


def tri_moments(tris):
    """area, centroid and central second moments of a triangle soup."""
    A = sx = sy = sxx = syy = sxy = 0.0
    for (x1, y1), (x2, y2), (x3, y3) in tris:
        a = abs((x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1)) * 0.5
        if a <= 0.0:
            continue
        A += a
        sx += a * (x1 + x2 + x3) / 3.0
        sy += a * (y1 + y2 + y3) / 3.0
        sxx += a / 6.0 * (x1 * x1 + x2 * x2 + x3 * x3 + x1 * x2 + x1 * x3 + x2 * x3)
        syy += a / 6.0 * (y1 * y1 + y2 * y2 + y3 * y3 + y1 * y2 + y1 * y3 + y2 * y3)
        sxy += a / 12.0 * (2 * x1 * y1 + 2 * x2 * y2 + 2 * x3 * y3 +
                           x1 * y2 + x2 * y1 + x1 * y3 + x3 * y1 + x2 * y3 + x3 * y2)
    if A <= 0.0:
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    cx, cy = sx / A, sy / A
    return A, cx, cy, sxx - A * cx * cx, syy - A * cy * cy, sxy - A * cx * cy


def raster_iou(ta, tb, res=64):
    pts = [p for tr in ta for p in tr] + [p for tr in tb for p in tr]
    if not pts:
        return 0.0
    x0 = min(p[0] for p in pts)
    y0 = min(p[1] for p in pts)
    x1 = max(p[0] for p in pts)
    y1 = max(p[1] for p in pts)
    cell = max(x1 - x0, y1 - y0, 1e-12) / float(res)
    W = max(1, int(math.ceil((x1 - x0) / cell)))
    H = max(1, int(math.ceil((y1 - y0) / cell)))

    def ras(tris):
        return raster_center([tuple(((p[0] - x0) / cell, (p[1] - y0) / cell)
                                    for p in tr) for tr in tris], W, H)
    ra, rb = ras(ta), ras(tb)
    inter = sum(popcount(a & b) for a, b in zip(ra, rb))
    union = sum(popcount(a | b) for a, b in zip(ra, rb))
    return inter / float(union) if union else 0.0


def _lin(s, ang, mirror):
    c, n = math.cos(ang) * s, math.sin(ang) * s
    if mirror:          # R * s * diag(-1, 1)
        return (-c, -n, -n, c)
    return (c, -n, n, c)


def _procrustes(P, Q, mirror):
    n = len(P)
    pmx = sum(p[0] for p in P) / n
    pmy = sum(p[1] for p in P) / n
    qmx = sum(q[0] for q in Q) / n
    qmy = sum(q[1] for q in Q) / n
    sc = ss = pp = 0.0
    for (px, py), (qx, qy) in zip(P, Q):
        px -= pmx
        py -= pmy
        if mirror:
            px = -px
        qx -= qmx
        qy -= qmy
        sc += px * qx + py * qy
        ss += px * qy - py * qx
        pp += px * px + py * py
    if pp <= 1e-18:
        return None
    ang = math.atan2(ss, sc)
    s = math.hypot(sc, ss) / pp
    L = _lin(s, ang, mirror)
    t = (qmx - (L[0] * pmx + L[1] * pmy), qmy - (L[2] * pmx + L[3] * pmy))
    err = 0.0
    for p, q in zip(P, Q):
        x, y = affine_pt((L, t), p)
        err += (x - q[0]) ** 2 + (y - q[1]) ** 2
    return (L, t), math.sqrt(err / n)


def align_shell(m_tris, a_tris, m_pts=None, a_pts=None, allow_mirror=True):
    """Best affine (uniform scale, rotation, optional mirror, translation)
    stacking member onto anchor. Returns (affine, match 0..1)."""
    Am, mx, my, mxx, myy, mxy = tri_moments(m_tris)
    Aa, ax, ay, axx, ayy, axy = tri_moments(a_tris)
    if Am <= 0 or Aa <= 0:
        return IDENT, 0.0
    mirrors = (False, True) if allow_mirror else (False,)
    size = math.sqrt(Aa)

    # 1. exact correspondence (duplicated / mirrored copies)
    if m_pts and a_pts and len(m_pts) == len(a_pts) and len(m_pts) >= 3:
        best = None
        for mir in mirrors:
            r = _procrustes(m_pts, a_pts, mir)
            if r and (best is None or r[1] < best[1]):
                best = r
        if best and best[1] <= 1e-4 * size:
            return best[0], 1.0

    # 2. shape matching: principal axes + 90 deg candidates, scored by IoU
    s = math.sqrt(Aa / Am)
    th_a = 0.5 * math.atan2(2 * axy, axx - ayy)
    th_m = 0.5 * math.atan2(2 * mxy, mxx - myy)
    cands = set()
    for mir in mirrors:
        tm = -th_m if mir else th_m
        for base in (th_a - tm, 0.0):
            for k in range(4):
                ang = (base + k * math.pi / 2) % (2 * math.pi)
                cands.add((mir, round(ang, 6)))

    def build(mir, ang):
        L = _lin(s, ang, mir)
        t = (ax - (L[0] * mx + L[1] * my), ay - (L[2] * mx + L[3] * my))
        return (L, t)

    scored = []
    for mir, ang in cands:
        aff = build(mir, ang)
        scored.append((raster_iou(affine_tris(aff, m_tris), a_tris), mir, ang, aff))
    if m_pts and a_pts and len(m_pts) == len(a_pts) and len(m_pts) >= 3:
        for mir in mirrors:
            r = _procrustes(m_pts, a_pts, mir)
            if r:
                scored.append((raster_iou(affine_tris(r[0], m_tris), a_tris),
                               mir, None, r[0]))
    scored.sort(key=lambda x: -x[0])
    iou, mir, ang, aff = scored[0]
    if ang is not None:
        for step in (math.radians(2.0), math.radians(0.5)):
            for d in (-step, step):
                a2 = build(mir, ang + d)
                v = raster_iou(affine_tris(a2, m_tris), a_tris)
                if v > iou:
                    iou, ang, aff = v, ang + d, a2
    return aff, iou


# ---- layout tools (4.0-b) - pure functions ------------------------------------
class LUnit(object):
    """What layout tools see: one rigid unit with bounds and a center."""
    __slots__ = ("id", "rect", "center", "movable")

    def __init__(self, uid, rect, center=None, movable=True):
        self.id = uid
        self.rect = tuple(rect)
        self.center = center or ((rect[0] + rect[2]) * 0.5, (rect[1] + rect[3]) * 0.5)
        self.movable = movable


def rect_union(rects):
    rects = list(rects)
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


def rect_center(r):
    return ((r[0] + r[2]) * 0.5, (r[1] + r[3]) * 0.5)


def aff_translate(du, dv):
    return ((1.0, 0.0, 0.0, 1.0), (du, dv))


def aff_scale_about(s, cx, cy):
    return ((s, 0.0, 0.0, s), (cx - s * cx, cy - s * cy))


def rect_transform(aff, r):
    pts = [affine_pt(aff, p) for p in ((r[0], r[1]), (r[2], r[1]), (r[0], r[3]), (r[2], r[3]))]
    return (min(p[0] for p in pts), min(p[1] for p in pts),
            max(p[0] for p in pts), max(p[1] for p in pts))


def _plural(n, word):
    return "{} {}{}".format(n, word, "" if n == 1 else "s")


def _skipped_note(units):
    n = sum(1 for u in units if not u.movable)
    return " ({} pinned skipped)".format(n) if n else ""


ALIGN_LABELS = {"left": "left edges", "hcenter": "horizontal centers", "right": "right edges",
                "top": "top edges", "vmiddle": "vertical centers", "bottom": "bottom edges"}


def layout_align(units, mode, ref_rect, ref_center=None):
    """Move units so the chosen edge / center matches the reference."""
    rc = ref_center or rect_center(ref_rect)
    out = {}
    for u in units:
        if not u.movable:
            continue
        r = u.rect
        du = dv = 0.0
        if mode == "left":
            du = ref_rect[0] - r[0]
        elif mode == "right":
            du = ref_rect[2] - r[2]
        elif mode == "hcenter":
            du = rc[0] - u.center[0]
        elif mode == "top":
            dv = ref_rect[3] - r[3]
        elif mode == "bottom":
            dv = ref_rect[1] - r[1]
        elif mode == "vmiddle":
            dv = rc[1] - u.center[1]
        if abs(du) > 1e-12 or abs(dv) > 1e-12:
            out[u.id] = aff_translate(du, dv)
    msg = "Aligned {} to {}{}.".format(_plural(len(units), "unit"),
                                        ALIGN_LABELS.get(mode, mode), _skipped_note(units))
    return out, msg


def spatial_order(units, axis):
    """Left-to-right for horizontal work, top-to-bottom for vertical."""
    if axis == "h":
        return sorted(units, key=lambda u: (u.center[0], -u.center[1]))
    return sorted(units, key=lambda u: (-u.center[1], u.center[0]))


def layout_distribute(units, axis, method, gap=0.0, spatial=False):
    """units in selection order. First and last are anchors (fixed for
    'centers' and 'gaps'); 'fixed' keeps only the first one in place."""
    if len(units) < 3 and method != "fixed":
        return {}, "Distribute needs at least 3 units (2 anchors + 1 between)."
    if len(units) < 2:
        return {}, "Distribute needs at least 2 units."
    if spatial:
        units = spatial_order(units, axis)
    ax = 0 if axis == "h" else 1
    first, last = units[0], units[-1]
    sign = 1.0 if last.center[ax] >= first.center[ax] else -1.0

    def flow(u):   # (start, end, center) along the flow direction
        lo, hi = u.rect[ax], u.rect[ax + 2]
        if sign > 0:
            return lo, hi, u.center[ax]
        return -hi, -lo, -u.center[ax]

    middle = sorted(units[1:-1], key=lambda u: flow(u)[2])
    pinned_mid = [u for u in middle if not u.movable]
    middle = [u for u in middle if u.movable]
    out = {}
    warn = ""

    def put(u, delta):
        if abs(delta) > 1e-12:
            d = delta * sign
            out[u.id] = aff_translate(d, 0.0) if ax == 0 else aff_translate(0.0, d)

    f0, f1, fc = flow(first)
    l0, l1, lc = flow(last)
    if method == "centers":
        n = len(middle) + 1
        for i, u in enumerate(middle, 1):
            put(u, fc + (lc - fc) * i / float(n) - flow(u)[2])
        what = "centers evenly"
    elif method == "gaps":
        sizes = sum(flow(u)[1] - flow(u)[0] for u in middle)
        g = (l0 - f1 - sizes) / float(len(middle) + 1)
        pos = f1 + g
        for u in middle:
            s0, s1, _ = flow(u)
            put(u, pos - s0)
            pos += (s1 - s0) + g
        what = "with equal gaps"
        if g < 0:
            warn = " Not enough room - neighbours overlap."
    else:
        seq = middle + ([last] if last.movable else [])
        pos = f1 + gap
        for u in seq:
            s0, s1, _ = flow(u)
            put(u, pos - s0)
            pos += (s1 - s0) + gap
        what = "with a fixed gap"
    note = " ({} pinned left in place)".format(len(pinned_mid)) if pinned_mid else ""
    msg = "Distributed {} {} {}{}.{}".format(
        _plural(len(units), "unit"), "horizontally" if ax == 0 else "vertically",
        what, note, warn)
    return out, msg


def layout_match(units, ref, dim):
    """Uniform scale about each unit's box center to match the reference."""
    rw = ref.rect[2] - ref.rect[0]
    rh = ref.rect[3] - ref.rect[1]
    out = {}
    for u in units:
        if not u.movable or u.id == ref.id:
            continue
        w = u.rect[2] - u.rect[0]
        h = u.rect[3] - u.rect[1]
        if dim == "width":
            s = rw / w if w > 1e-12 else 1.0
        elif dim == "height":
            s = rh / h if h > 1e-12 else 1.0
        else:
            s = min(rw / w if w > 1e-12 else 1.0, rh / h if h > 1e-12 else 1.0)
        if abs(s - 1.0) > 1e-9:
            c = rect_center(u.rect)
            out[u.id] = aff_scale_about(s, c[0], c[1])
    label = {"width": "width", "height": "height", "both": "size (fit)"}[dim]
    return out, "Matched {} of {} to the reference{}.".format(
        label, _plural(len(units) - 1, "unit"), _skipped_note(units))


def layout_grid(units, count, by, gap, origin, cell_align="center"):
    """Arrange units in reading order into a table. by = 'columns' | 'rows'.
    origin = (left, top) in UV. Each unit is centered in its cell."""
    mv = [u for u in units if u.movable]
    n = len(mv)
    if n == 0:
        return {}, "Nothing to arrange - all selected units are pinned."
    count = max(1, int(count))
    if by == "columns":
        cols = min(count, n)
        rows = int(math.ceil(n / float(cols)))
    else:
        rows = min(count, n)
        cols = int(math.ceil(n / float(rows)))
    colw = [0.0] * cols
    rowh = [0.0] * rows
    for i, u in enumerate(mv):
        r, c = divmod(i, cols)
        colw[c] = max(colw[c], u.rect[2] - u.rect[0])
        rowh[r] = max(rowh[r], u.rect[3] - u.rect[1])
    xs, x = [], origin[0]
    for w in colw:
        xs.append(x)
        x += w + gap
    ys, y = [], origin[1]
    for h in rowh:
        ys.append(y)
        y -= h + gap
    out = {}
    for i, u in enumerate(mv):
        r, c = divmod(i, cols)
        w = u.rect[2] - u.rect[0]
        h = u.rect[3] - u.rect[1]
        if cell_align == "topleft":
            du = xs[c] - u.rect[0]
            dv = ys[r] - u.rect[3]
        else:
            du = xs[c] + colw[c] * 0.5 - (u.rect[0] + w * 0.5)
            dv = ys[r] - rowh[r] * 0.5 - (u.rect[1] + h * 0.5)
        if abs(du) > 1e-12 or abs(dv) > 1e-12:
            out[u.id] = aff_translate(du, dv)
    return out, "Arranged {} in a {} x {} grid{}.".format(
        _plural(n, "unit"), cols, rows, _skipped_note(units))


def layout_nudge(units, du, dv):
    out = {u.id: aff_translate(du, dv) for u in units if u.movable}
    return out, "Nudged {}{}.".format(_plural(len(out), "unit"), _skipped_note(units))


def layout_set_bounds(units, cur, new):
    """Move / uniformly scale the whole selection so its box becomes `new`
    (any of the 4 values may be None = unchanged). Scale pivots on the
    box's lower-left corner; aspect ratio is always kept."""
    w0 = cur[2] - cur[0]
    h0 = cur[3] - cur[1]
    u0, v0, w1, h1 = new
    s = 1.0
    if w1 is not None and w0 > 1e-12:
        s = w1 / w0
    elif h1 is not None and h0 > 1e-12:
        s = h1 / h0
    tu = cur[0] if u0 is None else u0
    tv = cur[1] if v0 is None else v0
    aff = affine_compose(aff_translate(tu - cur[0], tv - cur[1]),
                         aff_scale_about(s, cur[0], cur[1]))
    out = {u.id: aff for u in units if u.movable}
    return out, "Updated selection bounds{}.".format(_skipped_note(units))


def snap_ops(ops, rects, texel, tile=(0, 0)):
    """Rigidly shift each result so its box's lower-left corner sits on a
    texel corner (never snaps individual vertices)."""
    out = {}
    for uid, aff in ops.items():
        r = rect_transform(aff, rects[uid])
        du = round((r[0] - tile[0]) / texel) * texel + tile[0] - r[0]
        dv = round((r[1] - tile[1]) / texel) * texel + tile[1] - r[1]
        out[uid] = affine_compose(aff_translate(du, dv), aff)
    return out



# ---- 4.0-c: smart guides ---------------------------------------------------------
def guide_lines(rects, tile, border=0.0):
    """Candidate snap lines: every edge and center of the given rects plus
    the tile edges (inset by border) and tile center."""
    xs, ys = [], []
    for r in rects:
        xs += [r[0], (r[0] + r[2]) * 0.5, r[2]]
        ys += [r[1], (r[1] + r[3]) * 0.5, r[3]]
    tu, tv = tile
    xs += [tu + border, tu + 0.5, tu + 1 - border]
    ys += [tv + border, tv + 0.5, tv + 1 - border]
    return sorted(set(xs)), sorted(set(ys))


def snap_move(rect, du, dv, lines, threshold):
    """Adjust a move so the rect's edges/centers snap to the nearest line.
    Returns (du, dv, snapped_x or None, snapped_y or None)."""
    xs, ys = lines
    moved = (rect[0] + du, rect[1] + dv, rect[2] + du, rect[3] + dv)

    def best(vals, cands):
        hit = None
        for v in vals:
            for c in cands:
                d = c - v
                if abs(d) <= threshold and (hit is None or abs(d) < abs(hit[0])):
                    hit = (d, c)
        return hit
    bx = best((moved[0], (moved[0] + moved[2]) * 0.5, moved[2]), xs)
    by = best((moved[1], (moved[1] + moved[3]) * 0.5, moved[3]), ys)
    if bx:
        du += bx[0]
    if by:
        dv += by[0]
    return du, dv, (bx[1] if bx else None), (by[1] if by else None)


def aff_rotate_about(ang, cx, cy):
    c, s = math.cos(ang), math.sin(ang)
    return ((c, -s, s, c), (cx - (c * cx - s * cy), cy - (s * cx + c * cy)))


def snap_angle(ang, step_deg):
    st = math.radians(step_deg)
    return round(ang / st) * st


# ---- 4.0-d: orientation ------------------------------------------------------------
def convex_hull(points):
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _rot_extent(pts, ang):
    c, s = math.cos(ang), math.sin(ang)
    xs = [c * x - s * y for x, y in pts]
    ys = [s * x + c * y for x, y in pts]
    return max(xs) - min(xs), max(ys) - min(ys)


def min_area_rotation(points, prefer="landscape"):
    """Rotation (radians, CCW) that gives the smallest bounding box
    (rotating calipers on the hull). The result is wider than tall for
    'landscape', taller for 'portrait'."""
    hull = convex_hull(points)
    if len(hull) < 3:
        return 0.0
    best = None
    for i in range(len(hull)):
        a, b = hull[i], hull[(i + 1) % len(hull)]
        ang = -math.atan2(b[1] - a[1], b[0] - a[0])
        w, h = _rot_extent(hull, ang)
        if best is None or w * h < best[0] - 1e-15:
            best = (w * h, ang, w, h)
    _, ang, w, h = best
    if (prefer == "landscape" and h > w + 1e-12) or (prefer == "portrait" and w > h + 1e-12):
        ang += math.pi / 2
    # smallest equivalent turn: keep within (-90, 90]
    while ang > math.pi / 2:
        ang -= math.pi
    while ang <= -math.pi / 2:
        ang += math.pi
    return ang


def principal_angle(tris):
    A, _, _, xx, yy, xy = tri_moments(tris)
    if A <= 0:
        return 0.0
    return 0.5 * math.atan2(2 * xy, xx - yy)


def axis_turn(ang):
    """Smallest rotation that makes a line at `ang` horizontal or vertical."""
    q = math.pi / 2
    return -(ang - round(ang / q) * q)


# ---- 4.0-g: quality checks ----------------------------------------------------------
def mip_conflicts(entries, res, gutter_cells=1):
    """entries: [(id, tris in tile-local 0..1)]. Rasterise at `res` and
    report ids whose dilated footprint touches another id's footprint."""
    rows = {}
    for eid, tris in entries:
        ct = [tuple((p[0] * res, p[1] * res) for p in tr) for tr in tris]
        rows[eid] = dilate_rows(raster_conservative(ct, res, res), gutter_cells, res)
    ids = list(rows)
    spans = {}
    for eid in ids:
        rr = rows[eid]
        nz = [i for i, r in enumerate(rr) if r]
        spans[eid] = (nz[0], nz[-1]) if nz else None
    bad = set()
    for i, a in enumerate(ids):
        if spans[a] is None:
            continue
        for b in ids[i + 1:]:
            if spans[b] is None or spans[b][0] > spans[a][1] or spans[a][0] > spans[b][1]:
                continue
            y0 = max(spans[a][0], spans[b][0])
            y1 = min(spans[a][1], spans[b][1])
            ra, rb = rows[a], rows[b]
            if any(ra[y] & rb[y] for y in range(y0, y1 + 1)):
                bad.add(a)
                bad.add(b)
    return bad


def next_tile(tile):
    u, v = tile
    return (0, v + 1) if u >= 9 else (u + 1, v)


def udim_of(tile):
    return 1001 + tile[0] + 10 * tile[1]


# ---- multi-UDIM packing ------------------------------------------------------------
class MultiPacker(Packer):
    """Packs across several tiles in order: each unit goes to the first tile
    it fits in. Works with Packer.max_scale / max_margin unchanged."""

    def __init__(self, items, tile_obstacles, rotations, border_uv, tick=_noop_tick):
        self.tick = tick
        self.items = sorted(items, key=lambda i: -i.area * i.pscale ** 2)
        self.rots = tuple(rotations)
        self.border_uv = border_uv
        self.packers = [(t, Packer([], ob, rotations, border_uv, t, tick))
                        for t, ob in tile_obstacles]
        self.packs_run = 0

    def pack(self, G, S, margin_uv, partial=False):
        self.packs_run += 1
        remaining = self.items
        out = {}
        for t, pk in self.packers:
            if not remaining:
                break
            pk.items = remaining
            placed, remaining = pk.pack(G, S, margin_uv, partial=True)
            for key, val in placed.items():
                out[key] = (t,) + tuple(val)
        if partial:
            return out, remaining
        return None if remaining else out

    def scale_upper_bound(self, G):
        b = _cells(self.border_uv, G) / float(G)
        lost = 1 - (1 - 2 * b) ** 2
        free = sum(max(pk.obst.free_fraction(G) - lost, 0.0) for _, pk in self.packers)
        free = max(free, 1e-6)
        area = sum(i.area * i.pscale ** 2 for i in self.items) or 1e-12
        hi = math.sqrt(free / area)
        for i in self.items:
            hi = min(hi, (1 - 2 * b) / (i.lmax * i.pscale))
        return max(hi, 1e-6)


QUALITY = {"Draft": 128, "Normal": 256, "High": 512, "Ultra": 1024}
ROTATIONS = {"90\u00b0 steps": (0, 1, 2, 3), "180\u00b0 only": (0, 2), "None": (0,)}

class Rect(object):
    """Axis-aligned rectangle in UV space. Origin is bottom-left."""

    __slots__ = ("x", "y", "w", "h")

    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = float(x), float(y), float(w), float(h)

    @property
    def right(self):
        return self.x + self.w

    @property
    def top(self):
        return self.y + self.h

    @property
    def area(self):
        return self.w * self.h

    @property
    def centre(self):
        return (self.x + self.w * 0.5, self.y + self.h * 0.5)

    def contains(self, other):
        return (self.x <= other.x + EPS and self.y <= other.y + EPS
                and self.right >= other.right - EPS
                and self.top >= other.top - EPS)

    def intersects(self, other):
        return not (other.x >= self.right - EPS or other.right <= self.x + EPS
                    or other.y >= self.top - EPS or other.top <= self.y + EPS)

    def grown(self, amount):
        return Rect(self.x - amount, self.y - amount,
                    self.w + amount * 2.0, self.h + amount * 2.0)

    def __repr__(self):
        return "Rect(%.4f, %.4f, %.4f, %.4f)" % (self.x, self.y, self.w, self.h)

def align(items, edge):
    """Return {key: (du, dv)} that aligns items to an edge of their collective
    bounding box. Pinned items define the target and never move themselves."""
    if edge not in ALIGN_EDGES:
        raise ValueError("Unknown alignment %r; expected one of %s"
                         % (edge, ", ".join(ALIGN_EDGES)))
    live = [i for i in items if not i.is_degenerate]
    if len(live) < 2:
        return {}

    anchors = [i for i in live if i.pinned] or live
    targets = {
        "left": min(i.u for i in anchors),
        "right": max(i.u + i.width for i in anchors),
        "bottom": min(i.v for i in anchors),
        "top": max(i.v + i.height for i in anchors),
        "centre_u": sum(i.u + i.width * 0.5 for i in anchors) / len(anchors),
        "centre_v": sum(i.v + i.height * 0.5 for i in anchors) / len(anchors),
    }
    target = targets[edge]

    deltas = {}
    for item in live:
        if item.pinned:
            continue
        if edge == "left":
            deltas[item.key] = (target - item.u, 0.0)
        elif edge == "right":
            deltas[item.key] = (target - (item.u + item.width), 0.0)
        elif edge == "bottom":
            deltas[item.key] = (0.0, target - item.v)
        elif edge == "top":
            deltas[item.key] = (0.0, target - (item.v + item.height))
        elif edge == "centre_u":
            deltas[item.key] = (target - (item.u + item.width * 0.5), 0.0)
        else:
            deltas[item.key] = (0.0, target - (item.v + item.height * 0.5))
    return {k: d for k, d in deltas.items() if abs(d[0]) > EPS or abs(d[1]) > EPS}

def distribute(items, axis="u", spacing=None):
    """Even out spacing along an axis between the two extreme items.

    spacing=None spreads items evenly between the current extremes, which is
    what artists expect from a distribute button. A number instead sets a fixed
    gap and grows outward from the first item.
    """
    if axis not in ("u", "v"):
        raise ValueError("axis must be 'u' or 'v'")
    live = [i for i in items if not i.is_degenerate]
    if len(live) < 3 and spacing is None:
        return {}

    horizontal = axis == "u"
    ordered = sorted(live, key=lambda i: i.u if horizontal else i.v)
    sizes = [i.width if horizontal else i.height for i in ordered]

    if spacing is None:
        start = ordered[0].u if horizontal else ordered[0].v
        last = ordered[-1]
        end = (last.u + last.width) if horizontal else (last.v + last.height)
        gaps = len(ordered) - 1
        free_space = (end - start) - sum(sizes)
        gap = free_space / gaps if gaps > 0 else 0.0
    else:
        start = ordered[0].u if horizontal else ordered[0].v
        gap = float(spacing)

    deltas = {}
    cursor = start
    for item, size in zip(ordered, sizes):
        current = item.u if horizontal else item.v
        shift = cursor - current
        if item.pinned:
            # A pinned item anchors the run: resync the cursor to where it
            # actually sits rather than dragging it.
            cursor = (item.u if horizontal else item.v) + size + gap
            continue
        if abs(shift) > EPS:
            deltas[item.key] = (shift, 0.0) if horizontal else (0.0, shift)
        cursor += size + gap
    return deltas

def orientation_correction(points, snap=None):
    """Rotation in degrees to straighten a shell. `snap` rounds to a grid.

    Uses the reference engine's rotating-calipers minimum-area box, which is
    exact, rather than the moment-based principal axis a lopsided vertex
    distribution can skew.
    """
    angle = math.degrees(min_area_rotation(points))
    if snap:
        angle = round(angle / float(snap)) * float(snap)
    while angle > 90.0:
        angle -= 180.0
    while angle <= -90.0:
        angle += 180.0
    return angle

def texel_density(uv_area, world_area, map_size):
    """Pixels per world unit for a shell: map_size * sqrt(uv / world).

    The square root because density is linear (pixels per unit length)
    while both areas are squared quantities. Zero when either area is
    degenerate, which density_report treats as unmeasurable rather than as
    infinitely dense.
    """
    if uv_area <= 0.0 or world_area <= 0.0 or map_size <= 0:
        return 0.0
    return float(map_size) * math.sqrt(uv_area / world_area)


def density_report(entries, target=None, tolerance=0.05):
    """Classify shells by density. entries: [(key, uv_area, world_area)].

    tolerance is fractional: 0.05 means within 5% of target is acceptable.
    Target defaults to the area-weighted mean, which is the density the asset
    already mostly has - a better reference than an arbitrary number.
    """
    measured = []
    for key, uv_area, world_area, map_size in entries:
        measured.append((key, texel_density(uv_area, world_area, map_size),
                         abs(world_area)))

    usable = [(k, d, w) for k, d, w in measured if d > 0.0]
    if not usable:
        return OrderedDict([("target", 0.0), ("under", []), ("over", []),
                            ("ok", []), ("unmeasurable", [k for k, _, _ in measured])])

    if target is None:
        total_weight = sum(w for _, _, w in usable)
        target = (sum(d * w for _, d, w in usable) / total_weight
                  if total_weight > 0 else usable[0][1])

    under, over, ok = [], [], []
    for key, density, _ in usable:
        ratio = density / target
        if ratio < 1.0 - tolerance:
            under.append((key, density, ratio))
        elif ratio > 1.0 + tolerance:
            over.append((key, density, ratio))
        else:
            ok.append((key, density, ratio))

    return OrderedDict([
        ("target", target),
        ("under", sorted(under, key=lambda r: r[2])),
        ("over", sorted(over, key=lambda r: -r[2])),
        ("ok", ok),
        ("unmeasurable", [k for k, d, _ in measured if d <= 0.0]),
    ])

def find_overlaps(items, ignore_pinned_pairs=True):
    """Pairs of items whose bounding boxes overlap.

    Sweep over a u-sorted list, so this is near-linear for a laid-out sheet and
    only degrades when everything is genuinely stacked - the case where every
    pair really does overlap.
    """
    live = [i for i in items if not i.is_degenerate]
    order = sorted(range(len(live)), key=lambda i: live[i].u)
    pairs = []
    count = len(order)

    for a_pos in range(count):
        a = live[order[a_pos]]
        a_rect = Rect(a.u, a.v, a.width, a.height)
        for b_pos in range(a_pos + 1, count):
            b = live[order[b_pos]]
            if b.u > a.u + a.width + EPS:
                break
            if ignore_pinned_pairs and a.pinned and b.pinned:
                continue
            if a_rect.intersects(Rect(b.u, b.v, b.width, b.height)):
                pairs.append((a.key, b.key))
    return pairs

def find_out_of_bounds(items, udims=None):
    """Items straying outside the tiles they are supposed to occupy."""
    allowed = set(udims or [1001])
    offenders = []
    for item in items:
        if item.is_degenerate:
            continue
        corners = ((item.u, item.v),
                   (item.u + item.width - EPS, item.v + item.height - EPS))
        tiles = set(offset_to_udim(u, v) for u, v in corners)
        if len(tiles) > 1:
            offenders.append((item.key, "straddles tiles %s"
                              % sorted(tiles)))
        elif not tiles.issubset(allowed):
            offenders.append((item.key, "in tile %d, not in the target set"
                              % sorted(tiles)[0]))
    return offenders


# =====================================================================
# ADAPTER - the box-based item and the pack() the M5b handler calls
# =====================================================================

class PackItem(object):
    """Light box item for align / distribute / QC. Packing uses RasterItem."""
    __slots__ = ("key", "u", "v", "width", "height", "pinned", "payload",
                 "tris", "area")

    def __init__(self, key, width, height, u=0.0, v=0.0, pinned=False,
                 payload=None, tris=None, area=None):
        self.key, self.u, self.v = key, u, v
        self.width, self.height = width, height
        self.pinned, self.payload = pinned, payload
        self.tris = tris            # UV triangles, when the caller has them
        self.area = area if area is not None else width * height

    @property
    def is_degenerate(self):
        return self.width <= EPS or self.height <= EPS


class Placement(object):
    __slots__ = ("key", "u", "v", "rotated", "scale", "tile", "matrix",
                 "translation")

    def __init__(self, key, u, v, rotated, scale, tile, matrix=None,
                 translation=None):
        self.key, self.u, self.v = key, u, v
        self.rotated, self.scale, self.tile = rotated, scale, tile
        # The full affine from original UVs to packed UVs. The handler applies
        # this directly rather than decomposing it.
        self.matrix = matrix
        self.translation = translation


class PackResult(object):
    def __init__(self):
        self.placements = {}
        self.unplaced = []
        self.scale = 1.0
        self.tiles_used = []
        self.seconds = 0.0
        self._occ = 0.0

    @property
    def mean_occupancy(self):
        return self._occ


def _tile_of_udim(udim):
    n = int(udim) - 1001
    return (n % 10, n // 10)


def pack(items, udims=None, padding=0.002, allow_rotation=True, sort="area",
         scale_to_fit=False, quality="Normal", ref_res=None):
    """Drive the raster engine with the M5b handler's box-item contract.

    Each item must carry .tris (UV triangles) for real-shape packing; an
    item with no triangles falls back to its bounding rectangle so nothing
    crashes when M2 has not supplied geometry yet.
    """
    import time as _time
    started = _time.time()
    result = PackResult()
    udims = list(udims or [1001])
    tiles = [_tile_of_udim(u) for u in udims]

    live = [it for it in items if not it.is_degenerate and not it.pinned]
    result.unplaced = [it.key for it in items if it.is_degenerate]
    if not live:
        result.tiles_used = []
        result.seconds = _time.time() - started
        return result

    G = QUALITY.get(quality, 256)
    Gs = min(G, 256)
    rr = ref_res or max(96, min(384, G // 2))
    rots = (0, 1, 2, 3) if allow_rotation else (0,)

    def _tris(it):
        if it.tris:
            return it.tris
        u0, v0, u1, v1 = it.u, it.v, it.u + it.width, it.v + it.height
        return [((u0, v0), (u1, v0), (u1, v1)),
                ((u0, v0), (u1, v1), (u0, v1))]

    raster = []
    for it in live:
        u0, v0 = it.u, it.v
        bbox = (u0, v0, u0 + it.width, v0 + it.height)
        raster.append(RasterItem(it.key, _tris(it), bbox, it.area, 1.0, rr))

    tile_obst = [(t, ObstacleSet([], t)) for t in tiles]
    packer = MultiPacker(raster, tile_obst, rots, padding, _noop_tick)

    try:
        if scale_to_fit:
            # Shrink until everything fits (never enlarging past 1.0).
            S = packer.max_scale(Gs, G, padding, cap=1.0)
        else:
            # Pack at true size. Whatever does not fit is returned unplaced,
            # for overflow tiles or quarantine to handle - we do NOT silently
            # shrink, which would change every shell's texel density without
            # the artist asking.
            S = 1.0
        placed = packer.pack(G, S, padding, partial=True)
    except UserError:
        result.seconds = _time.time() - started
        result.unplaced.extend(it.key for it in live)
        return result

    placements, unplaced = placed
    by_key = {r.key: r for r in raster}
    used_tiles = []
    for key, (tile, k, x, y) in placements.items():
        A, (tx, ty) = by_key[key].transform(k, x, y, G, S, tile)
        result.placements[key] = Placement(
            key=key, u=tx, v=ty, rotated=bool(k % 2), scale=S, tile=tile,
            matrix=A, translation=(tx, ty))
        if tile not in used_tiles:
            used_tiles.append(tile)
    result.unplaced.extend(it.key for it in unplaced)
    result.scale = S
    result.tiles_used = [1001 + t[0] + 10 * t[1] for t in used_tiles]
    total = sum(it.area for it in live)
    result._occ = (total / max(len(used_tiles), 1)) if used_tiles else 0.0
    result.seconds = _time.time() - started
    return result
