\
"""
UV Studio - Module 4: Recipe / History
======================================

PURPOSE
    Every operation is a recorded step that can be disabled, reordered and
    replayed from the original UVs. Zero Maya imports: steps are data, replay
    produces a plan, and Module 2 executes it. That keeps the history testable
    without Maya and keeps execution in the one module allowed to touch the
    scene.

THE HARD PART - REPLAY FIDELITY
    Some Maya operations are not reproducible. Running u3dUnfold twice on the
    same input can give slightly different results, so replaying a recipe by
    re-running every command would drift. Steps therefore declare a fidelity:

      EXACT       parameters fully determine the result (move, rotate, scale,
                  flip, stack). Replay re-runs the command.
      SNAPSHOT    result cannot be reproduced (unfold, optimize, relax).
                  Replay applies the stored result instead of re-running.
      TOPOLOGY    changes UV connectivity (cut, sew, merge, split). Exact, but
                  acts as a reorder barrier - see below.

REORDER BARRIERS
    A cut changes which UVs exist and which shells they belong to. Moving a
    later step before that cut would apply it to indices that did not exist
    yet. TOPOLOGY steps are therefore barriers: steps cannot be reordered
    across them, and disabling one invalidates the snapshots of everything
    after it. This is the single rule that keeps replay honest.

SNAPSHOT BUDGET
    A snapshot of 200k UVs is ~3 MB. A long session has hundreds of steps.
    Snapshots therefore store only the UVs a step actually touched, and the
    store evicts oldest-first under a budget. An evicted snapshot does not
    corrupt the recipe - the step is marked degraded and reports that it must
    be re-run rather than replayed.

USAGE
    Runs standalone on any Python:
        python uvstudio_m4_recipe.py

Target: Python 3.7+ (Maya 2022) and later
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections import OrderedDict

__version__ = "1.1.0"
MODULE_ID = "M4"

EXACT = "exact"
SNAPSHOT = "snapshot"
TOPOLOGY = "topology"
FIDELITIES = (EXACT, SNAPSHOT, TOPOLOGY)

# Rough bytes per stored UV: index + two floats, plus dict overhead. Used for
# budgeting only; precision here is not worth the cost of measuring.
BYTES_PER_UV = 40
DEFAULT_BUDGET_BYTES = 64 * 1024 * 1024


# =====================================================================
# SECTION 1 - Snapshots
# =====================================================================

class Snapshot(object):
    """The UVs a step changed, stored as a sparse delta.

    Only touched indices are kept. A move of one shell on a 130k-UV mesh
    stores a few thousand entries, not 130k.
    """

    __slots__ = ("mesh", "uvs", "created")

    def __init__(self, mesh, uvs, created=None):
        self.mesh = mesh
        self.uvs = uvs                  # {uv_index: (u, v)}
        self.created = created or time.time()

    @property
    def count(self):
        return len(self.uvs)

    @property
    def bytes(self):
        return self.count * BYTES_PER_UV

    def to_dict(self):
        return OrderedDict([
            ("mesh", self.mesh),
            ("created", self.created),
            # JSON keys must be strings; restored as ints on load.
            ("uvs", {str(k): list(v) for k, v in self.uvs.items()}),
        ])

    @classmethod
    def from_dict(cls, data):
        uvs = {int(k): tuple(v) for k, v in (data.get("uvs") or {}).items()}
        return cls(data.get("mesh"), uvs, data.get("created"))


class SnapshotStore(object):
    """Holds snapshots under a byte budget, evicting oldest first.

    Eviction never deletes a step. It removes the stored result, and the step
    reports itself degraded: replayable only by re-running, which for a
    SNAPSHOT step means the result may differ from what the artist saw.
    Silently dropping that distinction would make replay quietly wrong.
    """

    def __init__(self, budget_bytes=DEFAULT_BUDGET_BYTES, protect_recent=5):
        self.budget_bytes = int(budget_bytes)
        self.protect_recent = int(protect_recent)
        self._snapshots = OrderedDict()     # step_id -> Snapshot
        self.evicted = []                   # step_ids, oldest first

    def put(self, step_id, snapshot):
        self._snapshots.pop(step_id, None)
        self._snapshots[step_id] = snapshot
        self._enforce_budget()

    def get(self, step_id):
        return self._snapshots.get(step_id)

    def has(self, step_id):
        return step_id in self._snapshots

    def drop(self, step_id):
        return self._snapshots.pop(step_id, None) is not None

    @property
    def bytes(self):
        return sum(s.bytes for s in self._snapshots.values())

    @property
    def count(self):
        return len(self._snapshots)

    def _enforce_budget(self):
        """Evict oldest first, but never the most recent `protect_recent`.

        Protecting the tail matters: the steps most likely to be toggled or
        replayed are the ones just made, and evicting those makes undo of
        recent work lossy exactly when it is most likely to be needed.
        """
        if self.budget_bytes <= 0:
            return
        while self.bytes > self.budget_bytes:
            ids = list(self._snapshots)
            evictable = ids[:-self.protect_recent] if self.protect_recent else ids
            if not evictable:
                break                       # everything left is protected
            victim = evictable[0]
            self._snapshots.pop(victim, None)
            self.evicted.append(victim)


# =====================================================================
# SECTION 2 - Steps
# =====================================================================

class Step(object):
    """One recorded operation."""

    __slots__ = ("id", "kind", "label", "params", "fidelity", "targets",
                 "enabled", "created", "note")

    def __init__(self, kind, label=None, params=None, fidelity=EXACT,
                 targets=None, enabled=True, step_id=None, created=None,
                 note=""):
        if fidelity not in FIDELITIES:
            raise ValueError("Unknown fidelity %r; expected one of %s"
                             % (fidelity, ", ".join(FIDELITIES)))
        self.id = step_id or uuid.uuid4().hex[:12]
        self.kind = kind
        self.label = label or kind
        self.params = dict(params or {})
        self.fidelity = fidelity
        self.targets = list(targets or [])      # mesh names
        self.enabled = bool(enabled)
        self.created = created or time.time()
        self.note = note

    @property
    def is_barrier(self):
        return self.fidelity == TOPOLOGY

    @property
    def needs_snapshot(self):
        return self.fidelity == SNAPSHOT

    def to_dict(self):
        return OrderedDict([
            ("id", self.id), ("kind", self.kind), ("label", self.label),
            ("params", self.params), ("fidelity", self.fidelity),
            ("targets", self.targets), ("enabled", self.enabled),
            ("created", self.created), ("note", self.note),
        ])

    @classmethod
    def from_dict(cls, data):
        return cls(kind=data["kind"], label=data.get("label"),
                   params=data.get("params"), fidelity=data.get("fidelity", EXACT),
                   targets=data.get("targets"), enabled=data.get("enabled", True),
                   step_id=data.get("id"), created=data.get("created"),
                   note=data.get("note", ""))

    def __repr__(self):
        return ("<Step %s %s%s%s>"
                % (self.id, self.kind,
                   "" if self.enabled else " DISABLED",
                   " [%s]" % self.fidelity if self.fidelity != EXACT else ""))


# =====================================================================
# SECTION 3 - Recipe
# =====================================================================

class ReorderError(RuntimeError):
    pass


class Recipe(object):
    """An ordered, editable history of operations."""

    def __init__(self, name="UV Studio recipe", store=None):
        self.name = name
        self.steps = []
        self.store = store or SnapshotStore()
        self.created = time.time()

    # -- editing -----------------------------------------------------
    def add(self, step, snapshot=None):
        self.steps.append(step)
        if snapshot is not None:
            self.store.put(step.id, snapshot)
        return step

    def index_of(self, step_id):
        for i, step in enumerate(self.steps):
            if step.id == step_id:
                return i
        raise KeyError("No step %r in this recipe" % step_id)

    def get(self, step_id):
        return self.steps[self.index_of(step_id)]

    def remove(self, step_id):
        step = self.steps.pop(self.index_of(step_id))
        self.store.drop(step_id)
        return step

    def set_enabled(self, step_id, enabled):
        """Toggle a step. Disabling a barrier invalidates later snapshots.

        Everything after a topology change was recorded against UV indices that
        the change produced. Remove the change and those indices no longer mean
        the same thing, so the stored results after it are no longer valid.
        """
        step = self.get(step_id)
        step.enabled = bool(enabled)
        invalidated = []
        if step.is_barrier:
            position = self.index_of(step_id)
            for later in self.steps[position + 1:]:
                if self.store.drop(later.id):
                    invalidated.append(later.id)
        return invalidated

    def move(self, step_id, new_index):
        """Reorder a step. Refuses to cross a topology barrier.

        Returns the new index. Raises ReorderError rather than silently
        clamping, because a move that quietly does something else is worse than
        one that fails.
        """
        current = self.index_of(step_id)
        new_index = max(0, min(int(new_index), len(self.steps) - 1))
        if new_index == current:
            return current

        low, high = sorted((current, new_index))
        span = self.steps[low:high + 1]
        crossed = [s for s in span
                   if s.is_barrier and s.id != step_id]
        if crossed:
            raise ReorderError(
                "Cannot move %s across topology step(s) %s. A later step was "
                "recorded against UV indices those steps created."
                % (step_id, ", ".join(s.kind for s in crossed)))

        step = self.steps.pop(current)
        self.steps.insert(new_index, step)
        return new_index

    # -- inspection --------------------------------------------------
    @property
    def enabled_steps(self):
        return [s for s in self.steps if s.enabled]

    def degraded_steps(self):
        """SNAPSHOT steps whose stored result is gone.

        These can still be replayed, but by re-running a non-deterministic
        command, so the result may differ from what the artist approved.
        """
        return [s for s in self.steps
                if s.needs_snapshot and not self.store.has(s.id)]

    def barriers(self):
        return [s for s in self.steps if s.is_barrier]

    # -- replay ------------------------------------------------------
    def plan(self):
        """Produce the ordered action list to rebuild from the original UVs.

        Each action is one of:
          run     - execute the command with these parameters
          apply   - write these stored UV values directly
          skip    - disabled, recorded so the UI can show what was skipped

        Module 2 executes this; Module 4 never touches a scene.
        """
        actions = []
        for step in self.steps:
            if not step.enabled:
                actions.append(OrderedDict([
                    ("action", "skip"), ("step", step.id), ("kind", step.kind),
                    ("reason", "disabled")]))
                continue

            if step.needs_snapshot:
                snapshot = self.store.get(step.id)
                if snapshot is not None:
                    actions.append(OrderedDict([
                        ("action", "apply"), ("step", step.id),
                        ("kind", step.kind), ("mesh", snapshot.mesh),
                        ("uv_count", snapshot.count)]))
                    continue
                actions.append(OrderedDict([
                    ("action", "run"), ("step", step.id), ("kind", step.kind),
                    ("params", dict(step.params)), ("targets", list(step.targets)),
                    ("degraded", True),
                    ("reason", "snapshot evicted; result may differ")]))
                continue

            actions.append(OrderedDict([
                ("action", "run"), ("step", step.id), ("kind", step.kind),
                ("params", dict(step.params)), ("targets", list(step.targets)),
                ("degraded", False)]))
        return actions

    def summary(self):
        degraded = self.degraded_steps()
        return OrderedDict([
            ("name", self.name),
            ("steps", len(self.steps)),
            ("enabled", len(self.enabled_steps)),
            ("barriers", len(self.barriers())),
            ("snapshots", self.store.count),
            ("snapshot_bytes", self.store.bytes),
            ("evicted", len(self.store.evicted)),
            ("degraded", [s.id for s in degraded]),
        ])

    # -- persistence -------------------------------------------------
    def to_dict(self, include_snapshots=True):
        data = OrderedDict([
            ("module", MODULE_ID), ("version", __version__),
            ("name", self.name), ("created", self.created),
            ("steps", [s.to_dict() for s in self.steps]),
        ])
        if include_snapshots:
            data["snapshots"] = {
                s.id: self.store.get(s.id).to_dict()
                for s in self.steps if self.store.has(s.id)}
        return data

    def to_json(self, include_snapshots=True, indent=2):
        return json.dumps(self.to_dict(include_snapshots), indent=indent)

    @classmethod
    def from_dict(cls, data, store=None):
        recipe = cls(name=data.get("name", "UV Studio recipe"), store=store)
        recipe.created = data.get("created", time.time())
        for entry in data.get("steps", []):
            recipe.steps.append(Step.from_dict(entry))
        for step_id, snap in (data.get("snapshots") or {}).items():
            recipe.store.put(step_id, Snapshot.from_dict(snap))
        return recipe

    @classmethod
    def from_json(cls, text, store=None):
        return cls.from_dict(json.loads(text), store=store)


# =====================================================================
# SECTION 3b - The library on disk (D-06)
# =====================================================================

class RecipeLibrary(object):
    """Recipes on disk. This is the whole of D-06.

    `to_json()` has existed since M4 was written and nothing ever called it,
    so every recipe died with the Maya session that made it. A replayable
    history that cannot outlive the session is a log, not a feature.

    SNAPSHOTS ARE OFF BY DEFAULT. A recipe's steps are ~1 KB; its snapshots
    are the UV arrays they were taken from, which is two orders of magnitude
    larger. A folder of files that size is a folder nobody prunes, and
    snapshots only earn their weight for SNAPSHOT-fidelity steps whose
    command is non-deterministic. Saving them is a per-save choice, never a
    default that quietly fills someone's prefs directory.

    Writes land in a temporary file and are then moved into place. A recipe
    half-written because Maya was closed mid-save is worse than no recipe at
    all: it parses as valid JSON right up to the truncation.
    """

    SUFFIX = ".uvrecipe.json"

    def __init__(self, folder=None):
        self.folder = folder or self.default_folder()

    @staticmethod
    def default_folder():
        """Beside the prefs in Maya, under the home directory anywhere else.

        Importing maya here rather than at module scope keeps M4's promise
        that it has no Maya dependency: outside Maya this falls through to
        the home directory instead of failing to import.
        """
        try:
            import maya.cmds as cmds
            return os.path.join(cmds.internalVar(userAppDir=True),
                                "uvstudio", "recipes")
        except Exception:
            return os.path.join(os.path.expanduser("~"), ".uvstudio",
                                "recipes")

    # -- naming ----------------------------------------------------------
    @staticmethod
    def slug(name):
        """A file name from a recipe name, without surprises.

        Anything that is not a letter, digit, dash or underscore becomes an
        underscore, so a recipe called "car/body v2" cannot write outside the
        folder or collide with a path separator.
        """
        cleaned = "".join(c if (c.isalnum() or c in "-_") else "_"
                          for c in str(name).strip())
        return (cleaned.strip("_") or "recipe")[:64]

    def path_for(self, name):
        return os.path.join(self.folder, self.slug(name) + self.SUFFIX)

    def _resolve(self, name):
        path = str(name)
        if not path.endswith(self.SUFFIX):
            return self.path_for(name)
        return path if os.path.isabs(path) else os.path.join(self.folder,
                                                             path)

    # -- listing ----------------------------------------------------------
    def names(self):
        """What is saved, as the files themselves report it.

        The name INSIDE the file wins over the file name: a recipe saved as
        "car body" and later renamed on disk should still call itself what
        its author called it. An unreadable file is still listed, under its
        file name - a recipe that vanishes from the list because it is
        corrupt is a recipe the artist cannot find out is corrupt.
        """
        found = []
        try:
            entries = sorted(os.listdir(self.folder))
        except OSError:
            return found
        for entry in entries:
            if not entry.endswith(self.SUFFIX):
                continue
            path = os.path.join(self.folder, entry)
            record = OrderedDict([("name", entry[:-len(self.SUFFIX)]),
                                  ("file", entry), ("path", path),
                                  ("steps", None), ("readable", False),
                                  ("bytes", 0)])
            try:
                record["bytes"] = os.path.getsize(path)
                with open(path, "r") as handle:
                    data = json.load(handle)
                record["name"] = data.get("name") or record["name"]
                record["steps"] = len(data.get("steps") or [])
                record["readable"] = True
            except Exception:
                pass
            found.append(record)
        return found

    # -- persistence -------------------------------------------------------
    def save(self, recipe, name=None, include_snapshots=False):
        name = name or recipe.name
        path = self.path_for(name)
        payload = recipe.to_dict(include_snapshots=include_snapshots)
        payload["name"] = name
        temporary = path + ".writing"
        try:
            if not os.path.isdir(self.folder):
                os.makedirs(self.folder)
            with open(temporary, "w") as handle:
                json.dump(payload, handle, indent=2)
            if os.path.exists(path):
                os.remove(path)
            os.rename(temporary, path)
        except Exception:
            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except OSError:
                pass
            raise
        recipe.name = name
        return path

    def load(self, name, store=None):
        with open(self._resolve(name), "r") as handle:
            return Recipe.from_dict(json.load(handle), store=store)

    def delete(self, name):
        try:
            os.remove(self._resolve(name))
            return True
        except OSError:
            return False


# =====================================================================
# SECTION 4 - Construction helpers
# =====================================================================

def move_step(mesh, du, dv, unit=None):
    return Step("move", label="Move %s" % (unit or "selection"),
                params={"du": du, "dv": dv, "unit": unit},
                fidelity=EXACT, targets=[mesh])


def rotate_step(mesh, degrees, pivot, unit=None):
    return Step("rotate", label="Rotate %.1f deg" % degrees,
                params={"degrees": degrees, "pivot": list(pivot), "unit": unit},
                fidelity=EXACT, targets=[mesh])


def scale_step(mesh, su, sv, pivot, unit=None):
    return Step("scale", label="Scale %.3f x %.3f" % (su, sv),
                params={"su": su, "sv": sv, "pivot": list(pivot), "unit": unit},
                fidelity=EXACT, targets=[mesh])


def pack_step(mesh, udims, padding, sort, rotation):
    return Step("pack", label="Pack %d tile(s)" % len(udims),
                params={"udims": list(udims), "padding": padding,
                        "sort": sort, "allow_rotation": rotation},
                fidelity=EXACT, targets=[mesh])


def stack_step(mesh, families):
    return Step("stack", label="Stack %d family/families" % families,
                params={"families": families}, fidelity=EXACT, targets=[mesh])


def unfold_step(mesh, command, params=None):
    """Unfold/optimize/relax. Not reproducible, so it carries a snapshot."""
    return Step("unfold", label="Unfold (%s)" % command,
                params=dict(params or {}, command=command),
                fidelity=SNAPSHOT, targets=[mesh])


def cut_step(mesh, components):
    return Step("cut", label="Cut %d component(s)" % len(components),
                params={"components": list(components)},
                fidelity=TOPOLOGY, targets=[mesh])


def sew_step(mesh, components):
    return Step("sew", label="Sew %d component(s)" % len(components),
                params={"components": list(components)},
                fidelity=TOPOLOGY, targets=[mesh])


# =====================================================================
# SECTION 5 - Self test
# =====================================================================

def _check(label, got, want, failures):
    ok = got == want
    print("%-54s %s  (got %r)" % (label, "PASS" if ok else "FAIL", got))
    if not ok:
        failures.append(label)


def _check_true(label, condition, failures, detail=""):
    print("%-54s %s  %s" % (label, "PASS" if condition else "FAIL", detail))
    if not condition:
        failures.append(label)


def run_self_test():
    failures = []
    print("=" * 78)
    print("UV Studio M4 - Recipe self test (v%s)" % __version__)
    print("=" * 78)

    mesh = "|car|bodyShape"
    recipe = Recipe("test")
    a = recipe.add(move_step(mesh, 0.1, 0.0, unit="u0"))
    b = recipe.add(rotate_step(mesh, 90.0, (0.5, 0.5), unit="u0"))
    cut = recipe.add(cut_step(mesh, ["%s.e[1:20]" % mesh]))
    c = recipe.add(scale_step(mesh, 1.5, 1.5, (0.5, 0.5), unit="u1"))

    _check("four steps recorded", len(recipe.steps), 4, failures)
    _check("one barrier identified", len(recipe.barriers()), 1, failures)

    print("\n--- enable / disable ---")
    recipe.set_enabled(a.id, False)
    _check("disabled step drops out of enabled list",
           len(recipe.enabled_steps), 3, failures)
    plan = recipe.plan()
    _check("disabled step still appears in the plan as a skip",
           plan[0]["action"], "skip", failures)
    recipe.set_enabled(a.id, True)

    print("\n--- reorder ---")
    _check("move within a barrier-free span succeeds",
           recipe.move(b.id, 0), 0, failures)
    try:
        recipe.move(c.id, 0)
        _check_true("moving across a barrier is refused", False, failures)
    except ReorderError as exc:
        _check_true("moving across a barrier is refused", True, failures,
                    str(exc).split(".")[0][:46])
    _check("refused move left the order untouched",
           recipe.index_of(c.id), 3, failures)

    print("\n--- snapshots ---")
    snap = Snapshot(mesh, {i: (i * 0.001, i * 0.002) for i in range(5000)})
    unfold = recipe.add(unfold_step(mesh, "u3dUnfold"), snapshot=snap)
    _check("snapshot stored", recipe.store.has(unfold.id), True, failures)
    _check("snapshot is sparse, not whole-mesh", snap.count, 5000, failures)
    plan = recipe.plan()
    applied = [a for a in plan if a["action"] == "apply"]
    _check("snapshot step replays by applying, not re-running",
           len(applied), 1, failures)
    _check("  and no unfold command is re-run",
           [a for a in plan if a["kind"] == "unfold"
            and a["action"] == "run"], [], failures)

    print("\n--- eviction ---")
    small = SnapshotStore(budget_bytes=10000 * BYTES_PER_UV, protect_recent=2)
    tight = Recipe("evict", store=small)
    ids = []
    for i in range(8):
        step = tight.add(unfold_step(mesh, "u3dUnfold"),
                         snapshot=Snapshot(mesh, {j: (0.0, 0.0)
                                                  for j in range(3000)}))
        ids.append(step.id)
    _check_true("store stayed within budget",
                small.bytes <= small.budget_bytes, failures,
                "%d <= %d" % (small.bytes, small.budget_bytes))
    _check_true("oldest snapshots were evicted first",
                small.evicted and small.evicted[0] == ids[0], failures,
                "%d evicted" % len(small.evicted))
    _check_true("the two most recent are protected",
                small.has(ids[-1]) and small.has(ids[-2]), failures)
    degraded = tight.degraded_steps()
    _check_true("evicted steps are reported as degraded, not lost",
                len(degraded) == len(small.evicted) and len(tight.steps) == 8,
                failures, "%d degraded of 8 steps" % len(degraded))
    plan = tight.plan()
    rerun = [a for a in plan if a.get("degraded")]
    _check_true("degraded steps fall back to re-running, flagged",
                len(rerun) == len(degraded), failures)

    print("\n--- barrier invalidation ---")
    inv = Recipe("barrier")
    inv.add(move_step(mesh, 0.1, 0.0))
    bar = inv.add(cut_step(mesh, ["%s.e[0]" % mesh]))
    after = inv.add(unfold_step(mesh, "u3dUnfold"),
                    snapshot=Snapshot(mesh, {1: (0.0, 0.0)}))
    _check("snapshot present before the barrier is disabled",
           inv.store.has(after.id), True, failures)
    invalidated = inv.set_enabled(bar.id, False)
    _check("disabling a barrier invalidates later snapshots",
           invalidated, [after.id], failures)
    _check("  the step survives, only its stored result goes",
           len(inv.steps), 3, failures)

    print("\n--- persistence ---")
    text = recipe.to_json()
    restored = Recipe.from_json(text)
    _check("step count survives a JSON round trip",
           len(restored.steps), len(recipe.steps), failures)
    _check("step ids survive",
           [s.id for s in restored.steps], [s.id for s in recipe.steps], failures)
    _check("fidelities survive",
           [s.fidelity for s in restored.steps],
           [s.fidelity for s in recipe.steps], failures)
    original_snap = recipe.store.get(unfold.id)
    restored_snap = restored.store.get(unfold.id)
    _check_true("snapshot values survive, with int keys",
                restored_snap is not None
                and restored_snap.count == original_snap.count
                and restored_snap.uvs[42] == original_snap.uvs[42],
                failures)
    lean = json.loads(recipe.to_json(include_snapshots=False))
    _check_true("snapshots can be excluded for a small recipe file",
                "snapshots" not in lean, failures,
                "%d vs %d chars" % (len(recipe.to_json(False)), len(text)))

    print("\n--- guards ---")
    try:
        Step("bogus", fidelity="wishful")
        _check_true("unknown fidelity rejected", False, failures)
    except ValueError:
        _check_true("unknown fidelity rejected", True, failures)
    try:
        recipe.index_of("nosuchstep")
        _check_true("unknown step id rejected", False, failures)
    except KeyError:
        _check_true("unknown step id rejected", True, failures)

    print("\n--- scale ---")
    big = Recipe("big", store=SnapshotStore(budget_bytes=32 * 1024 * 1024))
    started = time.time()
    for i in range(2000):
        big.add(move_step(mesh, 0.001 * i, 0.0))
    for i in range(0, 2000, 200):
        big.add(unfold_step(mesh, "u3dUnfold"),
                snapshot=Snapshot(mesh, {j: (0.0, 0.0) for j in range(4000)}))
    build = time.time() - started
    started = time.time()
    actions = big.plan()
    planning = time.time() - started
    _check("2010 steps planned", len(actions), 2010, failures)
    _check_true("planning is fast", planning < 0.5, failures,
                "build %.3fs, plan %.3fs" % (build, planning))
    print("  snapshot store: %d snapshots, %.1f MB, %d evicted"
          % (big.store.count, big.store.bytes / 1048576.0,
             len(big.store.evicted)))

    print("")
    print("=" * 78)
    print("%d failure(s)" % len(failures))
    for name in failures:
        print("  FAILED: %s" % name)
    print("=" * 78)
    return not failures


if __name__ == "__main__":
    run_self_test()
