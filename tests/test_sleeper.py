"""Player resolution and league scoring translation."""

from __future__ import annotations

import pytest

from fantasy_decision.providers.base import ProviderError
from fantasy_decision.providers.sleeper import SleeperProvider, scoring_from_sleeper

INDEX = {
    "1": {"player_id": "1", "full_name": "Bijan Robinson", "position": "RB", "team": "ATL",
          "status": "Active", "injury_status": None, "depth_chart_order": 1, "gsis_id": "00-0001"},
    "2": {"player_id": "2", "full_name": "Breece Hall", "position": "RB", "team": "NYJ",
          "status": "Active", "injury_status": "Questionable", "injury_body_part": "Ankle",
          "depth_chart_order": 1},
    "3": {"player_id": "3", "full_name": "Michael Carter", "position": "RB", "team": "ARI", "status": "Active"},
    "4": {"player_id": "4", "full_name": "Michael Carter II", "position": "DB", "team": "NYJ", "status": "Active"},
    "5": {"player_id": "5", "full_name": "Michael Pittman", "position": "WR", "team": "IND", "status": "Inactive"},
    "6": {"player_id": "6", "full_name": "Michael Pittman", "position": "WR", "team": "FA", "status": "Active"},
}


@pytest.fixture
def sleeper(monkeypatch):
    provider = SleeperProvider()
    monkeypatch.setattr(provider, "player_index", lambda: INDEX)
    return provider


class TestResolvePlayer:
    def test_exact_name(self, sleeper):
        ref = sleeper.resolve_player("Bijan Robinson")
        assert ref.sleeper_id == "1"
        assert ref.position == "RB"
        assert ref.slug == "bijan_robinson"

    def test_is_case_and_punctuation_insensitive(self, sleeper):
        assert sleeper.resolve_player("bijan  robinson").sleeper_id == "1"
        assert sleeper.resolve_player("BIJAN ROBINSON").sleeper_id == "1"

    def test_partial_name(self, sleeper):
        assert sleeper.resolve_player("Breece").sleeper_id == "2"

    def test_raw_sleeper_id_passes_through(self, sleeper):
        assert sleeper.resolve_player("2").sleeper_id == "2"

    def test_skips_non_fantasy_positions(self, sleeper):
        # "Michael Carter" would be ambiguous if the DB were a candidate; it is not.
        assert sleeper.resolve_player("Michael Carter").sleeper_id == "3"

    def test_ambiguous_name_raises_rather_than_guessing(self, sleeper):
        # Two active-eligible WRs share a name only when status does not disambiguate.
        with pytest.raises(ProviderError, match="ambiguous"):
            sleeper.resolve_player("Michael")

    def test_active_status_breaks_a_tie(self, sleeper):
        assert sleeper.resolve_player("Michael Pittman").sleeper_id == "6"

    def test_unknown_player(self, sleeper):
        with pytest.raises(ProviderError, match="no player matching"):
            sleeper.resolve_player("Nobody At All")


class TestInjury:
    def test_reads_designation_and_depth_chart(self, sleeper):
        ref = sleeper.resolve_player("Breece Hall")
        injury = sleeper.injury_for(ref)
        assert injury.status == "Questionable"
        assert injury.body_part == "Ankle"
        assert injury.is_starter_on_depth_chart is True

    def test_healthy_player_has_no_status(self, sleeper):
        injury = sleeper.injury_for(sleeper.resolve_player("Bijan Robinson"))
        assert injury.status is None

    def test_unknown_depth_chart_is_none_not_false(self, sleeper):
        injury = sleeper.injury_for(sleeper.resolve_player("Michael Carter"))
        assert injury.is_starter_on_depth_chart is None


class TestScoringTranslation:
    def test_full_ppr(self):
        scoring = scoring_from_sleeper({"rec": 1.0, "pass_td": 4.0})
        assert scoring.name == "PPR"
        assert scoring.reception == 1.0

    def test_half_ppr(self):
        assert scoring_from_sleeper({"rec": 0.5}).name == "Half-PPR"

    def test_standard(self):
        assert scoring_from_sleeper({"rec": 0.0}).name == "Standard"

    def test_te_premium_is_named(self):
        assert "TE premium" in scoring_from_sleeper({"rec": 1.0, "bonus_rec_te": 0.5}).name

    def test_notable_bonuses_become_notes(self):
        scoring = scoring_from_sleeper({"rec": 1.0, "pass_td": 6.0, "bonus_rec_yd_100": 3.0})
        assert "6-point passing touchdowns" in scoring.bonus_notes
        assert "100-yard receiving bonus" in scoring.bonus_notes

    def test_empty_settings_fall_back_to_ppr_defaults(self):
        scoring = scoring_from_sleeper({})
        assert scoring.reception == 1.0
        assert scoring.pass_td == 4.0
