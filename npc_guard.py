"""E32: aim at a recent tracked NPC attacker without choosing movement.

NPC damage events identify the source in ``by``. Aim from the current
gun muzzle at half the drone's configured height above its observed feet.
"""
import math


class NpcGuard:
    def __init__(self, hello):
        config = hello.get('config', {})
        self.npc_height = float(config.get('npc', {}).get('height', 3.5))
        self.muzzle_height = float(config.get('mech', {}).get('muzzle_height', 7.0))
        self.shooter_id = None
        self.last_npc_damage = -math.inf
        self.last_other_damage = -math.inf

    def aim(self, obs, enemies):
        now, me = obs['t'], obs['self']
        if now > 60.0:
            return None
        # Refresh even when defensive aiming is currently disallowed.
        for event in obs.get('events', ()):
            if event.get('type') != 'damaged' or event.get('amount', 0) <= 0:
                continue
            weapon = event.get('weapon')
            if weapon == 'npc':
                shooter = event.get('by')
                if shooter is not None:
                    self.shooter_id = shooter
                    self.last_npc_damage = now
            elif weapon != 'zone':
                self.last_other_damage = now

        slots = me.get('weapons', ())
        if (not me.get('in_zone') or me.get('max_hp', 0) <= 0
                or me.get('hp', 0) < .65 * me['max_hp']
                or not slots or not slots[0] or slots[0].get('kind') != 'gun'
                or (len(slots) > 1 and slots[1] and slots[1].get('kind') == 'laser'
                    and slots[1].get('charge', 0) > 0)
                or now - self.last_npc_damage > 2.0 + 1e-9
                or now - self.last_other_damage <= 3.0 + 1e-9):
            return None
        position = me['pos']
        if any(enemy.get('kind') == 'mech' and enemy.get('hp', 1) > 0
               and math.dist(position[:2], enemy['pos'][:2]) <= 70.0
               for enemy in enemies):
            return None
        shooter = next((actor for actor in obs.get('mechs', ())
                        if actor.get('kind') == 'npc' and actor.get('id') == self.shooter_id
                        and actor.get('hp', 1) > 0), None)
        if shooter is None or math.dist(position, shooter['pos']) > 50.0:
            return None
        target = shooter['pos']
        muzzle = float(me.get('stats', {}).get('muzzle_height', self.muzzle_height))
        dx, dy = target[0] - position[0], target[1] - position[1]
        dz = target[2] + self.npc_height / 2 - position[2] - muzzle
        if not all(math.isfinite(value) for value in (dx, dy, dz)):
            return None
        return [math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))]
