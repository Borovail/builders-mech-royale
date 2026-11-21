"""Independent E13 public Bot.act checks against an E10 snapshot.

python -B tests/check_opening.py --kit KIT --baseline E10/example_bot.py

This is the bounded official-map ground/resource fixture from check_economy,
not a match or a survival estimate. Acceleration, swept collision, gun flight,
crate damage, cube pickup and explicit offer choice are simulated there.
Jump/air physics, opponents' damage and random crate weapons are omitted.
The extra laser is a real competing empty-slot pickup, with official visibility,
pickup radius and equip delay. Coordinates are fixed independently of E13.
"""

import argparse
import copy
import json
import math
from pathlib import Path
import tempfile

import check_economy as economy
from check_routes import load_kit


START = (-280.0, -280.0)
RESOURCE = (-260.0, -270.0)
LASER = (-200.0, -300.0)
GOAL = (-180.0, -300.0)


def scene(hello, template, arena, kind="cube", seconds=0.2, cubes=0):
    hz = hello["config"]["match"]["tick_hz"]
    obs = economy.observation(hello, template, arena, round(seconds*hz), START, GOAL)
    obs["self"]["cubes"] = cubes
    obs["self"]["aim"] = [math.atan2(RESOURCE[1]-START[1], RESOURCE[0]-START[0]), 0.0]
    obs["pickups"] = [economy.ground_item(arena, LASER)]
    obs["pickups"][0]["kind"] = "laser"
    item = economy.ground_item(arena, RESOURCE, kind)
    if kind == "crate":
        item["hp"] = hello["config"]["crates"]["hp"]
    obs["cubes" if kind == "cube" else "crates"] = [item]
    return obs


def same_action(candidate, baseline, hello, obs):
    a = economy.action(candidate(copy.deepcopy(hello)), copy.deepcopy(obs))
    b = economy.action(baseline(copy.deepcopy(hello)), copy.deepcopy(obs))
    return a == b, a, b


def closed_loop(factory, hello, template, arena, kind):
    """Add the competing pickup to the existing independent resource model."""
    instances = []

    class WithLaser(factory):
        def __init__(self, greeting):
            super().__init__(greeting)
            self.equipped_at = None
            self.offer_at = None
            self.offered_choice = None
            instances.append(self)

        def act(self, obs):
            item = economy.ground_item(arena, LASER)
            item["kind"] = "laser"
            now = obs["t"]
            if self.equipped_at is None:
                if economy.visible(item, obs["self"]["pos"], obs["self"]["aim"][0], hello["config"]["vision"]):
                    obs["pickups"] = [item]
            else:
                obs["self"]["weapons"][1] = {
                    "kind": "laser", "stats": copy.deepcopy(hello["config"]["weapons"]["laser"]),
                    "cooldown": max(0, self.equipped_at+hello["config"]["loot"]["equip_s"]-now)}
            if obs["offer"] is not None and self.offer_at is None:
                self.offer_at = now
            command = super().act(obs)
            if obs["offer"] is not None and "pick" in command:
                self.offered_choice = command["pick"]
            if (command.get("take") == item["id"] and self.equipped_at is None
                    and math.dist(item["pos"], obs["self"]["pos"]) <= hello["config"]["loot"]["pickup_radius"]):
                self.equipped_at = now
            return command

    economy.DRAWERS[WithLaser] = economy.DRAWERS[factory]
    original = economy.fixture
    economy.fixture = lambda index: (START, RESOURCE, GOAL)
    try:
        result = economy.run(WithLaser, hello, template, arena, kind, seconds=18)
    finally:
        economy.fixture = original
    result.update(scenario="opening_"+kind, laser_equipped_s=instances[0].equipped_at,
                  offer_observed_s=instances[0].offer_at,
                  explicit_offer_index=instances[0].offered_choice)
    result["equipment"] = int(instances[0].equipped_at is not None)
    return result


def checks(candidate, baseline, hello, template, arena):
    results = []
    for kind in ("cube", "crate"):
        obs = scene(hello, template, arena, kind)
        a = economy.action(candidate(copy.deepcopy(hello)), copy.deepcopy(obs))
        b = economy.action(baseline(copy.deepcopy(hello)), copy.deepcopy(obs))
        # A direct, unobstructed fork: resource is northeast, the laser southeast.
        results.append({"scenario": kind+"_fork", "candidate_move": a["move"],
                        "baseline_move": b["move"],
                        "passed": a["move"][1] > 0 and b["move"][1] < 0})
        for mode in ("no_resource", "after_60", "has_cube", "rooftop", "too_far",
                     "incoming_damage", "close_opponent", "urgent_zone"):
            obs = scene(hello, template, arena, kind)
            field = "cubes" if kind == "cube" else "crates"
            if mode == "no_resource":
                obs[field] = []
            elif mode == "after_60":
                obs["t"], obs["tick"] = 60.0, round(60*hello["config"]["match"]["tick_hz"])
            elif mode == "has_cube":
                obs["self"]["cubes"] = 1
            elif mode == "rooftop":
                start, goal, roof, _ = economy.roof_fixture(arena, hello["config"]["mech"])
                obs = economy.observation(hello, template, arena, 4, start, goal)
                roof.pop("kind")
                if kind == "crate":
                    roof["hp"] = hello["config"]["crates"]["hp"]
                obs[field] = [roof]
            elif mode == "too_far":
                point = (START[0], START[1]+150)
                obs[field][0]["pos"] = [*point, arena.height(point)]
            elif mode == "incoming_damage":
                obs["events"] = [{"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 0., "by": 5}]
            elif mode == "close_opponent":
                obs["mechs"] = [{"id": 5, "kind": "mech", "pos": [-260., -285., arena.height(START)],
                                  "vel": [0, 0, 0], "aim": [math.pi, 0], "hp": 1400,
                                  "max_hp": 1400, "weapons": ["gun"]}]
            elif mode == "urgent_zone":
                obs["zone"].update(next_radius=15, shrink_start=-10, shrink_end=obs["t"]+2)
            equal, a, b = same_action(candidate, baseline, hello, obs)
            results.append({"scenario": kind+"_"+mode, "passed": equal,
                            "candidate_action": a, "baseline_action": b})
            if mode in ("incoming_damage", "close_opponent", "urgent_zone"):
                first = scene(hello, template, arena, kind)
                bots = [candidate(copy.deepcopy(hello)), baseline(copy.deepcopy(hello))]
                for bot in bots:
                    economy.action(bot, copy.deepcopy(first))
                obs["t"] += 0.1
                obs["tick"] += hello["config"]["match"]["decide_every"]
                a, b = [economy.action(bot, copy.deepcopy(obs)) for bot in bots]
                results.append({"scenario": kind+"_cancel_on_"+mode, "passed": a == b,
                                "candidate_action": a, "baseline_action": b})
        a = closed_loop(candidate, hello, template, arena, kind)
        b = closed_loop(baseline, hello, template, arena, kind)
        gain = a["first_gain_s"]
        # Acceptance is a real early gain and offer, with the same baseline
        # conditions. No candidate-specific utility or distance threshold here.
        a["passed"] = bool(gain is not None and gain < 60 and a["cubes"] >= 1
                           and a["upgrades"] and a["offer_observed_s"] is not None
                           and a["explicit_offer_index"] is not None
                           and a["blocked_frames"] == 0
                           and (b["first_gain_s"] is None or gain < b["first_gain_s"]))
        results.append({"scenario": kind+"_real_gain", "passed": a["passed"], "candidate": a, "baseline": b})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", type=Path, required=True)
    parser.add_argument("--bot", type=Path, default=Path(__file__).resolve().parents[1]/"example_bot.py")
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mech-opening-check-") as temporary:
        hello, template, arena = load_kit(args.kit.resolve(), Path(temporary))
        stats = hello["config"]["mech"]
        for a, b in ((START, RESOURCE), (RESOURCE, LASER), (START, LASER), (LASER, GOAL)):
            if arena.clearance(a, b, stats["radius"], stats["height"])[0] <= 0 or arena.slope(a, b) > stats["max_slope_deg"]:
                raise ValueError(f"Opening fixture is not traversable: {a} -> {b}")
        candidate = economy.load_bot(args.bot, "opening_candidate")
        baseline = economy.load_bot(args.baseline, "opening_baseline")
        print("model=bounded-ground-resource-not-match")
        print("candidate_sources="+json.dumps(economy.source_hashes(args.bot), sort_keys=True))
        print("baseline_sources="+json.dumps(economy.source_hashes(args.baseline), sort_keys=True))
        print("world_sha256="+arena.world_sha256)
        results = checks(candidate, baseline, hello, template, arena)
        for result in results:
            print(json.dumps(result, sort_keys=True))
        passed = all(result["passed"] for result in results)
        print("PASS: opening public actions" if passed else "FAIL: opening public actions")
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
