# rh-dashboard

> Metadata — Topic: Robinhood statement dashboard · Category: Production · Date: 2026-08-15

Reads every CSV in a folder of Robinhood statement exports, removes duplicate
rows (statements overlap month to month), matches buys against sells to work
out what actually closed, and writes a single self-contained HTML dashboard
answering one question honestly: **did this account make money, and what is
still held?**

Standard library, plus **DuckDB** as the analytical store. No network calls
and no external CSS/JS/fonts in the *output*: the dashboard is one HTML file
that opens offline, because the input is real account activity. That guarantee
is unchanged and CI enforces it.

## Quick start

```bash
./rh-dashboard build              # reads input/*.csv, writes output/dashboard.html
./rh-dashboard build -i sample_data -o /tmp/preview   # try the bundled sample first
./rh-dashboard serve              # same thing, served, with CSV upload
./rh-dashboard build --cost-basis fifo   # match a 1099-B instead of averaging
uv run ./rh-dashboard selftest    # 369 assertions across 11 groups
```

Then open `output/dashboard.html` in a browser, or `http://127.0.0.1:8080` if
you used `serve`.

`build` and `serve` need nothing installed. The test suite does — DuckDB is
declared in `pyproject.toml` and pinned in `uv.lock`:

```bash
uv sync --frozen                  # one dependency, into ./.venv
uv run ./rh-dashboard selftest
```

The project itself is never installed (`[tool.uv] package = false`); uv only
provides the dependency, and `rh-dashboard` still imports the package off
`sys.path` from the repo.

### Cost basis

Closed equity lots are matched at **average cost** by default: every open lot
of a ticker is blended into one running cost per share, and a sale realizes
against that blend. `--cost-basis fifo` consumes the oldest lot first instead,
which is what Robinhood's own 1099-B reports — pick it if you are comparing
this dashboard against a tax document.

The two agree on lifetime P&L for a position that fully closes; they differ
only in *when* a gain lands while a position is still partly open. Options
ignore the setting entirely, since a contract has no purchase price to average
and two strikes on the same underlying must never blend.

The rendered page always names the mode that produced it — two dashboards
built from the same statements under different settings otherwise look
identical and report different numbers. The server reads
`RH_DASHBOARD_COST_BASIS` (the chart's `costBasis` value) and refuses to start
on an unrecognised one rather than quietly falling back.

## The central idea: buying a stock is not a loss

This is the thing most transaction-ledger dashboards get wrong, and it is why
this one is built the way it is.

A Robinhood statement is a **cash ledger**. Naively summing the Amount column
per category makes buying 100 shares of TSLA look like −$25,000 of performance.
It isn't. That cash became stock; your net worth didn't move. Only *selling*
realizes a gain or a loss, and only for the shares actually sold.

So every equity and option trade is FIFO lot-matched (`positions.py`), and the
dashboard reports two separate numbers that never get conflated:

| | What it is |
|---|---|
| **Net income** | Realized P&L on positions that actually closed, + dividends/interest, − fees, − margin interest, − Gold. The "did I make money" number. |
| **Open positions** | Shares and contracts still held, valued at **cost**. A holding, not a gain or a loss. |

Worked through with the bundled sample:

- **TSLA** — bought 100, never sold → 100 shares open at $25,000 cost basis.
  Contributes **$0** to net income.
- **AAPL** — bought 100 @ $180, sold 50 @ $190 → **+$500** realized on the 50
  sold; the other 50 stay open at $9,000 cost basis.
- **SPY** — bought 10 @ $550, sold all 10 @ $560 → **+$100** realized, nothing
  open.

### It reconciles, and the suite proves it

```
net income − cash in open positions + deposits + withdrawals ≡ total cash movement
```

The dashboard shows both sides of that identity side by side, and
`metrics.compute()` asserts it on every run — if the income/holding split ever
double-counts or drops a row, `reconciliation_error` goes non-zero and the page
says the figures are unreliable instead of quietly showing numbers that don't
add up.

**There is no mark-to-market anywhere.** A statement export carries no live
price, so open positions are shown at what they cost, never at what they are
worth today. Unrealized P&L on open shares is *not knowable* from this input,
and nothing here estimates it.

## Layout

```
rh-dashboard/
├── rh-dashboard               executable CLI
├── input/                     INPUT: drop your statement CSVs here (gitignored)
├── output/                    OUTPUT: dashboard.html lands here (gitignored)
├── sample_data/               two fictional statements built to demonstrate the
│                                open/closed/partial cases above
└── rh_dashboard/
    ├── loader.py              find + parse CSVs, tolerant of header spelling
    ├── dedupe.py              exact-row duplicate removal across files
    ├── categorize.py          the eleven-bucket classification rules
    ├── positions.py           FIFO lot matching — realized P&L vs. open holdings
    ├── metrics.py             the income statement + reconciliation
    ├── render.py              inline SVG chart builders (generic; no category logic)
    ├── dashboard.py           HTML assembly, theme tokens, category→colour
    ├── pipeline.py            build_dashboard() — the public API
    ├── server.py              stdlib http.server front end: serve + upload
    └── selftest.py            internal verification suite
```

## Running it as a service

`./rh-dashboard build` needs a shell and a folder you can drop files into.
`./rh-dashboard serve` needs neither, which is what makes it deployable:

```bash
./rh-dashboard serve --host 0.0.0.0 --port 8080 -i /data/input -o /data/output
```

It serves the same dashboard, with one addition — a **Statements** button in the
top right that opens a dialog for adding and removing statement CSVs. An upload
is accepted only if `loader.py` can actually parse it; anything else comes back
with the reason instead of landing on disk to break the next build. Uploading a
statement you already have is harmless and says so, since duplicate rows across
overlapping exports are removed anyway.

Set `RH_DASHBOARD_USER` and `RH_DASHBOARD_PASSWORD` to require HTTP Basic auth.
Auth is off unless **both** are set — a username with no password is not
"nearly protected", it is open. `/healthz` is always exempt, so a probe can't
be locked out by a credential change.

Set `RH_DASHBOARD_AUTH_REQUIRED=true` to say you *meant* to have auth: missing
or empty credentials then make the server refuse to start instead of coming up
unauthenticated. The Helm chart sets it automatically whenever `auth.enabled`,
so a Secret with a renamed key or an empty value crash-loops visibly rather
than quietly serving your account statement to anyone who can reach it.

The page the CLI writes is untouched by any of this: `build` output is
byte-for-byte what it was before the server existed, with no upload button in
it, because a dashboard you mail to someone shouldn't carry controls that post
into the void.

This is `http.server`, not a hardened web server — a deliberate trade for the
zero-dependency rule. Fine for one person on a private network; don't put it on
the internet.

### Kubernetes

A `Dockerfile` (with no `pip install`, because there is nothing to install) and
a Helm chart are in the repo:

Tagging a commit `v1.2.3` publishes a multi-arch image and the packaged chart
to GHCR, so the usual path is:

```bash
helm install rh oci://ghcr.io/thetaskmaster42/rh-dashboard-chart/rh-dashboard \
  --version 1.2.3 \
  --set image.repository=ghcr.io/thetaskmaster42/rh-dashboard
```

Or build it yourself:

```bash
docker build -t your-registry/rh-dashboard:1.0.0 .
helm install rh ./chart \
  --set image.repository=your-registry/rh-dashboard \
  --set auth.enabled=true --set auth.username=me --set auth.password=... \
  --set ingress.enabled=true --set ingress.hosts[0].host=rh.homelab.lan
```

One ReadWriteOnce PVC is mounted at `/data`, with `input/` and `output/` as
subpaths. The deployment is fixed at one replica with a `Recreate` strategy:
two pods can't share an RWO volume, and would race on the input folder during
an upload if they could. The PVC carries `helm.sh/resource-policy: keep`, so
uninstalling the release doesn't delete your statements.

If uploads fail with a permission error, `podSecurityContext.fsGroup` is the
value to check — the container runs as UID 1000 and a freshly provisioned
volume is usually root-owned.

If the pull fails with `failed to fetch anonymous token: 401 Unauthorized`, the
GHCR package is private. **Package visibility is fixed at first publish and is
independent of the repository's** — making the repo public later does not
bring existing packages with it, and there is no REST endpoint for it. Either
flip it once under *Package settings → Change visibility*, or keep it private
and set `imagePullSecrets`.

Credentials are best supplied as a Secret you manage yourself — a
SOPS-encrypted one, for example — via `auth.existingSecret`, with
`auth.usernameKey`/`auth.passwordKey` naming its keys. The chart then creates
no Secret and no password passes through Helm values.

## Input format

Point `--input` at a folder; every `*.csv` in it is read (sorted by filename).
Expected columns, matching Robinhood's documented statement export:

```
Activity Date, Process Date, Settle Date, Instrument, Description, Trans Code, Quantity, Price, Amount
```

Header matching is case/space-insensitive and accepts common alternates
(`Date`, `Symbol`, `Type`, `Net Amount`, …) — see `HEADER_ALIASES` in
`loader.py`. Only `Activity Date`, `Trans Code` and `Amount` are required. A
file missing one of those is skipped with an error in the run log rather than
aborting the run; a single unparseable row is skipped the same way.

**Verified against one real 556-row Robinhood export** covering 2020–2026 and
21 distinct trans codes, which is where the corporate-action, `MINT`, `CIL` and
`DFEE` rules come from. That is one account's history, not the full Robinhood
vocabulary — a code it has never seen still falls through to the sign-based
fallback, and the page says so rather than guessing quietly.

Three quirks of the real format worth knowing, all handled:

- **Empty Amount is meaningful**, not malformed — expirations, assignments,
  mergers, exchanges and splits move shares without moving cash. Treated as
  `$0.00` rather than skipped. (Skipping them dropped 45 rows.)
- **Rows are newest-first**, which matters for same-day ordering (see below).
- **A trailing `S` on Quantity** marks the outgoing leg of a corporate action.
- Trailing blank lines and the disclaimer paragraph Robinhood appends after the
  last record are skipped silently, not reported as errors.

## Categorisation

| Category | Rule | Counts as |
|---|---|---|
| **Equity** | `Buy`/`Sell` without an option-looking description | income *(realized only)* |
| **Options** | `BTO`/`STO`/`BTC`/`STC`/`OEXP`/`OASGN`/`OEXER`/`OCA`, or a `Buy`/`Sell` whose description matches `… Call $150.00` | income *(realized only)* |
| **Dividends/Interest** | any code containing `DIV` (`CDIV`, `MDIV`, …), plus `INT` with a non-negative amount | income |
| **Fees** | `AFEE` and other `*FEE` codes | income (negative) |
| **Margin** | description mentions "margin", or `INT` with a negative amount | income (negative) |
| **Gold** | `GOLD` / `GDBP` | income (negative) |
| **Deposits** | `ACH` / `RTP` with a non-negative amount | **not** income — financing |
| **Withdraw** | `ACH` / `RTP` with a negative amount | **not** income — financing |
| **Corporate action** | `MRGS`, `SXCH`, `SPR` (merger, share exchange, split/spinoff) | **not** income — moves shares, no cash |
| **Debits / Credits** | anything left over, by sign — shown as "Other" | income, but flagged |

Less obvious codes, verified against a real 556-row export: **`MINT`** is
"Aggregated Margin Rate" (margin interest, easily mistaken for a transfer),
**`DFEE`** is a sponsored-ADR processing fee, **`CIL`** is cash in lieu of a
fractional share (equity proceeds), **`GDBP`** is a Gold deposit-boost payment
(income, which nets against the subscription cost).

Every classification carries a reason. Unrecognised trans codes fall back to
the sign-based Debits/Credits split, render as a single muted **"Other"**
swatch, get a `†` in the transaction table, and are listed in the page's "Data
quality notes" — that's the signal to add a rule in `categorize.py`.

Deposits/Withdraw assume Robinhood doesn't use distinct codes per direction, so
sign is the signal; adjust `TRANSFER_CODES` if your export proves otherwise.

## Corporate actions

Mergers, SPAC closings, ticker changes and splits move shares without moving
cash, and Robinhood writes them as **same-day pairs** where a trailing `S` on
Quantity marks the outgoing leg:

```
7/26/2021  SXCH  CCIV  200S   ← 200 CCIV surrendered
7/26/2021  SXCH  LCID  200    ← 200 LCID received
1/12/2026  SPR   AKAN  200S   ← 5:1 reverse split
1/12/2026  SPR   AKAN   40
```

Both legs carry an **empty Amount**, so the `S` is the only thing distinguishing
direction. These are treated as **basis-preserving transfers, not taxable
events**: the surrendered lots' cost basis carries onto the shares received, and
the exchange realizes nothing. Selling the replacement shares later produces the
correct gain against the original purchase price.

Getting this wrong is not subtle — before it was handled, every SPAC and ticker
change in a real account (CCIV, GGPI, AACQ, VSLR, KTOV…) stayed "open" forever
because nothing ever closed the position.

## Deduplication

Overlapping monthly/annual exports repeat rows, but a row repeated **within one
file is not a duplicate** — it is two real transactions that happen to be
identical, which is ordinary:

```
10/14/2020  STC  1  $2.98   PLUG 10/16/2020 Call $20.00
10/14/2020  STC  1  $2.98   PLUG 10/16/2020 Call $20.00   ← closes the 2nd contract
```

So dedupe is **multiplicity-preserving**: for each distinct row it counts
occurrences per source file and keeps the *maximum* across files, not one. Twice
in each of two overlapping exports is two transactions — not one, not four.
(Collapsing to one previously deleted a 100-share purchase and left an option
contract open forever.)

## Lot matching details

- **FIFO** (first in, first out) — the standard default when no specific tax lot
  is elected. If you elected specific lots, your realized figures will differ.
- **Options match per contract**, keyed on the Description with Robinhood's
  `"Option Expiration for …"` prefix stripped, so the row that closes a contract
  matches the row that opened it.
- **Opens are processed before closes on the same date.** Statements carry a
  date but no time and export newest-first, so raw file order lists a same-day
  sell *above* the buy that funded it — which used to produce a phantom short
  plus a still-open position on same-day round trips.
- **An option still open past its own expiry is flagged**, since that means its
  closing row is missing from the input.
- **Expirations fall out naturally**: `OEXP` carries amount 0, so a long option
  that expired worthless realizes its full debit as a loss and a short one keeps
  its full credit as a gain.
- **Selling more than the statements account for** (position opened before the
  earliest file, or a short sale) is counted at full proceeds with no cost
  basis, and **flagged** in the data quality notes rather than silently guessed.

## Limitations

- No live prices → no unrealized P&L and no current portfolio value; only cost
  basis for what is held.
- Positions opened before the earliest statement have no cost basis here (see
  above — it's flagged when it happens).
- Categorisation rules are pattern-matched against documented trans codes, not
  verified against a live account export.
- Dedupe is an exact match on every column except source file, with
  multiplicity preserved per file (see above).
- Option assignment/exercise is treated as closing the contract; any resulting
  share movement is counted from its own Buy/Sell row.
- A record of what happened — not investment advice, not tax advice. Don't file
  taxes from these numbers.
