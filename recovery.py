"""Brief fire-only pauses for the official built-in HP regeneration.

``Recovery(hello).pause_fire(obs)`` is a boolean modifier, called once per
observation. True asks the caller to replace only ``action['fire']`` with
``[False, False]``. Movement, navigation goals, aim and all other actions remain
the caller's responsibility. There is no separate healing action in the game.

Only a recent confirmed outgoing hit can start a pause. This includes crate
hits: dealing damage to a crate resets the same regeneration timer. Missing
health alone never starts an idle period. The manager uses public observations
and the live regeneration constants, without examining weapon types.
"""
from __future__ import annotations

import math


class Recovery:
    def __init__(self, hello):
        self.config = hello["config"]["mech"]
        self._last_hit = -math.inf
        self._last_damage = -math.inf
        self._last_hp = None
        self._paused = False
        self._deadline = 0.0
        self._goal_hp = 0.0
        self._next_pause = 0.0
        self._last_end = -math.inf

    @staticmethod
    def _contact(obs):
        pos = obs["self"]["pos"][:2]
        nearby = 0
        for actor in obs.get("mechs", ()):
            distance = math.dist(pos, actor["pos"][:2])
            if actor["kind"] == "npc":
                if distance < 50.0:
                    return True
            else:
                if distance < 90.0:
                    return True
                nearby += distance < 140.0
        return nearby >= 2

    def _stop(self, now, delay):
        self._paused = False
        self._last_end = now
        self._next_pause = now + max(2.0, delay * 0.5)

    def pause_fire(self, obs):
        now, me = obs["t"], obs["self"]
        hp, maximum = float(me["hp"]), max(1.0, float(me["max_hp"]))
        stats = me.get("stats", {})
        delay = max(0.0, float(stats.get("regen_delay", self.config["regen_delay"])))
        rate = max(0.0, float(stats.get("regen_per_s", self.config["regen_per_s"])))
        incoming = self._last_hp is not None and hp < self._last_hp - 0.01
        # Damage events remain authoritative even when healing/cube growth in
        # the same observation masks the net HP loss. A first damaged snapshot
        # has no trustworthy quiet history, so wait a full delay in that case.
        if self._last_hp is None and hp < maximum:
            self._last_damage = now
        for event in obs.get("events", ()):
            if event["type"] == "damaged":
                incoming = True
            elif event["type"] == "hit" and float(event.get("amount", 0.0)) > 0.0:
                self._last_hit = now
        self._last_hp = hp
        if incoming:
            self._last_damage = now
        unsafe = incoming or not me["in_zone"] or self._contact(obs) or rate <= 0.0
        if self._paused:
            # In-flight outgoing hits can move the regeneration start later,
            # but cannot extend this pause's original deadline.
            if unsafe or hp >= self._goal_hp or now >= self._deadline:
                self._stop(now, delay)
                return False
            return True
        if (unsafe or now < self._next_pause or hp >= 0.80 * maximum
                or now - self._last_damage < delay
                or now - self._last_hit > 0.75 or self._last_hit <= self._last_end):
            return False
        remaining = max(0.0, max(self._last_hit, self._last_damage) + delay - now)
        # At most three seconds of regeneration and nine seconds of total
        # silence. If live rules leave no meaningful healing time, don't pause.
        duration = min(9.0, remaining + min(3.0, 0.10 * maximum / rate))
        if duration - remaining < 0.5:
            return False
        self._paused = True
        self._deadline = now + duration
        self._goal_hp = min(0.90 * maximum, hp + 0.10 * maximum)
        return True
