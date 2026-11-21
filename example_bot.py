"""Mech Royale bot with obstacle-aware movement toward the next safe circle."""
import math
import mechfight as mf
from economy import Economy
from mobility import Mobility
from navigation import Navigator
from recovery import Recovery
from secondary import Secondary
from supply import SupplyDrops


class Bot:
    name = "Borovail E24"

    def __init__(self, hello: mf.Hello):
        self.navigator = Navigator.from_hello(hello)
        self.mobility = Mobility(hello)
        self.economy = Economy(hello, self.navigator)
        self.recovery = Recovery(hello, self.navigator)
        self.secondary = Secondary(hello)
        self.supplies = SupplyDrops(hello, self.navigator)
        self.crate_height = hello["config"]["crates"]["height"]

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        x, y, z = me["pos"]
        choice = self.economy.decide(obs)
        supply = self.supplies.decide(obs)
        recovery = self.recovery.decide(obs)
        route = recovery or supply
        goal = route["destination"] if route else choice["destination"]
        holding = bool(route and route["hold"])
        act: mf.Action = {"tick": obs["tick"]}
        if choice["upgrade_index"] is not None:
            act["pick"] = choice["upgrade_index"]
        if choice["pickup_id"] is not None:
            act["take"] = choice["pickup_id"]
        enemies = sorted((m for m in obs["mechs"] if m["kind"] == "mech"), key=lambda m: math.dist(m["pos"], me["pos"]))
        yaw = math.atan2(goal[1] - y, goal[0] - x)
        direction = (0.0, 0.0) if holding else self.navigator.direction(me["pos"], goal, me["stats"]["radius"])
        jump = obs["tick"] % 40 == 0 and route is None
        if me["grounded"] and me["in_zone"] and any(direction):
            jump = False
        boost = not holding and self.mobility.boost(obs, direction, jumping=jump)
        if recovery and not me["grounded"]:
            boost = False
        act.update(move=list(direction), aim=[yaw, 0.0], boost=boost, jump=jump)
        if enemies:
            t = enemies[0]["pos"]
            aim = [math.atan2(t[1] - y, t[0] - x), math.atan2(t[2] - z - 2.5, math.dist(t[:2], (x, y)))]
            act["aim"] = aim
            act["fire"] = [False, False] if recovery else [True, self.secondary.trigger(obs, enemies[0])]
            mf.draw.line([x, y, z + 7], [t[0], t[1], t[2] + 4.5], "#ff5040")
        elif recovery:
            act.update(aim=list(me["aim"]), fire=[False, False])
        elif supply is None and choice["crate"] is not None:
            cx, cy, cz = choice["crate"]["pos"]
            dz = (cz + self.crate_height / 2) - (z + me["stats"]["muzzle_height"])
            act.update(aim=[math.atan2(cy - y, cx - x), math.atan2(dz, math.hypot(cx - x, cy - y))],
                       fire=[True, False])
        elif supply or choice["reason"] == "none":
            # Legs keep the route while the game camera reveals new pickups.
            act["aim"] = [math.remainder(me["aim"][0] + 0.4, math.tau), 0.0]
        mf.draw.point([goal[0], goal[1], z], "#40c0ff", 6)
        mf.draw.label(f"hp {me['hp']:.0f} · {len(enemies)} in sight")
        return act


if __name__ == "__main__":
    mf.run(Bot)
