"""ESPN scoreboard provider: schedule, opponent, and the Vegas line.

The market number matters more than any single box score. Rather than ask Jev to
reason about a spread, we convert spread + over/under into an *implied team total*
here, deterministically, and hand it the resulting points expectation.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fantasy_decision.models import GameContext
from fantasy_decision.providers.base import HttpProvider, ProviderError

logger = logging.getLogger("fantasy_decision.providers.espn")

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
SCOREBOARD_TTL = 60 * 10

# Sleeper and ESPN agree on almost every abbreviation. These are the exceptions.
TEAM_ALIASES = {
    "WAS": "WSH",
    "WSH": "WAS",
    "JAC": "JAX",
    "JAX": "JAC",
    "LA": "LAR",
    "OAK": "LV",
    "SD": "LAC",
    "STL": "LAR",
}

_DETAILS_RE = re.compile(r"^([A-Z]{2,4})\s*([+-]?\d+(?:\.\d+)?)$")


def team_matches(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    a, b = a.upper(), b.upper()
    return a == b or TEAM_ALIASES.get(a) == b or TEAM_ALIASES.get(b) == a


def parse_spread_details(details: str | None) -> tuple[str | None, float | None]:
    """Parse ESPN's `odds.details` string into (favorite_abbrev, line_magnitude).

    Examples: "KC -3.5" -> ("KC", 3.5); "EVEN" / "PK" -> (None, 0.0).
    """
    if not details:
        return None, None
    text = details.strip().upper()
    if text in {"EVEN", "PK", "PICK", "PICK'EM", "PICKEM"}:
        return None, 0.0
    match = _DETAILS_RE.match(text.replace("  ", " "))
    if not match:
        return None, None
    abbrev, raw_line = match.group(1), float(match.group(2))
    return abbrev, abs(raw_line)


def implied_totals(team_spread: float | None, over_under: float | None) -> tuple[float | None, float | None]:
    """Split an over/under across two teams using the spread.

    `team_spread` is relative to the team of interest: negative means favored. The
    identity is implied = total/2 - spread/2, so a 47.5 total with a -3.5 spread
    gives the favourite 25.5 and the underdog 22.0.
    """
    if over_under is None or team_spread is None:
        return None, None
    half = over_under / 2.0
    team = round(half - team_spread / 2.0, 2)
    opponent = round(half + team_spread / 2.0, 2)
    return team, opponent


class EspnProvider(HttpProvider):
    name = "espn"

    def scoreboard(self, season: str, week: int, season_type: int = 2) -> list[dict[str, Any]]:
        payload = self.get_json(
            SCOREBOARD,
            ttl_seconds=SCOREBOARD_TTL,
            params={"dates": season, "seasontype": season_type, "week": week},
        )
        if not isinstance(payload, dict):
            raise ProviderError("espn: unexpected scoreboard payload")
        events = payload.get("events")
        if not isinstance(events, list):
            raise ProviderError("espn: scoreboard has no events")
        return [event for event in events if isinstance(event, dict)]

    def game_context(self, team: str | None, season: str, week: int) -> GameContext:
        """Build the GameContext for `team` in the given week."""
        if not team:
            raise ProviderError("espn: player has no team")

        for event in self.scoreboard(season, week):
            competitions = event.get("competitions") or []
            if not competitions or not isinstance(competitions[0], dict):
                continue
            competition = competitions[0]
            competitors = [c for c in competition.get("competitors") or [] if isinstance(c, dict)]

            mine = next((c for c in competitors if team_matches(_abbrev(c), team)), None)
            if mine is None:
                continue
            theirs = next((c for c in competitors if c is not mine), None)

            return _build_context(competition, event, mine, theirs, team)

        # Not on the scoreboard for this week: almost always a bye.
        return GameContext(on_bye=True)


def _abbrev(competitor: dict[str, Any]) -> str | None:
    team = competitor.get("team")
    if isinstance(team, dict):
        value = team.get("abbreviation")
        return str(value).upper() if value else None
    return None


def _build_context(
    competition: dict[str, Any],
    event: dict[str, Any],
    mine: dict[str, Any],
    theirs: dict[str, Any] | None,
    team: str,
) -> GameContext:
    home_away = mine.get("homeAway") if mine.get("homeAway") in {"home", "away"} else None

    venue = competition.get("venue")
    indoor = venue.get("indoor") if isinstance(venue, dict) and isinstance(venue.get("indoor"), bool) else None

    team_spread: float | None = None
    over_under: float | None = None

    odds_entries = [o for o in competition.get("odds") or [] if isinstance(o, dict)]
    if odds_entries:
        odds = odds_entries[0]
        raw_ou = odds.get("overUnder")
        if isinstance(raw_ou, (int, float)):
            over_under = float(raw_ou)

        favorite, line = parse_spread_details(odds.get("details") if isinstance(odds.get("details"), str) else None)
        if line is not None:
            if line == 0:
                team_spread = 0.0
            elif favorite is not None:
                team_spread = -line if team_matches(favorite, team) else line

        if team_spread is None and isinstance(odds.get("spread"), (int, float)):
            # ESPN's bare `spread` field is stated relative to the home team.
            home_spread = float(odds["spread"])
            team_spread = home_spread if home_away == "home" else -home_spread

    team_total, opponent_total = implied_totals(team_spread, over_under)

    return GameContext(
        opponent=_abbrev(theirs) if theirs else None,
        home_away=home_away,  # type: ignore[arg-type]
        kickoff=event.get("date") if isinstance(event.get("date"), str) else None,
        spread=team_spread,
        over_under=over_under,
        implied_team_total=team_total,
        implied_opponent_total=opponent_total,
        venue_indoor=indoor,
        on_bye=False,
    )
