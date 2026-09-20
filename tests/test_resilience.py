"""Caching and graceful degradation.

The design promise is that a dead provider costs one signal, not the run - and that
the gap is named in state rather than silently dropped.
"""

from __future__ import annotations

import time

import pytest

from fantasy_decision.cache import DiskCache
from fantasy_decision.dossier import gather_dossiers
from fantasy_decision.models import GameContext, InjuryInfo, PlayerRef, TrendingSignal, UsageStats
from fantasy_decision.providers.base import ProviderError, soft_call


class TestDiskCache:
    def test_round_trips_a_value(self, tmp_path):
        cache = DiskCache(tmp_path)
        cache.set("k", {"a": 1})
        assert cache.get("k", ttl_seconds=60) == {"a": 1}

    def test_expired_entries_are_misses(self, tmp_path):
        cache = DiskCache(tmp_path)
        cache.set("k", "v")
        time.sleep(0.01)
        assert cache.get("k", ttl_seconds=0.001) is None

    def test_zero_ttl_always_misses(self, tmp_path):
        cache = DiskCache(tmp_path)
        cache.set("k", "v")
        assert cache.get("k", ttl_seconds=0) is None

    def test_unknown_key_is_a_miss(self, tmp_path):
        assert DiskCache(tmp_path).get("never-written", ttl_seconds=60) is None

    def test_disabled_cache_stores_nothing(self, tmp_path):
        cache = DiskCache(tmp_path, enabled=False)
        cache.set("k", "v")
        assert cache.get("k", ttl_seconds=60) is None

    def test_corrupt_entry_is_a_miss_not_a_crash(self, tmp_path):
        cache = DiskCache(tmp_path)
        cache.set("k", "v")
        next(tmp_path.glob("*.json")).write_text("{not json")
        assert cache.get("k", ttl_seconds=60) is None

    def test_unserialisable_value_does_not_raise(self, tmp_path):
        # A cache write failure must never break a decision.
        DiskCache(tmp_path).set("k", {1, 2, 3})

    def test_clear_removes_entries(self, tmp_path):
        cache = DiskCache(tmp_path)
        cache.set("a", 1)
        cache.set("b", 2)
        assert cache.clear() == 2
        assert cache.get("a", ttl_seconds=60) is None


class TestSoftCall:
    def test_passes_through_a_success(self):
        missing: list[str] = []
        assert soft_call("thing", lambda: 42, missing) == 42
        assert missing == []

    def test_records_a_provider_failure_by_name(self):
        missing: list[str] = []

        def boom():
            raise ProviderError("upstream down")

        assert soft_call("Vegas line", boom, missing) is None
        assert missing == ["Vegas line"]

    def test_an_unexpected_exception_is_also_contained(self):
        missing: list[str] = []

        def boom():
            raise KeyError("shape changed")

        assert soft_call("usage metrics", boom, missing) is None
        assert missing == ["usage metrics"]


class FakeSleeper:
    def __init__(self, *, trending_ok=True, injury_ok=True):
        self.trending_ok = trending_ok
        self.injury_ok = injury_ok

    def trending(self, kind="add", **_):
        if not self.trending_ok:
            raise ProviderError("trending unavailable")
        return {"1": (1200, 4)}

    def injury_for(self, ref):
        if not self.injury_ok:
            raise ProviderError("player index unavailable")
        return InjuryInfo(status="Questionable", depth_chart_order=1)

    def trending_signal(self, ref, adds, drops):
        entry = adds.get(ref.sleeper_id or "")
        return TrendingSignal(adds_24h=entry[0] if entry else None, add_rank=entry[1] if entry else None)


class FakeEspn:
    def __init__(self, *, ok=True):
        self.ok = ok

    def game_context(self, team, season, week):
        if not self.ok:
            raise ProviderError("scoreboard 503")
        return GameContext(opponent="CAR", implied_team_total=27.0)


class FakeNflverse:
    def __init__(self, *, ok=True):
        self.ok = ok

    def usage_for(self, ref, season, week):
        if not self.ok:
            raise ProviderError("nflverse extra not installed")
        return UsageStats(games_sampled=5, snap_pct=0.78)


@pytest.fixture
def refs():
    return [PlayerRef(name="Alpha Back", position="RB", team="ATL", sleeper_id="1")]


class TestGracefulDegradation:
    def gather(self, refs, **kwargs):
        defaults = {"sleeper": FakeSleeper(), "espn": FakeEspn(), "nflverse": FakeNflverse()}
        return gather_dossiers(refs, season="2026", week=3, **(defaults | kwargs))

    def test_everything_working_leaves_nothing_missing(self, refs):
        dossier = self.gather(refs)["alpha_back"]
        assert dossier.missing == []
        assert dossier.game.implied_team_total == 27.0
        assert dossier.usage.snap_pct == 0.78
        assert dossier.trending.adds_24h == 1200

    def test_a_dead_scoreboard_costs_only_the_market_signal(self, refs):
        dossier = self.gather(refs, espn=FakeEspn(ok=False))["alpha_back"]
        assert dossier.injury.status == "Questionable"  # other providers unaffected
        assert dossier.game.implied_team_total is None
        assert any("Vegas line" in entry for entry in dossier.missing)

    def test_missing_nflverse_extra_is_recorded_not_fatal(self, refs):
        dossier = self.gather(refs, nflverse=None)["alpha_back"]
        assert any("advanced usage" in entry for entry in dossier.missing)
        assert dossier.injury.status == "Questionable"

    def test_every_provider_down_still_produces_a_dossier(self, refs):
        dossier = self.gather(
            refs,
            sleeper=FakeSleeper(trending_ok=False, injury_ok=False),
            espn=FakeEspn(ok=False),
            nflverse=FakeNflverse(ok=False),
        )["alpha_back"]
        assert dossier.ref.name == "Alpha Back"
        # Four named gaps, so the model is told what it does not know.
        assert len(dossier.missing) >= 4

    def test_gaps_are_named_per_player(self, refs):
        refs.append(PlayerRef(name="Bravo Back", position="RB", team="NYJ", sleeper_id="2"))
        dossiers = self.gather(refs, nflverse=FakeNflverse(ok=False))
        assert any("Alpha Back" in entry for entry in dossiers["alpha_back"].missing)
        assert any("Bravo Back" in entry for entry in dossiers["bravo_back"].missing)
