# Plaid integration: daily transaction fetch

Status: **design, awaiting review. Nothing implemented.**

Goal: keep the dashboard current without manually downloading statements. The
existing CSV exports stay as the **historic load**; Plaid fills forward from
the last date already on file.

---

## The shape of the idea

Plaid becomes *just another CSV producer*. The fetcher writes rows into
`input/` in the same statement format the loader already parses, and the whole
pipeline — loader → dedupe → categorize → positions → metrics → dashboard —
runs unchanged.

```
Robinhood statement CSVs  ─┐
   (historic, manual)      ├─►  input/*.csv  ─►  existing pipeline  ─►  dashboard
Plaid daily fetch         ─┘
   (plaid-YYYY-MM-DD.csv)
```

This is deliberately the least invasive option available. Nothing in the
engine learns about Plaid, the offline guarantee is untouched, and if the
integration is ever removed the CSVs it wrote remain valid input.

**No new runtime dependency.** Plaid is a plain JSON REST API and
`urllib.request` is enough, so the import guard's allowlist does not grow. The
`plaid-python` SDK buys generated models this project does not need.

---

## What I verified, and what I could not

**Verified:**

- `/investments/transactions/get` takes `access_token`, `start_date`,
  `end_date`, and `options` (`count` max **500**, `offset`, `account_ids`).
- History is capped at **two years prior to the initial linking of the Item**,
  not two years rolling. Linking late permanently loses what came before —
  which is exactly why the CSV historic load matters and must be kept.
- Data can take **one to two minutes** to be ready after Link completes.
- **Hosted Link** (`/link/token/create` with a `hosted_link` object) returns a
  `hosted_link_url` that Plaid itself hosts. This matters: the ordinary Link
  SDK *must* be loaded from `https://cdn.plaid.com` and cannot be bundled,
  which would put an external script into a project whose central guarantee is
  that its output makes no network request. Hosted Link keeps that script
  entirely outside this codebase.

**Not verified — this gates the whole feature:**

> **Does Robinhood expose Investments *transactions* through Plaid, or only
> balances and holdings?**

Public sources conflate "Robinhood uses Plaid" (true — for ACH funding) with
"Robinhood is readable *via* Plaid Investments" (unconfirmed). Per-product
institution coverage is only answerable with live credentials, so it is a
**pre-flight check, not an assumption**:

```
POST /institutions/get_by_id
  { "institution_id": "<robinhood>", "country_codes": ["US"],
    "options": { "include_optional_metadata": true } }
→ inspect `products` for "investments"
```

If `investments` is absent, **stop** — no amount of code makes the data appear,
and the fallback is the upload flow that already exists.

---

## The five things that would silently corrupt the numbers

Ordered by how quietly they fail. Every one produces a plausible-looking
dashboard rather than an error.

### 1. The sign convention is inverted

Plaid's Investments schema differs from its Transactions schema, and so does
its sign convention: **purchases are positive, sales are negative.** A
Robinhood statement is the opposite — a buy is `($18,000.00)`.

Getting this backwards does not crash. It reports every gain as a loss and
every loss as a gain, and the reconciliation identity still closes, because it
closes against whatever cash the rows claim moved.

**Mitigation:** negate on import, and assert it with a fixture where a known
buy must land negative. Also assert the *direction* end-to-end: a fetched buy
followed by a fetched sale at a higher price must produce a **positive**
realized figure.

### 2. The same trade arriving from both sources

The historic CSVs and the daily fetch will overlap at the boundary, and
`dedupe.py` is multiplicity-preserving: it counts occurrences per source file
and keeps the **maximum**. Two sources reporting one trade collapse to one
*only if the mapped fields are byte-identical* — same date, instrument, trans
code, quantity, price, amount, and `shares_removed`.

Any drift — a price rounded differently, a description Plaid words its own way
— and the row is not a duplicate but a second real transaction. **A double
counted 100-share purchase is exactly the bug `dedupe.py` exists to prevent**,
approached from the other side.

**Mitigation:** fetch strictly *after* the last activity date present in the
CSVs, with a deliberate overlap window, and assert on a fixture that the
overlap collapses. This is the single highest-risk area and deserves the most
test weight.

### 3. Option contracts key on a Robinhood-specific string

`positions.py` matches option positions on the normalised contract
*Description* — `AAPL 7/17/2026 Call $220.00`. That format is Robinhood's.
Plaid describes securities its own way.

A contract opened in the CSV history and closed by a Plaid row **will not
match**. It stays open forever and its close is counted at full proceeds with
an oversell warning — the same failure the corporate-action work fixed, from a
new direction.

**Mitigation:** map Plaid option securities into the exact Robinhood
description format, and assert a cross-source round trip — open from a CSV
fixture, close from a Plaid fixture, and require one clean realized figure with
**no** warnings. If the format cannot be reproduced faithfully, options must be
excluded from the fetch and said so out loud, rather than quietly mismatched.

### 4. Corporate actions have no trailing `S`

The basis-preserving corporate-action logic depends on
`Transaction.shares_removed` — the trailing `S` on Quantity — because both legs
of an exchange carry an empty Amount and that suffix is the *only* direction
signal. Plaid has no such convention.

**Mitigation:** derive direction from Plaid's `type`/`subtype` where possible;
where it is not possible, **skip the row and warn** rather than guess a
direction. A mis-directed corporate action destroys cost basis silently, which
is precisely what `unmatched_corporate_action_basis` was built to surface.

### 5. There is no `trans_code`

`categorize.py`'s eleven-bucket rule set keys on trans code (`CDIV`, `MINT`,
`DFEE`, `MRGS`, `SXCH`, `SPR`, …), checked against a real 556-row export. Plaid
supplies `type` and `subtype` instead.

**Mitigation:** a new mapping table, `plaid_codes.py`, from
`(type, subtype)` → Robinhood trans code, following the same convention as
everywhere else: anything unmapped falls to the sign-based fallback and is
**flagged**, never silently bucketed. The existing `Classified.fallback`
machinery already surfaces this on the page.

---

## Components

| Piece | Does |
|---|---|
| `plaid_client.py` | `urllib` POSTs to the Plaid API. Client id/secret/access token from env, never logged, never rendered. |
| `plaid_map.py` | Plaid investment transaction → `Transaction`. Sign inversion, code mapping, option description reconstruction. The whole risk surface, in one file. |
| `plaid_fetch.py` | Work out the window, page through results (500/page), write `input/plaid-YYYY-MM-DD.csv`. |
| `cli.py` | `rh-dashboard fetch [--since] [--dry-run]` |
| `chart/` | A `CronJob` writing to the same PVC, plus a Secret. |

**Config**, fail-closed in the same style as auth — with `PLAID_ENABLED` set,
missing credentials **raise and refuse to start** rather than quietly doing
nothing:

```
PLAID_ENABLED  PLAID_CLIENT_ID  PLAID_SECRET  PLAID_ACCESS_TOKEN  PLAID_ENV
```

**Getting the access token** is a one-time manual step, deliberately outside
this app: create a link token with `hosted_link`, open the returned URL in a
browser, complete the flow, exchange the resulting `public_token` for a
long-lived `access_token`, and store it as a SOPS Secret. A homelab behind
Tailscale has no public endpoint for Plaid's `SESSION_FINISHED` webhook, so the
exchange is done by hand once rather than by code that would need an inbound
route. `rh-dashboard plaid-link` can print the URL and walk through it.

**Scheduling**: a Kubernetes `CronJob` on the existing PVC, not a thread in the
server. It matches the deployment model already in place, it can be run
manually, and a failed fetch does not take the dashboard down with it.

---

## Why the fetch writes CSV rather than going to DuckDB

DuckDB is declared and proven to load but nothing in the pipeline uses it. It
is tempting to make this the feature that justifies it. It should not be:

- writing CSV means the entire existing pipeline, and all 463 assertions,
  apply to fetched data unchanged
- the files are inspectable and diffable when a number looks wrong, which for a
  brand-new upstream is worth more than query speed
- a bad fetch is undone by deleting a file

The store remains a separate decision, and is a better one once there is more
than one consumer of the data.

---

## Verification

- **pre-flight**: `investments` present in Robinhood's Plaid product list
- **sign**: a fetched buy is negative; a fetched buy-then-higher-sale produces a
  *positive* realized figure
- **overlap**: a trade present in both a CSV fixture and a Plaid fixture appears
  **once**, with `dedupe.removed` reporting it
- **cross-source options**: opened from CSV, closed from Plaid → one realized
  figure, zero warnings
- **unmapped code**: an unknown `(type, subtype)` is flagged, not bucketed
- **corporate action without a direction signal**: skipped and warned
- **secrets**: no credential appears in the page, the CLI output, or a written CSV
- **offline**: the built page still contains no `https?://` — the fetcher runs
  before the build and never touches the output

All fixtures are hand-built and fictional, as everywhere else in this project.
No real statement rows in tests.

---

## Open questions for you

1. **Is Robinhood readable via Plaid Investments at all?** Everything here is
   contingent on the pre-flight check. Worth running first.
2. **Plaid billing** — Investments is a paid product per Item per month on most
   plans. Fine for one account, but it is a real cost this project has not had.
3. **Options: map or exclude?** Reconstructing Robinhood's contract description
   faithfully is the risky part. Excluding options from the fetch keeps equity
   current and leaves options to statement uploads, which is less useful but
   cannot produce a wrong number.
4. **What happens on a fetch gap?** If the CronJob is down for a week, the next
   run must widen its window rather than fetch only yesterday. Simplest correct
   rule: always fetch from `last activity date on file − 7 days`, and let
   dedupe absorb the overlap.
