"""Check the optional referee correction against the published cube-size rule."""
import copy
from types import SimpleNamespace
import unittest

from tools.cube_size_arena import with_cube_size
from tools.supply_arena import with_supply_drops


# Dimensions and the two separate cube rules from the official kit hello.
CONFIG = {
    "mech": {"radius": 2.5, "height": 9.0, "eye_height": 8.0,
             "muzzle_height": 7.0, "hp": 1400, "energy": 100},
    "cubes": {"size_per_cube": .05, "stat_cap": 10,
              "hp_per_cube": .04, "upgrade_thresholds": [1, 3, 6, 10, 15, 21]},
}


class CollectingEpisode:
    def collect(self, actor, action):
        self.last_collect = (actor, action)
        actor.cubes += action.get("collected", 0)
        return "collected"


def mech(ident=0):
    stats = dict(CONFIG["mech"])
    return SimpleNamespace(id=ident, kind="mech", radius=stats["radius"],
                           height=stats["height"], stats=stats, cubes=0,
                           pos=[12.0, -8.0, 3.0], hp=500, max_hp=1400,
                           energy=25, events=[], offer=None)


class CubeSizeTest(unittest.TestCase):
    def setUp(self):
        self.ep = with_cube_size(CollectingEpisode)()
        self.ep.cfg = copy.deepcopy(CONFIG)

    def assert_size(self, actor, cubes, cfg=None):
        cfg = cfg or self.ep.cfg
        factor = 1 + cfg["cubes"]["size_per_cube"] * cubes
        for field in ("radius", "height", "eye_height", "muzzle_height"):
            self.assertAlmostEqual(actor.stats[field], cfg["mech"][field] * factor)
        self.assertEqual(actor.radius, actor.stats["radius"])
        self.assertEqual(actor.height, actor.stats["height"])

    def test_collect_updates_geometry_and_public_stats_for_every_mech(self):
        for ident in (0, 15):
            actor = mech(ident)
            original_pos = actor.pos.copy()
            action = {"collected": 1}
            self.assertEqual(self.ep.collect(actor, action), "collected")
            self.assertEqual(self.ep.last_collect, (actor, action))
            self.assert_size(actor, 1)
            self.assertEqual(actor.pos, original_pos)
            self.assertEqual((actor.hp, actor.energy, actor.stats["hp"]), (500, 25, 1400))

    def test_size_has_no_stat_cap_and_does_not_compound(self):
        actor = mech()
        self.ep.collect(actor, {})
        self.assert_size(actor, 0)
        self.ep.collect(actor, {"collected": 10})
        self.assert_size(actor, 10)
        self.ep.collect(actor, {"collected": 11})
        self.assert_size(actor, 21)
        self.assertAlmostEqual(actor.radius, 5.125)
        before = copy.deepcopy(vars(actor))
        self.ep.collect(actor, {})
        self.assertEqual(vars(actor), before)

    def test_uses_config_instead_of_hardcoded_balance(self):
        self.ep.cfg["cubes"]["size_per_cube"] = .1
        self.ep.cfg["mech"].update(radius=3, height=12, eye_height=10, muzzle_height=9)
        actor = mech()
        self.ep.collect(actor, {"collected": 2})
        self.assert_size(actor, 2)

    def test_npc_dimensions_are_not_scaled(self):
        actor = mech(100)
        actor.kind = "npc"
        before = (actor.radius, actor.height, actor.stats.copy())
        self.ep.collect(actor, {"collected": 3})
        self.assertEqual((actor.radius, actor.height, actor.stats), before)

    def test_real_supply_extension_award_keeps_reward_and_scales_above_cap(self):
        episode_type = with_cube_size(with_supply_drops(CollectingEpisode))
        ep = episode_type.__new__(episode_type)
        ep.cfg = copy.deepcopy(CONFIG)
        ep.drop_rules = {"cubes": 3}
        ep.now = 99.0
        ep.taken_by_player = ep.taken_by_rivals = 0
        ep.player = mech(0)
        rival = mech(15)
        for actor in (ep.player, rival):
            # Exercise ordinary collection followed by the actual supply
            # extension's reward, crossing the HP/damage cap in one event.
            ep.collect(actor, {"collected": 9})
            actor.max_hp = 1400 * (1 + 9 * .04)
            ep.award(actor, 20000)
            self.assertEqual(actor.cubes, 12)
            self.assert_size(actor, 12, ep.cfg)
            self.assertAlmostEqual(actor.hp, 1960)
            self.assertAlmostEqual(actor.max_hp, 1960)
            self.assertEqual(actor.energy, 100)
            self.assertEqual(actor.events[-1], {"type": "drop", "id": 20000, "cubes": 12})
        self.assertEqual((ep.taken_by_player, ep.taken_by_rivals), (1, 1))


if __name__ == "__main__":
    unittest.main()
