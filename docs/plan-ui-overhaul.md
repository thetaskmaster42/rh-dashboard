# UI overhaul: period views, daily P&L, and stat rings

Status: **design, awaiting review.**

The arithmetic is settled and tested. This is presentation — with one
exception, noted below, where a tile needs a number the engine does not
currently produce.

---

## On "move away from the standard HTTP server"

Worth separating two things that sound like one:

**The page must stay self-contained.** No external script, no stylesheet, no
font, no `https?://` anywhere in the output — CI fails the build otherwise, and
it is the reason a downloaded dashboard opens offline years later. A framework
on the page means either a CDN script (breaks it outright) or a bundler and a
node toolchain (breaks the "clone and run" property, and adds a build step
between a change and seeing it).

**Nothing in the reference screenshots needs a framework.** Donut rings are one
SVG circle with `stroke-dasharray`. The daily P&L chart is `<rect>` elements.
The period selector is five buttons. This project already hand-rolls its line
and bar charts as inline SVG; these are the same technique with better
styling. The gap between what it looks like now and what it should look like is
**design work, not stack work** — spacing, elevation, type scale, colour
discipline.

**The server** is a separate question, and the honest answer is that it is not
the bottleneck. `http.server` serves one user on a LAN, rebuilds a cached page
only when the volume changes, and costs zero dependencies. Swapping it buys
nothing this deployment can feel.

**Recommendation: keep both.** If something later genuinely needs a framework —
live streaming updates, a multi-account switcher, virtualised tables over
100k rows — that is the moment to revisit, and it will be obvious.

---

## What is being added

### 1. Period selector — `1m` `3m` `1y` `at`

These are the existing `Window`, pre-set. The engine already reports over any
range with the delta identity intact, so this is a control surface over work
that is done.

**One decision that is easy to get wrong:** anchor the periods to the **last
activity date on file**, not to today. A statement export is historic; `1m`
against today's date on a June–July fixture reports an empty window and four
zeroed rings. The page will say what it anchored to.

`at` (all time) is the unwindowed report exactly as it is now.

### 2. Daily realized P&L — the bar chart

One bar per calendar day in the period, height = realized P&L closed **on that
day**, green above the axis and red below. Days with no closing trade are empty
slots, not gaps — the axis stays continuous so a busy week reads as a busy
week.

This is a direct grouping of `positions.realized_events` by `activity_date`,
which is already exactly "what was realized on a specific day". No new
arithmetic.

### 3. The four stat rings

| Tile | Definition | Available today? |
|---|---|---|
| **Profit** | net income for the period | yes |
| **Winning trades** | closing events with P&L > 0, over all closing events | yes |
| **Avg gain in $** | mean P&L of winning closes; ring shows the win/loss split | yes |
| **Avg gain in %** | mean of `realized ÷ cost of the units closed` | **no — see below** |

**The one engine change.** `RealizedEvent` carries P&L and quantity but not
what the closed units cost, so a percentage has no denominator. The value is
already computed inside the matching loop — `lot.cash_per_unit × matched` is
summed there to produce the P&L — so this is recording a number that already
exists, not deriving a new one:

```python
@dataclass
class RealizedEvent:
    ...
    cost_basis: float      # cost of the units this close consumed
```

It is additive, defaults safely, and changes no existing figure. Both cost
bases keep working, since the cost consumed is read from whichever lot the
mode chose.

**Short options have no cost to divide by.** A sold-to-open contract opens for
a *credit*; `realized ÷ cost` is either undefined or nonsense. Percentage is
reported for debit positions only, and the tile says how many closes it covers
rather than quietly averaging over a smaller set than the other three.

### 4. Layout

Following the reference: a period bar and a nav card top-right, the period
title large on the left, four rings across the top of the main card, the
cumulative line below them, then the existing tables underneath.

"More material" concretely means: near-black ground with genuinely elevated
surfaces rather than outlined boxes, a tighter type scale, restrained use of
green/red so they mean *direction* and nothing else, and generous whitespace
between sections. The existing colour tokens already support light and dark;
this is a re-tune of the dark palette, not a new theming system.

---

## Sequencing

Two branches, because one is arithmetic and one is appearance and they review
very differently:

1. **Metrics** — `RealizedEvent.cost_basis`, the daily series, win rate, average
   win/loss, average percent, and the period presets. Hand-derived constants
   against `sample_data`, as with everything else.
2. **Visual** — the rings, the daily chart, the period bar, and the restyle.

The offline guarantee, the reconciliation identity, and the per-ticker
attribution identity all still have to hold at every step. `sample_data` has
only three closing events, which makes some of these tiles thin — a second
fixture with losses in it is likely needed so "winning trades" is not
permanently 100%.

---

## Decisions taken

1. **A trade is one closing event** — the sale or expiry that realizes P&L. An
   option counts when it closes, whether by an early buy-to-close, a
   sell-to-close, or expiry. A **rollover counts once**: the leg that closed
   realizes, the further-dated leg it opened stays uncounted until it closes.
   This is what `positions.realized_events` already contains.
2. **"Avg gain in %" is dropped**, so `RealizedEvent` needs no `cost_basis`
   after all and the engine is untouched. The fourth ring becomes **average
   loss**, which pairs with average win and needs nothing new.
3. **`sample_data` gained a losing trade and an option rollover** — MSFT bought
   and sold down, and a TSLA put bought back at a loss on the same day a
   further-dated put is sold. Five trades now close: three wins, two losses,
   60% win rate, and the fixture ends holding an open short option.

## Superseded

1. **What counts as a trade?** I propose **each closing event** — a partial
   sale counts as one trade. The alternative is a full round trip, which reads
   better but reports nothing at all until a position closes completely, and
   would show `0 trades` on a month where you sold half of everything.
2. **Percent basis for options.** Debit-only, as above? Or exclude options from
   the percentage tile entirely and label it "Equity only"?
3. **A fixture with losses.** `sample_data` currently wins every trade. I would
   add a losing trade to it — which moves the hand-derived constants again, as
   the second AAPL lot did. Worth it for honest-looking tiles, or keep the
   fixture stable and accept 100%?
