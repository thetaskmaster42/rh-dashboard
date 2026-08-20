# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`rh-dashboard` reads a folder of Robinhood statement CSVs, dedupes them,
categorizes every row into one of eleven buckets, FIFO-matches trades to
separate realized P&L from open holdings, and writes a self-contained HTML
dashboard.

Extracted from `thetaskmaster42/trading`, where it lived alongside two
unrelated options-research projects. Nothing here depends on them any more.

## Environment and tooling

**Standard library only.** No dependencies, no virtualenv, no `pip install`.
There is also no packaging, lint, format, or CI configuration — no
`pyproject.toml`, `setup.py`, `Makefile`, or `.github/workflows`. Don't go
looking: `./rh-dashboard selftest` is the entire quality gate. The package is
imported off `sys.path` by the repo-relative `rh-dashboard` executable, never
installed.

The CLI resolves its default input/output paths from `__file__`, not the
working directory, so it behaves the same whichever folder you invoke it from.

## Commands

```bash
./rh-dashboard build                                 # reads input/*.csv, writes output/dashboard.html
./rh-dashboard build -i sample_data -o /tmp/preview  # try it on the bundled fictional fixtures first
./rh-dashboard build --filename jan.html             # rename the output file
./rh-dashboard selftest                              # 190 assertions across 8 groups
```

`selftest.py` *is* the test suite — there is no pytest. It cannot run a subset
by name; it always runs all 8 groups.

`input/*.csv` and `output/*.html` are gitignored: run output here is real
account data and is deliberately **not** committed. Only `sample_data/`
(fictional, hand-built) is tracked, and it is what `selftest` and the README
quick start both run against. Develop against `sample_data`; never paste real
statement rows into tests, fixtures, or commit messages.

### What to run after a change

| You touched | Run |
|---|---|
| `rh_dashboard/**` | `./rh-dashboard selftest` |
| `sample_data/*.csv` | re-derive the expected constants in `selftest.py` **by hand**, then `./rh-dashboard selftest` |

## Architecture

**The one thing to understand before changing anything**: a Robinhood
statement is a *cash ledger*, but the dashboard reports an *income statement*.
Buying a stock is not an expense — it converts cash into an asset of equal
value. Summing raw Buy/Sell cash per category (which this project did
originally, and which was wrong) makes any account holding open positions look
like it's losing money. So Equity and Options reach Net Income as **FIFO-matched
realized P&L on closed lots only**, and what's still open is reported separately
as cost basis. Don't "simplify" that back into a cash sum.

**There is deliberately no mark-to-market.** A statement export carries no live
price, so open positions are shown at cost and unrealized P&L is simply not
knowable from this input. Don't add an estimate, and don't wire a quote feed in
— the project's guarantee is that the output HTML makes no network calls and
opens offline.

Linear pipeline: `loader → dedupe → categorize → positions → metrics →
dashboard`. **Order matters**: positions must be matched before metrics, since
metrics asks `positions.py` for the realized figures.

1. **`loader.py`** finds every `*.csv` in the input folder and parses rows into
   `Transaction`s. Header matching is alias-tolerant (`HEADER_ALIASES`); only
   Activity Date/Trans Code/Amount are required. A bad row or a file missing a
   required column is skipped and reported, never fatal to the run.
2. **`dedupe.py`** collapses exact-duplicate rows (same everything except
   source file) — Robinhood's monthly exports overlap in date range, so the
   same row appears in two files verbatim.
3. **`categorize.py`** sorts each transaction into one of eleven categories by
   trans code and description pattern (rules table in `README.md`). The rule
   set was checked against a real 556-row export; the non-obvious ones
   (`MINT` = margin interest, `CIL` = cash in lieu, `DFEE` = ADR fee,
   `MRGS`/`SXCH`/`SPR` = corporate actions) came from that and are easy to
   re-break by "tidying". Anything that doesn't match an explicit rule falls
   back to a sign-based Debits/Credits split and is flagged
   (`Classified.fallback`) rather than silently trusted.
   `model.py` holds the groupings that decide how each category aggregates:
   `INCOME_CATEGORIES`, `TRANSFER_CATEGORIES` (financing, excluded from
   income), `LOT_MATCHED_CATEGORIES` (Equity/Options), `FALLBACK_CATEGORIES`,
   and `CATEGORY_ORDER` — the one fixed iteration order every
   table/chart/legend uses so a category's colour slot never shifts.
4. **`positions.py`** FIFO-matches every opening trade against its closing
   trades. One engine covers equity and options because both reduce to
   `realized = (closing cash/unit + opening cash/unit) × units matched` when
   you work in signed cash-per-unit (`amount / quantity`) rather than prices —
   which also means no options multiplier is needed, since Amount already
   includes it. Option positions key on the contract *Description* (two AAPL
   calls at different strikes must not match); equity keys on ticker. Closing
   more than the statements account for is counted at full proceeds and
   **warned about**, never silently guessed.
5. **`metrics.py`** builds the income statement: per-category `cash_total`
   (raw) *and* `income_total` (realized, for lot-matched categories), monthly
   cumulative series driven by realized events rather than raw cash, and
   `net_income`. It also computes `reconciliation_error` and asserts the
   identity `net income − open position cash + transfers ≡ total cash
   movement`. **If you change how any category aggregates, that identity is
   the check that catches a double-count** — the dashboard renders a loud
   warning when it fails.
6. **`render.py`** draws the line/bar charts as hand-rolled inline SVG —
   no charting library. They are embedded in the page rather than linked so
   marks can use `var(--series-N)` and repaint for dark mode. It's fully
   generic; category→colour resolution lives in `dashboard.py`
   (`category_color`/`display_key`), which folds the Debits/Credits fallback
   pair into one muted "Other" swatch everywhere, since the eight real
   categories already spend the categorical palette's validated 8-slot ceiling.
   **`dashboard.py`** assembles the page: Summary (net income + the two-column
   calculation/reconciliation tables), the open-positions table, stat tiles,
   legend/filter chips, transaction table, and the one vanilla-JS block driving
   hover tooltips — no external script, style, or font, so the file works
   fully offline.
7. **`pipeline.build_dashboard`** is the public API tying the above together;
   **`cli.py`** is the argparse wrapper; `rh-dashboard` (repo-relative
   executable) puts its own directory on `sys.path` and calls `cli.main`.

`selftest.py` group 3 unit-tests the FIFO engine (open-only, partial sale, FIFO
ordering across lots, short options, expiry, oversell) and group 7 asserts the
reconciliation invariants. `sample_data/` is built specifically to exercise the
open/partial/closed cases — TSLA never sold, AAPL half sold, SPY fully closed —
so those numbers are load-bearing; changing the CSVs means re-deriving the
expected constants in `selftest.py` by hand.

## Convention

**Skip loudly, never substitute.** When the input doesn't support an answer,
emit a reason the dashboard prints — `Classified.fallback`, the oversell
warning, the reconciliation banner — instead of filling the gap with a
plausible number.
