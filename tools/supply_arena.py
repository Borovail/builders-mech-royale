"""Optional local-match extension for the three tournament supply rings.

Only used by the offline referee. Opponents may capture rings on their normal
routes. Their movement policy, exact landing distribution and sound cues are
not known from the public protocol.
"""
import math
import random


def with_supply_drops(base_episode):
    class SupplyEpisode(base_episode):
        def __init__(self, hello, layout, seed):
            super().__init__(hello, layout, seed)
            self.drop_rules = self.cfg["drops"]
            self.drop_rng = random.Random(f"supply-positions/{seed}")
            self.pods = {}
            self.called = 0
            self.taken_by_player = self.taken_by_rivals = 0

        def landing_point(self):
            center, radius = self.zone["next_center"], self.zone["next_radius"]
            for _ in range(2000):
                angle = self.drop_rng.uniform(0, math.tau)
                distance = radius * self.drop_rules["spread"] * math.sqrt(self.drop_rng.random())
                x = center[0] + distance * math.cos(angle)
                y = center[1] + distance * math.sin(angle)
                z = self.layout.ground(x, y)
                if (self.layout.walk(x, y, z, self.cfg["mech"]["radius"])
                        and self.layout.surface(x, y) <= z + self.cfg["mech"]["step_up"]):
                    return [x, y, z]
            raise ValueError("No open landing point found inside the next circle")

        def obs(self, tick):
            obs = super().obs(tick)
            rules = self.drop_rules
            while self.called < len(rules["times"]) and self.now >= rules["times"][self.called]:
                index = self.called
                self.called += 1
                self.pods[20000 + index] = {
                    "pos": self.landing_point(), "lands_at": rules["times"][index] + rules["fall_s"],
                    "holder": None, "progress": 0.0,
                }
            obs["drops"] = [
                {"id": ident, "pos": list(pod["pos"]), "radius": rules["radius"],
                 "capture_s": rules["capture_s"], "holder": pod["holder"],
                 "progress": pod["progress"], "lands_in": max(0.0, pod["lands_at"] - self.now)}
                for ident, pod in self.pods.items()
            ]
            return obs

        def award(self, actor, ident):
            # Apply the same cube stats and simplified upgrade choices as the
            # base referee. Supply cubes arrive together with one drop event.
            count = self.drop_rules["cubes"]
            rules, before = self.cfg["cubes"], actor.cubes
            actor.cubes += count
            scaled = min(actor.cubes, rules["stat_cap"]) - min(before, rules["stat_cap"])
            actor.max_hp += scaled * self.cfg["mech"]["hp"] * rules["hp_per_cube"]
            actor.hp = actor.max_hp
            actor.energy = actor.stats["energy"]
            actor.events.append({"type": "drop", "id": ident, "cubes": actor.cubes})
            if actor.offer is None and any(before < t <= actor.cubes for t in rules["upgrade_thresholds"]):
                actor.offer = {"id": actor.cubes, "expires": self.now + 5, "choices": [
                    {"id": key, "name": key, "desc": ""} for key in ("armor", "plating", "gun_damage")
                ]}
            if actor is self.player:
                self.taken_by_player += 1
            else:
                self.taken_by_rivals += 1

        def step_shots(self):
            # The base loop calls this once after movement of *all* actors
            # on every 20 Hz tick. Resolve occupancy once, in that same order.
            super().step_shots()
            for ident, pod in list(self.pods.items()):
                if self.now < pod["lands_at"]:
                    continue
                occupants = [a for a in self.actors.values() if a.kind == "mech" and a.hp > 0
                             and math.dist(a.pos[:2], pod["pos"][:2]) <= self.drop_rules["radius"]]
                if not occupants:
                    pod.update(holder=None, progress=0.0)
                elif len(occupants) == 1:
                    actor = occupants[0]
                    if pod["holder"] != actor.id:
                        pod.update(holder=actor.id, progress=0.0)
                    pod["progress"] += self.dt
                    if pod["progress"] + 1e-9 >= self.drop_rules["capture_s"]:
                        self.award(actor, ident)
                        del self.pods[ident]

        def run(self, bot, trace):
            result = super().run(bot, trace)
            result.update(supply_calls=self.called, player_captures=self.taken_by_player,
                          rival_captures=self.taken_by_rivals)
            return result

    return SupplyEpisode
