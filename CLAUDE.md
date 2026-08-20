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

**Standard library only.** No dependencies, no virtualenv, no `pip install` —
including the HTTP server, which is `http.server` rather than Flask precisely
to keep this true. There is also no packaging, lint, format, or CI
configuration — no `pyproject.toml`, `setup.py`, `Makefile`, or
`.github/workflows`. Don't go looking: `./rh-dashboard selftest` is the entire
quality gate. The package is imported off `sys.path` by the repo-relative
`rh-dashboard` executable, never installed.

The `Dockerfile` and `chart/` exist for a homelab deployment where statements
live on a PVC. The image has no pip layer for the same reason.

The CLI resolves its default input/output paths from `__file__`, not the
working directory, so it behaves the same whichever folder you invoke it from.

## Commands

```bash
./rh-dashboard build                                 # reads input/*.csv, writes output/dashboard.html
./rh-dashboard build -i sample_data -o /tmp/preview  # try it on the bundled fictional fixtures first
./rh-dashboard build --filename jan.html             # rename the output file
./rh-dashboard serve -i sample_data -o /tmp/serve    # same page, served, with upload
./rh-dashboard selftest                              # 235 assertions across 9 groups
```

`selftest.py` *is* the test suite — there is no pytest. It cannot run a subset
by name; it always runs all 9 groups. Group 9 binds a real socket on port 0 and
drives the handler over HTTP, so it needs no network but does need loopback.

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
| `dashboard.py` | `./rh-dashboard selftest`, and confirm `build` output didn't move: diff a fresh `build -i sample_data` against the previous one, ignoring the `Generated` timestamp |
| `chart/**` | `helm lint ./chart && helm template rh ./chart` |

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
   **An empty Amount is meaningful, not malformed** — expirations, assignments,
   mergers and splits move shares without moving cash, so those rows parse as
   `$0.00`. Skipping them dropped 45 rows of a real export. Statement rows
   arrive newest-first and Quantity may carry a trailing `S`; both matter
   downstream.
2. **`dedupe.py`** is **multiplicity-preserving**, not collapse-to-one. The
   same row appears verbatim in two overlapping monthly exports (a duplicate),
   but a row repeated *within one file* is two real identical transactions
   (not a duplicate). So it counts occurrences per source file and keeps the
   **maximum** across files. Collapsing to one deleted a real 100-share
   purchase and left an option contract open forever — don't "simplify" this
   into a `set()`.
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
   Two ordering/semantic rules live here and are easy to break:
   - **Opens are processed before closes on the same date** (`_phase`).
     Statements carry a date but no time and export newest-first, so raw file
     order lists a same-day sell above the buy that funded it — which produced
     a phantom short plus a still-open position on same-day round trips.
   - **Corporate actions (`MRGS`/`SXCH`/`SPR`) are basis-preserving transfers,
     not taxable events.** They come as same-day pairs whose only direction
     signal is the trailing `S` on Quantity (`Transaction.shares_removed`,
     also part of `dedupe_key()`), since both legs have an empty Amount. The
     surrendered lots' cost basis carries onto the shares received and the
     exchange realizes nothing. Before this was handled, every SPAC and ticker
     change stayed "open" forever because nothing ever closed the position.
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
8. **`server.py`** is an optional front end, not part of the pipeline: an
   `http.server` handler serving `GET /` plus a small JSON API for uploading
   and deleting CSVs on the volume. Two invariants worth keeping:
   **`build_page(interactive=False)` output must stay byte-identical** — the
   upload chrome lives in `INTERACTIVE_CSS`/`INTERACTIVE_JS`/`_header_actions`/
   `_files_dialog`, injected only when the server asks, so a dashboard built on
   the CLI never carries a button that posts nowhere. And **an upload is
   validated by running `loader.load_file` over it**, not by sniffing its name
   or header — same "skip loudly" rule as everywhere else, with the parser's
   own error text returned to the user. Uploads are safe to repeat because
   `dedupe.py` keeps the per-file maximum multiplicity.

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
