"""rh-dashboard — command line interface."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .model import CostBasis

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                      # rh-dashboard/
DEFAULT_INPUT = ROOT / "input"
DEFAULT_OUTPUT = ROOT / "output"


def _add_cost_basis(p: argparse.ArgumentParser) -> None:
    """Which open lot a sale consumes. Averaging blends the open lots; FIFO
    takes the oldest first, which is what Robinhood's own 1099-B reports. The
    two differ in *when* P&L lands, not in how much of it there is."""
    # Defaults to None, not to "average": `serve` also reads
    # RH_DASHBOARD_COST_BASIS, and a flag default would shadow the environment
    # so silently that the variable would look like it did nothing.
    p.add_argument("--cost-basis", choices=[c.value for c in CostBasis],
                   default=None,
                   help="equity cost basis for closed lots "
                        f"(default: {CostBasis.AVERAGE.value}; "
                        f"{CostBasis.FIFO.value} matches a 1099-B)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rh-dashboard",
        description="Build a P&L dashboard from a folder of Robinhood statement CSVs.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"rh-dashboard {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    b = sub.add_parser("build", help="read the CSVs and write the dashboard")
    b.add_argument("-i", "--input", type=Path, default=DEFAULT_INPUT,
                   help=f"folder of statement CSVs (default: {DEFAULT_INPUT})")
    b.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT,
                   help=f"output folder (default: {DEFAULT_OUTPUT})")
    b.add_argument("--filename", default="dashboard.html",
                   help="output file name (default: dashboard.html)")
    _add_cost_basis(b)

    v = sub.add_parser("serve", help="serve the dashboard and accept CSV uploads")
    v.add_argument("--host", default="127.0.0.1",
                   help="bind address (default: 127.0.0.1; use 0.0.0.0 in a container)")
    v.add_argument("--port", type=int, default=8080, help="port (default: 8080)")
    v.add_argument("-i", "--input", type=Path, default=DEFAULT_INPUT,
                   help=f"folder of statement CSVs (default: {DEFAULT_INPUT})")
    v.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT,
                   help=f"output folder (default: {DEFAULT_OUTPUT})")
    _add_cost_basis(v)

    sub.add_parser("selftest", help="run the internal verification suite")

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.command:
        build_parser().print_help()
        return 1

    if args.command == "build":
        return _cmd_build(args)
    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "selftest":
        from .selftest import run_selftest
        return 0 if run_selftest() else 1
    return 1


def _cmd_serve(args) -> int:
    from .server import ConfigError, ServerConfig, serve
    try:
        # None lets RH_DASHBOARD_COST_BASIS decide; the flag wins when given.
        cfg = ServerConfig.from_env(
            args.input, args.output,
            cost_basis=CostBasis(args.cost_basis) if args.cost_basis else None)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return serve(args.host, args.port, cfg)


def _cmd_build(args) -> int:
    from .loader import LoadError
    from .pipeline import build_dashboard
    try:
        res = build_dashboard(input_dir=args.input, output_dir=args.output,
                              filename=args.filename,
                              cost_basis=(CostBasis(args.cost_basis)
                                          if args.cost_basis else CostBasis.AVERAGE))
    except LoadError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print()
    print(f"  dashboard   {res['output']}")
    print(f"  sources     {', '.join(res['files_read'])}")
    print(f"  kept        {res['transactions_kept']} of {res['transactions_loaded']} "
          f"transactions ({res['duplicates_removed']} duplicate(s) removed)")
    if res["date_range"]:
        print(f"  date range  {res['date_range'][0]} -> {res['date_range'][1]}")
    if res["row_errors"]:
        print(f"  row errors  {len(res['row_errors'])}")
        for e in res["row_errors"][:20]:
            print(f"      {e}")
        if len(res["row_errors"]) > 20:
            print(f"      ... and {len(res['row_errors']) - 20} more")
    if res["fallback_count"]:
        codes = ", ".join(f"{k}({v})" for k, v in res["fallback_codes"].items())
        print(f"  unclassified {res['fallback_count']} row(s) hit the sign-based "
              f"fallback: {codes}")
    for w in res["position_warnings"][:10]:
        print(f"  lot warning {w}")

    print()
    print("  NET INCOME (realized)")
    for cat, total in res["income_by_category"].items():
        note = "  realized P&L, closed only" if cat in ("Equity", "Options") else ""
        print(f"    {cat:22s} {total:>14,.2f}{note}")
    if res["other_income"]:
        print(f"    {'Other (unclassified)':22s} {res['other_income']:>14,.2f}")
    print(f"    {'-' * 38}")
    print(f"    {'NET INCOME':22s} {res['net_income']:>14,.2f}")

    print()
    print("  NOT INCOME (holdings and transfers)")
    print(f"    {'Open equity':22s} {res['open_equity_cost_basis']:>14,.2f}  "
          f"{res['open_shares']:g} shares held, at cost")
    for h in res["open_positions"]:
        if h["category"] == "Equity":
            print(f"      {h['instrument'] or h['key']:<10s} {h['quantity']:>8g} sh  "
                  f"@ {h['avg_price']:>10,.2f}  = {h['cost_basis']:>12,.2f}")
    if abs(res["open_options_net_cash"]) > 0.005:
        print(f"    {'Open options':22s} {res['open_options_net_cash']:>14,.2f}  net cash")
    for cat, total in res["transfers_by_category"].items():
        print(f"    {cat:22s} {total:>14,.2f}")
    print(f"    {'-' * 38}")
    print(f"    {'TOTAL CASH MOVEMENT':22s} {res['total_cash_movement']:>14,.2f}")
    if not res["reconciles"]:
        print(f"\n  WARNING: reconciliation off by {res['reconciliation_error']:,.2f} — "
              f"figures are unreliable, please report this")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
