"""Domain models.

Everything the app knows about a start/sit decision lives here as typed Pydantic
models. The rule that shapes this module: *code calculates, Jev judges*. Derived
numbers (implied team totals, usage shares, trend deltas) are computed here and in
the providers, so the model only ever has to make the semantic call.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

Position = Literal["QB", "RB", "WR", "TE", "K", "DEF"]
HomeAway = Literal["home", "away"]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def slugify(name: str) -> str:
    """Stable, Jev-friendly label for a player.

    Choice criteria are keyed by string labels and those labels come back verbatim
    in `probabilities`, so they need to be readable but also safe to use as dict keys.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "unknown"


class PlayerRef(Frozen):
    """Identity of a candidate player, resolved across data sources."""

    name: str
    position: Position | None = None
    team: str | None = None
    sleeper_id: str | None = None
    gsis_id: str | None = None

    @computed_field
    @property
    def slug(self) -> str:
        return slugify(self.name)

    def label(self) -> str:
        bits = [self.name]
        if self.position and self.team:
            bits.append(f"({self.position} - {self.team})")
        elif self.position:
            bits.append(f"({self.position})")
        return " ".join(bits)


class InjuryInfo(Frozen):
    """Availability signals. `status` is Sleeper's designation, verbatim."""

    status: str | None = None  # Questionable / Doubtful / Out / IR / Sus / None
    body_part: str | None = None
    notes: str | None = None
    depth_chart_position: str | None = None
    depth_chart_order: int | None = None

    @computed_field
    @property
    def is_starter_on_depth_chart(self) -> bool | None:
        if self.depth_chart_order is None:
            return None
        return self.depth_chart_order <= 1


class GameContext(Frozen):
    """The game the player is in, plus the market's view of it.

    `implied_team_total` is the single most useful derived number here: it is the
    number of points Vegas expects this player's offense to score, and it is
    computed deterministically from the spread and the over/under.
    """

    opponent: str | None = None
    home_away: HomeAway | None = None
    kickoff: str | None = None
    spread: float | None = None  # relative to THIS player's team; negative = favored
    over_under: float | None = None
    implied_team_total: float | None = None
    implied_opponent_total: float | None = None
    venue_indoor: bool | None = None
    on_bye: bool = False

    @computed_field
    @property
    def is_favorite(self) -> bool | None:
        if self.spread is None:
            return None
        return self.spread < 0

    @computed_field
    @property
    def projected_margin(self) -> float | None:
        """Positive means this team is projected to win by this much."""
        if self.spread is None:
            return None
        return -self.spread


class UsageStats(Frozen):
    """Opportunity metrics from nflverse. These predict fantasy output far better
    than past fantasy points do, which is why they get their own model."""

    games_sampled: int = 0
    snap_pct: float | None = None
    target_share: float | None = None
    air_yards_share: float | None = None
    wopr: float | None = None  # weighted opportunity rating
    targets_per_game: float | None = None
    carries_per_game: float | None = None
    rz_touches_per_game: float | None = None
    ppr_points_per_game: float | None = None

    # Recent form: same metrics over the last 3 games, for trend detection.
    recent_snap_pct: float | None = None
    recent_target_share: float | None = None
    recent_ppr_points_per_game: float | None = None

    @computed_field
    @property
    def snap_trend(self) -> float | None:
        """Positive means the player's role is growing."""
        if self.snap_pct is None or self.recent_snap_pct is None:
            return None
        return round(self.recent_snap_pct - self.snap_pct, 4)

    @computed_field
    @property
    def target_share_trend(self) -> float | None:
        if self.target_share is None or self.recent_target_share is None:
            return None
        return round(self.recent_target_share - self.target_share, 4)


class TrendingSignal(Frozen):
    """Sleeper's add/drop velocity, used as a crowd proxy for breaking news.

    A player spiking in adds on a Sunday morning usually means a beat reporter
    said something we do not otherwise have in state.
    """

    adds_24h: int | None = None
    drops_24h: int | None = None
    add_rank: int | None = None


class PlayerDossier(BaseModel):
    """Everything we gathered about one candidate.

    `missing` is deliberately part of the payload sent to Jev. Silently omitting a
    field would let a calibrated model assume the absent signal was neutral; naming
    the gap lets it widen its uncertainty instead.
    """

    model_config = ConfigDict(extra="forbid")

    ref: PlayerRef
    injury: InjuryInfo = Field(default_factory=InjuryInfo)
    game: GameContext = Field(default_factory=GameContext)
    usage: UsageStats = Field(default_factory=UsageStats)
    trending: TrendingSignal = Field(default_factory=TrendingSignal)
    missing: list[str] = Field(default_factory=list)

    @property
    def slug(self) -> str:
        return self.ref.slug


class ScoringSettings(Frozen):
    """Scoring rules that materially change a start/sit call."""

    name: str = "PPR"
    reception: float = 1.0
    te_reception_bonus: float = 0.0
    pass_td: float = 4.0
    rush_rec_td: float = 6.0
    pass_yard: float = 0.04
    rush_rec_yard: float = 0.1
    interception: float = -2.0
    fumble_lost: float = -2.0
    bonus_notes: list[str] = Field(default_factory=list)


class LeagueSettings(Frozen):
    name: str = "Manual entry"
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)
    roster_positions: list[str] = Field(default_factory=list)
    source: Literal["sleeper", "manual"] = "manual"


class SubJudgment(Frozen):
    """One per-player judgment from Jev's first pass, kept with its rubric so the
    explanation layer can render it without inventing language."""

    name: str
    kind: Literal["score", "noul"]
    value: float
    confidence: float | None = None
    legend: dict[int, str] = Field(default_factory=dict)
    max_score: int | None = None

    @computed_field
    @property
    def nearest_level(self) -> str | None:
        """The rubric description closest to the expected score."""
        if self.kind != "score" or not self.legend:
            return None
        nearest = min(self.legend, key=lambda level: abs(level - self.value))
        return self.legend[nearest]


class PlayerAnalysis(Frozen):
    """Pass-one output for a single player."""

    slug: str
    judgments: dict[str, SubJudgment] = Field(default_factory=dict)

    def get(self, name: str) -> SubJudgment | None:
        return self.judgments.get(name)


class Recommendation(Frozen):
    """One Choice answer, normalised for display."""

    pick: str
    confidence: float
    probabilities: dict[str, float]

    @computed_field
    @property
    def pick_probability(self) -> float:
        return self.probabilities.get(self.pick, 0.0)

    @computed_field
    @property
    def margin(self) -> float:
        """Gap between the top option and the runner-up."""
        ranked = sorted(self.probabilities.values(), reverse=True)
        if len(ranked) < 2:
            return ranked[0] if ranked else 0.0
        return round(ranked[0] - ranked[1], 4)


class Decision(BaseModel):
    """The full result of a start/sit query."""

    model_config = ConfigDict(extra="forbid")

    week: int
    season: str
    league: LeagueSettings
    dossiers: dict[str, PlayerDossier]
    analyses: dict[str, PlayerAnalysis]
    start: Recommendation
    safest: Recommendation | None = None
    highest_upside: Recommendation | None = None
    coin_flip_probability: float | None = None
    model: str = "jev-latest"
    input_tokens: int | None = None
    latency_ms: float | None = None

    def dossier_for(self, slug: str) -> PlayerDossier | None:
        return self.dossiers.get(slug)

    def name_for(self, slug: str) -> str:
        dossier = self.dossiers.get(slug)
        return dossier.ref.name if dossier else slug

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
