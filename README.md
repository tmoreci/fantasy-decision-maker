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
| `cli.py` | Typer + Rich interface |

## Caveats

- Jev cannot hallucinate a *value outside your schema*, but it can absolutely be
  wrong about football. Type-safety is not correctness.
- The ESPN scoreboard endpoint is undocumented and can change shape without notice.
  Providers degrade rather than crash, but a silent shape change shows up as a
  missing signal — `--verbose` will say so.
- Nothing here knows about your opponent's lineup. It optimises the single slot you
  asked about.
