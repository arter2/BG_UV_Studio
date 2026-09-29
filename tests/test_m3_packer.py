"""Characterization tests for M3, the packer.

M3 is pure Python and the core of the tool, but ships with no self test.
These pin its current behaviour from the outside, through the built bundle,
so a refactor of the packer can be checked without Maya:

    python -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvstudio_v7_7 as bundle  # noqa: E402

m3 = bundle.m3


def square(key, size, u=0.0, v=0.0, **kwargs):
    tris = [((u, v), (u + size, v), (u + size, v + size)),
            ((u, v), (u + size, v + size), (u, v + size))]
    return m3.PackItem(key, size, size, u=u, v=v, tris=tris, **kwargs)


def placed_bounds(item, placement):
    """UV bounds of item's triangles after the placement's affine."""
    pts = [p for tri in m3.affine_tris((placement.matrix,
                                        placement.translation), item.tris)
           for p in tri]
    us = [p[0] for p in pts]
    vs = [p[1] for p in pts]
    return m3.Rect(min(us), min(vs), max(us) - min(us), max(vs) - min(vs))


class UdimMath(unittest.TestCase):

    def test_offset_to_udim(self):
        self.assertEqual(m3.offset_to_udim(0.5, 0.5), 1001)
        self.assertEqual(m3.offset_to_udim(1.5, 0.5), 1002)
        self.assertEqual(m3.offset_to_udim(0.5, 1.5), 1011)
        self.assertEqual(m3.offset_to_udim(9.5, 2.5), 1030)

    def test_tile_and_udim_round_trip(self):
        for udim in (1001, 1002, 1010, 1011, 1034):
            self.assertEqual(m3.udim_of(m3._tile_of_udim(udim)), udim)


class Bitmaps(unittest.TestCase):

    def test_bit_runs(self):
        self.assertEqual(m3.bit_runs(0), [])
        self.assertEqual(m3.bit_runs(0b1), [(0, 1)])
        self.assertEqual(m3.bit_runs(0b1110011), [(0, 2), (4, 3)])

    def test_popcount(self):
        self.assertEqual(m3.popcount(0b101101), 4)


class RectOps(unittest.TestCase):

    def test_touching_rects_do_not_intersect(self):
        a = m3.Rect(0, 0, 1, 1)
        self.assertFalse(a.intersects(m3.Rect(1, 0, 1, 1)))
        self.assertTrue(a.intersects(m3.Rect(0.5, 0.5, 1, 1)))

    def test_contains_and_grown(self):
        a = m3.Rect(0, 0, 1, 1)
        self.assertTrue(a.grown(0.1).contains(a))
        self.assertFalse(a.contains(a.grown(0.1)))
        self.assertAlmostEqual(a.grown(0.5).area, 4.0)


class QualityChecks(unittest.TestCase):

    def test_find_overlaps(self):
        items = [square("a", 0.3), square("b", 0.3, u=0.2),
                 square("c", 0.2, u=0.6)]
        self.assertEqual(m3.find_overlaps(items), [("a", "b")])

    def test_pinned_pairs_ignored_by_default(self):
        items = [square("a", 0.3, pinned=True),
                 square("b", 0.3, u=0.1, pinned=True)]
        self.assertEqual(m3.find_overlaps(items), [])
        self.assertEqual(len(m3.find_overlaps(items, False)), 1)

    def test_find_out_of_bounds(self):
        items = [square("in", 0.2, u=0.1, v=0.1),
                 square("straddle", 0.2, u=0.9),
                 square("other", 0.2, u=1.1)]
        found = dict(m3.find_out_of_bounds(items))
        self.assertNotIn("in", found)
        self.assertIn("straddles", found["straddle"])
        self.assertIn("not in the target set", found["other"])


class Align(unittest.TestCase):

    def test_align_left_moves_only_unpinned(self):
        items = [square("a", 0.1, u=0.2), square("b", 0.1, u=0.5),
                 square("pin", 0.1, u=0.4, pinned=True)]
        deltas = m3.align(items, "left")
        self.assertNotIn("pin", deltas)
        self.assertAlmostEqual(deltas["a"][0], 0.2)
        self.assertAlmostEqual(deltas["b"][0], -0.1)

    def test_unknown_edge_raises(self):
        with self.assertRaises(ValueError):
            m3.align([square("a", 0.1)], "diagonal")


class Pack(unittest.TestCase):

    def pack(self, items, **kwargs):
        kwargs.setdefault("quality", "Draft")
        return m3.pack(items, **kwargs)

    def assert_valid_layout(self, items, result, udims=(1001,)):
        by_key = {it.key: it for it in items}
        rects = []
        for key, placement in result.placements.items():
            self.assertIn(placement.tile[0] + 10 * placement.tile[1] + 1001,
                          udims)
            rect = placed_bounds(by_key[key], placement)
            tile = m3.Rect(placement.tile[0], placement.tile[1], 1, 1)
            self.assertTrue(tile.contains(rect), "%s leaves its tile" % key)
            rects.append((key, rect))
        for i, (ka, ra) in enumerate(rects):
            for kb, rb in rects[i + 1:]:
                self.assertFalse(ra.intersects(rb),
                                 "%s overlaps %s" % (ka, kb))

    def test_squares_fit_without_overlap_at_true_size(self):
        items = [square("s%d" % i, 0.2, u=i * 0.05, v=i * 0.03)
                 for i in range(12)]
        result = self.pack(items)
        self.assertEqual(sorted(result.placements),
                         sorted(it.key for it in items))
        self.assertEqual(result.unplaced, [])
        self.assertEqual(result.scale, 1.0)
        self.assertEqual(result.tiles_used, [1001])
        self.assert_valid_layout(items, result)
        for key, placement in result.placements.items():
            self.assertAlmostEqual(placed_bounds(
                dict((it.key, it) for it in items)[key], placement).w,
                0.2, places=6)

    def test_overflow_is_returned_not_shrunk(self):
        items = [square("s%d" % i, 0.4) for i in range(6)]
        result = self.pack(items)
        self.assertEqual(result.scale, 1.0)
        self.assertTrue(result.unplaced)
        self.assertEqual(len(result.placements) + len(result.unplaced), 6)
        self.assert_valid_layout(items, result)

    def test_overflow_spills_into_next_udim(self):
        items = [square("s%d" % i, 0.4) for i in range(6)]
        result = self.pack(items, udims=[1001, 1002])
        self.assertEqual(result.unplaced, [])
        self.assertEqual(sorted(result.tiles_used), [1001, 1002])
        self.assert_valid_layout(items, result, udims=(1001, 1002))

    def test_scale_to_fit_shrinks_but_never_enlarges(self):
        items = [square("s%d" % i, 0.4) for i in range(6)]
        result = self.pack(items, scale_to_fit=True)
        self.assertEqual(result.unplaced, [])
        self.assertLess(result.scale, 1.0)
        self.assert_valid_layout(items, result)
        small = [square("t", 0.1)]
        self.assertLessEqual(self.pack(small, scale_to_fit=True).scale, 1.0)

    def test_no_rotation_keeps_orientation(self):
        items = [m3.PackItem("r", 0.5, 0.1, tris=[
            ((0, 0), (0.5, 0), (0.5, 0.1)), ((0, 0), (0.5, 0.1), (0, 0.1))])]
        result = self.pack(items, allow_rotation=False)
        self.assertFalse(result.placements["r"].rotated)

    def test_pinned_and_degenerate_items(self):
        items = [square("live", 0.2), square("pin", 0.2, pinned=True),
                 m3.PackItem("flat", 0.2, 0.0)]
        result = self.pack(items)
        self.assertEqual(list(result.placements), ["live"])
        self.assertEqual(result.unplaced, ["flat"])

    def test_empty_input(self):
        result = self.pack([])
        self.assertEqual(result.placements, {})
        self.assertEqual(result.tiles_used, [])

    def test_items_without_triangles_pack_as_boxes(self):
        items = [m3.PackItem("box%d" % i, 0.3, 0.3) for i in range(4)]
        result = self.pack(items)
        self.assertEqual(len(result.placements), 4)


if __name__ == "__main__":
    unittest.main()
