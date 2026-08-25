"""
Internal verification suite. `rh-dashboard selftest`

Same shape as `Production/Options/optionsuite/selftest.py`: a flat list of
grouped checks, printed as it runs. No pytest — this file *is* the test suite,
run in one shot with no per-test selection.

The two groups that matter:

  **Group 3** unit-tests the FIFO engine against hand-computed arithmetic,
  including the two cases that motivated it: shares bought and never sold
  (nothing realized, everything open) and a partial sale (half realized, half
  still open).

  **Group 7** asserts the reconciliation identity — net income, plus cash
  parked in open positions, plus transfers, equals every dollar that moved. If
  the income/holding split ever double-counts or drops a row, that check fails
  even when every individual total still looks plausible.
"""
from __future__ import annotations

import base64
import html as html_mod
import json
import re
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from .categorize import categorize, categorize_all
from .dedupe import dedupe
from .loader import LoadError, load_file, load_folder
from .metrics import compute
from .model import Category, Classified, CostBasis, Transaction, Window
from .pipeline import build_dashboard
from .positions import compute_positions, compute_windowed, normalise_contract

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample_data"

FAILS: list[str] = []
CHECKS = 0
GROUPS = 14


def _close(got, want, tol) -> bool:
    """Compare with a money tolerance, *including* numbers nested in sequences.

    Comparing a tuple of floats used to fall straight through to `==`, which
    made any grouped assertion exact by accident. Summing the same ledger in a
    different order lands a few parts in 1e-12 away — enough for
    `-24806.839999999997 != -24806.84` to fail on one interpreter and pass on
    another, which is a property of binary floating point, not of the code
    under test. Half a cent is the right resolution for every figure here.
    """
    if isinstance(want, bool) or isinstance(got, bool):
        return got == want
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        return abs(got - want) <= tol
    if isinstance(want, (list, tuple)) and isinstance(got, (list, tuple)):
        return len(got) == len(want) and all(
            _close(g, w, tol) for g, w in zip(got, want))
    return got == want


def check(name, got, want, tol=0.005):
    global CHECKS
    CHECKS += 1
    if not _close(got, want, tol):
        FAILS.append(f"{name}: got {got!r}, expected {want!r}")


def group(n, title):
    print(f"{n}. {title}")


def _calc_column(page: str, heading: str) -> list[tuple[str, float]]:
    """The (label, amount) pairs of one rendered calculation table.

    Reads the page rather than the model on purpose: a table whose rows do not
    sum to its own total is wrong in the only way the reader can see, and no
    assertion about `Metrics` catches that.
    """
    block = page.split(heading)[1].split("</table>")[0]
    out = []
    for m in re.finditer(
            r"<td>(?:<span[^>]*></span>)?([^<]*)</td>"
            r"<td class=\"num[^\"]*\">([^<]*)</td>", block):
        label = html_mod.unescape(m.group(1)).strip()
        raw = html_mod.unescape(m.group(2)).replace("$", "").replace(",", "").strip()
        neg = raw.startswith("-")
        out.append((label, float(raw.lstrip("+-")) * (-1 if neg else 1)))
    return out


def _ticker_column(page: str) -> list[tuple[str, float]]:
    """(ticker, net contribution) from the rendered by-ticker table, total last."""
    block = page.split('id="by-ticker"')[1].split("</table>")[0]
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", block, re.S):
        cells = [html_mod.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        if len(cells) < 8 or cells[0] == "Ticker":
            continue
        raw = cells[-1].replace("$", "").replace(",", "")
        if raw in ("", "\u2014"):
            continue
        out.append((cells[0], float(raw.lstrip("+-")) * (-1 if raw.startswith("-") else 1)))
    return out


def _strip_generated(html: str) -> str:
    """Drop the build timestamp so two renders can be compared."""
    return re.sub(r"Generated [^<]*", "", html)


def _txn(**kw) -> Transaction:
    base = dict(activity_date=date(2026, 1, 1), process_date=None, settle_date=None,
                instrument="TEST", description="", trans_code="BUY", quantity=1.0,
                price=1.0, amount=-1.0, source_file="fixture.csv", row_index=1)
    base.update(kw)
    return Transaction(**base)


def _trade(day: int, code: str, qty: float, amount: float, instrument="TEST",
           description="", category=Category.EQUITY) -> Classified:
    """A Classified equity/option trade, for driving the FIFO engine directly."""
    return Classified(
        txn=_txn(activity_date=date(2026, 1, day), trans_code=code, quantity=qty,
                 amount=amount, instrument=instrument, description=description,
                 price=abs(amount / qty) if qty else None),
        category=category, reason="fixture", fallback=False)


# ---------------------------------------------------------------------------
def _group_1_parsing():
    group(1, "money and date parsing")
    tmp = Path(tempfile.mkdtemp())
    try:
        f = tmp / "edge.csv"
        f.write_text(
            "Activity Date,Trans Code,Amount,Quantity,Price\n"
            '06/01/2026,Buy,"($1,234.56)",2,$617.28\n'   # parens + comma = negative
            "06/02/2026,Sell,$500.00,,\n"                 # plain positive
            "06/03/2026,ACH,0,,\n"                         # zero
            "not-a-date,ACH,5,,\n"                          # bad date -> skipped
            "06/05/2026,GOLD,not-a-number,,\n"              # bad amount -> skipped
        )
        rows, errors = load_file(f)
        check("parses (1,234.56) as -1234.56", rows[0].amount, -1234.56)
        check("parses $500.00 as 500.0", rows[1].amount, 500.0)
        check("parses 0 as 0.0", rows[2].amount, 0.0)
        check("keeps the 3 good rows", len(rows), 3)
        check("records 2 row errors", len(errors), 2)
    finally:
        shutil.rmtree(tmp)


def _group_2_headers():
    group(2, "header aliases and input validation")
    tmp = Path(tempfile.mkdtemp())
    try:
        f = tmp / "aliases.csv"
        f.write_text("Date,Symbol,Type,Net Amount\n07/01/2026,SPY,Buy,-100.00\n")
        rows, _ = load_file(f)
        check("alternate headers parse", len(rows), 1)
        check("alternate headers map correctly", rows[0].amount, -100.0)

        missing = tmp / "missing.csv"
        missing.write_text("Foo,Bar\n1,2\n")
        rows2, errors2 = load_file(missing)
        check("file missing required columns yields no rows", len(rows2), 0)
        check("file missing required columns is reported", len(errors2), 1)

        for label, path in (("missing directory", tmp / "nope"),
                             ("directory with no CSVs", tmp / "empty")):
            if label.startswith("directory"):
                path.mkdir()
            raised = False
            try:
                load_folder(path)
            except LoadError:
                raised = True
            check(f"load_folder on a {label} raises LoadError", raised, True)
    finally:
        shutil.rmtree(tmp)


def _group_3_fifo():
    group(3, "FIFO lot matching")

    # --- bought and never sold: nothing realized, everything open ----------
    r = compute_positions([_trade(2, "Buy", 100, -25000.0, "TSLA")])
    check("open-only: no realized P&L", r.realized(Category.EQUITY), 0.0)
    check("open-only: 100 shares still held", r.open_shares, 100.0)
    check("open-only: cost basis is what was paid", r.open_equity_cost_basis, 25000.0)
    check("open-only: one holding", len(r.equity_holdings), 1)
    check("open-only: avg price per share", r.equity_holdings[0].avg_price, 250.0)

    # --- partial sale: half realized, half still open ----------------------
    r = compute_positions([
        _trade(3, "Buy", 100, -18000.0, "AAPL"),     # 100 @ 180
        _trade(5, "Sell", 50, 9500.0, "AAPL"),        # 50 @ 190
    ])
    check("partial: realized on the 50 sold only",
          r.realized(Category.EQUITY), 500.0)          # (190-180) * 50
    check("partial: 50 shares remain open", r.open_shares, 50.0)
    check("partial: remaining cost basis at original price",
          r.open_equity_cost_basis, 9000.0)            # 50 * 180
    check("partial: avg cost unchanged by the sale",
          r.equity_holdings[0].avg_price, 180.0)

    # --- fully closed round trip ------------------------------------------
    r = compute_positions([
        _trade(5, "Buy", 10, -5500.0, "SPY"),
        _trade(10, "Sell", 10, 5600.0, "SPY"),
    ])
    check("closed: full realized P&L", r.realized(Category.EQUITY), 100.0)
    check("closed: nothing left open", r.open_shares, 0.0)
    check("closed: no holding row", len(r.equity_holdings), 0)

    # --- two lots, one sale: the whole point of the cost-basis setting -----
    # This is the only shape where the two modes can disagree, and nothing in
    # sample_data has it — no ticker there has two buys before a sell, so the
    # fixture cannot tell the modes apart. These assertions are what make the
    # setting testable at all.
    multi_lot = [
        _trade(1, "Buy", 50, -500.0, "MULTI"),        # 50 @ 10  (oldest)
        _trade(2, "Buy", 50, -1000.0, "MULTI"),       # 50 @ 20
        _trade(3, "Sell", 60, 1800.0, "MULTI"),       # 60 @ 30
    ]

    # 50 from the $10 lot -> (30-10)*50 = 1000; 10 from the $20 lot -> (30-20)*10 = 100
    r = compute_positions(multi_lot, cost_basis=CostBasis.FIFO)
    check("fifo: oldest lot matched first", r.realized(Category.EQUITY), 1100.0)
    check("fifo: 40 shares left", r.open_shares, 40.0)
    check("fifo: remainder priced at the newer lot",
          r.open_equity_cost_basis, 800.0)             # 40 * 20
    check("fifo: cash identity holds",
          r.realized(Category.EQUITY) - r.open_equity_cost_basis, 300.0)  # -500-1000+1800

    # blended cost is (50*10 + 50*20) / 100 = 15, so (30-15)*60 = 900
    r = compute_positions(multi_lot, cost_basis=CostBasis.AVERAGE)
    check("average: sale realizes against the blend",
          r.realized(Category.EQUITY), 900.0)
    check("average: 40 shares left", r.open_shares, 40.0)
    check("average: remainder priced at the blend",
          r.open_equity_cost_basis, 600.0)             # 40 * 15
    check("average: per-share cost is the blend",
          r.equity_holdings[0].avg_price, 15.0)
    check("average: cash identity holds",
          r.realized(Category.EQUITY) - r.open_equity_cost_basis, 300.0)
    check("average: the book collapses to a single lot",
          len(r.equity_holdings), 1)

    # The pair that would catch a mode silently reverting.
    check("average is the default", compute_positions(multi_lot).realized(
        Category.EQUITY), 900.0)
    check("the two modes genuinely disagree here",
          compute_positions(multi_lot, cost_basis=CostBasis.FIFO).realized(
              Category.EQUITY)
          != compute_positions(multi_lot, cost_basis=CostBasis.AVERAGE).realized(
              Category.EQUITY), True)

    # Timing, not totals: close the position out and both modes agree exactly.
    closed_out = multi_lot + [_trade(4, "Sell", 40, 1000.0, "MULTI")]
    fifo_all = compute_positions(closed_out, cost_basis=CostBasis.FIFO)
    avg_all = compute_positions(closed_out, cost_basis=CostBasis.AVERAGE)
    check("a fully closed position realizes the same either way",
          fifo_all.realized(Category.EQUITY), avg_all.realized(Category.EQUITY))
    check("a fully closed position realizes the ledger's cash",
          avg_all.realized(Category.EQUITY), 1300.0)   # -500-1000+1800+1000
    check("a fully closed position leaves nothing open either way",
          (fifo_all.open_shares, avg_all.open_shares), (0.0, 0.0))

    # Selling everything then buying again must not inherit the old average.
    reopened = [
        _trade(1, "Buy", 100, -1000.0, "RESET"),      # 100 @ 10
        _trade(2, "Sell", 100, 1200.0, "RESET"),      # all out
        _trade(3, "Buy", 10, -500.0, "RESET"),        # 10 @ 50, fresh
    ]
    r = compute_positions(reopened)
    check("average resets once the position is fully closed",
          r.equity_holdings[0].avg_price, 50.0)
    check("average reset keeps the realized figure intact",
          r.realized(Category.EQUITY), 200.0)

    # Options must ignore the setting: two strikes never blend.
    calls = [
        _trade(1, "BTO", 1, -300.0, "AAPL", "AAPL 7/17/2026 Call $220.00",
               Category.OPTIONS),
        _trade(2, "BTO", 1, -100.0, "AAPL", "AAPL 7/17/2026 Call $260.00",
               Category.OPTIONS),
        _trade(3, "STC", 1, 500.0, "AAPL", "AAPL 7/17/2026 Call $220.00",
               Category.OPTIONS),
    ]
    for mode in (CostBasis.AVERAGE, CostBasis.FIFO):
        r = compute_positions(calls, cost_basis=mode)
        check(f"options ignore cost basis ({mode.value}): only its own contract closes",
              r.realized(Category.OPTIONS), 200.0)
        check(f"options ignore cost basis ({mode.value}): the other strike stays open",
              [h.key for h in r.option_holdings], ["AAPL 7/17/2026 Call $260.00"])

    # --- short option round trip ------------------------------------------
    contract = "AAPL 7/17/2026 Call $220.00"
    r = compute_positions([
        _trade(12, "STO", 1, 320.0, "AAPL", contract, Category.OPTIONS),
        _trade(20, "BTC", 1, -110.0, "AAPL", contract, Category.OPTIONS),
    ])
    check("short option: realized credit minus debit",
          r.realized(Category.OPTIONS), 210.0)
    check("short option: nothing open", len(r.option_holdings), 0)

    # --- option left open keeps its credit out of income -------------------
    r = compute_positions([
        _trade(12, "STO", 1, 320.0, "AAPL", contract, Category.OPTIONS)])
    check("open short option: no realized P&L", r.realized(Category.OPTIONS), 0.0)
    check("open short option: credit held as open", r.open_options_net_cash, 320.0)
    check("open short option: flagged as a credit position",
          r.option_holdings[0].is_credit_position, True)

    # --- long option expiring worthless -----------------------------------
    r = compute_positions([
        _trade(12, "BTO", 1, -500.0, "NVDA", "NVDA 8/21/2026 Put $120.00", Category.OPTIONS),
        _trade(30, "OEXP", 1, 0.0, "NVDA", "NVDA 8/21/2026 Put $120.00", Category.OPTIONS),
    ])
    check("expired long option: full debit realized as a loss",
          r.realized(Category.OPTIONS), -500.0)
    check("expired long option: nothing open", len(r.option_holdings), 0)

    # --- different contracts must not match each other ---------------------
    r = compute_positions([
        _trade(1, "STO", 1, 300.0, "AAPL", "AAPL 7/17/2026 Call $220.00", Category.OPTIONS),
        _trade(2, "STO", 1, 100.0, "AAPL", "AAPL 7/17/2026 Call $260.00", Category.OPTIONS),
    ])
    check("distinct strikes are distinct positions", len(r.option_holdings), 2)

    # --- an expiration's Description is prefixed; it must still match ------
    # Real data: STO "GOOG 8/14/2026 Call $390.00" closed by
    # OEXP "Option Expiration for GOOG 8/14/2026 Call $390.00".
    r = compute_positions([
        _trade(1, "STO", 1, 320.0, "GOOG", "GOOG 8/14/2026 Call $390.00", Category.OPTIONS),
        _trade(20, "OEXP", 1, 0.0, "GOOG",
               "Option Expiration for GOOG 8/14/2026 Call $390.00", Category.OPTIONS),
    ])
    check("prefixed expiration closes the contract it names",
          len(r.option_holdings), 0)
    check("prefixed expiration realizes the credit", r.realized(Category.OPTIONS), 320.0)
    check("normalise_contract strips the prefix",
          normalise_contract("Option Expiration for AMD 8/5/2026 Call $530.00"),
          "AMD 8/5/2026 Call $530.00")

    # --- same-day buy and sell: opens must be processed first --------------
    # Real data lists newest-first, so the sell appears above the buy that
    # funded it; naive file ordering produced a phantom open position.
    same_day = [
        _trade(7, "Sell", 41.272514, 25629.49, "META"),
        _trade(7, "Buy", 0.272514, -165.07, "META"),
        _trade(7, "Buy", 41, -24834.93, "META"),
    ]
    r = compute_positions(same_day)
    check("same-day round trip leaves nothing open", r.open_shares, 0.0)
    check("same-day round trip realizes the whole move",
          r.realized(Category.EQUITY), 25629.49 - 165.07 - 24834.93)
    check("same-day round trip raises no oversold warning", len(r.warnings), 0)

    # --- corporate action: basis carries, nothing is realized --------------
    # CCIV 200 shares surrendered -> 200 LCID received, same day.
    ca = [
        _trade(1, "Buy", 200, -4810.00, "CCIV"),
        Classified(_txn(activity_date=date(2026, 1, 20), trans_code="SXCH",
                        instrument="CCIV", quantity=200.0, quantity_suffix="S",
                        amount=0.0, price=None),
                   Category.CORPORATE_ACTION, "fixture", False),
        Classified(_txn(activity_date=date(2026, 1, 20), trans_code="SXCH",
                        instrument="LCID", quantity=200.0, amount=0.0, price=None),
                   Category.CORPORATE_ACTION, "fixture", False),
    ]
    r = compute_positions(ca)
    held = {h.instrument: h for h in r.equity_holdings}
    check("surrendered ticker is closed out", "CCIV" in held, False)
    check("received ticker is held", held["LCID"].quantity, 200.0)
    check("cost basis carries across the exchange", held["LCID"].cost_basis, 4810.00)
    check("a corporate action realizes nothing", r.realized(Category.EQUITY), 0.0)
    check("total share count is unchanged in kind", r.open_shares, 200.0)

    # a reverse split: 200 out, 40 in, same basis, same day
    rs = [
        _trade(1, "Buy", 200, -3200.00, "AKAN"),
        Classified(_txn(activity_date=date(2026, 2, 10), trans_code="SPR",
                        instrument="AKAN", quantity=200.0, quantity_suffix="S",
                        amount=0.0, price=None),
                   Category.CORPORATE_ACTION, "fixture", False),
        Classified(_txn(activity_date=date(2026, 2, 10), trans_code="SPR",
                        instrument="AKAN", quantity=40.0, amount=0.0, price=None),
                   Category.CORPORATE_ACTION, "fixture", False),
    ]
    r = compute_positions(rs)
    h = r.equity_holdings[0]
    check("reverse split leaves the post-split share count", h.quantity, 40.0)
    check("reverse split preserves total basis", h.cost_basis, 3200.00)
    check("reverse split multiplies per-share cost", h.avg_price, 80.00)

    # --- an outgoing corporate-action leg with nothing coming back ---------
    # A cash merger or a delisting surrenders lots and never returns shares.
    # The basis has nowhere to go; it must be named, not dropped.
    orphan = [
        _trade(1, "Buy", 100, -2405.00, "CCIV"),
        Classified(_txn(activity_date=date(2026, 1, 20), trans_code="SXCH",
                        instrument="CCIV", quantity=100.0, quantity_suffix="S",
                        amount=0.0, price=None),
                   Category.CORPORATE_ACTION, "fixture", False),
    ]
    r = compute_positions(orphan)
    check("orphaned corporate-action basis is reported, not dropped",
          r.unmatched_corporate_action_basis, 2405.00)
    check("orphaned corporate-action basis raises a warning naming the amount",
          any("no shares arrived in exchange" in w and "2,405.00" in w
              for w in r.warnings), True)
    check("orphaned corporate-action basis names the position that gave it up",
          any(w.startswith("CCIV:") and "surrendered" in w for w in r.warnings), True)
    check("nothing is realized by an orphaned corporate action",
          r.realized(Category.EQUITY), 0.0)
    check("the surrendered shares are gone from holdings", r.open_shares, 0.0)

    # The mirror case must stay silent: a matched pair distributes its pool in
    # full, so a well-formed exchange raises no residual and no warning.
    r = compute_positions(ca)
    check("a matched corporate action leaves no residual basis",
          r.unmatched_corporate_action_basis, 0.0)
    check("a matched corporate action raises no residual warning",
          any("no shares arrived in exchange" in w for w in r.warnings), False)

    # --- an option open past its own expiry is flagged ---------------------
    r = compute_positions([
        _trade(1, "STO", 1, 100.0, "XYZ", "XYZ 1/5/2026 Call $10.00", Category.OPTIONS),
        Classified(_txn(activity_date=date(2026, 9, 1), trans_code="Sell",
                        instrument="OTHER", quantity=1.0, amount=10.0),
                   Category.EQUITY, "fixture", False),   # moves last activity forward
    ])
    check("contract left open past expiry is warned about",
          any("no closing row" in w for w in r.warnings), True)

    # The same contract, reported only as far as 2026-06-30 but with the whole
    # file available. The closing row exists — it is just dated later — so this
    # is not missing data and must not be described as though it were. Without
    # the distinction, every windowed report accuses its own input.
    later_close = _trade(1, "STO", 1, 100.0, "XYZ",
                         "XYZ 1/5/2026 Call $10.00", Category.OPTIONS)
    closing_row = Classified(
        _txn(activity_date=date(2026, 7, 20), trans_code="BTC", instrument="XYZ",
             description="XYZ 1/5/2026 Call $10.00", quantity=1.0, amount=-40.0),
        Category.OPTIONS, "fixture", False)
    as_of_row = Classified(
        _txn(activity_date=date(2026, 6, 30), trans_code="Sell", instrument="OTHER",
             quantity=1.0, amount=10.0), Category.EQUITY, "fixture", False)
    reported = [later_close, as_of_row]
    r = compute_positions(reported, full_history=reported + [closing_row])
    check("a closing row dated after the reported range is not called missing",
          any("no closing row" in w for w in r.warnings), False)
    check("a closing row dated after the reported range is named by date",
          any("2026-07-20" in w and "end of the range being reported" in w
              for w in r.warnings), True)

    # And with no full history to consult, the old wording is what remains —
    # the default must behave exactly as it did before the parameter existed.
    r = compute_positions(reported)
    check("without full history the missing-closing-row wording is unchanged",
          any("no closing row" in w for w in r.warnings), True)

    # A contract that has not expired yet at the reported end is simply open.
    unexpired = [
        _trade(1, "STO", 1, 100.0, "XYZ", "XYZ 12/18/2026 Call $10.00",
               Category.OPTIONS),
        as_of_row,
    ]
    r = compute_positions(unexpired, full_history=unexpired)
    check("a contract not yet expired at the reported end is not flagged",
          [w for w in r.warnings if "expired" in w], [])

    # --- selling more than held is flagged, not silently guessed -----------
    r = compute_positions([
        _trade(1, "Buy", 10, -1000.0, "GAP"),
        _trade(2, "Sell", 25, 2750.0, "GAP"),
    ])
    check("oversold: warning raised", len(r.warnings) >= 1, True)
    check("oversold: matched portion plus bare proceeds",
          r.realized(Category.EQUITY), 1750.0)         # (110-100)*10 + 110*15
    check("oversold: nothing left open", r.open_shares, 0.0)
    # nothing open means realized must equal the raw cash sum (-1000 + 2750)
    check("oversold: realized equals total cash when nothing is left open",
          r.realized(Category.EQUITY), 1750.0)


# Hand-derived from sample_data/*.csv. Recomputed independently of the code
# under test: each figure below was worked out from the two CSVs by hand and
# cross-checked against the arithmetic in the comments.
#
# AAPL is bought TWICE before it is sold — 100 @ $180 on 06/03 and 100 @ $150
# on 06/04 — which is the only shape where the two cost bases can disagree.
# Everything else in the fixture is single-lot and reads the same either way.
#
#   AAPL blended cost  (100*180 + 100*150) / 200                     -> $165.00
#   AAPL realized      average: (190-165)*50                         -> +1,250
#                      fifo:    (190-180)*50, oldest lot first       ->   +500
#   SPY realized       (560-550)*10                                  ->   +100
#   Equity realized    average 1,250+100 = +1,350 ; fifo 500+100     ->   +600
#   Options realized   STO +320 then BTC -110 on one contract        ->   +210
#   Div/Interest       CDIV 8.75 + MDIV 3.10                         -> +11.85
#   Fees               AFEE                                          ->  -2.50
#   Margin             INT (negative)                                -> -12.34
#   Gold               GOLD -5.00 + GDBP -5.00                       -> -10.00
#   Other              SLIP (no rule)                                ->  +1.15
#   Net income         average 1350+210+11.85-2.50-12.34-10+1.15     -> +1,548.16
#                      fifo     600+210+11.85-2.50-12.34-10+1.15     ->   +798.16
#
# A corporate action is also present: CCIV 100 bought for $2,405, then
# surrendered for 100 LCID the following month. It carries NO Amount, so it
# changes no income figure at all — it only moves the $2,405 of cost basis
# from CCIV to LCID, which is exactly the property being asserted.
#
#   AAPL still held    150 shares: average 150*165 = 24,750 ; fifo
#                      50*180 + 100*150 = 24,000
#   Open equity        average  25,000 + 24,750 + 2,700 + 2,405 = 54,855
#                      fifo     25,000 + 24,000 + 2,700 + 2,405 = 54,105
#                      370 shares either way
#
# The two modes differ by exactly 750 in realized income AND by exactly 750 in
# open cost basis, in opposite directions — so total cash movement is IDENTICAL
# under both. That is the whole claim about cost basis in one number: it moves
# P&L through time without creating or destroying any.
#
#   Total cash         both modes                                    -> -24,806.84
#     average  1,548.16 - 54,855 + 0 + 28,500 = -24,806.84
#     fifo       798.16 - 54,105 + 0 + 28,500 = -24,806.84
EXPECTED_INCOME = {                 # the default mode: average cost
    Category.EQUITY: 1350.00,
    Category.OPTIONS: 210.00,
    Category.DIVIDENDS_INTEREST: 11.85,
    Category.FEES: -2.50,
    Category.MARGIN: -12.34,
    Category.GOLD: -10.00,
}
EXPECTED_COUNTS = {
    Category.EQUITY: 8, Category.OPTIONS: 2, Category.DIVIDENDS_INTEREST: 2,
    Category.FEES: 1, Category.MARGIN: 1, Category.GOLD: 2,
    Category.DEPOSITS: 2, Category.WITHDRAW: 1, Category.CORPORATE_ACTION: 2,
}
EXPECTED_NET_INCOME = 1548.16       # average cost
EXPECTED_OPEN_SHARES = 370.0        # unchanged by the mode
EXPECTED_OPEN_COST = 54855.00       # average cost
EXPECTED_TOTAL_CASH = -24806.84     # unchanged by the mode
EXPECTED_DEPOSITS = 30500.00
EXPECTED_WITHDRAW = -2000.00
EXPECTED_OTHER = 1.15

# The same fixture read the other way. Only three figures move.
EXPECTED_FIFO_EQUITY = 600.00
EXPECTED_FIFO_NET_INCOME = 798.16
EXPECTED_FIFO_OPEN_COST = 54105.00


def _sample_metrics():
    lr = load_folder(SAMPLE)
    dd = dedupe(lr.transactions)
    classified = categorize_all(dd.kept)
    positions = compute_positions(classified)
    return lr, dd, classified, positions, compute(classified, positions, dd.removed,
                                                   lr.row_errors)


def _group_4_sample_totals():
    group(4, "sample_data reproduces hand-derived totals")
    lr, dd, classified, positions, m = _sample_metrics()

    check("no row errors in the sample CSVs", len(lr.row_errors), 0)
    check("24 raw rows across both files", len(lr.transactions), 24)
    check("2 cross-file duplicates removed", dd.removed, 2)
    check("22 transactions kept", len(dd.kept), 22)
    check("no lot-matching warnings on clean data", len(positions.warnings), 0)
    # corporate action rows carry no Amount and must survive the loader
    check("corporate action rows are loaded, not dropped as unparseable",
          sum(1 for t in dd.kept if t.trans_code == "SXCH"), 2)
    check("the outgoing leg keeps its S marker",
          sum(1 for t in dd.kept if t.shares_removed), 1)

    for cat, want in EXPECTED_INCOME.items():
        check(f"{cat.value} income", m.income_of(cat), want)
    for cat, want in EXPECTED_COUNTS.items():
        check(f"{cat.value} transaction count", m.categories[cat].count, want)

    check("other (unclassified) income", m.other_income, EXPECTED_OTHER)
    check("net income", m.net_income, EXPECTED_NET_INCOME)
    check("open shares held", m.open_shares, EXPECTED_OPEN_SHARES)
    check("open equity cost basis", m.open_equity_cost_basis, EXPECTED_OPEN_COST)
    check("no open options", m.open_options_net_cash, 0.0)
    check("total cash movement", m.total_cash_movement, EXPECTED_TOTAL_CASH)
    check("deposits", m.categories[Category.DEPOSITS].cash_total, EXPECTED_DEPOSITS)
    check("withdraw", m.categories[Category.WITHDRAW].cash_total, EXPECTED_WITHDRAW)

    # the specific per-ticker positions the sample was built to demonstrate
    held = {h.instrument: h for h in positions.equity_holdings}
    check("TSLA fully open (never sold)", held["TSLA"].quantity, 100.0)
    check("TSLA cost basis", held["TSLA"].cost_basis, 25000.0)
    check("AAPL open after selling 50 of 200 across two lots",
          held["AAPL"].quantity, 150.0)
    check("AAPL remaining cost basis at the blend", held["AAPL"].cost_basis, 24750.0)
    check("AAPL per-share cost is the blend, not either lot price",
          held["AAPL"].avg_price, 165.0)
    check("NVDA fully open", held["NVDA"].quantity, 20.0)
    check("SPY fully closed, not in holdings", "SPY" in held, False)
    # the corporate action: CCIV is gone, LCID holds its basis, income untouched
    check("CCIV surrendered, no longer held", "CCIV" in held, False)
    check("LCID received in exchange", held["LCID"].quantity, 100.0)
    check("LCID inherited CCIV's cost basis", held["LCID"].cost_basis, 2405.00)
    check("the exchange realized nothing",
          m.income_of(Category.EQUITY), EXPECTED_INCOME[Category.EQUITY])
    check("corporate action moved no cash",
          m.categories[Category.CORPORATE_ACTION].cash_total, 0.0)

    # --- the same fixture read under FIFO ---------------------------------
    # AAPL's two lots are what make this possible: the fixture used to agree
    # with itself under either mode, which meant it could not test the setting
    # at all. Each figure below is hand-derived in the block above.
    fifo = compute_positions(classified, cost_basis=CostBasis.FIFO)
    avg = compute_positions(classified, cost_basis=CostBasis.AVERAGE)
    fifo_m = compute(classified, fifo, dd.removed, lr.row_errors)

    check("fifo: equity realized takes the oldest lot",
          fifo.realized(Category.EQUITY), EXPECTED_FIFO_EQUITY)
    check("fifo: net income", fifo_m.net_income, EXPECTED_FIFO_NET_INCOME)
    check("fifo: open equity cost basis",
          fifo.open_equity_cost_basis, EXPECTED_FIFO_OPEN_COST)
    fifo_held = {h.instrument: h for h in fifo.equity_holdings}
    check("fifo: AAPL keeps the untouched lot plus the remainder of the first",
          fifo_held["AAPL"].cost_basis, 24000.0)       # 50*180 + 100*150
    check("fifo: AAPL per-share cost", fifo_held["AAPL"].avg_price, 160.0)

    # The anti-regression pair: these must not collapse back into each other.
    check("the two modes disagree on equity income",
          avg.realized(Category.EQUITY) - fifo.realized(Category.EQUITY), 750.00)
    check("options are untouched by the mode",
          avg.realized(Category.OPTIONS), fifo.realized(Category.OPTIONS))
    check("share count is untouched by the mode",
          (avg.open_shares, fifo.open_shares),
          (EXPECTED_OPEN_SHARES, EXPECTED_OPEN_SHARES))

    # ...and the invariant that makes the whole idea honest: whatever one mode
    # recognises early, it holds back in open basis by exactly as much, so the
    # cash that actually moved is the same number either way.
    check("what one mode realizes early it holds back in basis",
          avg.open_equity_cost_basis - fifo.open_equity_cost_basis,
          avg.realized(Category.EQUITY) - fifo.realized(Category.EQUITY))
    check("total cash movement is identical under both modes",
          fifo_m.total_cash_movement, m.total_cash_movement)
    check("both modes reconcile", (m.reconciles, fifo_m.reconciles), (True, True))


def _group_5_categorize():
    group(5, "categorisation rules")
    cases = [
        (_txn(trans_code="BTO", description="SPY 9/18/2026 Call $800.00"), Category.OPTIONS, False),
        (_txn(trans_code="STC", description="SPY 9/18/2026 Put $700.00"), Category.OPTIONS, False),
        (_txn(trans_code="Buy", description="AAPL", amount=-500.0), Category.EQUITY, False),
        (_txn(trans_code="Sell", description="AAPL", amount=500.0), Category.EQUITY, False),
        (_txn(trans_code="Buy", description="AAPL 7/17/2026 Call $220.00"), Category.OPTIONS, False),
        (_txn(trans_code="INT", description="Margin Interest", amount=-3.0), Category.MARGIN, False),
        (_txn(trans_code="INT", description="Interest on cash", amount=2.0),
         Category.DIVIDENDS_INTEREST, False),
        (_txn(trans_code="AFEE", description="ADR Fee", amount=-2.5), Category.FEES, False),
        (_txn(trans_code="afee", description="adr fee", amount=-1.0), Category.FEES, False),
        (_txn(trans_code="GOLD", amount=-5.0), Category.GOLD, False),
        (_txn(trans_code="GDBP", amount=-5.0), Category.GOLD, False),
        (_txn(trans_code="CDIV", amount=8.75), Category.DIVIDENDS_INTEREST, False),
        (_txn(trans_code="MDIV", amount=3.10), Category.DIVIDENDS_INTEREST, False),
        (_txn(trans_code="QDIV", amount=1.0), Category.DIVIDENDS_INTEREST, False),
        (_txn(trans_code="ACH", description="ACH Deposit", amount=1000.0), Category.DEPOSITS, False),
        (_txn(trans_code="RTP", description="Instant Deposit", amount=500.0), Category.DEPOSITS, False),
        (_txn(trans_code="ACH", description="ACH Withdrawal", amount=-500.0), Category.WITHDRAW, False),
        (_txn(trans_code="RTP", description="Instant Withdrawal", amount=-50.0), Category.WITHDRAW, False),
        (_txn(trans_code="WHATEVER", amount=50.0), Category.CREDITS, True),
        (_txn(trans_code="WHATEVER", amount=-50.0), Category.DEBITS, True),
    ]
    for txn, want_cat, want_fallback in cases:
        got = categorize(txn)
        check(f"{txn.trans_code}/{txn.amount:g} -> {want_cat.value}",
              got.category, want_cat)
        check(f"{txn.trans_code}/{txn.amount:g} fallback flag", got.fallback, want_fallback)


def _group_6_dedupe():
    group(6, "dedupe semantics")
    # filenames are alphabetical here because load_folder reads them sorted
    a = _txn(source_file="2026-01.csv", row_index=2)
    b = _txn(source_file="2026-02.csv", row_index=9)                 # identical to a
    c = _txn(source_file="2026-02.csv", row_index=10, amount=-1.01)  # genuinely different
    r = dedupe([a, b, c])
    check("identical rows across files collapse", len(r.kept), 2)
    check("the earlier file's copy is kept", r.kept[0].source_file, "2026-01.csv")
    check("removed count", r.removed, 1)

    # A row repeated WITHIN one file is two real transactions, not a duplicate.
    # Collapsing these silently deleted a share purchase and left an option
    # contract open forever on real data — see the module docstring.
    twice = [_txn(source_file="only.csv", row_index=i) for i in (2, 3)]
    r = dedupe(twice)
    check("identical rows within one file are both kept", len(r.kept), 2)
    check("nothing removed within a single file", r.removed, 0)

    # Multiplicity survives an overlap: twice in each of two files is still two.
    overlap = ([_txn(source_file="2026-01.csv", row_index=i) for i in (2, 3)]
               + [_txn(source_file="2026-02.csv", row_index=i) for i in (5, 6)])
    r = dedupe(overlap)
    check("2-in-each-file collapses to 2, not 1 or 4", len(r.kept), 2)
    check("both survivors come from one file",
          len({t.source_file for t in r.kept}), 1)

    # An uneven overlap keeps the larger count (the fuller export wins).
    uneven = ([_txn(source_file="2026-01.csv", row_index=2)]
              + [_txn(source_file="2026-02.csv", row_index=i) for i in (5, 6, 7)])
    r = dedupe(uneven)
    check("uneven overlap keeps the maximum multiplicity", len(r.kept), 3)


def _group_7_reconciliation():
    group(7, "reconciliation invariants")
    _, _, classified, positions, m = _sample_metrics()

    check("sample reconciles exactly", m.reconciles, True)
    check("reconciliation error is zero", m.reconciliation_error, 0.0)

    # the identity, spelled out independently of metrics.compute
    rebuilt = (m.net_income - m.open_equity_cost_basis + m.open_options_net_cash
               + m.categories[Category.DEPOSITS].cash_total
               + m.categories[Category.WITHDRAW].cash_total
               + m.corporate_action_cash)
    check("net income + holdings + transfers + CA cash == total cash",
          rebuilt, m.total_cash_movement)
    check("a share-for-share exchange moves no cash", m.corporate_action_cash, 0.0)

    # raw cash across every category must equal the ledger sum, untouched by
    # the income/holding split
    check("category cash totals sum to total cash movement",
          sum(s.cash_total for s in m.categories.values()), m.total_cash_movement)

    # for lot-matched categories the gap between cash and income is exactly
    # the money sitting in open positions
    check("equity cash-vs-income gap equals open cost basis",
          m.categories[Category.EQUITY].unrealized_gap, -m.open_equity_cost_basis)
    check("options cash-vs-income gap equals open option cash",
          m.categories[Category.OPTIONS].unrealized_gap, m.open_options_net_cash)
    for cat in (Category.DIVIDENDS_INTEREST, Category.FEES, Category.MARGIN, Category.GOLD):
        check(f"{cat.value} has no cash-vs-income gap",
              m.categories[cat].unrealized_gap, 0.0)

    # cumulative series must land on their totals
    check("net income cumulative ends at net income",
          m.net_income_cumulative[-1], m.net_income)
    for cat in EXPECTED_INCOME:
        check(f"{cat.value} cumulative ends at its income total",
              m.categories[cat].income_cumulative[-1], m.income_of(cat))

    # --- a corporate action that carries cash ------------------------------
    # Corporate actions reach neither net income nor transfers, so cash on one
    # used to land nowhere but the error term: a real cash-plus-stock merger
    # would raise the "something is double-counted" banner while nothing was
    # actually wrong. The term is named now, so the identity absorbs it.
    merger = [
        _trade(1, "Buy", 100, -2405.00, "CCIV"),
        Classified(_txn(activity_date=date(2026, 1, 20), trans_code="MRGS",
                        instrument="CCIV", quantity=100.0, quantity_suffix="S",
                        amount=0.0, price=None),
                   Category.CORPORATE_ACTION, "fixture", False),
        Classified(_txn(activity_date=date(2026, 1, 20), trans_code="MRGS",
                        instrument="LCID", quantity=100.0, amount=0.0, price=None),
                   Category.CORPORATE_ACTION, "fixture", False),
        # the cash half of the consideration
        Classified(_txn(activity_date=date(2026, 1, 20), trans_code="MRGS",
                        instrument="CCIV", description="Cash consideration",
                        quantity=None, amount=250.00, price=None),
                   Category.CORPORATE_ACTION, "fixture", False),
    ]
    pm = compute_positions(merger)
    mm = compute(merger, pm, 0, [])
    check("a cash merger's cash is a named term", mm.corporate_action_cash, 250.00)
    check("a cash merger still reconciles", mm.reconciles, True)
    check("a cash merger leaves no reconciliation error", mm.reconciliation_error, 0.0)
    check("a cash merger does not leak into net transfers", mm.net_transfers, 0.0)


def _group_8_dashboard():
    group(8, "end-to-end dashboard build")
    tmp = Path(tempfile.mkdtemp())
    try:
        res = build_dashboard(input_dir=SAMPLE, output_dir=tmp, filename="dashboard.html")
        check("build reports ok", res["ok"], True)
        check("build reports reconciled", res["reconciles"], True)
        check("net income surfaced in the result", res["net_income"], EXPECTED_NET_INCOME)
        check("open shares surfaced in the result", res["open_shares"], EXPECTED_OPEN_SHARES)
        check("four open equity positions listed (TSLA, AAPL, NVDA, LCID)",
              len([p for p in res["open_positions"] if p["category"] == "Equity"]), 4)

        check("the result reports which cost basis produced it",
              res["cost_basis"], "average")

        out = Path(res["output"])
        check("output file exists", out.exists(), True)
        html = out.read_text(encoding="utf-8")
        check("doctype present", html.strip().startswith("<!doctype html>"), True)
        check("the page states the cost basis that produced it",
              "Equity cost basis: <strong>average cost</strong>" in html, True)
        check("the page does not claim FIFO when averaging",
              "matched <strong>FIFO</strong>" in html, False)

        # The same statements under the other mode must say so, or two pages
        # that disagree on the numbers look identical to a reader.
        alt = build_dashboard(input_dir=SAMPLE, output_dir=tmp,
                              filename="fifo.html", cost_basis=CostBasis.FIFO)
        alt_html = Path(alt["output"]).read_text(encoding="utf-8")
        check("the FIFO page states FIFO",
              "Equity cost basis: <strong>FIFO (first in, first out)</strong>" in alt_html,
              True)
        check("the alternate mode is surfaced in the result", alt["cost_basis"], "fifo")
        # AAPL's two lots mean the fixture now reports genuinely different
        # numbers under the two modes — which is the point of having it.
        check("the two pages report different net income",
              alt["net_income"], EXPECTED_FIFO_NET_INCOME)
        check("...and it is not the default's figure",
              alt["net_income"] != res["net_income"], True)
        check("the FIFO page prints its own net income",
              "$798.16" in alt_html, True)
        check("the FIFO page does not print the average-cost figure",
              "$1,548.16" in alt_html, False)
        check("both pages agree on shares held",
              alt["open_shares"], res["open_shares"])
        for label in ("Net income", "Open positions", "Equity", "Options",
                      "Dividends/Interest", "Fees", "Margin", "Gold",
                      "Deposits", "Withdraw", "Corporate action",
                      "TSLA", "AAPL", "NVDA", "LCID"):
            check(f"page mentions {label}", label in html, True)
        check("page does not list the surrendered ticker as held",
              ">CCIV<" in html.split('id="txn-table"')[0], False)
        check("page states the net income figure", "$1,548.16" in html, True)
        check("page states total shares held", "370 shares" in html, True)
        check("page states open cost basis", "$54,855.00" in html, True)
        check("page explains buying is not a loss",
              "converts cash into an asset" in html, True)
        # The favicon is the one asset with a real pull to embed, so it is
        # also the easiest thing to "fix" later by linking a file — which
        # would break the offline guarantee for a single tab icon.
        check("page declares a favicon", 'rel="icon"' in html, True)
        check("the favicon is embedded, not linked",
              'href="data:image/png;base64,' in html, True)
        # Extract defensively: a missing favicon must fail these checks, not
        # raise out of the suite and skip every group after this one.
        parts = html.split('href="data:image/png;base64,')
        favicon = base64.b64decode(parts[1].split('"')[0]) if len(parts) > 1 else b""
        check("the embedded favicon is a real PNG",
              favicon[:8], b"\x89PNG\r\n\x1a\n")
        check("the favicon stays small enough to inline",
              0 < len(favicon) < 4096, True)
        check("no external script src", "<script src=" in html, False)
        check("no external stylesheet", 'rel="stylesheet"' in html, False)
        check("no http(s) references at all", "http://" in html or "https://" in html, False)
    finally:
        shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# Group 9 drives the real handler over a real socket rather than calling the
# route methods directly: the things most likely to break here — auth, the
# body-size cap, multipart parsing, path traversal — all live in the HTTP
# layer, and a unit test that skips it would prove none of them.
def _serve_temp():
    """Start a throwaway server on a free port. Returns (base_url, cfg, stop)."""
    from http.server import ThreadingHTTPServer

    from .server import ServerConfig, make_handler

    tmp = Path(tempfile.mkdtemp())
    cfg = ServerConfig(input_dir=tmp / "input", output_dir=tmp / "output",
                       username="tester", password="s3cret", max_upload=64 * 1024)
    cfg.input_dir.mkdir(parents=True)
    cfg.output_dir.mkdir(parents=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg))
    # Quiet: the suite prints its own output and per-request logging would bury it.
    httpd.RequestHandlerClass.log_message = lambda *a, **k: None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def stop():
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)

    return f"http://127.0.0.1:{httpd.server_address[1]}", cfg, stop


_AUTH = {"Authorization": "Basic " + base64.b64encode(b"tester:s3cret").decode()}


def _request(url, data=None, headers=None, method=None):
    """Returns (status, body_bytes); an HTTP error status is a result here,
    not an exception."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _upload(base, filename, payload, headers=None):
    body = (b'--B\r\nContent-Disposition: form-data; name="file"; filename="'
            + filename.encode() + b'"\r\nContent-Type: text/csv\r\n\r\n'
            + payload + b"\r\n--B--\r\n")
    h = dict(headers if headers is not None else _AUTH)
    h["Content-Type"] = "multipart/form-data; boundary=B"
    status, raw = _request(base + "/api/upload", body, h, "POST")
    return status, json.loads(raw)


def _group_9_server():
    group(9, "http server, upload and file management")
    import os

    from .server import AuthConfigError, ConfigError, ServerConfig, safe_name

    # Credentials arrive as env vars in every deployment, so from_env is the
    # real configuration surface — and its dangerous failure is fail-open.
    def _cfg(**env):
        saved = {k: os.environ.get(k) for k in
                 ("RH_DASHBOARD_USER", "RH_DASHBOARD_PASSWORD",
                  "RH_DASHBOARD_AUTH_REQUIRED", "RH_DASHBOARD_COST_BASIS")}
        try:
            for k in saved:
                os.environ.pop(k, None)
            os.environ.update(env)
            return ServerConfig.from_env("in", "out")
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

    check("no credentials means auth is off", _cfg().auth_required, False)
    check("both credentials means auth is on",
          _cfg(RH_DASHBOARD_USER="u", RH_DASHBOARD_PASSWORD="p").auth_required, True)
    check("a username alone does not half-enable auth",
          _cfg(RH_DASHBOARD_USER="u").auth_required, False)

    # Cost basis arrives the same way, and an unrecognised value must refuse
    # rather than fall back — serving figures computed a different way than the
    # operator asked for is a failure that never announces itself.
    check("cost basis defaults to average when unset", _cfg().cost_basis,
          CostBasis.AVERAGE)
    check("cost basis can be set from the environment",
          _cfg(RH_DASHBOARD_COST_BASIS="fifo").cost_basis, CostBasis.FIFO)
    check("cost basis tolerates surrounding whitespace and case",
          _cfg(RH_DASHBOARD_COST_BASIS="  FIFO ").cost_basis, CostBasis.FIFO)
    refused = False
    try:
        _cfg(RH_DASHBOARD_COST_BASIS="lifo")
    except ConfigError:
        refused = True
    check("an unrecognised cost basis refuses to start", refused, True)
    check("an explicit argument beats the environment",
          ServerConfig.from_env("in", "out", cost_basis=CostBasis.FIFO).cost_basis,
          CostBasis.FIFO)

    # ...which only works if the CLI flag leaves the decision open when it was
    # not given. An argparse default of "average" here made the environment
    # variable look like it did nothing at all: `serve` read the flag's default,
    # never the variable, and started happily on a value it should have
    # refused. The flag beats the environment; its *absence* must not.
    from .cli import build_parser
    check("the serve flag defaults to None so the environment can decide",
          build_parser().parse_args(["serve"]).cost_basis, None)
    check("the serve flag is honoured when given",
          build_parser().parse_args(["serve", "--cost-basis", "fifo"]).cost_basis,
          "fifo")
    check("the build flag defaults to None too",
          build_parser().parse_args(["build"]).cost_basis, None)
    check("an empty password does not enable auth",
          _cfg(RH_DASHBOARD_USER="u", RH_DASHBOARD_PASSWORD="").auth_required, False)

    def _raises(**env):
        try:
            _cfg(**env)
        except AuthConfigError:
            return True
        return False

    check("auth demanded but no credentials refuses to start",
          _raises(RH_DASHBOARD_AUTH_REQUIRED="true"), True)
    check("auth demanded with only a username refuses to start",
          _raises(RH_DASHBOARD_AUTH_REQUIRED="true", RH_DASHBOARD_USER="u"), True)
    check("auth demanded with an empty password refuses to start",
          _raises(RH_DASHBOARD_AUTH_REQUIRED="true",
                  RH_DASHBOARD_USER="u", RH_DASHBOARD_PASSWORD=""), True)
    check("auth demanded and satisfied starts normally",
          _raises(RH_DASHBOARD_AUTH_REQUIRED="true",
                  RH_DASHBOARD_USER="u", RH_DASHBOARD_PASSWORD="p"), False)
    check("the demand flag is opt-in, not any truthy-looking string",
          _raises(RH_DASHBOARD_AUTH_REQUIRED="false"), False)

    # filename sanitising, before any socket is involved
    check("traversal is reduced to a basename", safe_name("../../etc/evil.csv"), "evil.csv")
    check("backslash traversal is reduced too",
          safe_name(r"..\..\windows\evil.csv"), "evil.csv")
    check("a non-csv name is refused", safe_name("notes.txt"), None)
    check("a bare dotfile name is refused", safe_name(".csv"), None)
    check("an ordinary name survives intact",
          safe_name("2026-06-statement.csv"), "2026-06-statement.csv")

    base, cfg, stop = _serve_temp()
    try:
        csv_bytes = (SAMPLE / "2026-06-statement.csv").read_bytes()

        # health and auth
        status, raw = _request(base + "/healthz")
        check("healthz answers without credentials", status, 200)
        check("healthz reports ok", json.loads(raw)["status"], "ok")
        check("the dashboard demands credentials", _request(base + "/")[0], 401)
        bad = {"Authorization": "Basic " + base64.b64encode(b"tester:wrong").decode()}
        check("a wrong password is refused", _request(base + "/", headers=bad)[0], 401)
        check("a wrong user is refused", _request(
            base + "/",
            headers={"Authorization": "Basic "
                     + base64.b64encode(b"nobody:s3cret").decode()})[0], 401)
        check("a garbled auth header is refused",
              _request(base + "/", headers={"Authorization": "Basic !!!"})[0], 401)

        # an empty volume is a normal starting state, not a 500
        status, body = _request(base + "/", headers=_AUTH)
        page = body.decode("utf-8")
        check("an empty input folder still serves a page", status, 200)
        check("the empty page says there is nothing yet", "Nothing to show yet" in page, True)
        check("the empty page still offers the upload dialog", 'id="files-dialog"' in page, True)
        # Separate document from build_page's, so it needs its own check.
        check("the empty page carries the favicon too",
              'href="data:image/png;base64,' in page, True)
        check("the empty page makes no external request",
              "http://" in page or "https://" in page, False)

        # rejections, each with a reason the dialog can show
        status, res = _upload(base, "notes.txt", csv_bytes)
        check("a non-csv upload is rejected", status, 400)
        check("the non-csv rejection names the reason",
              "*.csv" in res["detail"], True)
        check("an empty upload is rejected", _upload(base, "empty.csv", b"")[0], 400)
        check("an upload with no transactions is rejected",
              _upload(base, "none.csv", b"Activity Date,Trans Code,Amount\n")[0], 400)
        status, res = _upload(base, "cols.csv", b"Activity Date,Trans Code\n06/02/2026,Buy\n")
        check("a csv missing a required column is rejected", status, 400)
        check("the rejection names the missing column", "amount" in res["detail"], True)
        check("an oversized body is rejected",
              _upload(base, "big.csv", b"x" * (65 * 1024))[0], 413)
        check("an upload without credentials is refused",
              _upload(base, "sneak.csv", csv_bytes, headers={})[0], 401)
        check("nothing rejected was written to the volume",
              list(cfg.input_dir.glob("*.csv")), [])

        # the happy path
        status, res = _upload(base, "2026-06-statement.csv", csv_bytes)
        check("a valid statement is accepted", status, 200)
        check("the accepted upload reports its name", res["saved_as"],
              "2026-06-statement.csv")
        check("the accepted upload reports its row count", res["rows"], 12)
        check("the file landed in the input folder",
              (cfg.input_dir / "2026-06-statement.csv").is_file(), True)

        # re-uploading the same export is how you find out it's already there
        status, res = _upload(base, "2026-06-statement.csv", csv_bytes)
        check("an identical re-upload is a no-op", res["status"], "duplicate")
        check("an identical re-upload writes no second file",
              len(list(cfg.input_dir.glob("*.csv"))), 1)

        # different bytes under the same name are a different statement
        status, res = _upload(base, "2026-06-statement.csv", csv_bytes + b"\n")
        check("a changed file under the same name is kept separately",
              res["saved_as"], "2026-06-statement-2.csv")
        check("the original was not overwritten",
              (cfg.input_dir / "2026-06-statement.csv").read_bytes(), csv_bytes)

        # traversal survives the whole round trip, not just safe_name()
        status, res = _upload(base, "../../evil.csv", csv_bytes)
        check("a traversing filename is written inside the input folder",
              (cfg.input_dir / "evil.csv").is_file(), True)
        check("and nowhere above it", (cfg.input_dir.parent / "evil.csv").exists(), False)

        # listing
        status, raw = _request(base + "/api/files", headers=_AUTH)
        names = [f["name"] for f in json.loads(raw)["files"]]
        check("the file listing shows every csv", len(names), 3)
        check("the listing reports parsed row counts",
              json.loads(raw)["files"][0]["rows"] > 0, True)

        # the built page, and the cache noticing the volume changed
        status, body = _request(base + "/", headers=_AUTH)
        page = body.decode("utf-8")
        check("the dashboard builds from the uploaded file", status, 200)
        check("the built page reports net income", "Net income" in page, True)
        check("the served page carries the upload control", 'id="open-files"' in page, True)
        check("the served page is still self-contained", "<script src=" in page, False)
        check("the served page still links no stylesheet", 'rel="stylesheet"' in page, False)

        # delete
        hdr = dict(_AUTH)
        hdr["Content-Type"] = "application/json"
        status, raw = _request(base + "/api/files/delete", b'{"name": "evil.csv"}',
                               hdr, "POST")
        check("deleting a file succeeds", json.loads(raw)["status"], "deleted")
        check("the file is gone", (cfg.input_dir / "evil.csv").exists(), False)
        check("deleting a missing file 404s",
              _request(base + "/api/files/delete", b'{"name": "gone.csv"}',
                       hdr, "POST")[0], 404)
        check("deleting a traversing path is refused",
              _request(base + "/api/files/delete", b'{"name": "../../../etc/passwd"}',
                       hdr, "POST")[0], 400)
        check("an unknown route 404s", _request(base + "/nope", headers=_AUTH)[0], 404)
    finally:
        stop()


def _group_10_store():
    """The declared dependency, proven to actually load and work.

    This exists so "the image builds" means something. DuckDB is the first
    runtime dependency this project has ever had, and the interesting failure
    is not a syntax error — it is a wheel that does not exist for the
    interpreter or the architecture in front of it. The homelab is arm64, so
    the assertion that matters is made *inside* the built container by the CI
    image job, not only on a developer laptop.

    Nothing in the pipeline imports DuckDB yet; the store lands with the
    date-window work. Until then this is the whole of its surface.
    """
    group(10, "analytical store")
    try:
        import duckdb
    except ImportError as e:                         # pragma: no cover - env
        check(f"duckdb imports ({e}) — run `uv sync`, then "
              f"`uv run ./rh-dashboard selftest`", False, True)
        return

    check("duckdb reports a version", bool(duckdb.__version__), True)
    con = duckdb.connect(":memory:")
    try:
        check("duckdb evaluates a trivial query",
              con.sql("select 42 as answer").fetchone(), (42,))
        # A round trip through a real table, since that is what the store will
        # do: the engine has to hold typed columns, not just answer literals.
        con.execute("create table t (ticker varchar, qty double, d date)")
        con.execute("insert into t values ('AAPL', 150.0, date '2026-07-05')")
        row = con.sql("select ticker, qty, d from t").fetchone()
        check("duckdb round-trips a typed row", (row[0], row[1]), ("AAPL", 150.0))
        check("duckdb returns a real date", row[2], date(2026, 7, 5))
        # Window functions are the reason for choosing it over hand-rolled
        # aggregation; assert one works rather than assuming.
        con.execute("insert into t values ('AAPL', 50.0, date '2026-07-06')")
        running = con.sql("select sum(qty) over (order by d) from t order by d").fetchall()
        check("duckdb computes a running total", [r[0] for r in running], [150.0, 200.0])
    finally:
        con.close()


# Hand-derived from sample_data for the two calendar-month windows. Worked out
# from the CSVs, not read off the engine:
#
#   JUNE  cash  -25000-18000-15000-5500+5600-2405 (equity) +320 (STO)
#               +30000 (ACH) +8.75 -2.50 -12.34 -5.00        -> -29,996.09
#         realized  SPY (560-550)*10 = +100 ; the STO only opens -> +100
#         net       100 + 8.75 - 2.50 - 12.34 - 5.00          ->     +88.91
#         open      TSLA 25,000 + AAPL 200*165 = 33,000 + CCIV 2,405
#                                                  = 60,405 (400 shares)
#         options   the STO's +320 is still open
#
#   JULY  cash  +9500 -110 +3.10 -2000 +500 -2700 -5.00 +1.15 ->  +5,189.25
#         realized  AAPL (190-165)*50 = +1,250 ; BTC closes the STO -> +210
#         net       1250 + 210 + 3.10 - 5.00 + 1.15           ->  +1,459.25
#         open      TSLA 25,000 + AAPL 150*165 = 24,750 + LCID 2,405
#                            + NVDA 2,700 = 54,855 (370 shares)
#
# The delta identity, spelled out for each:
#   JUNE     88.91 - (60,405 - 0)      + (320 - 0)   + 30,000  = -29,996.09
#   JULY  1,459.25 - (54,855 - 60,405) + (0 - 320)   -  1,500  =  +5,189.25
#
# And the two windows must sum to the full range, on both income and cash.
JUNE = Window(date(2026, 6, 1), date(2026, 6, 30))
JULY = Window(date(2026, 7, 1), date(2026, 7, 31))


def _windowed(window: Window, cost_basis=CostBasis.AVERAGE):
    """The three-pass windowed report over sample_data."""
    lr = load_folder(SAMPLE)
    dd = dedupe(lr.transactions)
    cls = categorize_all(dd.kept)
    reported, opening = compute_windowed(cls, window, cost_basis=cost_basis)
    m = compute([c for c in cls if window.contains(c.txn.activity_date)],
                reported, dd.removed, lr.row_errors,
                window=window, opening=opening)
    return cls, reported, opening, m


def _group_11_window():
    group(11, "date windows")

    # --- the whole point: slicing rows first would destroy cost basis -------
    # AAPL is bought in June and part-sold in July. Match the July rows alone
    # and the +1,250 gain becomes +9,500 of pure proceeds.
    lr = load_folder(SAMPLE)
    cls = categorize_all(dedupe(lr.transactions).kept)
    naive = compute_positions([c for c in cls if JULY.contains(c.txn.activity_date)])
    check("slicing rows before matching invents a gain",
          naive.realized(Category.EQUITY), 9500.0)
    check("...and complains about an oversell it caused itself",
          any("more unit(s)" in w for w in naive.warnings), True)

    _, jun_pos, jun_open, jun = _windowed(JUNE)
    _, jul_pos, jul_open, jul = _windowed(JULY)

    check("june: net income", jun.net_income, 88.91)
    check("june: total cash", jun.total_cash_movement, -29996.09)
    check("june: equity realized", jun_pos.realized(Category.EQUITY), 100.00)
    check("june: the STO only opened, so options realized nothing",
          jun_pos.realized(Category.OPTIONS), 0.0)
    check("june: opening basis is zero", jun.opening_equity_cost_basis, 0.0)
    check("june: closing basis", jun.open_equity_cost_basis, 60405.00)
    check("june: shares at window end", jun.open_shares, 400.0)
    check("june: the open short option is still carried",
          jun.open_options_net_cash, 320.00)
    check("june: reconciles", jun.reconciles, True)
    check("june: reconciliation error", jun.reconciliation_error, 0.0)

    check("july: net income", jul.net_income, 1459.25)
    check("july: total cash", jul.total_cash_movement, 5189.25)
    check("july: equity realized against the pre-window lots",
          jul_pos.realized(Category.EQUITY), 1250.00)
    check("july: the BTC closes June's short for its full credit",
          jul_pos.realized(Category.OPTIONS), 210.00)
    check("july: opening basis is june's closing basis",
          jul.opening_equity_cost_basis, jun.open_equity_cost_basis)
    check("july: closing basis", jul.open_equity_cost_basis, 54855.00)
    check("july: shares at window end", jul.open_shares, 370.0)
    check("july: reconciles", jul.reconciles, True)
    check("july: reconciliation error", jul.reconciliation_error, 0.0)

    # --- as-of semantics: the inverse of what the full range reports -------
    # In June the exchange has not happened yet. This is the cleanest proof the
    # as-of view is real rather than a filtered final state.
    jun_held = {h.instrument for h in jun_pos.equity_holdings}
    check("june: CCIV is still held", "CCIV" in jun_held, True)
    check("june: LCID does not exist yet", "LCID" in jun_held, False)
    jul_held = {h.instrument for h in jul_pos.equity_holdings}
    check("july: CCIV is gone", "CCIV" in jul_held, False)
    check("july: LCID holds its basis", "LCID" in jul_held, True)

    # --- additivity: disjoint windows must sum to the range containing them -
    _, _, _, full = _windowed(Window(date(2026, 1, 1), date(2026, 12, 31)))
    check("windows are additive on income",
          jun.net_income + jul.net_income, full.net_income)
    check("windows are additive on cash",
          jun.total_cash_movement + jul.total_cash_movement,
          full.total_cash_movement)
    check("a full-range window matches the unwindowed figures",
          (full.net_income, full.total_cash_movement, full.open_shares),
          (EXPECTED_NET_INCOME, EXPECTED_TOTAL_CASH, EXPECTED_OPEN_SHARES))
    check("a full-range window opens at zero",
          (full.opening_equity_cost_basis, full.opening_options_net_cash), (0.0, 0.0))

    # --- months come from the window, not from the rows --------------------
    check("june months", jun.months, ["2026-06"])
    check("july months", jul.months, ["2026-07"])
    check("the reported range is the window itself",
          jul.date_range, ("2026-07-01", "2026-07-31"))
    check("cumulative net income lands on the windowed total",
          jul.net_income_cumulative[-1], jul.net_income)

    # --- an empty window is a legitimate report, not zeros ------------------
    _, quiet_pos, _, quiet = _windowed(Window(date(2026, 7, 1), date(2026, 7, 4)))
    check("empty window: no rows", quiet.txn_count, 0)
    check("empty window: no income", quiet.net_income, 0.0)
    check("empty window: no cash", quiet.total_cash_movement, 0.0)
    check("empty window: still reports what was being held",
          quiet.open_shares, 400.0)
    check("empty window: ...at its real cost, not zero",
          quiet.open_equity_cost_basis, 60405.00)
    check("empty window: the month axis survives", quiet.months, ["2026-07"])
    check("empty window: reconciles", quiet.reconciles, True)

    # --- one day, the day the sale happens ---------------------------------
    _, day_pos, day_open, day = _windowed(Window(date(2026, 7, 5), date(2026, 7, 5)))
    check("one day: income is the realized gain", day.net_income, 1250.00)
    check("one day: cash is the full proceeds", day.total_cash_movement, 9500.00)
    check("one day: basis falls by the cost of what was sold",
          day.open_equity_cost_basis - day.opening_equity_cost_basis, -8250.00)
    check("one day: reconciles across the boundary", day.reconciles, True)

    # --- the prefix property the whole design rests on ---------------------
    # The fold is a deterministic left-to-right pass, so the events of a
    # baseline run must BE the pre-window prefix of the as-of-end run. If this
    # ever stopped holding, every windowed figure would be quietly wrong.
    as_of_all = compute_positions([c for c in cls if JULY.as_of(c.txn.activity_date)])
    prefix = [(e.activity_date, e.key, round(e.amount, 6))
              for e in as_of_all.realized_events if e.activity_date < JULY.start]
    baseline = [(e.activity_date, e.key, round(e.amount, 6))
                for e in jul_open.realized_events]
    check("baseline events are exactly the pre-window prefix", baseline, prefix)

    # --- cost basis and windows compose ------------------------------------
    _, jul_f_pos, _, jul_f = _windowed(JULY, cost_basis=CostBasis.FIFO)
    check("july under fifo: equity realized",
          jul_f_pos.realized(Category.EQUITY), 500.00)
    check("july under fifo: net income", jul_f.net_income, 709.25)
    check("july under fifo: closing basis", jul_f.open_equity_cost_basis, 54105.00)
    check("july under fifo: same cash as average cost",
          jul_f.total_cash_movement, jul.total_cash_movement)
    check("july under fifo: reconciles", jul_f.reconciles, True)
    check("both modes open june at the same basis, nothing having been sold",
          _windowed(JULY, cost_basis=CostBasis.FIFO)[3].opening_equity_cost_basis,
          jul.opening_equity_cost_basis)


def _group_12_window_page():
    """The window reaching the page, and the page agreeing with itself."""
    group(12, "windowed dashboard")
    tmp = Path(tempfile.mkdtemp())
    try:
        res = build_dashboard(input_dir=SAMPLE, output_dir=tmp,
                              filename="july.html",
                              start=date(2026, 7, 1), end=date(2026, 7, 31))
        check("windowed build reports the window",
              res["window"], ("2026-07-01", "2026-07-31"))
        check("windowed build still reports the statements' own span",
              res["full_range"], ("2026-06-02", "2026-07-28"))
        check("windowed build net income", res["net_income"], 1459.25)
        check("windowed build open shares", res["open_shares"], 370.0)
        check("windowed build reconciles", res["reconciles"], True)

        html = Path(res["output"]).read_text(encoding="utf-8")
        check("the page names the window",
              "Showing 2026-07-01 to 2026-07-31" in html, True)
        check("the page says lots were matched over the whole history",
              "2026-06-02 to 2026-07-28" in html, True)
        check("the page dates its holdings", "Held as of 2026-07-31" in html, True)

        # The reconciliation column has to show the CHANGE across the window,
        # with both endpoints, or the reader cannot see where it came from.
        check("the equity row shows both endpoints",
              "Open equity cost basis $60,405.00 \u2192 $54,855.00" in html, True)
        # ...and this one is the either-endpoint guard: the short option is
        # fully closed inside the window, so it ends at zero. Testing only the
        # end would suppress a row carrying a real -$320.
        check("a position closed inside the window still gets a row",
              "Open option cash $320.00 \u2192 $0.00" in html, True)
        check("the arrow is a character, not an escaped entity",
              "&amp;rarr;" in html, False)

        # The column must add up *as printed*. Asserting the underlying
        # identity is not the same thing: reporting levels where the identity
        # needs deltas leaves every number individually defensible and the
        # visible column silently wrong, which is exactly the failure a reader
        # would trust. So parse what was rendered and sum it.
        rows = _calc_column(html, "Reconciled to cash that moved")
        check("the cash column has a total row", rows[-1][0].startswith("Total"), True)
        check("the printed cash rows sum to the printed total",
              sum(v for _, v in rows[:-1]), rows[-1][1])
        income = _calc_column(html, "How net income is calculated")
        check("the printed income rows sum to the printed net income",
              sum(v for _, v in income[:-1]), income[-1][1])
        check("both columns agree on net income", income[-1][1], rows[0][1])

        # An unwindowed page must carry none of that chrome.
        plain = build_dashboard(input_dir=SAMPLE, output_dir=tmp,
                                filename="plain.html")
        plain_html = Path(plain["output"]).read_text(encoding="utf-8")
        check("no window means no window callout",
              "Showing 2026-07-01" in plain_html, False)
        check("no window means no delta arrows in the cash column",
              "\u2192" in plain_html.split("Reconciled to cash that moved")[1]
              .split("</table>")[0], False)
        check("the unwindowed result reports no window", plain["window"], None)

        # Open-ended requests resolve against the data rather than being refused.
        since = build_dashboard(input_dir=SAMPLE, output_dir=tmp,
                                filename="since.html", start=date(2026, 7, 1))
        check("--from alone runs to the last row on file",
              since["window"], ("2026-07-01", "2026-07-28"))
        check("--from alone reports the same income as the full month",
              since["net_income"], 1459.25)
        until = build_dashboard(input_dir=SAMPLE, output_dir=tmp,
                                filename="until.html", end=date(2026, 6, 30))
        check("--to alone starts at the first row on file",
              until["window"], ("2026-06-02", "2026-06-30"))
        check("--to alone reports june", until["net_income"], 88.91)

        # Whatever the window, the page must still open offline.
        for name, page in (("windowed", html), ("open-ended", Path(
                since["output"]).read_text(encoding="utf-8"))):
            check(f"{name} page makes no network request",
                  bool(re.search(r"https?://", page)), False)
            check(f"{name} page links no external script or stylesheet",
                  ("<script src=" in page or 'rel="stylesheet"' in page), False)
    finally:
        shutil.rmtree(tmp)


def _group_13_by_ticker():
    """Net income split per ticker, and the identity that keeps it honest."""
    group(13, "per-ticker attribution")
    _, _, _, _, m = _sample_metrics()
    by = {t.ticker: t for t in m.by_ticker}

    # Attribution reads the Instrument column, which already carries the ticker
    # on a dividend row and the *underlying* on an option row. Asserting the
    # split per ticker is what proves that, rather than a rule that happens to
    # work on this fixture.
    check("AAPL realized equity", by["AAPL"].realized_equity, 1250.00)
    check("AAPL realized options come from the underlying, not the contract",
          by["AAPL"].realized_options, 210.00)
    check("AAPL dividends", by["AAPL"].dividends, 3.10)
    check("AAPL net contribution", by["AAPL"].net_contribution, 1463.10)
    check("AAPL shares still held", by["AAPL"].shares, 150.0)

    check("SPY realized equity", by["SPY"].realized_equity, 100.00)
    check("SPY dividends", by["SPY"].dividends, 8.75)
    check("SPY net contribution", by["SPY"].net_contribution, 108.75)
    check("SPY is fully closed, so nothing is held", by["SPY"].shares, 0.0)

    check("a position never sold contributes nothing",
          by["TSLA"].net_contribution, 0.0)
    check("...but is still reported as held", by["TSLA"].shares, 100.0)
    check("shares received in an exchange are held under the new ticker",
          by["LCID"].shares, 100.0)
    check("the surrendered ticker is gone from the rollup", "CCIV" in by, False)

    # Account-level costs belong to no ticker. Naming them is what keeps the
    # column summing; spreading them across tickers would be a guess.
    u = m.unattributed
    check("account-level costs land in Unattributed",
          u.other_income, -23.69)          # -2.50 fees -12.34 margin -10 gold +1.15
    check("Unattributed holds no shares", u.shares, 0.0)
    check("Unattributed claims no realized trades",
          (u.realized_equity, u.realized_options), (0.0, 0.0))

    # The identity: nothing dropped, nothing double-counted.
    check("per-ticker contributions plus unattributed equal net income",
          sum(t.net_contribution for t in m.by_ticker) + u.net_contribution,
          m.net_income)
    check("attribution error is zero", m.ticker_attribution_error, 0.0)

    # It has to hold under the other cost basis too, at different numbers.
    lr = load_folder(SAMPLE)
    dd = dedupe(lr.transactions)
    cls = categorize_all(dd.kept)
    fifo_pos = compute_positions(cls, cost_basis=CostBasis.FIFO)
    fifo_m = compute(cls, fifo_pos, dd.removed, lr.row_errors)
    fifo_by = {t.ticker: t for t in fifo_m.by_ticker}
    check("fifo: AAPL contributes less, the rest sitting in open basis",
          fifo_by["AAPL"].net_contribution, 713.10)
    check("fifo: attribution still closes", fifo_m.ticker_attribution_error, 0.0)
    check("fifo: the mode does not move an unattributed cost",
          fifo_m.unattributed.other_income, m.unattributed.other_income)

    # ...and under a window, where the split is over the window only.
    _, _, _, jul = _windowed(JULY)
    jul_by = {t.ticker: t for t in jul.by_ticker}
    check("july: AAPL contributes the windowed figures",
          jul_by["AAPL"].net_contribution, 1463.10)
    check("july: SPY closed before the window and contributes nothing here",
          "SPY" in jul_by, False)
    check("july: unattributed is only the window's account costs",
          jul.unattributed.other_income, -3.85)        # -5.00 gold +1.15 lending
    check("july: attribution closes on the windowed net income",
          jul.ticker_attribution_error, 0.0)
    check("july: a held position still shows its as-of-end shares",
          jul_by["TSLA"].shares, 100.0)

    # And the rendered table must add up as printed, same rule as the
    # reconciliation columns — a split that only balances in the model is no
    # use to the person reading the page.
    tmp = Path(tempfile.mkdtemp())
    try:
        res = build_dashboard(input_dir=SAMPLE, output_dir=tmp)
        page = Path(res["output"]).read_text(encoding="utf-8")
        check("the page carries a by-ticker table", 'id="by-ticker"' in page, True)
        printed = _ticker_column(page)
        check("the printed ticker rows sum to the printed total",
              sum(v for _, v in printed[:-1]), printed[-1][1])
        check("the printed total is net income", printed[-1][1], m.net_income)
        check("Unattributed is shown, not folded away",
              any(n == "Unattributed" for n, _ in printed), True)
    finally:
        shutil.rmtree(tmp)


def _page_scripts(page: str) -> tuple[list[str], list[str]]:
    """(javascript blocks, json data blocks), refusing anything external."""
    js, data = [], []
    for m in re.finditer(r"<script([^>]*)>(.*?)</script>", page, re.S):
        attrs, body = m.group(1), m.group(2)
        check("no script block loads an external file", "src=" in attrs, False)
        (data if 'type="application/json"' in attrs else js).append(body)
    return js, data


def _row_incomes(page: str) -> dict[str, float]:
    """Per-ticker sum of the data-income the drill-down accumulates."""
    out: dict[str, float] = {}
    for m in re.finditer(r'<tr data-key="[^"]*" data-ticker="([^"]*)"[^>]*'
                         r'data-income="([-0-9.]+)"', page):
        out[m.group(1)] = out.get(m.group(1), 0.0) + float(m.group(2))
    return out


def _group_14_controls():
    """The in-page controls, checked structurally — no browser needed.

    A browser is what would really exercise this, and there isn't one here. But
    most of what can break is static and checkable: JavaScript that references
    an element the page never renders, a JSON data block that is not valid
    JSON, a filter whose values do not match the attributes it filters on, or a
    drill-down whose arithmetic disagrees with the table it was built from.
    """
    group(14, "in-page controls")
    tmp = Path(tempfile.mkdtemp())
    try:
        res = build_dashboard(input_dir=SAMPLE, output_dir=tmp)
        page = Path(res["output"]).read_text(encoding="utf-8")
        _, _, _, _, m = _sample_metrics()

        js, data = _page_scripts(page)
        check("the page carries exactly one script block", len(js), 1)
        for block in data:
            ok = True
            try:
                json.loads(block)
            except ValueError:
                ok = False
            check("every embedded chart data block is valid JSON", ok, True)

        # Anything the script looks up by id must actually be rendered, or the
        # control is dead on arrival and nothing else would say so.
        source = js[0]
        for ident in sorted(set(re.findall(r"getElementById\('([^']+)'\)", source))):
            if ident == "f-outside":
                continue                      # only rendered under a window
            check(f"the script's #{ident} exists on the page",
                  f'id="{ident}"' in page, True)

        check("the category dropdown is present", 'id="f-cat"' in page, True)
        check("the ticker dropdown is present", 'id="f-ticker"' in page, True)
        check("no window means no out-of-window toggle",
              'id="f-outside"' in page, False)
        check("the legend no longer carries filter state",
              "data-active" in page, False)

        # Every value the dropdowns offer has to match an attribute on some row,
        # or selecting it silently empties the table.
        cats = set(re.findall(r'<select id="f-cat">.*?</select>', page, re.S)[0]
                   .split('value="')[1:])
        offered = {c.split('"')[0] for c in cats} - {""}
        present = set(re.findall(r'<tr data-key="([^"]*)"', page))
        check("every category the dropdown offers exists on a row",
              sorted(offered - present), [])
        tickers = {t.split('"')[0] for t in
                   re.findall(r'<select id="f-ticker">.*?</select>', page, re.S)[0]
                   .split('value="')[1:]} - {""}
        row_tickers = set(re.findall(r'data-ticker="([^"]*)"', page))
        check("every ticker the dropdown offers exists on a row",
              sorted(tickers - row_tickers), [])

        # The drill-down accumulates data-income, so its running total IS the
        # by-ticker figure. Assert the source data agrees rather than trusting
        # two calculations to stay in step.
        per_row = _row_incomes(page)
        for t in m.by_ticker:
            if t.ticker in per_row:
                check(f"{t.ticker}: row incomes accumulate to its rollup figure",
                      per_row[t.ticker], t.net_contribution)
        check("row incomes across every ticker sum to net income",
              sum(per_row.values()), m.net_income)

        # --- windowed: the rows behind a reported gain stay reachable -------
        wres = build_dashboard(input_dir=SAMPLE, output_dir=tmp, filename="w.html",
                               start=date(2026, 7, 1), end=date(2026, 7, 31))
        wpage = Path(wres["output"]).read_text(encoding="utf-8")
        check("a window renders the out-of-window toggle",
              'id="f-outside"' in wpage, True)
        check("the heading says how many of how many",
              "Transactions in window (10 of 22)" in wpage, True)
        inside = len(re.findall(r'data-in-window="true"', wpage))
        outside = len(re.findall(r'data-in-window="false"', wpage))
        check("in-window rows are marked", inside, 10)
        check("out-of-window rows are kept, not dropped", outside, 12)
        check("the june purchase behind july's gain is on the page",
              'data-ticker="AAPL" data-date="2026-06-03"' in wpage, True)
        check("...and marked as outside the window",
              bool(re.search(r'data-date="2026-06-03"[^>]*data-in-window="false"',
                             wpage)), True)

        # Under a window the drill-down must accumulate the WINDOW's figures.
        wper = _row_incomes(wpage)
        _, _, _, jul = _windowed(JULY)
        jul_by = {t.ticker: t for t in jul.by_ticker}
        check("windowed row incomes accumulate to the windowed rollup",
              wper["AAPL"], jul_by["AAPL"].net_contribution)
        check("a row outside the window contributes nothing to this report",
              wper.get("SPY", 0.0), 0.0)
        check("windowed row incomes sum to the windowed net income",
              sum(wper.values()), jul.net_income)

        for name, p_ in (("plain", page), ("windowed", wpage)):
            check(f"{name} page still makes no network request",
                  bool(re.search(r"https?://", p_)), False)
            check(f"{name} page links no external stylesheet",
                  'rel="stylesheet"' in p_, False)
    finally:
        shutil.rmtree(tmp)


def run_selftest() -> bool:
    _group_1_parsing()
    _group_2_headers()
    _group_3_fifo()
    _group_4_sample_totals()
    _group_5_categorize()
    _group_6_dedupe()
    _group_7_reconciliation()
    _group_8_dashboard()
    _group_9_server()
    _group_10_store()
    _group_11_window()
    _group_12_window_page()
    _group_13_by_ticker()
    _group_14_controls()

    print()
    if FAILS:
        print(f"FAILED — {len(FAILS)} of {CHECKS} checks failed:")
        for f in FAILS:
            print(f"  - {f}")
        return False
    print(f"PASSED — {CHECKS} explicit assertions across {GROUPS} groups")
    return True


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_selftest() else 1)
