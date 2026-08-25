"""
FIFO lot matching: which positions are still open, and what actually got
realised on the ones that closed.

## Why this exists

Robinhood statements are a transaction ledger, not a positions ledger. But for
trades specifically, the ledger carries everything needed to reconstruct
positions: every opening and closing row has a quantity and an amount. Match
each closing trade against the oldest still-open lot (FIFO — the standard
default absent an explicit tax-lot election) and you learn three things the
raw cash sum cannot tell you:

  1. how many shares/contracts are still held, and at what cost
  2. how much of the cash that left the account is a *holding* rather than a loss
  3. what genuinely closed, and what it made or lost

Buying 100 TSLA and never selling is not "-$25,000 of P&L" — it is $25,000 of
cash converted into $25,000 of stock. Selling 50 of 100 shares realises P&L on
those 50 only; the other 50 stay open. That distinction is this module's whole
job, and it is what makes the Net Income figure on the dashboard meaningful.

## One engine, two instruments

Equity and options reduce to the same arithmetic if you work in **signed cash
per unit** (`amount / quantity`) rather than in prices:

    realised = (closing cash/unit + opening cash/unit) x units matched

For an equity round trip: open Buy 5 @ -$2,750 -> -550/share; close Sell 5 @
+$2,400 -> +480/share; realised = (480 - 550) x 5 = -$350. For a short call:
open STO +$320 -> +320/contract; close BTC -$110 -> -110/contract; realised =
(-110 + 320) x 1 = +$210. Same formula, and it needs no options multiplier
because the statement's Amount already includes it.

Expirations fall out for free: OEXP carries amount 0, so a long option that
expired worthless realises its full debit as a loss, and a short one keeps its
full credit as a gain.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import date

from .model import Category, Classified, CostBasis

# Trans codes that OPEN a position, per category.
OPENING_CODES = {
    Category.EQUITY: {"BUY"},
    Category.OPTIONS: {"BTO", "STO"},
}
# Trans codes that CLOSE one. OEXP/OASGN/OEXER close at whatever amount the
# statement carries — which is *blank* for an expiration, parsed to 0 by
# `loader.py`, exactly right for a contract that expired worthless.
CLOSING_CODES = {
    Category.EQUITY: {"SELL"},
    Category.OPTIONS: {"BTC", "STC", "OEXP", "OASGN", "OASSN", "OEXER", "OCA"},
}
# Codes in a lot-matched category that settle in cash with no quantity to match
# (cash in lieu of a fractional share). Counted straight into realised P&L —
# expected, so no warning.
CASH_SETTLEMENT_CODES = {"CIL"}

# Robinhood prefixes an expiration's Description with a phrase, so the row that
# closes a contract does not read identically to the row that opened it:
#     STO   "GOOG 8/14/2026 Call $390.00"
#     OEXP  "Option Expiration for GOOG 8/14/2026 Call $390.00"
# Stripping the prefix is what lets the two match as one position.
_DESC_PREFIXES = re.compile(
    r"^(option\s+(expiration|assignment|exercise)\s+for|"
    r"expiration\s+of|assigned|exercised)\s+", re.IGNORECASE)


def normalise_contract(description: str) -> str:
    """Reduce an option Description to the contract it names, so the opening
    and closing rows key to the same position."""
    d = " ".join((description or "").split())
    prev = None
    while prev != d:                       # peel repeated/stacked prefixes
        prev = d
        d = _DESC_PREFIXES.sub("", d).strip()
    return d


_EXPIRY_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def contract_expiry(description: str) -> date | None:
    """The expiration date named inside an option Description, if it has one."""
    m = _EXPIRY_RE.search(description or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


@dataclass
class Lot:
    quantity: float
    cash_per_unit: float      # signed: negative = cash paid, positive = credit received
    opened: date


def _absorb(lots: deque, qty: float, cash_per_unit: float, opened: date,
            blend: bool) -> None:
    """Add an opening lot to a book.

    Under average cost the book never holds more than one equity lot: a
    purchase is folded into the running blend, so every close downstream
    consumes it at the blended rate through the *same* FIFO walk, without
    needing to know which mode it is in. Under FIFO the lot is queued behind
    whatever is already there. Keeping the difference to this one function is
    what stops the two modes drifting apart — there is exactly one place a sale
    can be matched, and exactly one place a cost can be set.

    The blended lot keeps the *earliest* open date, since that is when the
    position began, and both the corporate-action and holdings paths read it.
    """
    if blend and lots:
        held = lots[0]
        total = held.quantity + qty
        if total > 1e-9:
            lots[0] = Lot(
                total,
                (held.quantity * held.cash_per_unit + qty * cash_per_unit) / total,
                min(held.opened, opened))
            return
    lots.append(Lot(qty, cash_per_unit, opened))


@dataclass
class Holding:
    """A still-open position in one instrument (or one option contract)."""
    key: str                  # ticker for equity; contract description for options
    instrument: str
    category: Category
    quantity: float
    cost_basis: float         # cash tied up; positive = paid out (a debit position)
    net_credit: float         # cash taken in; positive = received (a short/credit position)
    avg_price: float          # per-unit cash, absolute
    first_opened: date | None

    @property
    def is_credit_position(self) -> bool:
        """True for a position opened for a credit (a short option), where
        'cost basis' is really an obligation rather than money spent."""
        return self.net_credit > 0


@dataclass
class RealizedEvent:
    """One closing trade's realised P&L, dated at the close — so a monthly
    cumulative series can be built the same way every other category's is."""
    activity_date: date
    key: str
    instrument: str
    category: Category
    quantity: float
    amount: float


@dataclass
class PositionsResult:
    holdings: list[Holding]                       # still-open, sorted by key
    realized_events: list[RealizedEvent]
    realized_by_category: dict[Category, float]
    open_shares: float                            # total equity shares held
    open_equity_cost_basis: float
    open_options_net_cash: float
    warnings: list[str]
    # Cost basis surrendered by a corporate action that never reached any
    # incoming shares. Non-zero means open positions understate cost by
    # exactly this much — which is what the reconciliation banner is seeing.
    unmatched_corporate_action_basis: float = 0.0

    def realized(self, cat: Category) -> float:
        return self.realized_by_category.get(cat, 0.0)

    @property
    def equity_holdings(self) -> list[Holding]:
        return [h for h in self.holdings if h.category is Category.EQUITY]

    @property
    def option_holdings(self) -> list[Holding]:
        return [h for h in self.holdings if h.category is Category.OPTIONS]


def _position_key(c: Classified) -> str:
    """Equity positions are per ticker. Option positions are per *contract* —
    two AAPL calls at different strikes or expiries are different positions, and
    the (normalised) Description is what distinguishes them."""
    if c.category is Category.OPTIONS:
        return (normalise_contract(c.txn.description)
                or c.txn.instrument.strip() or "(unknown contract)")
    return c.txn.instrument.strip() or "(no symbol)"


def _phase(c: Classified) -> int:
    """
    Ordering within a single activity date.

    Statements carry a date but no time, and Robinhood exports newest-first, so
    file order is actively misleading: a same-day sell is listed *above* the buy
    that funded it. Sorting by file order made a same-day round trip look like a
    phantom short followed by a still-open position.

    Opens are therefore processed before closes on the same date — you cannot
    sell what you have not bought — and corporate-action legs run in between,
    outgoing before incoming, so surrendered cost basis exists before the
    replacement shares need it.
    """
    code = c.txn.trans_code.strip().upper()
    if c.category is Category.CORPORATE_ACTION:
        return 1 if c.txn.shares_removed else 2
    if code in OPENING_CODES.get(c.category, ()):
        return 0
    return 3


def compute_positions(classified: list[Classified], *,
                      full_history: list[Classified] | None = None,
                      cost_basis: CostBasis = CostBasis.AVERAGE) -> PositionsResult:
    """Match every closing trade against the lots it closes.

    `classified` is the range being *reported*. `full_history` is every row the
    statements carry, and is only ever used to tell one thing apart from
    another in the expiry warning: a contract with no closing row anywhere is a
    gap in the input, while a contract whose closing row is simply dated after
    the reported range is not a problem at all. Passing nothing leaves the two
    the same, which is exactly the unwindowed behaviour.

    `cost_basis` selects which open equity lot a sale consumes. It changes when
    P&L is recognised, not how much of it exists in total — see `CostBasis`.
    """
    blend_equity = cost_basis is CostBasis.AVERAGE
    tracked = [c for c in classified
               if c.category in OPENING_CODES or c.category is Category.CORPORATE_ACTION]
    tracked.sort(key=lambda c: (c.txn.activity_date, _phase(c),
                                 c.txn.source_file, c.txn.row_index))

    # Cost basis surrendered by corporate actions, per date, waiting to be
    # carried onto whatever shares arrive in exchange the same day.
    ca_basis_pool: dict[date, float] = {}
    ca_incoming_qty: dict[date, float] = {}
    # What actually reached incoming shares, and which positions gave it up —
    # so an undistributed remainder can be named rather than just vanishing.
    ca_basis_consumed: dict[date, float] = {}
    ca_basis_sources: dict[date, list[str]] = {}
    for c in tracked:
        if c.category is Category.CORPORATE_ACTION and not c.txn.shares_removed:
            if c.txn.quantity:
                d = c.txn.activity_date
                ca_incoming_qty[d] = ca_incoming_qty.get(d, 0.0) + abs(c.txn.quantity)
    tradeable = tracked

    books: dict[str, dict] = {}
    realized_events: list[RealizedEvent] = []
    realized_by_category: dict[Category, float] = {}
    warnings: list[str] = []

    for c in tradeable:
        txn, cat = c.txn, c.category
        code = txn.trans_code.strip().upper()
        key = _position_key(c)
        # A corporate action moves *equity* shares, so shares arriving from a
        # merger or split open an Equity position — the position's category is
        # never "Corporate action", which only describes the event.
        book_cat = Category.EQUITY if cat is Category.CORPORATE_ACTION else cat
        book = books.setdefault(key, {
            "lots": deque(), "instrument": txn.instrument.strip(), "category": book_cat,
        })

        qty = abs(txn.quantity) if txn.quantity else None
        if not qty:
            # No quantity means nothing can be matched. Rather than drop the
            # row or invent a lot, count its cash directly as realised.
            realized_by_category[cat] = realized_by_category.get(cat, 0.0) + txn.amount
            realized_events.append(RealizedEvent(
                txn.activity_date, key, txn.instrument, cat, 0.0, txn.amount))
            if code not in CASH_SETTLEMENT_CODES:
                warnings.append(
                    f"{key}: {txn.activity_date.isoformat()} {code} has no usable quantity "
                    f"— its {_money(txn.amount)} is counted as realised directly, since it "
                    f"cannot be matched to a specific lot")
            continue

        # --- corporate actions: move shares, carry basis, realise nothing ---
        if cat is Category.CORPORATE_ACTION:
            d = txn.activity_date
            if txn.shares_removed:
                # Surrender the lots and remember what they cost, so the
                # replacement shares inherit that basis instead of appearing
                # from nowhere at zero.
                removed_basis, remaining = 0.0, qty
                while remaining > 1e-9 and book["lots"]:
                    lot = book["lots"][0]
                    matched = min(lot.quantity, remaining)
                    removed_basis += -lot.cash_per_unit * matched
                    lot.quantity -= matched
                    remaining -= matched
                    if lot.quantity <= 1e-9:
                        book["lots"].popleft()
                ca_basis_pool[d] = ca_basis_pool.get(d, 0.0) + removed_basis
                if removed_basis > 1e-9:
                    ca_basis_sources.setdefault(d, []).append(key)
                if remaining > 1e-9:
                    warnings.append(
                        f"{key}: corporate action on {d.isoformat()} removed {remaining:g} "
                        f"more share(s) than these statements show as held — the original "
                        f"purchase likely predates the input window, so no cost basis "
                        f"carries forward for that portion")
            else:
                # Receive the replacement shares at the surrendered basis,
                # split across every incoming leg that day by share count.
                total_in = ca_incoming_qty.get(d, 0.0)
                pool = ca_basis_pool.get(d, 0.0)
                share = (qty / total_in) if total_in > 1e-9 else 1.0
                basis = pool * share
                # Shares received in exchange open a position like any other
                # purchase, so they blend the same way — otherwise a merger
                # would quietly leave a second lot behind in average mode.
                _absorb(book["lots"], qty, -basis / qty if qty else 0.0, d,
                        blend_equity)
                ca_basis_consumed[d] = ca_basis_consumed.get(d, 0.0) + basis
                if pool <= 1e-9:
                    warnings.append(
                        f"{key}: received {qty:g} share(s) via a corporate action on "
                        f"{d.isoformat()} with no surrendered cost basis to carry over — "
                        f"recorded at zero cost, so a later sale will overstate the gain")
            continue

        cash_per_unit = txn.amount / qty

        if code in OPENING_CODES[cat]:
            _absorb(book["lots"], qty, cash_per_unit, txn.activity_date,
                    blend_equity and cat is Category.EQUITY)
            continue

        if code in CLOSING_CODES[cat]:
            remaining, event_pnl = qty, 0.0
            while remaining > 1e-9 and book["lots"]:
                lot = book["lots"][0]
                matched = min(lot.quantity, remaining)
                event_pnl += (cash_per_unit + lot.cash_per_unit) * matched
                lot.quantity -= matched
                remaining -= matched
                if lot.quantity <= 1e-9:
                    book["lots"].popleft()
            if remaining > 1e-9:
                # Closed more than any open lot covers: the opening trade falls
                # outside these statements, or it's a short sale. Counted at
                # full proceeds with no cost basis, and flagged rather than
                # silently guessed at.
                event_pnl += cash_per_unit * remaining
                warnings.append(
                    f"{key}: closed {remaining:g} more unit(s) on "
                    f"{txn.activity_date.isoformat()} than any open lot in these "
                    f"statements covers — counted at full proceeds with no cost "
                    f"basis; the opening trade likely predates the input window")
            realized_by_category[cat] = realized_by_category.get(cat, 0.0) + event_pnl
            realized_events.append(RealizedEvent(
                txn.activity_date, key, txn.instrument, cat, qty, event_pnl))
            continue

        # A tradeable-category code that is neither opening nor closing.
        realized_by_category[cat] = realized_by_category.get(cat, 0.0) + txn.amount
        realized_events.append(RealizedEvent(
            txn.activity_date, key, txn.instrument, cat, qty, txn.amount))
        warnings.append(
            f"{key}: trans code {code!r} on {txn.activity_date.isoformat()} is neither "
            f"an opening nor a closing code for {cat.value}; its {_money(txn.amount)} "
            f"is counted as realised directly")

    # Surrendered basis reaches the replacement shares only if an incoming leg
    # arrives the same day. A cash merger, a delisting, or an incoming leg that
    # lands in a later statement leaves the pool with nowhere to go. Dropping it
    # silently understates open cost by exactly that amount, which surfaces much
    # later as a reconciliation banner with no stated cause — so state the cause.
    unmatched_ca_basis = 0.0
    for d, pool in sorted(ca_basis_pool.items()):
        residual = pool - ca_basis_consumed.get(d, 0.0)
        if residual <= 1e-9:
            continue
        unmatched_ca_basis += residual
        names = ", ".join(ca_basis_sources.get(d, ())) or "(unknown position)"
        warnings.append(
            f"{names}: corporate action on {d.isoformat()} surrendered "
            f"{_money(residual)} of cost basis but no shares arrived in exchange that "
            f"day — the incoming leg is missing from these statements, or the position "
            f"was closed out for cash. That basis is not carried forward, so open "
            f"positions understate cost by {_money(residual)}")

    holdings: list[Holding] = []
    open_shares = open_equity_cost = open_options_cash = 0.0
    for key, book in books.items():
        qty = sum(l.quantity for l in book["lots"])
        if qty <= 1e-9:
            continue
        net_cash = sum(l.quantity * l.cash_per_unit for l in book["lots"])
        cat = book["category"]
        holdings.append(Holding(
            key=key, instrument=book["instrument"], category=cat, quantity=qty,
            cost_basis=max(-net_cash, 0.0), net_credit=max(net_cash, 0.0),
            avg_price=abs(net_cash) / qty,
            first_opened=min((l.opened for l in book["lots"]), default=None)))
        if cat is Category.EQUITY:
            open_shares += qty
            open_equity_cost += -net_cash
        else:
            open_options_cash += net_cash

    # An option still open past its own expiration date usually means the
    # statements are missing its closing row — exactly the failure that leaves a
    # long-dead contract sitting in "open positions". But "past its expiration"
    # has to be judged against the end of the range being *reported*, not
    # against the last row on file: a contract that has not expired yet at the
    # reported end is simply an open position, and one whose closing row is
    # merely dated later is not a gap in the input at all. Both of those read as
    # missing data if the two dates are conflated, so keep them apart.
    as_of = max((c.txn.activity_date for c in classified), default=None)
    closes_after: dict[str, date] = {}
    if full_history is not None and as_of is not None:
        for c in full_history:
            if c.category is not Category.OPTIONS:
                continue
            if c.txn.trans_code.strip().upper() not in CLOSING_CODES[Category.OPTIONS]:
                continue
            if c.txn.activity_date <= as_of:
                continue
            k = _position_key(c)
            if k not in closes_after or c.txn.activity_date < closes_after[k]:
                closes_after[k] = c.txn.activity_date
    if as_of:
        for h in holdings:
            if h.category is not Category.OPTIONS:
                continue
            exp = contract_expiry(h.key)
            if not (exp and exp < as_of):
                continue
            amount = _money(h.net_credit or -h.cost_basis)
            closed = closes_after.get(h.key)
            if closed:
                warnings.append(
                    f"{h.key}: expired {exp.isoformat()}, and its closing row is dated "
                    f"{closed.isoformat()} — after {as_of.isoformat()}, the end of the "
                    f"range being reported. It is correctly shown as open as of that "
                    f"date, and its {amount} is realised later, outside this range")
            else:
                warnings.append(
                    f"{h.key}: expired {exp.isoformat()} but no closing row (OEXP/OASGN/"
                    f"STC/BTC) appears in these statements, so it is still counted as "
                    f"open — its {amount} has not been realised. The closing row is "
                    f"probably outside the input window")

    holdings.sort(key=lambda h: (h.category.value, h.key))
    realized_events.sort(key=lambda e: e.activity_date)

    return PositionsResult(
        holdings=holdings,
        realized_events=realized_events,
        realized_by_category=realized_by_category,
        open_shares=open_shares,
        open_equity_cost_basis=open_equity_cost,
        open_options_net_cash=open_options_cash,
        warnings=warnings,
        unmatched_corporate_action_basis=unmatched_ca_basis,
    )


def _money(v: float) -> str:
    return f"{'-' if v < 0 else ''}${abs(v):,.2f}"
