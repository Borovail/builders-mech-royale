"""Run an independent, explicitly unofficial 16-mech placement surrogate.

Infrastructure only: training/squad16.py, training/arena.py and navigation.py
from the MIT project below. No competitor bot/policy is loaded. Engine files
remain external and unmodified. This is not the organizer's scorer/house AI.

Example (Python 3.12+, standard library only)::

    python tools/local_matches.py --engine ENGINE --kit KIT --bot-dir BOT \
        --seeds 0 --count 48 --workers 4 --json results.json --csv results.csv

Each spawned worker builds the campus once. Bot/engine exceptions fail the
run; successful individual results are flushed to stdout as JSON lines.
"""
from __future__ import annotations

import argparse
import atexit
from concurrent.futures import ProcessPoolExecutor, as_completed
import csv
import hashlib
import importlib
import importlib.util
import json
import math
import multiprocessing
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
import types
from unittest.mock import patch


ENGINE_URL = "https://github.com/atharvatripathi2026/gtgoats_mech_royale"
ENGINE_COMMIT = "4b8012873dfd7ae43371fe0ee663104a6efebcc1"
# Downloaded pinned files, checked before imports. A different engine must be
# reviewed and explicitly given a different manifest; silently drifting the
# benchmark would invalidate comparisons.
ENGINE_SHA256 = {
    "navigation.py": "5932f62b53721a344404646b0391d0b4aa1d3ec691e2bd121297f35e7e43398c",
    "training/arena.py": "59d80bfe97394ff8edd917aeaf48cfd5ac610b340ecdaefc0518f600544b3504",
    "training/squad16.py": "5ac60ce5e9c8b3b3d922a0000fe5f824a57cbe97c8245bb784fcff0950fe84f5",
}
CAVEATS = [
    "Independent SquadArena surrogate, not organizer practice or an official score.",
    "Rival tiers, aim error, pacing, search routes and activation thresholds are hypotheses.",
    "Physics, collision, sensing, loot, upgrades and NPC behavior are approximations.",
    "Rivals do not jet or farm crates; NPC drones only engage the player; supply drops are absent.",
    "No enforced 50 ms deadline or subprocess protocol; decision latency is measured in process.",
    "The evaluated bot receives only the public hello/observation, never simulator internals.",
    "Policies with wall-clock planning budgets may vary with host load; compare under the same worker count.",
]

_arena = None
_bot_class = None
_draw = None
_worker_init_s = None


class PlanningClock:
    """Deterministic navigation budget only; benchmark timing stays real."""
    def __init__(self):
        self.value = 0.0

    def perf_counter(self):
        self.value += 0.000001
        return self.value


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def bot_manifest(directory):
    # Entry bot and its local imports in this project live at the root.
    directory = Path(directory)
    files = sorted(directory.glob("*.py"))
    policy = directory / "policy.json"
    if policy.is_file():
        files.append(policy)
    return {p.name: sha256(p) for p in files}


def require_engine(directory):
    actual = {name: sha256(Path(directory) / name) for name in ENGINE_SHA256}
    if actual != ENGINE_SHA256:
        raise ValueError(f"Engine differs from pinned {ENGINE_COMMIT}: {actual}")
    return actual


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def worker_init(engine, kit, bot_dir, expected_bot_manifest, supplies, fixed_planning=False,
                cube_size=False, audit_places=False, legal_spawns=False,
                house_every_observation=False, movement_geometry=False, entrypoint="example_bot.py"):
    global _arena, _bot_class, _draw, _worker_init_s
    start = time.perf_counter()
    engine, bot_dir = Path(engine), Path(bot_dir)
    require_engine(engine)
    if bot_manifest(bot_dir) != expected_bot_manifest:
        raise RuntimeError("Bot source changed between dispatch and worker import")

    # arena.py uses `from navigation import Navigator`. Resolve that binding
    # to engine infrastructure first, then free the name for our own bot.
    engine_nav = load_file("navigation", engine / "navigation.py")
    package = types.ModuleType("training")
    package.__path__ = [str(engine / "training")]
    sys.modules["training"] = package
    squad = importlib.import_module("training.squad16")
    if house_every_observation:
        # The pinned Episode.run calls update_zone once per observation.
        class PerObservationHouse(squad.SquadEpisode):
            def update_zone(self):
                self.next_house_tick = self.now
                return super().update_zone()

        squad.SquadEpisode = PerObservationHouse
    if movement_geometry:
        from movement_arena import with_movement_geometry
        squad.SquadEpisode = with_movement_geometry(squad.SquadEpisode)
    if legal_spawns:
        from spawn_arena import with_legal_spawns
        squad.SquadEpisode = with_legal_spawns(squad.SquadEpisode)
    if supplies:
        from supply_arena import with_supply_drops
        squad.SquadEpisode = with_supply_drops(squad.SquadEpisode)
    if cube_size:
        from cube_size_arena import with_cube_size
        squad.SquadEpisode = with_cube_size(squad.SquadEpisode)
    if audit_places:
        from placement_audit import with_placement_audit
        squad.SquadEpisode = with_placement_audit(squad.SquadEpisode)
    if sys.modules["training.arena"].Navigator is not engine_nav.Navigator:
        raise RuntimeError("Engine navigation namespace was not isolated")
    sys.modules["_benchmark_engine_navigation"] = engine_nav
    del sys.modules["navigation"]
    sys.path.insert(0, str(bot_dir))
    # The organizer's original example calls mf.run(Bot) even on import.
    # Only suppress the stdio loop here; SquadArena drives that same Bot.act.
    wrapper = load_file("mechfight", bot_dir / "mechfight.py")
    with patch.object(wrapper, "run"):
        bot = load_file("_benchmark_evaluated_bot", bot_dir / entrypoint)
    bot_nav = sys.modules.get("navigation")
    if bot_nav is not None and Path(bot_nav.__file__).resolve() != (bot_dir / "navigation.py").resolve():
        raise RuntimeError("Bot imported navigation from outside --bot-dir")
    if fixed_planning and bot_nav is not None:
        # Replace the evaluated module's binding, never the shared time module
        # or the independent engine navigator. Search limits remain the bot's.
        bot_nav.time = PlanningClock()
    _bot_class = bot.Bot
    _draw = getattr(sys.modules.get("mechfight"), "draw", None)
    _arena = squad.SquadArena(kit)
    atexit.register(_arena.close)
    _worker_init_s = time.perf_counter() - start


class MeasuredBot:
    """Mirror wrapper drawing lifecycle; preserve public actions unchanged."""
    def __init__(self, hello):
        if _draw is not None:
            _draw.caps = hello["config"].get("debug", {})
            _draw.clear()
        self.bot = _bot_class(hello)
        self.latencies = []
        self.first_cube_s = self.first_secondary_s = self.hp_at_60 = None

    def __getattr__(self, name):
        return getattr(self.bot, name)

    def act(self, obs):
        if _draw is not None:
            _draw.clear()
        me = obs["self"]
        if self.first_cube_s is None and me["cubes"] > 0:
            self.first_cube_s = obs["t"]
        if self.first_secondary_s is None and len(me["weapons"]) > 1 and me["weapons"][1]:
            self.first_secondary_s = obs["t"]
        if self.hp_at_60 is None and obs["t"] >= 60:
            self.hp_at_60 = me["hp"]
        start = time.perf_counter()
        result = self.bot.act(obs)
        self.latencies.append((time.perf_counter() - start) * 1000)
        if _draw is not None:
            _draw.take()
        if not isinstance(result, dict):
            raise TypeError("Bot.act must return an action dictionary")
        return result


def run_seed(seed):
    start = time.perf_counter()
    measured = None

    def factory(hello):
        nonlocal measured
        measured = MeasuredBot(hello)
        return measured

    result = _arena.run(factory, seed)
    place = result.get("place_proxy")
    if isinstance(place, bool) or not isinstance(place, (int, float)) or not 1 <= place <= 16:
        raise ValueError(f"Invalid local placement for seed {seed}: {result!r}")
    score = 16 - place  # Explicitly derive; never rescale the four-mech score.
    if not math.isclose(result["score"], score, abs_tol=1e-9):
        raise ValueError(f"Engine score is incompatible with 16-mech place: {result!r}")
    latency = sorted(measured.latencies)
    return {**result, "seed": seed, "place": place, "score": score,
            "first_cube_s": measured.first_cube_s, "first_secondary_s": measured.first_secondary_s,
            "hp_at_60": measured.hp_at_60,
            "wall_s": time.perf_counter() - start, "worker_init_s": _worker_init_s,
            "decisions": len(latency), "decision_p99_ms": latency[max(0, math.ceil(.99 * len(latency)) - 1)],
            "decision_max_ms": max(latency), "exceptions": 0}


def git_info(directory):
    def run(*args):
        p = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else None
    return {"head": run("rev-parse", "HEAD"), "status": run("status", "--short")}


def write_results(args, metadata, results, status="complete"):
    rows = sorted(results, key=lambda row: row["seed"])
    scores = [row["score"] for row in rows]
    report = {"status": status, "metadata": metadata, "summary": {
        "matches": len(rows), "mean_score": statistics.mean(scores) if scores else None,
        "mean_place": statistics.mean(r["place"] for r in rows) if rows else None,
        "score_stdev": statistics.stdev(scores) if len(scores) > 1 else None,
        "mean_kills": statistics.mean(r["kills"] for r in rows) if rows else None,
        "exceptions": int(status == "failed"), "wall_s": time.perf_counter() - metadata["_start"],
    }, "matches": rows}
    serializable = {**report, "metadata": {k: v for k, v in metadata.items() if k != "_start"}}
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(serializable, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    if args.csv and rows:
        Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
        with Path(args.csv).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return serializable


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--engine", required=True, type=Path)
    parser.add_argument("--kit", required=True, type=Path)
    parser.add_argument("--bot-dir", required=True, type=Path)
    parser.add_argument("--entrypoint", default="example_bot.py",
                        help="Root Python module defining Bot, matching the tested image entrypoint")
    parser.add_argument("--seeds", required=True, type=int, help="First seed of a consecutive held-out series")
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--json", type=Path)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--supplies", action="store_true", help="Enable the unofficial supply-ring extension")
    parser.add_argument("--cube-size", action="store_true", help="Apply the published per-cube size scaling to every mech")
    parser.add_argument("--audit-places", action="store_true",
                        help="Record how rival eliminations and end-of-match ranking contribute to placement")
    parser.add_argument("--legal-spawns", action="store_true",
                        help="Check the 16 initial mech cylinders against the published building geometry")
    parser.add_argument("--house-every-observation", action="store_true",
                        help="Sensitivity test: update house commands at every player observation")
    parser.add_argument("--movement-geometry", action="store_true",
                        help="Check grounded mech steps against the published building prisms")
    parser.add_argument("--fixed-planning", action="store_true",
                        help="Diagnostic: advance only bot navigation's planning clock by 1 us per call")
    args = parser.parse_args(argv)
    if args.count < 1 or args.workers < 1:
        parser.error("--count and --workers must be positive")
    args.engine, args.kit, args.bot_dir = (p.resolve() for p in (args.engine, args.kit, args.bot_dir))
    engine_hashes = require_engine(args.engine)
    source_hashes = bot_manifest(args.bot_dir)
    if not args.entrypoint.endswith(".py") or args.entrypoint not in source_hashes:
        parser.error("--entrypoint must name a Python file in the root of --bot-dir")
    metadata = {"engine_url": ENGINE_URL, "engine_commit": ENGINE_COMMIT,
                "engine_sha256": engine_hashes, "engine_changes": [],
                "adapter_sha256": sha256(Path(__file__)),
                "kit_stream_sha256": sha256(args.kit / "check/stream.jsonl.gz"),
                "world_sha256": sha256(args.kit / "arena/v2/world.json"),
                "terrain_sha256": sha256(args.kit / "arena/v2/terrain.i16"),
                "bot_dir": str(args.bot_dir), "bot_source_sha256": source_hashes,
                "entrypoint": args.entrypoint,
                "bot_git": git_info(args.bot_dir), "seeds_start": args.seeds,
                "count": args.count, "workers": min(args.workers, args.count),
                "python": sys.version, "platform": sys.platform, "caveats": CAVEATS,
                "planning_clock": {"mode": "wall_clock"},
                "_start": time.perf_counter()}
    if args.supplies:
        metadata["extension_sha256"] = sha256(Path(__file__).with_name("supply_arena.py"))
        metadata["engine_changes"] = ["local supply-ring subclass"]
        metadata["caveats"] = [c.replace("; supply drops are absent", "") for c in CAVEATS] + [
            "Rings use sampled open ground and horizontal occupancy. Rival policies do not seek them.",
            "The exact drop distribution, landing sound and official upgrade queue are not modeled.",
        ]
    if args.cube_size:
        metadata["cube_size_sha256"] = sha256(Path(__file__).with_name("cube_size_arena.py"))
        metadata["engine_changes"].append("uncapped cube size scaling")
    if args.audit_places:
        metadata["placement_audit_sha256"] = sha256(Path(__file__).with_name("placement_audit.py"))
    if args.legal_spawns:
        metadata["spawn_arena_sha256"] = sha256(Path(__file__).with_name("spawn_arena.py"))
        metadata["engine_changes"].append("validate initial mech spawns against published prisms")
    if args.house_every_observation:
        metadata["engine_changes"].append("update house commands at each player observation")
    if args.movement_geometry:
        metadata["movement_arena_sha256"] = sha256(Path(__file__).with_name("movement_arena.py"))
        metadata["engine_changes"].append("grounded mech movement uses indexed prism checks")
    if args.fixed_planning:
        metadata["planning_clock"] = {
            "mode": "fixed_counter", "seconds_per_perf_counter_call": 0.000001,
            "scope": "evaluated bot navigation module only",
            "navigation_sha256": source_hashes.get("navigation.py"),
            "latency_clock": "real wall clock",
        }
        metadata["caveats"] = [c for c in metadata["caveats"]
                               if not c.startswith("Policies with wall-clock planning budgets")] + [
            "Diagnostic fixed planning clock changes bot A* time slicing; real latency is still measured.",
        ]
    results = []
    initargs = tuple(str(p) for p in (args.engine, args.kit, args.bot_dir)) + (
        source_hashes, args.supplies, args.fixed_planning, args.cube_size, args.audit_places,
        args.legal_spawns, args.house_every_observation, args.movement_geometry, args.entrypoint)
    pool = ProcessPoolExecutor(max_workers=metadata["workers"],
            mp_context=multiprocessing.get_context("spawn"), initializer=worker_init, initargs=initargs)
    futures = []
    try:
        futures = [pool.submit(run_seed, seed) for seed in range(args.seeds, args.seeds + args.count)]
        for future in as_completed(futures):
            result = future.result()  # Exceptions abort, never become a placement.
            results.append(result)
            print(json.dumps({"type": "match", **result}, allow_nan=False), flush=True)
        if bot_manifest(args.bot_dir) != source_hashes:
            raise RuntimeError("Bot source changed during the benchmark; results are invalid")
        report = write_results(args, metadata, results)
        print(json.dumps({"type": "summary", **report["summary"], "unofficial": True}), flush=True)
    except BaseException as exc:
        for future in futures:
            future.cancel()
        metadata["failure"] = f"{type(exc).__name__}: {exc}"
        write_results(args, metadata, results, status="failed")
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    main()
