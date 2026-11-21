"""Public Bot.act recovery relocation checks against exact E15.

Run: python -B tests/check_recovery_move.py --kit KIT [--baseline FILE]
Uses the official hello, terrain and prisms. The bounded ground integration
checks collision/slope and persistence only: combat, HP regeneration, airborne
physics and match placements are deliberately not simulated.
"""

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile

from check_economy import action, ground_item, load_bot, observation, source_hashes
from check_routes import EPS, load_kit


START = (-280.0, -280.0)
DAMAGE = {"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 0, "by": 5}


def fixture(hello, template, arena, now=90.0, position=START, goal=START,
            hp=0.5, damaged=True):
    tick = round(now * hello["config"]["match"]["tick_hz"])
    obs = observation(hello, template, arena, tick, position, goal)
    obs["self"]["hp"] = obs["self"]["max_hp"] * hp
    obs["self"]["energy"] = obs["self"]["max_energy"]
    obs["events"] = [copy.deepcopy(DAMAGE)] if damaged else []
    obs["zone"].update(center=list(START), radius=100.0,
                       next_radius=80.0, shrink_start=200.0, shrink_end=240.0)
    return obs


def pair(candidate, baseline, hello, observations):
    bots = [factory(copy.deepcopy(hello)) for factory in (candidate, baseline)]
    # Recovery requires 0.6 s of low velocity before attempting a move.
    seed = copy.deepcopy(observations[0])
    seed["t"] -= 0.6
    seed["tick"] -= round(0.6*hello["config"]["match"]["tick_hz"])
    for bot in bots:
        action(bot, copy.deepcopy(seed))
    outputs = [[], []]
    for obs in observations:
        for bot, commands in zip(bots, outputs):
            commands.append(action(bot, copy.deepcopy(obs)))
    return outputs


def unit(command):
    return math.hypot(*command["move"])


def checks(candidate, baseline, hello, template, arena):
    results = []

    def record(name, passed, **details):
        results.append({"scenario": name, "passed": bool(passed), **details})

    obs = fixture(hello, template, arena)
    new, old = pair(candidate, baseline, hello, [obs])
    record("damaged_low_hp_at_goal_moves", unit(new[0]) > 0.99 and unit(old[0]) < EPS,
           candidate_move=new[0]["move"], baseline_move=old[0]["move"])
    record("ground_move_suppresses_jump", not new[0].get("jump", False))
    record("center_relocation_keeps_energy", not new[0].get("boost", False))

    for name, now, hp, damaged in (
        ("healthy", 90, 1.0, True), ("above_hp_threshold", 90, .5501, True),
        ("no_damage", 90, .5, False), ("before_window", 44.9, .5, True),
        ("after_window", 180.1, .5, True), ("dead", 90, 0, True),
    ):
        current = fixture(hello, template, arena, now=now, hp=hp, damaged=damaged)
        new, old = pair(candidate, baseline, hello, [current])
        record(name+"_preserves_e15", new == old)

    for name, now, hp in (("at_180", 180, .5),
                          ("at_hp_threshold", 90, .55)):
        current = fixture(hello, template, arena, now=now, hp=hp)
        new, old = pair(candidate, baseline, hello, [current])
        record(name+"_moves", unit(new[0]) > .99 and unit(old[0]) < EPS)

    begin = fixture(hello, template, arena, now=45)
    ready = fixture(hello, template, arena, now=45.6, damaged=False)
    new, old = pair(candidate, baseline, hello, [begin, ready])
    record("window_opens_at_45_then_waits_for_stall", new[0] == old[0]
           and unit(new[1]) > .99 and unit(old[1]) < EPS)

    for name in ("outside", "airborne", "roof", "zone_damage", "effect_only",
                 "outside_current_circle", "outside_next_circle", "no_safe_endpoint",
                 "normal_route", "nearby_pickup", "pickup_target", "active_supply", "capturing_supply"):
        current = fixture(hello, template, arena)
        if name == "outside":
            current["self"]["in_zone"] = False
        elif name == "airborne":
            current["self"]["grounded"] = False
        elif name == "roof":
            current["self"]["pos"][2] += 30
        elif name == "zone_damage":
            current["events"][0]["weapon"] = "zone"
        elif name == "effect_only":
            current["events"] = [{"type": "effect", "kind": "burn"}]
        elif name == "outside_current_circle":
            current["zone"]["center"] = [START[0]+110, START[1]]
        elif name == "outside_next_circle":
            current["zone"]["next_radius"] = 1
        elif name == "no_safe_endpoint":
            current["zone"]["radius"] = current["zone"]["next_radius"] = 10
        elif name == "normal_route":
            current["zone"]["next_center"] = [START[0]+60, START[1]]
        elif name == "nearby_pickup":
            current["self"]["weapons"][1] = None
            current["pickups"] = [ground_item(arena, (START[0]+1, START[1]))]
        elif name == "pickup_target":
            current["events"] = []
            current["self"]["weapons"][1] = None
            current["pickups"] = [ground_item(arena, (START[0]+20, START[1]))]
        elif name in ("active_supply", "capturing_supply"):
            # Seed damage before the supply's three-second cooldown, so the
            # final observation qualifies both for recovery and real capture.
            current["events"] = []
            point = START if name == "capturing_supply" else (START[0]+20, START[1])
            current["drops"] = [{"id": 20001, "pos": [*point, arena.height(point)],
                                 "radius": 8, "capture_s": 4, "lands_in": 0,
                                 "progress": 1 if name == "capturing_supply" else 0,
                                 "holder": current["self"]["id"] if name == "capturing_supply" else None}]
        observations = [current]
        if name in ("active_supply", "capturing_supply", "pickup_target"):
            seed = fixture(hello, template, arena, now=86.5, hp=1)
            observations.insert(0, seed)
        new, old = pair(candidate, baseline, hello, observations)
        record(name+"_preserves_e15", new == old,
               candidate_move=new[-1]["move"], baseline_move=old[-1]["move"])

    # Start normally, then independently remove eligibility on the next call.
    for name in ("healed", "damage_expired", "left_zone", "left_ground",
                 "next_zone_changed", "pickup_appears"):
        first = fixture(hello, template, arena)
        second = fixture(hello, template, arena, now=90.1, damaged=False)
        if name == "healed":
            second["self"]["hp"] = second["self"]["max_hp"]
        elif name == "damage_expired":
            second = fixture(hello, template, arena, now=94.1, damaged=False)
        elif name == "left_zone":
            second["self"]["in_zone"] = False
        elif name == "left_ground":
            second["self"]["grounded"] = False
        elif name == "next_zone_changed":
            second["zone"]["next_radius"] = 10
        elif name == "pickup_appears":
            second["self"]["weapons"][1] = None
            second["pickups"] = [ground_item(arena, (START[0]+1, START[1]))]
        new, old = pair(candidate, baseline, hello, [first, second])
        record(name+"_aborts", unit(new[0]) > .99 and new[1] == old[1])

    first = fixture(hello, template, arena)
    second = fixture(hello, template, arena, now=90.1, damaged=False)
    new, old = pair(candidate, baseline, hello, [first, second])
    record("waypoint_persists_without_duplicate_damage", new[0]["move"] == new[1]["move"]
           and unit(new[1]) > .99 and unit(old[1]) < EPS)

    expired = fixture(hello, template, arena, now=93.1, damaged=False)
    retry = fixture(hello, template, arena, now=93.8, damaged=True)
    new, old = pair(candidate, baseline, hello, [first, expired, retry])
    record("ttl_stops_and_retry_delay_prevents_relaunch", unit(new[0]) > .99
           and new[1:] == old[1:])

    before_damage = fixture(hello, template, arena, now=89.9, hp=1, damaged=False)
    samples = [before_damage]
    for i in range(8):
        x = START[0] + (0.9 if i % 2 == 0 else -0.9)
        observation = fixture(hello, template, arena, now=90 + i / 10,
                              position=(x, START[1]), damaged=(i == 0))
        observation["self"]["vel"] = [-6.0 if i % 2 == 0 else 6.0, 0.0, 0.0]
        samples.append(observation)
    new, old = pair(candidate, baseline, hello, samples)
    record("close_oscillation_relocates_after_delay",
           new[:7] == old[:7] and old[-1]["move"][0] > 0.99
           and new[-2]["move"][0] < -0.99 and new[-1]["move"][0] < -0.99)

    # Official prism lane: the eastward 24 m leg intersects a solid building.
    # Recovery must select a different verified heading, with no navigator mock.
    point = (-130., 64.)
    blocked = fixture(hello, template, arena, position=point, goal=point)
    blocked["zone"]["center"] = list(point)
    new, old = pair(candidate, baseline, hello, [blocked])
    move, stats = new[0]["move"], hello["config"]["mech"]
    endpoint = [point[i]+24*move[i] for i in range(2)]
    east_clearance = arena.clearance(point, (point[0]+24, point[1]), stats["radius"], stats["height"])[0]
    chosen_clearance = arena.clearance(point, endpoint, stats["radius"], stats["height"])[0]
    record("official_prism_rejects_blocked_heading", east_clearance < 0
           and unit(new[0]) > .99 and unit(old[0]) < EPS
           and chosen_clearance >= -EPS
           and arena.slope(point, endpoint) <= stats["max_slope_deg"]+EPS,
           candidate_move=move, blocked_east_clearance_m=east_clearance,
           chosen_clearance_m=chosen_clearance)
    return results


def walk(factory, hello, template, arena):
    """Official acceleration + independent swept geometry, no combat model."""
    bot = factory(copy.deepcopy(hello))
    config, stats = hello["config"], hello["config"]["mech"]
    hz, every = config["match"]["tick_hz"], config["match"]["decide_every"]
    position, velocity, command = list(START), [0., 0.], {"move": [0., 0.]}
    blocked, travel, jumps, first_move, commands = 0, 0., 0, None, []
    excursion, stopped_after_launch = 0., False
    for frame in range(int(5*hz)):
        now = 90 + frame/hz
        if frame % every == 0:
            obs = fixture(hello, template, arena, now=now, position=position, damaged=frame == 0)
            obs["self"]["vel"] = [*velocity, 0]
            command = action(bot, obs)
            commands.append(command)
            if unit(command) > .1:
                first_move = now if first_move is None else first_move
                jumps += bool(command.get("jump"))
            if frame >= int(3.7*hz):
                # Once the temporary leg has ended, normal E15 movement back
                # to next_center must resume, even when it reverses direction.
                expected = [START[i]-position[i] for i in range(2)]
                length_to_goal = math.hypot(*expected)
                if length_to_goal > stats["radius"]:
                    stopped_after_launch |= sum(a*b for a, b in zip(command["move"], expected)) > .99*length_to_goal
        desired = [stats["walk_speed"]*v for v in command["move"]]
        delta = [desired[i]-velocity[i] for i in range(2)]
        length = math.hypot(*delta)
        fraction = min(1., stats["ground_accel"]/hz/length) if length else 0
        velocity = [velocity[i]+delta[i]*fraction for i in range(2)]
        proposed = [position[i]+velocity[i]/hz for i in range(2)]
        if (arena.clearance(position, proposed, stats["radius"], stats["height"])[0] < -EPS
                or arena.slope(position, proposed) > stats["max_slope_deg"]+EPS):
            blocked += 1
            velocity = [0., 0.]
        else:
            travel += math.dist(position, proposed)
            position = proposed
            excursion = max(excursion, math.dist(START, position))
    return {"distance_m": round(travel, 4), "displacement_m": round(math.dist(START, position), 4),
            "maximum_excursion_m": round(excursion, 4), "resumed_e15_route": stopped_after_launch,
            "blocked_frames": blocked, "jump_requests_while_moving": jumps,
            "first_move_s": first_move, "last_move_norm": unit(commands[-1])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--kit", required=True, type=Path)
    parser.add_argument("--bot", type=Path, default=root/"example_bot.py")
    parser.add_argument("--baseline", type=Path, default=root/"artifacts/e15-submission-exact/example_bot.py")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mech-recovery-check-") as temporary:
        hello, template, arena = load_kit(args.kit.resolve(), Path(temporary))
        candidate, baseline = load_bot(args.bot, "recovery_candidate"), load_bot(args.baseline, "recovery_baseline")
        print("model=bounded-ground-fixture combat=omitted match-score=not-measured")
        for name, path in (("candidate", args.bot), ("baseline", args.baseline)):
            hashes = source_hashes(path)
            for module in ("recovery_move.py",):
                if (path.parent/module).exists():
                    hashes[module] = hashlib.sha256((path.parent/module).read_bytes()).hexdigest()
            print(name+"_sources="+json.dumps(hashes, sort_keys=True))
        print("world_sha256="+arena.world_sha256)
        print("terrain_sha256="+hashlib.sha256(arena.raw).hexdigest())
        results = checks(candidate, baseline, hello, template, arena)
        new, old = walk(candidate, hello, template, arena), walk(baseline, hello, template, arena)
        results.append({"scenario": "closed_loop_short_clear_relocation", "candidate": new,
                        "baseline": old, "passed": 8 < new["maximum_excursion_m"] < 26
                        and new["distance_m"] < 60 and new["blocked_frames"] == 0
                        and new["jump_requests_while_moving"] == 0
                        and new["resumed_e15_route"] and old["distance_m"] < EPS})
        for result in results:
            print("check="+json.dumps(result, sort_keys=True))
        passed = all(result["passed"] for result in results)
        print(("PASS" if passed else "FAIL")+f": {len(results)} public recovery scenarios")
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
