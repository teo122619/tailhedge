"""CLI entry point for a bare connectivity/data check
(`python -m tailhedge.snapshot_cli`).

Connects to TWS/IB Gateway via `ibkr.py`, prints SPX spot, the VIX term
structure and a slice of the OTM put chain (or, with `--list-expiries`, the
available expiries per tradingClass). No sizing, no ticket — meant to be run
first to confirm the connection and market-data subscriptions are working
before using `advisor_cli.py`/`hedge_cli.py`.
"""

from __future__ import annotations

import argparse
import sys

from tailhedge.ibkr import (
    IBKRConnection,
    MarketSnapshot,
    MarketDataUnavailableError,
    fetch_market_snapshot,
    list_option_params,
    summarize_option_params,
)


def format_snapshot(snap: MarketSnapshot) -> str:
    lines = []
    if snap.spot != snap.spot:  # nan
        lines.append(
            "⚠ SPX spot not available (nan): market closed with no frozen data, "
            "or no subscription. Option IV can only be computed while the market is open."
        )
    lines += [
        f"Spot SPX: {snap.spot:,.2f}   (put expiry: {snap.expiry})",
        "VIX term structure:",
    ]
    for k, v in snap.vix_term.items():
        lines.append(f"  {k}: {'n/a' if v is None else f'{v:.2f}'}")
    lines.append(f"OTM put chain ({len(snap.put_chain)} strikes):")
    lines.append(snap.put_chain.head(20).to_string(index=False))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tailhedge.snapshot")
    p.add_argument("--expiry", help="put expiry YYYYMMDD (required unless --list-expiries)")
    p.add_argument("--band", type=float, default=0.15)
    p.add_argument("--trading-class", default="SPX", help="SPX (monthly/quarterly) or SPXW (weekly)")
    p.add_argument(
        "--list-expiries",
        action="store_true",
        help="list available expiries/tradingClass and exit",
    )
    a = p.parse_args(argv)

    try:
        with IBKRConnection() as ib:
            if a.list_expiries:
                print(summarize_option_params(list_option_params(ib, "SPX")))
                return 0
            if not a.expiry:
                p.error("--expiry is required (or use --list-expiries to discover them)")
            try:
                snap = fetch_market_snapshot(
                    ib, expiry=a.expiry, band=a.band, trading_class=a.trading_class
                )
            except MarketDataUnavailableError as e:
                print(f"Data error: {e}", file=sys.stderr)
                return 1
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
    print(format_snapshot(snap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
