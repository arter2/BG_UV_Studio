"""Build the single-file UV Studio bundle from src/uvstudio/.

    python tools/build.py              write uvstudio_v7_7.py
    python tools/build.py --check      exit 1 if the bundle is out of date

The bundle embeds each module as a raw string and exec's it into its own
module object at import (see _bootstrap in tools/bundle_tail.py.in). Edit
the files under src/uvstudio/ and rebuild; never edit the bundle by hand.
"""

from __future__ import print_function

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "uvstudio")
TOOLS = os.path.join(ROOT, "tools")
OUTPUT = os.path.join(ROOT, "uvstudio_v7_7.py")

# Execution order matters: _bootstrap runs sources in this order, and M5c
# must run before M1 so M1's factories see the panel module.
# The flag keeps the leading "\" line continuation some sources were
# embedded with, so the output stays byte-identical to the shipped build.
MODULES = [
    ("uvstudio_m2_scene_bridge", True),
    ("uvstudio_m3_packer_core", True),
    ("uvstudio_m4_recipe", True),
    ("uvstudio_m6_texture", False),
    ("uvstudio_m5_ui", True),
    ("uvstudio_m5b_handlers", True),
    ("uvstudio_m5c_panel", True),
    ("uvstudio_m9_cluster_map", True),
    ("uvstudio_m1_hosted_editor", True),
]


def _read(path):
    with open(path, "r") as handle:
        return handle.read()


INCLUDE = "# @include "


def _expand(body):
    """Inline "# @include <path>" lines from src/uvstudio/.

    Shared helpers live in one file and are copied into each module at build
    time. They cannot be imported at run time instead: the modules exec in a
    fixed order and M1 does not load outside Maya, so a runtime import from
    one module into another would make one module's failure break the other.
    """
    out = []
    for line in body.splitlines(True):
        if line.startswith(INCLUDE):
            out.append(_read(os.path.join(SRC, line[len(INCLUDE):].strip())))
        else:
            out.append(line)
    return "".join(out)


def build():
    parts = [_read(os.path.join(TOOLS, "bundle_head.py.in"))]
    for index, (name, continuation) in enumerate(MODULES):
        body = _expand(_read(os.path.join(SRC, name + ".py")))
        if "'''" in body:
            raise ValueError("%s contains ''' and cannot be embedded" % name)
        if index:
            parts.append("\n")
        parts.append("_SOURCES['%s'] = r'''%s%s'''\n"
                     % (name, "\\\n" if continuation else "", body))
    parts.append(_read(os.path.join(TOOLS, "bundle_tail.py.in")))
    return "".join(parts)


def main(argv):
    text = build()
    compile(text, OUTPUT, "exec")
    if "--check" in argv:
        current = _read(OUTPUT) if os.path.exists(OUTPUT) else ""
        if current != text:
            print("%s is out of date; run tools/build.py" % OUTPUT)
            return 1
        print("up to date")
        return 0
    with open(OUTPUT, "w") as handle:
        handle.write(text)
    print("wrote %s (%d bytes)" % (OUTPUT, len(text)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
