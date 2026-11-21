"""Opt-in exact cylinder checks for the local referee's initial mech spawns.

This does not replace referee movement or reproduce official spawning. Only
the initial ground points sampled for the sixteen mechs are filtered. NPCs,
crates, dropped items and roof sampling retain the pinned referee behavior.
"""
import json
from pathlib import Path


def _inside(x, y, ring):
    inside = False
    for (ax, ay), (bx, by) in zip(ring, ring[1:] + ring[:1]):
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
    return inside


def _distance2(x, y, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    size = dx * dx + dy * dy
    t = max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / size)) if size else 0.0
    return (x - a[0] - t * dx) ** 2 + (y - a[1] - t * dy) ** 2


class SpawnGeometry:
    """Static, exact horizontal footprint and vertical prism intervals."""

    def __init__(self, world):
        self.bounds = world["bounds"]
        self.prisms = []
        for item in world["prisms"]:
            outer = list(zip(item["ring"][::2], item["ring"][1::2]))
            if not outer:
                continue
            holes = [list(zip(flat[::2], flat[1::2])) for flat in item.get("holes", [])]
            rings = [outer, *holes]
            edges = [(a, b) for ring in rings for a, b in zip(ring, ring[1:] + ring[:1])]
            box = (min(x for x, _ in outer), min(y for _, y in outer),
                   max(x for x, _ in outer), max(y for _, y in outer))
            self.prisms.append((box, float(item["base"]), float(item["top"]), outer, holes, edges))

    def legal(self, pos, radius, height):
        x, y, z = pos
        bounds = self.bounds
        if (x - radius < bounds["minX"] or x + radius > bounds["maxX"]
                or y - radius < bounds["minY"] or y + radius > bounds["maxY"]):
            return False
        for box, base, top, outer, holes, edges in self.prisms:
            if (top <= z + 1e-6 or base >= z + height - 1e-6
                    or x + radius < box[0] or x - radius > box[2]
                    or y + radius < box[1] or y - radius > box[3]):
                continue
            if _inside(x, y, outer) and not any(_inside(x, y, hole) for hole in holes):
                return False
            if any(_distance2(x, y, a, b) < radius * radius - 1e-9 for a, b in edges):
                return False
        return True


def with_legal_spawns(base_episode):
    """Wrap pinned SquadEpisode; preserve its RNG unless a spawn is rejected."""
    geometry_cache = {}

    class LegalSpawnEpisode(base_episode):
        def __init__(self, hello, layout, seed):
            world_path = Path(layout.directory) / "v2" / "world.json"
            if world_path not in geometry_cache:
                geometry_cache[world_path] = SpawnGeometry(json.loads(world_path.read_text(encoding="utf-8")))
            self.spawn_geometry = geometry_cache[world_path]
            self.legal_spawn_checks = self.legal_spawn_rejections = 0
            super().__init__(hello, layout, seed)

        def open_point(self, radius, roof=False):
            # In pinned SquadEpisode, actors stays empty throughout the spawn
            # separation/restart loop. All sixteen actors exist before any
            # NPC, crate, floor item or roof point is sampled.
            if self.actors or roof:
                return super().open_point(radius, roof)
            stats = self.cfg["mech"]
            for _ in range(2000):
                point = super().open_point(radius, roof)
                self.legal_spawn_checks += 1
                if self.spawn_geometry.legal(point, stats["radius"], stats["height"]):
                    return point
                self.legal_spawn_rejections += 1
            raise ValueError("No exact legal ground spawn found by the local referee")

        def run(self, bot, trace):
            result = super().run(bot, trace)
            result.update(legal_spawn_checks=self.legal_spawn_checks,
                          legal_spawn_rejections=self.legal_spawn_rejections)
            return result

    return LegalSpawnEpisode
