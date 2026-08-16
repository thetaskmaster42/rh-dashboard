"""
Rule-based categorisation into eleven buckets: Equity, Options, Margin, Fees,
Dividends/Interest, Gold, Deposits, Withdraw, Corporate action, and the
Debits/Credits fallback.

The code sets below were checked against a real 556-row Robinhood export, and
the less obvious ones are worth naming: **MINT** is "Aggregated Margin Rate"
(margin interest, not a transfer), **DFEE** is a sponsored-ADR processing fee,
**CIL** is cash in lieu of a fractional share, **GDBP** is a Gold deposit-boost
payment (income, which nets against the Gold subscription cost), and
**MRGS/SXCH/SPR** are share-moving corporate actions that carry no Amount at
all.

The rules below are the commonly-documented Robinhood "Trans Code" vocabulary,
**not verified against a live account** (see `loader.py`'s header note — same
constraint). Every classification carries a `reason` string that the dashboard
can show, and anything that falls through the explicit rules is flagged as a
fallback rather than silently trusted:

  1. Options  — an options trans code (BTO/STO/BTC/STC/OEXP/OASGN/OEXER/OCA),
     or a Buy/Sell whose Description reads like an option contract
     ("... Call $150.00" / "... Put $30.00").
  2. Equity   — a plain Buy/Sell that didn't match the options description
     pattern.
  3. Margin   — Description mentions "margin", or trans code INT with a
     negative amount (an interest *charge* — margin interest is the only kind
     of interest Robinhood typically charges).
  4. Fees     — AFEE (ADR fee) and other `*FEE` trans codes.
  5. Dividends/Interest — any trans code containing "DIV" (CDIV, MDIV, or any
     other dividend code Robinhood adds — the family is matched by pattern,
     not an exhaustive list), plus trans code INT with a non-negative amount
     (interest *income*, the other half of the INT split in rule 3).
  6. Gold     — GOLD or GDBP (Robinhood Gold subscription/benefit charges).
  7. Deposits / Withdraw — trans code ACH or RTP, split by the sign of
     Amount: money in is a Deposit, money out is a Withdraw. Robinhood doesn't
     appear to use distinct codes for the two directions, so sign is the
     signal.
  8. Debits / Credits — anything left over, bucketed purely by the sign of
     Amount. This is also the fallback for any trans code not recognised
     above — `reason` says so explicitly, and `metrics.py` / the dashboard
     surface the count and the distinct codes so they can be reviewed and, if
     needed, promoted into an explicit rule here.

If your statements use codes not covered here, extend the code-sets below —
that is the intended extension point.
"""
from __future__ import annotations

import re

from .model import Category, Classified, Transaction

OPTION_CODES = {"BTO", "STO", "BTC", "STC", "OEXP", "OASGN", "OEXER", "OCA", "OASSN"}
EQUITY_CODES = {"BUY", "SELL"}
# AFEE = ADR fee, DFEE = sponsored-ADR agency processing fee. Any other *FEE
# code is caught by the suffix rule below.
FEE_CODES = {"AFEE", "DFEE"}
GOLD_CODES = {"GOLD", "GDBP"}        # subscription charges and boost payments
TRANSFER_CODES = {"ACH", "RTP"}      # split into Deposits/Withdraw by amount sign
# MINT is Robinhood's "Aggregated Margin Rate" line — margin interest charged
# on a rolled-up basis rather than per day.
MARGIN_CODES = {"MINT"}
# Share-moving, cash-free events: merger/acquisition, share exchange (SPAC
# closings and ticker changes), split / reverse split / spinoff. These carry an
# empty Amount and are matched in pairs by `positions.py`.
CORPORATE_ACTION_CODES = {"MRGS", "MRGR", "SXCH", "SPR", "SPLT", "SPL", "REC", "NC"}
# Cash received in lieu of a fractional share, typically after a split or
# merger. It is equity proceeds with no quantity attached to match against.
CASH_SETTLEMENT_CODES = {"CIL"}

# "AAPL 1/17/2025 Call $150.00" / "SPY Put $735.00" — Robinhood's option
# Description format, loosely matched so minor formatting differences still hit.
_OPTION_DESC_RE = re.compile(r"\b(call|put)s?\b.{0,12}\$\s?\d", re.IGNORECASE)


def categorize(txn: Transaction) -> Classified:
    code = txn.trans_code.strip().upper()
    desc = txn.description or ""
    looks_like_option = bool(_OPTION_DESC_RE.search(desc))

    if code in OPTION_CODES:
        return Classified(txn, Category.OPTIONS, f"trans code {code!r} is an options code",
                           fallback=False)
    if code in EQUITY_CODES and looks_like_option:
        return Classified(txn, Category.OPTIONS,
                           f"trans code {code!r} but description reads as an option contract",
                           fallback=False)
    if code in EQUITY_CODES:
        return Classified(txn, Category.EQUITY, f"trans code {code!r} is an equity trade",
                           fallback=False)
    if code in CORPORATE_ACTION_CODES:
        direction = "shares out" if txn.shares_removed else "shares in"
        return Classified(
            txn, Category.CORPORATE_ACTION,
            f"trans code {code!r} is a corporate action ({direction}) — moves shares, "
            f"not cash", fallback=False)
    if code in CASH_SETTLEMENT_CODES:
        return Classified(
            txn, Category.EQUITY,
            f"trans code {code!r} is cash in lieu of a fractional share — equity proceeds",
            fallback=False)
    if code in MARGIN_CODES:
        return Classified(txn, Category.MARGIN,
                           f"trans code {code!r} is a margin interest charge", fallback=False)
    if "margin" in desc.lower():
        return Classified(txn, Category.MARGIN, "description mentions margin", fallback=False)
    if code in FEE_CODES or code.endswith("FEE"):
        return Classified(txn, Category.FEES, f"trans code {code!r} is a fee", fallback=False)
    if code == "INT":
        if txn.amount < 0:
            return Classified(txn, Category.MARGIN,
                               "trans code INT with a negative amount — assumed margin interest",
                               fallback=False)
        return Classified(txn, Category.DIVIDENDS_INTEREST,
                           "trans code INT with a non-negative amount — interest income",
                           fallback=False)
    if code in GOLD_CODES:
        return Classified(txn, Category.GOLD,
                           f"trans code {code!r} is a Robinhood Gold charge", fallback=False)
    if "DIV" in code:
        return Classified(txn, Category.DIVIDENDS_INTEREST,
                           f"trans code {code!r} matches the dividend code pattern (*DIV*)",
                           fallback=False)
    if code in TRANSFER_CODES:
        if txn.amount >= 0:
            return Classified(txn, Category.DEPOSITS,
                               f"trans code {code!r} with a non-negative amount — money in",
                               fallback=False)
        return Classified(txn, Category.WITHDRAW,
                           f"trans code {code!r} with a negative amount — money out",
                           fallback=False)

    cat = Category.CREDITS if txn.amount >= 0 else Category.DEBITS
    return Classified(
        txn, cat,
        f"unrecognised trans code {code!r} — bucketed by cash sign ({cat.value})",
        fallback=True)


def categorize_all(transactions: list[Transaction]) -> list[Classified]:
    return [categorize(t) for t in transactions]
