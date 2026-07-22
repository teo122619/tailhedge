"""CLI entry point for sizing only (`python -m tailhedge.cli`).

Runs the beta/R-squared regression of a positions CSV against SPX across one
or more lookback windows and prints the SPX-equivalent notional to hedge, from
either a prices CSV (`--source csv`, via `data.py`) or a live IBKR connection
(`--source ibkr`, via `ibkr.py`). No option chain, no ticket — that's
`advisor_cli.py`/`hedge_cli.py`'s job; this one wraps `sizing.run_sizing`.
"""

from __future__ import annotations

import argparse

import pandas as pd

import sys

from tailhedge.data import CsvPriceHistoryProvider
from tailhedge.sizing import run_sizing, InsufficientDataError


def _print_report(rep) -> None:
    print(f"NAV: {rep.nav:,.0f}  (SPX ticker: {rep.spx_ticker})")
    print("Beta/R² sensitivity by window:")
    print(rep.sensitivity.to_string(index=False))
    print("SPX-equivalent notional to hedge, by window:")
    for w, n in rep.notional_by_window.items():
        print(f"  window={w:>4}  notional={n:,.0f}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tailhedge.sizing")
    p.add_argument("--source", choices=["csv", "ibkr"], default="csv")
    p.add_argument("--prices", default=None, help="Prices CSV (required with --source csv)")
    p.add_argument("--positions", required=True)
    p.add_argument("--spx", default="SPX")
    p.add_argument("--nav", type=float, default=None)
    p.add_argument("--windows", default="120,250")
    p.add_argument("--lookback", type=int, default=756)
    p.add_argument("--returns-freq", choices=("daily", "weekly"), default="daily",
                   help="regression frequency (no auto: this CLI has no listings input)")
    a = p.parse_args(argv)

    pos_df = pd.read_csv(a.positions)
    positions = dict(zip(pos_df["ticker"], pos_df["market_value"].astype(float)))
    windows = [int(w) for w in a.windows.split(",")]

    try:
        if a.source == "ibkr":
            from tailhedge.ibkr import IBKRConnection, IBKRPriceHistoryProvider

            try:
                with IBKRConnection() as ib:
                    rep = run_sizing(
                        IBKRPriceHistoryProvider(ib),
                        positions=positions,
                        spx_ticker=a.spx,
                        windows=windows,
                        lookback_days=a.lookback,
                        nav=a.nav,
                        freq=a.returns_freq,
                    )
            except OSError as e:   # ConnectionRefusedError, timeout, host unreachable
                from tailhedge.config import IBKRConfig
                cfg = IBKRConfig.from_env()
                print(
                    f"Cannot connect to TWS/IB Gateway at {cfg.host}:{cfg.port} ({e}).\n"
                    "Is TWS or IB Gateway running with the API enabled? "
                    "Set TAILHEDGE_IB_HOST / TAILHEDGE_IB_PORT if you use non-default values.",
                    file=sys.stderr,
                )
                return 1
        else:
            if a.prices is None:
                p.error("--prices is required with --source csv")
            rep = run_sizing(
                CsvPriceHistoryProvider(a.prices),
                positions=positions,
                spx_ticker=a.spx,
                windows=windows,
                lookback_days=a.lookback,
                nav=a.nav,
                freq=a.returns_freq,
            )
    except InsufficientDataError as e:
        print(f"Data error: {e}", file=sys.stderr)
        return 1

    _print_report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
