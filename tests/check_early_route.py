"""Independent E16 public-action policy scenarios on the official arena.

This checks directions and policy priorities, not pickup frequency, survival or
match score. No movement/combat simulator is used. --baseline is exact E10.
"""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile

from check_economy import action, ground_item, load_bot, observation
from check_routes import direct_move, load_kit


class PlanningClock:
    def __init__(self):
        self.value = 0.0

    def perf_counter(self):
        self.value += 0.000001
        return self.value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kit', type=Path, required=True)
    parser.add_argument('--bot', type=Path, default=Path(__file__).resolve().parents[1]/'example_bot.py')
    parser.add_argument('--baseline', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='mech-early-check-') as temporary:
        hello, template, arena = load_kit(args.kit.resolve(), Path(temporary))
        candidate = load_bot(args.bot, 'early_candidate')
        baseline = load_bot(args.baseline, 'early_baseline')
        # Equal bounded A* work eliminates wall-clock scheduling differences.
        for factory in (candidate, baseline):
            factory.__init__.__globals__['Navigator'].direction.__globals__['time'] = PlanningClock()
        start, goal = (-150.0, 64.0), (-50.0, 64.0)
        tick = int(hello['config']['match']['tick_hz'])

        def obs(position=start, target=goal, at=tick):
            return observation(hello, template, arena, at, position, target)

        def command(factory, value):
            return action(factory(copy.deepcopy(hello)), copy.deepcopy(value))

        checks = []

        def record(name, passed, **values):
            checks.append(dict(scenario=name, passed=bool(passed), **values))

        initial = obs()
        bot = candidate(copy.deepcopy(hello))
        a = action(bot, copy.deepcopy(initial))
        b = command(baseline, initial)
        direction = a['move']
        waypoint = tuple(start[i]+30.0*direction[i] for i in range(2))
        stats = hello['config']['mech']
        clearance = arena.clearance(start, waypoint, stats['radius'], stats['height'])[0]
        onward = tuple(start[i]+60.0*direction[i] for i in range(2))
        onward_clearance = arena.clearance(start, onward, stats['radius'], stats['height'])[0]
        slope = arena.slope(start, onward)
        # The E10 goal is blocked; E16 should choose a substantially sideways,
        # still forward, clear 30m waypoint rather than the old grid direction.
        desired = (0.4 < direction[0] < 0.95 and abs(direction[1]) > 0.4
                   and math.dist(a['move'], b['move']) > 0.05
                   and math.dist(waypoint, goal) < math.dist(start, goal)
                   and clearance > 0 and onward_clearance > 0
                   and slope <= stats['max_slope_deg'])
        record('early_opening_waypoint', desired, candidate_move=a['move'], baseline_move=b['move'],
               waypoint=list(waypoint), clearance_m=clearance, onward_clearance_m=onward_clearance,
               max_slope_deg=slope)
        record('only_movement_changes', {k:v for k,v in a.items() if k != 'move'} ==
               {k:v for k,v in b.items() if k != 'move'})
        shifted = (start[0]+direction[0]-0.2*direction[1],
                   start[1]+direction[1]+0.2*direction[0])
        follow = obs(shifted, at=tick+2)
        next_action = action(bot, follow)
        record('stable_waypoint_after_progress', math.dist(next_action['move'], direct_move(shifted, waypoint)) < 1e-7)
        record('deterministic_initial_action', all(command(candidate, initial) == a for _ in range(3)))

        cases = []
        value = obs((-280.0, -280.0), (-180.0, -280.0))
        cases.append(('clear_lane', value))
        for name in ('outside_zone', 'airborne', 'visible_enemy', 'filled_slot',
                     'after_45_seconds', 'urgent_zone', 'resource_priority', 'cube_priority',
                     'crate_priority', 'incoming_damage'):
            value = obs()
            if name == 'outside_zone':
                value['self']['in_zone'] = False
            elif name == 'airborne':
                value['self']['grounded'] = False
                value['self']['pos'][2] += 5
            elif name == 'visible_enemy':
                value['mechs'] = [{'id': 5, 'kind': 'mech', 'pos': [-130, 64, arena.height((-130,64))],
                                  'vel': [0,0,0], 'aim': [math.pi,0], 'hp': 1400,
                                  'max_hp': 1400, 'weapons': ['gun']}]
            elif name == 'filled_slot':
                value['self']['weapons'][1] = {'kind':'lightning', 'cooldown':0,
                    'stats':copy.deepcopy(hello['config']['weapons']['lightning'])}
            elif name == 'after_45_seconds':
                value['t'], value['tick'] = 45.0, 45*tick
            elif name == 'urgent_zone':
                value['zone'].update(radius=20, next_radius=10, shrink_start=-10, shrink_end=3)
            elif name == 'resource_priority':
                value['pickups'] = [ground_item(arena, (-148,70))]
            elif name in ('cube_priority', 'crate_priority'):
                kind = name.split('_')[0]
                item = ground_item(arena, (-148,70), kind)
                if kind == 'crate':
                    item['hp'] = hello['config']['crates']['hp']
                value['cubes' if kind == 'cube' else 'crates'] = [item]
            elif name == 'incoming_damage':
                value['events'] = [{'type':'damaged','amount':45,'weapon':'gun','by':5,'bearing':0}]
            cases.append((name,value))
        for name,value in cases:
            ca, ba = command(candidate,value), command(baseline,value)
            record(name, ca == ba, candidate_move=ca['move'], baseline_move=ba['move'])
        print('model=public-action-fixtures planning_clock=one-microsecond-per-call')
        print('world_sha256='+arena.world_sha256)
        print('candidate_sha256='+hashlib.sha256(args.bot.read_bytes()).hexdigest())
        if (args.bot.parent/'early_route.py').is_file():
            print('early_route_sha256='+hashlib.sha256((args.bot.parent/'early_route.py').read_bytes()).hexdigest())
        print('baseline_sha256='+hashlib.sha256(args.baseline.read_bytes()).hexdigest())
        for check in checks:
            print(json.dumps(check, sort_keys=True))
        passed = all(c['passed'] for c in checks)
        print('PASS: E16 public policy scenarios' if passed else 'FAIL: E16 public policy scenarios')
        return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
