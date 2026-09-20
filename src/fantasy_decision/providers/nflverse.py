"""nflverse provider: opportunity metrics that actually predict fantasy output.

Snap share, target share, air yards share and red-zone work tell you far more about
next week than last week's fantasy points do. This is the heaviest provider (it
pulls season parquet files through `nfl_data_py`), so it is an optional extra and
reports itself unavailable rather than failing the run.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from fantasy_decision.models import PlayerRef, UsageStats
from fantasy_decision.providers.base import ProviderError

logger = logging.getLogger("fantasy_decision.providers.nflverse")

RECENT_WINDOW = 3


def _import_nfl() -> Any:
    try:
        import nfl_data_py  # noqa: PLC0415
    except ImportError as error:  # pragma: no cover - depends on install extras
        raise ProviderError(
            "nflverse: advanced usage needs the optional extra. Install with: pip install 'fantasy-decision-maker[advanced]'"
        ) from error
    return nfl_data_py


def _mean(series: Any) -> float | None:
    """Mean of a pandas Series, as a plain float, or None when empty/NaN."""
    if series is None or len(series) == 0:
        return None
    value = series.mean()
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if value != value else round(value, 4)  # NaN check


class NflverseProvider:
    """Season-level usage lookups, memoised per season."""

    name = "nflverse"

    def __init__(self, *, include_redzone: bool = True) -> None:
        self.include_redzone = include_redzone

    # ------------------------------------------------------------ id mapping

    @staticmethod
    @lru_cache(maxsize=1)
    def _id_map() -> dict[str, dict[str, str]]:
        """sleeper_id -> {gsis_id, pfr_id, name} using nflverse's id crosswalk."""
        nfl = _import_nfl()
        try:
            frame = nfl.import_ids()
        except Exception as error:  # noqa: BLE001 - upstream data fetch
            raise ProviderError(f"nflverse: id crosswalk unavailable: {error}") from error

        mapping: dict[str, dict[str, str]] = {}
        for row in frame.to_dict("records"):
            sleeper = row.get("sleeper_id")
            if sleeper is None or sleeper != sleeper:  # NaN check
                continue
            key = str(sleeper).split(".")[0]  # pandas may float-ify the id
            mapping[key] = {
                "gsis_id": str(row.get("gsis_id") or ""),
                "pfr_id": str(row.get("pfr_id") or ""),
                "name": str(row.get("name") or ""),
            }
        return mapping

    def gsis_id_for(self, ref: PlayerRef) -> str | None:
        if ref.gsis_id:
            return ref.gsis_id
        if ref.sleeper_id:
            entry = self._id_map().get(ref.sleeper_id)
            if entry and entry["gsis_id"]:
                return entry["gsis_id"]
        return None

    # --------------------------------------------------------------- weekly

    @staticmethod
    @lru_cache(maxsize=4)
    def _weekly(season: int) -> Any:
        nfl = _import_nfl()
        try:
            return nfl.import_weekly_data([season])
        except Exception as error:  # noqa: BLE001
            raise ProviderError(f"nflverse: weekly data for {season} unavailable: {error}") from error

    @staticmethod
    @lru_cache(maxsize=4)
    def _snaps(season: int) -> Any:
        nfl = _import_nfl()
        try:
            return nfl.import_snap_counts([season])
        except Exception as error:  # noqa: BLE001
            raise ProviderError(f"nflverse: snap counts for {season} unavailable: {error}") from error

    @staticmethod
    @lru_cache(maxsize=4)
    def _redzone(season: int) -> dict[str, float]:
        """gsis_id -> red-zone touches per game played, from play-by-play.

        Column-subsetted so this pulls a few MB rather than the full pbp file.
        """
        nfl = _import_nfl()
        columns = [
            "season", "week", "yardline_100", "rush_attempt", "pass_attempt",
            "rusher_player_id", "receiver_player_id",
        ]
        try:
            pbp = nfl.import_pbp_data([season], columns=columns, downcast=True, cache=False)
        except Exception as error:  # noqa: BLE001
            raise ProviderError(f"nflverse: play-by-play for {season} unavailable: {error}") from error

        inside = pbp[pbp["yardline_100"] <= 20]
        touches: dict[str, float] = {}
        weeks: dict[str, set[int]] = {}

        for id_column, play_column in (("rusher_player_id", "rush_attempt"), ("receiver_player_id", "pass_attempt")):
            subset = inside[(inside[play_column] == 1) & inside[id_column].notna()]
            for player_id, week in zip(subset[id_column], subset["week"], strict=False):
                key = str(player_id)
                touches[key] = touches.get(key, 0.0) + 1.0
                weeks.setdefault(key, set()).add(int(week))

        return {key: round(count / max(len(weeks[key]), 1), 3) for key, count in touches.items()}

    # ----------------------------------------------------------------- usage

    def usage_for(self, ref: PlayerRef, season: int, through_week: int) -> UsageStats:
        gsis = self.gsis_id_for(ref)
        if not gsis:
            raise ProviderError(f"nflverse: no gsis id for {ref.name}")

        weekly = self._weekly(season)
        rows = weekly[(weekly["player_id"] == gsis) & (weekly["week"] < through_week)]
        if len(rows) == 0:
            raise ProviderError(f"nflverse: no {season} weekly rows for {ref.name}")

        recent = rows.nlargest(RECENT_WINDOW, "week") if "week" in rows else rows

        snap_pct = recent_snap_pct = None
        try:
            snaps = self._snaps(season)
            pfr_id = (self._id_map().get(ref.sleeper_id or "") or {}).get("pfr_id")
            if pfr_id and "pfr_player_id" in snaps:
                snap_rows = snaps[(snaps["pfr_player_id"] == pfr_id) & (snaps["week"] < through_week)]
            else:
                snap_rows = snaps[(snaps["player"] == ref.name) & (snaps["week"] < through_week)]
            if len(snap_rows):
                snap_pct = _mean(snap_rows["offense_pct"])
                recent_snap_pct = _mean(snap_rows.nlargest(RECENT_WINDOW, "week")["offense_pct"])
        except ProviderError:
            logger.info("nflverse: snap counts unavailable, continuing without them")

        rz = None
        if self.include_redzone:
            try:
                rz = self._redzone(season).get(gsis)
            except ProviderError:
                logger.info("nflverse: red-zone data unavailable, continuing without it")

        games = int(len(rows))
        return UsageStats(
            games_sampled=games,
            snap_pct=snap_pct,
            target_share=_mean(rows.get("target_share")),
            air_yards_share=_mean(rows.get("air_yards_share")),
            wopr=_mean(rows.get("wopr")),
            targets_per_game=_mean(rows.get("targets")),
            carries_per_game=_mean(rows.get("carries")),
            rz_touches_per_game=rz,
            ppr_points_per_game=_mean(rows.get("fantasy_points_ppr")),
            recent_snap_pct=recent_snap_pct,
            recent_target_share=_mean(recent.get("target_share")),
            recent_ppr_points_per_game=_mean(recent.get("fantasy_points_ppr")),
        )
