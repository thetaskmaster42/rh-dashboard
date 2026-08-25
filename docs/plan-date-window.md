# Feature: date window

Status: **design, awaiting approval to implement.**

This is a significant change from the original project, so development and
testing happen in a separate environment before anything reaches the homelab.

## Decisions taken

| # | Decision | Consequence |
|---|---|---|
| 1 | **DuckDB** as the analytical store | first runtime dependency; see "The dependency question" |
| 2 | Net income reported **cumulative *and* windowed**, both on the page | one per-day series, two readings of it |
| 3 | Equity cost basis becomes **moving average**, recomputed on purchase only | diverges from FIFO in *timing*; see "Cost basis" |
| 4 | Category filtering moves from chips to a **dropdown** | replaces `_legend_chips` as the filter control |
| 5 | A **per-ticker transaction view** with running cumulative net income | new drill-down, in-page |
| 6 | **uv** as the package manager | `pyproject.toml` + `uv.lock`; CI and image grow an install step |
| 7 | **FIFO available behind a setting, off by default** | average cost is the default; both modes must reconcile |
| 8 | **Filter and group by ticker** throughout | ticker dropdown, a by-ticker rollup table, and the drill-down in 5 |

No medallion architecture. One table at transaction grain, SQL views over it —
the "one big table" option, since the data is linear and small (~556 rows,
~50 KB for a real export).

---

## Architecture

```
input/*.csv  ──►  loader → dedupe → categorize → basis fold  ──►  DuckDB
                  (Python, unchanged)              (Python)         │
                                                                    ▼
                                                     SQL views: windowing,
                                                     cumulative series,
                                                     per-ticker rollups
                                                                    │
                                                                    ▼
                                                          metrics → dashboard
```

**What stays in Python, and why.** Parsing, dedupe, categorization and the
corporate-action pairing stay exactly where they are. Those rules were derived
from a real 556-row export and each one exists because it broke first; they are
cheap in Python and expensive to re-verify in SQL. The moving-average fold also
stays in Python — it is a genuine sequential recurrence (each average depends on
the previous one), which SQL expresses only as a recursive CTE, for no gain over
a fifteen-line loop.

**What moves to SQL, and why.** Everything aggregate: windowing, the cumulative
series, per-category and per-ticker rollups, the drill-down in decision 5.
These are what SQL is actually better at, and they are the queries that will
keep growing.

**The write path.** The Python pass emits one row per transaction carrying its
category, its realized amount (if any), and the position state *after* it —
shares held, average cost, basis. DuckDB then holds a single `transactions`
table at that grain, and every view derives from it. Ingestion is a full
rebuild, not an incremental append: the source of truth remains the CSVs on the
volume, the database is a derived cache, and a corrupted or deleted `.duckdb`
file is fixed by re-running the load. That keeps the existing upload path — which
is deployed and working — completely unchanged.

## The dependency question

DuckDB is the first runtime dependency this project has ever had, and the
zero-dependency rule is currently *enforced*: the `selftest` CI job fails the
build if a `requirements.txt` or `pyproject.toml` ever appears, and the
`Dockerfile` deliberately has no pip layer.

Choosing DuckDB means changing all of that on purpose. Dependencies are managed
with **uv** (0.12.3 is already installed locally):

- `pyproject.toml` declares the dependency set and `uv.lock` pins it, so CI, the
  image and a laptop all resolve identically. `uv sync --frozen` everywhere;
  a lockfile drift fails the build rather than silently upgrading DuckDB.
- Set `[tool.uv] package = false`. The project is a *virtual* project: uv
  installs the dependencies but not `rh_dashboard` itself, so the repo-relative
  `rh-dashboard` executable keeps putting its own directory on `sys.path` exactly
  as it does today. No install step, no `[project.scripts]`, no change to how the
  CLI resolves its paths.
- The image copies the uv binary from `ghcr.io/astral-sh/uv` and runs
  `uv sync --frozen --no-dev`, in a layer cached on the lockfile. The multi-arch
  build resolves `linux/arm64` and `amd64` from the same lock.
- `selftest` becomes `uv run ./rh-dashboard selftest`.

**Rescope the CI guard, do not delete it.** The filename check
(`requirements.txt` / `pyproject.toml` must not exist) was only ever a proxy for
the real rule: the shipping dashboard imports nothing unexpected. Replace it with
a check that enforces that directly — walk `rh_dashboard/*.py` with `ast`,
collect every top-level module imported, and fail if any falls outside
`sys.stdlib_module_names` plus an explicit allowlist. That is a *stronger* guard
than the one it replaces: it catches a stray `import requests` that a filename
check never would, and it makes the allowlist the single place the dependency
set is stated.

Verified: DuckDB 1.5.5 publishes `manylinux_2_28_aarch64` wheels for CPython
3.10 through 3.14, so the k3s nodes and the local 3.14 interpreter are both
covered. It is not installed locally yet.

This is a deliberate trade, not an oversight — noting it here so the next person
reading the CI config understands why the rule changed.

## Cost basis: moving average

Average cost is a **running fold**, not an aggregate over history:

```
on buy   :  basis += qty × price ;  shares += qty
on sell  :  shares -= qty                       # average unchanged
avg       =  basis / shares                     # recomputed on purchase
```

Sells reduce quantity and leave the average alone, which is equivalent to
reducing basis by `qty × avg`. Stating it as a fold matters — the aggregate form
("sum of purchases ÷ number of shares") gives the wrong answer as soon as a
purchase follows a sale:

```
buy 100 @ 120, buy 100 @ 100, sell 50 @ 130, buy 50 @ 200

moving average (correct)              132.50
sum of purchases ÷ shares purchased   128.00   ✗
sum of purchases ÷ shares held        160.00   ✗
```

Rules that need to be explicit, because the fold has no answer for them
otherwise:

- **Shares reach zero** → reset average to zero, so a later repurchase does not
  inherit a stale average.
- **Oversell** (shares would go negative) → keep today's behaviour: count at full
  proceeds and *warn*. Never silently guess a basis.
- **Corporate actions** stay basis-preserving: the surrendered `(shares, basis)`
  pair transfers to the received ticker and the exchange realizes nothing. Under
  average cost this is simpler than under FIFO, not harder.
- **Options do not get average cost.** There is no meaningful purchase price for
  a short-to-open contract, and two contracts on the same underlying at different
  strikes must never blend. Options keep the existing signed cash-per-unit
  matching, which needs no price and no multiplier. Equity moves to average cost;
  options do not.

### FIFO, behind a setting

Average cost is the default. FIFO stays available and off by default, since
retrofitting it later is far more expensive than keeping it now:

- `--cost-basis {average,fifo}` on the CLI, `RH_DASHBOARD_COST_BASIS` for the
  server, and a Helm value that sets the env var. Default `average` everywhere.
- Both modes are the *same* fold with a different lot-consumption rule, so this
  is a strategy parameter threaded through one function — not two engines.
- **The page must state which mode produced it.** Two dashboards with different
  cost-basis settings look identical and disagree; the mode belongs in the
  summary and the footer, in the same spirit as every other "skip loudly"
  disclosure in this project.
- The reconciliation identity must hold in **both** modes. It is the check that
  the FIFO path did not rot while average cost was the one in use.
- Options ignore the setting entirely — they have no lots to order.

### How this differs from today

The divergence is in **timing**, not in totals. Over a position that fully
closes, FIFO and average cost realize the same lifetime P&L. While a position is
open they split differently between "realized" and "still open", which is
exactly what a date window makes visible:

```
buy 100 @ 120, buy 100 @ 100, sell 50 @ 130

average cost  :  (130 − 110) × 50  =  1,000
FIFO          :  (130 − 120) × 50  =    500      ← what ships today
```

Two things follow, both worth knowing before this lands:

1. **Reported figures will move**, and will no longer match Robinhood's own
   reporting. For US taxable brokerage accounts, average cost is generally
   available for mutual-fund and DRIP shares; individual equities use FIFO or
   specific identification, and Robinhood's 1099-B reports FIFO. If the dashboard
   is ever used to sanity-check tax documents, it will disagree with them by
   design.
2. **`sample_data` does not exercise the change.** No ticker in the fixture has
   two buys before a sell, so FIFO and average cost agree on every number in it.
   Every existing selftest constant survives untouched — and the switch ships
   untested unless a new multi-lot fixture is added. That fixture is a required
   part of this work, not a nice-to-have.

## Net income: cumulative and windowed

Both are readings of one per-day series, so they cannot disagree:

```
windowed(W)      = Σ events in W                       additive across windows
cumulative(d)    = Σ events with date ≤ d              running total from inception
windowed(W)      = cumulative(end) − cumulative(start − 1)
```

The additivity property is the correctness check: two disjoint windows must sum
to the range that contains them. Confirmed against `sample_data` on the current
engine — June `+88.91` and July `+709.25` sum to the full-range `+798.16`, with
reconciliation error `0.000000` in all three.

On the page:

- the line chart keeps showing **cumulative** net income, as it does today
- the summary gains **both** figures, labelled unambiguously — "Net income in
  window" and "Net income to date" — never one number called "Net income"
- open-position figures are **as of the window end**, and every "still held"
  string says so

### Windowing without destroying basis

Slicing rows to the window and then folding is wrong: it discards the basis of
anything bought earlier. In `sample_data`, AAPL is bought in June and half-sold
in July — filter to July first and the buy disappears, turning a `+$500` gain
into a `+$9,500` fiction plus a spurious oversell warning.

So the fold always runs over full history, and only the *reporting* is windowed.
Three passes, all using the existing pure function:

| pass | rows | gives |
|---|---|---|
| as-of-end | `activity_date ≤ end` | holdings and basis **at the window end** |
| baseline | `activity_date < start` | the **opening** position state |
| full | everything | used only to reword the expiry warning |

Filtering keys on `activity_date` — never `process_date` or `settle_date`, which
would split a same-day corporate-action pair across the boundary and destroy its
basis.

The reconciliation identity generalises to a delta — cash moved equals income
earned, less the change in capital tied up in positions, plus financing:

```
cash(W) = net_income(W)
        − (open_equity_cost_end  − open_equity_cost_start)
        + (open_options_cash_end − open_options_cash_start)
        + transfers(W)
```

The full range reduces to today's identity exactly, so there is no
`if window is None` branch anywhere.

## Dashboard changes

**Category dropdown (decision 4).** The filter chips are replaced by a single
`<select>`, defaulting to "All categories", filtering the transaction table and
the charts together. The legend keeps its colour swatches — it still explains
the chart — but stops being the filter control. Category order comes from
`CATEGORY_ORDER` as everywhere else, so a category's colour slot never shifts.

**Ticker filter and grouping (decision 8).** A ticker dropdown sits beside the
category dropdown and filters the transaction table and charts the same way.
Alongside it, a **by-ticker rollup table** — one row per ticker, sortable, and
obeying the active date window:

| column | source |
|---|---|
| Ticker | position key |
| Shares held (at window end) | as-of-end pass |
| Average cost / basis | the fold, or FIFO lots when the setting is on |
| Realized equity P&L in window | windowed realized events |
| Realized options P&L in window | windowed realized events, by underlying |
| Dividends in window | category rollup, attributed by ticker |
| Net contribution | the per-ticker definition below |

The two dropdowns and the date window compose: category × ticker × window all
narrow the same underlying row set, and the totals row recomputes against
whatever is selected.

**Per-ticker view (decision 5).** A ticker dropdown, plus click-through from the
open-positions table, opens an in-page view listing every transaction touching
that ticker — buys, sells, options on that underlying, dividends, ticker-specific
fees — with a running cumulative net income column beside them.

This needs one definition stated up front, or the numbers will not add up:

```
per-ticker net income = realized equity P&L
                      + realized options P&L on that underlying
                      + dividends on that ticker
                      + ticker-attributable fees (e.g. ADR fees)
```

Categories with no ticker — deposits, withdrawals, margin interest, account-level
fees — go to an explicit **Unattributed** bucket. The check that attribution
neither double-counted nor dropped anything is then:

```
Σ per-ticker net income + unattributed  ≡  total net income
```

That identity should be asserted in the test suite, in the same spirit as the
existing reconciliation check.

**No new pages.** Both controls stay in the existing header/dialog pattern, as
with upload.

## Engine defects to fix first

Three problems exist on `main` today and windowing amplifies each. They are
independent of everything above and are best fixed on their own branch, against
the current engine, before this work starts:

1. **Orphaned corporate-action basis is destroyed silently.** `ca_basis_pool` is
   written on the outgoing leg (`positions.py:257`) and read only inside the
   incoming branch (`:268`). A cash merger, a delisting, or an incoming leg
   arriving in a later statement leaves the pool undistributed with no warning.
   Windowed, the window reconciles off by exactly the lost basis even when the
   full range happens to reconcile.
2. **The expiry warning becomes false.** `positions.py:349` compares against
   `max(date)` of whatever list it was given — on an as-of-end pass that is the
   window end. This is the only reason for the third (full) pass.
3. **`C(CORPORATE_ACTION) ≡ 0` is assumed but unenforced.** `total_cash_movement`
   sums all of `CATEGORY_ORDER` including corporate actions; `expected_cash`
   omits them. A cash-plus-stock merger breaks reconciliation today.

## Rules that must survive the port

Each came from a real export and each exists because it broke first. A SQL
rewrite loses them easily:

| rule | how it gets broken |
|---|---|
| dedupe keeps the **per-file maximum** multiplicity | `SELECT DISTINCT` deletes real repeated transactions — it once erased a 100-share purchase |
| opens processed **before** closes on the same date | statements carry no time; `ORDER BY activity_date` without that tiebreaker recreates the phantom-short bug |
| empty Amount means **$0.00**, not malformed | `WHERE amount IS NOT NULL` drops 45 rows of a real export |
| trailing `S` on Quantity marks the outgoing corporate-action leg | it is the only direction signal — both legs have empty Amount |
| corporate actions are basis-preserving transfers | joining on process/settle date splits the same-day pair |
| the reconciliation identity spans **all** categories | dividends, fees, margin and deposits have no ticker, so a ticker-grained view cannot express it |

## Verification

Hand-derived from `sample_data`, then confirmed against the engine:

| | June window | July window |
|---|---|---|
| Equity realized | +100.00 | **+500.00** |
| Options realized | 0.00 | **+210.00** |
| Net income (windowed) | +88.91 | **+709.25** |
| Net income (cumulative, to end) | +88.91 | **+798.16** |
| Total cash | −14,996.09 | **+5,189.25** |
| Opening / ending equity basis | 0.00 → 45,405.00 | **45,405.00 → 39,105.00** |
| Opening / ending option cash | 0.00 → +320.00 | **+320.00 → 0.00** |
| Open shares at end | 300 | 270 |
| Reconciliation error | 0.000000 | 0.000000 |

Plus:

- **as-of semantics** — in the June window `CCIV` is held at 100 and `LCID` is
  absent; in the full range the inverse. Proves the as-of view is real rather
  than a filtered final state.
- **additivity** — `88.91 + 709.25 = 798.16` and
  `−14,996.09 + 5,189.25 = −9,806.84`, both exact.
- **cumulative ≡ windowed** — `cumulative(end) − cumulative(start−1)` equals the
  windowed figure for every window tested.
- **multi-lot fixture (new)** — a ticker with two buys before a sell, asserting
  average cost and *not* the FIFO figure. Without this the basis change is
  untested, and it is also the only fixture that can tell the two modes apart.
- **both cost-basis modes** — the full suite runs under `average` and `fifo`,
  and the reconciliation identity holds in both. On `sample_data` the two agree
  on every figure; on the multi-lot fixture they must differ, by the amount the
  fixture was built to produce.
- **by-ticker rollup** — the rollup's totals row equals the unfiltered figures,
  and filtering to one ticker matches that ticker's row.
- **empty window** — zero rows, months still derived from the window, open
  positions reported at their carried-in values rather than zeros.
- **per-ticker attribution** — `Σ per-ticker + unattributed ≡ total net income`.
- **orphaned CA basis** — synthetic outgoing `SXCH` leg with no incoming leg
  warns by name rather than firing a bare reconciliation banner.

## Open items

1. **Dividend attribution to a ticker** is by description parsing
   (`SPY Cash Div: ...`), which is the same class of pattern rule as
   `categorize.py` and carries the same risk of a real export not matching.
   Unattributable dividends must fall into **Unattributed** and be flagged, never
   guessed onto a ticker.
2. **Options attribution to an underlying** parses the contract description.
   `normalise_contract` already does most of this; confirm it yields the
   underlying cleanly for every contract shape in a real export before the
   by-ticker rollup relies on it.
