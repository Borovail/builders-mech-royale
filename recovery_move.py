"""One short ground relocation after a wounded mech stalls at its zone goal.

The caller retains resource priorities through ``allowed`` and supplies its
ordinary fallback direction.  Returned waypoints have a verified direct ground
approach, so the caller may use a normalized heading without changing the
navigator's route state.  Only public health, damage, movement and zone data
are used; this module does not inspect opponents or choose combat actions.
"""
from __future__ import annotations

import math


class RecoveryMove:
    _DISTANCE = 24.0
    _HIT_WINDOW = 4.0
    _STALL_DELAY = 0.6
    _TTL = 3.0
    _RETRY_DELAY = 6.0
    _HEADINGS = tuple((math.cos(i * math.pi / 4),
                       math.sin(i * math.pi / 4)) for i in range(8))

    def __init__(self, navigator):
        self.navigator = navigator
        self._last_hit = -math.inf
        self._stalled_since = None
        self._waypoint = None
        self._expires = -math.inf
        self._retry_after = -math.inf

    @staticmethod
    def _inside_both(point, zone, margin):
        # Discs are convex. Keeping both endpoints inside both shrunken discs
        # also keeps the complete straight approach and mech body inside them.
        return all(math.dist(point, zone[center]) + margin <= zone[radius]
                   for center, radius in (("center", "radius"),
                                          ("next_center", "next_radius")))

    def _release(self):
        self._waypoint = None
        self._stalled_since = None

    def destination(self, obs, fallback_goal, original_move, *, allowed):
        """Return a bounded temporary goal, or None to preserve normal movement.

        Call on every observation, including resource pursuit: damage memory
        is refreshed before ``allowed`` can cancel an existing relocation.
        An active waypoint deliberately survives normal velocity increasing
        and moving away from the fallback goal, but never an eligibility loss.
        """
        now, me, zone = obs["t"], obs["self"], obs["zone"]
        if any(event["type"] == "damaged" and event.get("weapon") != "zone"
               and event.get("amount", 0) > 0
               for event in obs.get("events", ())):
            self._last_hit = now

        pos = tuple(me["pos"][:2])
        radius = me["stats"]["radius"]
        margin = radius + 2.0
        if (not allowed or not self.navigator.available
                or not 45.0 <= now <= 180.0
                or not 0.0 < me["hp"] <= 0.55 * me["max_hp"]
                or now - self._last_hit > self._HIT_WINDOW
                or not me["grounded"] or not me["in_zone"]
                or abs(me["pos"][2] - self.navigator.ground(*pos)) > self.navigator.step_up + 0.2
                or not self._inside_both(pos, zone, margin)):
            self._release()
            return None

        if self._waypoint is not None:
            if (now >= self._expires
                    or math.dist(pos, self._waypoint) <= max(2.0, radius)
                    or not self._inside_both(self._waypoint, zone, margin)
                    or not self.navigator.clear_ground_segment(pos, self._waypoint, radius)):
                self._release()
                return None
            return self._waypoint

        stalled = (math.hypot(*original_move) <= 0.1
                   or math.hypot(*me["vel"][:2]) <= 3.0)
        if (math.dist(pos, fallback_goal) > max(10.0, 3.0 * radius)
                or not stalled):
            self._stalled_since = None
            return None
        if self._stalled_since is None:
            self._stalled_since = now
        if (now - self._stalled_since + 1e-9 < self._STALL_DELAY
                or now < self._retry_after):
            return None

        # This is one attempt, bounded to eight static headings. Avoid testing
        # blocked ground again every observation when no complete leg exists.
        self._retry_after = now + self._RETRY_DELAY
        candidates = [(pos[0] + self._DISTANCE * dx,
                       pos[1] + self._DISTANCE * dy)
                      for dx, dy in self._HEADINGS]
        candidates.sort(key=lambda point: (math.dist(point, zone["next_center"]),
                                            math.dist(point, zone["center"])))
        for point in candidates:
            if (self._inside_both(point, zone, margin)
                    and self.navigator.clear_ground_segment(pos, point, radius)):
                self._waypoint = point
                self._expires = now + self._TTL
                return point
        return None
