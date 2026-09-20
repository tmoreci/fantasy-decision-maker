"""Turn Jev's numbers into something a human can act on.

Jev never emits text, so every word of explanation is assembled here from the
pass-one rubrics. That constraint is the reason the decision is decomposed at all:
the sub-judgments *are* the explanation.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from fantasy_decision.models import Decision, SubJudgment
from fantasy_decision.questions import METRIC_LABELS, METRIC_ORDER

Polarity = Literal["higher_better", "lower_better", "descriptive"]

POLARITY: dict[str, Polarity] = {
    "volume": "higher_better",
    "matchup": "higher_better",
    "game_script": "higher_better",
    "injury_risk": "lower_better",
    "ceiling": "descriptive",
    "data_sufficiency": "higher_better",
}

# With three options a coin flip sits at 33%, with two it sits at 50%. A verdict is
# only meaningful relative to that baseline, so the bands below are computed against
# it rather than against fixed percentages.
CLEAR_EDGE = 0.20
SLIGHT_EDGE = 0.08


class Factor(BaseModel):
    """One metric, compared across the candidates."""

    metric: str
    label: str
    values: dict[str, str]
    raw: dict[str, float]
    edge: str | None = None
    swing: float = 0.0


class Verdict(BaseModel):
    headline: str
    detail: str
    strength: Literal["clear", "lean", "coin-flip"]


class Explanation(BaseModel):
    verdict: Verdict
    factors: list[Factor]
    warnings: list[str]
    differentiators: list[str]


def _normalised(judgment: SubJudgment) -> float:
    """Map any judgment onto 0..1 so metrics can be compared with each other."""
    if judgment.kind == "score" and judgment.max_score:
        return judgment.value / judgment.max_score
    return judgment.value


def _display(judgment: SubJudgment) -> str:
    if judgment.kind == "score":
        out_of = judgment.max_score if judgment.max_score is not None else "?"
        return f"{judgment.value:.1f}/{out_of}  {judgment.nearest_level or ''}".strip()
    return f"{judgment.value:.0%}"


def verdict_for(decision: Decision) -> Verdict:
    """Interpret the winner's probability against the no-information baseline."""
    start = decision.start
    winner = decision.name_for(start.pick)
    count = max(len(start.probabilities), 2)
    baseline = 1.0 / count
    lift = start.pick_probability - baseline

    coin_flip = decision.coin_flip_probability or 0.0
    if lift < SLIGHT_EDGE or coin_flip > 0.6 or start.confidence < 0.4:
        strength: Literal["clear", "lean", "coin-flip"] = "coin-flip"
    elif lift >= CLEAR_EDGE and start.confidence >= 0.6:
        strength = "clear"
    else:
        strength = "lean"

    detail = {
        "clear": (
            f"{start.pick_probability:.0%} of the probability mass lands on {winner}, "
            f"well clear of the {baseline:.0%} you would get from a coin flip."
        ),
        "lean": (
            f"{winner} takes {start.pick_probability:.0%} against a {baseline:.0%} baseline - "
            "a real edge, but not one to agonise over."
        ),
        "coin-flip": (
            f"{winner} edges it at {start.pick_probability:.0%}, barely above the {baseline:.0%} "
            "baseline. Start whichever you would not regret."
        ),
    }[strength]

    headline = {
        "clear": f"Start {winner}",
        "lean": f"Lean {winner}",
        "coin-flip": f"Slight lean {winner} - effectively a toss-up",
    }[strength]

    return Verdict(headline=headline, detail=detail, strength=strength)


def factors_for(decision: Decision) -> list[Factor]:
    """One row per metric, with the candidate who holds the edge marked."""
    factors: list[Factor] = []
    for metric in METRIC_ORDER:
        values: dict[str, str] = {}
        raw: dict[str, float] = {}
        for slug, analysis in decision.analyses.items():
            judgment = analysis.get(metric)
            if judgment is None:
                continue
            values[slug] = _display(judgment)
            raw[slug] = _normalised(judgment)

        if len(raw) < 2:
            continue

        polarity = POLARITY.get(metric, "higher_better")
        best = max(raw, key=lambda s: raw[s]) if polarity != "lower_better" else min(raw, key=lambda s: raw[s])
        swing = round(max(raw.values()) - min(raw.values()), 4)

        factors.append(
            Factor(
                metric=metric,
                label=METRIC_LABELS.get(metric, metric),
                values=values,
                raw=raw,
                edge=best if polarity != "descriptive" and swing > 0.05 else None,
                swing=swing,
            )
        )
    return factors


def differentiators_for(decision: Decision, factors: list[Factor], limit: int = 3) -> list[str]:
    """The handful of metrics that actually separated the candidates."""
    winner = decision.start.pick
    ranked = sorted((f for f in factors if f.swing > 0.05), key=lambda f: f.swing, reverse=True)

    lines: list[str] = []
    for factor in ranked[:limit]:
        if factor.edge == winner:
            lines.append(f"{factor.label} favours {decision.name_for(winner)}: {factor.values.get(winner, '')}")
        elif factor.edge:
            lines.append(
                f"{factor.label} actually favours {decision.name_for(factor.edge)} "
                f"({factor.values.get(factor.edge, '')}) - the pick wins on other grounds."
            )
        else:
            spread = ", ".join(f"{decision.name_for(s)} {v}" for s, v in factor.values.items())
            lines.append(f"{factor.label} varies: {spread}")
    return lines


def warnings_for(decision: Decision) -> list[str]:
    """Deterministic checks that should override or qualify the model's call."""
    warnings: list[str] = []

    for slug, dossier in decision.dossiers.items():
        name = dossier.ref.name
        if dossier.game.on_bye:
            warnings.append(f"{name} is on a bye this week and will score zero.")
        status = (dossier.injury.status or "").strip()
        if status.upper() in {"OUT", "IR", "PUP", "SUS", "DNR"}:
            warnings.append(f"{name} is listed {status} and is unlikely to play at all.")
        elif status:
            warnings.append(f"{name} is listed {status}{f' ({dossier.injury.body_part})' if dossier.injury.body_part else ''}.")

        sufficiency = decision.analyses.get(slug, None)
        judgment = sufficiency.get("data_sufficiency") if sufficiency else None
        if judgment is not None and judgment.value < 0.45:
            warnings.append(
                f"Thin data on {name} - the model flagged the available information as insufficient."
            )
        if dossier.missing:
            warnings.append(f"Could not fetch for {name}: {'; '.join(dossier.missing[:3])}.")

    if decision.start.confidence < 0.4:
        warnings.append(
            f"Low selection confidence ({decision.start.confidence:.0%}). Worth a second opinion."
        )

    if decision.safest and decision.highest_upside and decision.safest.pick != decision.highest_upside.pick:
        safe = decision.name_for(decision.safest.pick)
        upside = decision.name_for(decision.highest_upside.pick)
        warnings.append(f"Floor and ceiling disagree: {safe} is the safer start, {upside} the higher ceiling.")

    return warnings


def explain(decision: Decision) -> Explanation:
    factors = factors_for(decision)
    return Explanation(
        verdict=verdict_for(decision),
        factors=factors,
        warnings=warnings_for(decision),
        differentiators=differentiators_for(decision, factors),
    )
