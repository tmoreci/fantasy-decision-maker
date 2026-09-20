"""The explanation layer: Jev cannot write prose, so this code must."""

from __future__ import annotations

import pytest

from fantasy_decision.decide import decide
from fantasy_decision.explain import explain, factors_for, verdict_for, warnings_for
from helpers import FakeJevClient, choice_answer, noul_answer


def make(dossiers, league, analysis_answers, decision_answers):
    return decide(
        dossiers, league, season="2026", week=3,
        client=FakeJevClient(analysis_answers, decision_answers),
    )


@pytest.fixture
def decision(dossiers, league, analysis_answers, decision_answers):
    return make(dossiers, league, analysis_answers, decision_answers)


class TestVerdictBands:
    def test_a_dominant_pick_reads_as_clear(self, decision):
        verdict = verdict_for(decision)
        assert verdict.strength == "clear"
        assert verdict.headline == "Start Alpha Back"

    def test_a_near_even_split_reads_as_a_toss_up(self, dossiers, league, analysis_answers, decision_answers):
        decision_answers["start"] = choice_answer({"alpha_back": 0.53, "bravo_back": 0.47}, 0.55)
        decision_answers["coin_flip"] = noul_answer(0.8)
        verdict = verdict_for(make(dossiers, league, analysis_answers, decision_answers))
        assert verdict.strength == "coin-flip"
        assert "toss-up" in verdict.headline

    def test_low_selection_confidence_downgrades_a_strong_probability(
        self, dossiers, league, analysis_answers, decision_answers
    ):
        # High probability but the model is unsure of its own pick: not a "clear" call.
        decision_answers["start"] = choice_answer({"alpha_back": 0.78, "bravo_back": 0.22}, 0.30)
        verdict = verdict_for(make(dossiers, league, analysis_answers, decision_answers))
        assert verdict.strength == "coin-flip"

    def test_verdict_is_judged_against_the_number_of_options(
        self, dossiers, league, analysis_answers, decision_answers
    ):
        # 40% is weak with two options but a real edge with three.
        decision_answers["start"] = choice_answer({"alpha_back": 0.40, "bravo_back": 0.60}, 0.7)
        two_way = verdict_for(make(dossiers, league, analysis_answers, decision_answers))
        assert "33%" not in two_way.detail
        assert "50%" in two_way.detail


class TestFactors:
    def test_one_row_per_metric_with_both_players(self, decision):
        factors = {f.metric: f for f in factors_for(decision)}
        assert "volume" in factors and "matchup" in factors
        assert set(factors["volume"].values) == {"alpha_back", "bravo_back"}

    def test_higher_is_better_for_workload(self, decision):
        factors = {f.metric: f for f in factors_for(decision)}
        assert factors["volume"].edge == "alpha_back"  # 3.6 vs 2.1

    def test_lower_is_better_for_injury_risk(self, decision):
        factors = {f.metric: f for f in factors_for(decision)}
        # Alpha at 6% risk beats Bravo at 48%, so the edge goes to the *lower* value.
        assert factors["injury_risk"].edge == "alpha_back"

    def test_upside_is_descriptive_and_claims_no_winner(self, decision):
        factors = {f.metric: f for f in factors_for(decision)}
        assert factors["ceiling"].edge is None

    def test_score_cells_show_the_rubric_text_not_just_a_number(self, decision):
        factors = {f.metric: f for f in factors_for(decision)}
        assert "level" in factors["volume"].values["alpha_back"]
        assert "/4" in factors["volume"].values["alpha_back"]

    def test_noul_cells_render_as_percentages(self, decision):
        factors = {f.metric: f for f in factors_for(decision)}
        assert factors["game_script"].values["alpha_back"] == "82%"


class TestDifferentiators:
    def test_names_the_metrics_that_actually_separated_them(self, decision):
        lines = explain(decision).differentiators
        assert lines
        assert any("Game script" in line or "Expected workload" in line for line in lines)

    def test_flags_a_metric_that_favours_the_loser(self, dossiers, league, analysis_answers, decision_answers):
        # Give Bravo a far better matchup; the pick should own up to it.
        analysis_answers["bravo_back::matchup"] = {
            "type": "score", "score": 4.0, "confidence": 0.9,
            "legend": {str(i): f"level {i}" for i in range(5)},
            "probabilities": {str(i): (1.0 if i == 4 else 0.0) for i in range(5)},
        }
        analysis_answers["alpha_back::matchup"] = {
            "type": "score", "score": 0.2, "confidence": 0.9,
            "legend": {str(i): f"level {i}" for i in range(5)},
            "probabilities": {str(i): (1.0 if i == 0 else 0.0) for i in range(5)},
        }
        lines = explain(make(dossiers, league, analysis_answers, decision_answers)).differentiators
        assert any("actually favours Bravo Back" in line for line in lines)


class TestWarnings:
    def test_surfaces_an_injury_designation(self, decision):
        assert any("Questionable" in w and "Bravo Back" in w for w in warnings_for(decision))

    def test_surfaces_missing_data(self, decision):
        assert any("Could not fetch" in w for w in warnings_for(decision))

    def test_flags_a_bye_week(self, dossiers, league, analysis_answers, decision_answers):
        dossiers["bravo_back"].game = dossiers["bravo_back"].game.model_copy(update={"on_bye": True})
        warnings = warnings_for(make(dossiers, league, analysis_answers, decision_answers))
        assert any("on a bye" in w for w in warnings)

    def test_flags_a_ruled_out_player(self, dossiers, league, analysis_answers, decision_answers):
        dossiers["bravo_back"].injury = dossiers["bravo_back"].injury.model_copy(update={"status": "Out"})
        warnings = warnings_for(make(dossiers, league, analysis_answers, decision_answers))
        assert any("unlikely to play at all" in w for w in warnings)

    def test_flags_thin_data(self, dossiers, league, analysis_answers, decision_answers):
        analysis_answers["bravo_back::data_sufficiency"] = noul_answer(0.2)
        warnings = warnings_for(make(dossiers, league, analysis_answers, decision_answers))
        assert any("Thin data" in w for w in warnings)

    def test_flags_when_floor_and_ceiling_disagree(self, dossiers, league, analysis_answers, decision_answers):
        decision_answers["highest_upside"] = choice_answer({"alpha_back": 0.3, "bravo_back": 0.7}, 0.6)
        warnings = warnings_for(make(dossiers, league, analysis_answers, decision_answers))
        assert any("Floor and ceiling disagree" in w for w in warnings)

    def test_quiet_when_nothing_is_wrong(self, dossiers, league, analysis_answers, decision_answers):
        dossiers["bravo_back"].injury = dossiers["bravo_back"].injury.model_copy(update={"status": None})
        dossiers["bravo_back"].missing = []
        warnings = warnings_for(make(dossiers, league, analysis_answers, decision_answers))
        assert warnings == []


def test_explain_assembles_everything(decision):
    explanation = explain(decision)
    assert explanation.verdict.headline
    assert explanation.factors
    assert explanation.differentiators
