"""Retrospective, chronological checks of an unofficial local score scale.

Examples (Python 3.12+, standard library)::

    python tools/score_calibration.py --backtest-csv results/score-forecast-backtest.csv
    python tools/score_calibration.py --mode original --local-mean 7.0
    python tools/score_calibration.py --mode per-observation --local-mean 7.0
    python tools/score_calibration.py --run-json artifacts/audit-e10.json

The organizer's starter benchmark and four team-reported scores are recorded
in PLAN.md to two decimals. They are aggregate scores. This rule
was specified retrospectively: chronological exclusion of a target label is
not prospective validation. No rule, penalty, or mode is selected on E10.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ("starter", "e01", "e05", "e07", "e10")
OFFICIAL = (1.15, 1.56, 1.60, 1.98, 2.00)
MODES = ("original", "per-observation")
# One squared local score point of ridge penalty is a fixed safety shrinkage,
# not estimated using future labels. Negative associations transfer no gain.
RIDGE = 1.0
METHODS = ("previous_official", "local_delta", "shrunk_local_delta", "raw_local")
PINNED_METADATA = {
    "engine_url": "https://github.com/atharvatripathi2026/gtgoats_mech_royale",
    "engine_commit": "4b8012873dfd7ae43371fe0ee663104a6efebcc1",
    "engine_sha256": {
        "navigation.py": "5932f62b53721a344404646b0391d0b4aa1d3ec691e2bd121297f35e7e43398c",
        "training/arena.py": "59d80bfe97394ff8edd917aeaf48cfd5ac610b340ecdaefc0518f600544b3504",
        "training/squad16.py": "5ac60ce5e9c8b3b3d922a0000fe5f824a57cbe97c8245bb784fcff0950fe84f5",
    },
    "kit_stream_sha256": "884a1bbb2afe288ea5b2d60ce20c74274ba37351ec31bf3334c160f13ec48a95",
    "world_sha256": "e0302dd4777b527b00a6a4b2e02759915402a23c58839d921e9fcc6d58092445",
    "terrain_sha256": "3aa74308e096e36ed29230f135b0f3ff3ae09dfa164c2b687455b4f975b01d38",
    "extension_sha256": "e39fc96dc2cdbc78a197c9299115760c903400372bbf2c912df26e2c4ffffacd",
}
SUPPLIES = "local supply-ring subclass"
PER_OBSERVATION = "update house commands at each player observation"


def finite_number(value):
    return ((isinstance(value, int) and not isinstance(value, bool))
            or (isinstance(value, float) and math.isfinite(value)))


def inspect_run_report(path):
    """Compute raw mean first; authorize history only for a matching manifest.

    A mean remains useful on another mode, but it cannot authorize a server
    estimate. Metadata are declarations, not a verification of executed code.
    """
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Run JSON must contain an object")
    matches = data.get("matches")
    if not isinstance(matches, list) or not matches:
        raise ValueError("Run JSON must contain a nonempty matches list")
    scores = []
    for index, row in enumerate(matches):
        if not isinstance(row, dict):
            raise ValueError(f"Match {index}: expected an object")
        score = row.get("score")
        if not finite_number(score) or not 0 <= score <= 15:
            raise ValueError(f"Match {index}: score must be finite and between 0 and 15")
        scores.append(score)
    raw_mean = statistics.mean(scores)
    reasons = []
    if data.get("status") != "complete":
        reasons.append("run status is not complete")
    if any(row.get("exceptions") != 0 for row in matches):
        reasons.append("match exceptions are nonzero or missing")
    if any(row.get("scenario") != "squad16" for row in matches):
        reasons.append("matches are not all squad16")
    summary = data.get("summary")
    if not isinstance(summary, dict):
        reasons.append("summary is missing or invalid")
    else:
        reported = summary.get("mean_score")
        if not finite_number(reported) or not math.isclose(
                raw_mean, reported, rel_tol=0, abs_tol=1e-9):
            reasons.append("summary mean_score disagrees with matches or is invalid")
        if summary.get("matches") != len(matches):
            reasons.append("summary matches count disagrees with matches")
        if summary.get("exceptions") != 0:
            reasons.append("summary exceptions are nonzero or missing")
    seeds = [row.get("seed") for row in matches]
    if (len(matches) != 48 or any(type(seed) is not int for seed in seeds)
            or sorted(seeds) != list(range(8101, 8149))):
        reasons.append("historical calibration requires exactly 48 unique seeds 8101..8148")
    for row in matches:
        place = row.get("place", row.get("place_proxy"))
        if not finite_number(place) or row["score"] != 16 - place:
            reasons.append("match placement disagrees with score or is missing")
            break
    metadata = data.get("metadata")
    mode = None
    if not isinstance(metadata, dict):
        reasons.append("metadata are missing or invalid")
    else:
        for field, expected in PINNED_METADATA.items():
            if metadata.get(field) != expected:
                reasons.append(f"historical manifest mismatch: {field}")
        changes = metadata.get("engine_changes")
        if changes == [SUPPLIES]:
            mode = "original"
        elif changes in ([SUPPLIES, PER_OBSERVATION], [PER_OBSERVATION, SUPPLIES]):
            mode = "per-observation"
        else:
            reasons.append("engine_changes differ from both historical modes")
        if any(field in metadata for field in (
                "cube_size_sha256", "spawn_arena_sha256", "movement_arena_sha256")):
            reasons.append("physics/spawn overlays have no historical calibration")
        clock = metadata.get("planning_clock")
        if (not isinstance(clock, dict) or clock.get("mode") != "fixed_counter"
                or clock.get("seconds_per_perf_counter_call") != 1e-6
                or clock.get("scope") != "evaluated bot navigation module only"):
            reasons.append("planning clock differs from historical fixed planning")
        if metadata.get("count") != 48 or metadata.get("seeds_start") != 8101:
            reasons.append("metadata seed range differs from historical range")
    return {"raw_local_mean": raw_mean, "matches": len(matches),
            "mode": mode if not reasons else None, "reasons": reasons}


def bounded_score(value):
    return min(15.0, max(0.0, value))


def transfer_coefficient(local, official):
    """Fit ONLY the supplied completed history; shrink toward no transfer."""
    if len(local) != len(official) or not local:
        raise ValueError("Local and official histories must have equal nonzero length")
    dx = [b - a for a, b in zip(local, local[1:])]
    dy = [b - a for a, b in zip(official, official[1:])]
    return min(1.0, max(0.0, sum(x * y for x, y in zip(dx, dy)) /
                        (RIDGE + sum(x * x for x in dx))))


def predictions(local_history, official_history, next_local):
    if not math.isfinite(next_local) or not 0 <= next_local <= 15:
        raise ValueError("A local mean must be finite and between 0 and 15")
    beta = transfer_coefficient(local_history, official_history)
    delta = next_local - local_history[-1]
    last = official_history[-1]
    return {
        "previous_official": (last, 0.0),
        "local_delta": (bounded_score(last + delta), 1.0),
        "shrunk_local_delta": (bounded_score(last + beta * delta), beta),
        "raw_local": (next_local, None),
    }


def load_local(mode, root=ROOT):
    if mode not in MODES:
        raise ValueError("Unknown mode; never reuse calibration across modes")
    relative = ("results/referee-calibration.csv" if mode == "original" else
                "results/house-cadence-comparison.csv")
    path = root / relative
    with path.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    values = []
    for version in VERSIONS:
        selected = (rows if mode == "original" else
                    [row for row in rows if row["version"] == version])
        seeds = [int(row["seed"]) for row in selected]
        if sorted(seeds) != list(range(8101, 8149)):
            raise ValueError(f"{relative}: {version} needs exactly seeds 8101..8148")
        field = f"{version}_score" if mode == "original" else "per_observation_points"
        scores = [float(row[field]) for row in selected]
        if any(not math.isfinite(score) or not 0 <= score <= 15 for score in scores):
            raise ValueError(f"Invalid placement score for {version}")
        values.append(statistics.mean(scores))
    content = path.read_text(encoding="utf-8-sig").encode("utf-8")
    return values, relative, hashlib.sha256(content).hexdigest()


def verify_original_artifacts(local, root=ROOT):
    """Optional raw artifacts must reproduce the tracked table, if present."""
    checked = []
    for version, mean in zip(VERSIONS, local):
        prefix = "cal13" if version in ("starter", "e01", "e05") else "pod-repeat"
        relative = f"artifacts/{prefix}-{version}.json"
        path = root / relative
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        matches = data["matches"]
        if (data["status"] != "complete" or len(matches) != 48 or
                sorted(row["seed"] for row in matches) != list(range(8101, 8149)) or
                not math.isclose(statistics.mean(row["score"] for row in matches), mean)):
            raise ValueError(f"Raw artifact disagrees with tracked table: {relative}")
        checked.append(relative)
    return checked


def backtest(mode, local, source, source_sha256):
    rows = []
    for i in range(1, len(VERSIONS)):
        # A target's official result is accessed only AFTER all its forecasts.
        forecasts = predictions(local[:i], OFFICIAL[:i], local[i])
        for method in METHODS:
            prediction, beta = forecasts[method]
            rows.append({
                "mode": mode, "source": source, "source_sha256": source_sha256,
                "training_versions": ";".join(VERSIONS[:i]), "target": VERSIONS[i],
                "matches_per_local_mean": 48,
                "previous_local_mean": local[i - 1], "raw_local_mean": local[i],
                "local_delta": local[i] - local[i - 1],
                "previous_official": OFFICIAL[i - 1], "method": method,
                "transfer_coefficient": beta, "prediction": prediction,
                "reported_official": OFFICIAL[i],
                "absolute_error": abs(prediction - OFFICIAL[i]),
            })
    return rows


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: round(value, 9) if isinstance(value, float) else value
                             for key, value in row.items()})


def zone_bound():
    """Arithmetic deletion on fixed recorded trajectories; no causal replay."""
    path = ROOT / "results" / "placement-attribution.csv"
    with path.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    if sorted(int(r["seed"]) for r in rows) != list(range(8101, 8149)):
        raise ValueError("Placement attribution must cover the same 48 starts")
    total = sum(int(r["e10_score"]) for r in rows)
    zone = sum(int(r["e10_points_zone"]) for r in rows)
    nonzone, matches, official = total - zone, len(rows), OFFICIAL[VERSIONS.index("e10")]
    gap = total / matches - official
    return {"raw_local_mean": total / matches, "reported_official": official,
            "maximum_zone_deletion": zone / matches,
            "remaining_nonzone_mean": nonzone / matches,
            "remaining_gap": nonzone / matches - official,
            "maximum_fraction_of_gap": (zone / matches) / gap}


def print_forecasts(mode, raw_mean):
    local, _, _ = load_local(mode)
    print(f"Historical mode={mode}; RAW local mean={raw_mean:.6f}")
    # The simple, already observed server reference is deliberately first.
    for method, (value, beta) in predictions(local, OFFICIAL, raw_mean).items():
        suffix = "" if beta is None else f", transfer={beta:.3f}"
        print(f"  {method}: {value:.2f}{suffix}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=MODES,
                        help="Manual mean's exact local mode (default: original)")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--local-mean", type=float,
                        help="New candidate's RAW mean; caller asserts mode compatibility")
    source.add_argument("--run-json", type=Path,
                        help="Read local_matches.py report and infer a compatible mode")
    parser.add_argument("--backtest-csv", type=Path,
                        help="Write backtests of both modes, without pooling their means")
    args = parser.parse_args()
    mode = args.mode or "original"
    if args.run_json:
        if args.mode or args.backtest_csv:
            parser.error("--run-json cannot be combined with --mode or --backtest-csv")
        try:
            report = inspect_run_report(args.run_json)
        except (OSError, ValueError) as exc:
            parser.error(f"Invalid run report: {exc}")
        print(f"RAW local mean from {report['matches']} available match rows: "
              f"{report['raw_local_mean']:.6f}")
        if report["mode"] is None:
            print("No historical calibration; no server forecast is available for this report.")
            for reason in report["reasons"]:
                print(f"  {reason}")
            return
        mode = report["mode"]
        print_forecasts(mode, report["raw_local_mean"])
    elif args.local_mean is not None:
        if not finite_number(args.local_mean) or not 0 <= args.local_mean <= 15:
            parser.error("--local-mean must be finite and between 0 and 15")
        print("Manual input: compatibility with the selected history is caller-asserted.")
        print_forecasts(mode, args.local_mean)
    all_rows = []
    for history_mode in MODES if args.backtest_csv else (mode,):
        local, source, digest = load_local(history_mode)
        if history_mode == "original":
            checked = verify_original_artifacts(local)
            print(f"original: checked {len(checked)}/5 optional raw JSON artifacts")
        rows = backtest(history_mode, local, source, digest)
        all_rows.extend(rows)
        print(f"{history_mode}: chronological retrospective errors over four transitions")
        for method in METHODS:
            errors = [row["absolute_error"] for row in rows if row["method"] == method]
            print(f"  {method}: MAE {statistics.mean(errors):.3f}, max {max(errors):.3f}")
    if args.backtest_csv:
        write_rows(args.backtest_csv, all_rows)
        print(f"Wrote {args.backtest_csv}")
    print("Source: official starter benchmark and four team-reported tournament scores.")
    print("Retrospective rule, not validated server prediction; no prediction interval.")
    print("For a different engine/seed/behavior mode, collect a separate history first.")
    bound = zone_bound()
    print("E10 fixed-trajectory arithmetic: deleting ALL zone points leaves "
          f"{bound['remaining_nonzone_mean']:.3f} vs reported 2.00; "
          f"zone explains at most {bound['maximum_fraction_of_gap']:.1%} of the gap.")


if __name__ == "__main__":
    main()
