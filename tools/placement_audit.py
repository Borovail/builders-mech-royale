"""Observe local SquadEpisode deaths and reconstruct its placement points.

This wrapper changes no actions, damage, physics, random state or clocks. It
records a mech's transition from positive to nonpositive HP after the original
hit() returns. NPC/crate deaths never contribute placement points. Attribution
means the *fatal hit*, not all damage or a causal claim about the strategy.

For a dead player the pinned engine awards a point for every rival dead no later
than the player (same-tick deaths are separated here). For a living player it
also awards points for living rivals with lower/equal HP. These HP points and
equal-HP ties are explicit; they are not reported as kills or zone deaths.
"""


def with_placement_audit(base_episode):
    class PlacementAuditEpisode(base_episode):
        def hit(self, src, dst, kind, damage):
            was_alive = dst.hp > 0
            result = super().hit(src, dst, kind, damage)
            if was_alive and dst.hp <= 0 and dst.kind == "mech":
                cause = ("zone" if kind == "zone" else "player" if src is self.player
                         else "other" if src is not None else "unknown")
                self._placement_deaths.append({
                    "id": dst.id, "t": self.dead[dst.id], "cause": cause,
                    "weapon": kind, "by": src.id if src is not None else None,
                    "source_kind": src.kind if src is not None else None,
                })
            return result

        def run(self, bot, trace):
            self._placement_deaths = []
            result = super().run(bot, trace)
            by_id = {death["id"]: death for death in self._placement_deaths}
            if set(by_id) != set(self.dead):
                raise RuntimeError("Audit did not observe every mech death")
            points = {key: 0 for key in ("zone", "player", "other", "unknown",
                                         "same_tick", "hp", "hp_tie")}
            rival_deaths = {key: 0 for key in ("zone", "player", "other", "unknown")}
            player_dead = self.player.id in self.dead
            player_time = self.dead.get(self.player.id)
            for ident in self.opponents:
                death = by_id.get(ident)
                if death is not None:
                    rival_deaths[death["cause"]] += 1
                    if player_dead and death["t"] == player_time:
                        points["same_tick"] += 1
                    elif not player_dead or death["t"] < player_time:
                        points[death["cause"]] += 1
                elif not player_dead:
                    hp = self.actors[ident].hp
                    if hp < self.player.hp:
                        points["hp"] += 1
                    elif hp == self.player.hp:
                        points["hp_tie"] += 1
            reconstructed = sum(points.values())
            if reconstructed != result["score"] or reconstructed != 16-result["place_proxy"]:
                raise RuntimeError(f"Attribution does not reconstruct placement: {points}, {result}")
            player_death = by_id.get(self.player.id)
            timeout = not player_dead and self.now >= self.duration
            end = ("dead" if player_dead else "timeout" if timeout
                   else "all_rivals_dead" if all(ident in self.dead for ident in self.opponents)
                   else "other_stop")
            result.update({"audit_points_"+key: value for key, value in points.items()})
            result.update({"audit_rival_deaths_"+key: value for key, value in rival_deaths.items()})
            result.update(audit_end=end,
                          audit_player_death_cause=player_death["cause"] if player_death else None,
                          audit_player_death_weapon=player_death["weapon"] if player_death else None,
                          audit_player_killer=player_death["by"] if player_death else None,
                          audit_deaths=list(self._placement_deaths))
            return result

    return PlacementAuditEpisode
