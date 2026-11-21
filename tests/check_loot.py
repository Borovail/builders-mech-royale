"""Independent secondary-loot regression through public Bot.act.

Run: python tests/check_loot.py --kit /path/to/mechfight-kit
Also accepts the kit archive and --bot /path/to/example_bot.py for comparisons.
This is a deterministic, short, closed-loop ground walk, NOT a match simulator:
walk speed is fixed; jump, boost, acceleration, combat and other players are
omitted. Real map clearance/terrain and protocol horizontal/vertical take rules
are checked. Local pickups and route progress do not predict placement scores.
"""

import argparse
import copy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile

from check_routes import EPS, direct_move, load_kit


START, GOAL, ITEM = (-130.0, 40.0), (-70.0, 40.0), (-118.0, 47.0)


def pickup(arena, xy=ITEM, dz=0.0):
    return {"id": 8013, "kind": "lightning", "pos": [*xy, arena.height(xy)+dz]}


def observation(hello, template, arena, position, velocity, tick, weapons,
                pickups, mode="safe", events=()):
    obs = copy.deepcopy(template)
    config = hello["config"]
    now = tick/config["match"]["tick_hz"]
    obs.update(tick=tick, t=now, offer=None)
    for key in ("mechs", "projectiles", "crates", "cubes", "pickups", "sounds",
                "beams", "events", "drops"):
        obs[key] = []
    obs["pickups"] = copy.deepcopy(pickups)
    obs["events"] = list(events)
    # Give every scenario the same long, clear route. Urgent cases differ only
    # in circle geometry/timing; current radius shrinks consistently with time.
    radius = 200.0
    next_radius, shrink_start, shrink_end = 100.0, 60.0, 100.0
    if mode == "outside":
        radius, next_radius = 20.0, 10.0
    elif mode == "closing":
        radius = 80.0+(5.0-80.0)*min(1.0, now/2.0)
        next_radius, shrink_start, shrink_end = 5.0, 0.0, 2.0
    obs["zone"].update(center=list(GOAL), radius=radius, next_center=list(GOAL),
                       next_radius=next_radius, shrink_start=shrink_start,
                       shrink_end=shrink_end)
    obs["self"].update(pos=[*position, arena.height(position)],
                       vel=[*velocity, 0.0], grounded=True,
                       in_zone=math.dist(position, GOAL) <= radius,
                       stats=copy.deepcopy(config["mech"]),
                       weapons=copy.deepcopy(weapons))
    return obs


def validate_action(action, tick):
    move = action.get("move", [0.0, 0.0])
    if action.get("tick") != tick or len(move) != 2 or not all(
            isinstance(value, (int, float)) and math.isfinite(value) for value in move):
        raise ValueError(f"Invalid action at tick {tick}: {action}")
    if math.hypot(*move) > 1.0+EPS:
        raise ValueError(f"Move exceeds unit length: {move}")
    return move


def take(action, item, position, weapons, config):
    """Apply only §6/§7 take rules, before movement on the decision tick."""
    if "take" not in action:
        return None
    reason = None
    if item is None or action["take"] != item["id"]:
        reason = "gone"
    elif math.dist(position[:2], item["pos"][:2]) > config["loot"]["pickup_radius"] or abs(position[2]-item["pos"][2]) > 4.0:
        reason = "range"
    elif any(weapon and weapon["kind"] == item["kind"] for weapon in weapons):
        reason = "have"
    elif all(weapons):
        reason = "full"
    if reason:
        return {"type": "take_failed", "id": action["take"], "reason": reason}
    slot = weapons.index(None)
    weapons[slot] = {"kind": item["kind"], "cooldown": max(
        config["loot"]["equip_s"], config["weapons"][item["kind"]]["cooldown"]),
        "stats": copy.deepcopy(config["weapons"][item["kind"]])}
    return {"type": "took", "id": item["id"], "kind": item["kind"], "slot": slot}


def walk(factory, hello, template, arena, name, xy=ITEM, mode="safe", full=False,
         dz=0.0, seconds=9.0):
    bot = factory(copy.deepcopy(hello))
    config, position, velocity = hello["config"], START, [0.0, 0.0]
    hz, every = config["match"]["tick_hz"], config["match"]["decide_every"]
    speed = config["mech"]["walk_speed"]
    weapons = copy.deepcopy(template["self"]["weapons"])
    weapons[1] = None
    if full:
        weapons[1] = {"kind": "mortar", "cooldown": 0.0,
                      "stats": copy.deepcopy(config["weapons"]["mortar"])}
    item = pickup(arena, xy, dz)
    move, events = [0.0, 0.0], []
    collected = attempts = failures = blocked = 0
    travelled, max_lateral = 0.0, 0.0
    reached = False
    for tick in range(math.ceil(seconds*hz)):
        if tick % every == 0:
            # All tested items begin in proximity-sensor range. Objects behind
            # us disappear when outside that range; no invented item memory.
            visible = [item] if item and math.dist(position, item["pos"][:2]) <= config["vision"]["near_range"] else []
            obs = observation(hello, template, arena, position, velocity, tick,
                              weapons, visible, mode, events)
            action = bot.act(obs)
            move = validate_action(action, tick)
            events = []
            if "drop" in action:
                raise ValueError("Secondary acquisition must not drop existing weapons")
            if "take" in action:
                attempts += 1
                event = take(action, item, obs["self"]["pos"], weapons, config)
                events = [event]
                if event["type"] == "took":
                    collected += 1
                    item = None
                else:
                    failures += 1
        proposed = tuple(position[i]+speed*move[i]/hz for i in range(2))
        clearance, _ = arena.clearance(position, proposed, config["mech"]["radius"], config["mech"]["height"])
        if clearance < -EPS or arena.slope(position, proposed) > config["mech"]["max_slope_deg"]:
            blocked += 1
            velocity = [0.0, 0.0]
        else:
            travelled += math.dist(position, proposed)
            velocity = [speed*value for value in move]
            position = proposed
        max_lateral = max(max_lateral, abs(position[1]-START[1]))
        if math.dist(position, GOAL) <= 1.0:
            reached = True
            break
    expected = name == "nearby_secondary"
    passed = reached and blocked == failures == 0 and collected == int(expected)
    if not expected:
        passed &= attempts == 0 and max_lateral <= EPS
    return {"scenario": name, "collected": collected, "take_attempts": attempts,
            "take_failures": failures, "reached_circle_goal": reached,
            "elapsed_s": round((tick+1)/hz, 3), "distance_m": round(travelled, 3),
            "max_lateral_m": round(max_lateral, 3), "blocked_frames": blocked,
            "passed": bool(passed)}


def released_target(factory, hello, template, arena, reason):
    bot = factory(copy.deepcopy(hello))
    weapons = copy.deepcopy(template["self"]["weapons"])
    weapons[1] = None
    item = pickup(arena, (-129.0, 41.0) if reason == "failed" else ITEM)
    first = observation(hello, template, arena, START, [0, 0], 0, weapons, [item])
    initial = bot.act(first)
    validate_action(initial, 0)
    tick = hello["config"]["match"]["decide_every"]
    events = [{"type": "take_failed", "id": item["id"], "reason": "gone"}] if reason == "failed" else []
    # A disappeared pickup or an authoritative failure must release its target
    # immediately, even when a stale visible entry accompanies the event.
    remaining = [item] if reason == "failed" else []
    obs = observation(hello, template, arena, START, [0, 0], tick, weapons, remaining, events=events)
    action = bot.act(obs)
    move = validate_action(action, tick)
    passed = "take" not in action and math.dist(move, direct_move(START, GOAL)) <= EPS
    if reason == "failed":
        passed &= initial.get("take") == item["id"]
    return {"scenario": "release_"+reason, "move": move, "passed": passed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", required=True, type=Path)
    parser.add_argument("--bot", type=Path, default=Path(__file__).resolve().parents[1]/"example_bot.py")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mech-loot-check-") as directory:
        hello, template, arena = load_kit(args.kit.resolve(), Path(directory))
        sys.path.insert(0, str(args.bot.resolve().parent))
        spec = importlib.util.spec_from_file_location("loot_bot_under_test", args.bot.resolve())
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = hello["config"]
        for a, b in ((START, GOAL), (START, ITEM), (ITEM, GOAL)):
            if arena.clearance(a, b, config["mech"]["radius"], config["mech"]["height"])[0] <= 0:
                raise ValueError("Fixture is no longer open ground on this map")
        if abs(ITEM[1]-START[1]) <= config["loot"]["pickup_radius"]:
            raise ValueError("Control route must pass outside the actual take radius")
        print("model=fixed-speed-ground-walk take=protocol-horizontal-plus-4m-vertical")
        print("bot_sha256="+hashlib.sha256(args.bot.read_bytes()).hexdigest())
        print("world_sha256="+arena.world_sha256)
        print("terrain_sha256="+hashlib.sha256(arena.raw).hexdigest())
        results = [walk(module.Bot, hello, template, arena, "nearby_secondary"),
                   walk(module.Bot, hello, template, arena, "full_slot", full=True),
                   walk(module.Bot, hello, template, arena, "outside_circle", mode="outside"),
                   walk(module.Bot, hello, template, arena, "urgent_shrink", mode="closing"),
                   walk(module.Bot, hello, template, arena, "high_item", xy=(-129.0, 40.0), dz=20.0),
                   walk(module.Bot, hello, template, arena, "out_of_the_way", xy=(-145.0, 40.0)),
                   released_target(module.Bot, hello, template, arena, "disappeared"),
                   released_target(module.Bot, hello, template, arena, "failed")]
        for result in results:
            print(json.dumps(result, sort_keys=True))
        passed = all(result["passed"] for result in results)
        print("PASS: secondary-loot regression" if passed else "FAIL: secondary-loot regression")
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
