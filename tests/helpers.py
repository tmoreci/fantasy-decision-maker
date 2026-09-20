"""Builders for fake Jev responses.

These go through `model_validate_json` rather than `model_validate` on purpose: the
SDK's response models are strict, and only the JSON path coerces string object keys
into the integer score levels the real API returns.
"""

from __future__ import annotations

import json
from typing import Any

from typesafe_sdk import SystemOneResponse


def score_answer(score: float, confidence: float, levels: int = 5) -> dict[str, Any]:
    legend = {str(i): f"level {i}" for i in range(levels)}
    mass = {str(i): (1.0 if i == round(score) else 0.0) for i in range(levels)}
    return {"type": "score", "score": score, "confidence": confidence, "legend": legend, "probabilities": mass}


def noul_answer(probability: float) -> dict[str, Any]:
    return {"type": "noul", "noul": probability}


def choice_answer(probabilities: dict[str, float], confidence: float) -> dict[str, Any]:
    pick = max(probabilities, key=lambda k: probabilities[k])
    return {"type": "choice", "choice": pick, "confidence": confidence, "probabilities": probabilities}


def build_response(answers: dict[str, Any], *, input_tokens: int = 100) -> SystemOneResponse:
    payload = {
        "model": "jev-latest",
        "usage": {"input_tokens": input_tokens, "output_tokens": 12},
        "answers": answers,
    }
    return SystemOneResponse.model_validate_json(json.dumps(payload))


class FakeJevClient:
    """Stands in for TypeSafeClient, recording what it was asked."""

    def __init__(self, analysis: dict[str, Any], decision: dict[str, Any]) -> None:
        self._responses = [analysis, decision]
        self.calls: list[dict[str, Any]] = []

    def system_one(self, state: Any, questions: Any, *, model: str | None = None, **_: Any) -> SystemOneResponse:
        self.calls.append({"state": state, "questions": questions, "model": model})
        if not self._responses:
            raise AssertionError("FakeJevClient called more times than it has responses")
        return build_response(self._responses.pop(0))
