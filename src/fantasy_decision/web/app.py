"""FastAPI application: a phone-friendly front end for the start/sit decision."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from fantasy_decision.cache import DiskCache
from fantasy_decision.providers.espn import EspnProvider
from fantasy_decision.providers.nflverse import NflverseProvider
from fantasy_decision.providers.sleeper import SleeperProvider
from fantasy_decision.web import api, pages
from fantasy_decision.web.security import ACCESS_COOKIE, RateLimiter, passcode_is_valid, token_is_valid
from fantasy_decision.web.views import Services

logger = logging.getLogger("fantasy_decision.web")

# Reachable without the passcode: the login page itself, assets, and the health check.
OPEN_PATHS = {"/login", "/healthz"}


@dataclass(frozen=True)
class WebSettings:
    passcode: str | None = None
    decisions_per_hour: int = 30
    logins_per_hour: int = 10
    advanced: bool = True
    warm_player_index: bool = True

    @classmethod
    def from_env(cls) -> WebSettings:
        return cls(
            passcode=os.environ.get("APP_PASSCODE") or None,
            decisions_per_hour=int(os.environ.get("DECISIONS_PER_HOUR", "30")),
            advanced=os.environ.get("FDM_ADVANCED", "1").lower() not in {"0", "false", "no"},
        )


def default_services(settings: WebSettings) -> Services:
    cache = DiskCache()
    return Services(
        sleeper=SleeperProvider(cache),
        espn=EspnProvider(cache),
        nflverse=NflverseProvider() if settings.advanced else None,
    )


def _warm(services: Services) -> None:
    """Load the player index in the background so the first search isn't slow."""
    try:
        services.sleeper.player_index()
    except Exception as error:  # noqa: BLE001 - warming is best effort
        logger.warning("could not preload the Sleeper player index: %s", error)


def create_app(settings: WebSettings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or WebSettings.from_env()
    services = services or default_services(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.warm_player_index:
            threading.Thread(target=_warm, args=(services,), daemon=True).start()
        yield
        services.close()

    app = FastAPI(
        title="Start Who?",
        description="Pick who to start in fantasy football, with calibrated probabilities from TypeSafe AI's Jev.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.services = services
    app.state.decide_limiter = RateLimiter(settings.decisions_per_hour)
    app.state.login_limiter = RateLimiter(settings.logins_per_hour)
    # Tests inject a fake Jev client, so they don't need a real key to see the page unwarned.
    app.state.require_api_key = services.make_client() is None

    @app.middleware("http")
    async def require_passcode(request: Request, call_next):  # noqa: ANN001, ANN202
        passcode = settings.passcode
        path = request.url.path
        if not passcode or path in OPEN_PATHS or path.startswith("/static/"):
            return await call_next(request)

        bearer = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if token_is_valid(request.cookies.get(ACCESS_COOKIE), passcode) or passcode_is_valid(bearer, passcode):
            return await call_next(request)

        if path.startswith("/api/") or path in {"/docs", "/openapi.json"}:
            return JSONResponse({"detail": "Passcode required."}, status_code=401)
        if request.headers.get("hx-request"):
            return Response(status_code=200, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.include_router(api.router)
    app.include_router(pages.router)
    return app


def main() -> None:
    import uvicorn  # noqa: PLC0415

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        create_app(),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
