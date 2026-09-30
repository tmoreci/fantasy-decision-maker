"""The web app, end to end through FastAPI's TestClient, with Sleeper and Jev faked."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from helpers import FakeJevClient

from fantasy_decision.models import LeagueSettings
from fantasy_decision.providers.base import ProviderError
from fantasy_decision.providers.espn import EspnProvider
from fantasy_decision.providers.sleeper import SleeperProvider
from fantasy_decision.web import views
from fantasy_decision.web.app import WebSettings, create_app
from fantasy_decision.web.security import ACCESS_COOKIE, RateLimiter, access_token

INDEX = {
    "1": {"player_id": "1", "full_name": "Alpha Back", "position": "RB", "team": "ATL", "status": "Active",
          "search_rank": 5, "depth_chart_order": 1, "gsis_id": "00-0001"},
    "2": {"player_id": "2", "full_name": "Bravo Back", "position": "RB", "team": "NYJ", "status": "Active",
          "search_rank": 40, "injury_status": "Questionable", "injury_body_part": "Ankle"},
    "3": {"player_id": "3", "full_name": "Charlie Alphabet", "position": "WR", "team": "KC", "status": "Active",
          "search_rank": 1},
    "4": {"player_id": "4", "full_name": "Alpha Retired", "position": "QB", "team": None, "status": "Inactive"},
    "5": {"player_id": "5", "full_name": "Alpha Lineman", "position": "OL", "team": "DAL", "status": "Active"},
}


def _no_such_user(username: str) -> str:
    raise ProviderError(f"sleeper: no such user {username!r}")


@pytest.fixture
def sleeper(monkeypatch) -> SleeperProvider:
    provider = SleeperProvider()
    monkeypatch.setattr(provider, "player_index", lambda: INDEX)
    monkeypatch.setattr(provider, "current_state", lambda: {"week": 3, "season": "2026"})
    monkeypatch.setattr(provider, "trending", lambda *a, **k: {})
    monkeypatch.setattr(provider, "user_id", lambda name: "u1" if name == "tester" else _no_such_user(name))
    monkeypatch.setattr(
        provider, "leagues_for_user",
        lambda user_id, season: [{"league_id": "L1", "name": "Dynasty Bros", "total_rosters": 12}],
    )
    monkeypatch.setattr(provider, "rosters", lambda league_id: [{"owner_id": "u1", "players": ["1", "2", "3"], "starters": ["3"]}])
    monkeypatch.setattr(provider, "league", lambda league_id: LeagueSettings(name="Dynasty Bros", source="sleeper"))
    return provider


@pytest.fixture
def espn(monkeypatch) -> EspnProvider:
    provider = EspnProvider()

    def offline(*_, **__):
        raise ProviderError("espn: offline in tests")

    monkeypatch.setattr(provider, "game_context", offline)
    return provider


@pytest.fixture
def history(monkeypatch) -> list:
    written: list = []
    monkeypatch.setattr(views, "append_history", written.append)
    return written


@pytest.fixture
def make_app(sleeper, espn, analysis_answers, decision_answers, history):
    def build(*, jev: bool = True, **settings) -> TestClient:
        services = views.Services(
            sleeper=sleeper,
            espn=espn,
            make_client=(lambda: FakeJevClient(analysis_answers, decision_answers)) if jev else (lambda: None),
        )
        return TestClient(create_app(WebSettings(warm_player_index=False, **settings), services))

    return build


@pytest.fixture
def client(make_app) -> TestClient:
    return make_app()


class TestSearch:
    def test_prefix_beats_word_start_beats_substring(self, sleeper):
        names = [ref.name for ref in sleeper.search_players("alph")]
        assert names == ["Alpha Back", "Charlie Alphabet"]

    def test_skips_inactive_and_non_fantasy_positions(self, sleeper):
        names = {ref.name for ref in sleeper.search_players("alpha")}
        assert "Alpha Retired" not in names
        assert "Alpha Lineman" not in names

    def test_needs_two_characters(self, sleeper):
        assert sleeper.search_players("a") == []

    def test_api_includes_injury_status(self, client):
        body = client.get("/api/search", params={"q": "bravo"}).json()
        assert body == [{"sleeper_id": "2", "name": "Bravo Back", "position": "RB", "team": "NYJ",
                         "injury_status": "Questionable", "starter": False}]

    def test_page_renders_add_buttons(self, client):
        html = client.get("/search", params={"q": "alpha"}).text
        assert 'data-add-player="1"' in html
        assert "Alpha Back" in html


class TestLeaguesAndRoster:
    def test_leagues_page_remembers_username(self, client):
        response = client.get("/leagues", params={"username": "tester"})
        assert "Dynasty Bros (12 teams)" in response.text
        assert response.cookies.get("fdm_username") == "tester"

    def test_unknown_user_gets_plain_language(self, client):
        html = client.get("/leagues", params={"username": "nobody"}).text
        assert "find a Sleeper user called" in html
        assert "sleeper:" not in html

    def test_api_unknown_user_is_404(self, client):
        response = client.get("/api/leagues", params={"username": "nobody"})
        assert response.status_code == 404

    def test_roster_is_grouped_with_starters_and_injuries(self, client):
        html = client.get("/roster", params={"username": "tester", "league_id": "L1"}).text
        assert html.index("<legend>RB</legend>") < html.index("<legend>WR</legend>")
        assert "starting" in html  # Charlie Alphabet
        assert "Questionable" in html

    def test_api_roster_orders_by_position(self, client):
        body = client.get("/api/roster", params={"username": "tester", "league_id": "L1"}).json()
        assert [p["name"] for p in body] == ["Alpha Back", "Bravo Back", "Charlie Alphabet"]
        assert body[2]["starter"] is True


class TestDecide:
    def test_api_returns_decision_and_explanation(self, client, history):
        response = client.post("/api/decide", json={"players": ["1", "2"], "league_id": "L1"})
        assert response.status_code == 200
        body = response.json()
        assert body["decision"]["start"]["pick"] == "alpha_back"
        assert body["explanation"]["verdict"]["headline"] == "Start Alpha Back"
        assert body["decision"]["league"]["name"] == "Dynasty Bros"
        assert len(history) == 1

    def test_page_renders_verdict_with_every_candidate(self, client):
        html = client.post("/decide", data={"player": ["1", "2"], "need": "safe"}).text
        assert "Start Alpha Back" in html
        assert "74%" in html and "26%" in html
        assert 'class="verdict clear"' in html

    def test_page_asks_for_two_or_three(self, client):
        html = client.post("/decide", data={"player": ["1"]}).text
        assert "Pick two or three players" in html

    def test_duplicate_picks_collapse(self, client):
        html = client.post("/decide", data={"player": ["1", "1"]}).text
        assert "Pick two or three players" in html

    def test_api_validates_count(self, client):
        assert client.post("/api/decide", json={"players": ["1"]}).status_code == 422

    def test_unknown_player_is_a_400_with_friendly_copy(self, client):
        response = client.post("/api/decide", json={"players": ["1", "Zed Nobody"]})
        assert response.status_code == 400
        assert "Couldn't find a player called" in response.json()["detail"]

    def test_missing_api_key(self, make_app, monkeypatch):
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        client = make_app(jev=False)
        assert "no TypeSafe API key" in client.get("/").text
        response = client.post("/api/decide", json={"players": ["1", "2"]})
        assert response.status_code == 503

    def test_rate_limit(self, make_app):
        client = make_app(decisions_per_hour=1)
        assert client.post("/api/decide", json={"players": ["1", "2"]}).status_code == 200
        assert client.post("/api/decide", json={"players": ["1", "2"]}).status_code == 429
        assert "Take a break" in client.post("/decide", data={"player": ["1", "2"]}).text


class TestPasscode:
    @pytest.fixture
    def locked(self, make_app) -> TestClient:
        return make_app(passcode="gridiron")

    def test_pages_redirect_to_login(self, locked):
        response = locked.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_htmx_requests_get_an_hx_redirect(self, locked):
        response = locked.get("/search", params={"q": "alpha"}, headers={"HX-Request": "true"})
        assert response.headers["HX-Redirect"] == "/login"

    def test_api_is_401(self, locked):
        assert locked.get("/api/search", params={"q": "alpha"}).status_code == 401

    def test_open_paths(self, locked):
        assert locked.get("/healthz").status_code == 200
        assert locked.get("/static/app.css").status_code == 200

    def test_wrong_passcode(self, locked):
        response = locked.post("/login", data={"passcode": "nope"})
        assert response.status_code == 401
        assert "not it" in response.text

    def test_right_passcode_sets_a_token_not_the_passcode(self, locked):
        response = locked.post("/login", data={"passcode": "gridiron"}, follow_redirects=False)
        assert response.status_code == 303
        token = response.cookies.get(ACCESS_COOKIE)
        assert token == access_token("gridiron")
        assert "gridiron" not in token
        assert locked.get("/").status_code == 200

    def test_bearer_header_for_api_clients(self, locked):
        response = locked.get("/api/search", params={"q": "alpha"}, headers={"Authorization": "Bearer gridiron"})
        assert response.status_code == 200


def test_rate_limiter_window(monkeypatch):
    clock = iter([0.0, 1.0, 2.0, 3601.5])
    monkeypatch.setattr("fantasy_decision.web.security.time.monotonic", lambda: next(clock))
    limiter = RateLimiter(2, window_seconds=3600)
    assert limiter.allow("ip")
    assert limiter.allow("ip")
    assert not limiter.allow("ip")
    assert limiter.allow("ip")  # the first hit has aged out
