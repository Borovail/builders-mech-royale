"""E20 public-action A/B checks on official terrain; no match score model."""
import argparse
import copy
import json
from pathlib import Path
import tempfile

from check_routes import load_kit
from check_economy import action, aligned, fixture, ground_item, load_bot, observation, source_hashes


def checks(candidate, baseline, hello, template, arena):
    start, target, goal = fixture(0)
    hz = hello["config"]["match"]["tick_hz"]

    def obs(kind="cube", now=10, cubes=0):
        value = observation(hello, template, arena, round(now*hz), start, goal)
        value["self"]["cubes"] = cubes
        pickup = ground_item(arena, goal)
        pickup["kind"] = "laser"
        value["pickups"] = [pickup]
        item = ground_item(arena, target, kind)
        if kind == "crate":
            item["hp"] = hello["config"]["crates"]["hp"]
        value["cubes" if kind == "cube" else "crates"] = [item]
        return value

    far_resource = target
    for kind in ("cube", "crate"):
        # Crates include destruction time; use a shorter approach so both
        # resources fit the opening's bounded work budget on official stats.
        target = far_resource if kind == "cube" else tuple((a+b)/2 for a, b in zip(start, far_resource))
        for now, cubes in ((10, 0), (99.9, 2)):
            value = obs(kind, now, cubes)
            old = action(baseline(copy.deepcopy(hello)), copy.deepcopy(value))
            new = action(candidate(copy.deepcopy(hello)), copy.deepcopy(value))
            yield {"scenario": f"prefer_{kind}_{now}_{cubes}", "passed":
                aligned(old["move"], start, goal) and aligned(new["move"], start, target)
                and (kind != "crate" or new.get("fire") == [True, False])}
        for mode in ("time_100", "three_cubes", "low_hp", "near_weapon", "damage", "outside"):
            value = obs(kind)
            if mode == "time_100":
                value["t"], value["tick"] = 100, round(100*hz)
            elif mode == "three_cubes":
                value["self"]["cubes"] = 3
            elif mode == "low_hp":
                value["self"]["hp"] = value["self"]["max_hp"]*0.5
            elif mode == "near_weapon":
                near = (start[0]+15, start[1])
                value["pickups"][0]["pos"] = [*near, arena.height(near)]
            elif mode == "damage":
                value["events"] = [{"type": "damaged", "weapon": "gun", "amount": 50}]
            else:
                value["self"]["in_zone"] = False
            old = action(baseline(copy.deepcopy(hello)), copy.deepcopy(value))
            new = action(candidate(copy.deepcopy(hello)), copy.deepcopy(value))
            yield {"scenario": f"preserve_{kind}_{mode}", "passed": old == new}

        bot = candidate(copy.deepcopy(hello))
        first = obs(kind)
        first["cubes" if kind == "cube" else "crates"] = []
        committed = action(bot, first)
        later = obs(kind, 10.6)
        changed = action(bot, later)
        yield {"scenario": f"preempt_distant_weapon_for_{kind}", "passed":
            aligned(committed["move"], start, goal) and aligned(changed["move"], start, target)}
        # A nearly collected weapon remains worth finishing before growth.
        bot = candidate(copy.deepcopy(hello))
        action(bot, first)
        later["self"]["pos"] = [goal[0]-20, goal[1], arena.height((goal[0]-20, goal[1]))]
        old = action(baseline(copy.deepcopy(hello)), copy.deepcopy(later))
        new = action(bot, later)
        yield {"scenario": f"finish_near_weapon_before_{kind}", "passed": old == new}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", type=Path, required=True)
    parser.add_argument("--bot", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    candidate = load_bot(args.bot, "strategy_growth_candidate")
    baseline = load_bot(args.baseline, "strategy_growth_baseline")
    with tempfile.TemporaryDirectory(prefix="mech-strategy-growth-") as tmp:
        hello, template, arena = load_kit(args.kit.resolve(), Path(tmp))
        print(json.dumps({"candidate": source_hashes(args.bot), "baseline": source_hashes(args.baseline),
                          "world_sha256": arena.world_sha256}, sort_keys=True))
        rows = list(checks(candidate, baseline, hello, template, arena))
        for row in rows:
            print(json.dumps(row, sort_keys=True))
        failures = sum(not row["passed"] for row in rows)
        print(f"{'FAIL' if failures else 'PASS'}: strategy growth ({len(rows)} scenarios, {failures} failures)")
        return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
