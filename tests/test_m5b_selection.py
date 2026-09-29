"""Which shells a transform acts on, for each kind of Maya selection.

Drives M5b's rotate handler against a fake maya.cmds that reports a
selection the way Maya does (components on the transform path, e.g.
"|m.map[100]"), with the real M2 selection code in between.

    python -m unittest discover -s tests
"""

import os
import re
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvstudio_v7_7 as bundle  # noqa: E402

m5b = bundle.m5b
M2 = "uvstudio_m2_scene_bridge"
SHAPE = "|m|mShape"
SEL = []
FACE_TO_UV = {7: list(range(100, 104))}          # one face on shell 1
_saved = {}


def _ls(*args, **kw):
    if kw.get("selection"):
        return list(SEL)
    if kw.get("hilite"):
        return ["|m"]            # component mode: the mesh is hilited
    names = args[0] if args else []
    names = [names] if isinstance(names, str) else names
    out = []
    for name in names:
        m = re.match(r"(.*)\.map\[(\d+):(\d+)\]$", name)
        if m and kw.get("flatten"):
            out += ["%s.map[%d]" % (m.group(1), i)
                    for i in range(int(m.group(2)), int(m.group(3)) + 1)]
        else:
            out.append(name)
    return out


def _convert(items, **kw):
    out = []
    for item in items:
        m = re.match(r"(.*)\.f\[(\d+)\]$", item)
        if m:
            out += ["%s.map[%d]" % (m.group(1), i)
                    for i in FACE_TO_UV[int(m.group(2))]]
    return out


def setUpModule():
    _saved.update((k, sys.modules.get(k)) for k in
                  ("maya", "maya.cmds", "maya.mel", "maya.api",
                   "maya.api.OpenMaya", M2))
    if bundle.m2 is not None:
        raise unittest.SkipTest("real Maya present")
    m5b._install_test_stubs()
    cmds = sys.modules["maya.cmds"]
    cmds.ls = _ls
    cmds.listRelatives = lambda node, **kw: [SHAPE] if node == "|m" else []
    cmds.objectType = lambda node, isType=None: node == SHAPE
    cmds.polyListComponentConversion = _convert
    cmds.getAttr = lambda *a, **k: False
    cmds.select = lambda *a, **k: None
    module = types.ModuleType(M2)
    module.__dict__["__file__"] = "<uvstudio bundle>"
    sys.modules[M2] = module
    exec(compile(bundle._SOURCES[M2], "<uvstudio:%s>" % M2, "exec"),
         module.__dict__)


def tearDownModule():
    for name, module in _saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class RotateTargets(unittest.TestCase):

    def setUp(self):
        m2 = sys.modules[M2]
        self.shells = [m2.Shell(i, list(range(i * 100, i * 100 + 20)),
                                (u, u + 0.2, v, v + 0.2), 0.04, 0, mesh=SHAPE)
                       for i, (u, v) in enumerate(((.1, .1), (.5, .1),
                                                   (.1, .5), (.5, .5)))]
        self.units = [m2.LayoutUnit(i, [s]) for i, s in enumerate(self.shells)]

    def rotated_shells(self, selection):
        SEL[:] = selection
        bridge = m5b._FakeBridge(m5b._make_report(self.shells, self.units))
        m5b.rotate(settings={"degrees": 90.0}, meshes=["|m"], bridge=bridge)
        return sum(e[2] for e in bridge.journal if e[0] == "rotate") // 20

    def test_one_shell_of_uvs(self):
        uvs = ["|m.map[%d]" % i for i in range(100, 120)]
        self.assertEqual(self.rotated_shells(uvs), 1)

    def test_a_uv_range(self):
        self.assertEqual(self.rotated_shells(["|m.map[100:119]"]), 1)

    def test_a_single_uv_turns_its_whole_shell(self):
        self.assertEqual(self.rotated_shells(["|m.map[105]"]), 1)

    def test_faces_are_converted(self):
        self.assertEqual(self.rotated_shells(["|m.f[7]"]), 1)

    def test_object_plus_components_uses_the_components(self):
        uvs = ["|m.map[%d]" % i for i in range(100, 120)]
        self.assertEqual(self.rotated_shells(["|m"] + uvs), 1)

    def test_selected_mesh_means_every_shell(self):
        self.assertEqual(self.rotated_shells(["|m"]), 4)

    def test_nothing_selected_rotates_nothing(self):
        # Component mode, empty selection, mesh only hilited. v7.7 rotated
        # every shell on the sheet here.
        with self.assertRaises(RuntimeError) as caught:
            self.rotated_shells([])
        self.assertIn("Nothing is selected", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
