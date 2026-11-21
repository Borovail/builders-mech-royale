"""Public Bot.act ground-boost regression using the official hello and map.

Run: python tests/check_mobility.py --kit /path/to/mechfight-kit
An archive and --bot /path/to/example_bot.py are also accepted. This bounded
ground model integrates config acceleration, boost drain and delayed energy
regeneration at tick_hz. It ignores jumps, air physics, combat, collision slide
and other players. It measures local travel/energy, never match placement.
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

from check_routes import EPS, load_kit


START, GOAL = (-130.0, 40.0), (-70.0, 40.0)


def observation(hello, template, arena, tick, position=START, velocity=(0, 0),
                energy=None, contact=False, npc=False, events=(), grounded=True,
                outside=False, goal=GOAL):
    obs = copy.deepcopy(template)
    config = hello["config"]
    obs.update(tick=tick, t=tick/config["match"]["tick_hz"], offer=None)
    for key in ("mechs", "projectiles", "crates", "cubes", "pickups", "sounds",
                "beams", "events", "drops"):
        obs[key] = []
    obs["events"] = list(events)
    maximum = template["self"]["max_energy"]
    z = arena.height(position)+(0.0 if grounded else config["mech"]["height"])
    obs["self"].update(pos=[*position, z], vel=[*velocity, 0.0],
                       grounded=grounded, in_zone=not outside,
                       energy=maximum if energy is None else energy,
                       stats=copy.deepcopy(config["mech"]))
    obs["zone"].update(center=list(goal), radius=20.0 if outside else 200.0,
                       next_center=list(goal), next_radius=10.0 if outside else 100.0,
                       shrink_start=60.0, shrink_end=100.0)
    if contact or npc:
        obs["mechs"] = [{"id": 104 if npc else 5,
                         "kind": "npc" if npc else "mech",
                         "pos": [position[0]+20.0, position[1]+10.0, z],
                         "vel": [0.0, 0.0, 0.0], "aim": [0.0, 0.0],
                         "hp": 1000.0, "max_hp": 1400.0,
                         "weapons": ["gun"], "charging": False}]
    return obs


def action(bot, obs):
    result = bot.act(obs)
    move = result.get("move", [0.0, 0.0])
    if result.get("tick") != obs["tick"] or len(move) != 2 or not all(
            isinstance(v, (int, float)) and math.isfinite(v) for v in move):
        raise ValueError(f"Invalid action at tick {obs['tick']}: {result}")
    if math.hypot(*move) > 1.0+EPS or not isinstance(result.get("boost", False), bool):
        raise ValueError(f"Invalid movement at tick {obs['tick']}: {result}")
    return result


def energy_step(energy, maximum, boosting, now, last_spend, dt, stats):
    if boosting:
        energy = max(0.0, energy-stats["boost_drain"]*dt)
        last_spend = now
    elif now-last_spend >= stats["energy_regen_delay"]:
        energy = min(maximum, energy+stats["energy_regen"]*dt)
    return energy, last_spend


def walk(factory, hello, template, arena, contact, e01_rule=False):
    bot = factory(copy.deepcopy(hello))
    stats, match = hello["config"]["mech"], hello["config"]["match"]
    hz, every = match["tick_hz"], match["decide_every"]
    dt = 1.0/hz
    maximum = template["self"]["max_energy"]
    position, velocity, energy = START, (0.0, 0.0), maximum
    last_spend, travelled, boosted_s, blocked = -math.inf, 0.0, 0.0, 0
    command = {"move": [0.0, 0.0], "boost": False}
    reached = False
    # Twice the walking duration is enough for acceleration and final approach.
    seconds = 2.0*math.dist(START, GOAL)/stats["walk_speed"]
    for tick in range(math.ceil(seconds*hz)):
        now = tick/hz
        if tick % every == 0:
            obs = observation(hello, template, arena, tick, position, velocity,
                              energy, contact=contact)
            command = action(bot, obs)
            if e01_rule:
                # Explicit control: same public action and lane, with exactly
                # E01's boost=not in_zone rule. This is labelled as a control,
                # rather than claiming to load an historical bot implementation.
                command["boost"] = not obs["self"]["in_zone"]
        move = command.get("move", [0.0, 0.0])
        boosting = command.get("boost", False) and energy >= stats["boost_drain"]*dt and math.hypot(*move) > EPS
        speed = stats["boost_speed"] if boosting else stats["walk_speed"]
        desired = tuple(speed*v for v in move)
        delta = tuple(desired[i]-velocity[i] for i in range(2))
        length = math.hypot(*delta)
        fraction = min(1.0, stats["ground_accel"]*dt/length) if length else 0.0
        velocity = tuple(velocity[i]+delta[i]*fraction for i in range(2))
        proposed = tuple(position[i]+velocity[i]*dt for i in range(2))
        clearance, _ = arena.clearance(position, proposed, stats["radius"], stats["height"])
        if clearance < -EPS or arena.slope(position, proposed) > stats["max_slope_deg"]:
            blocked += 1
            velocity = (0.0, 0.0)
        else:
            travelled += math.dist(position, proposed)
            position = proposed
        energy, last_spend = energy_step(energy, maximum, boosting, now,
                                         last_spend, dt, stats)
        boosted_s += dt if boosting else 0.0
        if math.dist(position, GOAL) <= 1.0:
            reached = True
            break
    return {"scenario": "e01_boost_rule_control" if e01_rule else "contact_lane" if contact else "quiet_lane",
            "reached": reached, "elapsed_s": round((tick+1)/hz, 3),
            "distance_m": round(travelled, 3), "remaining_energy": round(energy, 3),
            "boosted_s": round(boosted_s, 3), "blocked_frames": blocked}


def edge_cases(factory, hello, template, arena):
    stats = hello["config"]["mech"]
    dt = hello["config"]["match"]["decide_every"]/hello["config"]["match"]["tick_hz"]
    cases = [("outside_ground", {"outside": True}, True),
             ("outside_air", {"outside": True, "grounded": False}, True),
             ("inside_air_contact", {"contact": True, "grounded": False}, False),
             ("low_energy", {"contact": True, "energy": min(stats["boost_drain"]*dt, template["self"]["max_energy"]*0.05)}, False),
             ("zone_damage_only", {"events": [{"type": "damaged", "weapon": "zone", "amount": 20, "by": None, "bearing": None}]}, False),
             ("npc_only", {"npc": True}, False),
             ("stationary_at_goal", {"contact": True, "goal": START}, False),
             ("fresh_weapon_damage", {"events": [{"type": "damaged", "weapon": "gun", "amount": 45, "by": 5, "bearing": 0.0}]}, True)]
    results = []
    for name, kwargs, expected in cases:
        # A normal decision avoids E01's retained periodic jump at tick 0.
        tick = hello["config"]["match"]["decide_every"]
        command = action(factory(copy.deepcopy(hello)), observation(hello, template, arena, tick, **kwargs))
        got = command.get("boost", False)
        passed = got == expected
        if name == "stationary_at_goal":
            passed &= math.hypot(*command.get("move", [0.0, 0.0])) <= EPS
        results.append({"scenario": name, "boost": got, "passed": bool(passed)})
    scheduled = action(factory(copy.deepcopy(hello)), observation(
        hello, template, arena, 0, contact=True))
    results.append({"scenario": "inside_scheduled_jump", "boost": scheduled.get("boost", False),
                    "jump": scheduled.get("jump", False),
                    "passed": scheduled.get("jump", False) and not scheduled.get("boost", False)})
    return results


def continuity(factory, hello, template, arena, contact_loss=False):
    """Exercise transitions using actual observations and bounded energy updates.

    Position is held fixed deliberately: this isolates energy/visibility state
    from travel and keeps the route long while several burst/rest cycles pass.
    """
    bot = factory(copy.deepcopy(hello))
    stats, match = hello["config"]["mech"], hello["config"]["match"]
    hz, every = match["tick_hz"], match["decide_every"]
    maximum = template["self"]["max_energy"]
    energy, last_spend, minimum = maximum, -math.inf, maximum
    previous, command, transitions = False, {}, []
    # Derived from full depletion, recharge and regen delay, not policy limits.
    duration = maximum/stats["boost_drain"]+maximum/stats["energy_regen"]+2*stats["energy_regen_delay"]+3.0
    for tick in range(math.ceil(duration*hz)):
        now = tick/hz
        if tick % every == 0:
            command = action(bot, observation(hello, template, arena, tick,
                                              energy=energy,
                                              contact=not contact_loss or tick == 0))
            boosting = command.get("boost", False)
            if boosting != previous:
                transitions.append({"t": round(now, 3), "boost": boosting,
                                    "energy": round(energy, 3),
                                    "jump": command.get("jump", False)})
            previous = boosting
        boosting = command.get("boost", False) and energy >= stats["boost_drain"]/hz
        energy, last_spend = energy_step(energy, maximum, boosting, now,
                                         last_spend, 1.0/hz, stats)
        minimum = min(minimum, energy)
    stops = [entry for entry in transitions if not entry["boost"]]
    starts = [entry for entry in transitions if entry["boost"]]
    if contact_loss:
        passed = len(starts) == 1 and len(stops) == 1 and not previous
    else:
        # An observable reserve and a full regeneration delay between bursts
        # establish hysteresis without assuming internal energy percentages.
        # Periodic jump decisions deliberately interrupt boost for one command;
        # those are separate from the energy-driven rest being measured here.
        rests = [b["t"]-a["t"] for a, b in zip(transitions, transitions[1:])
                 if not a["boost"] and not a["jump"]]
        passed = len(starts) >= 2 and stops and rests and min(rests)+EPS >= stats["energy_regen_delay"]
        passed = bool(passed and minimum > stats["boost_drain"]*stats["energy_regen_delay"])
    return {"scenario": "contact_loss" if contact_loss else "burst_rest_hysteresis",
            "min_energy": round(minimum, 3), "transitions": transitions,
            "passed": bool(passed)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", required=True, type=Path)
    parser.add_argument("--bot", type=Path, default=Path(__file__).resolve().parents[1]/"example_bot.py")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mech-mobility-check-") as directory:
        hello, template, arena = load_kit(args.kit.resolve(), Path(directory))
        sys.path.insert(0, str(args.bot.resolve().parent))
        spec = importlib.util.spec_from_file_location("mobility_bot_under_test", args.bot.resolve())
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        stats = hello["config"]["mech"]
        if arena.clearance(START, GOAL, stats["radius"], stats["height"])[0] <= 0:
            raise ValueError("Travel fixture must be a clear lane")
        print("model=ground-acceleration-plus-drain-delayed-regen jump=ignored combat=ignored")
        print("bot_sha256="+hashlib.sha256(args.bot.read_bytes()).hexdigest())
        print("world_sha256="+arena.world_sha256)
        print("terrain_sha256="+hashlib.sha256(arena.raw).hexdigest())
        quiet = walk(module.Bot, hello, template, arena, False)
        control = walk(module.Bot, hello, template, arena, True, e01_rule=True)
        contact = walk(module.Bot, hello, template, arena, True)
        quiet["passed"] = quiet["reached"] and quiet["blocked_frames"] == 0 and quiet["boosted_s"] == 0
        control["passed"] = control["reached"] and control["blocked_frames"] == 0 and control["elapsed_s"] == quiet["elapsed_s"] and control["remaining_energy"] == quiet["remaining_energy"]
        contact["passed"] = contact["reached"] and contact["blocked_frames"] == 0 and contact["boosted_s"] > 0 and contact["elapsed_s"] < quiet["elapsed_s"] and 0 < contact["remaining_energy"] < quiet["remaining_energy"]
        results = [quiet, control, contact, *edge_cases(module.Bot, hello, template, arena),
                   continuity(module.Bot, hello, template, arena),
                   continuity(module.Bot, hello, template, arena, contact_loss=True)]
        for result in results:
            print(json.dumps(result, sort_keys=True))
        passed = all(result["passed"] for result in results)
        print("PASS: mobility regression" if passed else "FAIL: mobility regression")
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
