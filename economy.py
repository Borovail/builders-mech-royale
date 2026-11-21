"""Bounded ground-resource decisions for the official mechfight/1 protocol.

Economy does not aim or fire. ``decide(obs)`` returns a plain dictionary:
``destination`` is an (x, y) navigation goal, falling back to next_center;
``pickup_id`` is an explicit weapon take request (empty slots only);
``crate`` is the currently visible crate to shoot, or None;
``upgrade_index`` is an index into the current offer, or None. ``reason`` is
pickup/cube/crate while pursuing a resource, otherwise zone/danger/ground/none.

Cubes are collected automatically by the server. Crate firing belongs to the
caller, which must retain combat priority. In particular a crate is never
returned while an opponent mech is present, or shortly after taking damage.
Only direct, radius-aware ground approaches are requested; the navigator keeps
its existing role of reaching the selected destination.
"""
from __future__ import annotations

import math


class Economy:
    def __init__(self, hello, navigator):
        self.config = hello["config"]
        self.navigator = navigator
        self.reach = float(self.config["loot"]["pickup_radius"])
        self.cube_reach = float(self.config["cubes"].get("pickup_radius", 3.5))
        self.upgrades = {item["id"]: item for item in self.config["upgrades"]}
        self._memory = {}
        self._blocked_until = {}
        self._geometry = {}
        self._target = None
        self._started = self._progress_at = 0.0
        self._best_distance = math.inf
        self._last_damage = -math.inf
        self._next_plan = 0.0
        self._target_limit = 0.0

    def _release(self, now, cooldown=0.0):
        if self._target is not None and cooldown:
            self._blocked_until[self._target] = now + cooldown
        self._target = None
        self._next_plan = min(self._next_plan, now)

    def _refresh(self, obs):
        now, me = obs["t"], obs["self"]
        self._blocked_until = {key: end for key, end in self._blocked_until.items() if end > now}
        visible = set()
        for kind, field, lifetime in (("pickup", "pickups", 24.0),
                                      ("cube", "cubes", 16.0),
                                      ("crate", "crates", 6.0)):
            for item in obs.get(field, ()):
                key = (kind, item["id"])
                visible.add(key)
                self._memory[key] = (item, now, lifetime)
        for event in obs.get("events", ()):
            kind = event["type"]
            if (kind == "damaged" and event.get("weapon") != "zone") or kind == "effect":
                self._last_damage = now
            if kind in ("took", "take_failed"):
                key = ("pickup", event["id"])
                delay = {"range": 0.5, "busy": 0.5}.get(event.get("reason"), 20.0)
                self._blocked_until[key] = now + delay
                if kind == "took" or event.get("reason") in ("gone", "have"):
                    self._memory.pop(key, None)
                if self._target == key:
                    self._release(now)
            if kind == "hit" and self._target == ("crate", event.get("target")):
                self._progress_at = now
            if kind == "pickup" and self._target and self._target[0] == "cube":
                old = self._memory.get(self._target)
                if old and math.dist(me["pos"][:2], old[0]["pos"][:2]) <= self.cube_reach + 2:
                    self._blocked_until[self._target] = now + 20
                    self._memory.pop(self._target, None)
                    self._release(now)
        stale = []
        for key, (item, seen, lifetime) in self._memory.items():
            if now - seen > lifetime:
                stale.append(key)
            elif key not in visible:
                # Proximity vision is independent of where the torso points.
                # Do not circle a resource that another player already took.
                near = math.dist(me["pos"][:2], item["pos"][:2]) < 12
                opened = key[0] == "crate" and any(
                    math.dist(cube["pos"][:2], item["pos"][:2]) < 5
                    for cube in obs.get("cubes", ()))
                if near or opened:
                    stale.append(key)
                    self._blocked_until[key] = now + 15
        for key in stale:
            self._memory.pop(key, None)
            if self._target == key:
                self._release(now)
        # Actual matches contain only a few dozen resources; caps also bound
        # work if a future protocol stream includes many dropped weapons.
        if len(self._memory) > 128:
            newest = sorted(self._memory, key=lambda key: self._memory[key][1], reverse=True)[:128]
            self._memory = {key: self._memory[key] for key in newest}
        if len(self._geometry) > 64:
            self._geometry.clear()
        return visible

    @staticmethod
    def circle_at(obs, when):
        zone, now = obs["zone"], obs["t"]
        start = max(now, zone["shrink_start"])
        if when <= start:
            return zone["center"], zone["radius"]
        fraction = min(1.0, (when - start) / max(0.01, zone["shrink_end"] - start))
        center = tuple(a + fraction * (b - a) for a, b in zip(zone["center"], zone["next_center"]))
        radius = zone["radius"] + fraction * (zone["next_radius"] - zone["radius"])
        return center, radius

    def _zone_safe(self, point, obs, distance, speed, radius, work):
        """Reserve time for the detour and the walk back inside the next circle."""
        now, zone = obs["t"], obs["zone"]
        margin = radius + 2.0
        travel = distance * 1.25 / speed
        arrival = now + travel + work + 0.7
        center, size = self.circle_at(obs, arrival)
        if math.dist(point, center) + margin > size:
            return False
        # A target inside the next circle remains safe through this phase.
        radial = math.dist(point, zone["next_center"])
        if radial + margin <= zone["next_radius"]:
            return True
        escape = max(0.0, radial - max(0.0, zone["next_radius"] - margin))
        duration = escape * 1.4 / speed + 1.0
        # Check the whole return leg against the circle at the time we use it.
        for fraction in (0.0, 0.5, 1.0):
            step = min(radial, escape * fraction)
            q = tuple(a + (b - a) * step / radial for a, b in zip(point, zone["next_center"])) if radial else point
            center, size = self.circle_at(obs, arrival + duration * fraction)
            if math.dist(q, center) + margin > size:
                return False
        return True

    def _upgrade(self, obs):
        offer = obs.get("offer")
        if not offer or not offer.get("choices"):
            return None
        me = obs["self"]
        held = {weapon["kind"] for weapon in me["weapons"] if weapon}
        hp = me["hp"] / max(1.0, me["max_hp"])
        # IDs below are from the official config, not translated offer names.
        # Survival, sustained gun damage and reliable laser shots dominate
        # situational chain/piercing bonuses in a 16-mech survival tournament.
        priorities = {
            "plating": 91 + (12 if hp < 0.6 else 0), "armor": 94,
            "servos": 88, "nanites": 86 + (8 if hp < 0.75 else 0),
            "gun_damage": 82, "gun_rate": 79, "gun_mag": 77,
            "gun_velocity": 75, "laser_damage": 89, "laser_charge": 83,
            "lightning_range": 84, "lightning_efficiency": 78,
            "mortar_reload": 73, "mortar_splash": 70, "mortar_velocity": 52,
            "reactor": 76 if "lightning" in held else 65, "jets": 69,
            "capacitor": 58, "gyros": 62, "sensors": 61,
            "laser_pierce": 47, "lightning_chain": 46,
        }

        def score(choice):
            definition = self.upgrades.get(choice["id"], {})
            required = definition.get("requires")
            if required and required not in held:
                return -1.0
            if choice["id"] in priorities:
                return priorities[choice["id"]]
            # Future additions still get a useful ranking from exact effects.
            value = 0.0
            for effect in definition.get("effects", ()):
                path = effect["path"]
                change = float(effect.get("mul", 1.0)) - 1.0
                if path.endswith((".hp", ".damage", ".walk_speed", ".boost_speed")):
                    value += change * 100
                elif path.endswith((".cooldown", ".reload", ".charge", ".regen_delay")):
                    value -= change * 100 + float(effect.get("add", 0))
                elif path == "mech.armor":
                    value += float(effect.get("add", 0)) * 100
            return value

        return max(range(len(offer["choices"])), key=lambda index: score(offer["choices"][index]))

    def _candidate(self, key, item, obs, opponents, speed, radius):
        """Cheap utility/risk gates, before spending work on map geometry."""
        me, point = obs["self"], item["pos"][:2]
        pos, kind = me["pos"][:2], key[0]
        distance = math.dist(pos, point)
        goal = obs["zone"]["next_center"]
        extra = max(0.0, distance + math.dist(point, goal) - math.dist(pos, goal))
        held = {weapon["kind"] for weapon in me["weapons"] if weapon}
        empty = any(weapon is None for weapon in me["weapons"])
        cubes = me["cubes"]
        thresholds = self.config["cubes"]["upgrade_thresholds"]
        to_offer = next((number - cubes for number in thresholds if number > cubes), 99)
        work = 0.0
        if kind == "pickup":
            if not empty or item["kind"] in held or item["kind"] == "gun":
                return None
            value = {"laser": 5.5, "lightning": 3.2, "mortar": 2.6}.get(item["kind"], 0)
            limit = 160.0 if item["kind"] == "laser" else 115.0
            extra_limit = speed * (14 if item["kind"] == "laser" else 8)
        elif kind == "cube":
            value = (1.8 if cubes < 10 else 0.8) + (1.2 if to_offer <= 1 else 0.4 if to_offer <= 2 else 0)
            nearby = sum(math.dist(point, cube["pos"][:2]) < 10 for cube in obs.get("cubes", ())[:64])
            value += min(1.5, max(0, nearby - 1) * 0.5)
            limit, extra_limit = (110.0 if cubes < 10 else 65.0), speed * 7
        else:
            if opponents or me["hp"] < 0.65 * me["max_hp"]:
                return None
            gun = next((weapon for weapon in me["weapons"] if weapon and weapon["kind"] == "gun"), None)
            if gun is None:
                return None
            stats = gun["stats"]
            damage = stats["damage"] * (1 + self.config["cubes"].get("damage_per_cube", 0.04)
                                         * min(cubes, self.config["cubes"].get("stat_cap", 10)))
            shots = math.ceil(float(item.get("hp", self.config["crates"]["hp"])) / max(1, damage))
            work = shots * stats["cooldown"] + distance / stats.get("speed", 252)
            if gun.get("ammo", shots) < shots:
                work += stats.get("reload", 2.4)
            value = (1.6 if cubes < 10 else 0.7) + (1.0 if to_offer <= 1 else 0)
            value += 1.4 if empty else 0
            limit, extra_limit = 100.0, speed * 6
        if distance > limit or extra > extra_limit:
            return None
        if me["hp"] < 0.45 * me["max_hp"] and distance > (30 if kind == "cube" else 20):
            return None
        if opponents:
            # A distant contact still permits a small useful diversion while
            # moving to safety. Never run toward a rival to win a loot race.
            if distance > 60 or extra > speed * 1.7:
                return None
            for actor in opponents:
                before, after = math.dist(pos, actor["pos"][:2]), math.dist(point, actor["pos"][:2])
                if before < 65 or after < 80 or after < before - 15:
                    return None
        for actor in obs.get("mechs", ()):
            if actor["kind"] == "npc" and math.dist(point, actor["pos"][:2]) < 48:
                return None
        if not self._zone_safe(point, obs, distance, speed, radius, work):
            return None
        # Extra journey matters more than distance when the item is on route.
        score = value / (2.0 + (0.65 * distance + 0.8 * extra) / speed + work)
        return score, distance, work

    def _accessible(self, key, item, me, now, radius):
        nav, point, pos = self.navigator, item["pos"], me["pos"]
        if abs(point[2] - nav.ground(*point[:2])) > nav.step_up + 0.2:
            return False
        cached = self._geometry.get(key)
        if cached and now - cached[0] < 1.0 and math.dist(pos[:2], cached[1]) < 8 and cached[2] == radius:
            return cached[3]
        # The public hook shares the navigator's loaded map, no second graph.
        result = nav.clear_ground_segment(pos[:2], point[:2], radius)
        self._geometry[key] = (now, tuple(pos[:2]), radius, result)
        return result

    def _decision(self, obs, key, item, visible, pick):
        pos, point = obs["self"]["pos"][:2], item["pos"][:2]
        kind = key[0]
        crate = None
        destination = tuple(point)
        if kind == "crate":
            distance = math.dist(pos, point)
            # Keep our cylinder away from the intact crate. Once it breaks,
            # the resulting cube/weapon becomes a normal resource target.
            stop = max(7.0, obs["self"]["stats"]["radius"] + self.config["crates"].get("radius", 1.4) + 2)
            destination = tuple(pos) if distance <= stop else tuple(
                p + (a - p) * stop / distance for a, p in zip(pos, point))
            if key in visible:
                crate = item
        return {"destination": destination, "pickup_id": None, "crate": crate,
                "upgrade_index": pick, "reason": kind}

    def decide(self, obs):
        me, now = obs["self"], obs["t"]
        visible = self._refresh(obs)
        pick = self._upgrade(obs)
        fallback = {"destination": tuple(obs["zone"]["next_center"]),
                    "pickup_id": None, "crate": None, "upgrade_index": pick, "reason": "none"}
        held = {weapon["kind"] for weapon in me["weapons"] if weapon}
        if any(weapon is None for weapon in me["weapons"]):
            near = [item for item in obs.get("pickups", ())
                    if item["kind"] not in held and item["kind"] != "gun"
                    and ("pickup", item["id"]) not in self._blocked_until
                    and math.dist(me["pos"][:2], item["pos"][:2]) <= self.reach
                    and abs(me["pos"][2] - item["pos"][2]) <= 4]
            if near:
                item = max(near, key=lambda p: {"laser": 3, "lightning": 2, "mortar": 1}.get(p["kind"], 0))
                self._blocked_until[("pickup", item["id"])] = now + 0.5
                self._release(now)
                fallback.update(pickup_id=item["id"], reason="pickup")
                return fallback
        if not me["in_zone"]:
            self._release(now)
            fallback["reason"] = "zone"
            return fallback
        if now - self._last_damage < 2.5:
            self._release(now)
            fallback["reason"] = "danger"
            return fallback
        nav = self.navigator
        if (not nav.available or me["pos"][2] > nav.ground(*me["pos"][:2]) + nav.air_height
                or (me["grounded"] and abs(me["pos"][2] - nav.ground(*me["pos"][:2])) > nav.step_up + 0.2)):
            self._release(now)
            fallback["reason"] = "ground"
            return fallback
        opponents = [actor for actor in obs.get("mechs", ()) if actor["kind"] == "mech"]
        speed = max(1.0, float(me["stats"].get("walk_speed", self.config["mech"]["walk_speed"])))
        radius = float(me["stats"]["radius"])
        if self._target is not None:
            entry = self._memory.get(self._target)
            candidate = self._candidate(self._target, entry[0], obs, opponents, speed, radius) if entry else None
            if candidate:
                _, distance, _ = candidate
                if distance < self._best_distance - 0.6:
                    self._best_distance, self._progress_at = distance, now
                if now - self._started > self._target_limit or now - self._progress_at > 2.5:
                    self._release(now, 12.0)
                elif self._accessible(self._target, entry[0], me, now, radius):
                    return self._decision(obs, self._target, entry[0], visible, pick)
                else:
                    self._release(now, 5.0)
            else:
                self._release(now)
        if now < self._next_plan:
            return fallback
        self._next_plan = now + 0.5
        candidates = []
        for key, (item, _, _) in self._memory.items():
            if key in self._blocked_until:
                continue
            candidate = self._candidate(key, item, obs, opponents, speed, radius)
            if candidate:
                candidates.append((candidate[0], key, item, candidate[1], candidate[2]))
        # Geometry sampling is bounded to four candidate approaches per half
        # second. Active targets use one cached check per second / 8 m moved.
        candidates.sort(key=lambda row: row[0], reverse=True)
        for _, key, item, distance, work in candidates[:4]:
            if self._accessible(key, item, me, now, radius):
                self._target = key
                self._started = self._progress_at = now
                self._best_distance = distance
                self._target_limit = min(30.0, 1.7 * distance / speed + work + 5)
                return self._decision(obs, key, item, visible, pick)
        if opponents:
            fallback["reason"] = "danger"
        return fallback
