"""
Duplicate removal across overlapping statement exports.

Robinhood's monthly and annual exports overlap in date range, so the same row
appears byte-for-byte in two files. Those are duplicates. But a row repeated
*within one file* is *not* a duplicate — it is two real transactions that
happen to be identical, which is entirely ordinary:

    10/14/2020  STC  1  $2.98   PLUG 10/16/2020 Call $20.00
    10/14/2020  STC  1  $2.98   PLUG 10/16/2020 Call $20.00   <- closes the 2nd contract

    08/31/2021  Buy  100  ($2,017.95)  Lucid Group
    08/31/2021  Buy  100  ($2,017.95)  Lucid Group            <- a second real purchase

Collapsing those to one row silently deletes a transaction: the first case
leaves an option contract open forever (it never gets closed), the second loses
100 shares from the position ledger. Both were observed on real data.

So the rule is **multiplicity-preserving**: for each distinct row, count how
many times it appears in each source file and keep the *maximum* across files,
not one. A row appearing twice in the monthly export and twice in the
overlapping annual export is two transactions, not one and not four.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .model import Transaction


@dataclass
class DedupeResult:
    kept: list[Transaction]
    removed: int
    removed_by_file: dict[str, int]   # source_file -> rows dropped from it


def dedupe(transactions: list[Transaction]) -> DedupeResult:
    # group occurrences of each identical row, remembering which file each came from
    occurrences: dict[tuple, list[Transaction]] = defaultdict(list)
    for txn in transactions:
        occurrences[txn.dedupe_key()].append(txn)

    keep_ids: set[int] = set()
    for group in occurrences.values():
        per_file = Counter(t.source_file for t in group)
        # the file that carries the most copies defines the true multiplicity;
        # ties break on filename order, matching load_folder's sorted read
        best_file = min(per_file, key=lambda f: (-per_file[f], f))
        keep_ids.update(id(t) for t in group if t.source_file == best_file)

    kept, removed_by_file = [], {}
    for txn in transactions:
        if id(txn) in keep_ids:
            kept.append(txn)
        else:
            removed_by_file[txn.source_file] = removed_by_file.get(txn.source_file, 0) + 1

    return DedupeResult(kept=kept, removed=len(transactions) - len(kept),
                        removed_by_file=removed_by_file)
