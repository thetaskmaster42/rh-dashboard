"""
The public entry point.

    from rh_dashboard import build_dashboard
    result = build_dashboard(input_dir="input", output_dir="output")

is the whole API. The CLI is a thin wrapper around it, same pattern as
`optionsuite.generate_option_trades` in `Production/Options`.

Pipeline order matters: positions must be matched *before* metrics, because
Equity and Options reach net income as FIFO-realised P&L rather than as raw
cash sums (see `positions.py` and `model.LOT_MATCHED_CATEGORIES`).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from .categorize import categorize_all
from .dashboard import build_page
from .dedupe import dedupe
from .loader import LoadError, load_folder
from .metrics import compute
from .model import INCOME_CATEGORIES, TRANSFER_CATEGORIES, CostBasis, Window
from .positions import compute_positions, compute_windowed


def build_dashboard(input_dir: str | Path = "input",
                    output_dir: str | Path = "output",
                    filename: str = "dashboard.html",
                    interactive: bool = False,
                    cost_basis: CostBasis = CostBasis.AVERAGE,
                    start: date | None = None,
                    end: date | None = None) -> dict:
    """
    Read every CSV in `input_dir`, dedupe, categorise, match positions, and
    write a self-contained HTML dashboard to `output_dir/filename`.

    `interactive` adds the statement-upload chrome to the page; it is set by
    `server.py` and never by the CLI, so a dashboard built on the command line
    stays a plain file with no controls that need a server behind them.

    `cost_basis` picks which open equity lot a sale consumes; it changes when
    P&L is recognised, not how much of it exists. The page states which mode
    produced it, because two dashboards built with different settings look
    identical and disagree.

    `start` and `end` narrow what is *reported*, never how lots are matched —
    see `positions.compute_windowed`. Either may be given alone: a missing end
    means "up to the last row on file", a missing start means "from the first".
    Both omitted is the unwindowed report, byte for byte as before.

    Raises `LoadError` if `input_dir` doesn't exist or has no CSVs — that's a
    setup problem, not a data problem. Bad individual rows are never fatal:
    they're skipped and reported in `row_errors`.
    """
    load_result = load_folder(input_dir)
    dd = dedupe(load_result.transactions)
    classified = categorize_all(dd.kept)

    # The statements' own span, kept separate from whatever range is reported:
    # the page has to be able to say "matched over all of this, shown for that".
    all_dates = [c.txn.activity_date for c in classified]
    full_range = ((min(all_dates).isoformat(), max(all_dates).isoformat())
                  if all_dates else None)

    window = None
    if (start or end) and all_dates:
        # An open-ended request is resolved against the data rather than
        # refused: "everything since April" is a reasonable thing to ask.
        window = Window(start or min(all_dates), end or max(all_dates))

    if window is None:
        positions = compute_positions(classified, cost_basis=cost_basis)
        reported = classified
        opening = None
    else:
        positions, opening = compute_windowed(classified, window,
                                              cost_basis=cost_basis)
        reported = [c for c in classified if window.contains(c.txn.activity_date)]

    metrics = compute(reported, positions, dd.removed, load_result.row_errors,
                      window=window, opening=opening, full_range=full_range)

    html = build_page(metrics, positions, reported, load_result.files_read,
                      load_result.row_errors, interactive=interactive,
                      cost_basis=cost_basis)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename
    out_path.write_text(html, encoding="utf-8")

    return {
        "ok": True,
        "output": str(out_path),
        "files_read": load_result.files_read,
        "transactions_loaded": len(load_result.transactions),
        "transactions_kept": len(dd.kept),
        "duplicates_removed": dd.removed,
        "row_errors": load_result.row_errors,
        "fallback_count": metrics.fallback.count,
        "fallback_codes": metrics.fallback.by_code,
        "position_warnings": positions.warnings,
        "date_range": metrics.date_range,
        "income_by_category": {c.value: metrics.income_of(c) for c in INCOME_CATEGORIES},
        "transfers_by_category": {c.value: metrics.categories[c].cash_total
                                  for c in TRANSFER_CATEGORIES},
        "other_income": metrics.other_income,
        "net_income": metrics.net_income,
        "cost_basis": cost_basis.value,
        "window": (window.start.isoformat(), window.end.isoformat()) if window else None,
        "full_range": full_range,
        "open_shares": metrics.open_shares,
        "open_equity_cost_basis": metrics.open_equity_cost_basis,
        "open_options_net_cash": metrics.open_options_net_cash,
        "open_positions": [
            {"key": h.key, "instrument": h.instrument, "category": h.category.value,
             "quantity": h.quantity, "avg_price": h.avg_price,
             "cost_basis": h.cost_basis, "net_credit": h.net_credit}
            for h in positions.holdings],
        "total_cash_movement": metrics.total_cash_movement,
        "reconciliation_error": metrics.reconciliation_error,
        "reconciles": metrics.reconciles,
    }


__all__ = ["build_dashboard", "LoadError"]
