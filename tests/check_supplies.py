"""Independent E10 A/B checks through the public Bot(hello).act(obs) interface.

python -B tests/check_supplies.py --kit KIT --baseline E07/example_bot.py

This is a bounded ground/resource fixture, never a match or tournament score.
It uses the official hello, replay, map, acceleration and energy constants and
check_routes' swept-prism/slope checks. Jump/air physics, combat, regeneration
of HP and upgrade offers are omitted to isolate supply capture. Supply clocks
follow protocol section 7: landing, horizontal ring membership, sole holder,
freeze on contest and reset on vacancy/holder change. No vertical exclusion is
specified for supply rings; an elevated opponent still contests horizontally.
HP/max HP and size are updated for three cubes only on completed capture;
full repair and energy likewise occur only then. Cube upgrades are not drawn.
"""

import argparse
import copy
import gzip
import hashlib
import json
import math
from pathlib import Path
import tempfile

from check_routes import EPS, load_kit
from check_mobility import energy_step
from check_economy import (action, aligned, fixture, load_bot, observation,
                           roof_fixture, source_hashes)


class CaptureClock:
    """Protocol clock independent of any candidate's decision implementation."""

    def __init__(self, config, pos, lands_at=0.0):
        self.id, self.pos, self.lands_at = 20001, list(pos), lands_at
        self.radius, self.capture_s = config["radius"], config["capture_s"]
        self.progress, self.holder, self.taken = 0.0, None, False

    def obs(self, now):
        return {"id": self.id, "pos": list(self.pos), "radius": self.radius,
                "lands_in": max(0.0, self.lands_at-now),
                "capture_s": self.capture_s, "progress": self.progress,
                "holder": self.holder}

    def step(self, now, dt, members):
        if self.taken or now+EPS < self.lands_at:
            return None
        # Position is ignored vertically: protocol says ring, without height.
        occupants = [ident for ident, pos in members
                     if math.dist(pos[:2], self.pos[:2]) <= self.radius+EPS]
        if not occupants:
            self.holder, self.progress = None, 0.0
        elif len(occupants) == 1:
            if occupants[0] != self.holder:
                self.holder, self.progress = occupants[0], 0.0
            self.progress += dt
            if self.progress+EPS >= self.capture_s:
                self.taken = True
                return self.holder
        return None


def clock_checks(config):
    clock = CaptureClock(config, [0, 0, 0], lands_at=2)
    here, other, above, away = (1, [0, 0, 0]), (2, [0, 0, 0]), (2, [0, 0, 40]), (1, [20, 0, 0])
    checks = []
    def check(name, ok):
        checks.append({"scenario": "clock_"+name, "passed": bool(ok)})
    clock.step(0, 1, [here]); check("falling_no_progress", clock.progress == 0)
    clock.step(2, 1, [here]); check("sole_progress", clock.progress == 1 and clock.holder == 1)
    clock.step(3, 1, [here, other]); check("contest_freezes", clock.progress == 1)
    clock.step(4, 1, [here, above]); check("height_does_not_remove_contest", clock.progress == 1)
    clock.step(5, 1, [away]); check("vacancy_resets", clock.progress == 0 and clock.holder is None)
    clock.step(6, 1, [here]); clock.step(7, 1, [other])
    check("holder_change_resets", clock.progress == 1 and clock.holder == 2)
    winners = [clock.step(8+i, 1, [other]) for i in range(3)]
    check("four_seconds_required", winners == [None, None, 2] and clock.taken)
    check("one_reward_only", clock.step(11, 1, [other]) is None)
    return checks


def run(factory, hello, template, arena, index=0, falling=False, disappear=False):
    start, target, goal = fixture(index)
    config, stats = hello["config"], copy.deepcopy(hello["config"]["mech"])
    hz, every = config["match"]["tick_hz"], config["match"]["decide_every"]
    dt = 1/hz
    state = observation(hello, template, arena, 0, start, goal)["self"]
    state.update(hp=700.0, energy=30.0, cubes=0)
    clock = CaptureClock(config["drops"], [*target, arena.height(target)], 9.0 if falling else 0.0)
    bot, last_spend = factory(copy.deepcopy(hello)), -math.inf
    command, events = {"move": [0, 0]}, []
    captured, vanished, resumed, reached, entered = None, None, None, None, None
    blocked, hold_jumps, hold_boosts = 0, 0, 0
    path, maximum_progress, pre_capture_hp, reward = 0.0, 0.0, 700.0, None
    for tick in range(int(26*hz)):
        now, position = tick/hz, state["pos"]
        if disappear and vanished is None and math.dist(position[:2], target) <= clock.radius+2:
            vanished = now
        if tick % every == 0:
            obs = observation(hello, template, arena, tick, position[:2], goal)
            obs["self"], obs["events"] = copy.deepcopy(state), events
            events = []
            obs["drops"] = [] if clock.taken or vanished is not None else [clock.obs(now)]
            command = action(bot, obs)
            in_ring = math.dist(position[:2], target) <= clock.radius
            if in_ring and not clock.taken and vanished is None:
                hold_jumps += bool(command.get("jump"))
                hold_boosts += bool(command.get("boost"))
            if (captured is not None or vanished is not None) and aligned(command["move"], position[:2], goal):
                resumed = now if resumed is None else resumed
        move = command["move"]
        boosting = bool(command.get("boost")) and state["energy"] >= stats["boost_drain"]*dt and math.hypot(*move) > EPS
        speed = stats["boost_speed"] if boosting else stats["walk_speed"]
        delta = [speed*move[i]-state["vel"][i] for i in range(2)]
        length = math.hypot(*delta)
        fraction = min(1, stats["ground_accel"]*dt/length) if length else 0
        velocity = [state["vel"][i]+delta[i]*fraction for i in range(2)]
        proposed = [position[i]+velocity[i]*dt for i in range(2)]
        if (arena.clearance(position[:2], proposed, stats["radius"], stats["height"])[0] < -EPS
                or arena.slope(position[:2], proposed) > stats["max_slope_deg"]+EPS):
            blocked += 1
            velocity = [0, 0]
        else:
            path += math.dist(position[:2], proposed)
            state["pos"] = [*proposed, arena.height(proposed)]
        state["vel"] = [*velocity, 0]
        state["energy"], last_spend = energy_step(state["energy"], state["max_energy"], boosting, now, last_spend, dt, stats)
        if not clock.taken and vanished is None:
            if math.dist(state["pos"][:2], target) <= clock.radius and entered is None:
                # Start of the first capture-accounting frame. The bounded
                # model samples ring membership after each movement frame.
                entered = now
            winner = clock.step(now, dt, [(state["id"], state["pos"])])
            maximum_progress = max(maximum_progress, clock.progress)
            if winner == state["id"]:
                pre_capture_hp = state["hp"]
                captured = (tick+1)/hz
                state["cubes"] += config["drops"]["cubes"]
                count = min(state["cubes"], config["cubes"]["stat_cap"])
                state["max_hp"] = config["mech"]["hp"]*(1+config["cubes"]["hp_per_cube"]*count)
                state["hp"], state["energy"] = state["max_hp"], state["max_energy"]
                for key in ("radius", "height", "eye_height", "muzzle_height"):
                    stats[key] = config["mech"][key]*(1+config["cubes"]["size_per_cube"]*state["cubes"])
                state["stats"] = copy.deepcopy(stats)
                reward = {"hp": state["hp"], "energy": state["energy"], "cubes": state["cubes"]}
                events.append({"type": "drop", "id": clock.id, "cubes": state["cubes"]})
        if math.dist(state["pos"][:2], goal) <= 1:
            reached = (tick+1)/hz
            break
    return {"scenario": ("falling_" if falling else "vanished_" if disappear else "landed_")+str(index),
            "distance_to_supply_m": round(math.dist(start, target), 3), "capture_at_s": captured,
            "first_ring_entry_s": entered, "maximum_progress_s": round(maximum_progress, 3),
            "hp_before_capture": pre_capture_hp, "reward": reward, "cubes": state["cubes"],
            "vanished_at_s": vanished, "resumed_at_s": resumed, "goal_at_s": reached,
            "blocked_frames": blocked, "ring_jump_requests": hold_jumps, "ring_boost_requests": hold_boosts,
            "distance_travelled_m": round(path, 3)}


def safety(factory, hello, template, arena):
    start, target, goal = fixture(0)
    every = int(hello["config"]["match"]["decide_every"])
    results = []
    for mode in ("disappeared", "damaged", "opponent", "contest", "elevated_contest", "zone_deadline", "stalled"):
        bot = factory(copy.deepcopy(hello))
        first = observation(hello, template, arena, every, start, goal)
        first["self"].update(hp=700, energy=30)
        drop = CaptureClock(hello["config"]["drops"], [*target, arena.height(target)])
        first["drops"] = [drop.obs(first["t"])]
        committed = not aligned(action(bot, first)["move"], start, goal)
        commands, positions = [], []
        ticks = range(2*every, int(16*hello["config"]["match"]["tick_hz"]), every) if mode == "stalled" else [2*every]
        for tick in ticks:
            obs = observation(hello, template, arena, tick, start, goal)
            obs["self"].update(hp=700, energy=30)
            obs["drops"] = [drop.obs(obs["t"])]
            if mode == "disappeared":
                obs["drops"] = []
            if mode == "damaged":
                obs["events"] = [{"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 0, "by": 5}]
            if mode in ("opponent", "contest", "elevated_contest"):
                point = (start[0]+15, start[1]) if mode == "opponent" else target
                obs["mechs"] = [{"id": 5, "kind": "mech", "pos": [*point, arena.height(point)+(40 if mode == "elevated_contest" else 0)],
                                  "vel": [0, 0, 0], "aim": [0, 0], "hp": 1400, "max_hp": 1400, "weapons": ["gun"]}]
                if mode != "opponent":
                    obs["drops"][0].update(holder=5, progress=2)
            if mode == "zone_deadline":
                obs["zone"].update(next_radius=10, shrink_start=-10, shrink_end=obs["t"]+1)
            commands.append(action(bot, obs))
            positions.append(start)
        fallback = [aligned(cmd["move"], pos, goal) for cmd, pos in zip(commands, positions)]
        passed = committed and (all(fallback[-10:]) if mode == "stalled" else all(fallback))
        results.append({"scenario": "cancel_"+mode, "initial_committed": committed,
                        "fallback_actions": sum(fallback), "observations": len(commands), "passed": bool(passed)})
    start, goal, elevated, prism = roof_fixture(arena, hello["config"]["mech"])
    obs = observation(hello, template, arena, every, start, goal)
    obs["self"].update(hp=700, energy=30)
    obs["drops"] = [CaptureClock(hello["config"]["drops"], elevated["pos"]).obs(obs["t"])]
    cmd = action(factory(copy.deepcopy(hello)), obs)
    results.append({"scenario": "reject_official_roof", "prism": prism,
                    "passed": aligned(cmd["move"], start, goal)})
    start, target, goal = fixture(0)
    for mode in ("critical_hp_far", "long_fall", "no_resource_need", "outside_zone", "airborne"):
        obs = observation(hello, template, arena, every, start, goal)
        obs["self"].update(hp=700, energy=30)
        obs["drops"] = [CaptureClock(hello["config"]["drops"], [*target, arena.height(target)]).obs(obs["t"])]
        if mode == "critical_hp_far":
            obs["self"]["hp"] = 400
        elif mode == "long_fall":
            obs["drops"][0]["lands_in"] = 25
        elif mode == "no_resource_need":
            obs["self"].update(hp=obs["self"]["max_hp"], energy=100, cubes=45)
        elif mode == "outside_zone":
            obs["self"]["in_zone"] = False
        elif mode == "airborne":
            obs["self"]["grounded"] = False
            obs["self"]["pos"][2] += 15
        cmd = action(factory(copy.deepcopy(hello)), obs)
        results.append({"scenario": "reject_"+mode, "passed": aligned(cmd["move"], start, goal)})
    for mode in ("frozen_clock", "reset_clock"):
        bot = factory(copy.deepcopy(hello))
        initial = observation(hello, template, arena, every, target, goal)
        initial["self"].update(hp=700, energy=30)
        drop = CaptureClock(hello["config"]["drops"], [*target, arena.height(target)])
        item = drop.obs(initial["t"])
        item.update(holder=initial["self"]["id"], progress=2.0)
        initial["drops"] = [copy.deepcopy(item)]
        command = action(bot, initial)
        held = (math.hypot(*command["move"]) <= EPS and not command.get("jump") and not command.get("boost"))
        fallback = []
        for tick in range(2*every, int(4*hello["config"]["match"]["tick_hz"]), every):
            obs = observation(hello, template, arena, tick, target, goal)
            obs["self"].update(hp=700, energy=30)
            obs["drops"] = [copy.deepcopy(item)]
            if mode == "reset_clock":
                obs["drops"][0].update(holder=None, progress=0)
            cmd = action(bot, obs)
            fallback.append(aligned(cmd["move"], target, goal))
        results.append({"scenario": "cancel_"+mode, "initial_held": held,
                        "fallback_actions": sum(fallback), "observations": len(fallback),
                        "passed": held and all(fallback[-10:])})
    return results


def replay_equivalence(factory, baseline, hello):
    class PlanningClock:
        # The navigator uses a wall-clock A* slice. Fixed planning cost gives
        # both snapshots equal search budgets despite OS scheduling noise.
        # This changes this equivalence check only; it measures no latency.
        def __init__(self):
            self.value = 0.0

        def perf_counter(self):
            self.value += 0.000001
            return self.value

    navigation_globals = [f.__init__.__globals__["Navigator"].direction.__globals__
                          for f in (factory, baseline)]
    previous_times = [g["time"] for g in navigation_globals]
    for g in navigation_globals:
        g["time"] = PlanningClock()
    try:
        return _replay_equivalence(factory, baseline, hello)
    finally:
        for g, real_time in zip(navigation_globals, previous_times):
            g["time"] = real_time


def _replay_equivalence(factory, baseline, hello):
    candidate, previous = factory(copy.deepcopy(hello)), baseline(copy.deepcopy(hello))
    path = Path(hello["map"]["dir"]).parent/"check/stream.jsonl.gz"
    different, count = [], 0
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        next(stream)
        for line in stream:
            obs = json.loads(line)
            if obs.get("type") != "obs":
                continue
            obs["drops"] = []
            a, b = action(candidate, copy.deepcopy(obs)), action(previous, copy.deepcopy(obs))
            count += 1
            if a != b:
                different.append(obs["tick"])
    return {"scenario": "no_drops_official_replay", "observations": count,
            "different_actions": len(different), "first_differing_ticks": different[:10],
            "planning_clock": "one_microsecond_per_perf_counter_call", "passed": not different}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", type=Path, required=True)
    parser.add_argument("--bot", type=Path, default=Path(__file__).resolve().parents[1]/"example_bot.py")
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mech-supply-check-") as directory:
        hello, template, arena = load_kit(args.kit.resolve(), Path(directory))
        factory, baseline = load_bot(args.bot, "supply_candidate"), load_bot(args.baseline, "supply_baseline")
        print("model=bounded-ground-supply-fixture score=none combat=none hp_regen=omitted air=omitted")
        print("candidate_sources="+json.dumps(source_hashes(args.bot), sort_keys=True))
        print("baseline_sources="+json.dumps(source_hashes(args.baseline), sort_keys=True))
        print("world_sha256="+arena.world_sha256)
        print("terrain_sha256="+hashlib.sha256(arena.raw).hexdigest())
        protocol = Path(hello["map"]["dir"]).parent/"protocol.md"
        print("protocol_sha256="+hashlib.sha256(protocol.read_bytes()).hexdigest() if protocol.is_file() else "protocol_source=official-kit-section-7")
        stats = hello["config"]["mech"]
        for i in range(3):
            start, target, goal = fixture(i)
            for a, b in ((start, target), (target, goal), (start, goal)):
                if (arena.clearance(a, b, stats["radius"], stats["height"])[0] <= 0 or
                        arena.slope(a, b) > stats["max_slope_deg"]):
                    raise ValueError(f"Fixture {i} is not a clear ground lane")
        checks = clock_checks(hello["config"]["drops"])
        checks += [replay_equivalence(factory, baseline, hello)]
        base_results, results = [], []
        for index, falling, disappear in [(0, False, False), (1, False, False), (2, False, False), (0, True, False), (0, False, True)]:
            base_results.append(run(baseline, hello, template, arena, index, falling, disappear))
            r = run(factory, hello, template, arena, index, falling, disappear)
            if disappear:
                r["passed"] = r["vanished_at_s"] is not None and r["cubes"] == 0 and r["resumed_at_s"] is not None and r["goal_at_s"] is not None
            else:
                r["passed"] = (r["capture_at_s"] is not None and r["cubes"] == hello["config"]["drops"]["cubes"]
                               and r["hp_before_capture"] == 700 and r["reward"]["hp"] > 1400
                               and r["reward"]["energy"] == 100 and r["resumed_at_s"] is not None
                               and r["goal_at_s"] is not None and r["blocked_frames"] == 0
                               and r["ring_jump_requests"] == r["ring_boost_requests"] == 0)
            results.append(r)
        checks += safety(factory, hello, template, arena)
        for result in base_results:
            print("baseline="+json.dumps(result, sort_keys=True))
        for result in results+checks:
            print("candidate="+json.dumps(result, sort_keys=True))
        passed = all(r["passed"] for r in results+checks)
        print("totals="+json.dumps({"candidate_captures": sum(r["capture_at_s"] is not None for r in results),
                                  "baseline_captures": sum(r["capture_at_s"] is not None for r in base_results),
                                  "passed": passed}, sort_keys=True))
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
