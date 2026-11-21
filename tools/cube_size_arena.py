"""Optional cube-size correction for the independent offline referee.

The official mechfight/1 protocol, section 10, scales radius, height, eye and
muzzle height by ``1 + config.cubes.size_per_cube * cubes``, without a cap.
The HP/damage stat cap is unrelated. Published upgrades do not change these
four base dimensions. Feet coordinates and all other stats remain untouched.

Wrap this outside other resource extensions so both acquisition paths pass
through it: ``with_cube_size(with_supply_drops(SquadEpisode))``. This only
corrects the dimensions consumed by the existing referee; its collision,
visibility and projectile simulation remain approximations.
"""


def with_cube_size(base_episode):
    """Return an episode class scaling each mech after collect or supply award."""

    class CubeSizeEpisode(base_episode):
        def _apply_cube_size(self, actor):
            if actor.kind != "mech":
                return
            factor = 1.0 + self.cfg["cubes"]["size_per_cube"] * actor.cubes
            base = self.cfg["mech"]
            for field in ("radius", "height", "eye_height", "muzzle_height"):
                actor.stats[field] = base[field] * factor
            # Movement and hit cylinders use attributes; observations, eye
            # position and muzzle position use the public stats dictionary.
            actor.radius = actor.stats["radius"]
            actor.height = actor.stats["height"]

        def collect(self, actor, action):
            result = super().collect(actor, action)
            self._apply_cube_size(actor)
            return result

        def award(self, actor, ident):
            result = super().award(actor, ident)
            self._apply_cube_size(actor)
            return result

    return CubeSizeEpisode
