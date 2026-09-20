"""Shared fixtures.

The TypeSafe and Sleeper/ESPN endpoints are never called in tests. `FakeJevClient`
mimics the real SDK closely in one way that matters: it builds responses through
`model_validate_json`, because the SDK's response models are strict and only coerce
string JSON keys to integer score levels on that path.
"""

from __future__ import annotations

from typing import Any

import pytest
from helpers import choice_answer, noul_answer, score_answer

from fantasy_decision.models import (
    GameContext,
    InjuryInfo,
    LeagueSettings,
    PlayerDossier,
    PlayerRef,
    ScoringSettings,
    TrendingSignal,
    UsageStats,
)
from fantasy_decision.questions import SEPARATOR


@pytest.fixture
def league() -> LeagueSettings:
    return LeagueSettings(name="Test League", scoring=ScoringSettings(name="PPR", reception=1.0), source="manual")


@pytest.fixture
def dossiers() -> dict[str, PlayerDossier]:
    alpha = PlayerDossier(
        ref=PlayerRef(name="Alpha Back", position="RB", team="ATL", sleeper_id="1", gsis_id="00-0001"),
        injury=InjuryInfo(status=None, depth_chart_order=1, depth_chart_position="RB"),
        game=GameContext(
            opponent="CAR", home_away="home", spread=-6.5, over_under=47.5,
            implied_team_total=27.0, implied_opponent_total=20.5, venue_indoor=True,
        ),
        usage=UsageStats(
            games_sampled=5, snap_pct=0.78, target_share=0.14, wopr=0.55,
            carries_per_game=17.2, ppr_points_per_game=17.8, recent_snap_pct=0.84,
            recent_target_share=0.17, recent_ppr_points_per_game=21.0,
        ),
        trending=TrendingSignal(adds_24h=1200, add_rank=4),
        missing=[],
    )
    bravo = PlayerDossier(
        ref=PlayerRef(name="Bravo Back", position="RB", team="NYJ", sleeper_id="2", gsis_id="00-0002"),
        injury=InjuryInfo(status="Questionable", body_part="Ankle", depth_chart_order=1),
        game=GameContext(
            opponent="BUF", home_away="away", spread=7.0, over_under=41.0,
            implied_team_total=17.0, implied_opponent_total=24.0, venue_indoor=False,
        ),
        usage=UsageStats(
            games_sampled=5, snap_pct=0.61, target_share=0.11, wopr=0.42,
            carries_per_game=12.0, ppr_points_per_game=11.4, recent_snap_pct=0.55,
            recent_target_share=0.09, recent_ppr_points_per_game=8.9,
        ),
        missing=["advanced usage metrics for Bravo Back"],
    )
    return {alpha.slug: alpha, bravo.slug: bravo}


@pytest.fixture
def analysis_answers(dossiers: dict[str, PlayerDossier]) -> dict[str, Any]:
    alpha, bravo = list(dossiers)
    return {
        f"{alpha}{SEPARATOR}volume": score_answer(3.6, 0.88),
        f"{alpha}{SEPARATOR}matchup": score_answer(3.1, 0.74),
        f"{alpha}{SEPARATOR}ceiling": score_answer(3.0, 0.7),
        f"{alpha}{SEPARATOR}injury_risk": noul_answer(0.06),
        f"{alpha}{SEPARATOR}game_script": noul_answer(0.82),
        f"{alpha}{SEPARATOR}data_sufficiency": noul_answer(0.91),
        f"{bravo}{SEPARATOR}volume": score_answer(2.1, 0.66),
        f"{bravo}{SEPARATOR}matchup": score_answer(1.2, 0.8),
        f"{bravo}{SEPARATOR}ceiling": score_answer(2.4, 0.6),
        f"{bravo}{SEPARATOR}injury_risk": noul_answer(0.48),
        f"{bravo}{SEPARATOR}game_script": noul_answer(0.27),
        f"{bravo}{SEPARATOR}data_sufficiency": noul_answer(0.7),
    }


@pytest.fixture
def decision_answers(dossiers: dict[str, PlayerDossier]) -> dict[str, Any]:
    alpha, bravo = list(dossiers)
    return {
        "start": choice_answer({alpha: 0.74, bravo: 0.26}, 0.81),
        "safest": choice_answer({alpha: 0.69, bravo: 0.31}, 0.72),
        "highest_upside": choice_answer({alpha: 0.58, bravo: 0.42}, 0.55),
        "coin_flip": noul_answer(0.18),
    }
