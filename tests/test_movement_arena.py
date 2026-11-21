"""Optional wall filter: exact tiny worlds and the pinned referee's real move.

Set MECH_KIT and MECH_ENGINE to enable the two recorded campus regressions.
The external engine is imported in an isolated subprocess, leaving the bot's
navigation module and the rest of the test process untouched.
"""
import copy
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from tools.movement_arena import MovementGeometry, with_movement_geometry
from tools.spawn_arena import SpawnGeometry


WORLD = {
    "bounds": {"minX": -100, "minY": -100, "maxX": 100, "maxY": 100},
    "prisms": [
        {"base": 0, "top": 12, "ring": [0, 0, 10, 0, 10, 10, 0, 10],
         "holes": [[3, 3, 7, 3, 7, 7, 3, 7]]},
        {"base": 4, "top": 8, "ring": [20, 0, 30, 0, 30, 10, 20, 10], "holes": []},
    ],
}


class OpenLayout:
    """Model the permissive raster query that caused the recorded crossings."""
    def __init__(self, directory):
        self.directory = directory
        self.walk_calls = []

    def walk(self, *point):
        self.walk_calls.append(point)
        return True


class AxisEpisode:
    """Only the pinned move's axis contract, with no wall-response reimplementation."""
    def __init__(self, hello, layout, seed):
        self.layout = layout

    def move(self, actor, action):
        for i in (0, 1):
            p = actor.pos.copy()
            p[i] += actor.vel[i]
            if self.layout.walk(*p, actor.radius):
                actor.pos[i] = p[i]
            else:
                actor.vel[i] = 0
        actor.energy -= 3 if action.get("boost") else 0

    def run(self, bot, trace):
        return {"score": 7}


def actor(pos, velocity, **changes):
    fields = dict(kind="mech", pos=list(pos), vel=list(velocity), radius=1, height=2,
                  grounded=True, energy=100, stats={"step_up": 1.2})
    fields.update(changes)
    return SimpleNamespace(**fields)


class MovementGeometryTest(unittest.TestCase):
    def setUp(self):
        self.geometry = MovementGeometry(WORLD)

    def test_exact_stationary_check_matches_existing_spawn_geometry(self):
        baseline = SpawnGeometry(WORLD)
        for x in [-99.5, -99, -2, -1, -.5, 1, 3.5, 4, 5, 9, 11, 25, 99, 99.5]:
            for y in [-2, -1, 0, 1, 4, 5, 7, 9, 10, 11]:
                for z in [0, 2, 3, 4, 7, 8, 12]:
                    with self.subTest(point=(x, y, z)):
                        self.assertEqual(self.geometry.legal((x, y, z), 1, 2),
                                         baseline.legal((x, y, z), 1, 2))

    def test_tangent_away_and_into_wall(self):
        g = self.geometry
        self.assertTrue(g.can_move((-1, 4, 0), (-1, 6, 0), 1, 2, 1.2))
        self.assertTrue(g.can_move((-1, 4, 0), (-2, 4, 0), 1, 2, 1.2))
        self.assertFalse(g.can_move((-1, 4, 0), (-.9, 4, 0), 1, 2, 1.2))
        self.assertTrue(g.can_move((4, 4, 0), (4, 6, 0), 1, 2, 1.2))
        self.assertFalse(g.can_move((4, 4, 0), (3.9, 4, 0), 1, 2, 1.2))

    def test_sweep_cannot_tunnel_through_wall_with_legal_endpoints(self):
        g = self.geometry
        self.assertTrue(g.legal((-2, 1, 0), 1, 2))
        self.assertTrue(g.legal((12, 1, 0), 1, 2))
        self.assertFalse(g.can_move((-2, 1, 0), (12, 1, 0), 1, 2, 1.2))

    def test_under_bridge_roof_arrival_and_departure(self):
        g = self.geometry
        self.assertTrue(g.can_move((18, 5, 0), (25, 5, 0), 1, 4, 1.2))
        self.assertFalse(g.can_move((18, 5, 0), (25, 5, 0), 1, 4.1, 1.2))
        self.assertTrue(g.can_move((18, 5, 8), (25, 5, 8), 1, 2, 1.2))
        self.assertTrue(g.can_move((25, 5, 8), (32, 5, 8), 1, 2, 1.2))

    def test_low_steps_need_headroom_and_cannot_exceed_step_up(self):
        world = copy.deepcopy(WORLD)
        world["prisms"] = [{"base": 0, "top": 1.2, "ring": [0, 0, 10, 0, 10, 10, 0, 10]}]
        g = MovementGeometry(world)
        self.assertTrue(g.can_move((-2, 5, 0), (2, 5, 0), 1, 2, 1.2))
        self.assertFalse(g.can_move((-2, 5, 0), (2, 5, 0), 1, 2, 1.1))
        world["prisms"].append({"base": 3, "top": 5, "ring": [0, 0, 10, 0, 10, 10, 0, 10]})
        self.assertFalse(MovementGeometry(world).can_move((-2, 5, 0), (2, 5, 0), 1, 2, 1.2))

    def test_map_edge_is_a_wall(self):
        self.assertTrue(self.geometry.can_move((98, 30, 0), (99, 30, 0), 1, 2, 1.2))
        self.assertFalse(self.geometry.can_move((98, 30, 0), (99.1, 30, 0), 1, 2, 1.2))

    def test_broad_phase_does_not_iterate_all_prisms_during_queries(self):
        world = copy.deepcopy(WORLD)
        world["prisms"] += [{"base": 0, "top": 20,
                             "ring": [200+i*20, 0, 210+i*20, 0, 210+i*20, 10, 200+i*20, 10]}
                            for i in range(1000)]
        g = MovementGeometry(world)
        self.assertEqual(len(g.candidates((-2, 4, 0), (-1, 4, 0), 1)), 1)
        class IndexedOnly(list):
            def __iter__(self):
                raise AssertionError("A query scanned every prism")
        g.prisms = IndexedOnly(g.prisms)
        self.assertTrue(g.can_move((-1, 4, 0), (-2, 4, 0), 1, 2, 1.2))
        self.assertTrue(g.legal((-1, 4, 0), 1, 2))


class MovementEpisodeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="mech-move-test-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "v2").mkdir()
        (root / "v2/world.json").write_text(json.dumps(WORLD), encoding="utf-8")
        self.layout = OpenLayout(root)
        self.episode_type = with_movement_geometry(AxisEpisode)

    def test_axis_slide_keeps_tangent_move_energy_and_shared_layout(self):
        ep = self.episode_type({}, self.layout, 1)
        a = actor((-1, 4, 0), (.2, .4))
        ep.move(a, {"boost": True})
        self.assertEqual(a.pos, [-1, 4.4, 0])
        self.assertEqual(a.vel, [0, .4])
        self.assertEqual(a.energy, 97)
        self.assertIs(ep.layout, self.layout)
        self.assertEqual(ep.run(None, None), {"score": 7, "movement_checks": 2,
                                             "movement_rejections": 1})

    def test_open_ground_is_identical_to_base_move(self):
        original = actor((-20, -20, 0), (.2, .4))
        filtered = copy.deepcopy(original)
        AxisEpisode({}, self.layout, 1).move(original, {"boost": True})
        self.episode_type({}, self.layout, 1).move(filtered, {"boost": True})
        self.assertEqual(vars(original), vars(filtered))

    def test_air_jump_and_npc_use_original_movement(self):
        for changes, action in [({"grounded": False}, {}), ({}, {"jump": True}),
                                ({"kind": "npc"}, {})]:
            ep = self.episode_type({}, self.layout, 1)
            a = actor((-1, 4, 0), (.2, .4), **changes)
            ep.move(a, action)
            self.assertEqual(a.pos, [-.8, 4.4, 0])
            self.assertEqual(ep.movement_checks, 0)

    def test_proxy_is_local_to_each_episode_and_never_filters_policy_queries(self):
        first = self.episode_type({}, self.layout, 1)
        second = self.episode_type({}, self.layout, 2)
        self.assertIsNot(first._movement_layout, second._movement_layout)
        self.assertTrue(first.layout.walk(-.5, 4, 0, 1))
        first.move(actor((-1, 4, 0), (.2, .4)), {})
        self.assertEqual(second.movement_checks, 0)
        self.assertTrue(second.layout.walk(-.5, 4, 0, 1))

    def test_layout_is_restored_if_the_original_move_raises(self):
        class BrokenEpisode(AxisEpisode):
            def move(self, actor, action):
                raise ValueError("test failure")
        ep = with_movement_geometry(BrokenEpisode)({}, self.layout, 1)
        with self.assertRaisesRegex(ValueError, "test failure"):
            ep.move(actor((-1, 4, 0), (.2, .4)), {})
        self.assertIs(ep.layout, self.layout)
        self.assertIsNone(ep._movement_layout.actor)

    def test_original_walk_rejection_still_wins(self):
        self.layout.walk = lambda *args: False
        ep = self.episode_type({}, self.layout, 1)
        a = actor((-20, -20, 0), (.2, .4))
        ep.move(a, {})
        self.assertEqual(a.pos, [-20, -20, 0])
        self.assertEqual(ep.movement_checks, 0)


@unittest.skipUnless(os.environ.get("MECH_KIT") and os.environ.get("MECH_ENGINE"),
                     "Set MECH_KIT and MECH_ENGINE for real pinned-engine regressions")
class PinnedRecordedStepsTest(unittest.TestCase):
    def test_indexed_published_geometry_matches_the_slow_exact_oracle(self):
        kit = Path(os.environ["MECH_KIT"])
        world = json.loads((kit / "arena/v2/world.json").read_text(encoding="utf-8"))
        indexed, oracle = MovementGeometry(world), SpawnGeometry(world)
        rng = random.Random(8801)
        points = []
        # Include points near each real contour, rather than mostly empty map.
        for prism in world["prisms"]:
            for flat in [prism["ring"], *prism.get("holes", [])]:
                ring = list(zip(flat[::2], flat[1::2]))
                for a, b in list(zip(ring, ring[1:] + ring[:1]))[::max(1, len(ring) // 4)]:
                    points.append(((a[0]+b[0])*.5+rng.uniform(-3, 3),
                                   (a[1]+b[1])*.5+rng.uniform(-3, 3),
                                   rng.choice([prism["base"], prism["top"]-1, prism["top"]])))
        for p in points:
            self.assertEqual(indexed.legal(p, 2.5, 9), oracle.legal(p, 2.5, 9), p)

    def test_both_recorded_transitions_use_the_real_move_and_stop_outside(self):
        # The same engine hashes used by tools/local_matches.py guard this test.
        script = r'''
import copy, csv, gzip, json, math, sys
from pathlib import Path
from tools.local_matches import require_engine
from tools.movement_arena import with_movement_geometry
engine, kit, root = map(Path, sys.argv[1:])
require_engine(engine)
sys.path.insert(0, str(engine))
from training.arena import Actor, Episode
from training.squad16 import Campus
with gzip.open(kit / "check/stream.jsonl.gz", "rt") as stream:
    hello = json.loads(next(stream))
layout = Campus(kit / "arena", hello["config"])
class MinimalEpisode(Episode):
    def __init__(self, hello, layout, seed):
        self.layout = layout
        self.cfg = hello["config"]
        self.dt = 1 / self.cfg["match"]["tick_hz"]
        self.now = 0
        self.zone = {"center": [0, 0], "radius": 10000, "dps": 0}
CorrectedEpisode = with_movement_geometry(MinimalEpisode)
for row in csv.DictReader((root / "results/referee-collision-steps.csv").open()):
    start = [float(row["from_"+axis]) for axis in "xyz"]
    end = [float(row["to_"+axis]) for axis in "xyz"]
    before = Actor(int(row["mech_id"]), "mech", start, hello["config"], 1)
    desired = [(end[i]-start[i]) / (1 / hello["config"]["match"]["tick_hz"]) for i in (0,1)]
    before.vel[:2] = desired
    action = {"move": [v / before.stats["walk_speed"] for v in desired]}
    original, corrected = copy.deepcopy(before), copy.deepcopy(before)
    base = MinimalEpisode(hello, layout, 1)
    ep = CorrectedEpisode(hello, layout, 1)
    assert ep.movement_geometry.legal(start, before.radius, before.height), row
    base.move(original, action)
    ep.move(corrected, action)
    assert all(abs(a-b)<1e-10 for a,b in zip(original.pos,end)), (row, original.pos)
    assert not ep.movement_geometry.legal(original.pos, before.radius, before.height), row
    assert ep.movement_geometry.legal(corrected.pos, before.radius, before.height), (row,corrected.pos)
    assert ep.movement_rejections > 0, row
    assert corrected.energy == original.energy, row
    assert math.dist(corrected.pos,start) <= math.dist(original.pos,start) + 1e-10, row
    # An outward command must move the corrected mech, rather than freeze it.
    away = [start[i]-end[i] for i in (0,1)]
    norm = math.hypot(*away)
    corrected.vel[:2] = [0,0]
    old = corrected.pos.copy()
    for _ in range(10):
        ep.move(corrected, {"move": [v/norm for v in away]})
        assert ep.movement_geometry.legal(corrected.pos, before.radius, before.height), (row,corrected.pos)
    assert math.dist(corrected.pos,old) > .1, row
    print(json.dumps({"seed":row["seed"],"original":original.pos,"corrected_first_step":old,
                      "after_away":corrected.pos,"checks":ep.movement_checks,"rejections":ep.movement_rejections}))
'''
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, "-B", "-c", script, os.environ["MECH_ENGINE"],
                                 os.environ["MECH_KIT"], str(root)], cwd=root,
                                text=True, capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([row["seed"] for row in rows], ["8111", "8134"])


if __name__ == "__main__":
    unittest.main()
