"""E27: a bounded mortar aiming window while the primary gun reloads.

decide returns None (use ordinary policy) or (aim, [gun, mortar]).
Projectile math uses published speed/gravity and muzzle offset; no engine API.
"""
import math

from navigation import _inside


def _blocked(prism, a, b):
    box, bottom, top, outer, holes, edges = prism
    # Max arc/chord separation is <0.068m at MAX_FLIGHT/16.
    bottom, top = bottom - .1, top + .1
    if (max(a[0], b[0]) < box[0] or min(a[0], b[0]) > box[2]
            or max(a[1], b[1]) < box[1] or min(a[1], b[1]) > box[3]
            or max(a[2], b[2]) < bottom or min(a[2], b[2]) > top):
        return False
    dx, dy, dz = (v - u for u, v in zip(a, b))
    cuts = [0., 1.]
    if abs(dz) > 1e-9:
        cuts.extend((z - a[2]) / dz for z in (bottom, top)
                    if 0. < (z - a[2]) / dz < 1.)
    for ax, ay, bx, by in edges:
        ex, ey = bx - ax, by - ay
        determinant = dx * ey - dy * ex
        if abs(determinant) < 1e-9:
            continue
        qx, qy = ax - a[0], ay - a[1]
        t = (qx * ey - qy * ex) / determinant
        u = (qx * dy - qy * dx) / determinant
        if 0. < t < 1. and 0. <= u <= 1.:
            cuts.append(t)
    cuts.sort()
    for left, right in zip(cuts, cuts[1:]):
        t = (left + right) / 2
        x, y, z = a[0] + dx * t, a[1] + dy * t, a[2] + dz * t
        if (bottom < z < top and _inside(x, y, outer)
                and not any(_inside(x, y, ring) for ring in holes)):
            return True
    return False


class Mortar:
    WINDOW = .4
    MAX_FLIGHT = 2.5

    def __init__(self, hello, navigator):
        self.config = hello.get('config', {})
        self.navigator = navigator
        self.target_id = None
        self.deadline = self.cooldown = 0.
        self.stats = dict(windows=0, shots=0, aborted=0, blocked=0, aiming_seconds=0.)
        self._last_t = None

    @staticmethod
    def solve(origin, target, velocity, speed, gravity, offset, ttl):
        """Earliest constant-velocity intercept, including forward muzzle offset."""
        if speed <= 0 or gravity <= 0 or ttl <= 0:
            return None
        limit = min(ttl, Mortar.MAX_FLIGHT)
        relative = tuple(v - u for u, v in zip(origin, target))

        def vector(t):
            return (relative[0] + velocity[0] * t,
                    relative[1] + velocity[1] * t,
                    relative[2] + velocity[2] * t + gravity * t * t / 2)

        def error(t):
            return math.hypot(*vector(t)) - offset - speed * t

        if error(0) <= 0:
            return None
        left = 0.
        for i in range(1, 51):
            right = limit * i / 50
            if error(right) <= 0:
                for _ in range(16):
                    mid = (left + right) / 2
                    if error(mid) > 0:
                        left = mid
                    else:
                        right = mid
                flight = (left + right) / 2
                delta = vector(flight)
                length = math.hypot(*delta)
                if length <= 0 or abs(error(flight)) > .01:
                    return None
                direction = tuple(v / length for v in delta)
                return (math.atan2(direction[1], direction[0]),
                        math.atan2(direction[2], math.hypot(*direction[:2])),
                        flight, direction)
            left = right
        return None

    def _clear(self, origin, direction, speed, gravity, offset, flight):
        nav = self.navigator
        if not getattr(nav, 'available', False):
            return False
        muzzle = tuple(origin[i] + offset * direction[i] for i in range(3))
        points = [origin, muzzle]
        for i in range(1, 17):
            t = flight * i / 16
            point = tuple(muzzle[j] + speed * t * direction[j]
                          - (gravity * t * t / 2 if j == 2 else 0) for j in range(3))
            if point[2] <= nav.ground(*point[:2]) + .2:
                return False
            points.append(point)
        # Broad-phase all map prisms, then cap the expensive exact intersections.
        bounds = (min(p[0] for p in points), min(p[1] for p in points),
                  max(p[0] for p in points), max(p[1] for p in points))
        prisms = []
        for prism in nav.prisms:
            b = prism[0]
            if not (b[2] < bounds[0] or b[0] > bounds[2] or b[3] < bounds[1] or b[1] > bounds[3]):
                prisms.append(prism)
                if len(prisms) > 24:
                    return False
        return not any(_blocked(p, a, b) for a, b in zip(points, points[1:]) for p in prisms)

    def _finish(self, now, failed):
        if failed:
            self.stats['aborted'] += 1
        self.target_id = None
        self.cooldown = now + .6

    def decide(self, obs, target):
        now, me = obs['t'], obs['self']
        if self._last_t is not None and self.target_id is not None:
            self.stats['aiming_seconds'] += max(0., now - self._last_t)
        self._last_t = now
        slots = me.get('weapons', ())
        active = self.target_id is not None
        if (len(slots) < 2 or not slots[0] or not slots[1]
                or slots[0].get('kind') != 'gun' or slots[1].get('kind') != 'mortar'
                or not target or target.get('kind') != 'mech' or target.get('hp', 1) <= 0
                or target.get('id') == me['id']):
            if active:
                self._finish(now, True)
            return None
        gun, mortar = slots[:2]
        if active and (target['id'] != self.target_id or now >= self.deadline
                       or gun.get('reload', 0) <= .15 or mortar.get('cooldown', 0) > 0):
            self._finish(now, True)
            return None
        if not active and (now < self.cooldown or gun.get('reload', 0) < .5
                           or mortar.get('cooldown', 0) > 0):
            return None
        mech = {**self.config.get('mech', {}), **me.get('stats', {})}
        weapon = {**self.config.get('weapons', {}).get('mortar', {}), **mortar.get('stats', {})}
        try:
            speed, gravity, ttl = (float(weapon[k]) for k in ('speed', 'gravity', 'ttl'))
            radius, height = float(mech['radius']), float(mech['muzzle_height'])
            pos, enemy = me['pos'], target['pos']
            distance = math.dist(pos[:2], enemy[:2])
            if not 50 <= distance <= 160 or distance <= weapon.get('splash_radius', 7) + radius + 3:
                raise ValueError('range')
            origin = (pos[0], pos[1], pos[2] + height)
            scale = 1 + self.config.get('cubes', {}).get('size_per_cube', .05) * target.get('cubes', 0)
            centre = (enemy[0], enemy[1], enemy[2] + self.config['mech']['height'] * scale / 2)
            velocity = tuple(target.get('vel', (0., 0., 0.)))
            values = (*origin, *centre, *velocity, speed, gravity, ttl, radius)
            if not all(math.isfinite(v) for v in values) or math.hypot(*velocity) > 35:
                raise ValueError('nonfinite or unstable')
            solution = self.solve(origin, centre, velocity, speed, gravity, radius + .5, ttl)
            if solution is None:
                raise ValueError('no intercept')
            yaw, pitch, flight, direction = solution
            if not mech['min_pitch'] <= pitch <= mech['max_pitch']:
                raise ValueError('pitch')
            if not self._clear(origin, direction, speed, gravity, radius + .5, flight):
                self.stats['blocked'] += 1
                raise ValueError('trajectory')
        except (KeyError, ValueError, TypeError, OverflowError, ZeroDivisionError):
            if active:
                self._finish(now, True)
            return None
        if not active:
            self.target_id, self.deadline = target['id'], now + self.WINDOW
            self.stats['windows'] += 1
        actual = me['aim']
        error_yaw = math.remainder(yaw - actual[0], math.tau)
        error_pitch = pitch - actual[1]
        tolerance = min(.04, 2 / max(1., speed * flight))
        if math.hypot(error_yaw * math.cos(pitch), error_pitch) <= tolerance:
            self.stats['shots'] += 1
            self._finish(now, False)
            return [yaw, pitch], [False, True]
        return [yaw, pitch], [False, False]
