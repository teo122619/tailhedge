from datetime import date

import numpy as np
import pandas as pd
import pytest

from tailhedge.pricing import bs_put_price
from tailhedge import advisor_cli
from tailhedge.advisor import SPY

SPOT = 5000.0


def _chain_two_expiries():
    """20261218: 31 liquid strikes at flat IV (B-L ok) + 20270618: 3 strikes (comparison)."""
    rows = []
    for k in np.arange(3500, 5001, 50):
        m = bs_put_price(SPOT, float(k), 0.43, 0.20)
        rows.append({
            "expiry": "20261218", "strike": float(k), "right": "P", "iv": 0.20,
            "delta": -min(0.5, max(0.02, (k / SPOT) ** 8)),  # fake delta increasing with K
            "vega": 8.0, "gamma": 3e-4, "mid": m,
            "bid": m * 0.98, "ask": m * 1.02, "moneyness": k / SPOT,
        })
    for k, d in ((3800.0, -0.05), (4100.0, -0.10), (4400.0, -0.15)):
        m = bs_put_price(SPOT, k, 0.92, 0.22)
        rows.append({
            "expiry": "20270618", "strike": k, "right": "P", "iv": 0.22,
            "delta": d, "vega": 14.0, "gamma": 2e-4, "mid": m,
            "bid": m * 0.98, "ask": m * 1.02, "moneyness": k / SPOT,
        })
    return pd.DataFrame(rows)


def test_run_advisor_full_report():
    out = advisor_cli.run_advisor(
        _chain_two_expiries(), spot=SPOT, notional=5_000_000.0, today=date(2026, 7, 14),
        select_expiry="20261218", target_delta=-0.10, annual_budget_pct=0.01,
    )
    assert "Tail-hedge Advisor" in out
    assert "Comparison table" in out
    assert "20261218" in out and "20270618" in out   # both expiries in the table
    assert "ORDER TICKET" in out
    assert "Breeden–Litzenberger" in out             # liquid chain -> B-L section present


def test_run_advisor_skips_bl_on_illiquid_expiry():
    out = advisor_cli.run_advisor(
        _chain_two_expiries(), spot=SPOT, notional=5_000_000.0, today=date(2026, 7, 14),
        select_expiry="20270618", target_delta=-0.10, annual_budget_pct=0.01,
    )
    assert "ORDER TICKET" in out
    assert "B-L section skipped" in out              # 3 strikes: degrades with a message, not a crash


def test_run_advisor_reports_selected_instrument_and_labels():
    out = advisor_cli.run_advisor(
        _chain_two_expiries(), spot=SPOT, notional=600_000.0, today=date(2026, 7, 14),
        select_expiry="20261218", target_delta=-0.10, annual_budget_pct=0.01,
        instrument=SPY, selection_note="SPX skipped: budget X < premium of 1 SPX contract.",
    )
    assert "Instrument: SPY (American, pays dividends)." in out
    assert "SPX skipped" in out
    assert "× SPY 20261218" in out           # ticket labeled SPY
    assert "P(SPY <" in out                  # B-L labeled SPY
    assert "American option" in out          # caveat B-L adapted


def test_run_advisor_default_spx_report_unchanged():
    """Backward compat: without instrument/selection_note the SPX report is unchanged from before."""
    out = advisor_cli.run_advisor(
        _chain_two_expiries(), spot=SPOT, notional=5_000_000.0, today=date(2026, 7, 14),
        select_expiry="20261218", target_delta=-0.10, annual_budget_pct=0.01,
    )
    assert "Instrument: SPX (cash-settled, European)." in out
    assert "× SPX 20261218" in out
    assert "Breeden–Litzenberger" in out
    assert "SPY" not in out                  # no leftover from the other instrument


def test_main_requires_portfolio_or_notional():
    with pytest.raises(SystemExit):
        advisor_cli.main(["--expiries", "20261218"])  # missing --notional/--portfolio: argparse exits


def test_empty_expiries_exits_cleanly():
    # "--expiries ' , '" strips to an empty list: must be a clean argparse exit,
    # not an IndexError on expiries[-1].
    with pytest.raises(SystemExit) as exc:
        advisor_cli.main(["--expiries", " , ", "--notional", "500000"])
    assert exc.value.code == 2


def test_run_advisor_empty_greeks_raises_clean_error():
    chain = _chain_two_expiries()
    chain["delta"] = float("nan")  # frozen data: modelGreeks absent across the whole chain
    with pytest.raises(ValueError, match="greeks unavailable"):
        advisor_cli.run_advisor(
            chain, spot=SPOT, notional=5_000_000.0, today=date(2026, 7, 14),
            select_expiry="20261218", target_delta=-0.10, annual_budget_pct=0.01,
        )


def test_run_advisor_budget_on_nav_total_not_coverage():
    # equity coverage 600k, total NAV 1M: the budget must be computed on the million
    out = advisor_cli.run_advisor(
        _chain_two_expiries(), spot=SPOT, notional=600_000.0, today=date(2026, 7, 14),
        select_expiry="20261218", target_delta=-0.10, annual_budget_pct=0.01,
        budget_nav=1_000_000.0,
    )
    assert "equity coverage $600,000" in out
    assert "total NAV $1,000,000" in out
    # budget = 1% * 1,000,000 * 157/365 = 4,301 (would be 2,581 on 600k coverage)
    assert "4,301" in out


def test_run_advisor_omits_budget_comparison_by_default():
    """Opt-in: without --budget-pcts the report stays identical to before (zero regressions)."""
    out = advisor_cli.run_advisor(
        _chain_two_expiries(), spot=SPOT, notional=5_000_000.0, today=date(2026, 7, 14),
        select_expiry="20261218", target_delta=-0.10, annual_budget_pct=0.01,
    )
    assert "BUDGET COMPARISON" not in out


def test_run_advisor_includes_budget_comparison_when_asked():
    out = advisor_cli.run_advisor(
        _chain_two_expiries(), spot=SPOT, notional=5_000_000.0, today=date(2026, 7, 14),
        select_expiry="20261218", target_delta=-0.10, annual_budget_pct=0.01,
        budget_pcts=[0.005, 0.0075, 0.01],
    )
    assert "BUDGET COMPARISON" in out
    assert "0.50%" in out and "0.75%" in out
    # premium ~$282/contract on a budget in the thousands: negligible-granularity regime
    assert "almost linearly" in out
    # the detailed ticket and B-L stay on the single --budget-pct
    assert "ORDER TICKET" in out and "Breeden–Litzenberger" in out


def test_parse_budget_pcts_accepts_comma_list():
    assert advisor_cli.parse_budget_pcts("0.005,0.0075,0.01") == [0.005, 0.0075, 0.01]
    assert advisor_cli.parse_budget_pcts(" 0.005 , 0.01 ") == [0.005, 0.01]
    assert advisor_cli.parse_budget_pcts(None) is None
    with pytest.raises(ValueError, match="0.5"):
        advisor_cli.parse_budget_pcts("0.5,0.01")   # 50%/yr: almost certainly a unit error


def test_main_scaffolds_missing_portfolio(tmp_path):
    path = tmp_path / "port.xlsx"
    rc = advisor_cli.main(["--expiries", "20261218", "--portfolio", str(path)])
    assert rc == 0
    assert path.exists()                       # template created without touching IBKR
    from tailhedge.portfolio import load_portfolio
    positions, nav, _ = load_portfolio(str(path))
    assert positions and nav > 0


def test_resolve_instrument_force_wins():
    from tailhedge.advisor import SPY
    inst, note = advisor_cli.resolve_instrument("SPY", None)
    assert inst is SPY and note == ""


def test_resolve_instrument_auto_falls_back_to_spy():
    from tailhedge.advisor import SPX, SPY, build_ticket
    row = pd.Series({"expiry": "20270114", "strike": 6100.0, "mid": 71.05,
                     "delta": -0.10, "vega": 9.4, "dte": 184})
    probe0 = build_ticket(row, budget=5000.0, notional=1_000_000.0, spot=7500.0,
                          symbol="SPX")  # 0 contracts
    probe2 = build_ticket(row, budget=20000.0, notional=1_000_000.0, spot=7500.0,
                          symbol="SPX")  # 2 contracts
    assert probe0.contracts == 0
    assert probe2.contracts == 2

    inst, note = advisor_cli.resolve_instrument(None, probe0)
    assert inst is SPY
    assert "SPX skipped" in note
    assert f"{probe0.mid * probe0.multiplier:,.0f}" in note

    inst2, note2 = advisor_cli.resolve_instrument(None, probe2)
    assert inst2 is SPX and note2 == ""


def test_force_underlying_is_parsed_and_validated():
    # allowed value
    p = advisor_cli._build_parser()
    a = p.parse_args(["--expiries", "20270114", "--notional", "2000000",
                      "--force-underlying", "SPY"])
    assert a.force_underlying == "SPY"


def test_auto_select_market_closed_chain_is_clean(monkeypatch, capsys):
    """Auto-select SPX pre-check on an all-NaN-greeks chain (market closed):
    the probe raises a plain ValueError → the CLI must exit cleanly, not traceback."""
    import tailhedge.ibkr as ibkr

    class FakeConn:
        def __enter__(self):
            return object()
        def __exit__(self, *a):
            return False

    def fake_chain(ib, symbol, expiry, band, trading_class):
        return pd.DataFrame([{
            "expiry": "20261218", "strike": 4400.0, "right": "P", "iv": float("nan"),
            "delta": float("nan"), "vega": float("nan"), "gamma": float("nan"),
            "mid": 100.0, "bid": 98.0, "ask": 102.0, "moneyness": 0.88,
        }])

    monkeypatch.setattr(ibkr, "IBKRConnection", FakeConn)
    monkeypatch.setattr(ibkr, "fetch_spot", lambda ib, symbol="SPX": 5000.0)
    monkeypatch.setattr(ibkr, "fetch_put_chain", fake_chain)

    rc = advisor_cli.main(["--expiries", "20261218", "--notional", "5000000"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Data error" in err
    assert "Traceback" not in err


def test_connection_error_is_clean(monkeypatch, capsys):
    class BoomConnection:
        def __enter__(self):
            raise ConnectionRefusedError("refused")
        def __exit__(self, *a):
            return False

    monkeypatch.setattr("tailhedge.ibkr.IBKRConnection", BoomConnection)
    rc = advisor_cli.main(["--expiries", "20261218", "--notional", "500000"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Cannot connect to TWS/IB Gateway" in err
    assert "Traceback" not in err
