"""Opt-in exact horizontal wall veto for grounded mechs in the local referee.

The pinned Episode.move still integrates acceleration, two axis moves, vertical
motion, energy and damage. Only its actual grounded-mech walk queries receive
an additional prism/cylinder check. House route selection, spawns, NPCs, shots
and airborne movement keep the pinned behavior. No actor is repositioned.

This is not the official physics implementation. Low ledges up to step_up are
allowed when a cylinder raised onto them has headroom; the pinned raster still
chooses the standing surface. Roof walking and departure are checked at the
current feet height. Falling/landing, eave support, slopes, mech separation and
recovery from an already embedded cylinder remain outside this correction.
"""
import json
import math
from pathlib import Path

if __package__:
    from .spawn_arena import SpawnGeometry, _distance2
else:
    from spawn_arena import SpawnGeometry, _distance2


def _segment_distance2(a, b, c, d):
    """Squared distance between two closed horizontal line segments."""
    def cross(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    ab_c, ab_d = cross(a, b, c), cross(a, b, d)
    cd_a, cd_b = cross(c, d, a), cross(c, d, b)
    if ab_c * ab_d < 0 and cd_a * cd_b < 0:
        return 0.0
    return min(_distance2(*a, c, d), _distance2(*b, c, d),
               _distance2(*c, a, b), _distance2(*d, a, b))


class MovementGeometry(SpawnGeometry):
    """Exact prism footprints with a uniform-grid broad phase, holes included."""

    def __init__(self, world, cell_size=16.0):
        super().__init__(world)
        self.cell_size = cell_size
        self.edge_cell_size = 4.0
        self.cells = {}
        self._details = {}
        for ident, prism in enumerate(self.prisms):
            left, bottom, right, top = prism[0]
            for column in range(math.floor(left / cell_size), math.floor(right / cell_size) + 1):
                for row in range(math.floor(bottom / cell_size), math.floor(top / cell_size) + 1):
                    self.cells.setdefault((column, row), []).append(ident)
            # Large stadium footprints have hundreds of small edges. A nearby
            # cylinder must not scan all of them after the prism broad phase.
            edge_cells, ray_rows, edge_boxes = {}, {}, []
            size = self.edge_cell_size
            edge_id = 0
            for ring_id, ring in enumerate([prism[3], *prism[4]]):
                for a, b in zip(ring, ring[1:] + ring[:1]):
                    left, right = min(a[0], b[0]), max(a[0], b[0])
                    bottom, top = min(a[1], b[1]), max(a[1], b[1])
                    edge_boxes.append((left, bottom, right, top))
                    for row in range(math.floor(bottom / size), math.floor(top / size) + 1):
                        if a[1] != b[1]:
                            ray_rows.setdefault(row, []).append((a, b, ring_id))
                        for column in range(math.floor(left / size), math.floor(right / size) + 1):
                            edge_cells.setdefault((column, row), []).append(edge_id)
                    edge_id += 1
            self._details[id(prism)] = ray_rows, edge_cells, edge_boxes

    def candidates(self, start, end, radius):
        """Return only prisms near the swept footprint, independent of map size."""
        size = self.cell_size
        left, right = min(start[0], end[0]) - radius, max(start[0], end[0]) + radius
        bottom, top = min(start[1], end[1]) - radius, max(start[1], end[1]) + radius
        ids = set()
        for column in range(math.floor(left / size), math.floor(right / size) + 1):
            for row in range(math.floor(bottom / size), math.floor(top / size) + 1):
                ids.update(self.cells.get((column, row), ()))
        return [self.prisms[i] for i in ids
                if self.prisms[i][0][0] <= right and self.prisms[i][0][2] >= left
                and self.prisms[i][0][1] <= top and self.prisms[i][0][3] >= bottom]

    def _horizontal_overlap(self, prism, start, end, radius):
        edges = prism[5]
        ray_rows, edge_cells, edge_boxes = self._details[id(prism)]
        size = self.edge_cell_size
        for point in (start, end):
            x, y = point[:2]
            inside_rings = 0
            for (ax, ay), (bx, by), ring_id in ray_rows.get(math.floor(y / size), ()):
                if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
                    inside_rings ^= 1 << ring_id
            if inside_rings & 1 and not inside_rings >> 1:
                return True
        left, right = min(start[0], end[0]) - radius, max(start[0], end[0]) + radius
        bottom, top = min(start[1], end[1]) - radius, max(start[1], end[1]) + radius
        edge_ids = set()
        for row in range(math.floor(bottom / size), math.floor(top / size) + 1):
            for column in range(math.floor(left / size), math.floor(right / size) + 1):
                edge_ids.update(edge_cells.get((column, row), ()))
        near_edges = (edges[i] for i in edge_ids
                      if edge_boxes[i][0] <= right and edge_boxes[i][2] >= left
                      and edge_boxes[i][1] <= top and edge_boxes[i][3] >= bottom)
        limit = radius * radius - 1e-9
        if start[:2] == end[:2]:
            return any(_distance2(*end[:2], a, b) < limit for a, b in near_edges)
        return any(_segment_distance2(start[:2], end[:2], a, b) < limit for a, b in near_edges)

    def _in_bounds(self, point, radius):
        x, y = point[:2]
        b = self.bounds
        return (b["minX"] + radius <= x <= b["maxX"] - radius
                and b["minY"] + radius <= y <= b["maxY"] - radius)

    def legal(self, pos, radius, height):
        """Check an actual stationary cylinder, with the spawn check's tolerances."""
        if not self._in_bounds(pos, radius):
            return False
        z = pos[2]
        for prism in self.candidates(pos, pos, radius):
            if (prism[1] < z + height - 1e-6 and prism[2] > z + 1e-6
                    and self._horizontal_overlap(prism, pos, pos, radius)):
                return False
        return True

    def can_move(self, start, end, radius, height, step_up):
        """Check the full horizontal axis sweep, permitting reachable low ledges."""
        if not self._in_bounds(end, radius):
            return False
        candidates = self.candidates(start, end, radius)
        feet = start[2]
        overlaps = []
        for prism in candidates:
            _, base, top, *_ = prism
            if top <= feet + 1e-6 or base >= feet + height + step_up - 1e-6:
                continue
            if self._horizontal_overlap(prism, start, end, radius):
                overlaps.append(prism)
        # A legal step must clear the head as well as the feet. Do not chain
        # several individually small rises into one rise larger than step_up.
        raised_feet = max([feet] + [p[2] for p in overlaps if p[2] <= feet + step_up + 1e-6])
        return not any(p[1] < raised_feet + height - 1e-6 and p[2] > raised_feet + 1e-6
                       for p in overlaps)


class _MovementLayout:
    """Episode-local proxy; never replace a method on the shared campus layout."""

    def __init__(self, original, episode):
        self.original, self.episode = original, episode
        self.actor = None

    def __getattr__(self, name):
        return getattr(self.original, name)

    def walk(self, x, y, feet, radius):
        if not self.original.walk(x, y, feet, radius):
            return False
        actor = self.actor
        if actor is None or not actor.grounded or actor.kind != "mech":
            return True
        if x == actor.pos[0] and y == actor.pos[1]:
            return True
        ep = self.episode
        ep.movement_checks += 1
        allowed = ep.movement_geometry.can_move(actor.pos, (x, y, feet), radius,
                                                actor.height, actor.stats["step_up"])
        ep.movement_rejections += not allowed
        return allowed


def with_movement_geometry(base_episode):
    """Wrap pinned SquadEpisode without changing its RNG or opponent commands."""
    geometry_cache = {}

    class MovementEpisode(base_episode):
        def __init__(self, hello, layout, seed):
            world_path = (Path(layout.directory) / "v2" / "world.json").resolve()
            if world_path not in geometry_cache:
                geometry_cache[world_path] = MovementGeometry(json.loads(world_path.read_text(encoding="utf-8")))
            self.movement_geometry = geometry_cache[world_path]
            self.movement_checks = self.movement_rejections = 0
            super().__init__(hello, layout, seed)
            self._movement_layout = _MovementLayout(self.layout, self)

        def move(self, actor, action):
            if actor.kind != "mech" or not actor.grounded or action.get("jump"):
                return super().move(actor, action)
            original = self.layout
            proxy = self._movement_layout
            previous_actor = proxy.actor
            proxy.actor = actor
            self.layout = proxy
            try:
                return super().move(actor, action)
            finally:
                self.layout = original
                proxy.actor = previous_actor

        def run(self, bot, trace):
            result = super().run(bot, trace)
            result.update(movement_checks=self.movement_checks,
                          movement_rejections=self.movement_rejections)
            return result

    return MovementEpisode
