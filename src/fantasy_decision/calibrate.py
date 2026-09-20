"""Backtesting and calibration.

Jev's probabilities are trained to be calibrated, which means they can be checked.
Replay historical weeks, compare the predicted probability against who actually
scored more, and you get a Brier score and a reliability curve.

This matters practically: the docs are explicit that confidence thresholds must be
validated against your own domain data rather than guessed. The bands in
`explain.py` should be set from the output of this module, not from intuition.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("fantasy_decision.calibrate")


class BacktestRecord(BaseModel):
    """One historical start/sit call, scored against what actually happened."""

    season: str
    week: int
    candidates: list[str]
    predicted: str
    predicted_probability: float
    probabilities: dict[str, float] = Field(default_factory=dict)
    confidence: float | None = None
    actual_best: str | None = None
    actual_points: dict[str, float] = Field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.actual_best is not None

    @property
    def correct(self) -> bool | None:
        if self.actual_best is None:
            return None
        return self.predicted == self.actual_best

    @property
    def points_left_on_bench(self) -> float | None:
        """How much the pick cost versus starting the best option."""
        if self.actual_best is None or not self.actual_points:
            return None
        return round(self.actual_points.get(self.actual_best, 0.0) - self.actual_points.get(self.predicted, 0.0), 2)


class Bin(BaseModel):
    lower: float
    upper: float
    count: int
    mean_predicted: float
    observed_accuracy: float

    @property
    def gap(self) -> float:
        """Positive means overconfident: the model claimed more than it delivered."""
        return round(self.mean_predicted - self.observed_accuracy, 4)


class CalibrationReport(BaseModel):
    sample_size: int
    accuracy: float
    baseline_accuracy: float
    brier: float
    baseline_brier: float
    skill_score: float
    mean_points_left_on_bench: float | None = None
    bins: list[Bin] = Field(default_factory=list)

    def summary(self) -> str:
        verdict = "well calibrated" if abs(self.brier - self.baseline_brier) < 0.01 and self.skill_score > 0 else (
            "better than guessing" if self.skill_score > 0 else "no better than guessing"
        )
        return (
            f"{self.sample_size} decisions | accuracy {self.accuracy:.1%} "
            f"(baseline {self.baseline_accuracy:.1%}) | Brier {self.brier:.4f} "
            f"(baseline {self.baseline_brier:.4f}) | skill {self.skill_score:+.3f} - {verdict}"
        )


def brier_score(records: list[BacktestRecord]) -> float:
    """Mean squared error of P(pick is correct) against the binary outcome.

    Lower is better. 0.0 is perfect; 0.25 is what you get by always saying 50%.
    """
    resolved = [r for r in records if r.resolved]
    if not resolved:
        raise ValueError("no resolved records to score")
    total = sum((r.predicted_probability - (1.0 if r.correct else 0.0)) ** 2 for r in resolved)
    return round(total / len(resolved), 6)


def reliability_curve(records: list[BacktestRecord], bin_count: int = 5) -> list[Bin]:
    """Bucket predictions by confidence and compare claimed against observed.

    A calibrated model's bins sit on the diagonal: the group it called 70% should
    come in around 70% correct.
    """
    resolved = [r for r in records if r.resolved]
    if not resolved or bin_count < 1:
        return []

    width = 1.0 / bin_count
    bins: list[Bin] = []
    for index in range(bin_count):
        lower = index * width
        upper = (index + 1) * width
        # Include the right edge in the final bin so p == 1.0 is not dropped.
        members = [
            r for r in resolved
            if lower <= r.predicted_probability < upper or (index == bin_count - 1 and r.predicted_probability == upper)
        ]
        if not members:
            continue
        bins.append(
            Bin(
                lower=round(lower, 3),
                upper=round(upper, 3),
                count=len(members),
                mean_predicted=round(sum(r.predicted_probability for r in members) / len(members), 4),
                observed_accuracy=round(sum(1 for r in members if r.correct) / len(members), 4),
            )
        )
    return bins


def calibration_report(records: list[BacktestRecord], bin_count: int = 5) -> CalibrationReport:
    resolved = [r for r in records if r.resolved]
    if not resolved:
        raise ValueError("no resolved records to report on")

    accuracy = sum(1 for r in resolved if r.correct) / len(resolved)

    # The honest baseline is picking at random from the options actually offered,
    # which is not always 1/3 - some decisions are between only two players.
    baseline_probabilities = [1.0 / max(len(r.candidates), 2) for r in resolved]
    baseline_accuracy = sum(baseline_probabilities) / len(resolved)
    baseline_brier = round(
        sum((p - (1.0 if r.correct else 0.0)) ** 2 for p, r in zip(baseline_probabilities, resolved, strict=True))
        / len(resolved),
        6,
    )

    brier = brier_score(resolved)
    skill = 0.0 if baseline_brier == 0 else round(1.0 - (brier / baseline_brier), 4)

    bench = [r.points_left_on_bench for r in resolved if r.points_left_on_bench is not None]

    return CalibrationReport(
        sample_size=len(resolved),
        accuracy=round(accuracy, 4),
        baseline_accuracy=round(baseline_accuracy, 4),
        brier=brier,
        baseline_brier=baseline_brier,
        skill_score=skill,
        mean_points_left_on_bench=round(sum(bench) / len(bench), 2) if bench else None,
        bins=reliability_curve(resolved, bin_count),
    )


def resolve_with_actuals(record: BacktestRecord, points_by_slug: dict[str, float]) -> BacktestRecord:
    """Attach real fantasy output to a prediction, producing a scored record."""
    if not points_by_slug:
        return record
    best = max(points_by_slug, key=lambda slug: points_by_slug[slug])
    return record.model_copy(
        update={
            "actual_best": best,
            "actual_points": {slug: round(points, 2) for slug, points in points_by_slug.items()},
        }
    )


def actual_points(season: int, week: int, gsis_ids: dict[str, str], ppr: float = 1.0) -> dict[str, float]:
    """Look up what each player actually scored, keyed by slug.

    Uses nflverse weekly data. `gsis_ids` maps slug -> gsis player id.
    """
    from fantasy_decision.providers.nflverse import NflverseProvider  # noqa: PLC0415

    weekly = NflverseProvider._weekly(season)  # noqa: SLF001 - intentional reuse of the memoised frame
    rows = weekly[weekly["week"] == week]
    column = "fantasy_points_ppr" if ppr >= 1.0 else "fantasy_points"

    points: dict[str, float] = {}
    for slug, gsis in gsis_ids.items():
        matched = rows[rows["player_id"] == gsis]
        if len(matched) == 0:
            continue
        value = matched.iloc[0].get(column)
        try:
            scored = float(value)
        except (TypeError, ValueError):
            continue
        if scored != scored:  # NaN
            continue
        if 0.0 < ppr < 1.0:
            # Half-PPR sits between the two columns nflverse provides.
            receptions = float(matched.iloc[0].get("receptions") or 0.0)
            scored = float(matched.iloc[0].get("fantasy_points") or 0.0) + ppr * receptions
        points[slug] = round(scored, 2)
    return points


def record_from_decision(decision: Any) -> BacktestRecord:
    """Build an unresolved record from a live Decision, ready to be scored later."""
    return BacktestRecord(
        season=decision.season,
        week=decision.week,
        candidates=list(decision.dossiers),
        predicted=decision.start.pick,
        predicted_probability=decision.start.pick_probability,
        probabilities=dict(decision.start.probabilities),
        confidence=decision.start.confidence,
    )
