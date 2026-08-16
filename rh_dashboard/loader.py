"""
Find every CSV in a folder and parse it into Transactions.

Robinhood's exported statement columns, as documented and commonly seen in
account-activity CSVs:

    Activity Date, Process Date, Settle Date, Instrument, Description,
    Trans Code, Quantity, Price, Amount

This has **not been verified against a live Robinhood export** — Robinhood is
unreachable from the environment this was built in (same limitation as
`Production/Options`'s yfinance provider). The header aliases below cover the
spellings documented across export eras; if your file uses a header this
doesn't recognise, `load_folder` reports exactly which file and column, and
`HEADER_ALIASES` is the one place to extend.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .model import Transaction

# canonical field -> accepted header spellings (matched case/space-insensitively)
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "activity_date": ("activity date", "date"),
    "process_date": ("process date",),
    "settle_date": ("settle date", "settlement date"),
    "instrument": ("instrument", "symbol", "ticker"),
    "description": ("description", "desc"),
    "trans_code": ("trans code", "transaction code", "type", "trans_code"),
    "quantity": ("quantity", "qty"),
    "price": ("price",),
    "amount": ("amount", "net amount"),
}
REQUIRED = ("activity_date", "trans_code", "amount")

_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y")
_MONEY_RE = re.compile(r"[^0-9.\-]")


class LoadError(RuntimeError):
    """Raised for a folder-level problem (missing dir, no CSVs)."""


@dataclass
class LoadResult:
    transactions: list[Transaction]
    files_read: list[str]
    row_errors: list[str]      # "<file>:<line> — <reason>", row skipped, run continues


def _normalise_header(fieldnames: list[str]) -> dict[str, str]:
    """Map each canonical field to the actual header string used in this file."""
    lookup = {re.sub(r"\s+", " ", h.strip().lower()): h for h in fieldnames}
    found: dict[str, str] = {}
    for canonical, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias in lookup:
                found[canonical] = lookup[alias]
                break
    return found


def _parse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_money(raw: str) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    cleaned = _MONEY_RE.sub("", raw)
    if cleaned in ("", "-", "."):
        return None
    try:
        val = float(cleaned)
    except ValueError:
        return None
    return -abs(val) if negative else val


def _parse_quantity(raw: str) -> tuple[float | None, str]:
    """
    Returns (magnitude, suffix).

    Robinhood marks the *outgoing* leg of a corporate action with a trailing
    letter on Quantity — "200S" against a matching "200" on the incoming leg
    (see `model.Transaction.shares_removed`). Stripping that letter as noise
    loses the only signal that says which side of a merger, exchange or split
    a row represents, so it is captured rather than discarded.
    """
    raw = (raw or "").strip()
    if not raw:
        return None, ""
    suffix = ""
    trailing = raw[-1]
    if trailing.isalpha():
        suffix = trailing.upper()
        raw = raw[:-1]
    return _parse_money(raw), suffix


def _is_blank_or_trailer(raw: dict, colmap: dict) -> bool:
    """
    True for a row that carries no transaction at all: a blank separator line,
    or the disclaimer paragraph Robinhood appends after the last record (which
    lands in the CSV as one long unkeyed field). Neither is an error worth
    reporting — they are part of the export's normal shape.
    """
    values = [v for k, v in raw.items() if k is not None and isinstance(v, str)]
    if any((v or "").strip() for v in values):
        return False
    # every mapped column empty; anything left over is a trailer/None-key blob
    return True


def load_file(path: Path) -> tuple[list[Transaction], list[str]]:
    """Parse one CSV. Never raises for a single bad row — it's recorded and skipped."""
    errors: list[str] = []
    rows: list[Transaction] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            errors.append(f"{path.name}:0 — no header row, file skipped")
            return rows, errors
        colmap = _normalise_header(list(reader.fieldnames))
        missing = [f for f in REQUIRED if f not in colmap]
        if missing:
            errors.append(
                f"{path.name}:0 — missing required column(s) {missing} "
                f"(found headers: {reader.fieldnames}); file skipped")
            return rows, errors

        for i, raw in enumerate(reader, start=2):  # header is line 1
            if _is_blank_or_trailer(raw, colmap):
                continue
            activity = _parse_date(raw.get(colmap["activity_date"], ""))
            raw_amount = (raw.get(colmap["amount"], "") or "").strip()
            trans_code = (raw.get(colmap["trans_code"], "") or "").strip()
            if activity is None:
                errors.append(f"{path.name}:{i} — unparseable activity date, row skipped")
                continue
            if not trans_code:
                errors.append(f"{path.name}:{i} — empty trans code, row skipped")
                continue

            # An EMPTY Amount is meaningful, not malformed: option expirations,
            # assignments, mergers, exchanges and splits move shares or
            # contracts without moving any cash, and Robinhood leaves the
            # column blank for exactly those. Treating blank as "no cash" keeps
            # them in the ledger so they can close positions; only genuinely
            # unparseable text is an error.
            if raw_amount:
                amount = _parse_money(raw_amount)
                if amount is None:
                    errors.append(
                        f"{path.name}:{i} — unparseable amount {raw_amount!r}, row skipped")
                    continue
            else:
                amount = 0.0

            quantity, qty_suffix = _parse_quantity(raw.get(colmap.get("quantity", ""), ""))
            rows.append(Transaction(
                activity_date=activity,
                process_date=_parse_date(raw.get(colmap.get("process_date", ""), "")),
                settle_date=_parse_date(raw.get(colmap.get("settle_date", ""), "")),
                instrument=(raw.get(colmap.get("instrument", ""), "") or "").strip(),
                description=" ".join(
                    (raw.get(colmap.get("description", ""), "") or "").split()),
                trans_code=trans_code,
                quantity=quantity,
                quantity_suffix=qty_suffix,
                price=_parse_money(raw.get(colmap.get("price", ""), "")),
                amount=amount,
                source_file=path.name,
                row_index=i - 1,
            ))
    return rows, errors


def load_folder(folder: str | Path) -> LoadResult:
    p = Path(folder)
    if not p.is_dir():
        raise LoadError(f"input folder not found: {p}")
    csv_paths = sorted(p.glob("*.csv"))
    if not csv_paths:
        raise LoadError(f"no CSV files in {p}")

    all_rows: list[Transaction] = []
    all_errors: list[str] = []
    for path in csv_paths:
        rows, errors = load_file(path)
        all_rows.extend(rows)
        all_errors.extend(errors)

    return LoadResult(transactions=all_rows, files_read=[p.name for p in csv_paths],
                       row_errors=all_errors)
