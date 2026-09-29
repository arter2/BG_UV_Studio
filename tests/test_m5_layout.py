"""Panel layout checks for M5's PANEL_LAYOUT, which M5c draws the tabs from.

    python -m unittest discover -s tests
"""

import os
import sys
import unittest
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvstudio_v7_7 as bundle  # noqa: E402

m5 = bundle.m5


def section_tools(tab, title):
    return [cell.get("tool") for t, s, cell in m5.layout_cells()
            if t == tab and s == title]


class PanelLayout(unittest.TestCase):

    def test_layout_names_only_real_tools_and_settings(self):
        self.assertEqual(m5.validate_layout(), [])

    def test_align_and_snap_has_distribute(self):
        # Earlier builds had Distribute beside the align buttons; v7.7 lost
        # it from this section. Keep both directions here.
        tools = section_tools("UV", "Align and Snap")
        self.assertIn("align_left", tools)
        self.assertIn("distribute_u", tools)
        self.assertIn("distribute_v", tools)

    def test_distribute_directions(self):
        self.assertEqual(m5.find_tool("distribute_u").defaults["axis"], "u")
        self.assertEqual(m5.find_tool("distribute_v").defaults["axis"], "v")


if __name__ == "__main__":
    unittest.main()


class ChecksLog(unittest.TestCase):

    def test_checks_tab_has_a_log(self):
        cells = [c for t, s, c in m5.layout_cells()
                 if t == "Checks" and s == "Log"]
        self.assertEqual([c.get("name") for c in cells], ["log"])

    def test_findings_are_listed(self):
        lines = bundle.m5c.detail_lines(OrderedDict([
            ("overlaps", [("a", "b"), ("c", "d")]), ("count", 2),
            ("flipped", []), ("by_tile", {1001: 3})]))
        self.assertEqual(lines, ["overlaps: 2 - ('a', 'b'), ('c', 'd')",
                                 "flipped: none", "by_tile: 1 - 1001=3"])

    def test_long_findings_are_capped(self):
        line = bundle.m5c.detail_lines({"shells": list(range(30))})[0]
        self.assertTrue(line.startswith("shells: 30 - 0, 1,"))
        self.assertTrue(line.endswith("(+18 more)"))
