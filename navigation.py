"""Ground navigator for the official static arena.

Only movement direction is chosen here.  Search and collision results survive
between decisions; no files are written and no third-party packages are needed.
"""
from __future__ import annotations

from array import array
import heapq
import json
import math
from pathlib import Path
import sys
import time


def _unit(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    return (dx / length, dy / length) if length else (0.0, 0.0)


def _ring(flat):
    points = list(zip(flat[::2], flat[1::2]))
    return [(points[i - 1][0], points[i - 1][1], x, y)
            for i, (x, y) in enumerate(points)]


def _inside(x, y, edges):
    inside = False
    for ax, ay, bx, by in edges:
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
    return inside


def _distance2(x, y, edge):
    ax, ay, bx, by = edge
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2)) if length2 else 0.0
    return (x - ax - t * dx) ** 2 + (y - ay - t * dy) ** 2


class Navigator:
    """Radius-aware 3 m ground grid, with bounded incremental A* and shortcuts.

    Invalid/missing maps and elevated positions retain the example's direct
    heading.  The ground planner does not attempt to invent jumps or roof routes.
    """

    _CELL = 3.0
    _BIN = 32.0
    _SLICE = 0.006
    _MAX_EXPAND = 120
    _MAX_VISITED = 16000

    def __init__(self):
        self.available = False
        self.route = []
        self._search = None
        self._goal = None
        self._target = None
        self._radius = None
        self._exhausted = False
        self._halted_at = None
        self._walk = {}
        self._heights = {}
        self._edges = {}
        self._bins = {}

    @classmethod
    def from_hello(cls, hello):
        nav = cls()
        try:
            root = Path(hello["map"]["dir"])
            files = hello["map"].get("files", [])
            world_name = next((name for name in files if name.endswith("world.json")), "v2/world.json")
            world_path = root / world_name
            world = json.loads(world_path.read_text(encoding="utf-8"))
            terrain = world["terrain"]
            data = array("h")
            data.frombytes((world_path.parent / terrain["file"]).read_bytes())
            if sys.byteorder != "little":
                data.byteswap()
            nav.nx, nav.ny = int(terrain["nx"]), int(terrain["ny"])
            if len(data) != nav.nx * nav.ny or nav.nx < 2 or nav.ny < 2:
                return nav
            nav._data = data
            nav.tx, nav.ty = float(terrain["minX"]), float(terrain["minY"])
            nav.ts, nav.scale = float(terrain["step"]), float(terrain["scale"])
            bounds = world["bounds"]
            nav.bounds = (bounds["minX"], bounds["minY"], bounds["maxX"], bounds["maxY"])
            mech = hello["config"]["mech"]
            nav.base_radius = float(mech["radius"])
            nav.height_ratio = float(mech["height"]) / nav.base_radius
            nav.step_up = float(mech["step_up"])
            nav.slope = math.tan(math.radians(mech["max_slope_deg"]))
            nav.air_height = mech["jump_speed"] ** 2 / (2 * mech["gravity"]) + 1.0
            nav.prisms = []
            for item in world["prisms"]:
                outer = _ring(item["ring"])
                if not outer:
                    continue
                holes = [_ring(hole) for hole in item.get("holes", [])]
                xs, ys = item["ring"][::2], item["ring"][1::2]
                box = (min(xs), min(ys), max(xs), max(ys))
                index = len(nav.prisms)
                nav.prisms.append((box, float(item["base"]), float(item["top"]), outer, holes,
                                   outer + [edge for hole in holes for edge in hole]))
                for iy in range(math.floor(box[1] / nav._BIN), math.floor(box[3] / nav._BIN) + 1):
                    for ix in range(math.floor(box[0] / nav._BIN), math.floor(box[2] / nav._BIN) + 1):
                        nav._bins.setdefault((ix, iy), []).append(index)
            nav.available = True
        except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError):
            # A missing optional map must not make the wrapper coast forever.
            pass
        return nav

    def ground(self, x, y):
        """Bilinear feet height, in the same world frame as observations."""
        fx = max(0.0, min(self.nx - 1.000001, (x - self.tx) / self.ts))
        fy = max(0.0, min(self.ny - 1.000001, (y - self.ty) / self.ts))
        ix, iy = int(fx), int(fy)
        ax, ay = fx - ix, fy - iy
        i = iy * self.nx + ix
        data = self._data
        return ((data[i] * (1 - ax) + data[i + 1] * ax) * (1 - ay)
                + (data[i + self.nx] * (1 - ax) + data[i + self.nx + 1] * ax) * ay) * self.scale

    def _candidates(self, x, y, radius):
        found = set()
        for iy in range(math.floor((y - radius) / self._BIN), math.floor((y + radius) / self._BIN) + 1):
            for ix in range(math.floor((x - radius) / self._BIN), math.floor((x + radius) / self._BIN) + 1):
                found.update(self._bins.get((ix, iy), ()))
        return found

    def _blocked(self, x, y, radius, height, ground):
        lowx, lowy, highx, highy = self.bounds
        if x - radius < lowx or y - radius < lowy or x + radius > highx or y + radius > highy:
            return True
        for index in self._candidates(x, y, radius):
            box, base, top, outer, holes, edges = self.prisms[index]
            if (top <= ground + self.step_up or base >= ground + height
                    or x + radius < box[0] or x - radius > box[2]
                    or y + radius < box[1] or y - radius > box[3]):
                continue
            if _inside(x, y, outer) and not any(_inside(x, y, hole) for hole in holes):
                return True
            if any(_distance2(x, y, edge) < radius * radius for edge in edges):
                return True
        return False

    def clear_ground_segment(self, a, b, radius):
        # A half-metre extra clearance covers the space between 1 m samples.
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        count = max(1, math.ceil(length))
        interval = length / count
        previous = self.ground(*a)
        height = radius * self.height_ratio
        for i in range(count + 1):
            t = i / count
            x, y = a[0] + t * dx, a[1] + t * dy
            ground = self.ground(x, y)
            padding = min(0.51, length * t * 0.5)
            if self._blocked(x, y, radius + padding, height, ground):
                return False
            if i and (ground - previous > self.step_up
                      or abs(ground - previous) > self.slope * interval + 0.02):
                return False
            previous = ground
        return True

    def _point(self, node):
        return node[0] * self._CELL, node[1] * self._CELL

    def _walkable(self, node, radius):
        if node not in self._walk:
            x, y = self._point(node)
            ground = self.ground(x, y)
            self._heights[node] = ground
            # Any point on a grid edge is <= half its diagonal from an end.
            margin = self._CELL * math.sqrt(2) / 2 + 0.05
            self._walk[node] = not self._blocked(x, y, radius + margin,
                                                   radius * self.height_ratio, ground)
        return self._walk[node]

    def _edge_clear(self, a, b):
        key = (a, b)
        if key not in self._edges:
            pa, pb = self._point(a), self._point(b)
            length = math.dist(pa, pb)
            count = math.ceil(length)
            previous = self._heights[a]
            clear = True
            for i in range(1, count + 1):
                t = i / count
                ground = self.ground(pa[0] + t * (pb[0] - pa[0]), pa[1] + t * (pb[1] - pa[1]))
                if ground - previous > self.step_up or abs(ground - previous) > self.slope * length / count + 0.02:
                    clear = False
                    break
                previous = ground
            self._edges[key] = clear
        return self._edges[key]

    def _begin(self, pos, goal, radius):
        center = (round(pos[0] / self._CELL), round(pos[1] / self._CELL))
        starts = [(center[0] + dx, center[1] + dy) for dx in range(-2, 3) for dy in range(-2, 3)]
        starts.sort(key=lambda node: math.dist(pos, self._point(node)))
        start = next((node for node in starts if self._walkable(node, radius)
                      and self.clear_ground_segment(pos, self._point(node), radius)), None)
        if start is None:
            self._exhausted = True
            return
        h = math.dist(self._point(start), goal)
        self._search = {"heap": [(h, 0.0, start)], "g": {start: 0.0}, "parent": {},
                        "closed": set(), "goal": goal, "start": start, "best": start,
                        "best_h": h}

    def _target_near(self, goal, radius):
        """A center inside a solid cannot be reached: use nearby open ground."""
        height = radius * self.height_ratio
        ground = self.ground(*goal)
        if not self._blocked(*goal, radius + 0.51, height, ground):
            return goal
        candidates = []
        # Projection onto the nearest footprint edges also handles courtyards.
        for index in self._candidates(*goal, radius + 0.51):
            _, base, top, _, _, edges = self.prisms[index]
            if top <= ground + self.step_up or base >= ground + height:
                continue
            for ax, ay, bx, by in edges:
                dx, dy = bx - ax, by - ay
                length2 = dx * dx + dy * dy
                if not length2:
                    continue
                t = max(0.0, min(1.0, ((goal[0] - ax) * dx + (goal[1] - ay) * dy) / length2))
                x, y = ax + dx * t, ay + dy * t
                candidates.append(((x - goal[0]) ** 2 + (y - goal[1]) ** 2,
                                   x, y, -dy / math.sqrt(length2), dx / math.sqrt(length2)))
        nodes = set()
        offset = radius + self._CELL * math.sqrt(2) + 1.0
        for _, x, y, nx, ny in heapq.nsmallest(24, candidates):
            for sign in (-1, 1):
                nodes.add((round((x + nx * offset * sign) / self._CELL),
                           round((y + ny * offset * sign) / self._CELL)))
        for node in sorted(nodes, key=lambda item: math.dist(self._point(item), goal)):
            if self._walkable(node, radius):
                return self._point(node)
        return goal

    def _path(self, search, end):
        result = [self._point(end)]
        while end in search["parent"]:
            end = search["parent"][end]
            result.append(self._point(end))
        result.reverse()
        return result

    def _advance(self, radius, deadline):
        search = self._search
        if search is None:
            return
        heap, costs, closed = search["heap"], search["g"], search["closed"]
        goal = search["goal"]
        for _ in range(self._MAX_EXPAND):
            if not heap or len(closed) >= self._MAX_VISITED:
                self.route = self._path(search, search["best"])
                self._exhausted = True
                self._search = None
                return
            if time.perf_counter() >= deadline:
                return
            _, cost, node = heapq.heappop(heap)
            if node in closed:
                continue
            closed.add(node)
            point = self._point(node)
            h = math.dist(point, goal)
            if h < search["best_h"]:
                search["best"], search["best_h"] = node, h
            # Only test the long shortcut near the goal: geometry work is bounded.
            if h <= 9 and self.clear_ground_segment(point, goal, radius):
                self.route = self._path(search, node) + [goal]
                self._search = None
                return
            for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (-1, 1), (-1, -1), (1, -1)):
                neighbor = node[0] + dx, node[1] + dy
                if neighbor in closed or not self._walkable(neighbor, radius):
                    continue
                next_cost = cost + self._CELL * (math.sqrt(2) if dx and dy else 1)
                if next_cost >= costs.get(neighbor, math.inf) or not self._edge_clear(node, neighbor):
                    continue
                costs[neighbor] = next_cost
                search["parent"][neighbor] = node
                heapq.heappush(heap, (next_cost + math.dist(self._point(neighbor), goal), next_cost, neighbor))

    def direction(self, pos_xyz, goal_xy, radius):
        pos, goal = tuple(pos_xyz[:2]), tuple(goal_xy[:2])
        direct = _unit(pos, goal)
        if not self.available or direct == (0.0, 0.0):
            return direct
        radius = max(0.01, float(radius))
        # This planner models ground surfaces. Keep the baseline on roofs/in air
        # above a normal jump, instead of treating the supporting building as a wall.
        ground = self.ground(*pos)
        if pos_xyz[2] > ground + self.air_height:
            return direct
        if pos_xyz[2] > ground + self.step_up:
            for index in self._candidates(*pos, radius):
                _, _, top, outer, holes, _ = self.prisms[index]
                if (abs(pos_xyz[2] - top) < 0.4 and _inside(*pos, outer)
                        and not any(_inside(*pos, hole) for hole in holes)):
                    return direct
        if goal != self._goal or radius != self._radius:
            self.route = []
            self._search = None
            self._goal, self._radius = goal, radius
            self._exhausted = False
            self._halted_at = None
            self._walk.clear()
            self._heights.clear()
            self._edges.clear()
            self._target = self._target_near(goal, radius)
        if self._halted_at is not None and math.dist(pos, self._halted_at) > 6.0:
            self.route = []
            self._search = None
            self._exhausted = False
            self._halted_at = None
        if self.clear_ground_segment(pos, goal, radius):
            self.route = []
            self._search = None
            return direct
        deadline = time.perf_counter() + self._SLICE
        if self._target != goal and math.dist(pos, self._target) < 2.0:
            return (0.0, 0.0)
        if not self.route and self._search is None and not self._exhausted:
            self._begin(pos, self._target, radius)
        self._advance(radius, deadline)
        route = self.route
        if not route and self._search is not None:
            route = self._path(self._search, self._search["best"])
        # Start near the current position even when an incremental search was
        # rooted several decisions ago. Limit shortcuts to four short segments.
        nearest = min(range(len(route)), key=lambda i: math.dist(pos, route[i])) if route else 0
        indices = list(dict.fromkeys(min(len(route) - 1, nearest + offset)
                                     for offset in (8, 4, 1, 0))) if route else []
        for i in indices:
            point = route[i]
            if math.dist(pos, point) < 0.75:
                continue
            if self.clear_ground_segment(pos, point, radius):
                if self.route:
                    del self.route[:i]
                return _unit(pos, point)
        # While searching, hold still if even the explored prefix is obstructed.
        # Never repeatedly command movement into a known wall.
        if self._search is not None or self._exhausted:
            if self._exhausted and self._halted_at is None:
                self._halted_at = pos
            return (0.0, 0.0)
        self.route = []
        # A displacement can put the cached path behind a wall. Re-root on the
        # next observation rather than returning a known obstructed heading.
        return (0.0, 0.0)
