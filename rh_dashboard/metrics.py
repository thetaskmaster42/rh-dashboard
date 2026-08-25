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
    txn_count: int
    date_range: tuple[str, str] | None
    fallback: FallbackSummary
    duplicates_removed: int
    row_errors: list[str]

    @property
    def reconciles(self) -> bool:
        return abs(self.reconciliation_error) < 0.01

    def income_of(self, cat: Category) -> float:
        return self.categories[cat].income_total


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
        opening_options_net_cash=0.0, window=None,
        txn_count=0, date_range=None,
        fallback=FallbackSummary(0, {}), duplicates_removed=duplicates_removed,
        row_errors=row_errors)


def compute(classified: list[Classified], positions: PositionsResult,
            duplicates_removed: int, row_errors: list[str], *,
            window: Window | None = None,
            opening: PositionsResult | None = None) -> Metrics:
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
        txn_count=len(classified),
        date_range=((window.start.isoformat(), window.end.isoformat()) if window
                    else (min(dates).isoformat(), max(dates).isoformat())
                    if dates else None),
        fallback=FallbackSummary(fallback_count,
                                 dict(sorted(fallback_codes.items(), key=lambda kv: -kv[1]))),
        duplicates_removed=duplicates_removed, row_errors=row_errors)
