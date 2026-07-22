"""CLI entry point for the one-shot, delta-targeted advisor
(`python -m tailhedge.advisor_cli`).

Wires config/portfolio sizing/IBKR data fetch into `advisor.run_advisor`
(pure, offline-testable) to print the three-lens comparison table, a
delta-targeted ticket (`--target-delta`, SPX with automatic SPY fallback), and
the Breeden-Litzenberger section from `density.py`, then saves the report
under `paths.default_runs_dir()`. For exploring a specific set of expiries in
detail; the moneyness-driven lifecycle lives in `hedge_cli.py`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from tailhedge.advisor import (
    INSTRUMENTS,
    SPX,
    SPY,
    Instrument,
    build_ticket,
    candidate_table,
    choose_instrument,
    dte_days,
    format_budget_comparison,
    format_ticket,
    premium_budget,
    select_candidate,
    ticket_for,
)
from tailhedge.density import density_report
from tailhedge.paths import default_runs_dir

_TABLE_COLS = [
    "expiry", "strike", "dte", "delta", "iv", "mid",
    "intr20", "intr30", "intr40", "vega_per_prem", "gamma_per_prem",
]


def _format_table(table: pd.DataFrame) -> str:
    df = table[_TABLE_COLS].sort_values(["expiry", "strike"])
    return df.to_string(index=False, float_format=lambda x: f"{x:,.3f}")


def run_advisor(
    chain: pd.DataFrame,
    spot: float,
    notional: float,          # equity notional covered (β·portfolio)
    today: date,
    select_expiry: str,
    target_delta: float,
    annual_budget_pct: float,
    r: float = 0.0,
    budget_nav: float | None = None,   # budget base (total NAV); default = notional
    budget_pcts: list[float] | None = None,   # opt-in: comparison table across levels
    instrument: Instrument = SPX,
    selection_note: str = "",
) -> str:
    """Full report (pure, offline-testable): comparison + ticket + B-L section."""
    nav = notional if budget_nav is None else budget_nav
    mult = instrument.multiplier
    table = candidate_table(chain, spot, today)
    if table.empty:
        raise ValueError(
            "No puts with valid mid/delta in the chain: greeks unavailable "
            "(market closed or frozen data?). Retry while the US market is open."
        )
    row = select_candidate(table, select_expiry, target_delta)
    budget = premium_budget(annual_budget_pct, nav, int(row["dte"]))
    ticket = build_ticket(row, budget, notional, spot, mult,
                          nav_total=nav, symbol=instrument.symbol)
    sel = f"Instrument: {instrument.symbol} ({instrument.note})."
    if selection_note:
        sel += f"\n  {selection_note}"
    parts = [
        "=== Tail-hedge Advisor ===",
        sel,
        f"Spot {spot:,.2f} | equity coverage ${notional:,.0f} | total NAV ${nav:,.0f} | "
        f"budget {annual_budget_pct:.2%}/yr → ${budget:,.0f} over {int(row['dte'])} days",
        "",
        "-- Comparison table (3 model-free lenses, real prices) --",
        _format_table(table),
        "",
        format_ticket(ticket),
    ]
    if budget_pcts:
        parts += ["", format_budget_comparison(row, budget_pcts, nav, notional, spot,
                                               mult, symbol=instrument.symbol)]
    try:
        parts += ["", density_report(chain, spot, select_expiry, today, r,
                                     symbol=instrument.symbol, style=instrument.style,
                                     pays_dividends=instrument.pays_dividends)]
    except ValueError as e:
        parts += ["", f"B-L section skipped: {e}"]
    return "\n".join(parts)


_MAX_SANE_PCT = 0.10   # 10%/yr: beyond this it's almost certainly a unit error


def parse_budget_pcts(raw: str | None) -> list[float] | None:
    """Parse '--budget-pcts 0.005,0.0075,0.01' into fractions. None if the flag is absent."""
    if raw is None:
        return None
    pcts = [float(x.strip()) for x in raw.split(",") if x.strip()]
    for pct in pcts:
        if pct > _MAX_SANE_PCT:
            raise ValueError(
                f"--budget-pcts: {pct:g} = {pct:.0%}/yr. The flag takes fractions "
                f"(0.005 = 0.5%/yr), not percentage points."
            )
    return pcts


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tailhedge.advisor")
    p.add_argument("--expiries", required=True,
                   help="comma-separated YYYYMMDD expiries (all shown in the comparison table)")
    p.add_argument("--select-expiry", default=None,
                   help="expiry of the put to buy (default: the last one in --expiries)")
    p.add_argument("--target-delta", type=float, default=-0.10)
    p.add_argument("--budget-pct", type=float, default=0.01, help="premium budget %%/yr (e.g. 0.01)")
    p.add_argument("--budget-pcts", default=None,
                   help="opt-in: levels to compare on the selected strike, e.g. 0.005,0.0075,0.01")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--portfolio", help="portfolio spreadsheet (positions + total NAV, USD); created if missing")
    g.add_argument("--notional", type=float, help="already-known notional to cover (coverage = NAV budget)")
    p.add_argument("--window", type=int, default=None,
                   help="beta window in observations (default: 250 daily / 52 weekly)")
    p.add_argument("--returns-freq", choices=("auto", "daily", "weekly"), default="auto",
                   help="regression frequency; auto = weekly when a non-US listing is declared")
    p.add_argument("--lookback", type=int, default=756)
    p.add_argument("--band", type=float, default=0.35,
                   help="OTM band of the chain (0.35 also covers the deep OTM of the -40%% lens)")
    p.add_argument("--trading-class", default="SPX",
                   help="override tradingClass for the SPX leg only (advanced)")
    p.add_argument("--force-underlying", choices=["SPX", "SPY"], default=None,
                   help="skip auto-selection and force the instrument")
    p.add_argument("--r", type=float, default=0.0, help="rate for the B-L CDF (0 = ignore discounting)")
    p.add_argument("--out-dir", default=None, help="default: ./runs")
    return p


def resolve_instrument(force: str | None, probe):
    """Choose instrument + selection note (see docs/strategy.md). force takes priority over auto-selection.

    probe: the SPX Ticket from the pre-check (None when force is passed).
    """
    if force:
        return INSTRUMENTS[force], ""
    if choose_instrument(probe.contracts) == "SPX":
        return SPX, ""
    note = (f"SPX skipped: budget ${probe.budget:,.0f} < premium of "
            f"1 SPX contract (~${probe.mid * probe.multiplier:,.0f}).")
    return SPY, note


def main(argv: list[str] | None = None) -> int:
    p = _build_parser()
    a = p.parse_args(argv)

    try:
        budget_pcts = parse_budget_pcts(a.budget_pcts)
    except ValueError as e:
        p.error(str(e))

    expiries = [e.strip() for e in a.expiries.split(",") if e.strip()]
    if not expiries:
        p.error("--expiries: at least one YYYYMMDD expiry is required")
    select_expiry = a.select_expiry or expiries[-1]
    if select_expiry not in expiries:
        p.error(f"--select-expiry {select_expiry} is not in --expiries")

    # Portfolio: scaffold-if-missing, then load (local, before IBKR)
    positions = None
    budget_nav = None
    listings = {}
    if a.portfolio:
        from tailhedge.portfolio import load_portfolio, write_template
        if not Path(a.portfolio).exists():
            write_template(a.portfolio)
            print(f"Template created at {a.portfolio}: fill in your positions + total NAV (USD), then re-run.")
            return 0
        try:
            positions, budget_nav, listings = load_portfolio(a.portfolio)
        except ValueError as e:
            print(f"Portfolio error: {e}", file=sys.stderr)
            return 1

    from tailhedge.ibkr import (  # lazy import: offline tests don't touch ib_async
        IBKRConnection,
        IBKRPriceHistoryProvider,
        MarketDataUnavailableError,
        fetch_put_chain,
        fetch_spot,
    )
    from tailhedge.sizing import FX_CAVEAT, InsufficientDataError, resolve_freq_window, run_sizing

    def _fetch(ib, inst, expiry):
        """(spot, chain) for one instrument at expiry `expiry`.

        --trading-class overrides the tradingClass for the SPX leg only (see docs/strategy.md);
        SPY always uses its own (inst.trading_class == "SPY").
        """
        tc = a.trading_class if inst.symbol == "SPX" else inst.trading_class
        return (fetch_spot(ib, inst.symbol),
                fetch_put_chain(ib, inst.symbol, expiry, a.band, tc))

    def _spy_expiry(ib):
        """SPY expiry nearest to the selected SPX one (monthlies offset by ~1 day)."""
        from tailhedge.ibkr import available_expiries, list_option_params, nearest_expiry
        exps = available_expiries(list_option_params(ib, "SPY"), "SPY")
        return nearest_expiry(select_expiry, exps)

    sizing_note = ""
    today = date.today()
    try:
        with IBKRConnection() as ib:
            if a.portfolio:
                freq, window = resolve_freq_window(a.returns_freq, a.window, bool(listings))
                rep = run_sizing(IBKRPriceHistoryProvider(ib, listings=listings),
                                 positions=positions, spx_ticker="SPX",
                                 windows=[window], lookback_days=a.lookback, freq=freq)
                coverage_notional = rep.notional_by_window[window]
                n_obs = int(rep.sensitivity["n_obs"].iloc[0])
                sizing_note = (
                    f"Sizing: positions declared ${rep.nav:,.0f}, total NAV ${budget_nav:,.0f}, "
                    f"window {window} ({freq}, n={n_obs}) → coverage ${coverage_notional:,.0f} "
                    f"(R² {float(rep.sensitivity['r_squared'].iloc[0]):.2f})"
                )
                if listings:
                    sizing_note += "\n" + FX_CAVEAT
            else:
                coverage_notional = a.notional
                budget_nav = a.notional

            nav_for_budget = coverage_notional if budget_nav is None else budget_nav

            def _expiry_note(rep):
                """Expiry remap note for when SPY is offset from the selected SPX one."""
                return (f" SPY expiry {rep} (nearest to the selected SPX {select_expiry})."
                        if rep != select_expiry else "")

            report_expiry = select_expiry
            if a.force_underlying:
                instrument, selection_note = resolve_instrument(a.force_underlying, None)
                if instrument is SPY:
                    report_expiry = _spy_expiry(ib)
                    selection_note += _expiry_note(report_expiry)
                spot, chain = _fetch(ib, instrument, report_expiry)
            else:
                # Pre-check on SPX: SPY is fetched only if SPX can't buy 1 contract (see docs/strategy.md).
                spot, chain = _fetch(ib, SPX, select_expiry)
                budget = premium_budget(a.budget_pct, nav_for_budget,
                                        dte_days(select_expiry, today))
                probe = ticket_for(chain, spot, today, select_expiry, a.target_delta,
                                   budget, coverage_notional, nav_total=nav_for_budget,
                                   symbol="SPX")
                instrument, selection_note = resolve_instrument(None, probe)
                if instrument is SPY:
                    report_expiry = _spy_expiry(ib)
                    spot, chain = _fetch(ib, SPY, report_expiry)
                    selection_note += _expiry_note(report_expiry)
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
    except ValueError as e:   # AFTER the MDUE/InsufficientDataError subclasses above:
        # e.g. the SPX probe on an all-NaN-greeks chain (market closed) → clean exit.
        print(f"Data error: {e}", file=sys.stderr)
        return 1

    try:
        report = run_advisor(chain, spot, coverage_notional, today, report_expiry,
                             a.target_delta, a.budget_pct, a.r, budget_nav=budget_nav,
                             budget_pcts=budget_pcts, instrument=instrument,
                             selection_note=selection_note)
    except ValueError as e:
        print(f"Data error: {e}", file=sys.stderr)
        return 1
    if sizing_note:
        report = sizing_note + "\n\n" + report
    print(report)

    out = Path(a.out_dir) if a.out_dir else default_runs_dir()
    out.mkdir(parents=True, exist_ok=True)
    stem = (f"tail-hedge-advisor-{today.strftime('%Y%m%d')}"
            f"-{report_expiry}-d{abs(int(round(a.target_delta * 100)))}-{instrument.symbol}")
    (out / f"{stem}.txt").write_text(report + "\n")
    print(f"\nSaved to {out}/{stem}.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
