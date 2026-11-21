"""Public Bot.act E17 regression against exact E10, using the official map.

Run: python tests/check_emergency_mobility.py --kit /path/to/mechfight-kit
This checks commands and bounded energy accounting, not match survival or score.
"""

import argparse
import copy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile

from check_mobility import START, action, energy_step, observation
from check_routes import EPS, load_kit


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ("economy", "mechfight", "mobility", "navigation", "secondary", "supply")


def factory(path, name):
    """Load each complete runtime independently; imports must not share Mobility."""
    for module in RUNTIME:
        sys.modules.pop(module, None)
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.Bot
    finally:
        sys.path.pop(0)


def run_checks(baseline, candidate, hello, template, arena):
    stats = hello["config"]["mech"]
    hz = hello["config"]["match"]["tick_hz"]
    dt = hello["config"]["match"]["decide_every"] / hz
    drain = stats["boost_drain"]
    start = 1.5 * drain
    stop = drain + drain * dt
    results = []

    def obs(now=60.1, energy=None, hp_fraction=0.5, damage=45, weapon="gun",
            maximum=None, **kwargs):
        events = [] if damage is None else [{"type": "damaged", "weapon": weapon,
                   "amount": damage, "by": 5, "bearing": 0.0}]
        value = observation(hello, template, arena, round(now * hz),
                            energy=start + drain * 0.1 if energy is None else energy,
                            events=events, **kwargs)
        value["self"]["hp"] = value["self"]["max_hp"] * hp_fraction
        if maximum is not None:
            value["self"]["max_energy"] = maximum
        return value

    def bots():
        return [baseline(copy.deepcopy(hello)), candidate(copy.deepcopy(hello))]

    def compare(name, pair, value, expected=None, same_boost=False):
        before, after = [action(bot, copy.deepcopy(value)) for bot in pair]
        nonboost = {k: v for k, v in before.items() if k != "boost"} == {
            k: v for k, v in after.items() if k != "boost"}
        passed = nonboost
        if expected is not None:
            passed &= after["boost"] == expected
        if same_boost:
            passed &= before == after
        result = {"scenario": name, "baseline_boost": before["boost"],
                  "candidate_boost": after["boost"], "nonboost_identical": nonboost,
                  "passed": bool(passed)}
        results.append(result)
        return before, after

    old, new = compare("qualifying_low_hp_hit", bots(), obs(), True)
    results[-1]["passed"] &= not old["boost"]
    compare("half_hp_inclusive", bots(), obs(hp_fraction=0.5), True)
    compare("below_half_hp", bots(), obs(hp_fraction=0.2), True)
    compare("start_energy_inclusive", bots(), obs(energy=start), True)
    compare("below_start_energy", bots(), obs(energy=start - 0.01), False, True)
    compare("window_starts_at_45", bots(), obs(now=45.0), True)

    cases = [
        ("before_window", {"now": 44.9}),
        ("after_window", {"now": 180.1}),
        ("above_half_hp", {"hp_fraction": 0.50001}),
        ("no_damage", {"damage": None}),
        ("zone_damage_only", {"weapon": "zone"}),
        ("zero_damage", {"damage": 0}),
        ("negative_damage", {"damage": -1}),
        ("airborne", {"grounded": False}),
        ("zero_movement", {"goal": START}),
        ("near_next_center", {"goal": (START[0] + 4.9, START[1])}),
        ("scheduled_jump", {"now": 60.0}),
        ("visible_enemy_without_hit", {"damage": None, "contact": True}),
        ("npc_without_hit", {"damage": None, "npc": True}),
    ]
    for name, kwargs in cases:
        compare(name, bots(), obs(**kwargs), False, True)
    compare("outside_preserves_escape", bots(), obs(outside=True), True, True)
    compare("normal_full_energy_contact", bots(), obs(energy=100, hp_fraction=1,
                                                     damage=None, contact=True), True, True)

    pair = bots()
    compare("hold_initial_hit", pair, obs(now=60.1), True)
    compare("hold_at_0_6_seconds", pair, obs(now=60.7, damage=None), True)
    compare("hold_expires_after_0_6_seconds", pair, obs(now=60.8, damage=None), False)

    pair = bots()
    compare("burst_before_floor", pair, obs(energy=start), True)
    compare("burst_stops_at_floor_plus_decision", pair,
            obs(now=60.2, energy=stop, damage=None), False)
    compare("cannot_restart_below_threshold", pair,
            obs(now=60.3, energy=start - 0.01, damage=None), False)
    compare("emergency_restarts_during_normal_cooldown", pair,
            obs(now=60.4, energy=start, damage=None), True)

    # A periodic jump at exactly 180s suspends the burst; the next decision is
    # outside the window. This exercises both the end boundary and public jump.
    pair = bots()
    compare("end_window_before_jump", pair, obs(now=179.9), True)
    compare("end_window_scheduled_jump", pair, obs(now=180.0, damage=None), False)
    compare("end_window_after_jump", pair, obs(now=180.1, damage=None), False)

    # With clipped maximum energy there still must be a whole second of ground
    # boost left after the issued command. No cap can relax that physical floor.
    for maximum in (drain * 0.5, drain, stop - 0.01, stop + 0.01,
                    drain * 1.2, drain * 1.8):
        pair = bots()
        _, command = compare(f"energy_cap_{maximum:.3f}", pair,
                             obs(energy=maximum, maximum=maximum))
        remaining = maximum - (drain * dt if command["boost"] else 0)
        # Existing energy below reserve cannot be repaired by a command, so in
        # that case the acceptance criterion is simply no boost.
        safe = remaining + EPS >= drain if maximum >= drain else not command["boost"]
        results[-1].update(remaining_energy=round(remaining, 6), reserve=drain)
        results[-1]["passed"] &= safe

    # A fresh hit resets the hold even if a previous burst already expired.
    pair = bots()
    compare("refresh_initial", pair, obs(now=60.1), True)
    compare("refresh_new_hit", pair, obs(now=60.6), True)
    compare("refresh_held", pair, obs(now=61.1, damage=None), True)
    compare("refresh_expired", pair, obs(now=61.3, damage=None), False)

    pair = bots()
    energies, last_spend = [start, start], [-math.inf, -math.inf]
    minimum, boosted, stopped, identical = start, [0, 0], False, True
    for step in range(20):
        now = 60.1 + step * dt
        commands = [action(bot, obs(now=now, energy=energy))
                    for bot, energy in zip(pair, energies)]
        identical &= {k: v for k, v in commands[0].items() if k != "boost"} == {
            k: v for k, v in commands[1].items() if k != "boost"}
        for i, command in enumerate(commands):
            boosting = command["boost"]
            boosted[i] += int(boosting)
            energies[i], last_spend[i] = energy_step(
                energies[i], template["self"]["max_energy"], boosting,
                now, last_spend[i], dt, stats)
        minimum = min(minimum, energies[1])
        stopped |= boosted[1] > 0 and not commands[1]["boost"]
    results.append({"scenario": "continuous_hits_integrated_drain_and_regen",
                    "candidate_minimum_energy": round(minimum, 6),
                    "reserve": drain, "baseline_boost_decisions": boosted[0],
                    "candidate_boost_decisions": boosted[1],
                    "stopped_at_reserve": stopped, "nonboost_identical": identical,
                    "passed": identical and boosted[0] == 0 and boosted[1] > 0
                    and stopped and minimum + EPS >= drain})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", required=True, type=Path)
    parser.add_argument("--bot", type=Path, default=ROOT / "example_bot.py")
    parser.add_argument("--baseline", type=Path,
                        default=ROOT / "artifacts/e10-stdio/e10-exact-9498346/example_bot.py")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mech-emergency-check-") as directory:
        hello, template, arena = load_kit(args.kit.resolve(), Path(directory))
        before = factory(args.baseline.resolve(), "emergency_baseline")
        after = factory(args.bot.resolve(), "emergency_candidate")
        print("model=public-Bot.act ground-command-energy-floor match-score=not-measured")
        for label, path in (("baseline", args.baseline), ("candidate", args.bot)):
            print(f"{label}_mobility_sha256=" + hashlib.sha256(
                path.with_name("mobility.py").read_bytes()).hexdigest())
        print("world_sha256=" + arena.world_sha256)
        results = run_checks(before, after, hello, template, arena)
        for result in results:
            print(json.dumps(result, sort_keys=True))
        passed = all(result["passed"] for result in results)
        print(f"{'PASS' if passed else 'FAIL'}: emergency mobility ({len(results)} scenarios)")
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
