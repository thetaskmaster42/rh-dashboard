"""
Turn classified transactions + matched positions into the income statement the
dashboard renders.

Two different totals live here and they answer different questions:

  **Net Income** — did this account make money? Realised equity P&L + realised
  options P&L + dividends/interest - fees - margin interest - Gold. Open
  positions are excluded (they are holdings, not gains); deposits and
  withdrawals are excluded (financing, not profit).

  **Total cash movement** — how did the cash balance change? Every Amount in
  the file, summed. Includes the money spent building open positions and the
  money moved in from the bank.

They reconcile exactly, and `compute()` asserts it:

    Net Income - open equity cost basis + open options net cash
               + deposits + withdrawals + corporate action cash
               ==  total cash movement

That last term is normally zero: a merger or split moves shares, not money, so
those rows carry a blank Amount. But a cash-plus-stock merger or cash-in-lieu
settlement does move money, and corporate actions reach neither Net Income nor
transfers — so leaving the term out made a true statement about a real export
fail to reconcile for a reason the banner could not name. It is a named term
here rather than an assumption nothing checks.

That identity is the proof the split is honest: nothing is double-counted and
nothing is dropped. If it ever fails, `reconciliation_error` is non-zero and
the dashboard says so rather than quietly showing numbers that don't add up.

Everything here is still **realised cash**, never mark-to-market: a statement
export carries no live price, so an open position is reported at what it cost,
never at what it is worth today. See the dashboard footer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .model import (CATEGORY_ORDER, FALLBACK_CATEGORIES, INCOME_CATEGORIES,
                    LOT_MATCHED_CATEGORIES, TRANSFER_CATEGORIES, Category,
                    Classified, Window)
from .positions import PositionsResult


def month_key(d) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _month_range(start_key: str, end_key: str) -> list[str]:
    sy, sm = (int(x) for x in start_key.split("-"))
    ey, em = (int(x) for x in end_key.split("-"))
    out, y, m = [], sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return out


@dataclass
class CategorySummary:
    category: Category
    cash_total: float          # raw sum of Amount — what actually moved
    income_total: float        # what reaches Net Income (realised, for lot-matched cats)
    count: int
    monthly_income: dict[str, float]
    income_cumulative: list[float]   # aligned to Metrics.months

    @property
    def is_lot_matched(self) -> bool:
        return self.category in LOT_MATCHED_CATEGORIES

    @property
    def unrealized_gap(self) -> float:
        """cash_total - income_total: for Equity/Options, the cash currently
        tied up in open positions. Zero for every other category."""
        return self.cash_total - self.income_total


@dataclass
class TradeStats:
    """Closed trades in the reported period.

    **A trade is one closing event.** A sale that closes half a position counts
    once, on the day it settles the P&L; an option counts when it closes,
    whether that is an early buy-to-close, a sell-to-close, or expiry. That is
    already exactly what `positions.realized_events` contains, so this is a
    reading of the engine rather than a second opinion about what a trade is.

    Opening a position is not a trade here and never appears: buying stock
    realizes nothing, and a sold-to-open contract realizes nothing until it is
    closed or expires. A rollover therefore counts as one trade — the leg that
    closed — while the new contract stays open and uncounted.
    """
    wins: list[float]
    losses: list[float]
    scratches: list[float]            # closed at exactly zero

    @property
    def count(self) -> int:
        return len(self.wins) + len(self.losses) + len(self.scratches)

    @property
    def win_rate(self) -> float:
        """Share of closed trades that made money, 0-100. Scratches count in
        the denominator: a trade that closed flat still happened."""
        return 100.0 * len(self.wins) / self.count if self.count else 0.0

    @property
    def avg_win(self) -> float:
        return sum(self.wins) / len(self.wins) if self.wins else 0.0

    @property
    def avg_loss(self) -> float:
        return sum(self.losses) / len(self.losses) if self.losses else 0.0

    @property
    def total(self) -> float:
        return sum(self.wins) + sum(self.losses) + sum(self.scratches)

    @property
    def best(self) -> float:
        return max(self.wins, default=0.0)

    @property
    def worst(self) -> float:
        return min(self.losses, default=0.0)


@dataclass
class TickerSummary:
    """What one ticker contributed to net income, and what is still held in it.

    `ticker` is empty for the **Unattributed** bucket, which is not a rounding
    device: margin interest, account fees, the Gold subscription and stock
    lending income are account-level and belong to no ticker. Forcing them onto
    one would be a guess, and this project does not guess — so they are named
    and shown, and the identity below still closes.

        sum(net_contribution over every ticker) + unattributed == net_income

    That identity is the check that attribution neither double-counted nor
    dropped anything, in the same spirit as the cash reconciliation.
    """
    ticker: str
    realized_equity: float
    realized_options: float
    dividends: float
    other_income: float               # every remaining income category
    shares: float
    cost_basis: float
    avg_price: float
    first_opened: date | None

    @property
    def net_contribution(self) -> float:
        return (self.realized_equity + self.realized_options
                + self.dividends + self.other_income)

    @property
    def is_unattributed(self) -> bool:
        return not self.ticker


@dataclass
class FallbackSummary:
    count: int
    by_code: dict[str, int]


@dataclass
class Metrics:
    months: list[str]
    categories: dict[Category, CategorySummary]
    net_income: float
    net_income_cumulative: list[float]
    total_cash_movement: float
    net_transfers: float
    corporate_action_cash: float       # normally 0.00 — see the module docstring
    other_income: float                # unclassified rows, folded into Net Income
    other_count: int
    open_shares: float
    open_equity_cost_basis: float
    open_options_net_cash: float
    reconciliation_error: float        # ~0 unless something is double-counted
    # The position state the window opened with. All zero without a window,
    # which is what makes the delta identity collapse to the unwindowed one.
    opening_shares: float
    opening_equity_cost_basis: float
    opening_options_net_cash: float
    window: Window | None
    # The span of the statements themselves. Distinct from `date_range`, which
    # under a window is the window. The difference is what lets the page say
    # "matched over all of this, reported over that".
    full_range: tuple[str, str] | None
    by_ticker: list[TickerSummary]
    unattributed: TickerSummary
    trades: TradeStats
    daily_realized: list[tuple[str, float]]   # one entry per day in the range
    txn_count: int
    date_range: tuple[str, str] | None
    fallback: FallbackSummary
    duplicates_removed: int
    row_errors: list[str]

    @property
    def ticker_attribution_error(self) -> float:
        """Every ticker's contribution plus the account-level bucket, against
        net income. Non-zero means attribution dropped or double-counted
        something — the same class of bug the cash reconciliation catches."""
        return (sum(t.net_contribution for t in self.by_ticker)
                + self.unattributed.net_contribution) - self.net_income

    @property
    def reconciles(self) -> bool:
        return abs(self.reconciliation_error) < 0.01

    def income_of(self, cat: Category) -> float:
        return self.categories[cat].income_total


_TICKER_INCOME_CATEGORIES = tuple(
    c for c in INCOME_CATEGORIES if c not in LOT_MATCHED_CATEGORIES
) + FALLBACK_CATEGORIES


def _by_ticker(classified: list[Classified], positions: PositionsResult
               ) -> tuple[list[TickerSummary], TickerSummary]:
    """Split net income by ticker, with everything account-level named separately.

    Attribution reads the **Instrument** column, not the description. A
    dividend row carries its ticker there (`SPY`, `AAPL`), and so does an
    option row — where Instrument is the *underlying*, which is exactly the
    grouping wanted, and which `RealizedEvent.instrument` already propagates.
    That makes the pattern-matching this was expected to need unnecessary; an
    empty Instrument is an account-level row and lands in Unattributed on its
    own, rather than by a rule that could mis-fire on a real export.

    Lot-matched categories come from realized events, never from raw cash —
    the same rule as everywhere else, or an open position would read as a loss.
    Transfers and corporate actions are excluded because neither is income.
    """
    acc: dict[str, dict] = {}

    def bucket(name: str) -> dict:
        return acc.setdefault(name, {
            "realized_equity": 0.0, "realized_options": 0.0,
            "dividends": 0.0, "other_income": 0.0,
            "shares": 0.0, "cost_basis": 0.0, "first_opened": None,
        })

    for e in positions.realized_events:
        if e.category is Category.EQUITY:
            bucket(e.instrument.strip())["realized_equity"] += e.amount
        elif e.category is Category.OPTIONS:
            bucket(e.instrument.strip())["realized_options"] += e.amount

    for c in classified:
        if c.category not in _TICKER_INCOME_CATEGORIES:
            continue
        b = bucket(c.txn.instrument.strip())
        key = ("dividends" if c.category is Category.DIVIDENDS_INTEREST
               else "other_income")
        b[key] += c.txn.amount

    for h in positions.equity_holdings:
        b = bucket((h.instrument or h.key).strip())
        b["shares"] += h.quantity
        b["cost_basis"] += h.cost_basis
        if h.first_opened and (b["first_opened"] is None
                               or h.first_opened < b["first_opened"]):
            b["first_opened"] = h.first_opened

    def build(name: str, d: dict) -> TickerSummary:
        return TickerSummary(
            ticker=name,
            realized_equity=d["realized_equity"],
            realized_options=d["realized_options"],
            dividends=d["dividends"],
            other_income=d["other_income"],
            shares=d["shares"],
            cost_basis=d["cost_basis"],
            avg_price=(d["cost_basis"] / d["shares"]) if d["shares"] else 0.0,
            first_opened=d["first_opened"])

    unattributed = build("", acc.pop("", {
        "realized_equity": 0.0, "realized_options": 0.0, "dividends": 0.0,
        "other_income": 0.0, "shares": 0.0, "cost_basis": 0.0,
        "first_opened": None}))
    rows = [build(name, d) for name, d in acc.items()]
    # Biggest contributor first; ticker breaks ties so the order is stable.
    rows.sort(key=lambda t: (-t.net_contribution, t.ticker))
    return rows, unattributed


def _trade_stats(positions: PositionsResult) -> TradeStats:
    wins, losses, scratches = [], [], []
    for e in positions.realized_events:
        if e.amount > 0.005:
            wins.append(e.amount)
        elif e.amount < -0.005:
            losses.append(e.amount)
        else:
            scratches.append(e.amount)
    return TradeStats(wins=wins, losses=losses, scratches=scratches)


def _daily_realized(positions: PositionsResult, start: date, end: date
                    ) -> list[tuple[str, float]]:
    """Realized P&L per calendar day, one entry per day in the range.

    Days with no closing trade are present at 0.00 rather than absent. A bar
    chart built from only the active days compresses a quiet fortnight into
    nothing and makes two adjacent trades look consecutive — the empty days are
    part of what the chart is saying.
    """
    by_day: dict[date, float] = {}
    for e in positions.realized_events:
        by_day[e.activity_date] = by_day.get(e.activity_date, 0.0) + e.amount
    out, d = [], start
    while d <= end:
        out.append((d.isoformat(), by_day.get(d, 0.0)))
        d += timedelta(days=1)
    return out


def _empty(duplicates_removed: int, row_errors: list[str]) -> Metrics:
    return Metrics(
        months=[],
        categories={c: CategorySummary(c, 0.0, 0.0, 0, {}, []) for c in CATEGORY_ORDER},
        net_income=0.0, net_income_cumulative=[], total_cash_movement=0.0,
        net_transfers=0.0, corporate_action_cash=0.0,
        other_income=0.0, other_count=0, open_shares=0.0,
        open_equity_cost_basis=0.0, open_options_net_cash=0.0,
        reconciliation_error=0.0,
        opening_shares=0.0, opening_equity_cost_basis=0.0,
        opening_options_net_cash=0.0, window=None, full_range=None,
        by_ticker=[], unattributed=TickerSummary("", 0.0, 0.0, 0.0, 0.0,
                                                 0.0, 0.0, 0.0, None),
        trades=TradeStats([], [], []), daily_realized=[],
        txn_count=0, date_range=None,
        fallback=FallbackSummary(0, {}), duplicates_removed=duplicates_removed,
        row_errors=row_errors)


def compute(classified: list[Classified], positions: PositionsResult,
            duplicates_removed: int, row_errors: list[str], *,
            window: Window | None = None,
            opening: PositionsResult | None = None,
            full_range: tuple[str, str] | None = None) -> Metrics:
    """Build the income statement.

    `classified` is the rows *inside* the window; `positions` is the result
    matched over full history, reported as of the window end and filtered to
    the window with `PositionsResult.windowed`. `opening` is the same engine run
    over everything strictly before the window — its final state is the state
    this window opened with.

    Both are keyword-only and `opening` is a whole `PositionsResult` rather than
    two loose floats, so the pair cannot be transposed and the opening share
    count is available for display.

    With no window the opening state is all zeros and every formula below
    collapses to exactly what it was before windows existed — there is no
    `if window is None` branch in the arithmetic.
    """
    # A window with nothing in it is a legitimate report — "nothing happened
    # this period, here is what you were holding" — and _empty would answer it
    # with zeros for holdings that really are still there.
    if not classified and window is None:
        return _empty(duplicates_removed, row_errors)

    dates = [c.txn.activity_date for c in classified]
    if window is not None:
        # Derived from the window, not from the rows: a quiet first or last
        # month would otherwise shrink the axis and make two reports over the
        # same period incomparable.
        months = _month_range(month_key(window.start), month_key(window.end))
    else:
        months = _month_range(month_key(min(dates)), month_key(max(dates)))

    cash_total: dict[Category, float] = {c: 0.0 for c in CATEGORY_ORDER}
    counts: dict[Category, int] = {c: 0 for c in CATEGORY_ORDER}
    monthly_cash: dict[Category, dict[str, float]] = {c: {} for c in CATEGORY_ORDER}
    fallback_codes: dict[str, int] = {}
    fallback_count = 0

    for c in classified:
        mk = month_key(c.txn.activity_date)
        cash_total[c.category] += c.txn.amount
        counts[c.category] += 1
        monthly_cash[c.category][mk] = monthly_cash[c.category].get(mk, 0.0) + c.txn.amount
        if c.fallback:
            fallback_count += 1
            fallback_codes[c.txn.trans_code] = fallback_codes.get(c.txn.trans_code, 0) + 1

    # Lot-matched categories take their monthly income from realised events
    # (dated at the close), not from raw cash (dated at every buy and sell).
    monthly_realized: dict[Category, dict[str, float]] = {
        c: {} for c in LOT_MATCHED_CATEGORIES}
    for ev in positions.realized_events:
        mk = month_key(ev.activity_date)
        bucket = monthly_realized.setdefault(ev.category, {})
        bucket[mk] = bucket.get(mk, 0.0) + ev.amount

    categories: dict[Category, CategorySummary] = {}
    for cat in CATEGORY_ORDER:
        if cat in LOT_MATCHED_CATEGORIES:
            income = positions.realized(cat)
            monthly_income = monthly_realized.get(cat, {})
        else:
            income = cash_total[cat]
            monthly_income = monthly_cash[cat]
        running, cumulative = 0.0, []
        for mk in months:
            running += monthly_income.get(mk, 0.0)
            cumulative.append(running)
        categories[cat] = CategorySummary(
            category=cat, cash_total=cash_total[cat], income_total=income,
            count=counts[cat], monthly_income=monthly_income,
            income_cumulative=cumulative)

    other_income = sum(categories[c].income_total for c in FALLBACK_CATEGORIES)
    other_count = sum(categories[c].count for c in FALLBACK_CATEGORIES)
    net_income = sum(categories[c].income_total for c in INCOME_CATEGORIES) + other_income

    net_income_cumulative = []
    for i in range(len(months)):
        net_income_cumulative.append(
            sum(categories[c].income_cumulative[i] for c in INCOME_CATEGORIES)
            + sum(categories[c].income_cumulative[i] for c in FALLBACK_CATEGORIES))

    open_eq_start = opening.open_equity_cost_basis if opening else 0.0
    open_opt_start = opening.open_options_net_cash if opening else 0.0
    open_shares_start = opening.open_shares if opening else 0.0

    total_cash_movement = sum(cash_total.values())
    net_transfers = sum(categories[c].cash_total for c in TRANSFER_CATEGORIES)
    # Cash-neutral by convention, not by guarantee. Summed explicitly so a
    # cash merger shifts a named row rather than the error term.
    corporate_action_cash = cash_total[Category.CORPORATE_ACTION]

    # Net Income minus what's parked in open positions plus financing must equal
    # the raw cash total. See the module docstring.
    # A *delta* identity: cash that moved equals income earned, less the change
    # in capital tied up in positions, plus financing. Without a window the
    # opening terms are zero and this is the original identity unchanged.
    expected_cash = (net_income
                     - (positions.open_equity_cost_basis - open_eq_start)
                     + (positions.open_options_net_cash - open_opt_start)
                     + net_transfers
                     + corporate_action_cash)
    reconciliation_error = total_cash_movement - expected_cash

    by_ticker, unattributed = _by_ticker(classified, positions)
    trades = _trade_stats(positions)
    # The bar chart spans the range being *reported*, so an empty window still
    # draws its own days rather than collapsing to nothing.
    span_start = window.start if window else min(dates)
    span_end = window.end if window else max(dates)
    daily_realized = _daily_realized(positions, span_start, span_end)

    return Metrics(
        months=months, categories=categories, net_income=net_income,
        net_income_cumulative=net_income_cumulative,
        total_cash_movement=total_cash_movement, net_transfers=net_transfers,
        corporate_action_cash=corporate_action_cash,
        other_income=other_income, other_count=other_count,
        open_shares=positions.open_shares,
        open_equity_cost_basis=positions.open_equity_cost_basis,
        open_options_net_cash=positions.open_options_net_cash,
        reconciliation_error=reconciliation_error,
        opening_shares=open_shares_start,
        opening_equity_cost_basis=open_eq_start,
        opening_options_net_cash=open_opt_start,
        window=window,
        full_range=full_range,
        by_ticker=by_ticker,
        unattributed=unattributed,
        trades=trades,
        daily_realized=daily_realized,
        txn_count=len(classified),
        date_range=((window.start.isoformat(), window.end.isoformat()) if window
                    else (min(dates).isoformat(), max(dates).isoformat())
                    if dates else None),
        fallback=FallbackSummary(fallback_count,
                                 dict(sorted(fallback_codes.items(), key=lambda kv: -kv[1]))),
        duplicates_removed=duplicates_removed, row_errors=row_errors)
