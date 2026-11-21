"""Short ground boosts toward Economy's existing early pickup/cube goal.

``LootBoost(hello).boost(obs, choice, jumping=False)`` is an additive boolean:
the caller keeps its normal Mobility decision and ORs this result into boost.
Call both managers every observation, without short-circuiting either call.
No goals, directions, jump decisions, aiming or firing are changed here.

Economy has already verified the direct ground approach. An immediate weapon
take has a different destination, so it is deliberately excluded as well as
crates, zone goals and every resource other than pickups and cubes.
"""
import math


class LootBoost:
    def __init__(self, hello):
        config = hello["config"]
        self.defaults = config["mech"]
        self.pickup_radius = float(config["loot"]["pickup_radius"])
        self.cube_radius = float(config["cubes"].get("pickup_radius", 3.5))
        self.contact_radius = 2.0 * float(config["vision"]["near_range"])
        self.decision_s = config["match"]["decide_every"] / config["match"]["tick_hz"]
        self._target = None
        self._until = self._restart_after = 0.0
        self._last_damage = -math.inf

    def _stop(self, now, regen_delay):
        if self._target is not None:
            self._restart_after = now + regen_delay + 0.4
        self._target = None

    def boost(self, obs, choice, *, jumping=False):
        now, me = obs["t"], obs["self"]
        stats = me["stats"]
        regen_delay = float(stats.get("energy_regen_delay", self.defaults["energy_regen_delay"]))
        if any(event["type"] in ("damaged", "effect") for event in obs.get("events", ())):
            self._last_damage = now
        reason = choice["reason"]
        if (reason not in ("pickup", "cube") or choice.get("pickup_id") is not None
                or now >= 60.0 or not me["in_zone"] or not me["grounded"] or jumping
                or me["hp"] < 0.85 * me["max_hp"] or now - self._last_damage < 2.0
                or any(math.dist(me["pos"][:2], actor["pos"][:2]) < self.contact_radius
                       for actor in obs.get("mechs", ()))):
            self._stop(now, regen_delay)
            return False
        drain = float(stats.get("boost_drain", self.defaults["boost_drain"]))
        fast = float(stats.get("boost_speed", self.defaults["boost_speed"]))
        walk = float(stats.get("walk_speed", self.defaults["walk_speed"]))
        accel = max(1.0, float(stats.get("ground_accel", self.defaults["ground_accel"])))
        maximum = float(me["max_energy"])
        reserve = max(0.70 * maximum, drain * (1.0 + regen_delay))
        reach = self.pickup_radius if reason == "pickup" else self.cube_radius
        # Release boost before the resource, allowing a decision interval and
        # full boosted stopping distance without changing the chosen direction.
        stop = reach + fast * fast / (2.0 * accel) + fast * self.decision_s + 1.0
        distance = math.dist(me["pos"][:2], choice["destination"])
        if drain <= 0.0 or fast <= walk or distance <= stop or maximum - reserve < drain * self.decision_s:
            self._stop(now, regen_delay)
            return False
        target = (reason, tuple(choice["destination"]))
        if self._target is not None and self._target != target:
            self._stop(now, regen_delay)
        if self._target is not None:
            if now + self.decision_s > self._until + 1e-9 or me["energy"] <= reserve + drain * self.decision_s:
                self._stop(now, regen_delay)
                return False
            return True
        if (now < self._restart_after or me["energy"] < 0.90 * maximum
                or distance < stop + 1.2 * walk):
            return False
        # One pulse spends at most a fifth of capacity, with a time cap. The
        # higher restart threshold leaves regeneration time between pulses.
        duration = min(1.5, 0.20 * maximum / drain)
        duration = math.floor(duration / self.decision_s + 1e-9) * self.decision_s
        if duration < self.decision_s:
            return False
        self._target, self._until = target, now + duration
        return True
