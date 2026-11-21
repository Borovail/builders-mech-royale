"""Spend spare ground-boost energy during contact, keeping a movement reserve."""
import math


class Mobility:
    def __init__(self, hello):
        self.defaults = hello["config"]["mech"]
        match = hello["config"]["match"]
        self.decision_s = match["decide_every"] / match["tick_hz"]
        self._burst_active = False
        self._threat_until = -math.inf
        self._restart_after = -math.inf

    def boost(self, obs, move, *, jumping=False):
        me = obs["self"]
        # Retain E01's circle escape, including its existing airborne jet use.
        if not me["in_zone"]:
            self._burst_active = False
            return True

        now = obs["t"]
        stats = me["stats"]
        drain = stats.get("boost_drain", self.defaults["boost_drain"])
        regen_delay = stats.get("energy_regen_delay", self.defaults["energy_regen_delay"])
        contact = any(actor["kind"] == "mech" for actor in obs["mechs"])
        damaged = any(event["type"] == "damaged" and event.get("weapon") != "zone"
                      for event in obs["events"])
        if contact or damaged:
            # Bridge one brief visibility gap without continuing a long chase.
            self._threat_until = now + 0.6

        maximum = me["max_energy"]
        reserve = min(maximum * 0.6, max(maximum * 0.35, drain * (1.0 + regen_delay)))
        start_energy = min(maximum, reserve + drain * 2.0)
        radius = stats.get("radius", self.defaults["radius"])
        goal_distance = math.dist(me["pos"][:2], obs["zone"]["next_center"])
        if (now > self._threat_until or math.hypot(*move) <= 0.1
                or goal_distance <= radius * 2.0 or maximum - reserve < drain * 0.75):
            self._burst_active = False
            return False
        if self._burst_active and me["energy"] <= reserve + drain * self.decision_s:
            self._burst_active = False
            self._restart_after = now + regen_delay + 0.4
        # A jump temporarily suspends ground boost. Resuming after landing
        # uses the same energy reserve as the interrupted burst.
        if not me["grounded"] or jumping:
            return False
        if not self._burst_active and now >= self._restart_after and me["energy"] >= start_energy:
            self._burst_active = True
        return self._burst_active
