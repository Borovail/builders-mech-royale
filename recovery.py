"""Early, bounded break-contact routes to real ground cover inside the circle.

Recovery(hello, navigator).decide(obs) returns None or {destination, hold}.
The caller gives this movement priority and releases both weapon triggers for
the whole decision. A released laser or an already flying shot can still hit;
observed hit events restart the five-second recovery clock here as well.
Search never changes the navigator graph and accepts only verified one- or
two-leg ground routes. Static prism intersections prove cover against recently
seen threats; they are not a prediction that enemies or shells will stay put.
"""
from __future__ import annotations

from collections import deque
import math

from economy import Economy
from navigation import _inside


class Recovery:
    SEARCH_INTERVAL = 0.75
    GROUND_CHECKS = 24
    MAX_PATH = 55.0
    HOLD_SECONDS = 22.0

    def __init__(self, hello, navigator):
        self.config = hello["config"]
        self.navigator = navigator
        self.phase = "idle"
        self.stats = {"activations": 0, "reached_cover": 0,
                      "hp_gained": 0.0, "hold_seconds": 0.0,
                      "max_quiet_seconds": 0.0, "quiet_windows": 0,
                      "searches": 0, "ground_checks": 0, "releases": {}}
        self._losses = deque()
        self._known = {}
        self._previous = None
        self._last_damage = self._last_combat = -math.inf
        self._next_search = self._cooldown = 0.0
        self._route = []
        self._cover_prisms = []
        self._started = self._arrived = self._progress_at = 0.0
        self._best_distance = math.inf
        self._quiet_recorded = False
        self._cover_check = 0.0
        self._checks_left = 0

    def _refresh(self, obs):
        now, me = obs["t"], obs["self"]
        damaged = any(e["type"] == "damaged" and e.get("weapon") != "zone"
                      for e in obs.get("events", ()))
        hit = any(e["type"] == "hit" for e in obs.get("events", ()))
        if self._previous is not None:
            old_t, old_hp, old_max = self._previous
            # Cube scaling changes max_hp and must not look like regeneration.
            if old_max == me["max_hp"]:
                lost = max(0.0, old_hp - me["hp"])
                if lost and me["in_zone"]:
                    self._losses.append((now, lost))
                    damaged = True
                if self.phase != "idle":
                    self.stats["hp_gained"] += max(0.0, me["hp"] - old_hp)
            if self.phase == "hold":
                self.stats["hold_seconds"] += max(0.0, now - old_t)
        self._previous = now, me["hp"], me["max_hp"]
        while self._losses and self._losses[0][0] < now - 6.0:
            self._losses.popleft()
        if damaged:
            self._last_damage = now
        if damaged or hit or not me["in_zone"]:
            self._last_combat = now
        if self.phase != "idle":
            quiet = max(0.0, now - max(self._last_combat, self._started))
            self.stats["max_quiet_seconds"] = max(self.stats["max_quiet_seconds"], quiet)
            if quiet >= 5.0 and not self._quiet_recorded:
                self.stats["quiet_windows"] += 1
                self._quiet_recorded = True
        for actor in obs.get("mechs", ()):
            if actor.get("id") != me["id"] and actor.get("hp", 1.0) > 0:
                self._known[actor["id"]] = (now, tuple(actor["pos"]))
        self._known = {key: value for key, value in self._known.items()
                       if now - value[0] <= 4.0}

    def _threats(self, me):
        # NPC fire also prevents regeneration. A bounded nearest set keeps a
        # crowded observation from multiplying every cover geometry test.
        positions = [value[1] for value in self._known.values()]
        return sorted(positions, key=lambda p: math.dist(p[:2], me["pos"][:2]))[:3]

    @staticmethod
    def _blocks(prism, origin, target):
        """Exact line/prism interior intersection, including footprint holes."""
        box, base, top, outer, holes, edges = prism
        if (max(origin[0], target[0]) < box[0] or min(origin[0], target[0]) > box[2]
                or max(origin[1], target[1]) < box[1] or min(origin[1], target[1]) > box[3]
                or max(origin[2], target[2]) < base or min(origin[2], target[2]) > top):
            return False
        dx, dy, dz = (b - a for a, b in zip(origin, target))
        cuts = [0.0, 1.0]
        if abs(dz) > 1e-9:
            cuts.extend((h - origin[2]) / dz for h in (base, top)
                        if 0.0 < (h - origin[2]) / dz < 1.0)
        for ax, ay, bx, by in edges:
            ex, ey = bx - ax, by - ay
            det = dx * ey - dy * ex
            if abs(det) < 1e-9:
                continue
            qx, qy = ax - origin[0], ay - origin[1]
            t, u = (qx * ey - qy * ex) / det, (qx * dy - qy * dx) / det
            if 0.0 < t < 1.0 and 0.0 <= u <= 1.0:
                cuts.append(t)
        cuts.sort()
        for left, right in zip(cuts, cuts[1:]):
            if right - left < 1e-6:
                continue
            t = (left + right) * 0.5
            x, y, z = origin[0] + t * dx, origin[1] + t * dy, origin[2] + t * dz
            if base < z < top and _inside(x, y, outer) and not any(_inside(x, y, h) for h in holes):
                return True
        return False

    def _covered(self, point, threats, radius, prisms):
        # Protect the upper body as well as its centre; low walls that only
        # hide feet are not a usable recovery shelter.
        z = self.navigator.ground(*point) + radius * self.navigator.height_ratio * 0.85
        target = (*point, z)
        muzzle = float(self.config["mech"].get("muzzle_height", 4.5))
        return all(any(self._blocks(p, (a[0], a[1], a[2] + muzzle), target)
                       for p in prisms) for a in threats)

    def _safe(self, route, obs, speed, radius, hold):
        pos, now = obs["self"]["pos"][:2], obs["t"]
        elapsed = 0.0
        for point in route:
            travel = 1.25 * math.dist(pos, point) / speed
            for fraction in (0.0, 0.5, 1.0):
                spot = tuple(a + (b - a) * fraction for a, b in zip(pos, point))
                center, size = Economy.circle_at(obs, now + elapsed + travel * fraction)
                if math.dist(spot, center) + radius + 3.0 > size:
                    return False
            elapsed += travel
            pos = point
        center, size = Economy.circle_at(obs, now + elapsed + hold)
        return math.dist(pos, center) + radius + 3.0 <= size

    def _clear(self, a, b, radius):
        if self._checks_left <= 0:
            return False
        self._checks_left -= 1
        self.stats["ground_checks"] += 1
        return self.navigator.clear_ground_segment(a, b, radius)

    def _search(self, obs, speed, radius, threats):
        nav, pos = self.navigator, obs["self"]["pos"][:2]
        self.stats["searches"] += 1
        self._checks_left = self.GROUND_CHECKS
        # Nearby bounding-box corners are only proposals. Every candidate and
        # every route segment still passes the actual polygon/terrain checks.
        def distance(prism):
            b = prism[0]
            return math.hypot(max(b[0] - pos[0], 0.0, pos[0] - b[2]),
                              max(b[1] - pos[1], 0.0, pos[1] - b[3]))
        prisms = sorted((p for p in nav.prisms if distance(p) <= 45.0), key=distance)[:8]
        margin = radius + 3.0
        points = set()
        for prism in prisms:
            x1, y1, x2, y2 = prism[0]
            xs, ys = (x1 - margin, (x1 + x2) / 2, x2 + margin), (y1 - margin, (y1 + y2) / 2, y2 + margin)
            points.update((x, y) for i, x in enumerate(xs) for j, y in enumerate(ys)
                          if i != 1 or j != 1)
        points = sorted((p for p in points if 3.0 <= math.dist(pos, p) <= self.MAX_PATH),
                        key=lambda p: (math.dist(pos, p), p))[:48]
        covered = [p for p in points if self._covered(p, threats, radius, prisms)]
        for point in covered[:12]:
            if not self._safe([point], obs, speed, radius, self.HOLD_SECONDS):
                continue
            if self._clear(pos, point, radius):
                return [point], prisms
        # A corner waypoint can put the final leg behind the building. Keep
        # the count strict and cache the first leg within this search only.
        first_legs = {}
        pairs = sorted((math.dist(pos, via) + math.dist(via, point), via, point)
                       for point in covered[:8] for via in points[:16] if via != point)
        for length, via, point in pairs:
            if length > min(self.MAX_PATH, speed * 7.0) or self._checks_left < 1:
                continue
            if not self._safe([via, point], obs, speed, radius, self.HOLD_SECONDS):
                continue
            if via not in first_legs:
                first_legs[via] = self._clear(pos, via, radius)
            if first_legs[via] and self._clear(via, point, radius):
                return [via, point], prisms
        return None

    def _release(self, now, reason):
        self.phase = "idle"
        self._route = []
        self._cooldown = now + 4.0
        reasons = self.stats["releases"]
        reasons[reason] = reasons.get(reason, 0) + 1

    def decide(self, obs):
        self._refresh(obs)
        now, me, nav = obs["t"], obs["self"], self.navigator
        radius = float(me["stats"]["radius"])
        speed = max(1.0, float(me["stats"].get("walk_speed", self.config["mech"]["walk_speed"])))
        grounded = (nav.available and me["grounded"]
                    and abs(me["pos"][2] - nav.ground(*me["pos"][:2])) <= nav.step_up + 0.2)
        if not grounded or not me["in_zone"]:
            if self.phase != "idle":
                self._release(now, "ground_or_zone")
            return None
        threats = self._threats(me)
        if self.phase != "idle":
            if me["hp"] >= 0.90 * me["max_hp"]:
                self._release(now, "healed")
                return None
            if not self._safe(self._route, obs, speed, radius,
                              max(0.0, self.HOLD_SECONDS - (now - self._arrived))
                              if self.phase == "hold" else self.HOLD_SECONDS):
                self._release(now, "zone")
                return None
            if now >= self._cover_check:
                self._cover_check = now + 0.25
                if threats and not self._covered(self._route[-1], threats, radius, self._cover_prisms):
                    self._release(now, "exposed")
                    return None
            if self.phase == "hold":
                if now - self._arrived >= self.HOLD_SECONDS:
                    self._release(now, "timeout")
                    return None
                # New damage after braking means cover failed against a shell,
                # an unseen flanker or a threat omitted by the bounded set.
                if self._last_damage > self._arrived + 0.75:
                    self._release(now, "damage_in_cover")
                    return None
                return {"destination": tuple(me["pos"][:2]), "hold": True}
            distance = math.dist(me["pos"][:2], self._route[0])
            if distance < self._best_distance - 0.5:
                self._best_distance, self._progress_at = distance, now
            if now - self._progress_at > 2.0 or now - self._started > 8.0:
                self._release(now, "stalled")
                return None
            if distance < 1.25:
                if len(self._route) > 1:
                    self._route.pop(0)
                    self._best_distance = math.inf
                    self._progress_at = now
                else:
                    self.phase = "hold"
                    self._arrived = now
                    self.stats["reached_cover"] += 1
                    return {"destination": tuple(me["pos"][:2]), "hold": True}
            return {"destination": self._route[0], "hold": False}
        sustained = (len(self._losses) >= 2 and self._losses[-1][0] - self._losses[0][0] >= 1.0
                     and sum(loss for _, loss in self._losses) >= 0.10 * me["max_hp"])
        if (now < max(15.0, self._cooldown, self._next_search) or not sustained
                or me["hp"] > 0.75 * me["max_hp"] or now - self._last_damage > 1.0
                or not threats):
            return None
        self._next_search = now + self.SEARCH_INTERVAL
        result = self._search(obs, speed, radius, threats)
        if result is None:
            return None
        self._route, self._cover_prisms = result
        self.phase = "approach"
        self._started = self._progress_at = now
        self._arrived = now
        self._best_distance = math.dist(me["pos"][:2], self._route[0])
        self._quiet_recorded = False
        self._cover_check = now + 0.25
        self.stats["activations"] += 1
        return {"destination": self._route[0], "hold": False}
