"""Focused slot policy tests and a timed, game-rule-only laser comparison.

Run with python -m unittest discover -s tests -p test_secondary.py -v.
The simulation counts opportunities, not hits, damage, or match scores.
"""
import copy
import math
import unittest

from secondary import Secondary


# Defaults transcribed from the official kit hello/config and protocol §7.
HELLO = {"config": {"mech": {"radius": 2.5, "walk_speed": 11.7,
                              "charge_move_mult": .5}, "weapons": {
    "laser": {"range": 280, "charge": .75, "min_charge": .15,
              "hold_s": 2, "cooldown": 1.65},
    "lightning": {"range": 45, "cone_deg": 30, "energy": 3},
    "mortar": {"speed": 72, "ttl": 7, "splash_radius": 7},
    "gun": {"range": 230},
}}}


def observation(kind="laser", **weapon_values):
    weapon = {"kind": kind, "cooldown": 0, "charge": 0, "stats": {}}
    weapon.update(weapon_values)
    return {"tick": 0, "t": 0, "self": {"id": 3, "pos": [0, 0, 0],
            "aim": [0, 0], "energy": 100, "stats": {},
            "weapons": [None, weapon]}}


def enemy(distance=30, yaw=0):
    return {"id": 4, "kind": "mech", "hp": 1400,
            "pos": [distance * math.cos(yaw), distance * math.sin(yaw), 0]}


def simulate_laser(policy, seconds=20):
    """Advance 0.1 s decisions with two official 0.05 s game ticks each.

    The target remains available at a fixed in-range position. The game
    holds a full charge for hold_s before auto-release; release below
    min_charge completes that minimum first. Speed cost is walk distance
    lost solely to charge_move_mult, without movement/collision physics.
    """
    obs = observation()
    weapon = obs["self"]["weapons"][1]
    stats = HELLO["config"]["weapons"]["laser"]
    mech = HELLO["config"]["mech"]
    charging = False
    full_hold = 0
    result = {"full_shots": 0, "release_shots": 0, "auto_shots": 0,
              "charge_s": 0.0, "full_hold_s": 0.0, "lost_walk_m": 0.0}
    sequence = []
    for decision in range(round(seconds / .1)):
        obs["tick"], obs["t"] = decision * 2, decision * .1
        held = policy(obs, enemy())
        sequence.append(held)
        for _ in range(2):
            if weapon["cooldown"] > 1e-9:
                weapon["cooldown"] = max(0, weapon["cooldown"] - .05)
                continue
            if held:
                charging = True
            if not charging:
                continue
            was_full = weapon["charge"] >= stats["charge"] - 1e-9
            result["charge_s"] += .05
            if was_full:
                full_hold += .05
                result["full_hold_s"] += .05
            else:
                weapon["charge"] = min(stats["charge"], weapon["charge"] + .05)
            release = not held and weapon["charge"] >= stats["min_charge"] - 1e-9
            automatic = full_hold >= stats["hold_s"] - 1e-9
            if release or automatic:
                result["full_shots"] += int(weapon["charge"] >= stats["charge"] - 1e-9)
                result["release_shots"] += int(release)
                result["auto_shots"] += int(automatic)
                weapon["cooldown"] = stats["cooldown"]
                weapon["charge"] = 0
                full_hold = 0
                charging = False
    result["lost_walk_m"] = result["charge_s"] * mech["walk_speed"] * (1 - mech["charge_move_mult"])
    return result, sequence


class SecondaryTests(unittest.TestCase):
    def setUp(self):
        self.policy = Secondary(HELLO)

    def test_laser_close_hold_full_release_and_cooldown(self):
        obs = observation()
        w = obs["self"]["weapons"][1]
        for charge in (0, .1, .2, .7):
            w["charge"] = charge
            self.assertTrue(self.policy.trigger(obs, enemy()))
        w["charge"] = .75
        self.assertFalse(self.policy.trigger(obs, enemy()))
        w.update(charge=0, cooldown=1.65)
        self.assertFalse(self.policy.trigger(obs, enemy()))
        w["cooldown"] = 0
        self.assertTrue(self.policy.trigger(obs, enemy()))

    def test_no_enemy_wrong_target_empty_unknown_and_dead(self):
        obs = observation()
        for target in (None, {**enemy(), "kind": "npc"},
                       {**enemy(), "id": 3}, {**enemy(), "hp": 0}):
            self.assertFalse(self.policy.trigger(obs, target))
        for slots in ([], [None], [None, None], [None, {"kind": "future"}]):
            obs["self"]["weapons"] = slots
            self.assertFalse(self.policy.trigger(obs, enemy()))

    def test_laser_disappearing_or_distant_enemy_releases(self):
        obs = observation(charge=.4)
        self.assertFalse(self.policy.trigger(obs, None))
        self.assertFalse(self.policy.trigger(obs, enemy(281)))

    def test_live_stats_override_defaults(self):
        obs = observation(charge=.4, stats={"charge": .4, "range": 20})
        self.assertFalse(self.policy.trigger(obs, enemy(15)))
        obs["self"]["weapons"][1]["charge"] = .2
        self.assertTrue(self.policy.trigger(obs, enemy(15)))
        self.assertFalse(self.policy.trigger(obs, enemy(21)))

    def test_lightning_range_energy_cooldown_yaw(self):
        obs = observation("lightning")
        self.assertTrue(self.policy.trigger(obs, enemy(45)))
        self.assertFalse(self.policy.trigger(obs, enemy(45.01)))
        obs["self"]["energy"] = 2.99
        self.assertFalse(self.policy.trigger(obs, enemy()))
        obs["self"]["energy"] = 3
        self.assertTrue(self.policy.trigger(obs, enemy(30, math.radians(30))))
        self.assertFalse(self.policy.trigger(obs, enemy(30, math.radians(30.1))))
        obs["self"]["aim"] = [math.pi - .1, 0]
        self.assertTrue(self.policy.trigger(obs, enemy(30, -math.pi + .1)))
        obs["self"]["weapons"][1]["cooldown"] = .1
        self.assertFalse(self.policy.trigger(obs, enemy(30, -math.pi + .1)))

    def test_lightning_live_energy_and_range(self):
        obs = observation("lightning", stats={"energy": 6, "range": 20})
        obs["self"]["energy"] = 5
        self.assertFalse(self.policy.trigger(obs, enemy(15)))
        obs["self"]["energy"] = 6
        self.assertTrue(self.policy.trigger(obs, enemy(15)))
        self.assertFalse(self.policy.trigger(obs, enemy(21)))

    def test_mortar_coarse_envelope_and_live_radius(self):
        obs = observation("mortar")
        self.assertFalse(self.policy.trigger(obs, enemy(9.5)))
        self.assertTrue(self.policy.trigger(obs, enemy(20)))
        self.assertFalse(self.policy.trigger(obs, enemy(505)))
        obs["self"]["stats"]["radius"] = 15
        self.assertFalse(self.policy.trigger(obs, enemy(20)))

    def test_secondary_gun(self):
        obs = observation("gun", ammo=5, reload=0)
        self.assertTrue(self.policy.trigger(obs, enemy()))
        obs["self"]["weapons"][1]["ammo"] = 0
        self.assertFalse(self.policy.trigger(obs, enemy()))

    def test_missing_configuration_fails_closed_without_mutation(self):
        obs = observation()
        before = copy.deepcopy(obs)
        self.assertFalse(Secondary({}).trigger(obs, enemy()))
        self.assertEqual(obs, before)

    def test_repeated_timed_sequence_improves_release_opportunities(self):
        old, old_sequence = simulate_laser(lambda obs, target: True)
        new, new_sequence = simulate_laser(self.policy.trigger)
        self.assertTrue(all(old_sequence))
        self.assertIn(False, new_sequence)
        self.assertGreater(new["full_shots"], old["full_shots"])
        self.assertEqual(new["auto_shots"], 0)
        self.assertEqual(new["full_shots"], new["release_shots"])
        self.assertLess(new["full_hold_s"], old["full_hold_s"])
        self.assertLess(new["lost_walk_m"], old["lost_walk_m"])


if __name__ == "__main__":
    unittest.main()
