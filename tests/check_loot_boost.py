"""Public Bot.act A/B travel checks for E12's early ground-loot boost.

python -B tests/check_loot_boost.py --kit KIT --baseline E07/example_bot.py

Bounded clear-ground fixture, never placement or survival scoring. Official
acceleration, braking, decision cadence, boost drain/delayed regeneration,
pickup radii and map geometry are used. Jumps/air physics, combat, foliage,
offers and weapon cooldowns are omitted. Both bots get identical observations
from their own closed-loop trajectory; no policy internals select the item.
"""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile

from check_routes import EPS, load_kit
from check_mobility import energy_step
from check_economy import (action, fixture, ground_item, load_bot, observation,
                           slew, source_hashes, visible)


def travel(factory, hello, template, arena, kind, index):
    start, target, goal = fixture(index)
    config, match = hello["config"], hello["config"]["match"]
    stats = copy.deepcopy(config["mech"])
    hz, every = match["tick_hz"], match["decide_every"]
    dt = 1 / hz
    state = observation(hello, template, arena, 0, start, goal)["self"]
    state.update(hp=state["max_hp"], energy=state["max_energy"],
                 weapons=[copy.deepcopy(state["weapons"][0]), None])
    bot, item = factory(copy.deepcopy(hello)), ground_item(arena, target, kind)
    field = "pickups" if kind == "pickup" else "cubes"
    command, events, last_spend = {"move": [0, 0]}, [], -math.inf
    gained, gain_energy, goal_at = None, None, None
    minimum, boosted_s, blocked = state["energy"], 0.0, 0
    for tick in range(int(30 * hz)):
        now = tick / hz
        if tick % every == 0:
            obs = observation(hello, template, arena, tick, state["pos"][:2], goal)
            obs["self"], obs["events"] = copy.deepcopy(state), events
            events = []
            if item is not None and visible(item, state["pos"], state["aim"][0], config["vision"]):
                obs[field] = [copy.deepcopy(item)]
            command = action(bot, obs)
            # Explicit take is resolved at the position of this decision.
            if (item is not None and kind == "pickup" and command.get("take") == item["id"]
                    and math.dist(state["pos"][:2], target) <= config["loot"]["pickup_radius"]
                    and abs(state["pos"][2] - item["pos"][2]) <= 4):
                weapon = item["kind"]
                state["weapons"][1] = {"kind": weapon, "cooldown": 1.0,
                                        "stats": copy.deepcopy(config["weapons"][weapon])}
                events.append({"type": "took", "id": item["id"], "kind": weapon, "slot": 1})
                gained, gain_energy, item = now, state["energy"], None
        position, move = state["pos"], command.get("move", [0, 0])
        boosting = (bool(command.get("boost")) and math.hypot(*move) > EPS
                    and state["energy"] >= stats["boost_drain"] * dt)
        speed = stats["boost_speed"] if boosting else stats["walk_speed"]
        delta = [speed * move[i] - state["vel"][i] for i in range(2)]
        length = math.hypot(*delta)
        fraction = min(1, stats["ground_accel"] * dt / length) if length else 0
        velocity = [state["vel"][i] + delta[i] * fraction for i in range(2)]
        proposed = [position[i] + velocity[i] * dt for i in range(2)]
        if (arena.clearance(position[:2], proposed, stats["radius"], stats["height"])[0] < -EPS
                or arena.slope(position[:2], proposed) > stats["max_slope_deg"] + EPS):
            blocked += 1
            velocity = [0, 0]
        else:
            state["pos"] = [*proposed, arena.height(proposed)]
        state["vel"] = [*velocity, 0]
        desired_aim = command.get("aim", state["aim"])
        state["aim"] = [slew(state["aim"][0], desired_aim[0], stats["turn_rate"], dt, True),
                        slew(state["aim"][1], desired_aim[1], stats["pitch_rate"], dt)]
        state["energy"], last_spend = energy_step(state["energy"], state["max_energy"],
                                                  boosting, now, last_spend, dt, stats)
        minimum = min(minimum, state["energy"])
        boosted_s += dt * boosting
        if (item is not None and kind == "cube"
                and math.dist(state["pos"][:2], target) <= config["cubes"]["pickup_radius"]
                and abs(state["pos"][2] - item["pos"][2]) <= 4):
            state["cubes"] += 1
            for key in ("radius", "height", "eye_height", "muzzle_height"):
                stats[key] = config["mech"][key] * (1 + config["cubes"]["size_per_cube"])
            state["stats"] = copy.deepcopy(stats)
            events.append({"type": "pickup", "cubes": state["cubes"]})
            gained, gain_energy, item = (tick + 1) / hz, state["energy"], None
        if gained is not None and math.dist(state["pos"][:2], goal) <= 1:
            goal_at = (tick + 1) / hz
            break
    return {"scenario": f"{kind}_{index}", "distance_to_loot_m": math.dist(start, target),
            "pickup_at_s": gained, "energy_at_pickup": gain_energy,
            "minimum_energy": round(minimum, 3), "end_energy": round(state["energy"], 3),
            "boosted_s": round(boosted_s, 3), "goal_at_s": goal_at, "blocked_frames": blocked}


def unchanged(candidate, baseline, hello, template, arena):
    start, target, goal = fixture(0)
    hz, every = hello["config"]["match"]["tick_hz"], hello["config"]["match"]["decide_every"]
    results = []
    for mode in ("no_items", "late_pickup", "contact", "low_energy", "airborne", "crate",
                 "near_pickup", "outside_zone", "scheduled_jump", "recent_damage", "low_hp"):
        a, b = candidate(copy.deepcopy(hello)), baseline(copy.deepcopy(hello))
        differences = 0
        for n in range(1, 31):
            tick = n * every + (int(150 * hz) if mode == "late_pickup" else 0)
            if mode == "scheduled_jump":
                tick = 40 * n
            obs = observation(hello, template, arena, tick, start, goal)
            obs["self"]["hp"] = obs["self"]["max_hp"]
            if mode != "no_items":
                item = ground_item(arena, start if mode == "near_pickup" else target)
                obs["pickups"] = [item]
            if mode == "contact":
                obs["mechs"] = [{"id": 5, "kind": "mech", "pos": [start[0] + 20, start[1], arena.height(start)],
                                  "vel": [0, 0, 0], "aim": [0, 0], "hp": 1400,
                                  "max_hp": 1400, "weapons": ["gun"], "charging": False}]
            if mode == "recent_damage":
                obs["events"] = [{"type": "damaged", "weapon": "gun", "amount": 45, "by": 5, "bearing": 0}]
            if mode == "low_energy":
                obs["self"]["energy"] = 15
            if mode == "low_hp":
                obs["self"]["hp"] *= 0.5
            if mode == "airborne":
                obs["self"]["grounded"] = False
                obs["self"]["pos"][2] += 15
            if mode == "outside_zone":
                obs["self"]["in_zone"] = False
            if mode == "crate":
                obs["pickups"] = []
                obs["crates"] = [dict(ground_item(arena, target, "crate"), hp=300)]
            differences += action(a, copy.deepcopy(obs)) != action(b, copy.deepcopy(obs))
        results.append({"scenario": "unchanged_" + mode, "observations": 30,
                        "different_actions": differences, "passed": differences == 0})
    return results


def cancellations(candidate, baseline, hello, template, arena):
    start, target, goal = fixture(0)
    every = hello["config"]["match"]["decide_every"]
    hz = hello["config"]["match"]["tick_hz"]
    results = []
    for mode in ("disappeared", "contact", "cutoff"):
        a, b = candidate(copy.deepcopy(hello)), baseline(copy.deepcopy(hello))
        first = observation(hello, template, arena, every, start, goal)
        first["self"]["hp"] = first["self"]["max_hp"]
        first["pickups"] = [ground_item(arena, target)]
        initial, previous = action(a, copy.deepcopy(first)), action(b, copy.deepcopy(first))
        same_except_boost = ({k: v for k, v in initial.items() if k != "boost"}
                             == {k: v for k, v in previous.items() if k != "boost"})
        tick = int(60 * hz) if mode == "cutoff" else every * 2
        position = (target[0] - 1, target[1]) if mode == "disappeared" else start
        obs = observation(hello, template, arena, tick, position, goal)
        obs["self"]["hp"] = obs["self"]["max_hp"]
        if mode != "disappeared":
            obs["pickups"] = [ground_item(arena, target)]
        if mode == "contact":
            obs["mechs"] = [{"id": 5, "kind": "mech", "pos": [start[0]+20, start[1], arena.height(start)],
                              "vel": [0, 0, 0], "aim": [0, 0], "hp": 1400,
                              "max_hp": 1400, "weapons": ["gun"], "charging": False}]
        same = action(a, copy.deepcopy(obs)) == action(b, copy.deepcopy(obs))
        results.append({"scenario": "cancel_burst_" + mode, "initial_boost": initial.get("boost", False),
                        "initial_only_boost_changed": same_except_boost and not previous.get("boost", False),
                        "same_as_baseline_after_cancel": same,
                        "passed": bool(initial.get("boost", False) and not previous.get("boost", False)
                                       and same_except_boost and same)})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", type=Path, required=True)
    parser.add_argument("--bot", type=Path, default=Path(__file__).resolve().parents[1] / "example_bot.py")
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mech-loot-boost-check-") as temporary:
        hello, template, arena = load_kit(args.kit.resolve(), Path(temporary))
        candidate = load_bot(args.bot, "loot_boost_candidate")
        baseline = load_bot(args.baseline, "loot_boost_baseline")
        print("model=bounded-ground-loot-travel placement=none air=combat=offers=omitted")
        for label, path in (("candidate", args.bot), ("baseline", args.baseline)):
            print(label + "_sources=" + json.dumps(source_hashes(path), sort_keys=True))
        print("world_sha256=" + arena.world_sha256)
        print("terrain_sha256=" + hashlib.sha256(arena.raw).hexdigest())
        results = []
        stats = hello["config"]["mech"]
        for kind in ("pickup", "cube"):
            for index in range(3):
                start, resource, goal = fixture(index)
                for p, q in ((start, resource), (resource, goal), (start, goal)):
                    if (arena.clearance(p, q, stats["radius"] * 1.05, stats["height"] * 1.05)[0] <= 0
                            or arena.slope(p, q) > stats["max_slope_deg"]):
                        raise ValueError(f"Fixture {index} is not a clear ground lane")
                previous = travel(baseline, hello, template, arena, kind, index)
                result = travel(candidate, hello, template, arena, kind, index)
                result["baseline"] = previous
                result["passed"] = bool(result["pickup_at_s"] is not None and previous["pickup_at_s"] is not None
                    and result["pickup_at_s"] < previous["pickup_at_s"] and result["goal_at_s"] is not None
                    and previous["goal_at_s"] is not None and result["goal_at_s"] <= previous["goal_at_s"]
                    and result["minimum_energy"] > stats["boost_drain"] * stats["energy_regen_delay"]
                    and result["blocked_frames"] == previous["blocked_frames"] == 0)
                results.append(result)
        results += unchanged(candidate, baseline, hello, template, arena)
        results += cancellations(candidate, baseline, hello, template, arena)
        for result in results:
            print(json.dumps(result, sort_keys=True))
        passed = all(result["passed"] for result in results)
        print("PASS: loot travel boost" if passed else "FAIL: loot travel boost")
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
