"""Independent E14 recovery checks on the official map and public Bot.act.

python -B tests/check_navigation_recovery.py --kit KIT --baseline E10/example_bot.py

Seed 9121's legal start reproduces an E10 stop after 5.85 m in this model.
The distant-circle route is diagnostic: a closed solid barrier around that
start prevents the originally proposed 20 m progress on the ground. The
barrier is checked below, independently of the bot. Acceptance uses a nearby
target whose whole straight approach has positive swept-cylinder clearance.
Seed 9114's literal surrogate spawn overlaps prism 210 by 1.473 m, so it is
reported as an invalid spawn, not used to demand a collision-free escape.
Its nearby legal control is offset (-0.5, +1.5) m from that sampled spawn.

This fixture models horizontal walking only: fixed walk speed, 20 Hz swept
cylinder collision and terrain slopes, with 10 Hz public observations. It
deliberately omits jump, boost, acceleration, enemies and resource decisions.
It is a navigation regression, not official physics or a placement estimate.
Only the navigation module's planning clock is deterministic; no route/search
internals are read. Every input and action passes the kit's schema validator.
"""
import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import tarfile
import tempfile

from check_economy import DRAWERS, load_bot, source_hashes
from check_routes import EPS, direct_move, load_kit, observation


RAW_9114 = (-34.03260609867618, 362.924780187449)
SCENES = {
    "seed_9121_distant_diagnostic": ((116.43235750140968, 301.57810787628165),
                                    (-55.00015709548133, -47.068433318972836)),
    "seed_9121_reachable_control": ((116.43235750140968, 301.57810787628165),
                                   (119.43235750140968, 309.57810787628165)),
    "seed_9114_legal_control": ((RAW_9114[0] - .5, RAW_9114[1] + 1.5),
                                (2.3962555145854054, 28.98697651336174)),
    "direct_control": ((-130.0, 40.0), (-70.0, 40.0)),
}


class PlanningClock:
    def __init__(self):
        self.value = 0.0

    def perf_counter(self):
        self.value += .000001
        return self.value


def official_schema(source, temporary):
    """Use the organizer's validator and schema, including when KIT is a tar."""
    if source.is_file():
        root = temporary / "schema"
        with tarfile.open(source) as archive:
            for relative in ("check/check.py", "mechfight.schema.json"):
                matches = [member for member in archive.getmembers()
                           if member.isfile() and member.name.endswith("/" + relative)]
                if len(matches) != 1:
                    raise ValueError(f"Expected exactly one kit member: {relative}")
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(matches[0]).read())
    else:
        root = source if (source / "check").is_dir() else source / "mechfight-kit"
    spec = importlib.util.spec_from_file_location("recovery_official_check", root / "check/check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = root / "mechfight.schema.json"
    return module.Schema(path), hashlib.sha256(path.read_bytes()).hexdigest()


def enclosure_certificate(arena, config):
    """Certify a closed forbidden boundary around 9121's ground component.

    Distance to a fixed solid is 1-Lipschitz. If samples spaced <=0.1 m
    overlap by >0.05 m, the whole boundary overlaps. All three solid prisms
    intersect the mech vertically throughout the rectangle, using extrema of
    the covering terrain vertices (bilinear heights lie between them).
    """
    left, bottom, right, top = 110.0, 291.0, 126.0, 315.5
    grid = arena.grid
    xs = range(math.floor((left - grid["minX"]) / grid["step"]),
               math.ceil((right - grid["minX"]) / grid["step"]) + 1)
    ys = range(math.floor((bottom - grid["minY"]) / grid["step"]),
               math.ceil((top - grid["minY"]) / grid["step"]) + 1)
    heights = [arena.height((grid["minX"] + i * grid["step"],
                             grid["minY"] + j * grid["step"])) for i in xs for j in ys]
    stats = config["mech"]
    solids = [prism for prism in arena.prisms if prism[0] in (188, 192, 196)]
    vertical_overlap = len(solids) == 3 and all(
        prism[1] < min(heights) + stats["height"] and prism[2] > max(heights)
        for prism in solids)
    original = arena.prisms
    arena.prisms = solids
    worst = -math.inf
    try:
        corners = [(left, bottom), (right, bottom), (right, top), (left, top), (left, bottom)]
        for a, b in zip(corners, corners[1:]):
            count = math.ceil(math.dist(a, b) / .1)
            for index in range(count + 1):
                point = tuple(x + (y - x) * index / count for x, y in zip(a, b))
                worst = max(worst, arena.clearance(point, point,
                                                  stats["radius"], stats["height"])[0])
    finally:
        arena.prisms = original
    start, goal = SCENES["seed_9121_distant_diagnostic"]
    closest = (max(left, min(right, goal[0])), max(bottom, min(top, goal[1])))
    bound = math.dist(start, goal) - math.dist(closest, goal)
    enclosed = vertical_overlap and worst + .05 < 0 and left < start[0] < right and bottom < start[1] < top
    return {"scenario": "seed_9121_ground_barrier", "enclosed": enclosed,
            "rectangle": [left, bottom, right, top], "prisms": [p[0] for p in solids],
            "max_sample_clearance_m": worst, "boundary_overlap_lower_bound_m": -worst - .05,
            "max_progress_inside_rectangle_m": bound, "terrain_height_range": [min(heights), max(heights)]}


def run_scene(factory, hello, template, arena, schema, name):
    start, goal = SCENES[name]
    cfg = hello["config"]
    stats = cfg["mech"]
    radius, height, speed = stats["radius"], stats["height"], stats["walk_speed"]
    hz, stride = cfg["match"]["tick_hz"], int(cfg["match"]["decide_every"])
    initial_clearance, nearest = arena.clearance(start, start, radius, height)
    if initial_clearance < -EPS:
        raise ValueError(f"Acceptance start {name} overlaps prism {nearest}")
    reachable_control = name == "seed_9121_reachable_control"
    if reachable_control:
        approach_clearance = arena.clearance(start, goal, radius, height)[0]
        approach_slope = arena.slope(start, goal)
        if approach_clearance <= 0 or approach_slope > stats["max_slope_deg"]:
            raise ValueError("Reachable control no longer has a safe straight ground approach")
    bot = factory(hello)
    position, velocity, move = start, [0.0, 0.0], [0.0, 0.0]
    travelled = max_idle = idle = max_direct_error = 0.0
    minimum, max_slope = initial_clearance, 0.0
    blocked = 0
    reached = False
    for tick in range(round(15 * hz)):
        if tick % stride == 0:
            obs = observation(template, cfg, arena, position, velocity, goal, tick)
            errors = schema.errors(obs, "Observation")
            if errors:
                raise ValueError(f"Invalid {name} observation: {errors}")
            DRAWERS[factory].clear()
            command = bot.act(obs)
            errors = schema.errors(command, "Action")
            if errors or command.get("tick") != tick:
                raise ValueError(f"Invalid {name} action: {errors}, tick={command.get('tick')}")
            move = command.get("move", [0.0, 0.0])
            if not all(math.isfinite(v) for v in move) or math.hypot(*move) > 1 + EPS:
                raise ValueError(f"Nonfinite/nonunit movement: {move}")
            if name == "direct_control":
                max_direct_error = max(max_direct_error, math.dist(move, direct_move(position, goal)))
        idle = idle + 1 / hz if math.hypot(*move) < 1e-6 else 0.0
        max_idle = max(max_idle, idle)
        proposed = tuple(position[i] + speed * move[i] / hz for i in range(2))
        clearance, _ = arena.clearance(position, proposed, radius, height)
        slope = arena.slope(position, proposed)
        minimum, max_slope = min(minimum, clearance), max(max_slope, slope)
        if clearance < -EPS or slope > stats["max_slope_deg"] + EPS:
            blocked += 1
            velocity = [0.0, 0.0]
        else:
            travelled += math.dist(position, proposed)
            velocity, position = [speed * v for v in move], proposed
        if math.dist(position, goal) <= 1.0:
            reached = True
            break
    progress = math.dist(start, goal) - math.dist(position, goal)
    acceptance = name != "seed_9121_distant_diagnostic"
    passed = blocked == 0 and max_idle <= 3.0 + EPS
    if name == "direct_control":
        passed &= reached and max_direct_error <= EPS
    elif reachable_control:
        passed &= reached
    else:
        passed &= travelled >= 50.0 and progress >= 20.0
    return {"scenario": name, "acceptance": acceptance,
            "passed": passed if acceptance else None, "elapsed_s": (tick + 1) / hz,
            "distance_m": travelled, "progress_m": progress, "final_pos": position,
            "max_zero_move_s": max_idle, "blocked_frames": blocked,
            "initial_clearance_m": initial_clearance, "min_clearance_m": minimum,
            "max_slope_deg": max_slope, "max_direct_error": max_direct_error,
            "reached": reached}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", required=True, type=Path)
    parser.add_argument("--bot", type=Path, default=Path(__file__).resolve().parents[1] / "example_bot.py")
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mech-recovery-check-") as directory:
        temporary = Path(directory)
        hello, template, arena = load_kit(args.kit.resolve(), temporary)
        schema, schema_hash = official_schema(args.kit.resolve(), temporary)
        print("metadata=" + json.dumps({"world_sha256": arena.world_sha256,
              "terrain_sha256": hashlib.sha256(arena.raw).hexdigest(),
              "schema_sha256": schema_hash, "planning_clock": "1us per perf_counter call",
              "scope": "horizontal navigation only; 15 s; no match score"}))
        clearance, prism = arena.clearance(RAW_9114, RAW_9114,
                                           hello["config"]["mech"]["radius"],
                                           hello["config"]["mech"]["height"])
        print("excluded_spawn=" + json.dumps({"seed": 9114, "pos": RAW_9114,
              "clearance_m": clearance, "prism": prism,
              "reason": "surrogate spawn overlaps official solid"}))
        certificate = enclosure_certificate(arena, hello["config"])
        print("geometry=" + json.dumps(certificate, sort_keys=True))
        passed = certificate["enclosed"] and certificate["max_progress_inside_rectangle_m"] < 20
        for label, path in (("baseline", args.baseline), ("candidate", args.bot)):
            if path is None:
                continue
            factory = load_bot(path.resolve(), "recovery_" + label)
            nav_class = factory.__init__.__globals__["Navigator"]
            nav_class.direction.__globals__["time"] = PlanningClock()
            print(label + "_source=" + json.dumps(source_hashes(path.resolve()), sort_keys=True))
            for name in SCENES:
                result = run_scene(factory, hello, template, arena, schema, name)
                print(label + "=" + json.dumps(result, sort_keys=True))
                if label == "candidate" and result["acceptance"]:
                    passed &= result["passed"]
        print("PASS: navigation recovery" if passed else "FAIL: navigation recovery")
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
