"""The Vegas-line math is the highest-leverage deterministic code in the app."""

from __future__ import annotations

import pytest

from fantasy_decision.providers.espn import (
    EspnProvider,
    implied_totals,
    parse_spread_details,
    team_matches,
)


class TestImpliedTotals:
    def test_splits_total_using_the_spread(self):
        # A 47.5 total with the team favoured by 3.5: 25.5 vs 22.0, a 3.5 margin.
        team, opponent = implied_totals(-3.5, 47.5)
        assert team == 25.5
        assert opponent == 22.0
        assert round(team - opponent, 2) == 3.5

    def test_underdog_gets_the_smaller_share(self):
        team, opponent = implied_totals(7.0, 41.0)
        assert team == 17.0
        assert opponent == 24.0

    def test_pick_em_splits_evenly(self):
        assert implied_totals(0.0, 44.0) == (22.0, 22.0)

    def test_totals_always_sum_back_to_the_over_under(self):
        for spread in (-10.5, -3.0, 0.0, 2.5, 9.0):
            for total in (37.0, 44.5, 52.0):
                team, opponent = implied_totals(spread, total)
                assert round(team + opponent, 2) == total

    @pytest.mark.parametrize("spread,total", [(None, 44.0), (-3.0, None), (None, None)])
    def test_missing_inputs_give_nothing_rather_than_guessing(self, spread, total):
        assert implied_totals(spread, total) == (None, None)


class TestParseSpreadDetails:
    def test_parses_favourite_and_line(self):
        assert parse_spread_details("KC -3.5") == ("KC", 3.5)

    def test_handles_pick_em(self):
        assert parse_spread_details("EVEN") == (None, 0.0)
        assert parse_spread_details("PK") == (None, 0.0)

    def test_unparseable_returns_nothing(self):
        assert parse_spread_details("see app for odds") == (None, None)
        assert parse_spread_details(None) == (None, None)


class TestTeamMatches:
    def test_exact_and_alias(self):
        assert team_matches("KC", "kc")
        assert team_matches("WAS", "WSH")  # Sleeper vs ESPN
        assert team_matches("OAK", "LV")   # relocated

    def test_rejects_different_teams_and_missing_values(self):
        assert not team_matches("KC", "SF")
        assert not team_matches(None, "KC")


SCOREBOARD = {
    "events": [
        {
            "id": "1",
            "date": "2026-09-20T17:00Z",
            "competitions": [
                {
                    "venue": {"fullName": "Mercedes-Benz Stadium", "indoor": True},
                    "competitors": [
                        {"homeAway": "home", "team": {"abbreviation": "ATL"}},
                        {"homeAway": "away", "team": {"abbreviation": "CAR"}},
                    ],
                    "odds": [{"details": "ATL -6.5", "overUnder": 47.5, "spread": -6.5}],
                }
            ],
        },
        {
            "id": "2",
            "date": "2026-09-20T20:00Z",
            "competitions": [
                {
                    "venue": {"indoor": False},
                    "competitors": [
                        {"homeAway": "home", "team": {"abbreviation": "BUF"}},
                        {"homeAway": "away", "team": {"abbreviation": "NYJ"}},
                    ],
                    # No `details`, so the home-relative `spread` field is the fallback.
                    "odds": [{"overUnder": 41.0, "spread": -7.0}],
                }
            ],
        },
    ]
}


class TestGameContext:
    @pytest.fixture
    def provider(self, monkeypatch):
        provider = EspnProvider()
        monkeypatch.setattr(provider, "scoreboard", lambda *a, **k: SCOREBOARD["events"])
        return provider

    def test_favourite_at_home(self, provider):
        game = provider.game_context("ATL", "2026", 3)
        assert game.opponent == "CAR"
        assert game.home_away == "home"
        assert game.spread == -6.5
        assert game.implied_team_total == 27.0
        assert game.is_favorite is True
        assert game.projected_margin == 6.5
        assert game.venue_indoor is True
        assert game.on_bye is False

    def test_underdog_on_the_road_via_spread_fallback(self, provider):
        game = provider.game_context("NYJ", "2026", 3)
        assert game.opponent == "BUF"
        assert game.home_away == "away"
        # Home spread of -7.0 means the away team is +7.0.
        assert game.spread == 7.0
        assert game.implied_team_total == 17.0
        assert game.is_favorite is False

    def test_team_not_on_the_scoreboard_is_a_bye(self, provider):
        game = provider.game_context("DET", "2026", 3)
        assert game.on_bye is True
        assert game.implied_team_total is None
