# fantasy-decision-maker

Decide who to start in your fantasy football matchup, using [TypeSafe AI's](https://typesafe.ai) **Jev** model.

Give it two or three players. It gathers everything it can about them, asks Jev,
and returns one pick with a calibrated probability.

```
$ start-who "Bijan Robinson" "Breece Hall" "Kyren Williams"

╭──────────────────────╮
│ Start Bijan Robinson │
╰──────────────────────╯
 61% of the probability mass lands on Bijan Robinson, well clear of the 33% you
 would get from a coin flip.

              P(this is the right start)
┌──────────────────┬──────────────────────────────┬─────┐
│ Bijan Robinson   │ █████████████████·········   │ 61% │
│ Breece Hall      │ ███████·····················  │ 24% │
│ Kyren Williams   │ ████························  │ 15% │
└──────────────────┴──────────────────────────────┴─────┘

Why
  • Game script favours Bijan Robinson: 82%
  • Expected workload favours Bijan Robinson: 3.6/4  Workhorse role...
  • Matchup actually favours Breece Hall (3.1/4) - the pick wins on other grounds.
```

## Why Jev rather than an LLM

Jev is a *System One* model: it does not generate text. You hand it JSON state plus
named, typed questions, and it returns typed answers with **calibrated
probabilities** in a single parallel pass (~70–500ms, $0.042/MTok in, output free).

That maps unusually well onto start/sit:

- The thing you want *is* a probability, and Jev's are trained to be honest ones
  (via RLCD — Reinforcement Learning for Calibrated Decisions).
- Questions are evaluated independently against shared state, so asking eighteen
  narrow questions costs one round trip instead of eighteen.
- It cannot return a player who isn't one of your options. Type-safety is structural.

It also cannot explain itself, which shapes the whole design below.

The decomposition is close to free: a three-player decision is ~5k input tokens
across both passes, or about **$0.0002**. A full 17-week season at three decisions
a week costs roughly a cent.

## How it works

```
 Sleeper ─┐
 ESPN ────┼──> providers ──> deterministic code computes the NUMBERS
 nflverse ┘                  (implied team totals, usage shares, trends)
                                        │
                              PASS 1 ─ per-player judgments, all
                              batched into ONE Jev request:
                              workload · matchup · game script ·
                              injury risk · upside · data quality
                                        │
                              fold pass 1 back into state
                                        │
                              PASS 2 ─ the head-to-head Choice,
                              plus safest / highest-upside / coin-flip
                                        │
                              deterministic explanation + warnings
```

Two passes, because questions inside one request cannot see each other's answers.
Putting pass one's output into pass two's `state` is the only way the final call
builds on the analysis — and those sub-judgments double as the explanation, since
Jev can't write one.

### Which number is "the confidence"

A `Choice` answer gives you two different numbers and the obvious one is the wrong one:

| Field | Meaning | How this app uses it |
|---|---|---|
| `probabilities[pick]` | **P(this is the right start)** | The headline percentage |
| `confidence` | How sure Jev is about its own selection | A review gate, not a headline |

The CLI always shows **every** candidate's probability. With three options a coin
flip is 33%, so a 41% winner is nearly a toss-up — reporting it alone would be
misleading.

## Data sources

| Source | Signal | Key needed |
|---|---|---|
| **Sleeper** | Roster import, league scoring, injury designations, depth chart, trending adds | No |
| **ESPN scoreboard** | Opponent, home/away, spread, over/under → **implied team total** | No |
| **nflverse** (`nfl_data_py`) | Snap share, target share, air yards share, WOPR, red-zone touches, 3-game trend | No |

The governing rule is **code calculates, Jev judges**. Implied team totals, usage
shares and trend deltas are computed in Python; Jev is only ever asked for the
semantic call.

When a source is down, the run continues and the gap is named explicitly in the
state under `unavailable_data`. That's deliberate: silently dropping a field would
let a calibrated model treat the missing signal as neutral, when the honest
response is to widen its uncertainty.

## Install

```bash
uv venv && uv pip install -e ".[advanced,dev]"   # 'advanced' pulls nflverse
export TYPESAFE_API_KEY=ts_...                   # https://typesafe.ai
```

Drop `advanced` for a lighter install; the usage provider then reports itself
unavailable and everything else still works.

## Use

```bash
# The main thing
start-who "Bijan Robinson" "Breece Hall"

# Use your league's real scoring settings
start-who "Puka Nacua" "Malik Nabers" --league 123456789012345678

# Protecting a lead, or chasing one
start-who "Tony Pollard" "Jaylen Warren" --need safe
start-who "Tony Pollard" "Jaylen Warren" --need upside

# Find your league id / see your roster
start-who leagues your-sleeper-name
start-who roster your-sleeper-name --league 1234... --position RB

# Machine-readable
start-who "A" "B" --json
```

Useful flags: `--week`, `--season`, `--scoring ppr|half|standard`, `--no-advanced`
(skip nflverse), `--no-cache`, `--verbose`.

## Web app

A phone-friendly site for people who'd never open a terminal. They type their Sleeper
username, tap two or three players from their roster (or search any player), say how
their matchup looks, and get a verdict card with every candidate's probability.

```bash
uv pip install -e ".[web,advanced]"
export TYPESAFE_API_KEY=ts_...
export APP_PASSCODE=pick-a-passcode   # optional: one shared passcode for your league
start-who-web                         # http://127.0.0.1:8000
```

The API key stays on the server, so your league never needs one. At about $0.0002 per
decision, a whole league's season still costs pennies. `DECISIONS_PER_HOUR` (default 30
per IP) caps how much a leaked link could spend.

**Deploying.** The `Dockerfile` runs on Fly.io, Render or Railway. Set `TYPESAFE_API_KEY`
and `APP_PASSCODE` as secrets, and mount a volume at `/data` so the cache and decision
history (which `start-who calibrate` scores) survive restarts. See `.env.example` for
every setting.

**API-first.** Everything the pages do is also a JSON endpoint (`/api/search`,
`/api/leagues`, `/api/roster`, `/api/decide`), documented at `/docs`. The HTMX pages are
a thin layer over the same functions in `web/views.py`, so a JavaScript front end or a
Discord bot can be built on the API without touching the backend. API clients
authenticate with `Authorization: Bearer <passcode>`.

## Checking whether the confidence means anything

Every decision is logged to `~/.cache/fantasy-decision-maker/decisions.jsonl`.
Once those games have been played:

```bash
start-who calibrate
```

This looks up what each player actually scored, then reports accuracy, a **Brier
score** against the random-pick baseline, and a **reliability curve** — the group
of calls it made at 70% should come in around 70% correct.

```
╭─ Calibration ──────────────────────────────────────────────────────────╮
│ 48 decisions | accuracy 62.5% (baseline 36.8%) | Brier 0.2013          │
│ (baseline 0.2402) | skill +0.162 - better than guessing                 │
╰────────────────────────────────────────────────────────────────────────╯
```

This is not decoration. TypeSafe's own docs are explicit that confidence thresholds
must be validated against your domain rather than guessed, and the "clear / lean /
coin-flip" bands in `explain.py` are exactly such thresholds. Run this before you
trust them.

## Layout

| Module | Role |
|---|---|
| `models.py` | Typed domain objects; derived metrics as computed fields |
| `providers/` | Sleeper, ESPN and nflverse, each failing softly |
| `dossier.py` | Fan-out gathering, plus the `state` payload builder |
| `questions.py` | The Jev question definitions and score rubrics |
| `decide.py` | Two-pass orchestration |
| `explain.py` | Deterministic explanation from the rubrics |
| `calibrate.py` | Brier score, reliability curve, backtesting |
| `service.py` | The pipeline both front ends call: resolve, gather, decide |
| `cli.py` | Typer + Rich interface |
| `web/` | FastAPI app: JSON API (`api.py`), HTMX pages (`pages.py`), shared logic (`views.py`) |

## Caveats

- Jev cannot hallucinate a *value outside your schema*, but it can absolutely be
  wrong about football. Type-safety is not correctness.
- The ESPN scoreboard endpoint is undocumented and can change shape without notice.
  Providers degrade rather than crash, but a silent shape change shows up as a
  missing signal — `--verbose` will say so.
- Nothing here knows about your opponent's lineup. It optimises the single slot you
  asked about.
