"""Independent E01 ground-route regression, using only Bot's public act interface.

Run: python tests/check_routes.py --kit /path/to/mechfight-kit.tar.gz
The kit may also be an extracted directory. --bot accepts a different bot file.
This is a deterministic horizontal direction/geometry check, NOT the server:
ground walk speed is fixed, jump/boost and acceleration are deliberately ignored.
Commands are held between decisions; each 20 Hz frame has swept-cylinder checks.
Only prisms are solid (§11); trees and scenery do not block server movement.
"""

import argparse
import copy
import gzip
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import struct
import sys
import tarfile
import tempfile


EPS = 1e-8
ROUTES = {"blocked": ((-130.0, 64.0), (-70.0, 64.0)),
          "direct": ((-130.0, 40.0), (-70.0, 40.0)),
          # Legal start 0.272 m from prism 113: must escape a planning margin.
          "near_wall": ((-121.6, 66.0), (-70.0, 64.0))}


def vertices(flat):
    return list(zip(flat[::2], flat[1::2]))


def edges(ring):
    return zip(ring, ring[1:] + ring[:1])


def point_segment(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    size = dx * dx + dy * dy
    t = max(0.0, min(1.0, ((p[0]-a[0])*dx + (p[1]-a[1])*dy) / size)) if size else 0
    return math.hypot(p[0]-a[0]-t*dx, p[1]-a[1]-t*dy)


def cross(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])


def segment_distance(a, b, c, d):
    # Opposite signs alone incorrectly identify disjoint collinear segments.
    values = (cross(a, b, c), cross(a, b, d), cross(c, d, a), cross(c, d, b))
    if values[0] * values[1] < 0 and values[2] * values[3] < 0:
        return 0.0
    return min(point_segment(a, c, d), point_segment(b, c, d),
               point_segment(c, a, b), point_segment(d, a, b))


def inside(p, ring):
    result = False
    for a, b in edges(ring):
        if point_segment(p, a, b) <= EPS:
            return True
        if (a[1] > p[1]) != (b[1] > p[1]):
            at_x = a[0] + (p[1]-a[1])*(b[0]-a[0])/(b[1]-a[1])
            if p[0] < at_x:
                result = not result
    return result


class Arena:
    def __init__(self, map_dir):
        world_bytes = (map_dir / "v2/world.json").read_bytes()
        self.world_sha256 = hashlib.sha256(world_bytes).hexdigest()
        self.world = json.loads(world_bytes)
        self.raw = (map_dir / "v2/terrain.i16").read_bytes()
        self.grid = self.world["terrain"]
        self.prisms = []
        for index, prism in enumerate(self.world["prisms"]):
            ring = vertices(prism["ring"])
            holes = [vertices(hole) for hole in prism["holes"]]
            bounds = (min(p[0] for p in ring), min(p[1] for p in ring),
                      max(p[0] for p in ring), max(p[1] for p in ring))
            self.prisms.append((index, prism["base"], prism["top"], ring, holes, bounds))

    def height(self, p):
        g = self.grid
        x, y = (p[0]-g["minX"])/g["step"], (p[1]-g["minY"])/g["step"]
        i, j = math.floor(x), math.floor(y)
        if not (0 <= i < g["nx"]-1 and 0 <= j < g["ny"]-1):
            raise ValueError("Route left the terrain grid")
        u, v = x-i, y-j
        def z(dx, dy):
            return struct.unpack_from("<h", self.raw, 2*((j+dy)*g["nx"]+i+dx))[0]*g["scale"]
        return (1-u)*(1-v)*z(0, 0)+u*(1-v)*z(1, 0)+(1-u)*v*z(0, 1)+u*v*z(1, 1)

    def clearance(self, a, b, radius, height):
        low, high = min(self.height(a), self.height(b)), max(self.height(a), self.height(b))+height
        minimum, nearest = math.inf, None
        for index, base, top, ring, holes, bounds in self.prisms:
            if top <= low or base >= high:
                continue
            dx = max(0.0, bounds[0]-max(a[0], b[0]), min(a[0], b[0])-bounds[2])
            dy = max(0.0, bounds[1]-max(a[1], b[1]), min(a[1], b[1])-bounds[3])
            if math.hypot(dx, dy)-radius >= minimum:
                continue
            def solid(p):
                return inside(p, ring) and not any(inside(p, hole) for hole in holes)
            if solid(a) or solid(b):
                distance = 0.0
            else:
                distance = min(segment_distance(a, b, c, d)
                               for boundary in [ring] + holes for c, d in edges(boundary))
            if distance-radius < minimum:
                minimum, nearest = distance-radius, index
        return minimum, nearest

    def slope(self, a, b):
        # Sample a swept frame at <= 0.10 m, including bilinear terrain changes.
        length = math.dist(a, b)
        if length < EPS:
            return 0.0
        n = max(1, math.ceil(length / 0.10))
        heights = [self.height((a[0]+(b[0]-a[0])*i/n, a[1]+(b[1]-a[1])*i/n))
                   for i in range(n+1)]
        return max(math.degrees(math.atan(abs(y-x)/(length/n))) for x, y in zip(heights, heights[1:]))


def direct_move(position, goal):
    distance = math.dist(position, goal)
    return [(goal[i]-position[i])/distance for i in range(2)] if distance else [0.0, 0.0]


def observation(template, config, arena, position, velocity, goal, tick):
    obs = copy.deepcopy(template)
    obs.update(tick=tick, t=tick/config["match"]["tick_hz"], offer=None)
    for key in ("mechs", "projectiles", "crates", "cubes", "pickups", "sounds", "beams", "events", "drops"):
        obs[key] = []
    obs["self"].update(pos=[*position, arena.height(position)], vel=[*velocity, 0.0],
                       grounded=True, in_zone=True, stats=copy.deepcopy(config["mech"]))
    obs["zone"].update(next_center=list(goal))
    return obs


def run_route(factory, hello, template, arena, name, seconds, tolerance):
    start, goal = ROUTES[name]
    bot = factory(copy.deepcopy(hello)) if factory else None
    config, position, velocity = hello["config"], start, [0.0, 0.0]
    hz, every = config["match"]["tick_hz"], int(config["match"]["decide_every"])
    radius, height = config["mech"]["radius"], config["mech"]["height"]
    speed = config["mech"]["walk_speed"]
    move, travelled, blocked, max_error = [0.0, 0.0], 0.0, 0, 0.0
    minimum, max_slope, first_block = math.inf, 0.0, None
    reached = False
    tick = 0
    for tick in range(math.ceil(seconds * hz)):
        if tick % every == 0:
            obs = observation(template, config, arena, position, velocity, goal, tick)
            action = bot.act(obs) if bot else {"move": direct_move(position, goal)}
            move = action.get("move", [0.0, 0.0])
            if len(move) != 2 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in move):
                raise ValueError(f"Invalid move at tick {tick}: {move}")
            if math.hypot(*move) > 1.0 + EPS:
                raise ValueError(f"Move exceeds unit length at tick {tick}: {move}")
            if name == "direct":
                max_error = max(max_error, math.dist(move, direct_move(position, goal)))
        proposed = tuple(position[i]+speed*move[i]/hz for i in range(2))
        clearance, prism = arena.clearance(position, proposed, radius, height)
        minimum = min(minimum, clearance)
        slope = arena.slope(position, proposed)
        max_slope = max(max_slope, slope)
        if clearance < -EPS or slope > config["mech"]["max_slope_deg"] + EPS:
            blocked += 1
            if first_block is None:
                first_block = {"tick": tick, "prism": prism, "at": [round(v, 3) for v in proposed]}
            velocity = [0.0, 0.0]
        else:
            travelled += math.dist(position, proposed)
            velocity = [speed*v for v in move]
            position = proposed
        if math.dist(position, goal) <= tolerance:
            reached = True
            break
    return {"route": name, "reached": reached, "frames": tick+1,
            "elapsed_s": round((tick+1)/hz, 3), "distance_m": round(travelled, 3),
            "remaining_m": round(math.dist(position, goal), 3),
            "progress_m": round(math.dist(start, goal)-math.dist(position, goal), 3),
            "min_swept_clearance_m": round(minimum, 6), "blocked_frames": blocked,
            "max_terrain_slope_deg": round(max_slope, 3),
            "max_direct_move_error": max_error, "first_block": first_block,
            "passed": reached and blocked == 0 and (name != "direct" or max_error <= EPS)}


def load_kit(source, temporary):
    wanted = ["arena/v2/world.json", "arena/v2/terrain.i16", "check/stream.jsonl.gz"]
    if source.is_file():
        with tarfile.open(source) as archive:
            for relative in wanted:
                matches = [member for member in archive.getmembers()
                           if member.name.endswith("/"+relative) and member.isfile()]
                if len(matches) != 1:
                    raise ValueError(f"Expected exactly one kit member: {relative}")
                target = temporary / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(matches[0]).read())
        root = temporary
        print("kit_sha256="+hashlib.sha256(source.read_bytes()).hexdigest())
    else:
        root = source
        if not (root/"arena").is_dir() and (root/"mechfight-kit").is_dir():
            root /= "mechfight-kit"
    with gzip.open(root/"check/stream.jsonl.gz", "rt", encoding="utf-8") as stream:
        hello, template = json.loads(next(stream)), json.loads(next(stream))
    hello["map"]["dir"] = str((root/"arena").resolve())
    return hello, template, Arena(root/"arena")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", required=True, type=Path)
    parser.add_argument("--bot", type=Path, default=Path(__file__).resolve().parents[1]/"example_bot.py")
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--tolerance", type=float, default=1.0)
    args = parser.parse_args()
    if args.seconds <= 0 or not 0 < args.tolerance < 2.5:
        parser.error("seconds must be positive and tolerance must be between 0 and 2.5 m")
    with tempfile.TemporaryDirectory(prefix="mech-route-check-") as directory:
        hello, template, arena = load_kit(args.kit.resolve(), Path(directory))
        sys.path.insert(0, str(args.bot.resolve().parent))
        spec = importlib.util.spec_from_file_location("route_bot_under_test", args.bot.resolve())
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        radius, height = hello["config"]["mech"]["radius"], hello["config"]["mech"]["height"]
        print(f"model=horizontal-ground-directions radius_m={radius} height_m={height} "
              f"walk_speed_m_s={hello['config']['mech']['walk_speed']} "
              f"tick_hz={hello['config']['match']['tick_hz']} "
              f"bot_sha256={hashlib.sha256(args.bot.read_bytes()).hexdigest()}")
        print("world_sha256="+arena.world_sha256)
        print("terrain_sha256="+hashlib.sha256(arena.raw).hexdigest())
        results = []
        for name, (start, goal) in ROUTES.items():
            control = run_route(None, hello, template, arena, name, args.seconds, args.tolerance)
            result = run_route(module.Bot, hello, template, arena, name, args.seconds, args.tolerance)
            if name != "direct":
                geometry_ok = arena.clearance(start, goal, 0.0, height)[0] <= EPS
                control_ok = not control["reached"] and control["blocked_frames"] > 0
                result["passed"] &= geometry_ok and control_ok
                if name == "near_wall":
                    initial_clearance = arena.clearance(start, start, radius, height)[0]
                    result["initial_clearance_m"] = round(initial_clearance, 6)
                    result["passed"] &= 0.0 < initial_clearance < 0.5
            else:
                result["passed"] &= control["passed"] and abs(result["distance_m"]-control["distance_m"]) <= EPS
            print("control="+json.dumps(control, sort_keys=True))
            print("bot="+json.dumps(result, sort_keys=True))
            results.append(result)
        passed = all(result["passed"] for result in results)
        print("PASS: E01 direction regression" if passed else "FAIL: E01 direction regression")
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
