"""Gather every signal we can about each candidate, then shape it for Jev.

Two responsibilities:

1. `gather_dossiers` fans out across the providers, tolerating failures and
   recording each gap by name.
2. `build_state` turns the dossiers into the JSON `state` payload that every
   question in a request is evaluated against.
"""

from __future__ import annotations

import logging
from typing import Any

from fantasy_decision.models import LeagueSettings, PlayerDossier, PlayerRef
from fantasy_decision.providers.base import soft_call
from fantasy_decision.providers.espn import EspnProvider
from fantasy_decision.providers.nflverse import NflverseProvider
from fantasy_decision.providers.sleeper import SleeperProvider

logger = logging.getLogger("fantasy_decision.dossier")


def gather_dossiers(
    refs: list[PlayerRef],
    *,
    season: str,
    week: int,
    sleeper: SleeperProvider,
    espn: EspnProvider | None = None,
    nflverse: NflverseProvider | None = None,
) -> dict[str, PlayerDossier]:
    """Build a dossier per player. Never raises for a missing signal."""
    adds: dict[str, tuple[int, int]] = {}
    drops: dict[str, tuple[int, int]] = {}
    shared_missing: list[str] = []
    trending = soft_call("sleeper trending adds", lambda: sleeper.trending("add"), shared_missing)
    if trending is not None:
        adds = trending
    trending_drops = soft_call("sleeper trending drops", lambda: sleeper.trending("drop"), shared_missing)
    if trending_drops is not None:
        drops = trending_drops

    dossiers: dict[str, PlayerDossier] = {}
    for ref in refs:
        missing: list[str] = list(shared_missing)
        dossier = PlayerDossier(ref=ref, missing=missing)

        injury = soft_call(f"injury status for {ref.name}", lambda r=ref: sleeper.injury_for(r), missing)
        if injury is not None:
            dossier.injury = injury

        signal = soft_call(
            f"trending signal for {ref.name}",
            lambda r=ref: sleeper.trending_signal(r, adds, drops),
            missing,
        )
        if signal is not None:
            dossier.trending = signal

        if espn is not None:
            game = soft_call(
                f"game context and Vegas line for {ref.name}",
                lambda r=ref: espn.game_context(r.team, season, week),
                missing,
            )
            if game is not None:
                dossier.game = game
        else:
            missing.append(f"game context and Vegas line for {ref.name}")

        if nflverse is not None:
            usage = soft_call(
                f"advanced usage metrics for {ref.name}",
                lambda r=ref: nflverse.usage_for(r, int(season), week),
                missing,
            )
            if usage is not None:
                dossier.usage = usage
        else:
            missing.append(f"advanced usage metrics for {ref.name}")

        dossier.missing = missing
        dossiers[dossier.slug] = dossier

    return dossiers


def _prune(value: Any) -> Any:
    """Drop null and empty fields, recursively.

    Token savings, not information loss: anything genuinely unknown is still named
    explicitly in the dossier's `missing` list, which is what tells a calibrated
    model to widen its uncertainty.
    """
    if isinstance(value, dict):
        cleaned = {k: _prune(v) for k, v in value.items() if v is not None}
        return {k: v for k, v in cleaned.items() if v not in ({}, [], "")}
    if isinstance(value, list):
        return [_prune(v) for v in value if v is not None]
    return value


def dossier_payload(dossier: PlayerDossier) -> dict[str, Any]:
    payload = _prune(dossier.model_dump(mode="json", exclude={"missing"}))
    payload["unavailable_data"] = dossier.missing or ["none - all sources reported"]
    return payload


def build_state(
    dossiers: dict[str, PlayerDossier],
    league: LeagueSettings,
    *,
    season: str,
    week: int,
    need: str = "auto",
) -> dict[str, Any]:
    """The shared state every question in a request is evaluated against."""
    return {
        "question_context": (
            "A fantasy football manager must choose exactly one of these players to start "
            f"in week {week} of the {season} NFL season. Only one can be started; the others sit on the bench."
        ),
        "week": week,
        "season": season,
        "manager_situation": {
            "auto": "No stated preference; pick the best expected outcome.",
            "safe": "The manager is favoured this week and wants the reliable floor.",
            "upside": "The manager is an underdog this week and needs a high ceiling.",
        }.get(need, "No stated preference; pick the best expected outcome."),
        "league": _prune(league.model_dump(mode="json")),
        "candidates": {slug: dossier_payload(d) for slug, d in dossiers.items()},
        "field_guide": {
            "implied_team_total": "Points Vegas expects this player's offense to score. Above ~26 is a strong offensive environment; below ~19 is poor.",
            "spread": "Relative to this player's own team. Negative means favoured.",
            "snap_pct": "Share of offensive snaps played, 0 to 1.",
            "target_share": "Share of the team's targets, 0 to 1.",
            "wopr": "Weighted opportunity rating, combining target share and air yards share. Above 0.6 is a primary option.",
            "snap_trend": "Recent 3-game snap share minus season average. Positive means a growing role.",
            "adds_24h": "How many fantasy managers added this player in the last day; a spike usually means breaking news.",
            "depth_chart_order": "1 is the starter at that spot.",
            "unavailable_data": "Signals we could not retrieve. Treat these as unknown, not as neutral or favourable.",
        },
    }
