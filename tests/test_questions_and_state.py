"""Question construction and the state payload."""

from __future__ import annotations

from typesafe_sdk import Choice, Noul, Score

from fantasy_decision.dossier import build_state, dossier_payload
from fantasy_decision.questions import (
    METRIC_ORDER,
    SCORE_RUBRICS,
    SEPARATOR,
    build_analysis_questions,
    build_decision_questions,
)


class TestAnalysisQuestions:
    def test_one_batch_covers_every_player(self, dossiers):
        candidates = {slug: d.ref.label() for slug, d in dossiers.items()}
        questions = build_analysis_questions(candidates)
        # Six judgments per player, all in a single request.
        assert len(questions) == 6 * len(candidates)
        for slug in candidates:
            for metric in METRIC_ORDER:
                assert f"{slug}{SEPARATOR}{metric}" in questions

    def test_question_names_split_back_cleanly(self, dossiers):
        candidates = {slug: d.ref.label() for slug, d in dossiers.items()}
        for key in build_analysis_questions(candidates):
            slug, sep, metric = key.partition(SEPARATOR)
            assert sep == SEPARATOR
            assert slug in candidates
            assert metric in METRIC_ORDER

    def test_primitives_match_the_metric(self, dossiers):
        questions = build_analysis_questions({s: d.ref.label() for s, d in dossiers.items()})
        slug = next(iter(dossiers))
        assert isinstance(questions[f"{slug}{SEPARATOR}matchup"], Score)
        assert isinstance(questions[f"{slug}{SEPARATOR}injury_risk"], Noul)

    def test_rubrics_are_ordered_and_nonempty(self):
        for name, levels in SCORE_RUBRICS.items():
            assert len(levels) >= 2, name
            assert all(isinstance(level, str) and level for level in levels), name

    def test_instructions_point_at_the_players_state_field(self, dossiers):
        questions = build_analysis_questions({s: d.ref.label() for s, d in dossiers.items()})
        slug = next(iter(dossiers))
        assert f"candidates.{slug}" in questions[f"{slug}{SEPARATOR}volume"].instructions


class TestDecisionQuestions:
    def test_choice_options_are_exactly_the_candidates(self, dossiers):
        candidates = {slug: d.ref.label() for slug, d in dossiers.items()}
        questions = build_decision_questions(candidates)
        assert set(questions["start"].criteria) == set(candidates)

    def test_offers_floor_and_ceiling_alongside_the_main_call(self, dossiers):
        questions = build_decision_questions({s: d.ref.label() for s, d in dossiers.items()})
        assert isinstance(questions["start"], Choice)
        assert isinstance(questions["safest"], Choice)
        assert isinstance(questions["highest_upside"], Choice)
        assert isinstance(questions["coin_flip"], Noul)

    def test_need_changes_the_emphasis(self, dossiers):
        candidates = {s: d.ref.label() for s, d in dossiers.items()}
        safe = build_decision_questions(candidates, need="safe")["start"].instructions
        upside = build_decision_questions(candidates, need="upside")["start"].instructions
        auto = build_decision_questions(candidates, need="auto")["start"].instructions
        assert "floor" in safe
        assert "ceiling" in upside
        assert safe != upside != auto


class TestState:
    def test_candidates_are_keyed_by_slug(self, dossiers, league):
        state = build_state(dossiers, league, season="2026", week=3)
        assert set(state["candidates"]) == set(dossiers)

    def test_missing_signals_are_named_rather_than_dropped(self, dossiers, league):
        state = build_state(dossiers, league, season="2026", week=3)
        bravo = state["candidates"]["bravo_back"]
        assert "advanced usage metrics for Bravo Back" in bravo["unavailable_data"]

    def test_a_complete_dossier_says_so_explicitly(self, dossiers):
        payload = dossier_payload(dossiers["alpha_back"])
        assert payload["unavailable_data"] == ["none - all sources reported"]

    def test_nulls_are_pruned_to_save_tokens(self, dossiers, league):
        state = build_state(dossiers, league, season="2026", week=3)
        alpha = state["candidates"]["alpha_back"]
        assert "status" not in alpha["injury"]  # was None
        assert alpha["injury"]["depth_chart_order"] == 1

    def test_derived_metrics_reach_the_model(self, dossiers, league):
        state = build_state(dossiers, league, season="2026", week=3)
        alpha = state["candidates"]["alpha_back"]
        assert alpha["game"]["implied_team_total"] == 27.0
        assert alpha["usage"]["snap_trend"] > 0  # role is growing

    def test_field_guide_explains_the_jargon(self, dossiers, league):
        state = build_state(dossiers, league, season="2026", week=3)
        assert "implied_team_total" in state["field_guide"]
        assert "unavailable_data" in state["field_guide"]

    def test_need_is_carried_into_state(self, dossiers, league):
        assert "underdog" in build_state(dossiers, league, season="2026", week=3, need="upside")["manager_situation"]
