"""The decision pipeline, independent of any interface.

The CLI and the web app both call `run_decision`. It raises rather than printing or
exiting, so each front end decides how a failure should look to its users.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from fantasy_decision.cache import DEFAULT_CACHE_DIR
from fantasy_decision.calibrate import record_from_decision
from fantasy_decision.decide import MAX_CANDIDATES, MIN_CANDIDATES, decide
from fantasy_decision.dossier import gather_dossiers
from fantasy_decision.models import Decision, LeagueSettings, PlayerRef
from fantasy_decision.providers.base import ProviderError
from fantasy_decision.providers.espn import EspnProvider
from fantasy_decision.providers.nflverse import NflverseProvider
from fantasy_decision.providers.sleeper import PRESETS, SleeperProvider

if TYPE_CHECKING:
    from typesafe_sdk import TypeSafeClient

logger = logging.getLogger("fantasy_decision.service")

HISTORY_PATH = DEFAULT_CACHE_DIR / "decisions.jsonl"
NEEDS = ("auto", "safe", "upside")


class InputError(ValueError):
    """The request itself is wrong (bad player name, wrong count). The user can fix it."""


class ConfigError(RuntimeError):
    """The deployment is missing something, such as the TypeSafe API key."""


class DecisionOutcome(BaseModel):
    decision: Decision
    notes: list[str] = Field(default_factory=list)


def resolve_league(sleeper: SleeperProvider, league_id: str | None, scoring: str) -> tuple[LeagueSettings, list[str]]:
    """The league's real scoring when we can read it, else a preset. Returns any notes about the fallback."""
    notes: list[str] = []
    league_id = league_id or os.environ.get("SLEEPER_LEAGUE_ID")
    if league_id:
        try:
            return sleeper.league(league_id), notes
        except ProviderError as error:
            notes.append(f"Could not read league {league_id} ({error}); falling back to {scoring} scoring.")

    preset = PRESETS.get(scoring.lower())
    if preset is None:
        raise InputError(f"Unknown scoring preset {scoring!r}. Use ppr, half or standard.")
    return LeagueSettings(name=f"Manual ({preset.name})", scoring=preset, source="manual"), notes


def resolve_players(sleeper: SleeperProvider, players: Sequence[str | PlayerRef]) -> list[PlayerRef]:
    """Names or Sleeper ids to PlayerRefs. An unknown or ambiguous name is an InputError."""
    if not MIN_CANDIDATES <= len(players) <= MAX_CANDIDATES:
        raise InputError("Give me two or three players to choose between.")

    refs: list[PlayerRef] = []
    for player in players:
        if isinstance(player, PlayerRef):
            refs.append(player)
            continue
        try:
            refs.append(sleeper.resolve_player(player))
        except ProviderError as error:
            raise InputError(str(error)) from error

    if len({ref.slug for ref in refs}) != len(refs):
        raise InputError("Those resolve to the same player. Give me distinct options.")
    return refs


def run_decision(  # noqa: PLR0913
    players: Sequence[str | PlayerRef],
    *,
    sleeper: SleeperProvider,
    espn: EspnProvider,
    nflverse: NflverseProvider | None = None,
    week: int | None = None,
    season: str | None = None,
    need: str = "auto",
    league_id: str | None = None,
    scoring: str = "ppr",
    client: TypeSafeClient | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> DecisionOutcome:
    """Resolve, gather, and ask Jev. Raises InputError, ConfigError, ProviderError or DecisionError."""
    if need not in NEEDS:
        raise InputError(f"need must be one of: {', '.join(NEEDS)}")
    if client is None and not os.environ.get("TYPESAFE_API_KEY"):
        raise ConfigError("TYPESAFE_API_KEY is not set.")

    stage = on_stage or (lambda _: None)

    state = sleeper.current_state()
    resolved_week = week or int(state.get("week") or 1)
    resolved_season = season or str(state.get("season") or "")

    league, notes = resolve_league(sleeper, league_id, scoring)
    refs = resolve_players(sleeper, players)

    stage("Gathering data...")
    dossiers = gather_dossiers(
        refs,
        season=resolved_season,
        week=resolved_week,
        sleeper=sleeper,
        espn=espn,
        nflverse=nflverse,
    )

    stage("Asking Jev...")
    arguments: dict[str, Any] = {"season": resolved_season, "week": resolved_week, "need": need}
    if client is not None:
        decision = decide(dossiers, league, client=client, **arguments)
    else:
        from typesafe_sdk import TypeSafeClient  # noqa: PLC0415 - keeps imports light

        with TypeSafeClient() as owned:
            decision = decide(dossiers, league, client=owned, **arguments)

    return DecisionOutcome(decision=decision, notes=notes)


def append_history(decision: Decision, path: Path = HISTORY_PATH) -> None:
    """Log every call so `calibrate` has real history to score later."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = record_from_decision(decision)
        names = {slug: d.ref.name for slug, d in decision.dossiers.items()}
        gsis = {slug: d.ref.gsis_id for slug, d in decision.dossiers.items() if d.ref.gsis_id}
        payload = record.model_dump(mode="json") | {"names": names, "gsis_ids": gsis}
        with path.open("a") as handle:
            handle.write(json.dumps(payload) + "\n")
    except OSError as error:
        logger.debug("could not write history: %s", error)
