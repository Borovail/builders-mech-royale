"""Public E22/E21 action comparison on the official arena; no match scores."""
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

    def obs(now=0, lands=25, rival=True):
        value = observation(hello, template, arena, round(now*hz), start, goal)
        value["self"].update(hp=700, energy=30, cubes=3)
        value["drops"] = [CaptureClock(hello["config"]["drops"],
            [*target, arena.height(target)]).obs(now)]
        value["drops"][0]["lands_in"] = lands
        if rival:
            point = (start[0], start[1]+120)
            value["mechs"] = [{"id": 5, "kind": "mech", "pos": [*point, arena.height(point)],
                "vel": [0, 0, 0], "aim": [0, 0], "hp": 1400, "max_hp": 1400,
                "weapons": ["gun"], "charging": False}]
        return value

    def fallback(value):
        value = copy.deepcopy(value)
        value["drops"] = []
        return action(candidate(copy.deepcopy(hello)), value)["move"]

    for lands, accepted in ((25, True), (6.01, True), (6, False)):
        value = obs(lands=lands)
        old = action(baseline(copy.deepcopy(hello)), copy.deepcopy(value))
        new = action(candidate(copy.deepcopy(hello)), copy.deepcopy(value))
        rival = value["mechs"][0]["pos"][:2]
        travel = math.dist(start, target)*1.25/hello["config"]["mech"]["walk_speed"]
        finish = max(travel, lands)+value["drops"][0]["capture_s"]+0.7
        reach = (math.dist(target, rival)-value["drops"][0]["radius"])/hello["config"]["mech"]["boost_speed"]
        yield {"scenario": f"distant_rival_landing_{lands}", "passed":
            math.dist(start, rival) >= 60 and math.dist(target, rival) >= 65 and reach <= finish+2
            and old["move"] == fallback(value)
            and (aligned(new["move"], start, target) if accepted else new["move"] == old["move"])}

    value = obs(rival=False)
    # The supply lies opposite the next circle: a 120 m detour is above E21's
    # 8 seconds and below E22's 16 seconds, with the whole route zone-safe.
    opposite = tuple(2*a-b for a, b in zip(start, target))
    value["zone"].update(next_center=list(opposite), next_radius=150)
    extra = math.dist(start, target)+math.dist(target, opposite)-math.dist(start, opposite)
    speed = hello["config"]["mech"]["walk_speed"]
    old = action(baseline(copy.deepcopy(hello)), copy.deepcopy(value))
    new = action(candidate(copy.deepcopy(hello)), copy.deepcopy(value))
    yield {"scenario": "longer_safe_detour", "detour_seconds": extra/speed,
           "passed": 8 < extra/speed <= 16 and old["move"] == fallback(value)
                     and aligned(new["move"], start, target)}

    for mode in ("landing_near", "rival_near", "foreign_holder", "damage", "roof"):
        bot = candidate(copy.deepcopy(hello))
        first = action(bot, obs())
        value = obs(1, 24)
        if mode == "landing_near":
            value = obs(19, 6)
            value["self"]["pos"] = [*target, arena.height(target)]
        elif mode == "rival_near":
            point = (start[0], start[1]+50)
            value["mechs"][0]["pos"] = [*point, arena.height(point)]
        elif mode == "foreign_holder":
            value["drops"][0].update(holder=5, progress=1)
        elif mode == "damage":
            value["events"] = [{"type": "damaged", "weapon": "gun", "amount": 40}]
        else:
            value["drops"][0]["pos"][2] += 40
        new = action(bot, value)
        yield {"scenario": "stage_cancel_"+mode, "passed":
               aligned(first["move"], start, target) and new["move"] == fallback(value)}

    # Preserve established immediate safety and capture-clock behavior.
    for row in safety(candidate, hello, template, arena):
        if row["scenario"] != "reject_long_fall":
            yield row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", type=Path, required=True)
    parser.add_argument("--bot", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    candidate = load_bot(args.bot, "strategy_stage_candidate")
    baseline = load_bot(args.baseline, "strategy_stage_baseline")
    with tempfile.TemporaryDirectory(prefix="mech-strategy-stage-") as tmp:
        hello, template, arena = load_kit(args.kit.resolve(), Path(tmp))
        print(json.dumps({"candidate": source_hashes(args.bot), "baseline": source_hashes(args.baseline),
                          "world_sha256": arena.world_sha256}, sort_keys=True))
        rows = list(checks(candidate, baseline, hello, template, arena))
        for row in rows:
            print(json.dumps(row, sort_keys=True))
        failures = sum(not row["passed"] for row in rows)
        print(f"{'FAIL' if failures else 'PASS'}: strategy staging ({len(rows)} scenarios, {failures} failures)")
        return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
