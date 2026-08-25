# rh-dashboard: serve, upload, and homelab deployment

## Context

`rh-dashboard` is today a one-shot CLI: drop statement CSVs into `input/`, run
`./rh-dashboard build`, get a self-contained `output/dashboard.html`. That works
on a laptop but not in a homelab, where the CSVs live on a persistent volume and
there is no shell to run a build from.

This plan turns it into a deployed service, split across **two branches** so the
plumbing can land and be used before the harder maths is settled.

Direction set during planning:

- **Standard-library HTTP**, not Flask/FastAPI — the zero-dependency rule holds.
- **No new pages.** Upload and date range are **icons in the page header** that
  open a native `<dialog>`. One URL, one page.
- **Vanilla JS**, extending the block already in `dashboard.py:245-373`. React
  and Vue both need a script tag or a bundler, and selftest group 8 asserts the
  page contains no `<script src=`, no stylesheet link, and no `http://`. That
  offline guarantee is worth more here than a component model.
- **Feature 2 (date filter) is deferred to its own branch.** Its maths is
  written up below for evaluation, not for implementation yet.

---

## Branch layout

| Branch | Contains | Status |
|---|---|---|
| `feature/serve-and-deploy` | stdlib HTTP server, upload icon + dialog, Dockerfile, Helm chart, **plus this document at `docs/plan-serve-and-deploy.md`** | build first |
| `feature/date-window` | the windowed-P&L maths and the date-range icon | after the plumbing lands, and after you sign off on the maths |

**Step 0**: branch off `main`, commit this plan under `docs/` for reference, then
build. Nothing else is implemented until you say so.

---

# Branch 1 — serve and deploy

## The server

`rh_dashboard/server.py`, built on `http.server.ThreadingHTTPServer` +
`BaseHTTPRequestHandler`. `cli.py` gains a `serve` subcommand
(`--host`, `--port`, `-i`, `-o`).

| Route | Does |
|---|---|
| `GET /` | the dashboard page, rendered with `interactive=True` |
| `POST /api/upload` | multipart CSV upload → validate → save to `input/` → JSON result |
| `GET /api/files` | JSON list of CSVs on the volume (name, size, mtime, row count) |
| `POST /api/files/delete` | remove one CSV |
| `GET /healthz` | 200, **auth-exempt**, touches no disk — for k8s probes |

Three things that will bite during implementation:

- **`cgi` is gone.** Removed in Python 3.13, and this repo runs on 3.14, so
  `cgi.FieldStorage` is not an option. Parse multipart with
  `email.parser.BytesParser` fed a synthesised `Content-Type` header — the
  documented stdlib replacement. Read at most `Content-Length` bytes and reject
  anything over the cap *before* reading the body.
- **`http.server` is not a hardened server.** For a single-user LAN app behind
  an ingress this is fine, and it is the price of the zero-dependency rule —
  but say so in the module docstring rather than pretending otherwise. The
  mitigations that matter: `ThreadingHTTPServer`, a hard body-size cap, basic
  auth, `readOnlyRootFilesystem` in the pod, and no route that reflects user
  input into HTML unescaped (`render.esc`, `render.py:32`, already exists).
- **Rebuild cache.** Key the built page on the sorted `(name, mtime, size)` of
  `input/*.csv`; uploads and deletes invalidate it. A full build on ~556 rows is
  milliseconds — this is about not re-reading the PV on every browser refresh.

**Auth** — optional HTTP Basic, off unless `RH_DASHBOARD_USER` and
`RH_DASHBOARD_PASSWORD` are both set. `hmac.compare_digest` on both fields,
`/healthz` exempt. ~25 lines.

## Upload, as an icon

`build_page` (`dashboard.py:637`) gains `interactive: bool = False`. When false —
the CLI path — output is **byte-identical to today**, so a downloaded dashboard
never carries dead controls. When true it emits, in the existing `<header>`:

- an upload icon button, opening a `<dialog>` with a file input and a drop zone;
- a file list showing what is already on the volume, each row with a delete button.

The JS is ~60 lines appended to the existing IIFE: `fetch('/api/upload', {method:
'POST', body: new FormData(form)})`, render the JSON result inline in the dialog,
reload on success. No framework, no build step.

**Validation before anything touches the volume** — the repo's "skip loudly,
never substitute" convention applied to uploads:

1. Extension must be `.csv`; name sanitised to `[A-Za-z0-9._-]`, no path
   separators, no leading dot.
2. Body-size cap (default 10 MB, configurable).
3. **Parse it with the real parser.** Write to a temp file, call
   `loader.load_file` (`loader.py:130`), reject if it raises, if a `REQUIRED`
   header is missing (`loader.py:39`), or if it yields zero transactions. Return
   the actual `row_errors` to the dialog instead of silently accepting a
   half-broken file.
4. Collision: identical bytes → no-op ("already uploaded"); different bytes →
   save as `name-2.csv`. Never silently overwrite.

Worth a comment in the code: uploading the same statement twice under two names
is **safe**, because `dedupe.py` keeps the per-file *maximum* multiplicity rather
than the sum. That property is exactly why an upload button is not dangerous here.

## Dockerfile

`python:3.13-slim`, non-root UID 1000, `WORKDIR /app`, copy the source — **no
`pip install`, no `requirements.txt`, no gunicorn**, since the server is stdlib.
`EXPOSE 8080`, `CMD ["python", "-m", "rh_dashboard.cli", "serve", "--host",
"0.0.0.0", "--port", "8080", "-i", "/data/input", "-o", "/data/output"]`.

Plus `.dockerignore` (`.git`, `input/*.csv`, `output/*.html`, `__pycache__`) so
real account data can never end up in an image layer.

## Helm chart

```
chart/
├── Chart.yaml
├── values.yaml
└── templates/
    ├── _helpers.tpl        name/label helpers
    ├── deployment.yaml
    ├── service.yaml        ClusterIP :8080
    ├── pvc.yaml            persistence.enabled/size/storageClass/existingClaim
    ├── ingress.yaml        behind ingress.enabled — host, TLS, annotations
    ├── secret.yaml         basic-auth creds; skipped when auth.existingSecret set
    └── NOTES.txt
```

Decisions that matter, each worth a comment in `values.yaml`:

- **One PVC mounted at `/data`**, with `subPath: input` and `subPath: output`.
  One RWO volume, two directories, no second claim to manage.
- **`replicaCount: 1` and `strategy: Recreate`.** An RWO PV cannot attach to two
  pods, and two replicas would race on `input/` during an upload. Scaling is not
  a goal — say so.
- **`podSecurityContext.fsGroup: 1000`.** Without it the mounted PV is root-owned
  and every upload fails with `EACCES`. This is the most likely first-deploy
  failure by a wide margin.
- `containerSecurityContext`: `runAsNonRoot`, `readOnlyRootFilesystem: true`,
  `allowPrivilegeEscalation: false`, drop `ALL`, with an `emptyDir` on `/tmp`
  for upload staging.
- Readiness + liveness probes on `/healthz`.
- `auth.enabled` / `username` / `password` / `existingSecret`, injected as env.
- `image.repository` / `image.tag` left to you — no registry assumed, no CI.

## Branch 1 verification

New **group 9** in `selftest.py` (append; don't renumber — `CLAUDE.md` cites
groups 3 and 7 by number). Update the hard-coded `"8 groups"` at
`selftest.py:552`, and the `190 assertions / 8 groups` prose in `README.md:20`
and `CLAUDE.md`.

Group 9 runs the handler against a temp dir with no network, via a
`ThreadingHTTPServer` on port 0 in a thread plus `urllib.request`:

- `/healthz` → 200 with auth off *and* on
- `/` → 200, contains "Net income", contains the upload icon; and with
  `interactive=False` the CLI page is unchanged (re-assert group 8's
  `"<script src=" not in html`, no stylesheet link, no `http://`)
- valid CSV upload lands on disk and appears in `/api/files`
- rejected with a stated reason: `.txt` extension, empty file, CSV missing
  `Amount`, body over the cap, `../` in the filename
- identical re-upload is a no-op; different bytes under the same name become
  `name-2.csv`
- delete removes the file and invalidates the cache
- auth on → 401 without credentials, 200 with

Manual:

```bash
./rh-dashboard selftest                                    # still bare python3, no deps
./rh-dashboard build -i sample_data -o /tmp/preview         # byte-identical to today
./rh-dashboard serve -i sample_data -o /tmp/serve --port 8080
helm lint ./chart && helm template ./chart
```

Then in a browser: upload `sample_data/2026-06-statement.csv` and confirm it is
recognised as already present; upload a junk file and confirm the rejection names
the reason; delete a file and confirm the page rebuilds. On the cluster, confirm
an upload survives a pod delete.

---

# Branch 2 — the date window (for your evaluation, not yet implemented)

## Why the obvious implementation is wrong

Slicing rows to the window and running the existing pipeline destroys cost basis.
In `sample_data`, AAPL is bought twice before it is sold — 2026-06-03 at $180
and 2026-06-04 at $150 — and 50 shares go on 2026-07-05 at $190. Filter to July
and both buys disappear:

```
full-history match   ->  +$500 realized      correct
slice-then-match     ->  +$9,500 "realized"  + a spurious oversell warning
```

## The design

FIFO-match over full history; report only the window. Three calls to the existing
`compute_positions` (`positions.py:193`) — it is pure and takes a plain list, so
the engine itself needs no window parameter:

| Call | Input rows | Gives |
|---|---|---|
| as-of-end | `activity_date <= end` | `holdings` and `open_*` **as of the window end**, all pre-window basis intact |
| baseline | `activity_date < start` | the **opening** `open_*` |
| full | everything | used *only* to reword the expiry warning (see below) |

`compute_positions([])` is already safe (`positions.py:343` uses
`max(..., default=None)`), so the unwindowed path passes `[]` as the baseline and
the maths collapses to today's with **no `if window is None` branch**.

New on `PositionsResult`: `windowed(start) -> PositionsResult` — a copy whose
`realized_events` and `realized_by_category` are filtered to the window, with
`holdings` / `open_shares` / `open_equity_cost_basis` / `open_options_net_cash`
left at their as-of-end values. Take `start` only; an `end` parameter would
silently no-op when it disagrees with the result it was built from.
`realized_by_category` must be **rebuilt by summing filtered events**, and must
tolerate keys outside `LOT_MATCHED_CATEGORIES` (`positions.py:227-239` can emit a
`CORPORATE_ACTION` event with amount 0).

A single `Window` dataclass in `model.py` — `start`, `end`, `contains(d)`,
`before(d)` — used by all three filters. It **must** key on `activity_date`, the
same field the sort key and `_phase` use: filtering on `process_date` or
`settle_date` would split a same-day corporate-action pair across the boundary and
destroy its basis, since `ca_basis_pool`/`ca_incoming_qty` are date-keyed and
assume both legs survive together. That is also why the filter is applied to
`classified` before `compute_positions`, never post-hoc to lots.

## The identity generalises

Today (`metrics.py:185`):

```
cash = net_income - open_equity_cost + open_options_net_cash + transfers
```

Windowed, it becomes a **delta** identity — cash moved equals income earned, less
the change in capital tied up in positions, plus financing:

```
cash(W) = net_income(W)
        - (open_equity_cost_end - open_equity_cost_start)
        + (open_options_net_cash_end - open_options_net_cash_start)
        + transfers(W)
```

This rests on a prefix property: the FIFO fold is a deterministic left-to-right
pass over `sorted(key=(activity_date, _phase, source_file, row_index))`, so
filtering by date preserves the order of survivors, and the state of
`compute_positions(rows <= end)` at the `< start` boundary **is** the final state
of `compute_positions(rows < start)`. Subtracting the prefix identity from the
as-of-end one gives the delta form. Full range reduces to today's exactly.

**Confirmed numerically**, in memory, with no code changes — driving the existing
`compute_positions` three times over `sample_data`:

```
average cost (the default)
2026-06-01..06-30  eq= 100.00 opt=  0.00  net=   88.91  cash=-29996.09  err=0.000000
2026-07-01..07-31  eq=1250.00 opt=210.00  net= 1459.25  cash=  5189.25  err=0.000000
full range         eq=1350.00 opt=210.00  net= 1548.16  cash=-24806.84  err=0.000000

fifo
2026-06-01..06-30  eq= 100.00 opt=  0.00  net=   88.91  cash=-29996.09  err=0.000000
2026-07-01..07-31  eq= 500.00 opt=210.00  net=  709.25  cash=  5189.25  err=0.000000
full range         eq= 600.00 opt=210.00  net=  798.16  cash=-24806.84  err=0.000000

additivity (average): 88.91 + 1459.25 = 1548.16  |  -29996.09 + 5189.25 = -24806.84
additivity (fifo):    88.91 +  709.25 =  798.16  |  -29996.09 + 5189.25 = -24806.84
```

Two disjoint windows summing exactly to the full range, and the July identity
holding *across* the CCIV→LCID corporate action, is the strongest evidence the
split is honest.

## Three defects to fix in the same change

Reviewing the engine for this turned up problems that exist today and that
windowing would amplify:

1. **Orphaned corporate-action basis is destroyed silently.** `ca_basis_pool` is
   written on the outgoing leg (`positions.py:257`) and read *only* inside the
   incoming branch (`:268`). A cash merger, a delisting, or an incoming leg that
   lands in a later statement leaves the pool undistributed with **no warning** —
   the code only warns for the mirror case (`:272`). Today that's a bare,
   unexplained reconciliation banner. Windowed, if the CA date is inside the
   window, the window reconciles off by exactly the lost basis even when the full
   range happens to reconcile. Fix: warn on any unconsumed pool and expose the
   residual so the banner can name it.
2. **The expiry warning would become false.** `positions.py:349` compares against
   `last_activity = max(date)` of whatever list it was given. On an as-of-end run
   that is the window end, so a contract whose closing row sits later in the file
   gets reported as "no closing row appears in these statements… probably outside
   the input window". This is the only thing the third (full) `compute_positions`
   call is for: distinguishing "no closing row exists anywhere" from "the closing
   row is after your window end."
3. **`C(CORPORATE_ACTION) ≡ 0` is assumed but unenforced.** `total_cash_movement`
   sums all of `CATEGORY_ORDER` including corporate actions, while `expected_cash`
   omits them. A cash-plus-stock merger breaks reconciliation today. Make it a
   named term rather than an unstated assumption.

## `metrics.compute` changes

```python
def compute(classified, positions, duplicates_removed, row_errors, *,
            window: Window | None = None,
            opening: PositionsResult | None = None) -> Metrics
```

Keyword-only, and pass the whole opening `PositionsResult` rather than two loose
floats — you get `opening.open_shares` for display and can't transpose arguments.

- **`months` comes from the window**, not from `min/max(dates)`. Otherwise a
  quiet first or last month silently shrinks the axis and two reports over the
  same window aren't comparable.
- **`_empty` (`metrics.py:109`) must not fire on an empty window.** "Nothing
  happened this period, here's what you were holding" is a legitimate report, and
  `_empty` returns `open_shares=0` — which renders `_open_positions_section` with
  four rows and a `<tfoot>` reading `0` / `$0.00` (`dashboard.py:542-543`). Skip
  it whenever a window is supplied; the only thing it protects against is
  `min()` on an empty list, which window-derived months already solve.
- Keep the group 7 guard `net_income_cumulative[-1] == net_income`
  (`selftest.py:494`). Trivially true today; under a window it becomes a real
  check that the month axis and the event filter agree.

## Dashboard changes

The right-hand **"Reconciled to cash that moved"** table (`dashboard.py:449-463`)
is the one that stops adding up, since the identity now needs deltas. Print both
endpoints so the reader can see where the delta came from:

```
Net income                                            +$1,459.25
Open equity cost basis  $60,405.00 → $54,855.00       +$5,550.00   (excluded)
Open option cash            $320.00 → $0.00             −$320.00   (excluded)
Deposits                                                +$500.00   (excluded)
Withdraw                                              −$2,000.00   (excluded)
────────────────────────────────────────────────────────────────
Total cash movement                                   +$5,189.25
```

The guards at `dashboard.py:450` and `:454` must test **either endpoint**, not
the end value — a position fully closed inside the window has `end == 0` and
would suppress a row carrying a real number.

Also required, or the page contradicts itself:

- A window callout near `dashboard.py:669`, first in the data-quality list:
  *"Showing 2026-07-01 to 2026-07-31. Lots were FIFO-matched over the full
  statement history (2026-06-02 to 2026-07-28), so a position opened before this
  window reports its true realized gain, not its full proceeds. Open-position
  figures are as of 2026-07-31."*
- Every "still held" string needs an as-of date: hero tile sub (`:490`),
  `open_note` (`:465-472`), open-positions sub-head (`:571`), footer (`~:778`),
  and the CLI's `shares held, at cost` line.
- `open_note` currently explains the as-of-end basis as the excluded number —
  but the excluded number is now the delta. Reword or the prose fights the table.
- The bar chart sub-head (`~:755`) says "All-time contribution per category".
- `_transactions_table` (`:607`) shows the July sell with no June buy, so the
  `+$500` can't be verified from the page. Pass the full list with a
  `data-in-window` attribute and a chip to reveal out-of-window rows — one click
  to the June buy. Heading becomes `Transactions in window (N of M)`.
- **The same `PositionsResult` must flow to both `metrics.compute` and
  `build_page`.** Passing the windowed one to metrics and the full one to the
  page renders full-history holdings under an as-of-end footer.
- `build_dashboard()["open_shares"]` silently changes meaning from "held now" to
  "held at window end". Return the window in the result dict.
- `_legend_chips` (`:588`) needs no change.

## Branch 2 verification

Hand-derived from `sample_data` (June + July 2026, overlapping on the 06/25 and
06/28 rows; 21 rows after dedupe), then confirmed against the engine:

| | June window | July window |
|---|---|---|
| Equity realized | +100.00 (SPY) | **+500.00** (AAPL vs the June $180 lot) |
| Options realized | 0.00 | **+210.00** (July BTC closing the June STO) |
| Net income | +88.91 | **+1,459.25** (fifo: +709.25) |
| Total cash | −29,996.09 | **+5,189.25** |
| Opening equity basis | 0.00 | **60,405.00** |
| Ending equity basis | 60,405.00 | **54,855.00** (fifo: 54,105.00) |
| Opening option cash | 0.00 | **+320.00** |
| Open shares (end) | 400 | 370 |
| Reconciliation error | 0.00 | 0.00 |

New **group 10**, with these as constants, plus:

- **as-of semantics**: in the June window, `held["CCIV"].quantity == 100` and
  `"LCID" not in held` — the exact inverse of group 4's full-range assertion, and
  the cleanest proof the as-of view is real rather than a filtered final state.
- **no spurious warning** in the June window: expiry 7/17 > `last_activity` 6/30.
- **empty window** `2026-07-01..07-04`: zero rows, `months == ["2026-07"]` (not
  `[]`), `open_shares == 400`, `open_equity_cost_basis == 60,405.00` — *not*
  zeros — and the rendered `<tfoot>` shows them.
- **one-day window** `2026-07-05..07-05`: under average cost `+1,250` income
  against `+9,500` cash, Δ basis `−8,250`; under fifo `+500` against the same
  `+9,500`, Δ basis `−9,000`. One line proving lots were matched across the
  boundary, and that the mode changes only where the split falls.
- **full-range window ≡ no window**: every existing constant reproduced, both
  opening terms 0.00.
- **the prefix property directly**: `realized_events(rows < start)` equals, event
  for event, the `date < start` prefix of `realized_events(rows <= end)`. This is
  the assumption the whole design rests on; assert it so it can't rot behind the
  aggregates.
- **orphaned CA basis**: synthetic fixture with an outgoing `SXCH` leg and no
  incoming leg — assert a warning names the undistributed basis rather than the
  banner firing bare.
- **the anti-regression pair**: windowed equity income is `1,250.00` under
  average cost (`500.00` under fifo) and *not* `9,500.00`; windowed options
  income is `210.00` and *not* `−110.00` in either mode.

---

## Docs

`README.md` and `CLAUDE.md` both need the `serve` command, the deployment
section, and updated assertion/group counts. `CLAUDE.md`'s pipeline section
should gain the three-call positions rule and the delta identity when branch 2
lands — those are exactly the kind of invariant that file exists to protect.
