"""Prefer a recent damaging mech when the nearest tracked mech is uninvolved.

Targeting().choose(obs, enemies) returns an existing enemy object unchanged,
or None when the baseline enemy list is empty. Pass enemies in the baseline
3-D distance order. Only target selection changes: the caller keeps its aim,
weapon triggers, movement, resources and upgrades exactly as before.
"""
from collections import deque
import math


class Targeting:
    WINDOW_SECONDS = 2.0
    MIN_DAMAGE = 30.0
    MAX_SOURCES = 16
    MAX_HITS_PER_SOURCE = 32

    def __init__(self):
        self._hits = {}
        self._last_tick = None

    def choose(self, obs, enemies):
        now = obs["t"]
        # The official observation may include a tracked actor. Requiring it
        # here means we never turn toward an absent source or a sound bearing.
        tracked = {enemy["id"]: enemy for enemy in enemies
                   if enemy.get("kind") == "mech"
                   and enemy["id"] != obs["self"]["id"]
                   and enemy.get("hp", 1.0) > 0.0}
        for source, hits in list(self._hits.items()):
            while hits and now - hits[0][0] > self.WINDOW_SECONDS:
                hits.popleft()
            if not hits:
                del self._hits[source]
        # Repeated inspection of one observation must not invent extra damage.
        tick = obs.get("tick", now)
        if tick != self._last_tick:
            self._last_tick = tick
            for event in obs.get("events", ()):
                source = event.get("by")
                if (event.get("type") != "damaged" or event.get("weapon") == "zone"
                        or source not in tracked or event.get("amount", 0.0) <= 0.0):
                    continue
                if source not in self._hits:
                    if len(self._hits) >= self.MAX_SOURCES:
                        # In the 16-mech game there are at most 15 rivals. This
                        # bound also handles a malformed or extended stream.
                        oldest = min(self._hits, key=lambda key: (self._hits[key][-1][0], key))
                        del self._hits[oldest]
                    self._hits[source] = deque(maxlen=self.MAX_HITS_PER_SOURCE)
                self._hits[source].append((now, float(event["amount"])))
        if not enemies:
            return None
        nearest = enemies[0]
        if nearest["id"] not in tracked:
            return nearest
        # Keep the existing nearest opponent when it is itself a recent source
        # of damage, even if another opponent dealt a larger hit.
        if nearest["id"] in self._hits:
            return nearest
        position = obs["self"]["pos"]
        nearest_distance = math.dist(position, nearest["pos"])
        limit = min(1.35 * nearest_distance, nearest_distance + 20.0)
        candidates = []
        for order, enemy in enumerate(enemies):
            source = enemy["id"]
            if source not in tracked or source not in self._hits:
                continue
            hits = self._hits[source]
            damage = sum(amount for _, amount in hits)
            if damage >= self.MIN_DAMAGE and math.dist(position, enemy["pos"]) <= limit:
                candidates.append((-damage, -hits[-1][0], order, source, enemy))
        return min(candidates, key=lambda row: row[:4])[-1] if candidates else nearest
