"""Regression for the optional offline referee's 20 Hz ring and reward."""
from types import SimpleNamespace
import unittest

from tools.supply_arena import with_supply_drops


class Clock:
    def step_shots(self):
        pass


def mech(ident):
    return SimpleNamespace(id=ident, kind="mech", hp=500, max_hp=1400,
                           energy=25, stats={"energy": 100}, cubes=0,
                           pos=[0, 0, 0], events=[], offer=None)


class RingTest(unittest.TestCase):
    def setUp(self):
        episode_type = with_supply_drops(Clock)
        self.ep = episode_type.__new__(episode_type)
        self.pod = {"lands_at": 95, "holder": None, "progress": 0, "pos": [0, 0, 0]}
        self.ep.pods = {20000: self.pod}
        self.ep.drop_rules = {"radius": 8, "capture_s": 4, "cubes": 3}
        self.ep.cfg = {"mech": {"hp": 1400},
                       "cubes": {"stat_cap": 10, "hp_per_cube": .04, "upgrade_thresholds": [1, 3]}}
        self.ep.player, self.rival = mech(0), mech(1)
        self.rival.pos = [9, 0, 0]
        self.ep.actors = {0: self.ep.player, 1: self.rival}
        self.ep.now, self.ep.dt = 94.5, .05
        self.ep.taken_by_player = self.ep.taken_by_rivals = 0

    def ticks(self, n):
        for _ in range(n):
            self.ep.step_shots()
            self.ep.now = round(self.ep.now + self.ep.dt, 2)

    def test_single_holder_contest_and_reward(self):
        self.ticks(10)
        self.assertEqual(self.pod["progress"], 0)
        self.ticks(30)
        self.rival.pos[0] = 8
        self.ticks(10)
        self.assertAlmostEqual(self.pod["progress"], 1.5)
        self.rival.pos[0] = 9
        self.ticks(49)
        self.assertEqual(self.ep.player.cubes, 0)
        self.ticks(1)
        self.assertEqual(self.ep.pods, {})
        self.assertEqual((self.ep.player.hp, self.ep.player.energy, self.ep.player.cubes), (1568, 100, 3))
        self.assertEqual(self.ep.taken_by_player, 1)

    def test_empty_and_change_of_holder_restart_clock(self):
        self.ticks(40)
        self.ep.player.pos[0] = 9
        self.ticks(1)
        self.assertIsNone(self.pod["holder"])
        self.ep.player.pos[0] = 0
        self.ticks(65)
        self.ep.player.pos[0], self.rival.pos[0] = 9, 0
        self.ticks(1)
        self.assertAlmostEqual(self.pod["progress"], .05)
        self.ticks(79)
        self.assertEqual(self.ep.taken_by_rivals, 1)
        self.assertEqual(self.ep.player.cubes, 0)
        self.assertEqual(self.rival.cubes, 3)
