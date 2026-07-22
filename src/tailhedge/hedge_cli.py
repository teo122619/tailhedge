"""CLI entry point for the primary Spitznagel lifecycle
(`python -m tailhedge.hedge_cli`), the main production command.

For one run: sizes coverage from the portfolio (`sizing.py`), reads the
current hedge book from IBKR and rolls anything at DTE <= 30, computes the
cycle budget net of the surviving book (`lifecycle.py`), selects a new put by
moneyness band with SPX->SPY fallback (`advisor.select_by_moneyness`), and
appends the Breeden-Litzenberger diagnostics (`density.py`) — all assembled
into one ticket-only run report.
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import date, datetime
from pathlib import Path

from tailhedge.advisor import (
    INSTRUMENTS, SPX, SPY, build_ticket, format_ticket,
    select_by_moneyness,
)
from tailhedge.lifecycle import (
    available_budget, cycle_budget, format_run_report, next_check_date, roll_due,
)
from tailhedge.paths import default_runs_dir
from tailhedge.sizing import FX_CAVEAT

_MAX_SANE_PCT = 0.10   # 10%/yr: beyond this it's almost certainly a unit error


def parse_pair(raw: str, name: str, conv) -> tuple:
    """'0.35,0.45' → (0.35, 0.45), validating order and cardinality."""
    parts = [x.strip() for x in raw.split(",") if x.strip()]
    if len(parts) != 2:
        raise ValueError(f"--{name}: expected two comma-separated values, e.g. 0.35,0.45")
    lo, hi = conv(parts[0]), conv(parts[1])
    if lo >= hi:
        raise ValueError(f"--{name}: the first value must be lower than the second")
    return lo, hi


def _pct(raw: str) -> float:
    v = float(raw)
    if v > _MAX_SANE_PCT:
        raise argparse.ArgumentTypeError(
            f"{v:g} = {v:.0%}/yr: the flag takes fractions (0.01 = 1%/yr)."
        )
    return v


def _cycles(raw: str) -> int:
    v = int(raw)
    if v < 1:
        raise argparse.ArgumentTypeError(f"{v}: cycles per year must be at least 1.")
    return v


def format_sizing_note(declared: float, nav: float, coverage: float, r_squared: float,
                       n_obs: int, freq: str, has_listings: bool) -> str:
    """Build the sizing block note: positions, NAV, coverage, beta stats, and FX caveat if applicable."""
    note = (f"Sizing: positions ${declared:,.0f}, total NAV ${nav:,.0f}, "
            f"β·portfolio coverage ${coverage:,.0f} "
            f"(R² {r_squared:.2f}, n={n_obs}, {freq} returns)")
    if has_listings:
        note += "\n" + FX_CAVEAT
    return note


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tailhedge.hedge",
        description="Spitznagel lifecycle: roll at DTE 30 + moneyness-based purchase. Ticket-only.",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--portfolio", help="portfolio spreadsheet (positions + total NAV, USD); created if missing")
    g.add_argument("--notional", type=float, help="already-known notional to cover (coverage = NAV budget)")
    p.add_argument("--budget-pct", type=_pct, default=0.01, help="budget %%/yr (default 0.01)")
    p.add_argument("--band", default="0.35,0.45", help="OTM band lo,hi (default 0.35,0.45)")
    p.add_argument("--dte-range", default="90,180", help="entry DTE window")
    p.add_argument("--roll-dte", type=int, default=30, help="sell trigger")
    p.add_argument("--cycles-per-year", type=_cycles, default=6, help="cycles/year (6 = bimonthly)")
    p.add_argument("--exclude-conids", default="", help="conIds to EXCLUDE from the hedge")
    p.add_argument("--force-underlying", choices=["SPX", "SPY"], default=None)
    p.add_argument("--window", type=int, default=None,
                   help="beta window in observations (default: 250 daily / 52 weekly)")
    p.add_argument("--returns-freq", choices=("auto", "daily", "weekly"), default="auto",
                   help="regression frequency; auto = weekly when a non-US listing is declared")
    p.add_argument("--lookback", type=int, default=756)
    p.add_argument("--r", type=float, default=0.0, help="rate for the B-L CDF")
    p.add_argument("--out-dir", default=None, help="default: ./runs")
    return p


def main(argv: list[str] | None = None) -> int:
    p = _build_parser()
    a = p.parse_args(argv)
    try:
        band = parse_pair(a.band, "band", float)
        dte_range = parse_pair(a.dte_range, "dte-range", int)
        exclude = tuple(int(x) for x in a.exclude_conids.split(",") if x.strip())
    except ValueError as e:
        p.error(str(e))

    positions = None
    nav = None
    listings = {}
    if a.portfolio:
        from tailhedge.portfolio import load_portfolio, write_template
        if not Path(a.portfolio).exists():
            write_template(a.portfolio)
            print(f"Template created at {a.portfolio}: fill in your positions + total NAV (USD), then re-run.")
            return 0
        try:
            positions, nav, listings = load_portfolio(a.portfolio)
        except ValueError as e:
            print(f"Portfolio error: {e}", file=sys.stderr)
            return 1

    from tailhedge.density import density_report
    from tailhedge.ibkr import (   # lazy import: offline tests don't touch ib_async
        IBKRConnection, IBKRPriceHistoryProvider, MarketDataUnavailableError,
        available_expiries, expiries_in_dte_range, fetch_put_chain, fetch_spot,
        fetch_puts_multi, list_option_params, read_hedge_book,
    )
    from tailhedge.sizing import InsufficientDataError, resolve_freq_window, run_sizing

    today = date.today()
    sizing_note = ""
    try:
        with IBKRConnection() as ib:
            # 1. sizing: β·portfolio coverage + NAV for the budget (see docs/strategy.md)
            if a.portfolio:
                freq, window = resolve_freq_window(a.returns_freq, a.window, bool(listings))
                rep = run_sizing(IBKRPriceHistoryProvider(ib, listings=listings),
                                 positions=positions, spx_ticker="SPX",
                                 windows=[window], lookback_days=a.lookback, freq=freq)
                coverage = rep.notional_by_window[window]
                row0 = rep.sensitivity.iloc[0]
                sizing_note = format_sizing_note(
                    rep.nav, nav, coverage, float(row0["r_squared"]),
                    int(row0["n_obs"]), freq, bool(listings))
            else:
                coverage = a.notional
                nav = a.notional

            # 2. hedge book from the account -> roll and cycle budget (see docs/strategy.md)
            book = read_hedge_book(ib, today, exclude_conids=exclude)
            # A non-finite mark (market closed / no data) must fail loudly, not be
            # read as a swollen book and silently suppress the purchase (anti-chasing).
            no_mark = [p_ for p_ in book if not math.isfinite(p_.market_value)]
            if no_mark:
                ids = ", ".join(f"{p_.symbol} {p_.expiry} {p_.strike:,.0f} Put "
                                f"(conId {p_.con_id})" for p_ in no_mark)
                raise MarketDataUnavailableError(
                    f"No valid mark for: {ids}. Market closed or no data? Retry while "
                    "the US market is open, or exclude the position with --exclude-conids."
                )
            rolls = roll_due(book, a.roll_dte)
            keep = [p_ for p_ in book if p_ not in rolls]
            target = cycle_budget(nav, a.budget_pct, a.cycles_per_year)
            avail = available_budget(target, keep)

            # 3. purchase: moneyness-based selection with SPX->SPY auto-selection (see docs/strategy.md)
            buy_section = ""
            diagnostics = ""
            if avail <= 0:
                buy_section = ("No purchase: book at/above cycle target "
                               "(anti-chasing).")
            else:
                order = ([INSTRUMENTS[a.force_underlying]] if a.force_underlying
                         else [SPX, SPY])
                last_err = None
                for inst in order:
                    try:
                        spot = fetch_spot(ib, inst.symbol)
                        exps = expiries_in_dte_range(
                            available_expiries(list_option_params(ib, inst.symbol),
                                               inst.trading_class),
                            today, dte_range[0], dte_range[1])
                        if not exps:
                            raise MarketDataUnavailableError(
                                f"No {inst.symbol} expiry with DTE "
                                f"{dte_range[0]}–{dte_range[1]}")
                        eps = 1e-9 * spot   # inclusive band: fetch the edge strikes too
                        chain = fetch_puts_multi(
                            ib, spot, inst.symbol, exps,
                            strike_lo=spot * (1 - band[1]) - eps,
                            strike_hi=spot * (1 - band[0]) + eps,
                            trading_class=inst.trading_class)
                        row = select_by_moneyness(chain, spot, today, avail,
                                                  band, dte_range, inst.multiplier)
                        ticket = build_ticket(row, avail, coverage, spot,
                                              inst.multiplier, nav_total=nav,
                                              symbol=inst.symbol,
                                              sizing_price=float(row["ask"]))
                        buy_section = format_ticket(
                            ticket, budget_label=f"cycle budget ${avail:,.0f}")
                        # 4. diagnostics: B-L on the chosen expiry, full smile
                        try:
                            bl_chain = fetch_put_chain(ib, inst.symbol,
                                                       str(row["expiry"]), 0.50,
                                                       inst.trading_class)
                            diagnostics = density_report(
                                bl_chain, spot, str(row["expiry"]), today, a.r,
                                symbol=inst.symbol, style=inst.style,
                                pays_dividends=inst.pays_dividends)
                        except (ValueError, MarketDataUnavailableError) as e:
                            diagnostics = f"B-L section skipped: {e}"
                        break
                    except (ValueError, MarketDataUnavailableError) as e:
                        last_err = e
                else:
                    buy_section = f"No purchase: {last_err}"
    except (MarketDataUnavailableError, InsufficientDataError) as e:
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

    report = format_run_report(today, rolls, keep, target, avail, buy_section,
                               next_check_date(today, keep, a.roll_dte,
                                               cycle_days=round(365 / a.cycles_per_year)),
                               diagnostics=diagnostics, sizing_note=sizing_note)
    print(report)

    out = Path(a.out_dir) if a.out_dir else default_runs_dir()
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out / f"{stamp}-hedge-run.txt"
    path.write_text(report + "\n")
    print(f"\nSaved to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
