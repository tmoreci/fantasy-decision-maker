"""HTML pages and HTMX fragments.

Fragments always answer 200, even for errors, because HTMX only swaps successful
responses; the error is rendered as a friendly message in place of the content.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from fantasy_decision.web import views
from fantasy_decision.web.security import ACCESS_COOKIE, access_token, passcode_is_valid
from fantasy_decision.web.views import DecideRequest

logger = logging.getLogger("fantasy_decision.web.pages")

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
router = APIRouter(include_in_schema=False)

USERNAME_COOKIE = "fdm_username"
LEAGUE_COOKIE = "fdm_league"
REMEMBER_SECONDS = 60 * 60 * 24 * 180

NEED_CHOICES = [
    ("auto", "Just pick the best"),
    ("safe", "I'm ahead, play it safe"),
    ("upside", "I'm behind, swing big"),
]


def _services(request: Request) -> views.Services:
    return request.app.state.services


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _remember(response: Response, request: Request, name: str, value: str) -> None:
    response.set_cookie(
        name, value, max_age=REMEMBER_SECONDS, httponly=True, samesite="lax", secure=request.url.scheme == "https"
    )


def _error(request: Request, error: Exception) -> HTMLResponse:
    known = (views.InputError, views.ConfigError, views.ProviderError, views.DecisionError)
    if not isinstance(error, known):
        logger.exception("unexpected error serving %s", request.url.path, exc_info=error)
    return templates.TemplateResponse(request, "_error.html", {"message": views.friendly_error(error)})


# ------------------------------------------------------------------------- pages


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "username": request.cookies.get(USERNAME_COOKIE, ""),
            "week": views.current_week(_services(request)),
            "needs": NEED_CHOICES,
            "missing_key": not os.environ.get("TYPESAFE_API_KEY") and request.app.state.require_api_key,
        },
    )


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"failed": False})


@router.post("/login", response_model=None)
def login(request: Request, passcode: Annotated[str, Form()] = "") -> Response:
    expected = request.app.state.settings.passcode
    if not expected:
        return RedirectResponse("/", status_code=303)
    if not request.app.state.login_limiter.allow(_client_key(request)) or not passcode_is_valid(passcode, expected):
        return templates.TemplateResponse(request, "login.html", {"failed": True}, status_code=401)
    response = RedirectResponse("/", status_code=303)
    _remember(response, request, ACCESS_COOKIE, access_token(expected))
    return response


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


# --------------------------------------------------------------------- fragments


@router.get("/leagues", response_class=HTMLResponse)
def leagues(request: Request, username: str = "") -> HTMLResponse:
    username = username.strip()
    if not username:
        return templates.TemplateResponse(request, "_error.html", {"message": "Type your Sleeper username first."})
    try:
        found = views.list_leagues(_services(request), username)
    except Exception as error:  # noqa: BLE001 - every failure becomes a message
        return _error(request, error)

    remembered = request.cookies.get(LEAGUE_COOKIE)
    selected = remembered if any(league.league_id == remembered for league in found) else None
    if selected is None and found:
        selected = found[0].league_id
    response = templates.TemplateResponse(
        request, "_leagues.html", {"leagues": found, "selected": selected, "username": username}
    )
    _remember(response, request, USERNAME_COOKIE, username)
    return response


@router.get("/roster", response_class=HTMLResponse)
def roster(request: Request, username: str = "", league_id: str = "") -> HTMLResponse:
    if not username.strip() or not league_id:
        return HTMLResponse("")
    try:
        players = views.roster_players(_services(request), username, league_id)
    except Exception as error:  # noqa: BLE001
        return _error(request, error)

    groups: dict[str, list[views.PlayerOption]] = {}
    for player in players:
        groups.setdefault(player.position or "Other", []).append(player)
    response = templates.TemplateResponse(request, "_roster.html", {"groups": groups})
    _remember(response, request, LEAGUE_COOKIE, league_id)
    return response


@router.get("/search", response_class=HTMLResponse)
def search(request: Request, q: Annotated[str, Query(max_length=60)] = "") -> HTMLResponse:
    try:
        results = views.search_players(_services(request), q) if len(q.strip()) >= 2 else []
    except Exception as error:  # noqa: BLE001
        return _error(request, error)
    return templates.TemplateResponse(request, "_search.html", {"results": results, "query": q.strip()})


@router.post("/decide", response_class=HTMLResponse)
def decide(
    request: Request,
    player: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI copies form defaults
    league_id: Annotated[str, Form()] = "",
    scoring: Annotated[str, Form()] = "ppr",
    need: Annotated[str, Form()] = "auto",
) -> HTMLResponse:
    picked = list(dict.fromkeys(p for p in player if p))
    if not 2 <= len(picked) <= 3:
        return templates.TemplateResponse(request, "_error.html", {"message": "Pick two or three players to compare."})
    if not request.app.state.decide_limiter.allow(_client_key(request)):
        return templates.TemplateResponse(
            request, "_error.html", {"message": "That's a lot of decisions for one hour. Take a break and try again later."}
        )

    try:
        body = DecideRequest(players=picked, league_id=league_id or None, scoring=scoring, need=need)  # type: ignore[arg-type]
        view = views.make_decision(_services(request), body)
    except ValueError as error:  # pydantic validation of scoring/need
        if isinstance(error, views.InputError):
            return _error(request, error)
        return templates.TemplateResponse(request, "_error.html", {"message": "Something about that request didn't make sense."})
    except Exception as error:  # noqa: BLE001
        return _error(request, error)

    decision = view.decision
    ranked = sorted(decision.start.probabilities.items(), key=lambda kv: -kv[1])
    cost = decision.input_tokens * 0.042 / 1_000_000 if decision.input_tokens else None
    return templates.TemplateResponse(
        request,
        "_verdict.html",
        {"decision": decision, "explanation": view.explanation, "notes": view.notes, "ranked": ranked, "cost": cost},
    )
