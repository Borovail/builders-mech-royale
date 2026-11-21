"""E21 public-action A/B scenarios on the official map, never match scores."""
import argparse
import copy
import json
import math
from pathlib import Path
import tempfile

from check_routes import load_kit
from check_economy import action, aligned, fixture, load_bot, observation, source_hashes
from check_supplies import CaptureClock, safety


def checks(candidate, baseline, hello, template, arena):
    start, target, goal = fixture(0)
    hz = hello["config"]["match"]["tick_hz"]

    def obs(now=0, position=start, lands=25):
        value = observation(hello, template, arena, round(now*hz), position, goal)
        value["self"].update(hp=700, energy=30, cubes=3)
        value["drops"] = [CaptureClock(hello["config"]["drops"],
            [*target, arena.height(target)]).obs(now)]
        value["drops"][0]["lands_in"] = lands
        return value

    for lands, accepts in ((25, True), (25.01, False), (40, False)):
        value = obs(lands=lands)
        old = action(baseline(copy.deepcopy(hello)), copy.deepcopy(value))
        new = action(candidate(copy.deepcopy(hello)), copy.deepcopy(value))
        yield {"scenario": f"fall_{lands}", "passed":
            aligned(old["move"], start, goal) and
            aligned(new["move"], start, target if accepts else goal)}

    bot = candidate(copy.deepcopy(hello))
    first = action(bot, obs())
    held = []
    for now in (4, 10, 20, 24.9, 25, 25.5, 26, 27, 28):
        value = obs(now, target, max(0, 25-now))
        if now >= 25:
            value["drops"][0].update(holder=value["self"]["id"], progress=now-25)
        cmd = action(bot, value)
        held.append(math.hypot(*cmd["move"]) == 0 and not cmd["boost"] and not cmd["jump"])
    value = obs(29, target, 0)
    value["events"] = [{"type": "drop", "id": 20001, "cubes": 6}]
    value["drops"] = []
    resumed = action(bot, value)
    yield {"scenario": "wait_then_capture_then_leave", "held_observations": sum(held),
           "passed": aligned(first["move"], start, target) and all(held)
                     and aligned(resumed["move"], target, goal)}

    for mode in ("damage", "opponent", "unsafe_zone", "slipped_landing", "missing"):
        bot = candidate(copy.deepcopy(hello))
        action(bot, obs())
        value = obs(1, start, 24)
        if mode == "damage":
            value["events"] = [{"type": "damaged", "weapon": "gun", "amount": 40}]
        elif mode == "opponent":
            value["drops"][0].update(holder=5, progress=1)
        elif mode == "unsafe_zone":
            value["zone"].update(next_radius=10, shrink_start=0, shrink_end=5)
        elif mode == "slipped_landing":
            value["drops"][0]["lands_in"] = 26
        else:
            value["drops"] = []
        yield {"scenario": "early_cancel_"+mode,
               "passed": aligned(action(bot, value)["move"], start, goal)}
    # Legacy long_fall=25 is the intentional change; all other original safety
    # cases still require E15's cancellation, roof and frozen-clock behavior.
    for row in safety(candidate, hello, template, arena):
        if row["scenario"] != "reject_long_fall":
            yield row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", type=Path, required=True)
    parser.add_argument("--bot", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    candidate = load_bot(args.bot, "strategy_supply_candidate")
    baseline = load_bot(args.baseline, "strategy_supply_baseline")
    with tempfile.TemporaryDirectory(prefix="mech-strategy-supply-") as tmp:
        hello, template, arena = load_kit(args.kit.resolve(), Path(tmp))
        print(json.dumps({"candidate": source_hashes(args.bot), "baseline": source_hashes(args.baseline),
                          "world_sha256": arena.world_sha256}, sort_keys=True))
        rows = list(checks(candidate, baseline, hello, template, arena))
        for row in rows:
            print(json.dumps(row, sort_keys=True))
        failures = sum(not row["passed"] for row in rows)
        print(f"{'FAIL' if failures else 'PASS'}: strategy supply ({len(rows)} scenarios, {failures} failures)")
        return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
