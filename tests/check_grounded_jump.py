"""Deterministic E15 public Bot.act regression; no match score is modelled.

python -B tests/check_grounded_jump.py --kit KIT --baseline E10/example_bot.py
Use --bot E10/example_bot.py for the expected failing control.
Official hello/map/replay and existing observation/import helpers are reused.
"""

import argparse
import copy
import json
import math
from pathlib import Path
import tempfile

from check_routes import EPS, load_kit
from check_economy import action, fixture, load_bot, observation, source_hashes


def scenarios(hello, template, arena):
    start, target, goal = fixture(0)
    every = hello["config"]["match"]["decide_every"]
    # Check both edges of two consecutive periods, including tick zero.
    for tick in (0, every, 40-every, 40, 40+every, 80):
        for mode in ("walking", "contact", "outside", "airborne", "stationary",
                     "supply_approach", "supply_hold"):
            obs = observation(hello, template, arena, tick, start, goal)
            if mode == "contact":
                obs["mechs"] = [{"id": 5, "kind": "mech",
                    "pos": [start[0]+20, start[1]+10, obs["self"]["pos"][2]],
                    "vel": [0, 0, 0], "aim": [0, 0], "hp": 1000,
                    "max_hp": 1400, "weapons": ["gun"], "charging": False}]
            elif mode == "outside":
                obs["self"]["in_zone"] = False
            elif mode == "airborne":
                obs["self"]["grounded"] = False
                obs["self"]["pos"][2] += 15
            elif mode == "stationary":
                obs["zone"].update(center=list(start), next_center=list(start))
            elif mode.startswith("supply_"):
                obs["self"].update(hp=700, energy=30)
                pos = start if mode == "supply_hold" else target
                obs["drops"] = [{"id": 20001,
                    "pos": [*pos, arena.height(pos)],
                    "radius": hello["config"]["drops"]["radius"],
                    "lands_in": 0, "capture_s": hello["config"]["drops"]["capture_s"],
                    "holder": None, "progress": 0}]
            yield f"{mode}_{tick}", mode, obs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", type=Path, required=True)
    parser.add_argument("--bot", type=Path, default=Path(__file__).resolve().parents[1]/"example_bot.py")
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    candidate = load_bot(args.bot, "grounded_jump_candidate")
    baseline = load_bot(args.baseline, "grounded_jump_baseline")
    failures = 0
    with tempfile.TemporaryDirectory(prefix="mech-grounded-jump-") as temporary:
        hello, template, arena = load_kit(args.kit.resolve(), Path(temporary))
        print(json.dumps({"candidate": source_hashes(args.bot),
                          "baseline": source_hashes(args.baseline),
                          "world_sha256": arena.world_sha256}, sort_keys=True))
        for name, mode, obs in scenarios(hello, template, arena):
            old = action(baseline(copy.deepcopy(hello)), copy.deepcopy(obs))
            new = action(candidate(copy.deepcopy(hello)), copy.deepcopy(obs))
            moving = math.hypot(*old["move"]) > EPS
            # Supply still suppresses its own jumping, even on approach.
            expected_old_jump = obs["tick"] % 40 == 0 and not mode.startswith("supply_")
            suppress = (expected_old_jump and obs["self"]["grounded"]
                        and obs["self"]["in_zone"] and moving)
            ignored = {"jump", "boost"} if suppress else set()
            errors = []
            if old.get("jump") != expected_old_jump:
                errors.append("baseline fixture did not exercise expected jump")
            if new.get("jump") != (expected_old_jump and not suppress):
                errors.append("unexpected jump")
            if {k: v for k, v in old.items() if k not in ignored} != {
                    k: v for k, v in new.items() if k not in ignored}:
                errors.append("unrelated action fields changed")
            expected_moving = mode not in ("stationary", "supply_hold")
            if moving != expected_moving:
                errors.append("fixture movement precondition failed")
            failures += bool(errors)
            print(json.dumps({"scenario": name, "jump": new.get("jump"),
                              "moving": moving, "passed": not errors,
                              "errors": errors}, sort_keys=True))
    print(f"{'FAIL' if failures else 'PASS'}: grounded jump regression ({failures} failures)")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
