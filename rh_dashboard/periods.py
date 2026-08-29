"""
Pre-computed period views: 1m, 3m, 1y, all time.

## Why these are built at build time

The page is a single self-contained file that has to work when double-clicked
from a downloads folder with no server behind it. A period button therefore
cannot ask anything to recompute — so every period is worked out during the
build and embedded, and the button switches between answers that already
exist. It is also why this holds a small flat summary per period rather than a
whole `Metrics`: four full reports would bloat the file for data the summary
view never shows.

## The anchor

Periods run **backwards from the last activity date in the statements**, not
from today. A statement export is historic; anchoring "1m" to today would
report an empty window and four zeroed rings for anyone opening last quarter's
dashboard. The page states the anchor so the reader is never guessing which
month "1m" means.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .metrics import TradeStats, _daily_realized, _trade_stats
from .model import Classified, CostBasis, Window
from .positions import compute_windowed

_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December")


def _months_before(d: date, months: int) -> date:
    """`d` minus `months` calendar months, clamped to a real day.

    Subtracting a month from the 31st has to land somewhere; the last day of
    the shorter month is the answer every calendar app gives, and it keeps
    consecutive windows from overlapping by a day.
    """
    total = (d.year * 12 + (d.month - 1)) - months
    year, month = divmod(total, 12)
    month += 1
    if month == 12:
        last = 31
    else:
        last = (date(year + (month == 12), month % 12 + 1, 1) - date.resolution).day
    return date(year, month, min(d.day, last))


@dataclass
class PeriodView:
    key: str                                  # "1m", "3m", "1y", "at"
    label: str
    title: str                                # "Jul 2026", or a year span
    start: date
    end: date
    trades: TradeStats
    daily: list[tuple[str, float]]
    events: list[dict] = field(default_factory=list)

    @property
    def profit(self) -> float:
        """Realized P&L on trades closed in the period.

        Deliberately the trade figure, not net income: these rings are about
        trading, and folding in the Gold subscription would make a month with
        no trades in it show a loss it did not trade its way into.
        """
        return self.trades.total

    @property
    def cumulative(self) -> list[tuple[str, float]]:
        out, run = [], 0.0
        for day, v in self.daily:
            run += v
            out.append((day, run))
        return out


def compute_periods(classified: list[Classified], *,
                    cost_basis: CostBasis = CostBasis.AVERAGE) -> list[PeriodView]:
    dates = [c.txn.activity_date for c in classified]
    if not dates:
        return []
    anchor, first = max(dates), min(dates)

    spans: list[tuple[str, date, date]] = [
        ("1m", _months_before(anchor, 1), anchor),
        ("3m", _months_before(anchor, 3), anchor),
        ("1y", _months_before(anchor, 12), anchor),
        ("at", first, anchor),
    ]

    views = []
    for key, start, end in spans:
        # Never claim to cover more than the statements do: a 1y window over
        # two months of data would draw ten empty months and make the period
        # look like a drawdown.
        start = max(start, first)
        window = Window(start, end)
        reported, _ = compute_windowed(classified, window, cost_basis=cost_basis)
        views.append(PeriodView(
            key=key,
            label=key,
            title=_title(start, end),
            start=start,
            end=end,
            trades=_trade_stats(reported),
            daily=_daily_realized(reported, start, end),
            events=[{"date": e.activity_date.isoformat(), "key": e.key,
                     "instrument": e.instrument, "category": e.category.value,
                     "quantity": e.quantity, "amount": round(e.amount, 2)}
                    for e in reported.realized_events],
        ))
    return views


def _title(start: date, end: date) -> str:
    if start.year == end.year and start.month == end.month:
        return f"{_MONTH_NAMES[end.month - 1][:3]} {end.year}"
    if start.year == end.year:
        return (f"{_MONTH_NAMES[start.month - 1][:3]}–"
                f"{_MONTH_NAMES[end.month - 1][:3]} {end.year}")
    return f"{start.year}–{end.year}"
