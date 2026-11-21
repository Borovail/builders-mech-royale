"""Digital trigger policy for the second slot, using observed game state.

The primary slot and commanded aim are deliberately outside this module.
The official kit exposes charge and cooldown, so no private clock is needed.
"""
import math


class Secondary:
    def __init__(self, hello):
        config = hello.get("config", {})
        self.weapons = config.get("weapons", {})
        self.mech = config.get("mech", {})

    def trigger(self, obs, enemy):
        """Return whether to hold slot 1 until the next decision.

        False releases a charging laser; the game may still fire its partial
        charge. Losing a target cannot cancel that shot in the kit protocol.
        """
        me = obs.get("self", {})
        slots = me.get("weapons", [])
        if len(slots) < 2 or not isinstance(slots[1], dict):
            return False
        weapon = slots[1]
        kind = weapon.get("kind")
        if kind not in ("gun", "mortar", "laser", "lightning"):
            return False
        if (not isinstance(enemy, dict) or enemy.get("kind") != "mech"
                or enemy.get("id") == me.get("id")
                or enemy.get("hp", 1) <= 0):
            return False
        if weapon.get("cooldown", 0) > 0:
            return False
        stats = {**self.weapons.get(kind, {}), **weapon.get("stats", {})}
        origin, target = me.get("pos", []), enemy.get("pos", [])
        if len(origin) != 3 or len(target) != 3:
            return False
        distance = math.dist(origin, target)
        if not math.isfinite(distance) or distance <= 0:
            return False

        if kind == "mortar":
            # Only a coarse game envelope, not an impact or trajectory model.
            # Distance outside the shell's entire lifetime is unreachable;
            # nearby targets risk the game's own splash and concussion.
            mech_stats = {**self.mech, **me.get("stats", {})}
            safe = stats.get("splash_radius", 0) + mech_stats.get("radius", 0)
            reach = stats.get("range", stats.get("speed", 0) * stats.get("ttl", 0))
            return safe < distance <= reach

        if distance > stats.get("range", 0):
            return False
        if kind == "laser":
            # Holding at full charge otherwise adds hold_s of half-speed
            # movement before the game automatically releases the beam.
            full = stats.get("charge", 0)
            return full > 0 and weapon.get("charge", 0) + 1e-9 < full
        if kind == "lightning":
            aim = me.get("aim", [])
            if not aim or me.get("energy", 0) < stats.get("energy", math.inf):
                return False
            yaw = math.atan2(target[1] - origin[1], target[0] - origin[0])
            error = abs(math.atan2(math.sin(yaw - aim[0]), math.cos(yaw - aim[0])))
            # The kit calls cone_deg the allowed difference from aim yaw.
            return error <= math.radians(stats.get("cone_deg", 0)) + 1e-9
        return weapon.get("ammo", 0) > 0 and weapon.get("reload", 0) <= 0
