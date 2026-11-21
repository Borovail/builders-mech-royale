"""Independent tiny-world checks for the optional initial mech-spawn filter."""
import copy
import json
import math
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from tools.spawn_arena import SpawnGeometry, with_legal_spawns


WORLD = {
    "bounds": {"minX": -100, "minY": -100, "maxX": 100, "maxY": 100},
    "prisms": [
        {"base": 0, "top": 12, "ring": [0, 0, 10, 0, 10, 10, 0, 10],
         "holes": [[3, 3, 7, 3, 7, 7, 3, 7]]},
        {"base": 4, "top": 8, "ring": [20, 0, 30, 0, 30, 10, 20, 10], "holes": []},
    ],
}
CLEAR_POINTS = [[-30 + 6 * column, -30 + 6 * row, 0]
                for row in range(4) for column in range(4)]
LATER_NPC = [1, 1, 0]  # Inside a wall: must remain unchanged after mechs exist.
LATER_ROOF = [1, 1, 12]


class ScriptedEpisode:
    """Keep the pinned constructor's sampling/separation lifecycle, no RNG."""
    def __init__(self, hello, layout, seed):
        self.cfg = hello["config"]
        self.layout = layout
        self.actors = {}
        self.draws = iter(copy.deepcopy(hello["points"]))
        self.calls = []
        self.separation_rejections = 0
        points = []
        while len(points) < 16:
            point = self.open_point(80)
            if all(math.dist(point[:2], other[:2]) >= 4 for other in points):
                points.append(point)
            else:
                self.separation_rejections += 1
        self.mech_points = points
        self.actors = {index: point for index, point in enumerate(points)}
        self.npc_point = self.open_point(90)
        self.roof_point = self.open_point(95, roof=True)

    def open_point(self, radius, roof=False):
        self.calls.append((radius, roof))
        return next(self.draws)

    def run(self, bot, trace):
        return {"score": 3, "unchanged": True}


class SpawnFilterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mech-spawn-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "v2").mkdir()
        (self.root / "v2/world.json").write_text(json.dumps(WORLD), encoding="utf-8")
        self.layout = SimpleNamespace(directory=self.root)
        self.episode_type = with_legal_spawns(ScriptedEpisode)

    def hello(self, points):
        return {"config": {"mech": {"radius": 1, "height": 2}},
                "points": points + [LATER_NPC, LATER_ROOF]}

    def test_rejects_inside_near_wall_hole_edge_and_bounds_then_retries(self):
        invalid = [[1, 1, 0], [-.5, 5, 0], [3.5, 5, 0], [-99.5, 0, 0]]
        legal = [[5, 5, 0], *CLEAR_POINTS[:15]]
        ep = self.episode_type(self.hello(invalid + legal), self.layout, 11)
        self.assertEqual(ep.mech_points, legal)
        self.assertEqual(ep.legal_spawn_checks, 20)
        self.assertEqual(ep.legal_spawn_rejections, 4)
        self.assertEqual(ep.npc_point, LATER_NPC)
        self.assertEqual(ep.roof_point, LATER_ROOF)
        result = ep.run(None, None)
        self.assertEqual(result, {"score": 3, "unchanged": True,
                                  "legal_spawn_checks": 20, "legal_spawn_rejections": 4})

    def test_original_sampler_still_enforces_mech_separation(self):
        points = [CLEAR_POINTS[0], [-28, -30, 0], *CLEAR_POINTS[1:]]
        ep = self.episode_type(self.hello(points), self.layout, 12)
        self.assertEqual(ep.mech_points, CLEAR_POINTS)
        self.assertEqual(ep.separation_rejections, 1)
        self.assertEqual(ep.legal_spawn_checks, 17)
        self.assertEqual(ep.legal_spawn_rejections, 0)
        self.assertTrue(all(math.dist(a[:2], b[:2]) >= 4
                            for i, a in enumerate(ep.mech_points) for b in ep.mech_points[i + 1:]))

    def test_all_legal_sequence_and_later_sampling_are_unchanged(self):
        hello = self.hello(CLEAR_POINTS)
        original = ScriptedEpisode(hello, self.layout, 13)
        filtered = self.episode_type(hello, self.layout, 13)
        repeated = self.episode_type(hello, self.layout, 13)
        for ep in (filtered, repeated):
            self.assertEqual(ep.mech_points, original.mech_points)
            self.assertEqual(ep.npc_point, original.npc_point)
            self.assertEqual(ep.roof_point, original.roof_point)
            self.assertEqual(ep.calls, original.calls)
            self.assertEqual(ep.legal_spawn_checks, 16)
            self.assertEqual(ep.legal_spawn_rejections, 0)

    def test_holes_tangency_and_actual_vertical_interval(self):
        geometry = SpawnGeometry(WORLD)
        self.assertTrue(geometry.legal([5, 5, 0], 1, 2))
        self.assertFalse(geometry.legal([3.5, 5, 0], 1, 2))
        self.assertTrue(geometry.legal([4, 5, 0], 1, 2))  # Tangent inside hole.
        self.assertTrue(geometry.legal([-1, 5, 0], 1, 2))  # Tangent outside wall.
        self.assertTrue(geometry.legal([25, 5, 0], 1, 2))  # Under elevated prism.
        self.assertTrue(geometry.legal([25, 5, 0], 1, 4))  # Head touches underside.
        self.assertFalse(geometry.legal([25, 5, 0], 1, 4.1))
        self.assertFalse(geometry.legal([25, 5, 3], 1, 2))
        self.assertTrue(geometry.legal([25, 5, 8], 1, 2))  # Feet on top.
        self.assertFalse(geometry.legal([99.5, 0, 0], 1, 2))
        self.assertTrue(geometry.legal([99, 0, 0], 1, 2))

    def test_roof_sampling_during_initial_phase_is_not_filtered(self):
        class EarlyRoofEpisode:
            def __init__(self, hello, layout, seed):
                self.cfg, self.actors = hello["config"], {}
                self.point = self.open_point(80, roof=True)

            def open_point(self, radius, roof=False):
                return LATER_NPC.copy()

        ep = with_legal_spawns(EarlyRoofEpisode)(self.hello([]), self.layout, 1)
        self.assertEqual(ep.point, LATER_NPC)
        self.assertEqual((ep.legal_spawn_checks, ep.legal_spawn_rejections), (0, 0))

    def test_no_legal_candidate_fails_after_bounded_attempts(self):
        class NoLegalEpisode:
            calls = 0

            def __init__(self, hello, layout, seed):
                self.cfg, self.actors = hello["config"], {}
                self.open_point(80)

            def open_point(self, radius, roof=False):
                NoLegalEpisode.calls += 1
                return [1, 1, 0]

        with self.assertRaisesRegex(ValueError, "No exact legal ground spawn"):
            with_legal_spawns(NoLegalEpisode)(self.hello([]), self.layout, 1)
        self.assertEqual(NoLegalEpisode.calls, 2000)


if __name__ == "__main__":
    unittest.main()
