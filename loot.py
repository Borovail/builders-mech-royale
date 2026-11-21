"""Short, interruptible ground detours for an empty secondary weapon slot."""
from __future__ import annotations

import math

class LootPolicy:
    """Return a navigation goal and an optional protocol-valid take intention.

    The existing trigger policy can use mortar and lightning immediately. Laser
    is still accepted at our feet, but is not worth a detour while both triggers
    are held. Only unobstructed ground approaches are considered; this policy
    never asks the navigator to solve another building or roof route for loot.
    """

    def __init__(self, hello, navigator):
        self.reach = float(hello["config"]["loot"]["pickup_radius"])
        self.walk_speed = float(hello["config"]["mech"]["walk_speed"])
        # Share the movement planner's loaded geometry.
        self.navigator = navigator
        self.target = None
        self._started = 0.0
        self._progress_at = 0.0
        self._best_distance = math.inf
        self._blocked_until = {}

    def _release(self, now):
        if self.target is not None:
            self._blocked_until[self.target] = now + 12.0
        self.target = None

    def _safe(self, pickup, obs, distance, speed, radius):
        zone = obs["zone"]
        point = pickup["pos"][:2]
        if not obs["self"]["in_zone"]:
            return False
        if math.dist(point, zone["center"]) + radius > zone["radius"]:
            return False
        escape = max(0.0, math.dist(point, zone["next_center"])
                     - zone["next_radius"] + radius)
        if escape == 0:
            return True
        # Outside the next circle, finish the detour AND reach that circle
        # before shrinking starts. Reserve time for acceleration/path bends.
        return obs["t"] + (distance + 1.5 * escape) / speed + 3.0 < zone["shrink_start"]

    def _reachable(self, pickup, obs, goal, speed, radius):
        me = obs["self"]
        point, pos = pickup["pos"], me["pos"]
        distance = math.dist(point[:2], pos[:2])
        if pickup["kind"] not in ("lightning", "mortar"):
            return False
        if distance > speed * 2.0 or not self.navigator.available:
            return False
        extra = distance + math.dist(point[:2], goal) - math.dist(pos[:2], goal)
        if extra > speed * 1.5 or not self._safe(pickup, obs, distance, speed, radius):
            return False
        # Pickup z is feet height. A different roof must never look like a
        # short planar walk; airborne jumps toward an existing target are OK.
        if abs(point[2] - self.navigator.ground(*point[:2])) > self.navigator.step_up:
            return False
        ground = self.navigator.ground(*pos[:2])
        if pos[2] > ground + self.navigator.air_height:
            return False
        if me["grounded"] and abs(pos[2] - ground) > self.navigator.step_up:
            return False
        return self.navigator.clear_ground_segment(pos[:2], point[:2], radius)

    def choose(self, obs):
        """Return ``(goal_xy, pickup_id_or_None)`` without changing combat."""
        me, now = obs["self"], obs["t"]
        goal = tuple(obs["zone"]["next_center"])
        self._blocked_until = {item: end for item, end in self._blocked_until.items() if end > now}
        for event in obs.get("events", ()):
            if event["type"] in ("take_failed", "took"):
                self._blocked_until[event["id"]] = now + 12.0
                if event["id"] == self.target:
                    self.target = None
        if me["weapons"][1] is not None:
            self.target = None
            return goal, None
        held = {weapon["kind"] for weapon in me["weapons"] if weapon is not None}
        pickups = [item for item in obs["pickups"]
                   if item["kind"] not in held and item["kind"] != "gun"
                   and item["id"] not in self._blocked_until]
        pickups.sort(key=lambda item: (math.dist(me["pos"][:2], item["pos"][:2]), item["id"]))
        for item in pickups:
            if (math.dist(me["pos"][:2], item["pos"][:2]) <= self.reach
                    and abs(me["pos"][2] - item["pos"][2]) <= 4.0):
                # Resume the original journey in the very action that equips.
                self._release(now)
                self._blocked_until[item["id"]] = now + 12.0
                return goal, item["id"]
        speed = max(0.1, float(me["stats"].get("walk_speed", self.walk_speed)))
        radius = float(me["stats"]["radius"])
        if self.target is not None:
            item = next((item for item in pickups if item["id"] == self.target), None)
            if item is not None:
                distance = math.dist(me["pos"][:2], item["pos"][:2])
                if distance < self._best_distance - 0.5:
                    self._best_distance, self._progress_at = distance, now
                if (now - self._started < 3.0 and now - self._progress_at < 1.0
                        and self._reachable(item, obs, goal, speed, radius)):
                    return tuple(item["pos"][:2]), None
            self._release(now)
            return goal, None
        # Bound the number of geometric checks even in a crowded loot field.
        for item in pickups[:6]:
            if self._reachable(item, obs, goal, speed, radius):
                self.target = item["id"]
                self._started = self._progress_at = now
                self._best_distance = math.dist(me["pos"][:2], item["pos"][:2])
                return tuple(item["pos"][:2]), None
        return goal, None
