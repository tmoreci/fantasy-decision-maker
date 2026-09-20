"""Jev question definitions.

The whole app hinges on this file. Two passes:

* Pass one asks a handful of narrow judgments *per player*. Jev evaluates every
  question independently against the shared state, in parallel, in a single
  request, so asking six questions about three players costs one round trip.
* Pass two folds pass one's answers back into the state and asks the head-to-head
  Choice. The two passes exist because questions cannot see each other's answers
  within a request; putting the analysis into state is the only way the final
  Choice can build on it.

Pass one's rubrics double as the explanation layer. Jev cannot write prose, so the
score legends are deliberately written as sentences a human would recognise.
"""

from __future__ import annotations

from typing import Any

from typesafe_sdk import Choice, Noul, Question, Score

# Question names are "<player slug>::<metric>". Slugs are [a-z0-9_], so "::" cannot collide.
SEPARATOR = "::"

MATCHUP_LEVELS = [
    "Brutal matchup: the opposing defense is among the league's best against this position.",
    "Tough matchup: the opposing defense is meaningfully above average against this position.",
    "Neutral matchup: the opposing defense is around league average against this position.",
    "Favourable matchup: the opposing defense is below average against this position.",
    "Elite matchup: the opposing defense is among the league's worst against this position.",
]

VOLUME_LEVELS = [
    "Minimal role: a rotational player who may not see meaningful touches or targets.",
    "Limited role: a complementary piece with a small share of the offense.",
    "Solid role: a dependable contributor with a steady share of the offense.",
    "Heavy role: a focal point of the offense, near the team lead in touches or targets.",
    "Workhorse role: a dominant, near-every-down share of the offense.",
]

CEILING_LEVELS = [
    "Very low variance: a predictable floor with almost no path to a big week.",
    "Low variance: steady production with limited upside.",
    "Balanced: a reasonable floor and a plausible path to a big week.",
    "High upside: real week-winning potential, with some risk of a quiet game.",
    "Boom or bust: a genuine league-winning ceiling paired with a real chance of nothing.",
]

# Score questions keyed by metric name, so the explanation layer can recover the rubric.
SCORE_RUBRICS: dict[str, list[str]] = {
    "matchup": MATCHUP_LEVELS,
    "volume": VOLUME_LEVELS,
    "ceiling": CEILING_LEVELS,
}

NOUL_METRICS = ("injury_risk", "game_script", "data_sufficiency")
METRIC_ORDER = ("volume", "matchup", "game_script", "injury_risk", "ceiling", "data_sufficiency")

METRIC_LABELS = {
    "volume": "Expected workload",
    "matchup": "Matchup",
    "game_script": "Game script",
    "injury_risk": "Injury risk",
    "ceiling": "Upside profile",
    "data_sufficiency": "Data quality",
}


def player_questions(slug: str, name: str) -> dict[str, Question]:
    """Pass-one questions for a single player."""
    where = f"candidates.{slug}"
    return {
        f"{slug}{SEPARATOR}volume": Score(
            instructions=(
                f"Considering only {name} (state field {where}), how large a share of his own offense "
                "is he expected to command this week? Judge the role, not the matchup."
            ),
            criteria=VOLUME_LEVELS,
        ),
        f"{slug}{SEPARATOR}matchup": Score(
            instructions=(
                f"How favourable is this week's defensive matchup for {name} (state field {where}), "
                "given his position and the opponent listed there?"
            ),
            criteria=MATCHUP_LEVELS,
        ),
        f"{slug}{SEPARATOR}ceiling": Score(
            instructions=(
                f"What is the shape of {name}'s likely outcome this week (state field {where})? "
                "Judge the spread of outcomes, not how good they are on average."
            ),
            criteria=CEILING_LEVELS,
        ),
        f"{slug}{SEPARATOR}injury_risk": Noul(
            instructions=(
                f"{name}'s availability or health is likely to meaningfully reduce his snaps, "
                f"workload, or effectiveness this week. See state field {where}."
            ),
            criteria={
                "true": "An injury designation, a recent return from injury, or a depth-chart situation that is likely to cut into his normal role.",
                "false": "He is expected to play his usual role at close to full effectiveness.",
            },
        ),
        f"{slug}{SEPARATOR}game_script": Noul(
            instructions=(
                f"The projected game flow favours {name} this week. Weigh the implied team total, "
                f"the spread, and how his role interacts with leading or trailing. See state field {where}."
            ),
            criteria={
                "true": "The expected pace, scoring environment, and game flow should increase his production.",
                "false": "The expected game flow works against his role, for example a run-heavy lead for a receiver or a negative script for a early-down back.",
            },
        ),
        f"{slug}{SEPARATOR}data_sufficiency": Noul(
            instructions=(
                f"There is enough information in state field {where} to judge {name} this week with confidence. "
                "Check unavailable_data before answering."
            ),
            criteria={
                "true": "The important signals - role, matchup, health, and scoring environment - are present.",
                "false": "Enough is missing or stale that any judgment here is largely guesswork.",
            },
        ),
    }


def build_analysis_questions(candidates: dict[str, str]) -> dict[str, Question]:
    """Pass one: every per-player question for every candidate, in one batch.

    `candidates` maps slug -> display name.
    """
    questions: dict[str, Question] = {}
    for slug, name in candidates.items():
        questions.update(player_questions(slug, name))
    return questions


def build_decision_questions(candidates: dict[str, str], *, need: str = "auto") -> dict[str, Question]:
    """Pass two: the head-to-head calls, asked against state enriched with pass one."""
    criteria: dict[str, Any] = {
        slug: f"Start {name}. Full profile in state field candidates.{slug}, prior analysis in state field analysis.{slug}."
        for slug, name in candidates.items()
    }

    emphasis = {
        "safe": " The manager is favoured this week, so weight the reliable floor over the ceiling.",
        "upside": " The manager is an underdog this week, so weight the ceiling over the floor.",
        "auto": "",
    }.get(need, "")

    return {
        "start": Choice(
            instructions=(
                "Exactly one of these players must be started in this fantasy football lineup this week. "
                "Which one will score the most fantasy points under this league's scoring settings?"
                + emphasis
            ),
            criteria=criteria,
        ),
        "safest": Choice(
            instructions=(
                "Which of these players has the most reliable floor, meaning the smallest chance of a "
                "week that actively loses the matchup?"
            ),
            criteria=criteria,
        ),
        "highest_upside": Choice(
            instructions=(
                "Which of these players has the best chance of a genuinely big week, the kind that wins "
                "a matchup on its own?"
            ),
            criteria=criteria,
        ),
        "coin_flip": Noul(
            instructions=(
                "These options are close enough in expected value that the choice between them barely "
                "matters this week."
            ),
            criteria={
                "true": "Any of them is a defensible start; the difference is within the noise of a single game.",
                "false": "There is a clear best option that a knowledgeable manager would identify.",
            },
        ),
    }
