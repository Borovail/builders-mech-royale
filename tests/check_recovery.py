"""Independent public Bot.act A/B check for the E11 regeneration experiment.

python -B tests/check_recovery.py --kit KIT --baseline E07/example_bot.py

Protocol section 6: five seconds without dealing OR taking damage, then 45 HP/s
inside the zone. This is a fixed-input firing/resource fixture, not a duel or
match simulation. Position, visibility and opponent behavior are prescribed;
movement/aim outputs are checked against E07 but not integrated. A quiet enemy
is 180 m away: within official 200 m sight and 230 m gun range. The deterministic
hit assumption is 100% for valid gun shots after projectile flight time; this
isolates how successful outgoing fire resets regeneration. Gun magazine,
cooldown, reload and HP timing use hello constants; spread, combat retaliation,
air physics and other weapons are omitted. No placement/score is estimated.
"""

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile

from check_routes import EPS, load_kit
from check_economy import action, load_bot, observation, source_hashes


def fixture(hello, template, arena, tick, hp=500.0, distance=180.0):
    start, goal = (-280.0, -280.0), (-180.0, -300.0)
    obs = observation(hello, template, arena, tick, start, goal)
    obs["self"].update(hp=hp, energy=100.0, aim=[0.0, 0.0])
    obs["self"]["weapons"][1] = None
    point = (start[0]+distance, start[1])
    obs["mechs"] = [{"id": 5, "kind": "mech", "pos": [*point, arena.height(point)],
                      "vel": [0.0, 0.0, 0.0], "aim": [0.0, 0.0],
                      "hp": 1400.0, "max_hp": 1400.0, "weapons": ["gun"]}]
    return obs


def without_fire(command):
    return {key: value for key, value in command.items() if key != "fire"}


def establish_history(bot, reference, hello, template, arena):
    # A real low-HP observation five seconds before the measurement avoids
    # claiming that a newly started Bot knows an unobserved damage history.
    # The measurement begins with an outgoing hit just as regen was eligible.
    tick = int((70-hello["config"]["mech"]["regen_delay"])*hello["config"]["match"]["tick_hz"])
    obs = fixture(hello, template, arena, tick)
    obs["mechs"] = []
    obs["events"] = [{"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 0.0, "by": 5}]
    action(bot, copy.deepcopy(obs))
    action(reference, copy.deepcopy(obs))


def simulate(factory, control, hello, template, arena, incoming_at=None, outside_at=None):
    config, stats = hello["config"], hello["config"]["mech"]
    gun = config["weapons"]["gun"]
    hz, every = config["match"]["tick_hz"], config["match"]["decide_every"]
    dt, seconds, start_time = 1/hz, 10.0, 70.0
    first_tick = int(start_time*hz)
    bot, reference = factory(copy.deepcopy(hello)), control(copy.deepcopy(hello))
    establish_history(bot, reference, hello, template, arena)
    hp, maximum, enemy_hp = 500.0, float(stats["hp"]), 1400.0
    cooldown, reload_s, ammo = 0.0, 0.0, int(gun["mag"])
    last_damage, first_regen, restored = 0.0, None, 0.0
    events = [{"type": "hit", "target": 5, "amount": 45, "weapon": "gun"}]
    shots, hits, pauses, compared, changed, fire_reenabled = 0, 0, 0, 0, [], None
    pending, command, incoming_applied = [], {"fire": [False, False]}, False
    for frame in range(round(seconds*hz)):
        now, tick = frame/hz, first_tick+frame
        outside = outside_at is not None and now+EPS >= outside_at
        cooldown = max(0.0, cooldown-dt)
        if reload_s > 0:
            reload_s = max(0.0, reload_s-dt)
            if reload_s <= EPS:
                ammo = int(gun["mag"])
        for at in list(pending):
            if at <= now+EPS:
                pending.remove(at)
                last_damage = now
                hits += 1
                enemy_hp -= gun["damage"]
                events.append({"type": "hit", "target": 5, "amount": gun["damage"], "weapon": "gun"})
        if incoming_at is not None and now+EPS >= incoming_at and not incoming_applied:
            hp -= gun["damage"]
            last_damage, incoming_applied = now, True
            events.append({"type": "damaged", "amount": gun["damage"], "weapon": "gun", "bearing": 0.0, "by": 5})
        if frame % every == 0:
            obs = fixture(hello, template, arena, tick, hp)
            obs["self"]["in_zone"] = not outside
            obs["mechs"][0]["hp"] = max(1.0, enemy_hp)
            weapon = obs["self"]["weapons"][0]
            weapon.update(ammo=ammo, cooldown=cooldown, reload=reload_s)
            obs["events"], events = events, []
            command = action(bot, copy.deepcopy(obs))
            original = action(reference, copy.deepcopy(obs))
            compared += 1
            if without_fire(command) != without_fire(original):
                changed.append(tick)
            firing = any(command.get("fire", [False, False]))
            pauses += not firing
            if (incoming_applied or outside) and firing and fire_reenabled is None:
                fire_reenabled = now
        if command.get("fire", [False])[0] and cooldown <= EPS and reload_s <= EPS:
            if ammo:
                ammo -= 1
                cooldown = gun["cooldown"]
                shots += 1
                pending.append(now+180.0/gun["speed"])
            else:
                reload_s = gun["reload"]
        if outside:
            hp = max(0.0, hp-template["zone"]["dps"]*dt)
            last_damage = now
        elif now-last_damage+EPS >= stats["regen_delay"]:
            gain = min(maximum-hp, stats["regen_per_s"]*dt)
            hp += gain
            restored += gain
            if gain > 0 and first_regen is None:
                first_regen = now
    return {"scenario": "new_incoming" if incoming_at is not None else "outside_zone" if outside_at is not None else "quiet_distant_target",
            "observations": compared, "paused_fire_observations": pauses,
            "shots": shots, "hits": hits, "enemy_hp_remaining": round(enemy_hp, 3),
            "outgoing_damage": hits*gun["damage"],
            "hp_restored": round(restored, 3), "hp_final": round(hp, 3),
            "first_regen_s": first_regen, "fire_reenabled_s": fire_reenabled,
            "non_fire_action_differences": len(changed), "first_changed_ticks": changed[:10]}


def safety(factory, baseline, hello, template, arena):
    hz, every = hello["config"]["match"]["tick_hz"], hello["config"]["match"]["decide_every"]
    tick = int(70*hz)
    results = []
    for mode in ("full_hp", "match_start", "cold_low_hp", "incoming", "urgent_zone", "outside_zone", "close_enemy"):
        bot, previous = factory(copy.deepcopy(hello)), baseline(copy.deepcopy(hello))
        establish_history(bot, previous, hello, template, arena)
        obs = fixture(hello, template, arena, tick)
        obs["events"] = [{"type": "hit", "target": 5, "amount": 45, "weapon": "gun"}]
        # Establish quiet low-HP history before independently changing a guard.
        for step in range(0, int(3*hz), int(every)):
            initial = copy.deepcopy(obs)
            initial["tick"], initial["t"] = tick+step, 70+step/hz
            initial["events"] = obs["events"] if step == 0 else []
            first, _ = action(bot, copy.deepcopy(initial)), action(previous, copy.deepcopy(initial))
        obs["tick"], obs["t"], obs["events"] = tick+int(3*hz), 73.0, []
        if mode == "full_hp":
            obs["self"]["hp"] = obs["self"]["max_hp"]
        elif mode == "match_start":
            bot, previous = factory(copy.deepcopy(hello)), baseline(copy.deepcopy(hello))
            obs = fixture(hello, template, arena, 0, hello["config"]["mech"]["hp"])
            first = {"fire": [True, False]}
        elif mode == "cold_low_hp":
            bot, previous = factory(copy.deepcopy(hello)), baseline(copy.deepcopy(hello))
            obs = fixture(hello, template, arena, tick)
            obs["events"] = [{"type": "hit", "target": 5, "amount": 45, "weapon": "gun"}]
            first = {"fire": [True, False]}
        elif mode == "incoming":
            obs["events"] = [{"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 0.0, "by": 5}]
            obs["self"]["hp"] -= 45
        elif mode == "urgent_zone":
            obs["zone"].update(next_radius=10.0, shrink_start=72.0, shrink_end=74.0)
        elif mode == "outside_zone":
            obs["self"]["in_zone"] = False
        elif mode == "close_enemy":
            obs["mechs"][0]["pos"][0] = obs["self"]["pos"][0]+30
        a, b = action(bot, copy.deepcopy(obs)), action(previous, copy.deepcopy(obs))
        nonfire_equal = without_fire(a) == without_fire(b)
        # When still in-zone, suppressing only fire does not delay E07's
        # escape movement. A future circle alone need not force firing.
        passed = nonfire_equal if mode == "urgent_zone" else a == b and any(a.get("fire", [False, False]))
        if mode not in ("match_start", "cold_low_hp", "urgent_zone"):
            passed &= not any(first.get("fire", [False, False]))
        results.append({"scenario": "guard_"+mode, "previously_paused": not any(first.get("fire", [False, False])),
                        "same_action_as_e07": a == b, "non_fire_actions_equal": nonfire_equal,
                        "firing": any(a.get("fire", [False, False])), "passed": bool(passed)})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", type=Path, required=True)
    parser.add_argument("--bot", type=Path, default=Path(__file__).resolve().parents[1]/"example_bot.py")
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mech-recovery-check-") as temporary:
        hello, template, arena = load_kit(args.kit.resolve(), Path(temporary))
        factory, baseline = load_bot(args.bot, "recovery_candidate"), load_bot(args.baseline, "recovery_baseline")
        print("model=fixed-input-regeneration hit_fraction=1 hit_delay=distance/gun_speed opponent_retaliation=none score=none")
        print("candidate_sources="+json.dumps(source_hashes(args.bot), sort_keys=True))
        print("baseline_sources="+json.dumps(source_hashes(args.baseline), sort_keys=True))
        print("world_sha256="+arena.world_sha256)
        protocol = Path(hello["map"]["dir"]).parent/"protocol.md"
        if protocol.is_file():
            print("protocol_sha256="+hashlib.sha256(protocol.read_bytes()).hexdigest())
        print("regen_rules="+json.dumps({k: hello["config"]["mech"][k] for k in ("regen_delay", "regen_per_s")}))
        if not 180 <= min(hello["config"]["vision"]["range"], hello["config"]["weapons"]["gun"]["range"]):
            raise ValueError("Quiet target must be both visible and within gun range")
        before = simulate(baseline, baseline, hello, template, arena)
        after = simulate(factory, baseline, hello, template, arena)
        after["passed"] = (after["hp_restored"] > before["hp_restored"] and after["hp_restored"] > 0
                           and after["first_regen_s"] >= hello["config"]["mech"]["regen_delay"]
                           and after["non_fire_action_differences"] == 0 and after["enemy_hp_remaining"] > 0)
        incoming = simulate(factory, baseline, hello, template, arena, incoming_at=2.0)
        incoming["passed"] = (incoming["fire_reenabled_s"] is not None and incoming["fire_reenabled_s"] <= 2.1
                              and (incoming["first_regen_s"] is None or incoming["first_regen_s"] >= 7.0)
                              and incoming["non_fire_action_differences"] == 0)
        outside = simulate(factory, baseline, hello, template, arena, outside_at=2.0)
        outside["passed"] = (outside["fire_reenabled_s"] is not None and outside["fire_reenabled_s"] <= 2.1
                             and outside["hp_restored"] == 0 and outside["non_fire_action_differences"] == 0)
        results = [after, incoming, outside]+safety(factory, baseline, hello, template, arena)
        print("baseline="+json.dumps(before, sort_keys=True))
        for result in results:
            print("candidate="+json.dumps(result, sort_keys=True))
        passed = all(r["passed"] for r in results)
        print("totals="+json.dumps({"passed": passed, "additional_hp_restored": after["hp_restored"]-before["hp_restored"]}, sort_keys=True))
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
