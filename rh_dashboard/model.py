"""
Core data model: Transaction, Category, and the category groupings that decide
how each category is aggregated.

One Transaction per statement row, normalised across whatever header spelling
the source CSV used. Nothing here talks to a filesystem or a chart — that's
`loader.py` and `render.py`.

## The central modelling decision

A Robinhood statement is a **cash ledger**, but what a reader wants is an
**income statement**. The two disagree in one important way: buying a stock is
not an expense. It converts $X of cash into $X of stock — your net worth is
unchanged. Summing raw Buy/Sell cash per category (what this project did
originally) makes any account holding open positions look like it is losing
money, which is why the categories below are split three ways rather than
just totalled:

  INCOME_CATEGORIES     real profit and loss — these sum to Net Income
  TRANSFER_CATEGORIES   financing; money moved in/out, not made or lost
  FALLBACK_CATEGORIES   unrecognised trans codes, conservatively left in income

and, orthogonally:

  LOT_MATCHED_CATEGORIES  Equity and Options, where the income figure is NOT
                          the raw cash sum but FIFO-matched realised P&L on
                          *closed* lots only (see `positions.py`). What's still
                          open is reported separately as cost basis, because
                          it is a holding, not a gain or a loss.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class Category(str, Enum):
    EQUITY = "Equity"
    OPTIONS = "Options"
    DIVIDENDS_INTEREST = "Dividends/Interest"
    FEES = "Fees"
    MARGIN = "Margin"
    GOLD = "Gold"
    DEPOSITS = "Deposits"
    WITHDRAW = "Withdraw"
    # Mergers, share exchanges, splits, spinoffs. Move shares without moving
    # cash, so they contribute nothing to income — but they open and close
    # positions, which is why they are a category rather than being ignored.
    CORPORATE_ACTION = "Corporate action"
    # last-resort fallback for a trans code none of the rules recognise
    DEBITS = "Debits"
    CREDITS = "Credits"

    def __str__(self) -> str:  # noqa: D105 - plain label in reports
        return self.value


@dataclass(frozen=True)
class Window:
    """An inclusive date range to *report* over.

    A window never changes how lots are matched — only which results are shown.
    Slicing rows to the window and then matching would destroy cost basis: in
    `sample_data` AAPL is bought in June and part-sold in July, so a July-only
    match turns a real +$1,250 gain into a +$9,500 fiction plus a spurious
    oversell warning. Lots are always matched over full history; the window
    selects what is reported and as of when.

    **Both ends are inclusive, and both key on `activity_date`** — the same
    field the sort key and `_phase` use. Filtering on `process_date` or
    `settle_date` would split a same-day corporate-action pair across the
    boundary, and since the surrendered basis is handed over by date, the
    outgoing leg's cost would land outside the window and simply vanish.
    """
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(
                f"window ends before it starts: {self.start.isoformat()} "
                f"to {self.end.isoformat()}")

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end

    def before(self, d: date) -> bool:
        """True for a date strictly earlier than the window — the baseline
        pass, whose final state is the window's opening state."""
        return d < self.start

    def as_of(self, d: date) -> bool:
        """True for anything at or before the window end — the as-of-end pass,
        which carries every pre-window lot at its real cost."""
        return d <= self.end

    @property
    def label(self) -> str:
        return f"{self.start.isoformat()} to {self.end.isoformat()}"


class CostBasis(str, Enum):
    """Which open lot a sale consumes.

    `AVERAGE` blends every open equity lot into one running cost, so a sale
    realises against the blend. `FIFO` consumes the oldest lot first — the IRS
    default absent a specific-identification election, and what Robinhood's own
    1099-B reports.

    The two differ only in *timing*. Over a position that fully closes they
    realise identical lifetime P&L; while it is open they split differently
    between what is realised and what is still held. That split is precisely
    what a date window makes visible, which is why the mode is selectable
    rather than assumed — and why the page has to say which one produced it.
    Two dashboards built with different settings look identical and disagree.

    Options ignore this entirely: a short-to-open contract has no purchase
    price to average, and two strikes on the same underlying must never blend.
    """
    AVERAGE = "average"
    FIFO = "fifo"


# Fixed display order. Every table/chart iterates categories in this order so a
# category's colour slot never shifts when one category happens to be empty.
# The first eight get their own categorical hue; DEBITS/CREDITS are the
# fallback tail and render folded into a single muted "Other" swatch.
CATEGORY_ORDER = (
    Category.EQUITY, Category.OPTIONS, Category.DIVIDENDS_INTEREST,
    Category.FEES, Category.MARGIN, Category.GOLD,
    Category.DEPOSITS, Category.WITHDRAW,
    Category.CORPORATE_ACTION, Category.DEBITS, Category.CREDITS,
)

PRIMARY_CATEGORIES = CATEGORY_ORDER[:8]                    # one categorical hue each
FALLBACK_CATEGORIES = (Category.DEBITS, Category.CREDITS)  # folded to "Other"

# Cash-neutral by definition: these rows carry no Amount. They are excluded
# from income and from every chart, and appear only in the transaction table
# and in what they do to positions.
NON_CASH_CATEGORIES = (Category.CORPORATE_ACTION,)

# Categories whose totals sum to Net Income. Equity and Options enter this sum
# as FIFO realised P&L, not raw cash — see LOT_MATCHED_CATEGORIES.
INCOME_CATEGORIES = (
    Category.EQUITY, Category.OPTIONS, Category.DIVIDENDS_INTEREST,
    Category.FEES, Category.MARGIN, Category.GOLD,
)

# Financing activity: money moved between the bank and the account. Excluded
# from Net Income — depositing your own money is not profit.
TRANSFER_CATEGORIES = (Category.DEPOSITS, Category.WITHDRAW)

# Categories where a position can be *open*, so the raw cash sum is not P&L.
# `positions.py` FIFO-matches these; only closed lots reach Net Income.
LOT_MATCHED_CATEGORIES = (Category.EQUITY, Category.OPTIONS)


@dataclass(frozen=True)
class Transaction:
    activity_date: date
    process_date: date | None
    settle_date: date | None
    instrument: str
    description: str
    trans_code: str
    quantity: float | None
    price: float | None
    amount: float
    source_file: str          # which CSV this row came from, for traceability
    row_index: int             # line number within source_file (1-based, header excluded)
    quantity_suffix: str = ""  # trailing letter on Quantity; "S" = shares leaving

    @property
    def shares_removed(self) -> bool:
        """
        True when this row takes shares *out* of the account.

        Corporate actions come in same-day pairs — Robinhood writes the
        outgoing leg's quantity as "200S" and the incoming leg's as "200":

            7/26/2021  SXCH  CCIV  200S   <- 200 CCIV shares surrendered
            7/26/2021  SXCH  LCID  200    <- 200 LCID shares received

        The suffix is the only thing distinguishing the two directions, since
        both legs carry an empty Amount.
        """
        return self.quantity_suffix == "S"

    def dedupe_key(self) -> tuple:
        """
        Identity for duplicate detection. Deliberately excludes `source_file`
        and `row_index` — the whole point is that the same trade line appears
        verbatim in two overlapping monthly exports and should collapse to one.

        Amount and price are rounded to the cent; Robinhood's own CSVs are
        already cent-precise, so this only guards against float noise.

        `quantity_suffix` is part of the identity: the two legs of a corporate
        action can land on the same date with the same instrument, quantity and
        an empty amount, differing *only* by the "S" — collapsing them would
        silently delete one side of a merger.
        """
        return (
            self.activity_date, self.process_date, self.settle_date,
            self.instrument, self.description, self.trans_code,
            self.quantity, self.quantity_suffix,
            round(self.price, 2) if self.price is not None else None,
            round(self.amount, 2),
        )


@dataclass
class Classified:
    """A Transaction plus the category it was sorted into and why."""
    txn: Transaction
    category: Category
    reason: str
    fallback: bool   # True when no rule matched and sign-based bucketing kicked in
