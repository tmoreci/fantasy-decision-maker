"""What the web app can do, independent of whether the answer is JSON or HTML.

`api.py` serialises these results; `pages.py` renders them into templates. Keeping
the logic here means a future JavaScript front end or chat bot gets exactly the
behaviour the HTMX pages have.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from fantasy_decision.decide import MAX_CANDIDATES, MIN_CANDIDATES, DecisionError
from fantasy_decision.explain import Explanation, explain
from fantasy_decision.models import Decision, PlayerRef
from fantasy_decision.providers.base import ProviderError
from fantasy_decision.providers.espn import EspnProvider
from fantasy_decision.providers.nflverse import NflverseProvider
from fantasy_decision.providers.sleeper import SleeperProvider
from fantasy_decision.service import ConfigError, InputError, append_history, run_decision

logger = logging.getLogger("fantasy_decision.web")

POSITION_ORDER = ["QB", "RB", "WR", "TE", "K", "DEF"]


@dataclass
class Services:
    """Long-lived providers shared by every request, so the 5MB player index loads once."""

    sleeper: SleeperProvider
    espn: EspnProvider
    nflverse: NflverseProvider | None = None
    # Returns a Jev client for one decision, or None to let the service open a real one.
    make_client: Callable[[], Any | None] = field(default=lambda: None)

    def close(self) -> None:
        self.sleeper.close()
        self.espn.close()


# ------------------------------------------------------------------ view models


class PlayerOption(BaseModel):
    sleeper_id: str
    name: str
    position: str | None = None
    team: str | None = None
    injury_status: str | None = None
    starter: bool = False

    @classmethod
    def from_ref(cls, ref: PlayerRef, *, injury_status: str | None = None, starter: bool = False) -> PlayerOption:
        return cls(
            sleeper_id=ref.sleeper_id or ref.name,
            name=ref.name,
            position=ref.position,
            team=ref.team,
            injury_status=injury_status,
            starter=starter,
        )


class LeagueOption(BaseModel):
    league_id: str
    name: str
    teams: int | None = None


class DecideRequest(BaseModel):
    players: list[str] = Field(
        min_length=MIN_CANDIDATES,
        max_length=MAX_CANDIDATES,
        description="Sleeper player ids (preferred) or names.",
    )
    league_id: str | None = None
    scoring: Literal["ppr", "half", "standard"] = "ppr"
    need: Literal["auto", "safe", "upside"] = "auto"
    week: int | None = None
    season: str | None = None


class DecisionView(BaseModel):
    decision: Decision
    explanation: Explanation
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------- actions


def current_week(services: Services) -> int | None:
    try:
        return int(services.sleeper.current_state().get("week") or 0) or None
    except (ProviderError, ValueError):
        return None


def search_players(services: Services, query: str, limit: int = 8) -> list[PlayerOption]:
    sleeper = services.sleeper
    results = []
    for ref in sleeper.search_players(query, limit=limit):
        try:
            status = sleeper.injury_for(ref).status
        except ProviderError:
            status = None
        results.append(PlayerOption.from_ref(ref, injury_status=status))
    return results


def list_leagues(services: Services, username: str) -> list[LeagueOption]:
    sleeper = services.sleeper
    season = str(sleeper.current_state().get("season") or "")
    user_id = sleeper.user_id(username.strip())
    return [
        LeagueOption(
            league_id=str(league.get("league_id")),
            name=str(league.get("name") or "Unnamed league"),
            teams=league.get("total_rosters") if isinstance(league.get("total_rosters"), int) else None,
        )
        for league in sleeper.leagues_for_user(user_id, season)
        if league.get("league_id")
    ]


def roster_players(services: Services, username: str, league_id: str) -> list[PlayerOption]:
    """The user's roster, current starters flagged, grouped by position."""
    sleeper = services.sleeper
    user_id = sleeper.user_id(username.strip())
    starters = {str(p) for p in sleeper.roster_for_user(league_id, user_id).get("starters") or []}

    players = []
    for ref in sleeper.bench_candidates(league_id, user_id):
        try:
            status = sleeper.injury_for(ref).status
        except ProviderError:
            status = None
        players.append(PlayerOption.from_ref(ref, injury_status=status, starter=ref.sleeper_id in starters))

    def order(player: PlayerOption) -> tuple[int, bool, str]:
        position = POSITION_ORDER.index(player.position) if player.position in POSITION_ORDER else len(POSITION_ORDER)
        return position, not player.starter, player.name

    return sorted(players, key=order)


def make_decision(services: Services, request: DecideRequest) -> DecisionView:
    outcome = run_decision(
        request.players,
        sleeper=services.sleeper,
        espn=services.espn,
        nflverse=services.nflverse,
        week=request.week,
        season=request.season,
        need=request.need,
        league_id=request.league_id or None,
        scoring=request.scoring,
        client=services.make_client(),
    )
    append_history(outcome.decision)
    return DecisionView(decision=outcome.decision, explanation=explain(outcome.decision), notes=outcome.notes)


# ------------------------------------------------------------------ error copy


def friendly_error(error: Exception) -> str:
    """Plain-language copy for the errors a non-technical user can actually hit."""
    message = re.sub(r"^sleeper: ", "", str(error))

    if isinstance(error, ConfigError):
        return "This app isn't fully set up yet: the server has no TypeSafe API key. Let the league admin know."
    if isinstance(error, DecisionError):
        return "The prediction service didn't give a usable answer. Try again in a minute."
    if match := re.match(r"no such user '(.*)'", message):
        return f"Couldn't find a Sleeper user called “{match[1]}”. Check the spelling (it's your username, not your team name)."
    if "has no roster in league" in message:
        return "You don't have a team in that league. Pick a different league."
    if match := re.match(r"no player matching '(.*)'", message):
        return f"Couldn't find a player called “{match[1]}”. Try their full name."
    if match := re.match(r"'(.*)' is ambiguous: (.*)", message):
        return f"“{match[1]}” matches more than one player ({match[2]}). Use the search to pick the right one."
    if isinstance(error, InputError):
        return message
    if isinstance(error, ProviderError):
        return f"Couldn't reach one of the stats sources ({message}). Try again in a minute."
    return "Something went wrong on our end. Try again in a minute."
