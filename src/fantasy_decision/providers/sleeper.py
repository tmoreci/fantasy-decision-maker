"""Sleeper provider: player index, injuries, depth charts, leagues and trending adds.

Sleeper's read API needs no authentication and no key, which makes it the natural
backbone for both roster import and injury/depth-chart signal.
"""

from __future__ import annotations

import logging
from typing import Any

from fantasy_decision.models import (
    InjuryInfo,
    LeagueSettings,
    PlayerRef,
    Position,
    ScoringSettings,
    TrendingSignal,
)
from fantasy_decision.providers.base import HttpProvider, ProviderError

logger = logging.getLogger("fantasy_decision.providers.sleeper")

BASE = "https://api.sleeper.app/v1"

PLAYER_INDEX_TTL = 60 * 60 * 12  # the index is large and only meaningfully changes daily
LEAGUE_TTL = 60 * 60
ROSTER_TTL = 60 * 5
TRENDING_TTL = 60 * 15
STATE_TTL = 60 * 30

VALID_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}


def _normalise(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


class SleeperProvider(HttpProvider):
    name = "sleeper"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._player_index: dict[str, dict[str, Any]] | None = None

    # ---------------------------------------------------------------- state

    def current_state(self) -> dict[str, Any]:
        """Current NFL week/season as Sleeper sees it."""
        payload = self.get_json(f"{BASE}/state/nfl", ttl_seconds=STATE_TTL)
        if not isinstance(payload, dict):
            raise ProviderError("sleeper: unexpected /state/nfl payload")
        return payload

    # --------------------------------------------------------------- players

    def player_index(self) -> dict[str, dict[str, Any]]:
        if self._player_index is None:
            payload = self.get_json(f"{BASE}/players/nfl", ttl_seconds=PLAYER_INDEX_TTL)
            if not isinstance(payload, dict):
                raise ProviderError("sleeper: unexpected /players/nfl payload")
            self._player_index = payload
        return self._player_index

    def resolve_player(self, query: str) -> PlayerRef:
        """Resolve a typed name (or a raw Sleeper id) to a PlayerRef.

        Matching is exact-on-normalised-full-name first, then unique substring. An
        ambiguous substring is an error rather than a guess: silently starting the
        wrong Kenneth Walker is worse than asking.
        """
        index = self.player_index()

        if query in index:
            return self._to_ref(query, index[query])

        target = _normalise(query)
        exact: list[tuple[str, dict[str, Any]]] = []
        partial: list[tuple[str, dict[str, Any]]] = []

        for player_id, record in index.items():
            if not isinstance(record, dict):
                continue
            if record.get("position") not in VALID_POSITIONS:
                continue
            full = record.get("full_name") or f"{record.get('first_name', '')} {record.get('last_name', '')}"
            normalised = _normalise(full)
            if not normalised:
                continue
            if normalised == target:
                exact.append((player_id, record))
            elif target and target in normalised:
                partial.append((player_id, record))

        candidates = exact or partial
        if not candidates:
            raise ProviderError(f"sleeper: no player matching {query!r}")
        if len(candidates) > 1:
            # Prefer an active player if that disambiguates cleanly.
            active = [c for c in candidates if c[1].get("status") == "Active"]
            if len(active) == 1:
                candidates = active
            else:
                names = ", ".join(
                    f"{c[1].get('full_name')} ({c[1].get('position')}-{c[1].get('team')})" for c in candidates[:6]
                )
                raise ProviderError(f"sleeper: {query!r} is ambiguous: {names}")

        player_id, record = candidates[0]
        return self._to_ref(player_id, record)

    @staticmethod
    def _to_ref(player_id: str, record: dict[str, Any]) -> PlayerRef:
        position = record.get("position")
        full = record.get("full_name") or f"{record.get('first_name', '')} {record.get('last_name', '')}".strip()
        return PlayerRef(
            name=full or player_id,
            position=position if position in VALID_POSITIONS else None,  # type: ignore[arg-type]
            team=record.get("team"),
            sleeper_id=player_id,
            gsis_id=record.get("gsis_id"),
        )

    def injury_for(self, ref: PlayerRef) -> InjuryInfo:
        if not ref.sleeper_id:
            raise ProviderError("sleeper: player has no sleeper id")
        record = self.player_index().get(ref.sleeper_id)
        if not isinstance(record, dict):
            raise ProviderError(f"sleeper: no record for {ref.name}")
        order = record.get("depth_chart_order")
        return InjuryInfo(
            status=record.get("injury_status") or None,
            body_part=record.get("injury_body_part") or None,
            notes=record.get("injury_notes") or None,
            depth_chart_position=record.get("depth_chart_position") or None,
            depth_chart_order=int(order) if isinstance(order, (int, float)) else None,
        )

    # -------------------------------------------------------------- trending

    def trending(self, kind: str = "add", *, lookback_hours: int = 24, limit: int = 200) -> dict[str, tuple[int, int]]:
        """Map player_id -> (count, rank) for trending adds or drops."""
        payload = self.get_json(
            f"{BASE}/players/nfl/trending/{kind}",
            ttl_seconds=TRENDING_TTL,
            params={"lookback_hours": lookback_hours, "limit": limit},
        )
        if not isinstance(payload, list):
            raise ProviderError("sleeper: unexpected trending payload")
        result: dict[str, tuple[int, int]] = {}
        for rank, entry in enumerate(payload, start=1):
            if isinstance(entry, dict) and entry.get("player_id") is not None:
                result[str(entry["player_id"])] = (int(entry.get("count", 0)), rank)
        return result

    def trending_signal(self, ref: PlayerRef, adds: dict[str, tuple[int, int]], drops: dict[str, tuple[int, int]]) -> TrendingSignal:
        add = adds.get(ref.sleeper_id or "")
        drop = drops.get(ref.sleeper_id or "")
        return TrendingSignal(
            adds_24h=add[0] if add else None,
            add_rank=add[1] if add else None,
            drops_24h=drop[0] if drop else None,
        )

    # --------------------------------------------------------------- leagues

    def user_id(self, username: str) -> str:
        payload = self.get_json(f"{BASE}/user/{username}", ttl_seconds=LEAGUE_TTL)
        if not isinstance(payload, dict) or not payload.get("user_id"):
            raise ProviderError(f"sleeper: no such user {username!r}")
        return str(payload["user_id"])

    def leagues_for_user(self, user_id: str, season: str) -> list[dict[str, Any]]:
        payload = self.get_json(f"{BASE}/user/{user_id}/leagues/nfl/{season}", ttl_seconds=LEAGUE_TTL)
        if not isinstance(payload, list):
            raise ProviderError("sleeper: unexpected leagues payload")
        return [entry for entry in payload if isinstance(entry, dict)]

    def league(self, league_id: str) -> LeagueSettings:
        payload = self.get_json(f"{BASE}/league/{league_id}", ttl_seconds=LEAGUE_TTL)
        if not isinstance(payload, dict):
            raise ProviderError(f"sleeper: no league {league_id}")
        return LeagueSettings(
            name=payload.get("name") or f"League {league_id}",
            scoring=scoring_from_sleeper(payload.get("scoring_settings") or {}),
            roster_positions=[str(p) for p in payload.get("roster_positions") or []],
            source="sleeper",
        )

    def rosters(self, league_id: str) -> list[dict[str, Any]]:
        payload = self.get_json(f"{BASE}/league/{league_id}/rosters", ttl_seconds=ROSTER_TTL)
        if not isinstance(payload, list):
            raise ProviderError("sleeper: unexpected rosters payload")
        return [entry for entry in payload if isinstance(entry, dict)]

    def roster_for_user(self, league_id: str, user_id: str) -> dict[str, Any]:
        for roster in self.rosters(league_id):
            if str(roster.get("owner_id")) == str(user_id):
                return roster
        raise ProviderError(f"sleeper: user {user_id} has no roster in league {league_id}")

    def bench_candidates(self, league_id: str, user_id: str, position: str | None = None) -> list[PlayerRef]:
        """Players on the user's roster, most useful first, optionally filtered by position."""
        roster = self.roster_for_user(league_id, user_id)
        player_ids = [str(p) for p in roster.get("players") or []]
        index = self.player_index()
        refs: list[PlayerRef] = []
        for player_id in player_ids:
            record = index.get(player_id)
            if not isinstance(record, dict):
                continue
            if record.get("position") not in VALID_POSITIONS:
                continue
            if position and record.get("position") != position.upper():
                continue
            refs.append(self._to_ref(player_id, record))
        return refs


def scoring_from_sleeper(raw: dict[str, Any]) -> ScoringSettings:
    """Translate Sleeper's scoring_settings blob into our settings model.

    Sleeper stores per-stat multipliers; the handful below are the ones that
    actually change a start/sit call.
    """

    def value(key: str, default: float) -> float:
        got = raw.get(key)
        return float(got) if isinstance(got, (int, float)) else default

    reception = value("rec", 1.0)
    te_bonus = value("bonus_rec_te", 0.0)

    if reception >= 1.0:
        name = "PPR"
    elif reception >= 0.5:
        name = "Half-PPR"
    elif reception > 0:
        name = f"{reception}-PPR"
    else:
        name = "Standard"
    if te_bonus:
        name += f" (+{te_bonus} TE premium)"

    notes: list[str] = []
    if value("bonus_rec_yd_100", 0.0):
        notes.append("100-yard receiving bonus")
    if value("bonus_rush_yd_100", 0.0):
        notes.append("100-yard rushing bonus")
    if value("pass_td", 4.0) >= 6:
        notes.append("6-point passing touchdowns")

    return ScoringSettings(
        name=name,
        reception=reception,
        te_reception_bonus=te_bonus,
        pass_td=value("pass_td", 4.0),
        rush_rec_td=value("rec_td", 6.0),
        pass_yard=value("pass_yd", 0.04),
        rush_rec_yard=value("rec_yd", 0.1),
        interception=value("pass_int", -2.0),
        fumble_lost=value("fum_lost", -2.0),
        bonus_notes=notes,
    )


PRESETS: dict[str, ScoringSettings] = {
    "ppr": ScoringSettings(name="PPR", reception=1.0),
    "half": ScoringSettings(name="Half-PPR", reception=0.5),
    "standard": ScoringSettings(name="Standard", reception=0.0),
}
