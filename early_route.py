"""Small, stable opening detours through demonstrably more open ground.

The caller owns resource/combat/grounding eligibility and calls ``reset`` when
that eligibility ends. ``destination(obs, goal)`` returns the unchanged goal
unless a bounded fan finds a clear approach with more clear ground beyond it.
No navigation searches or Navigator route state are changed here.
"""
from __future__ import annotations

import math


class EarlyRoute:
    _DISTANCE = 30.0
    _LOOK_STEP = 15.0
    _ANGLES = (0, -30, 30, -50, 50)

    def __init__(self, navigator):
        self.navigator = navigator
        self.reset()

    def reset(self):
        """Forget the optional route when a higher-priority decision takes over."""
        self._waypoint = None
        self._context = None
        self._next_check = -math.inf
        self._checked_pos = None
        self._expires = -math.inf
        self._best_distance = math.inf
        self._progress_at = -math.inf

    @staticmethod
    def _safe(point, zone, radius):
        return math.dist(point, zone["center"]) + radius + 3.0 <= zone["radius"]

    def _release(self, now, pos):
        self._waypoint = None
        self._next_check = now + 1.0
        self._checked_pos = pos

    def _lookahead(self, point, heading, obs, radius):
        """At most two short rays; stop at the first obstructed/unsafe step."""
        nav, distance = self.navigator, 0.0
        for _ in range(2):
            onward = (point[0] + self._LOOK_STEP * heading[0],
                      point[1] + self._LOOK_STEP * heading[1])
            if (not self._safe(onward, obs["zone"], radius)
                    or not nav.clear_ground_segment(point, onward, radius)):
                break
            distance += self._LOOK_STEP
            point = onward
        return distance

    def destination(self, obs, goal_xy):
        """Return a persistent clear waypoint, or the caller's original goal.

        Full selection costs at most 16 clear-ground segment checks (all <=30m).
        Open forward ground exits after three checks. A selected waypoint uses
        only one segment check per second until reached, cancelled or timed out.
        """
        goal = tuple(goal_xy[:2])
        nav, now, me, zone = self.navigator, obs["t"], obs["self"], obs["zone"]
        pos = tuple(me["pos"][:2])
        radius = float(me["stats"]["radius"])
        remaining = math.dist(pos, goal)
        if (not nav.available or now > 45.0 or remaining < 45.0
                or not me["in_zone"] or not self._safe(pos, zone, radius)):
            self.reset()
            return goal

        # Shrinking center/radius vary every tick; phase targets and transition
        # times are stable. Crossing either transition invalidates the detour.
        context = (goal, radius, tuple(zone["next_center"]), zone["next_radius"],
                   zone["shrink_start"], zone["shrink_end"],
                   now >= zone["shrink_start"], now >= zone["shrink_end"])
        if context != self._context:
            self.reset()
            self._context = context

        if self._waypoint is not None:
            distance = math.dist(pos, self._waypoint)
            toward_goal = (goal[0] - pos[0], goal[1] - pos[1])
            toward_waypoint = (self._waypoint[0] - pos[0], self._waypoint[1] - pos[1])
            if distance < self._best_distance - 1.0:
                self._best_distance, self._progress_at = distance, now
            if (distance <= 4.0 or now >= self._expires
                    or now - self._progress_at > 2.0
                    or sum(a * b for a, b in zip(toward_goal, toward_waypoint)) <= 0.0
                    or not self._safe(self._waypoint, zone, radius)):
                self._release(now, pos)
                return goal
            if now >= self._next_check:
                self._next_check = now + 1.0
                if not nav.clear_ground_segment(pos, self._waypoint, radius):
                    self._release(now, pos)
                    return goal
            return self._waypoint

        # Cache the common negative decision while the original route proceeds.
        if (now < self._next_check and self._checked_pos is not None
                and math.dist(pos, self._checked_pos) < 12.0):
            return goal
        self._next_check, self._checked_pos = now + 1.0, pos
        forward = ((goal[0] - pos[0]) / remaining, (goal[1] - pos[1]) / remaining)
        baseline = 0.0
        best = None
        for degrees in self._ANGLES:
            angle = math.radians(degrees)
            c, s = math.cos(angle), math.sin(angle)
            heading = (forward[0] * c - forward[1] * s,
                       forward[0] * s + forward[1] * c)
            point = (pos[0] + self._DISTANCE * heading[0],
                     pos[1] + self._DISTANCE * heading[1])
            if not self._safe(point, zone, radius):
                continue
            advance = remaining - math.dist(point, goal)
            if advance < 10.0:
                continue
            if not nav.clear_ground_segment(pos, point, radius):
                if degrees == 0:
                    halfway = ((pos[0] + point[0]) / 2, (pos[1] + point[1]) / 2)
                    if nav.clear_ground_segment(pos, halfway, radius):
                        baseline = self._DISTANCE / 2
                continue
            covered = self._DISTANCE + self._lookahead(point, heading, obs, radius)
            if degrees == 0:
                baseline = covered
                if covered == self._DISTANCE + 2 * self._LOOK_STEP:
                    return goal
                continue
            # Discrete coverage has priority; advancement breaks equal rays in
            # favour of a shallower detour. Never trade a clear center for a tie.
            if covered >= baseline + self._LOOK_STEP:
                score = (covered, advance)
                if best is None or score > best[0]:
                    best = score, point
        if best is not None:
            self._waypoint = best[1]
            self._expires = now + 6.0
            self._best_distance = self._DISTANCE
            self._progress_at = now
            return self._waypoint
        return goal
