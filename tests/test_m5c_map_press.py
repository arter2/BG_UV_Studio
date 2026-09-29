"""A panel press while the Cluster Map is showing targets the map's shells.

The panel class needs Qt to construct, so run_tool_id is called on a stand-in
object carrying only what it uses, with a fake M1 host holding the map.

    python -m unittest discover -s tests
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvstudio_v7_7 as bundle  # noqa: E402

Panel = bundle.m5c.UVStudioToolsPanel
M1 = "uvstudio_m1_hosted_editor"


class FakeMap(object):
    def __init__(self, selected, push_ok=True):
        self.controller = types.SimpleNamespace(
            selected_units=lambda: list(selected))
        self.push_ok = push_ok
        self.pushed = 0

    def _select_for_command(self):
        self.pushed += 1
        return self.push_ok


class FakeRunner(object):
    def __init__(self, events):
        self.events = events

    def run(self, tool_id, overrides=None):
        self.events.append(("run", tool_id))
        return types.SimpleNamespace(ok=False, message="stop here")


class MapPress(unittest.TestCase):

    def setUp(self):
        self._saved = sys.modules.get(M1)
        self.events = []
        self.logs = []
        self.panel = types.SimpleNamespace(
            runner=FakeRunner(self.events),
            log=lambda level, msg: self.logs.append((level, msg)),
            _push_map_selection=Panel._push_map_selection)

    def tearDown(self):
        if self._saved is None:
            sys.modules.pop(M1, None)
        else:
            sys.modules[M1] = self._saved

    def host(self, view, map_widget):
        module = types.ModuleType(M1)
        module._HOST = types.SimpleNamespace(view=view, map_widget=map_widget)
        sys.modules[M1] = module

    def press(self):
        Panel.run_tool_id(self.panel, "rotate_cw")

    def test_map_selection_is_pushed_before_the_tool_runs(self):
        view = FakeMap(["unit 3"])
        self.host("map", view)
        self.press()
        self.assertEqual(view.pushed, 1)
        self.assertEqual(self.events, [("run", "rotate_cw")])

    def test_a_failed_push_runs_nothing(self):
        self.host("map", FakeMap(["unit 3"], push_ok=False))
        self.press()
        self.assertEqual(self.events, [])
        self.assertIn("could not be selected", self.logs[-1][1])

    def test_no_map_selection_leaves_maya_selection_alone(self):
        view = FakeMap([])
        self.host("map", view)
        self.press()
        self.assertEqual(view.pushed, 0)
        self.assertEqual(self.events, [("run", "rotate_cw")])

    def test_uv_editor_view_ignores_the_map(self):
        view = FakeMap(["unit 3"])
        self.host("editor", view)
        self.press()
        self.assertEqual(view.pushed, 0)
        self.assertEqual(self.events, [("run", "rotate_cw")])

    def test_no_host(self):
        sys.modules.pop(M1, None)
        self.press()
        self.assertEqual(self.events, [("run", "rotate_cw")])


if __name__ == "__main__":
    unittest.main()
