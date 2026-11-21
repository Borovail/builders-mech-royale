import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from placement_audit import with_placement_audit


class ScriptedEpisode:
    def __init__(self, events, score):
        self.events = events
        self.score = score
        self.dead = {}
        self.actors = {i: SimpleNamespace(id=i, hp=100, kind="mech") for i in range(16)}
        self.player = self.actors[0]
        self.opponents = range(1, 16)
        self.now, self.duration = 0, 10

    def hit(self, src, dst, kind, damage):
        if dst.hp > 0:
            dst.hp -= damage
            if dst.hp <= 0:
                self.dead[dst.id] = self.now

    def run(self, bot, trace):
        for self.now, target, kind, source, amount in self.events:
            self.hit(self.actors.get(source), self.actors[target], kind, amount)
        self.now = self.duration
        return {"score": self.score, "place_proxy": 16 - self.score}


class PlacementAuditTest(unittest.TestCase):
    def test_tied_death_awards_one_point_and_keeps_fatal_cause(self):
        events = [(2, 1, "zone", None, 100), (2, 0, "zone", None, 100)]
        result = with_placement_audit(ScriptedEpisode)(events, 1).run(None, None)
        self.assertEqual(result["audit_points_same_tick"], 1)
        self.assertEqual(result["audit_points_zone"], 0)
        self.assertEqual(result["audit_rival_deaths_zone"], 1)
        self.assertEqual(result["audit_end"], "dead")

    def test_timeout_distinguishes_lower_hp_from_equal_hp(self):
        events = [(1, 1, "gun", 0, 100), (1, 2, "zone", None, 25)]
        result = with_placement_audit(ScriptedEpisode)(events, 15).run(None, None)
        self.assertEqual(result["audit_points_player"], 1)
        self.assertEqual(result["audit_points_hp"], 1)
        self.assertEqual(result["audit_points_hp_tie"], 13)
        self.assertEqual(result["audit_end"], "timeout")
