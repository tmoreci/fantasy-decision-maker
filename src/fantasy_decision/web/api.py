"""JSON API. The contract any front end builds on; browse it at /docs."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request

from fantasy_decision.decide import DecisionError
from fantasy_decision.providers.base import ProviderError
from fantasy_decision.service import ConfigError, InputError
from fantasy_decision.web import views
from fantasy_decision.web.views import DecideRequest, DecisionView, LeagueOption, PlayerOption

logger = logging.getLogger("fantasy_decision.web.api")

router = APIRouter(prefix="/api", tags=["api"])


def _services(request: Request) -> views.Services:
    return request.app.state.services


@router.get("/search", response_model=list[PlayerOption])
def search(request: Request, q: str = Query(min_length=1, max_length=60)) -> list[PlayerOption]:
    try:
        return views.search_players(_services(request), q)
    except ProviderError as error:
        raise HTTPException(502, views.friendly_error(error)) from error


@router.get("/leagues", response_model=list[LeagueOption])
def leagues(request: Request, username: str = Query(min_length=1, max_length=60)) -> list[LeagueOption]:
    try:
        return views.list_leagues(_services(request), username)
    except ProviderError as error:
        raise HTTPException(404, views.friendly_error(error)) from error


@router.get("/roster", response_model=list[PlayerOption])
def roster(
    request: Request,
    username: str = Query(min_length=1, max_length=60),
    league_id: str = Query(min_length=1, max_length=40),
) -> list[PlayerOption]:
    try:
        return views.roster_players(_services(request), username, league_id)
    except ProviderError as error:
        raise HTTPException(404, views.friendly_error(error)) from error


@router.post("/decide", response_model=DecisionView)
def decide(request: Request, body: DecideRequest) -> DecisionView:
    if not request.app.state.decide_limiter.allow(request.client.host if request.client else "unknown"):
        raise HTTPException(429, "Too many decisions this hour. Try again later.")
    try:
        return views.make_decision(_services(request), body)
    except InputError as error:
        raise HTTPException(400, views.friendly_error(error)) from error
    except ConfigError as error:
        raise HTTPException(503, views.friendly_error(error)) from error
    except (ProviderError, DecisionError) as error:
        raise HTTPException(502, views.friendly_error(error)) from error
