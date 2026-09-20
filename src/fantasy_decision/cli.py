"""Command line interface."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from fantasy_decision.cache import DEFAULT_CACHE_DIR, DiskCache
from fantasy_decision.calibrate import (
    BacktestRecord,
    calibration_report,
    record_from_decision,
    resolve_with_actuals,
)
from fantasy_decision.decide import DecisionError, decide
from fantasy_decision.dossier import gather_dossiers
from fantasy_decision.explain import explain
from fantasy_decision.models import Decision, LeagueSettings, PlayerRef
from fantasy_decision.providers.base import ProviderError
from fantasy_decision.providers.espn import EspnProvider
from fantasy_decision.providers.nflverse import NflverseProvider
from fantasy_decision.providers.sleeper import PRESETS, SleeperProvider

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Decide who to start in your fantasy football matchup, using TypeSafe AI's Jev model.",
)
console = Console()

HISTORY_PATH = DEFAULT_CACHE_DIR / "decisions.jsonl"
BAR_WIDTH = 28


# --------------------------------------------------------------------- rendering


def _bar(fraction: float, width: int = BAR_WIDTH) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return "█" * filled + "·" * (width - filled)


def render(decision: Decision) -> None:
    explanation = explain(decision)
    verdict = explanation.verdict

    colour = {"clear": "bold green", "lean": "bold yellow", "coin-flip": "bold magenta"}[verdict.strength]
    console.print()
    # The detail goes below the panel rather than in its subtitle: an expand=False
    # panel sizes itself to the headline and would truncate a full sentence.
    console.print(Panel(Text(verdict.headline, style=colour), expand=False))
    console.print(f"[dim]{verdict.detail}[/dim]\n")

    # Every candidate's probability, not just the winner's: with three options a
    # 40% pick is nearly a toss-up, and showing only the winner hides that.
    table = Table(title="P(this is the right start)", show_header=True, header_style="dim")
    table.add_column("Player")
    table.add_column("", justify="left")
    table.add_column("", justify="right")
    for slug, probability in sorted(decision.start.probabilities.items(), key=lambda kv: -kv[1]):
        is_pick = slug == decision.start.pick
        style = "bold green" if is_pick else "dim"
        table.add_row(
            Text(decision.name_for(slug), style=style),
            Text(_bar(probability), style=style),
            Text(f"{probability:.0%}", style=style),
        )
    console.print(table)

    if explanation.differentiators:
        console.print("\n[bold]Why[/bold]")
        for line in explanation.differentiators:
            console.print(f"  • {line}")

    if explanation.factors:
        factor_table = Table(title="\nFactor breakdown", show_header=True, header_style="dim")
        factor_table.add_column("Factor")
        for slug in decision.start.probabilities:
            factor_table.add_column(decision.name_for(slug))
        for factor in explanation.factors:
            cells = []
            for slug in decision.start.probabilities:
                value = factor.values.get(slug, "-")
                cells.append(Text(value, style="green" if factor.edge == slug else ""))
            factor_table.add_row(factor.label, *cells)
        console.print(factor_table)

    if decision.safest or decision.highest_upside:
        console.print("\n[bold]If your matchup needs something specific[/bold]")
        if decision.safest:
            console.print(
                f"  Safest floor:   [cyan]{decision.name_for(decision.safest.pick)}[/cyan] "
                f"({decision.safest.pick_probability:.0%})"
            )
        if decision.highest_upside:
            console.print(
                f"  Highest ceiling: [cyan]{decision.name_for(decision.highest_upside.pick)}[/cyan] "
                f"({decision.highest_upside.pick_probability:.0%})"
            )

    if explanation.warnings:
        console.print("\n[bold yellow]Worth knowing[/bold yellow]")
        for warning in explanation.warnings:
            console.print(f"  [yellow]![/yellow] {warning}")

    footer = f"{decision.model} · {decision.latency_ms:.0f}ms" if decision.latency_ms else decision.model
    if decision.input_tokens:
        footer += f" · {decision.input_tokens:,} input tokens (~${decision.input_tokens * 0.042 / 1_000_000:.5f})"
    console.print(f"\n[dim]{footer}[/dim]\n")


def _append_history(decision: Decision) -> None:
    """Log every call so `calibrate` has real history to score later."""
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = record_from_decision(decision)
        names = {slug: d.ref.name for slug, d in decision.dossiers.items()}
        gsis = {slug: d.ref.gsis_id for slug, d in decision.dossiers.items() if d.ref.gsis_id}
        payload = record.model_dump(mode="json") | {"names": names, "gsis_ids": gsis}
        with HISTORY_PATH.open("a") as handle:
            handle.write(json.dumps(payload) + "\n")
    except OSError as error:
        logging.getLogger("fantasy_decision.cli").debug("could not write history: %s", error)


# ------------------------------------------------------------------------ decide


@app.command("decide")
def decide_command(  # noqa: PLR0913
    players: Annotated[list[str], typer.Argument(help="Two or three player names to choose between.")],
    week: Annotated[Optional[int], typer.Option("--week", "-w", help="NFL week. Defaults to the current week.")] = None,
    season: Annotated[Optional[str], typer.Option("--season", help="Season year. Defaults to the current season.")] = None,
    need: Annotated[str, typer.Option("--need", help="auto, safe (protect a lead) or upside (chase a win).")] = "auto",
    league_id: Annotated[Optional[str], typer.Option("--league", "-l", help="Sleeper league id, to use its real scoring settings.")] = None,
    scoring: Annotated[str, typer.Option("--scoring", help="Scoring preset when no league is given: ppr, half or standard.")] = "ppr",
    advanced: Annotated[bool, typer.Option("--advanced/--no-advanced", help="Include nflverse usage metrics.")] = True,
    use_cache: Annotated[bool, typer.Option("--cache/--no-cache", help="Use the on-disk provider cache.")] = True,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the full decision as JSON instead of a table.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log provider activity.")] = False,
) -> None:
    """Pick who to start from two or three options."""
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(message)s")

    if not 2 <= len(players) <= 3:
        console.print("[red]Give me two or three players to choose between.[/red]")
        raise typer.Exit(2)

    if need not in {"auto", "safe", "upside"}:
        console.print("[red]--need must be one of: auto, safe, upside[/red]")
        raise typer.Exit(2)

    if not os.environ.get("TYPESAFE_API_KEY"):
        console.print("[red]TYPESAFE_API_KEY is not set.[/red] Get a key at https://typesafe.ai and export it.")
        raise typer.Exit(1)

    cache = DiskCache(enabled=use_cache)
    sleeper = SleeperProvider(cache)
    espn = EspnProvider(cache)

    try:
        state = sleeper.current_state()
        resolved_week = week or int(state.get("week") or 1)
        resolved_season = season or str(state.get("season") or "")

        league = _resolve_league(sleeper, league_id, scoring)

        refs: list[PlayerRef] = []
        for name in players:
            try:
                refs.append(sleeper.resolve_player(name))
            except ProviderError as error:
                console.print(f"[red]{error}[/red]")
                raise typer.Exit(2) from error

        if len({ref.slug for ref in refs}) != len(refs):
            console.print("[red]Those resolve to the same player. Give me distinct options.[/red]")
            raise typer.Exit(2)

        nflverse = NflverseProvider() if advanced else None

        with console.status("Gathering data..."):
            dossiers = gather_dossiers(
                refs,
                season=resolved_season,
                week=resolved_week,
                sleeper=sleeper,
                espn=espn,
                nflverse=nflverse,
            )

        from typesafe_sdk import TypeSafeClient  # noqa: PLC0415 - keeps --help fast

        with console.status("Asking Jev..."), TypeSafeClient() as client:
            decision = decide(
                dossiers,
                league,
                season=resolved_season,
                week=resolved_week,
                client=client,
                need=need,
            )
    except DecisionError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(1) from error
    except ProviderError as error:
        console.print(f"[red]Could not gather enough data: {error}[/red]")
        raise typer.Exit(1) from error
    finally:
        sleeper.close()
        espn.close()

    _append_history(decision)

    if as_json:
        console.print_json(json.dumps(decision.as_json()))
    else:
        render(decision)


def _resolve_league(sleeper: SleeperProvider, league_id: str | None, scoring: str) -> LeagueSettings:
    league_id = league_id or os.environ.get("SLEEPER_LEAGUE_ID")
    if league_id:
        try:
            return sleeper.league(league_id)
        except ProviderError as error:
            console.print(f"[yellow]Could not read league {league_id} ({error}); falling back to --scoring.[/yellow]")

    preset = PRESETS.get(scoring.lower())
    if preset is None:
        console.print(f"[red]Unknown scoring preset {scoring!r}. Use ppr, half or standard.[/red]")
        raise typer.Exit(2)
    return LeagueSettings(name=f"Manual ({preset.name})", scoring=preset, source="manual")


# ------------------------------------------------------------------ sleeper help


@app.command()
def leagues(
    username: Annotated[Optional[str], typer.Argument(help="Your Sleeper username.")] = None,
    season: Annotated[Optional[str], typer.Option("--season")] = None,
) -> None:
    """List your Sleeper leagues, so you can grab a league id."""
    username = username or os.environ.get("SLEEPER_USERNAME")
    if not username:
        console.print("[red]Give me a Sleeper username (or set SLEEPER_USERNAME).[/red]")
        raise typer.Exit(2)

    sleeper = SleeperProvider()
    try:
        resolved_season = season or str(sleeper.current_state().get("season") or "")
        user_id = sleeper.user_id(username)
        found = sleeper.leagues_for_user(user_id, resolved_season)
    except ProviderError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(1) from error
    finally:
        sleeper.close()

    if not found:
        console.print(f"[yellow]No {resolved_season} leagues for {username}.[/yellow]")
        return

    table = Table(title=f"{username} - {resolved_season}")
    table.add_column("League")
    table.add_column("League id")
    table.add_column("Teams", justify="right")
    for league in found:
        table.add_row(str(league.get("name", "?")), str(league.get("league_id", "?")), str(league.get("total_rosters", "?")))
    console.print(table)


@app.command()
def roster(
    username: Annotated[Optional[str], typer.Argument(help="Your Sleeper username.")] = None,
    league_id: Annotated[Optional[str], typer.Option("--league", "-l")] = None,
    position: Annotated[Optional[str], typer.Option("--position", "-p", help="Filter to QB, RB, WR, TE, K or DEF.")] = None,
) -> None:
    """Show your roster, with injury designations, so you know what to compare."""
    username = username or os.environ.get("SLEEPER_USERNAME")
    league_id = league_id or os.environ.get("SLEEPER_LEAGUE_ID")
    if not username or not league_id:
        console.print("[red]Need both a username and a --league id (or SLEEPER_USERNAME / SLEEPER_LEAGUE_ID).[/red]")
        raise typer.Exit(2)

    sleeper = SleeperProvider()
    try:
        user_id = sleeper.user_id(username)
        refs = sleeper.bench_candidates(league_id, user_id, position)
        table = Table(title=f"{username}'s roster")
        table.add_column("Player")
        table.add_column("Pos")
        table.add_column("Team")
        table.add_column("Status")
        for ref in sorted(refs, key=lambda r: (r.position or "", r.name)):
            injury = sleeper.injury_for(ref)
            table.add_row(ref.name, ref.position or "-", ref.team or "FA", injury.status or "")
        console.print(table)
    except ProviderError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(1) from error
    finally:
        sleeper.close()


# --------------------------------------------------------------------- calibrate


@app.command()
def calibrate(
    history: Annotated[Path, typer.Option("--history", help="Decision log to score.")] = HISTORY_PATH,
    bins: Annotated[int, typer.Option("--bins", help="Reliability curve buckets.")] = 5,
    resolve: Annotated[bool, typer.Option("--resolve/--no-resolve", help="Look up actual fantasy points via nflverse.")] = True,
) -> None:
    """Score past recommendations against what actually happened.

    This is how you find out whether the confidence percentages mean anything, and
    where to set the "too close to call" threshold for your own league.
    """
    if not history.exists():
        console.print(f"[yellow]No decision history at {history} yet. Run a few decisions first.[/yellow]")
        raise typer.Exit(1)

    raw = [json.loads(line) for line in history.read_text().splitlines() if line.strip()]
    records: list[BacktestRecord] = []

    for entry in raw:
        gsis_ids = entry.pop("gsis_ids", {}) or {}
        entry.pop("names", None)
        record = BacktestRecord.model_validate(entry)
        if resolve and not record.resolved and gsis_ids:
            try:
                from fantasy_decision.calibrate import actual_points  # noqa: PLC0415

                points = actual_points(int(record.season), record.week, gsis_ids)
                record = resolve_with_actuals(record, points)
            except Exception as error:  # noqa: BLE001 - scoring is best effort
                console.print(f"[dim]Could not resolve week {record.week}: {error}[/dim]")
        records.append(record)

    resolved_records = [r for r in records if r.resolved]
    if not resolved_records:
        console.print("[yellow]Nothing scoreable yet - games may not have been played.[/yellow]")
        raise typer.Exit(1)

    report = calibration_report(resolved_records, bins)
    console.print(Panel(report.summary(), title="Calibration"))

    table = Table(title="Reliability curve")
    table.add_column("Claimed")
    table.add_column("Observed")
    table.add_column("n", justify="right")
    table.add_column("Gap", justify="right")
    for entry_bin in report.bins:
        gap = entry_bin.gap
        style = "green" if abs(gap) < 0.1 else ("yellow" if abs(gap) < 0.2 else "red")
        table.add_row(
            f"{entry_bin.lower:.0%}-{entry_bin.upper:.0%}",
            f"{entry_bin.observed_accuracy:.0%}",
            str(entry_bin.count),
            Text(f"{gap:+.0%}", style=style),
        )
    console.print(table)

    if report.mean_points_left_on_bench is not None:
        console.print(f"\nAverage points left on the bench per decision: [bold]{report.mean_points_left_on_bench}[/bold]")


@app.command("clear-cache")
def clear_cache() -> None:
    """Delete cached provider responses."""
    removed = DiskCache().clear()
    console.print(f"Removed {removed} cached responses from {DEFAULT_CACHE_DIR}.")


# Make `start-who "A" "B"` work without typing the subcommand name.
def main() -> None:
    known = {"decide", "leagues", "roster", "calibrate", "clear-cache", "--help", "-h"}
    if len(sys.argv) > 1 and sys.argv[1] not in known and not sys.argv[1].startswith("-"):
        sys.argv.insert(1, "decide")
    app()


if __name__ == "__main__":
    main()
