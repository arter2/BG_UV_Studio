"""Panel layout checks for M5's PANEL_LAYOUT, which M5c draws the tabs from.

    python -m unittest discover -s tests
"""

import os
import sys
import unittest

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
