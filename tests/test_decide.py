"""End-to-end orchestration against a fake Jev client."""

from __future__ import annotations

import pytest

from fantasy_decision.decide import DecisionError, analysis_for_state, decide
from fantasy_decision.questions import SEPARATOR

from helpers import FakeJevClient, build_response, choice_answer, noul_answer, score_answer


@pytest.fixture
def client(analysis_answers, decision_answers) -> FakeJevClient:
    return FakeJevClient(analysis_answers, decision_answers)


@pytest.fixture
def decision(dossiers, league, client):
    return decide(dossiers, league, season="2026", week=3, client=client)


class TestTwoPassFlow:
    def test_makes_exactly_two_requests(self, decision, client):
        assert len(client.calls) == 2

    def test_first_pass_batches_every_player_question(self, decision, client, dossiers):
        assert len(client.calls[0]["questions"]) == 6 * len(dossiers)

    def test_second_pass_sees_the_first_passs_findings(self, decision, client):
        enriched = client.calls[1]["state"]
        assert "analysis" in enriched
        assert set(enriched["analysis"]) == set(client.calls[0]["state"]["candidates"])

    def test_first_pass_state_has_no_analysis_yet(self, decision, client):
        assert "analysis" not in client.calls[0]["state"]

    def test_both_passes_share_the_same_candidate_state(self, decision, client):
        assert client.calls[0]["state"]["candidates"] == client.calls[1]["state"]["candidates"]


class TestDecisionOutput:
    def test_picks_the_highest_probability_option(self, decision):
        assert decision.start.pick == "alpha_back"
        assert decision.start.pick_probability == 0.74

    def test_probabilities_cover_every_candidate(self, decision, dossiers):
        assert set(decision.start.probabilities) == set(dossiers)
        assert round(sum(decision.start.probabilities.values()), 2) == 1.0

    def test_margin_is_the_gap_to_the_runner_up(self, decision):
        assert decision.start.margin == pytest.approx(0.48)

    def test_confidence_is_kept_separate_from_probability(self, decision):
        # The two mean different things and must not be conflated.
        assert decision.start.confidence == 0.81
        assert decision.start.pick_probability == 0.74

    def test_floor_and_ceiling_calls_are_captured(self, decision):
        assert decision.safest.pick == "alpha_back"
        assert decision.highest_upside.pick == "alpha_back"

    def test_coin_flip_signal_is_captured(self, decision):
        assert decision.coin_flip_probability == 0.18

    def test_usage_is_summed_across_both_passes(self, decision):
        assert decision.input_tokens == 200

    def test_latency_is_recorded(self, decision):
        assert decision.latency_ms is not None and decision.latency_ms >= 0

    def test_serialises_to_json(self, decision):
        payload = decision.as_json()
        assert payload["start"]["pick"] == "alpha_back"
        assert payload["week"] == 3


class TestAnalysisParsing:
    def test_judgments_are_grouped_per_player(self, decision, dossiers):
        assert set(decision.analyses) == set(dossiers)
        assert decision.analyses["alpha_back"].get("volume").value == 3.6

    def test_score_judgments_keep_their_rubric(self, decision):
        volume = decision.analyses["alpha_back"].get("volume")
        assert volume.kind == "score"
        assert volume.max_score == 4
        assert volume.nearest_level == "level 4"

    def test_noul_judgments_have_no_separate_confidence(self, decision):
        # Noul answers express uncertainty in the probability itself.
        injury = decision.analyses["alpha_back"].get("injury_risk")
        assert injury.kind == "noul"
        assert injury.confidence is None

    def test_unexpected_answer_keys_are_ignored(self, dossiers, league, analysis_answers, decision_answers):
        analysis_answers["totally::unknown"] = noul_answer(0.5)
        analysis_answers["malformed_key"] = noul_answer(0.5)
        result = decide(
            dossiers, league, season="2026", week=3,
            client=FakeJevClient(analysis_answers, decision_answers),
        )
        assert set(result.analyses) == set(dossiers)

    def test_state_rendering_includes_both_number_and_rubric_text(self, decision):
        payload = analysis_for_state(decision.analyses)
        volume = payload["alpha_back"]["volume"]
        assert volume["score"] == 3.6
        assert volume["reads_as"] == "level 4"
        assert volume["out_of"] == 4


class TestGuards:
    def test_rejects_a_single_option(self, dossiers, league, client):
        only_one = {k: v for k, v in list(dossiers.items())[:1]}
        with pytest.raises(DecisionError, match="between 2 and 3"):
            decide(only_one, league, season="2026", week=3, client=client)

    def test_rejects_more_than_three_options(self, dossiers, league, client):
        too_many = dict(dossiers)
        for index in range(3):
            clone = list(dossiers.values())[0].model_copy(deep=True)
            clone.ref = clone.ref.model_copy(update={"name": f"Extra {index}"})
            too_many[clone.slug] = clone
        with pytest.raises(DecisionError, match="between 2 and 3"):
            decide(too_many, league, season="2026", week=3, client=client)

    def test_missing_start_answer_is_an_error_not_a_guess(self, dossiers, league, analysis_answers):
        class NoStart(FakeJevClient):
            def system_one(self, state, questions, *, model=None, **_):
                self.calls.append({"state": state, "questions": questions, "model": model})
                if len(self.calls) == 1:
                    return build_response(analysis_answers)
                return build_response({"coin_flip": noul_answer(0.5)})

        with pytest.raises(DecisionError, match="did not return"):
            decide(dossiers, league, season="2026", week=3, client=NoStart({}, {}))

    def test_three_way_decision_works(self, dossiers, league, analysis_answers, decision_answers):
        charlie = list(dossiers.values())[0].model_copy(deep=True)
        charlie.ref = charlie.ref.model_copy(update={"name": "Charlie Back", "sleeper_id": "3"})
        dossiers[charlie.slug] = charlie
        for metric, answer in (
            ("volume", score_answer(2.0, 0.6)), ("matchup", score_answer(2.0, 0.6)),
            ("ceiling", score_answer(2.0, 0.6)), ("injury_risk", noul_answer(0.2)),
            ("game_script", noul_answer(0.5)), ("data_sufficiency", noul_answer(0.8)),
        ):
            analysis_answers[f"{charlie.slug}{SEPARATOR}{metric}"] = answer
        decision_answers["start"] = choice_answer(
            {"alpha_back": 0.52, "bravo_back": 0.21, charlie.slug: 0.27}, 0.64
        )
        result = decide(
            dossiers, league, season="2026", week=3,
            client=FakeJevClient(analysis_answers, decision_answers),
        )
        assert len(result.start.probabilities) == 3
        assert result.start.pick == "alpha_back"
