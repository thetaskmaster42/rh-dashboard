# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`rh-dashboard` reads a folder of Robinhood statement CSVs, dedupes them,
categorizes every row into one of eleven buckets, lot-matches trades to
separate realized P&L from open holdings, and writes a self-contained HTML
dashboard.

Extracted from `thetaskmaster42/trading`, where it lived alongside two
unrelated options-research projects. Nothing here depends on them any more.

## Environment and tooling

**Standard library plus exactly one dependency.** For most of this project's
life there were none at all, and the rule was enforced by asserting that
`requirements.txt` and `pyproject.toml` did not exist. That changed
deliberately: **DuckDB** is the analytical store for the date-window work, and
dependencies are managed with **uv**.

What did *not* change is the reason the rule existed. The HTTP server is still
`http.server` rather than Flask. There is still no lint or format
configuration, no `Makefile`, and no packaging: `pyproject.toml` sets
`[tool.uv] package = false`, so uv installs the dependencies but never builds
or installs `rh_dashboard` itself, and the repo-relative `rh-dashboard`
executable still imports it off `sys.path`. There is no `[project.scripts]`.
The `version` in `pyproject.toml` is pinned to `0.0.0` and ignored — the real
one lives in `rh_dashboard/__init__.py`.

**Run the suite as `uv run ./rh-dashboard selftest`.** Bare `python3` still
works for `build` and `serve` (neither imports DuckDB yet), but group 10 will
fail without the venv, and it says so rather than raising an ImportError.

The guard is now `.github/scripts/check_imports.py`: it walks every module
under `rh_dashboard/` with `ast` and fails on any import outside
`sys.stdlib_module_names` plus an explicit allowlist. This is **stricter** than
the filename check it replaced — it catches an `import requests` added to a
module, including one hidden inside a function body, which a filename check
could never see. Adding a name to `ALLOWED` is the deliberate act that lets a
new dependency in.

The `Dockerfile` and `chart/` exist for a homelab deployment where statements
live on a PVC. The image copies the uv binary from `ghcr.io/astral-sh/uv` and
runs `uv sync --frozen --no-dev` in its own layer, keyed on the lockfile, so a
source edit does not re-resolve dependencies. `--frozen` everywhere means a
build that would need to change `uv.lock` fails instead of silently upgrading
DuckDB.
`.github/workflows/ci.yml` has four jobs, and each one guards a rule stated
elsewhere in this file:

- **selftest** on 3.11-3.13. Runs `check_imports.py` first, then
  `uv sync --frozen` and `uv run --frozen ./rh-dashboard selftest`.
  Then builds the sample page and greps it: no `<script src=`, no
  `rel="stylesheet"`, and **no `https?://` anywhere in the output HTML at
  all**. That last one is the easy trap — a documentation link, a source URL,
  even one inside an HTML comment, fails the build. Keep URLs out of
  `dashboard.py`'s emitted markup. The favicon is the one asset with a real
  pull to link rather than embed, so group 8 additionally asserts it is a
  `data:image/png;base64,` URI, decodes to a real PNG, and stays under 4 KB.
- **CLI page is byte-stable** (PRs only): rebuilds `sample_data` at the merge
  base and at HEAD and diffs, ignoring the `Generated` timestamp. It *warns*
  rather than fails — read the annotation, don't assume green means unchanged.
  Deliberately runs on bare `python3`, not uv: the merge base may predate the
  dependency, and `build` needs none either way.
- **helm**: `helm lint` plus `helm template` over every conditional path;
  asserts `--set auth.enabled=true` with no credentials **refuses** to render;
  and runs `.github/scripts/check_chart_auth_keys.py` over three rendered
  permutations to prove the Secret the chart writes and the Secret the
  Deployment reads carry the same keys — a mismatch renders valid YAML and
  only fails at pod start as `CreateContainerConfigError`. That script imports
  `yaml`; it is CI-only tooling and is outside the import guard, which scans
  `rh_dashboard/` only.
- **image**: builds the container, runs `python -m rh_dashboard.cli selftest`
  *inside it*, then boots it and drives `/healthz` → `/api/upload` → `/`.

`release.yml` publishes a multi-arch image and the packaged chart to GHCR on a
`v*` tag, never from `main`.

The CLI resolves its default input/output paths from `__file__`, not the
working directory, so it behaves the same whichever folder you invoke it from.

## Commands

```bash
./rh-dashboard build                                 # reads input/*.csv, writes output/dashboard.html
./rh-dashboard build -i sample_data -o /tmp/preview  # try it on the bundled fictional fixtures first
./rh-dashboard build --filename jan.html             # rename the output file
./rh-dashboard serve -i sample_data -o /tmp/serve    # same page, served, with upload
./rh-dashboard build --cost-basis fifo               # match a 1099-B instead of averaging
./rh-dashboard build --from 2026-07-01 --to 2026-07-31   # report one window
uv run ./rh-dashboard selftest                       # 562 assertions across 15 groups
```

`selftest.py` *is* the test suite — there is no pytest. It cannot run a subset
by name; it always runs all 15 groups. Group 9 binds a real socket on port 0 and
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
| `server.py` | `./rh-dashboard selftest` (group 9 drives the handler over real HTTP), and confirm the CLI page did *not* move — a server-only change that shifts `build` output means upload chrome leaked out of the `interactive` guard |
| `positions.py` | `./rh-dashboard selftest`, and check the change under **both** cost bases — `--cost-basis average` and `fifo` — the two report different numbers on `sample_data`, so a change that makes them agree is a bug |
| `chart/**` | `helm lint ./chart && helm template rh ./chart`, plus `helm template rh ./chart --set auth.enabled=true` with **no** credentials, which must *fail*; if you touched the Secret or the Deployment's env, also render with `auth.usernameKey`/`auth.passwordKey` overridden and run `.github/scripts/check_chart_auth_keys.py` over the output |
| `.github/workflows/**` | nothing local runs it; check the run on the PR |

## Architecture

**The one thing to understand before changing anything**: a Robinhood
statement is a *cash ledger*, but the dashboard reports an *income statement*.
Buying a stock is not an expense — it converts cash into an asset of equal
value. Summing raw Buy/Sell cash per category (which this project did
originally, and which was wrong) makes any account holding open positions look
like it's losing money. So Equity and Options reach Net Income as **lot-matched
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
4. **`positions.py`** lot-matches every opening trade against its closing
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
   - **Surrendered basis that never reaches incoming shares is counted, named
     and warned about** — never dropped. `ca_basis_pool` is filled by the
     outgoing leg and drained by the incoming one; a cash merger, a delisting,
     or an incoming leg in a later statement leaves a residual. It is exposed
     as `PositionsResult.unmatched_corporate_action_basis` so the
     reconciliation banner can name its own cause. The banner still fires —
     the basis really is gone and no honest number replaces it — but it says
     why.
   - **The expiry warning is judged against the end of the range being
     *reported*, not the last row on file.** Those are the same thing only
     while every call gets full history. `compute_positions(..., full_history=)`
     tells "no closing row exists anywhere" (a real gap) apart from "the
     closing row is simply dated later" (not a problem at all). Passing
     nothing leaves both the same, which is the unwindowed behaviour.
   - **A window never changes how lots are matched — only what is reported.**
     `compute_windowed(classified, window)` runs the engine twice: once over
     `activity_date <= end` (holdings and basis **as of the window end**, every
     pre-window lot at its real cost) and once over `< start` (the state the
     window opened with). `PositionsResult.windowed(start)` then filters the
     realized events, leaving holdings alone because they are already as-of-end.
     Slicing rows *before* matching is the bug this exists to prevent: on
     `sample_data` a July-only match turns AAPL's real `+1,250` into `+9,500`
     of bare proceeds plus an oversell warning it caused itself. `windowed`
     takes `start` alone on purpose — an `end` argument would let a caller pair
     holdings from one date with income from another. `Window` keys on
     `activity_date`, never process/settle, or a same-day corporate-action pair
     splits across the boundary and the surrendered basis vanishes.
   - **Cost basis is selectable and `CostBasis.AVERAGE` is the default.**
     Average cost blends every open equity lot into one running lot; FIFO
     queues them. The entire difference lives in `_absorb` — under averaging a
     purchase folds into the single held lot, so closes walk the *same* FIFO
     path in both modes and there stays exactly one place a sale is matched
     and one place a cost is set. Shares received from a corporate action
     blend the same way, or a merger silently leaves a second lot behind.
     **Options ignore the setting**: a short-to-open contract has no purchase
     price to average, and two strikes on the same underlying must never
     blend. The two modes agree on lifetime P&L for a fully closed position
     and differ only while one is open — which is exactly what a date window
     exposes, and why the mode is selectable rather than assumed.
5. **`metrics.py`** builds the income statement: per-category `cash_total`
   (raw) *and* `income_total` (realized, for lot-matched categories), monthly
   cumulative series driven by realized events rather than raw cash, and
   `net_income`. `_by_ticker` splits net income per ticker, reading the
   **Instrument** column rather than parsing descriptions — a dividend row
   carries its ticker there and an option row carries its *underlying*, which
   `RealizedEvent.instrument` propagates, so the pattern rules this looked like
   it would need are unnecessary and cannot mis-fire on a real export. An empty
   Instrument is an account-level row (margin interest, account fees, Gold,
   stock lending) and lands in the **Unattributed** bucket, which is named on
   the page rather than spread across tickers, because spreading it would be a
   guess. Lot-matched categories are attributed from realized events, never raw
   cash — attribute the cash and an open position reads as a huge loss. The
   check is `ticker_attribution_error`: **Σ per-ticker + unattributed ≡ net
   income**, the same class of guard as the cash reconciliation.
   It also computes `reconciliation_error` and asserts the
   identity `net income − Δopen equity cost + Δopen options cash + transfers +
   corporate action cash ≡ total cash movement`. It is a **delta** identity:
   cash moved equals income earned, less the change in capital tied up in
   positions, plus financing. `compute(..., window=, opening=)` are keyword-only
   and `opening` is a whole `PositionsResult` so the pair cannot be transposed;
   without a window the opening terms are zero and the formula collapses to the
   original exactly, with no `if window is None` branch in the arithmetic.
   `months` come from the window rather than from the rows, so a quiet first or
   last month cannot shrink the axis, and `_empty` is suppressed under a window
   because "nothing happened this period, here is what you were holding" is a
   legitimate report that zeros would misstate. That last term is normally zero — a merger
   moves shares, not money — but a cash-plus-stock merger does move cash and
   reaches neither income nor transfers, so leaving it implicit made a correct
   run fail to reconcile for a reason the banner could not name. **If you
   change how any category aggregates, that identity is the check that catches
   a double-count** — the dashboard renders a loud warning when it fails.
6. **`render.py`** draws the line/bar charts as hand-rolled inline SVG —
   no charting library. They are embedded in the page rather than linked so
   marks can use `var(--series-N)` and repaint for dark mode. It's fully
   generic; category→colour resolution lives in `dashboard.py`
   (`category_color`/`display_key`), which folds the Debits/Credits fallback
   pair into one muted "Other" swatch everywhere, since the eight real
   categories already spend the categorical palette's validated 8-slot ceiling.
   **`dashboard.py`** assembles the page: Summary (net income + the two-column
   calculation/reconciliation tables), the open-positions table, stat tiles,
   the category/ticker dropdowns (`_controls`) — the legend keeps its
   swatches but **stopped being the filter**, since eight independently
   toggleable pills had no notion of "all" and no obvious starting state — the
   by-ticker rollup and its drill-down `<dialog>`, transaction table, the
   window callout and the
   as-of-date caption (**every "still held" string on the page must agree with
   `_as_of(m)`**, or the page contradicts itself), the cost-basis note (the page
   **states which mode produced it** — two dashboards built from the same
   statements under different settings otherwise look identical and report
   different numbers, and the footer used to claim FIFO unconditionally),
   and the one vanilla-JS block driving
   hover tooltips — no external script, style, or font, so the file works
   fully offline. **The drill-down is assembled from rows the page already
   has**, never duplicated into it at build time: each transaction row carries
   `data-ticker`, `data-income` and `data-in-window`, and the dialog
   accumulates `data-income` in date order — so its running total *is* the
   by-ticker figure by construction rather than by a second calculation that
   could disagree. `_row_income` is what fills that in: only a **closing** row
   in a lot-matched category carries income, and it carries realized P&L rather
   than proceeds; under a window a row outside it contributes nothing
   **whatever its category**, or June's dividend would accumulate into a July
   report while June's sale did not. Under a window the table renders *all*
   rows and marks which fall inside, because hiding the June purchase behind a
   July sale leaves the reader unable to check the number. Nothing that
   *counts* comes from those extra rows.
   **`assets.py`** holds the only binary the page carries: the
   tab favicon, base64 PNG inline, because a linked icon file would be a
   network request and break that guarantee. Two places emit the `<head>` that
   references it — `dashboard.py` and `server.py`'s `_no_data_page` — so a head
   element added to one and not the other drifts them. `images/icon.png` is the
   512x512 original; the docstring in `assets.py` carries the Pillow snippet
   that regenerates the 32x32 bytes, and Pillow is deliberately not a
   dependency of anything that runs.
6b. **`periods.py`** pre-computes the `1m`/`3m`/`1y`/`at` views at build time
   and `dashboard.py` embeds them as JSON, because **a period button cannot ask
   anything to recompute** — the page has to work opened from a downloads
   folder with no server behind it. Periods run back from the **last activity
   date in the statements, never from today**: a statement export is historic,
   and anchoring to today would show empty rings to anyone opening last
   quarter's dashboard. A period wider than the data is clamped to it rather
   than drawing empty months that read as a drawdown, and `_months_before`
   clamps the day so a month back from the 31st lands on a real date.
   The charts are drawn client-side via `svg.innerHTML`, **not**
   `createElementNS`: the namespace URI is a literal scheme-and-slashes, and
   the offline check cannot tell a namespace from a CDN, so it fails the build.
   The performance card is **one wide chart with a Cumulative/Daily toggle**,
   not two half-width ones — they answer the same question at two resolutions
   and neither is legible at half width. The Trading Journal card's three
   entries all read the period already on screen, so a journal can never
   describe a different range from the rings above it.
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
   `dedupe.py` keeps the per-file maximum multiplicity — an identical re-upload
   answers `duplicate` and writes nothing, while the *same name with different
   bytes* is kept alongside as `name-2.csv` rather than overwriting.
   An empty volume is the normal starting state of a fresh deployment, not an
   error: `build_dashboard` raises `LoadError` there, and `_no_data_page`
   answers with the same upload chrome instead of a 500 the user cannot act on.
   A `PageCache` keyed on the input folder's file list/mtimes/sizes rebuilds
   the page only when the volume actually changed; upload and delete invalidate
   it explicitly. Config comes from `ServerConfig.from_env`:
   `RH_DASHBOARD_USER` + `RH_DASHBOARD_PASSWORD` (Basic auth, **off unless both
   are set**, and `/healthz` is always exempt so a probe survives a credential
   change) and `RH_DASHBOARD_MAX_UPLOAD` (default 10 MB; a real 556-row export
   is ~50 KB). Because "off unless both" **fails open**, a third var
   `RH_DASHBOARD_AUTH_REQUIRED` lets the operator declare intent: with it set,
   missing or empty credentials raise `AuthConfigError` and the process exits
   instead of serving unauthenticated. The chart sets it whenever
   `auth.enabled`, so a SOPS Secret with a renamed key or an empty value
   crash-loops visibly. Don't "fix" the fail-open by enabling auth from a
   partial config — a username with no password is not a password.
   `RH_DASHBOARD_COST_BASIS` picks the equity cost basis the same way (the
   chart's `costBasis`); an unrecognised value raises `ConfigError` — now the
   parent of `AuthConfigError`, so both refusals exit the same way — rather
   than falling back, because serving figures computed a different way than
   the operator asked for is a failure that never announces itself. **The CLI
   flags default to `None`, not to a mode**: an argparse default of `average`
   shadowed the env var completely, so `serve` read the flag's default and
   never the variable, and started happily on a value it should have refused.
   The flag beats the environment; its *absence* must not. The container runs
   `python -m rh_dashboard.cli serve --host 0.0.0.0` because the CLI default
   of `127.0.0.1` is deliberately unreachable from outside a container.

`selftest.py` group 3 unit-tests the lot engine (open-only, partial sale, FIFO
ordering across lots, average-cost blending, short options, expiry, oversell,
orphaned corporate-action basis) and group 7 asserts the reconciliation
invariants. `sample_data/` is built specifically to exercise the
open/partial/closed cases — TSLA never sold, SPY fully closed, and **AAPL
bought twice before it is sold** (100 @ $180 then 100 @ $150, 50 sold at $190)
— so those numbers are load-bearing; changing the CSVs means re-deriving the
expected constants in `selftest.py` by hand.

**AAPL's two lots are what make the cost-basis setting testable.** It is the
only shape where the modes can disagree, and they do: equity realized is
`+1,350` averaged against `+600` FIFO, net income `1,548.16` against `798.16`,
open basis `54,855` against `54,105`. Note the **750 appears in realized and in
open basis in opposite directions, so total cash movement is identical either
way** (`-24,806.84`) — that single number is the whole claim about cost basis:
it moves P&L through time without creating or destroying any. Group 4 asserts
that invariant directly, so a change that makes the two modes agree — or that
makes cash disagree — fails there rather than passing quietly. The `Price`
column is *not* what the engine reads; cost per unit is `amount / quantity`, so
a second lot only creates a divergence if its **Amount** differs per share.

## Convention

**Skip loudly, never substitute.** When the input doesn't support an answer,
emit a reason the dashboard prints — `Classified.fallback`, the oversell
warning, the reconciliation banner — instead of filling the gap with a
plausible number.
