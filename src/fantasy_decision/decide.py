"""Two-pass decision orchestration against Jev."""

from __future__ import annotations

import logging
import time
from typing import Any

from typesafe_sdk import ChoiceAnswer, ScoreAnswer, SystemOneResponse, TypeSafeClient

from fantasy_decision.dossier import build_state
from fantasy_decision.models import (
    Decision,
    LeagueSettings,
    PlayerAnalysis,
    PlayerDossier,
    Recommendation,
    SubJudgment,
)
from fantasy_decision.questions import (
    METRIC_LABELS,
    SEPARATOR,
    build_analysis_questions,
    build_decision_questions,
)

logger = logging.getLogger("fantasy_decision.decide")

MIN_CANDIDATES = 2
MAX_CANDIDATES = 3


class DecisionError(RuntimeError):
    """The decision could not be made at all (bad input, or the API refused)."""


def _to_judgment(name: str, answer: Any) -> SubJudgment | None:
    """Normalise a Jev answer into a SubJudgment, keeping its rubric."""
    if isinstance(answer, ScoreAnswer):
        legend = {int(level): text for level, text in answer.legend.items() if isinstance(text, str)}
        return SubJudgment(
            name=name,
            kind="score",
            value=float(answer.score),
            confidence=float(answer.confidence),
            legend=legend,
            max_score=max(legend) if legend else None,
        )
    # NoulAnswer carries its uncertainty in the probability itself, with no separate
    # confidence field, so `confidence` stays None here by design.
    noul = getattr(answer, "noul", None)
    if noul is not None:
        return SubJudgment(name=name, kind="noul", value=float(noul))
    return None


def parse_analysis(response: SystemOneResponse, slugs: list[str]) -> dict[str, PlayerAnalysis]:
    """Split pass-one's flat answer map back out per player."""
    grouped: dict[str, dict[str, SubJudgment]] = {slug: {} for slug in slugs}
    for key, answer in response.answers.items():
        slug, _, metric = key.partition(SEPARATOR)
        if not metric or slug not in grouped:
            logger.debug("ignoring unexpected answer key %r", key)
            continue
        judgment = _to_judgment(metric, answer)
        if judgment is not None:
            grouped[slug][metric] = judgment
    return {slug: PlayerAnalysis(slug=slug, judgments=judgments) for slug, judgments in grouped.items()}


def analysis_for_state(analyses: dict[str, PlayerAnalysis]) -> dict[str, Any]:
    """Render pass-one output into the compact form pass two reads from state.

    Each judgment is given as both the number and the rubric text it lands on, so
    the second pass does not have to re-derive what a 2.8 means.
    """
    payload: dict[str, Any] = {}
    for slug, analysis in analyses.items():
        entry: dict[str, Any] = {}
        for metric, judgment in analysis.judgments.items():
            label = METRIC_LABELS.get(metric, metric)
            if judgment.kind == "score":
                entry[metric] = {
                    "label": label,
                    "score": round(judgment.value, 2),
                    "out_of": judgment.max_score,
                    "reads_as": judgment.nearest_level,
                    "confidence": round(judgment.confidence or 0.0, 3),
                }
            else:
                entry[metric] = {
                    "label": label,
                    "probability_true": round(judgment.value, 3),
                }
        payload[slug] = entry
    return payload


def _recommendation(response: SystemOneResponse, key: str) -> Recommendation | None:
    answer = response.answers.get(key)
    if not isinstance(answer, ChoiceAnswer):
        return None
    return Recommendation(
        pick=answer.choice,
        confidence=float(answer.confidence),
        probabilities={label: round(float(p), 4) for label, p in answer.probabilities.items()},
    )


def decide(
    dossiers: dict[str, PlayerDossier],
    league: LeagueSettings,
    *,
    season: str,
    week: int,
    client: TypeSafeClient,
    need: str = "auto",
    model: str | None = None,
) -> Decision:
    """Run both passes and assemble the Decision."""
    if not MIN_CANDIDATES <= len(dossiers) <= MAX_CANDIDATES:
        raise DecisionError(
            f"Need between {MIN_CANDIDATES} and {MAX_CANDIDATES} players to choose between, got {len(dossiers)}."
        )

    candidates = {slug: dossier.ref.label() for slug, dossier in dossiers.items()}
    slugs = list(candidates)
    state = build_state(dossiers, league, season=season, week=week, need=need)

    started = time.perf_counter()

    # Pass one: every per-player judgment, evaluated in parallel in a single request.
    analysis_response = client.system_one(
        state=state,
        questions=build_analysis_questions(candidates),
        model=model,
    )
    analyses = parse_analysis(analysis_response, slugs)

    # Pass two: the head-to-head, against state enriched with pass one's findings.
    enriched = dict(state)
    enriched["analysis"] = analysis_for_state(analyses)
    enriched["analysis_note"] = (
        "The analysis field holds per-player judgments already made about this same state. "
        "Use them as inputs; they are not independent evidence."
    )

    decision_response = client.system_one(
        state=enriched,
        questions=build_decision_questions(candidates, need=need),
        model=model,
    )

    latency_ms = (time.perf_counter() - started) * 1000

    start = _recommendation(decision_response, "start")
    if start is None:
        raise DecisionError("Jev did not return a 'start' choice; cannot recommend a player.")

    coin_flip_answer = decision_response.nouls.get("coin_flip")
    input_tokens = sum(
        response.usage.input_tokens or 0 for response in (analysis_response, decision_response)
    )

    return Decision(
        week=week,
        season=season,
        league=league,
        dossiers=dossiers,
        analyses=analyses,
        start=start,
        safest=_recommendation(decision_response, "safest"),
        highest_upside=_recommendation(decision_response, "highest_upside"),
        coin_flip_probability=float(coin_flip_answer.noul) if coin_flip_answer else None,
        model=decision_response.model,
        input_tokens=input_tokens or None,
        latency_ms=round(latency_ms, 1),
    )
