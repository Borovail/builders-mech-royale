"""Independent public Bot.act resource checks on the official arena.

python tests/check_economy.py --kit KIT [--bot example_bot.py] [--baseline FILE]
KIT accepts the official archive or its extracted directory. --suite equipment
and --suite resources allow the independently reviewable features to be checked
separately. Baseline runs report the same closed-loop inventory metrics; only
the bot under test is required to pass the resource acquisition scenarios.

This is a bounded ground/resource fixture, NOT a game or combat simulator:
movement integrates acceleration with swept-prism/slope checks (check_routes),
but ignores jump, air physics, collision slide and enemy damage. Resource-only
gun shots use actual slewed aim, a stationary crate cylinder, cooldown, magazine,
reload and flight time. Spread is fixed at zero, crates drop exactly the official
cube count and no random weapon. FOV uses the official yaw/near ranges; foliage
and tree vision are omitted. No match score or survival estimate is produced.
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

from check_routes import EPS, direct_move, inside, load_kit
from check_mobility import energy_step


LANES = [((-280.0, -280.0), 60.0), ((-280.0, -240.0), 75.0),
         ((-200.0, 0.0), 90.0)]
DRAWERS = {}


def fixture(index):
    start, distance = LANES[index]
    resource = (start[0]+math.sqrt(distance**2-30.0**2), start[1]+30.0)
    return start, resource, (start[0]+100.0, start[1]-20.0)


def action(bot, obs):
    # Debug is normally cleared by mf.run, not Bot.act itself.
    DRAWERS[type(bot)].clear()
    result = bot.act(obs)
    move, aim = result.get("move", [0.0, 0.0]), result.get("aim", [0.0, 0.0])
    if (result.get("tick") != obs["tick"] or len(move) != 2 or len(aim) != 2
            or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in [*move, *aim])
            or math.hypot(*move) > 1.0+EPS):
        raise ValueError(f"Invalid public action at {obs['tick']}: {result}")
    if "pick" in result and (not obs["offer"] or not isinstance(result["pick"], int)
                            or not 0 <= result["pick"] < len(obs["offer"]["choices"])):
        raise ValueError(f"Invalid offer index: {result}")
    return result


def observation(hello, template, arena, tick, start, goal):
    obs = copy.deepcopy(template)
    obs.update(tick=tick, t=tick/hello["config"]["match"]["tick_hz"], offer=None)
    for key in ("mechs", "projectiles", "crates", "cubes", "pickups", "sounds",
                "beams", "events", "drops"):
        obs[key] = []
    stats = hello["config"]["mech"]
    obs["self"].update(pos=[*start, arena.height(start)], vel=[0.0, 0.0, 0.0],
                       aim=[0.0, 0.0], grounded=True, in_zone=True, cubes=0,
                       upgrades=[], stats=copy.deepcopy(stats))
    obs["zone"].update(center=list(goal), radius=300.0, next_center=list(goal),
                       next_radius=150.0, shrink_start=60.0, shrink_end=110.0)
    return obs


def ground_item(arena, point, kind="pickup"):
    item = {"id": 8001 if kind == "pickup" else 5001 if kind == "cube" else 1001,
            "pos": [*point, arena.height(point)]}
    if kind == "pickup":
        item["kind"] = "lightning"
    return item


def aligned(move, position, goal, threshold=0.995):
    expected = direct_move(position, goal)
    return math.dist(move, expected) < 1.0-threshold


def slew(value, target, speed, dt, angular=False):
    delta = (target-value+math.pi) % (2*math.pi)-math.pi if angular else target-value
    return value+max(-speed*dt, min(speed*dt, delta))


def visible(item, position, yaw, vision):
    delta = (item["pos"][0]-position[0], item["pos"][1]-position[1])
    distance = math.dist(item["pos"], position)
    angle = (math.atan2(delta[1], delta[0])-yaw+math.pi) % (2*math.pi)-math.pi
    return distance <= vision["near_range"] or (distance <= vision["range"] and
            abs(angle) <= math.radians(vision["fov_deg"])/2.0)


def crate_hit(position, aim, crate, stats, config, arena):
    """Ray/cylinder/terrain test for a stationary resource, with no opponents."""
    yaw, pitch = aim
    unit = (math.cos(yaw)*math.cos(pitch), math.sin(yaw)*math.cos(pitch), math.sin(pitch))
    muzzle = (position[0], position[1], position[2]+stats["muzzle_height"])
    offset = stats["radius"]+0.5
    origin = tuple(muzzle[i]+offset*unit[i] for i in range(3))
    center = crate["pos"]
    radius = config["crates"]["radius"]+config["projectile"]["gun_radius"]
    dx, dy = origin[0]-center[0], origin[1]-center[1]
    a, b = unit[0]**2+unit[1]**2, 2*(dx*unit[0]+dy*unit[1])
    disc = b*b-4*a*(dx*dx+dy*dy-radius*radius)
    if disc < 0 or a < EPS:
        return None
    enter = max(0.0, (-b-math.sqrt(disc))/(2*a))
    leave = (-b+math.sqrt(disc))/(2*a)
    if leave < enter or enter > config["weapons"]["gun"]["range"]:
        return None
    bottom, top = center[2], center[2]+config["crates"]["height"]
    if abs(unit[2]) < EPS:
        if not bottom <= origin[2] <= top:
            return None
    else:
        ztimes = sorted(((bottom-origin[2])/unit[2], (top-origin[2])/unit[2]))
        enter, leave = max(enter, ztimes[0]), min(leave, ztimes[1])
        if enter > leave:
            return None
    # The fixture corridors have no prism intersections. Ground may still
    # obstruct a downward shot; sample the ray at <= 0.25 m.
    for i in range(max(1, math.ceil(enter/0.25))):
        distance = enter*i/max(1, math.ceil(enter/0.25))
        point = tuple(origin[j]+unit[j]*distance for j in range(3))
        if point[2] <= arena.height(point[:2]):
            return None
    return enter/config["weapons"]["gun"]["speed"]


def run(factory, hello, template, arena, kind, index=0, disappear=False, seconds=24.0):
    start, target, goal = fixture(index)
    config, stats = hello["config"], copy.deepcopy(hello["config"]["mech"])
    hz, every = config["match"]["tick_hz"], config["match"]["decide_every"]
    dt = 1.0/hz
    bot = factory(copy.deepcopy(hello))
    state = observation(hello, template, arena, 0, start, goal)["self"]
    state["aim"] = [math.atan2(target[1]-start[1], target[0]-start[0]), 0.0]
    item = ground_item(arena, target, kind)
    if kind == "crate":
        item["hp"] = config["crates"]["hp"]
    resource = {"pickup": "pickups", "cube": "cubes", "crate": "crates"}[kind]
    cube, offer, events, arrivals = None, None, [], []
    command = {"move": [0.0, 0.0], "aim": list(state["aim"])}
    travelled, blocked, shots, hits, take_attempts = 0.0, 0, 0, 0, 0
    last_spend, minimum, first_gain, vanished_at = -math.inf, math.inf, None, None
    resumed_at, goal_at, destroyed_at = None, None, None
    for tick in range(math.ceil(seconds*hz)):
        now = tick/hz
        position = state["pos"]
        weapon = state["weapons"][0]
        weapon["cooldown"] = max(0.0, weapon["cooldown"]-dt)
        if weapon.get("reload", 0.0) > 0.0:
            weapon["reload"] = max(0.0, weapon["reload"]-dt)
            if weapon["reload"] <= EPS:
                weapon["ammo"] = config["weapons"]["gun"]["mag"]
        for at, damage in list(arrivals):
            if at <= now:
                arrivals.remove((at, damage))
                if item is not None and kind == "crate":
                    item["hp"] -= damage
                    hits += 1
                    events.append({"type": "hit", "target": item["id"], "amount": damage, "weapon": "gun"})
                    if item["hp"] <= 0:
                        destroyed_at, item = now, None
                        cube = ground_item(arena, target, "cube")
        if disappear and item is not None and math.dist(position, item["pos"]) <= 9.0:
            item, vanished_at = None, now
        if tick % every == 0:
            obs = observation(hello, template, arena, tick, position[:2], goal)
            obs["self"] = copy.deepcopy(state)
            obs["events"], events = events, []
            obs["offer"] = copy.deepcopy(offer)
            if item is not None and visible(item, position, state["aim"][0], config["vision"]):
                obs[resource] = [copy.deepcopy(item)]
            if cube is not None and visible(cube, position, state["aim"][0], config["vision"]):
                obs["cubes"] = [copy.deepcopy(cube)]
            command = action(bot, obs)
            if "take" in command:
                take_attempts += 1
                if (kind == "pickup" and item is not None and command["take"] == item["id"]
                        and state["weapons"][1] is None
                        and math.dist(position, item["pos"]) <= config["loot"]["pickup_radius"]):
                    state["weapons"][1] = {"kind": item["kind"], "cooldown": config["loot"]["equip_s"],
                                            "stats": copy.deepcopy(config["weapons"][item["kind"]])}
                    events.append({"type": "took", "id": item["id"], "kind": item["kind"], "slot": 1})
                    item, first_gain = None, now
            if offer is not None and "pick" in command and now <= offer["expires"]:
                selected = offer["choices"][command["pick"]]["id"]
                state["upgrades"].append(selected)
                events.append({"type": "upgrade", "id": selected})
                offer = None
            if (first_gain is not None or vanished_at is not None) and aligned(command["move"], position[:2], goal):
                resumed_at = resumed_at if resumed_at is not None else now
        desired_aim = command.get("aim", state["aim"])
        state["aim"] = [slew(state["aim"][0], desired_aim[0], stats["turn_rate"], dt, True),
                        slew(state["aim"][1], max(stats["min_pitch"], min(stats["max_pitch"], desired_aim[1])),
                             stats["pitch_rate"], dt)]
        if (kind == "crate" and item is not None and command.get("fire", [False])[0]
                and weapon["cooldown"] <= EPS and weapon.get("reload", 0.0) <= EPS):
            if weapon["ammo"] > 0:
                shots += 1
                weapon["ammo"] -= 1
                weapon["cooldown"] = config["weapons"]["gun"]["cooldown"]
                flight = crate_hit(position, state["aim"], item, stats, config, arena)
                if flight is not None:
                    arrivals.append((now+flight, weapon["stats"]["damage"]))
            else:
                weapon["reload"] = config["weapons"]["gun"]["reload"]
        move = command["move"]
        boosting = (command.get("boost", False) and state["energy"] >= stats["boost_drain"]*dt
                    and math.hypot(*move) > EPS)
        speed = stats["boost_speed"] if boosting else stats["walk_speed"]
        desired = [speed*v for v in move]
        delta = [desired[i]-state["vel"][i] for i in range(2)]
        length = math.hypot(*delta)
        fraction = min(1.0, stats["ground_accel"]*dt/length) if length else 0.0
        velocity = [state["vel"][i]+delta[i]*fraction for i in range(2)]
        proposed = tuple(position[i]+velocity[i]*dt for i in range(2))
        clearance = arena.clearance(position[:2], proposed, stats["radius"], stats["height"])[0]
        minimum = min(minimum, clearance)
        if clearance < -EPS or arena.slope(position[:2], proposed) > stats["max_slope_deg"]:
            blocked += 1
            velocity = [0.0, 0.0]
        else:
            travelled += math.dist(position[:2], proposed)
            state["pos"] = [*proposed, arena.height(proposed)]
        state["vel"] = [*velocity, 0.0]
        state["energy"], last_spend = energy_step(state["energy"], state["max_energy"],
                                                  boosting, now, last_spend, dt, stats)
        available_cube = cube if cube is not None else item if kind == "cube" else None
        if available_cube is not None and math.dist(state["pos"], available_cube["pos"]) <= config["cubes"]["pickup_radius"]:
            state["cubes"] += config["crates"]["cubes"] if kind == "crate" else 1
            first_gain = now
            if kind == "cube":
                item = None
            cube = None
            events.append({"type": "pickup", "cubes": state["cubes"]})
            # Deterministic official upgrade subset. No timeout auto-pick: the
            # regression specifically verifies a valid explicit public choice.
            defs = [u for u in config["upgrades"] if u["id"] in ("plating", "armor", "gun_damage")]
            offer = {"id": 1, "choices": [{k: u[k] for k in ("id", "name", "desc")} for u in defs],
                     "expires": now+config["bot"]["offer_timeout_s"]}
        if math.dist(state["pos"][:2], goal) <= 1.0:
            goal_at = (tick+1)/hz
            break
    return {"scenario": ("disappeared_" if disappear else "")+kind+f"_{int(LANES[index][1])}m",
            "equipment": int(state["weapons"][1] is not None), "cubes": state["cubes"],
            "upgrades": list(state["upgrades"]), "shots": shots, "crate_hits": hits,
            "destroyed_at_s": destroyed_at, "first_gain_s": first_gain,
            "vanished_at_s": vanished_at, "resumed_zone_at_s": resumed_at,
            "goal_reached_s": goal_at, "remaining_goal_m": round(math.dist(state["pos"][:2], goal), 3),
            "distance_m": round(travelled, 3), "blocked_frames": blocked,
            "minimum_swept_clearance_m": round(minimum, 5), "take_attempts": take_attempts}


def roof_fixture(arena, stats):
    """Use an actual official prism top, not an imaginary elevated pickup."""
    for index, base, top, ring, holes, bounds in arena.prisms:
        point = tuple(sum(p[i] for p in ring)/len(ring) for i in range(2))
        if not inside(point, ring) or any(inside(point, hole) for hole in holes):
            continue
        if top-arena.height(point) < stats["height"]:
            continue
        for yaw in (0.0, math.pi/2, math.pi, -math.pi/2):
            start = (point[0]+75*math.cos(yaw), point[1]+75*math.sin(yaw))
            goal = (start[0]+30*math.sin(yaw), start[1]-30*math.cos(yaw))
            try:
                if arena.clearance(start, goal, stats["radius"], stats["height"])[0] > 0 and arena.slope(start, goal) < stats["max_slope_deg"]:
                    return start, goal, {"id": 8009, "kind": "lightning", "pos": [*point, top]}, index
            except ValueError:
                continue
    raise ValueError("No official far rooftop fixture found")


def safety(factory, hello, template, arena, kind):
    start, target, goal = fixture(0)
    key = {"pickup": "pickups", "cube": "cubes", "crate": "crates"}[kind]
    resource = ground_item(arena, target, kind)
    if kind == "crate":
        resource["hp"] = hello["config"]["crates"]["hp"]
    match = hello["config"]["match"]
    every = match["decide_every"]
    results = []
    for mode in ("incoming_damage", "close_opponent", "urgent_zone", "disappeared", "stuck", "deadline"):
        bot = factory(copy.deepcopy(hello))
        first = observation(hello, template, arena, every, start, goal)
        first[key] = [copy.deepcopy(resource)]
        initial = action(bot, first)
        committed = not aligned(initial["move"], start, goal)
        positions, commands = [], []
        if mode == "stuck":
            ticks = range(every*2, int(8*match["tick_hz"]), every)
        else:
            # A delayed observation has advanced both clock and position. Its
            # progress distinguishes a wall-clock budget from zero-progress
            # stall handling; a 45 s pursuit must not survive this update.
            ticks = [int(45*match["tick_hz"]) if mode == "deadline" else every*2]
        for tick in ticks:
            position = (start[0]+10.0, start[1]+5.0) if mode == "deadline" else start
            obs = observation(hello, template, arena, tick, position, goal)
            obs[key] = [copy.deepcopy(resource)]
            if mode == "incoming_damage":
                obs["events"] = [{"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 0.0, "by": 5}]
                obs["projectiles"] = [{"id": 281, "kind": "gun", "pos": [start[0]+30, start[1], arena.height(start)+4],
                                       "vel": [-252, 0, 0]}]
            elif mode == "close_opponent":
                obs["mechs"] = [{"id": 5, "kind": "mech", "pos": [start[0]+20, start[1]-10, arena.height(start)],
                                  "vel": [0, 0, 0], "aim": [math.pi, 0], "hp": 1400, "max_hp": 1400,
                                  "weapons": ["gun"]}]
            elif mode == "urgent_zone":
                obs["zone"].update(next_radius=15, shrink_start=-10, shrink_end=obs["t"]+2)
            elif mode == "disappeared":
                # Within near sensors and absent: the old object is known gone.
                position = (target[0]-5, target[1])
                obs["self"]["pos"] = [*position, arena.height(position)]
                obs[key] = []
            commands.append(action(bot, obs))
            positions.append(position)
        fallback = [aligned(cmd["move"], p, goal) for cmd, p in zip(commands, positions)]
        passed = committed and (any(fallback) if mode == "stuck" else all(fallback))
        if mode == "stuck":
            # Sustained fallback, rather than a single transitional command.
            passed &= all(fallback[-int(2*match["tick_hz"]/every):])
        if kind == "crate":
            if mode == "close_opponent":
                # Combat receives the aim even when the previous decision
                # was firing at a stationary resource on the other side.
                expected_yaw = math.atan2(-10, 20)
                passed &= abs((commands[-1]["aim"][0]-expected_yaw+math.pi) % (2*math.pi)-math.pi) < 0.001
            else:
                passed &= not any(commands[-1].get("fire", [False, False]))
        results.append({"scenario": kind+"_"+mode, "initial_committed": committed,
                        "fallback_actions": sum(fallback), "observations": len(commands), "passed": bool(passed)})
    start, goal, pickup, prism = roof_fixture(arena, hello["config"]["mech"])
    obs = observation(hello, template, arena, every, start, goal)
    elevated = copy.deepcopy(pickup)
    if kind != "pickup":
        elevated.pop("kind")
    if kind == "crate":
        elevated["hp"] = hello["config"]["crates"]["hp"]
    obs[key] = [elevated]
    cmd = action(factory(copy.deepcopy(hello)), obs)
    results.append({"scenario": kind+"_far_official_roof", "prism": prism,
                    "height_above_ground_m": round(pickup["pos"][2]-arena.height(pickup["pos"][:2]), 3),
                    "passed": aligned(cmd["move"], start, goal) and "take" not in cmd})
    return results


def load_bot(path, name):
    # A historical --baseline directory must import its own navigation and
    # mobility, even after the candidate has been loaded into this process.
    # Each Bot class retains references to its own imported module objects.
    names = ("mechfight", "navigation", "mobility", "economy", "secondary", "supply")
    saved = {key: sys.modules.pop(key) for key in names if key in sys.modules}
    previous_path = list(sys.path)
    try:
        sys.path.insert(0, str(path.resolve().parent))
        spec = importlib.util.spec_from_file_location(name, path.resolve())
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        DRAWERS[module.Bot] = module.mf.draw
        return module.Bot
    finally:
        for key in names:
            sys.modules.pop(key, None)
        sys.modules.update(saved)
        sys.path[:] = previous_path


def source_hashes(path):
    names = (path.name, "mechfight.py", "navigation.py", "mobility.py", "economy.py", "secondary.py", "supply.py")
    return {name: hashlib.sha256((path.parent/name).read_bytes()).hexdigest()
            for name in names if (path.parent/name).is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", type=Path, required=True)
    parser.add_argument("--bot", type=Path, default=Path(__file__).resolve().parents[1]/"example_bot.py")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--suite", choices=("equipment", "resources", "all"), default="all")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mech-economy-check-") as temporary:
        hello, template, arena = load_kit(args.kit.resolve(), Path(temporary))
        factory = load_bot(args.bot, "economy_bot_under_test")
        baseline = load_bot(args.baseline, "economy_bot_baseline") if args.baseline else None
        print("model=ground-resource-fixture spread=zero jumps=ignored opponents=not-simulated")
        print("bot_sha256="+hashlib.sha256(args.bot.read_bytes()).hexdigest())
        print("candidate_sources="+json.dumps(source_hashes(args.bot), sort_keys=True))
        if args.baseline:
            print("baseline_sha256="+hashlib.sha256(args.baseline.read_bytes()).hexdigest())
            print("baseline_sources="+json.dumps(source_hashes(args.baseline), sort_keys=True))
        print("world_sha256="+arena.world_sha256)
        print("terrain_sha256="+hashlib.sha256(arena.raw).hexdigest())
        stats = hello["config"]["mech"]
        for index in range(len(LANES)):
            start, resource, goal = fixture(index)
            for a, b in ((start, resource), (resource, goal), (start, goal)):
                if (arena.clearance(a, b, stats["radius"], stats["height"])[0] <= 0
                        or arena.slope(a, b) > stats["max_slope_deg"]):
                    raise ValueError(f"Fixture {index} is not a clear ground lane: {a} -> {b}")
        kinds = ["pickup"] if args.suite == "equipment" else ["cube", "crate"] if args.suite == "resources" else ["pickup", "cube", "crate"]
        results, base_results = [], []
        for kind in kinds:
            for index in range(len(LANES)):
                result = run(factory, hello, template, arena, kind, index)
                gain = result["equipment"] == 1 if kind == "pickup" else result["cubes"] >= 1 and len(result["upgrades"]) >= 1
                result["passed"] = bool(gain and result["goal_reached_s"] is not None
                                         and result["resumed_zone_at_s"] is not None and result["blocked_frames"] == 0)
                results.append(result)
                if baseline:
                    base_results.append(run(baseline, hello, template, arena, kind, index))
            if kind != "crate":
                result = run(factory, hello, template, arena, kind, disappear=True)
                result["passed"] = (result["vanished_at_s"] is not None and result["resumed_zone_at_s"] is not None
                                    and result["goal_reached_s"] is not None and result["equipment"] == result["cubes"] == 0)
                results.append(result)
            results.extend(safety(factory, hello, template, arena, kind))
        for result in base_results:
            print("baseline="+json.dumps(result, sort_keys=True))
        for result in results:
            print("candidate="+json.dumps(result, sort_keys=True))
        print("totals="+json.dumps({"candidate_equipment": sum(r.get("equipment", 0) for r in results),
                                    "candidate_cubes": sum(r.get("cubes", 0) for r in results),
                                    "baseline_equipment": sum(r["equipment"] for r in base_results),
                                    "baseline_cubes": sum(r["cubes"] for r in base_results)}, sort_keys=True))
        passed = all(r["passed"] for r in results)
        print("PASS: public economy scenarios" if passed else "FAIL: public economy scenarios")
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
