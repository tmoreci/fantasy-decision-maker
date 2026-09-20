"""Calibration maths: the part that tells you whether the percentages mean anything."""

from __future__ import annotations

import pytest

from fantasy_decision.calibrate import (
    BacktestRecord,
    brier_score,
    calibration_report,
    record_from_decision,
    reliability_curve,
    resolve_with_actuals,
)


def record(probability: float, correct: bool, candidates: int = 3) -> BacktestRecord:
    names = [f"p{i}" for i in range(candidates)]
    return BacktestRecord(
        season="2026", week=3, candidates=names, predicted="p0",
        predicted_probability=probability,
        actual_best="p0" if correct else "p1",
        actual_points={"p0": 20.0 if correct else 8.0, "p1": 8.0 if correct else 20.0},
    )


class TestBrierScore:
    def test_perfect_confident_prediction_scores_zero(self):
        assert brier_score([record(1.0, True)]) == 0.0

    def test_confidently_wrong_scores_one(self):
        assert brier_score([record(1.0, False)]) == 1.0

    def test_maximum_uncertainty_scores_a_quarter(self):
        assert brier_score([record(0.5, True), record(0.5, False)]) == 0.25

    def test_hedging_beats_being_confidently_wrong(self):
        assert brier_score([record(0.6, False)]) < brier_score([record(0.95, False)])

    def test_needs_resolved_records(self):
        unresolved = BacktestRecord(
            season="2026", week=3, candidates=["a", "b"], predicted="a", predicted_probability=0.6
        )
        with pytest.raises(ValueError, match="no resolved records"):
            brier_score([unresolved])


class TestReliabilityCurve:
    def test_buckets_by_claimed_probability(self):
        records = [record(0.25, False), record(0.35, False), record(0.85, True), record(0.95, True)]
        bins = reliability_curve(records, bin_count=5)
        # Width-0.2 bins: 0.25/0.35 share [0.2,0.4); 0.85/0.95 share [0.8,1.0].
        assert [b.count for b in bins] == [2, 2]
        assert sum(b.count for b in bins) == 4

    def test_a_calibrated_model_sits_on_the_diagonal(self):
        # Ten calls at 70%, seven of which came in: the gap should vanish.
        records = [record(0.7, i < 7) for i in range(10)]
        bins = [b for b in reliability_curve(records, bin_count=5) if b.count]
        assert len(bins) == 1
        assert bins[0].observed_accuracy == pytest.approx(0.7)
        assert bins[0].gap == pytest.approx(0.0)

    def test_positive_gap_means_overconfident(self):
        records = [record(0.9, i < 3) for i in range(10)]  # claimed 90%, delivered 30%
        bins = [b for b in reliability_curve(records, bin_count=5) if b.count]
        assert bins[0].gap > 0.5

    def test_certainty_lands_in_the_final_bin(self):
        bins = reliability_curve([record(1.0, True)], bin_count=5)
        assert len(bins) == 1 and bins[0].count == 1

    def test_empty_input_gives_no_bins(self):
        assert reliability_curve([], bin_count=5) == []


class TestCalibrationReport:
    def test_baseline_reflects_how_many_options_were_offered(self):
        # Two-way decisions have a 50% baseline, three-way a 33% one.
        two_way = calibration_report([record(0.6, True, candidates=2) for _ in range(4)])
        three_way = calibration_report([record(0.6, True, candidates=3) for _ in range(4)])
        assert two_way.baseline_accuracy == pytest.approx(0.5)
        assert three_way.baseline_accuracy == pytest.approx(1 / 3, abs=1e-4)  # report rounds to 4dp

    def test_positive_skill_when_it_beats_random(self):
        records = [record(0.8, True) for _ in range(9)] + [record(0.8, False)]
        assert calibration_report(records).skill_score > 0

    def test_negative_skill_when_it_loses_to_random(self):
        records = [record(0.9, False) for _ in range(9)] + [record(0.9, True)]
        assert calibration_report(records).skill_score < 0

    def test_counts_points_left_on_the_bench(self):
        report = calibration_report([record(0.7, False), record(0.7, True)])
        # One miss costing 12 points, one hit costing nothing.
        assert report.mean_points_left_on_bench == pytest.approx(6.0)

    def test_summary_is_human_readable(self):
        summary = calibration_report([record(0.8, True) for _ in range(5)]).summary()
        assert "decisions" in summary and "Brier" in summary

    def test_needs_something_to_score(self):
        with pytest.raises(ValueError, match="no resolved records"):
            calibration_report([])


class TestResolution:
    def test_attaches_the_winner_from_actual_points(self):
        unresolved = BacktestRecord(
            season="2026", week=3, candidates=["a", "b"], predicted="a", predicted_probability=0.6
        )
        resolved = resolve_with_actuals(unresolved, {"a": 12.4, "b": 19.1})
        assert resolved.actual_best == "b"
        assert resolved.correct is False
        assert resolved.points_left_on_bench == pytest.approx(6.7)

    def test_a_correct_pick_leaves_nothing_on_the_bench(self):
        unresolved = BacktestRecord(
            season="2026", week=3, candidates=["a", "b"], predicted="a", predicted_probability=0.6
        )
        resolved = resolve_with_actuals(unresolved, {"a": 22.0, "b": 9.0})
        assert resolved.correct is True
        assert resolved.points_left_on_bench == 0.0

    def test_no_actuals_leaves_the_record_unresolved(self):
        unresolved = BacktestRecord(
            season="2026", week=3, candidates=["a", "b"], predicted="a", predicted_probability=0.6
        )
        assert resolve_with_actuals(unresolved, {}).resolved is False


def test_record_from_decision_captures_the_prediction(dossiers, league, analysis_answers, decision_answers):
    from fantasy_decision.decide import decide
    from helpers import FakeJevClient

    decision = decide(
        dossiers, league, season="2026", week=3,
        client=FakeJevClient(analysis_answers, decision_answers),
    )
    entry = record_from_decision(decision)
    assert entry.predicted == "alpha_back"
    assert entry.predicted_probability == 0.74
    assert entry.confidence == 0.81
    assert entry.resolved is False  # not scoreable until the games are played
