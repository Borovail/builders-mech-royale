"""Preposition at safe announced supply rings using public mechfight/1 rules.

``SupplyDrops(hello, navigator).decide(obs)`` returns ``None`` to leave the
ordinary Economy decision untouched, or ``{destination, reason, hold, drop_id}``.
Destination is an (x, y) ground goal and reason is "drop". While hold is true,
the caller must command zero movement, no boost and no jump; otherwise it may
walk toward destination. Capturing is automatic: there is no take/drop action.
Combat aiming, weapons and upgrade choices remain the caller's responsibility.

Drops are globally known, so an absent id is immediately abandoned. Only a
direct verified ground approach is accepted: this optional resource mechanism
does not invent roof routes, use private navigator state or alter its graph.
"""
from __future__ import annotations

import math

from economy import Economy


class SupplyDrops:
    def __init__(self, hello, navigator):
        self.config = hello["config"]
        self.navigator = navigator
        self._target = None
        self._blocked = {}
        self._geometry = {}
        self._retreat_geometry = {}
        self._last_damage = -math.inf
        self._started = self._advanced = 0.0
        self._best_distance = math.inf
        self._best_progress = 0.0
        self._limit = 0.0
        self._landing_deadline = 0.0

    def _release(self, now, cooldown=0.0):
        if self._target is not None and cooldown:
            self._blocked[self._target] = now + cooldown
        self._target = None

    def _zone_safe(self, point, obs, travel, work, speed, radius):
        # Check the approach and reserve an escape after waiting/capture. The
        # target must be safe for our whole body, not just its ring center.
        pos, zone, now = obs["self"]["pos"][:2], obs["zone"], obs["t"]
        margin = radius + 3.0
        for fraction in (0.0, 0.5, 1.0):
            p = tuple(a + fraction * (b - a) for a, b in zip(pos, point))
            center, size = Economy.circle_at(obs, now + travel * fraction)
            if math.dist(p, center) + margin > size:
                return False
        arrival = now + travel + work + 1.0
        center, size = Economy.circle_at(obs, arrival)
        if math.dist(point, center) + margin > size:
            return False
        radial = math.dist(point, zone["next_center"])
        escape = max(0.0, radial - max(0.0, zone["next_radius"] - margin))
        # Early reservations must not buy a long or obstructed retreat after
        # capture. Rings already in the next circle need no return leg.
        if escape / speed > 8.0:
            return False
        exit_point = tuple(a + (b - a) * escape / radial
                           for a, b in zip(point, zone["next_center"])) if radial else point
        if escape:
            key = (tuple(point), tuple(exit_point), radius)
            if key not in self._retreat_geometry:
                if len(self._retreat_geometry) >= 64:
                    self._retreat_geometry.clear()
                self._retreat_geometry[key] = self.navigator.clear_ground_segment(
                    point, exit_point, radius)
            if not self._retreat_geometry[key]:
                return False
        duration = 1.4 * escape / speed + 1.0
        for fraction in (0.0, 0.5, 1.0):
            p = tuple(a + (b - a) * escape * fraction / radial
                      for a, b in zip(point, zone["next_center"])) if radial else point
            center, size = Economy.circle_at(obs, arrival + duration * fraction)
            if math.dist(p, center) + margin > size:
                return False
        return True

    def _threatened(self, item, obs, finish, staging=False):
        me, point = obs["self"], item["pos"][:2]
        pos = me["pos"][:2]
        # Foreign progress cannot be stolen safely. A frozen clock may have an
        # unseen rival; the progress watchdog below also detects that case.
        if item.get("holder") not in (None, me["id"]):
            return True
        rival_speed = max(1.0, float(self.config["mech"]["boost_speed"]))
        for actor in obs.get("mechs", ()):
            here = math.dist(pos, actor["pos"][:2])
            there = math.dist(point, actor["pos"][:2])
            if actor["kind"] == "npc":
                if min(here, there) < 50.0:
                    return True
                continue
            if staging:
                # Distant sightings need not forbid safe early transit. Near
                # landing, the ordinary race/contact checks below apply again.
                if here < 60.0 or there < 65.0:
                    return True
            else:
                reach = max(0.0, there - float(item["radius"])) / rival_speed
                if here < 70.0 or there < 80.0 or reach <= finish + 2.0:
                    return True
        for sound in obs.get("sounds", ()):
            if (sound.get("dist") == "near" and sound.get("age", 0.0) < 1.0
                    and sound["kind"] in ("gun", "mortar", "explosion", "laser",
                                           "laser_charge", "lightning", "npc_gun")):
                return True
        # Enemy projectile observations omit our own shots. Nearby visible
        # fire is enough reason to skip this optional resource detour.
        for shot in obs.get("projectiles", ()):
            if (math.dist(shot["pos"], me["pos"]) < 25.0
                    or math.dist(shot["pos"], item["pos"]) < 20.0):
                return True
        return False

    def _candidate(self, item, obs, speed, radius):
        me, point, now = obs["self"], item["pos"][:2], obs["t"]
        distance = math.dist(me["pos"][:2], point)
        capture = max(0.0, float(item["capture_s"]))
        own = item.get("holder") == me["id"]
        remaining = max(0.0, capture - float(item["progress"])) if own else capture
        travel = distance * 1.25 / speed
        lands_in = max(0.0, float(item["lands_in"]))
        staging = lands_in > 6.0 and item.get("holder") is None
        wait = max(0.0, lands_in - travel)
        work = wait + remaining
        finish = travel + work + 0.7
        # Public announcements are a strategic destination before landing.
        # Bound the planned stationary exposure and total commitment, while
        # retaining the accepted schedule if arrival is faster than estimated.
        active = self._target == item["id"]
        if ((not active and lands_in > 25.0)
                or (active and lands_in > 0.0 and now + lands_in > self._landing_deadline)
                or work > 30.0 or finish > 35.0):
            return None
        goal = obs["zone"]["next_center"]
        extra = max(0.0, distance + math.dist(point, goal) - math.dist(me["pos"][:2], goal))
        hp = max(0.0, 1.0 - me["hp"] / max(1.0, me["max_hp"]))
        energy = max(0.0, 1.0 - me["energy"] / max(1.0, me["max_energy"]))
        cubes = me["cubes"]
        gain = max(0, float(self.config.get("drops", {}).get("cubes", 3)))
        cap = float(self.config["cubes"].get("stat_cap", 10))
        cube_value = min(gain, max(0.0, cap - cubes)) / max(1.0, gain)
        thresholds = self.config["cubes"]["upgrade_thresholds"]
        upgrade = any(cubes < number <= cubes + gain for number in thresholds)
        # Natural regeneration makes long trips for energy alone wasteful.
        # HP, useful cube scaling and a reachable upgrade get separate value.
        value = 4.0 * hp + (0.7 * energy if distance < 30.0 else 0.0)
        value += 2.0 * cube_value + (0.7 if upgrade else 0.0)
        holding = (self._target == item["id"] and distance < float(item["radius"]) - 1.0)
        # Broader routing applies only to the falling, unclaimed stage. Use
        # current distance when switching back to strict capture admission.
        max_distance, detour_seconds = (180.0, 16.0) if staging else (120.0, 8.0)
        if not holding and (value < 0.6 or distance > max_distance
                            or extra > speed * detour_seconds):
            return None
        if hp > 0.70 and distance > 30.0:
            return None
        if self._threatened(item, obs, finish, staging=staging):
            return None
        if not self._zone_safe(point, obs, travel, work, speed, radius):
            return None
        retreat = max(0.0, math.dist(point, goal)
                      - max(0.0, obs["zone"]["next_radius"] - radius - 3.0))
        return value / (2.0 + finish + extra / speed + retreat / speed), distance, finish

    def _accessible(self, item, me, now, radius):
        nav, point, pos = self.navigator, item["pos"], me["pos"]
        if abs(point[2] - nav.ground(*point[:2])) > nav.step_up + 0.2:
            return False
        cached = self._geometry.get(item["id"])
        if (cached and cached[1] == tuple(point) and cached[3] == radius
                and now - cached[0] < 0.5 and math.dist(cached[2], pos[:2]) < 6.0):
            return cached[4]
        result = nav.clear_ground_segment(pos[:2], point[:2], radius)
        self._geometry[item["id"]] = (now, tuple(point), tuple(pos[:2]), radius, result)
        return result

    @staticmethod
    def _decision(item, me):
        pos, point = me["pos"][:2], item["pos"][:2]
        # Brake well inside the ring. Remaining inertia under zero input must
        # fit too, so crossing the outer boundary never starts a false hold.
        velocity = math.hypot(*me["vel"][:2])
        accel = max(1.0, float(me["stats"].get("ground_accel", 58.5)))
        stopping = velocity * velocity / (2.0 * accel) + velocity * 0.1
        hold = math.dist(pos, point) + stopping <= max(0.0, float(item["radius"]) - 1.5)
        return {"destination": tuple(pos if hold else point), "reason": "drop",
                "hold": hold, "drop_id": item["id"]}

    def decide(self, obs):
        now, me = obs["t"], obs["self"]
        for event in obs.get("events", ()):
            if event["type"] in ("damaged", "effect"):
                self._last_damage = now
            if event["type"] == "drop":
                self._blocked[event["id"]] = now + 30.0
                if self._target == event["id"]:
                    self._release(now)
        drops = {item["id"]: item for item in obs.get("drops", ())[:16]}
        self._blocked = {key: end for key, end in self._blocked.items() if end > now}
        self._geometry = {key: value for key, value in self._geometry.items() if key in drops}
        if not drops:
            self._release(now)
            return None
        nav = self.navigator
        if (not me["in_zone"] or now - self._last_damage < 3.0 or not nav.available
                or not me["grounded"]
                or abs(me["pos"][2] - nav.ground(*me["pos"][:2])) > nav.step_up + 0.2):
            self._release(now, 5.0)
            return None
        speed = max(1.0, float(me["stats"].get("walk_speed", self.config["mech"]["walk_speed"])))
        if any(w and w["kind"] == "laser" and w.get("charge", 0.0) > 0 for w in me["weapons"]):
            speed *= float(me["stats"].get("charge_move_mult", 0.5))
        radius = float(me["stats"]["radius"])
        if self._target is not None:
            item = drops.get(self._target)
            candidate = self._candidate(item, obs, speed, radius) if item else None
            if candidate and self._accessible(item, me, now, radius):
                _, distance, _ = candidate
                own_progress = float(item["progress"]) if item.get("holder") == me["id"] else 0.0
                if distance < self._best_distance - 0.5 or own_progress > self._best_progress + 0.03:
                    self._advanced = now
                    self._best_distance = min(self._best_distance, distance)
                    self._best_progress = own_progress
                # Waiting for a known falling drop is not a stall. After it
                # lands, a frozen/reset clock must not hold us indefinitely.
                decision = self._decision(item, me)
                if decision["hold"] and float(item["lands_in"]) > 0.0:
                    self._advanced = now
                stall = 1.5 if decision["hold"] else 2.5
                if now - self._advanced <= stall and now - self._started <= self._limit:
                    return decision
            self._release(now, 8.0)
        candidates = []
        for item in drops.values():
            if item["id"] not in self._blocked:
                candidate = self._candidate(item, obs, speed, radius)
                if candidate:
                    candidates.append((candidate[0], item, candidate[1], candidate[2]))
        candidates.sort(key=lambda row: row[0], reverse=True)
        for _, item, distance, finish in candidates[:3]:
            if self._accessible(item, me, now, radius):
                self._target = item["id"]
                self._started = self._advanced = now
                self._best_distance = distance
                self._best_progress = float(item["progress"]) if item.get("holder") == me["id"] else 0.0
                self._limit = min(40.0, finish + 5.0)
                self._landing_deadline = now + max(0.0, float(item["lands_in"])) + 1.0
                return self._decision(item, me)
        return None
