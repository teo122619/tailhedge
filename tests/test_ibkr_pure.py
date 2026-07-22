import math
from types import SimpleNamespace

import pandas as pd
import pytest

from tailhedge.config import IBKRConfig
from tailhedge.ibkr import (
    resolve_contract,
    duration_str,
    ContractSpec,
    bars_to_series,
    organize_put_chain,
    mid,
    nearest_expiry,
    MarketDataUnavailableError,
)


def test_config_defaults():
    cfg = IBKRConfig()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 7497
    assert cfg.client_id == 11


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("TAILHEDGE_IB_PORT", "7496")
    monkeypatch.setenv("TAILHEDGE_IB_CLIENT_ID", "5")
    cfg = IBKRConfig.from_env()
    assert cfg.port == 7496
    assert cfg.client_id == 5
    assert cfg.host == "127.0.0.1"


def test_config_market_data_type(monkeypatch):
    assert IBKRConfig().market_data_type == 4  # delayed-frozen by default
    monkeypatch.setenv("TAILHEDGE_IB_MKT_DATA_TYPE", "1")
    assert IBKRConfig.from_env().market_data_type == 1


def test_resolve_contract_index_vs_stock():
    assert resolve_contract("SPX") == ContractSpec("IND", "SPX", "CBOE", "USD")
    assert resolve_contract("vix") == ContractSpec("IND", "VIX", "CBOE", "USD")
    assert resolve_contract("AAPL") == ContractSpec("STK", "AAPL", "SMART", "USD")


def test_resolve_contract_with_declared_listing():
    spec = resolve_contract("SXR8", exchange="IBIS", currency="eur")
    assert (spec.sec_type, spec.symbol, spec.exchange, spec.currency) == ("STK", "SXR8", "IBIS", "EUR")


def test_resolve_contract_currency_without_exchange_uses_smart():
    spec = resolve_contract("VUSA", currency="EUR")
    assert (spec.exchange, spec.currency) == ("SMART", "EUR")


def test_resolve_contract_without_listing_unchanged():
    assert resolve_contract("AAPL") == ContractSpec("STK", "AAPL", "SMART", "USD")
    assert resolve_contract("SPX").sec_type == "IND"


@pytest.mark.parametrize(
    "days,expected", [(250, "250 D"), (365, "365 D"), (366, "2 Y"), (756, "3 Y")]
)
def test_duration_str(days, expected):
    assert duration_str(days) == expected


def test_bars_to_series_parses_and_sorts():
    bars = [
        SimpleNamespace(date="2020-01-03", close=102.0),
        SimpleNamespace(date="2020-01-01", close=100.0),
        SimpleNamespace(date="2020-01-02", close=101.0),
    ]
    s = bars_to_series(bars)
    assert list(s.index) == list(pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-03"]))
    assert list(s.to_numpy()) == [100.0, 101.0, 102.0]
    assert s.name == "close"


def _tk(right, strike, expiry, iv, delta, bid, ask, vega=None, gamma=None):
    contract = SimpleNamespace(right=right, strike=strike, lastTradeDateOrContractMonth=expiry)
    greeks = None if iv is None else SimpleNamespace(impliedVol=iv, delta=delta, vega=vega, gamma=gamma)
    return SimpleNamespace(contract=contract, modelGreeks=greeks, bid=bid, ask=ask)


def test_mid_discards_sentinels():
    assert mid(1.0, 3.0) == 2.0
    assert mid(-1.0, 3.0) == 3.0  # IBKR sentinel -1 discarded
    assert math.isnan(mid(-1.0, None))


def test_organize_put_chain_filters_puts_and_sorts():
    tickers = [
        _tk("C", 7000, "20260918", 0.15, 0.40, 10.0, 12.0),  # call: discarded
        _tk("P", 6500, "20260918", 0.22, -0.20, 30.0, 32.0),
        _tk("P", 6000, "20260918", 0.28, -0.10, 15.0, 17.0),
    ]
    df = organize_put_chain(tickers, spot=7000.0)
    assert list(df.columns) == [
        "expiry", "strike", "right", "iv", "delta", "vega", "gamma",
        "mid", "bid", "ask", "moneyness",
    ]
    assert list(df["strike"]) == [6000.0, 6500.0]  # puts only, sorted
    assert df.loc[df["strike"] == 6500.0, "mid"].iloc[0] == 31.0
    assert abs(df.loc[df["strike"] == 6000.0, "moneyness"].iloc[0] - 6000.0 / 7000.0) < 1e-12


def test_organize_put_chain_includes_real_greeks_and_quotes():
    tickers = [_tk("P", 6000, "20260918", 0.28, -0.10, 15.0, 17.0, vega=8.5, gamma=0.0004)]
    df = organize_put_chain(tickers, spot=7000.0)
    row = df.iloc[0]
    assert row["vega"] == 8.5
    assert row["gamma"] == 0.0004
    assert row["bid"] == 15.0
    assert row["ask"] == 17.0


def test_organize_put_chain_missing_greeks_are_nan():
    # modelGreeks None (market closed / IV not computed): vega/gamma nan, no crash
    tickers = [_tk("P", 6000, "20260918", None, None, 15.0, 17.0)]
    df = organize_put_chain(tickers, spot=7000.0)
    assert math.isnan(df.iloc[0]["vega"])
    assert math.isnan(df.iloc[0]["gamma"])


def test_nearest_expiry_exact_match_returns_itself():
    assert nearest_expiry("20270115", ["20270115", "20270219"]) == "20270115"


def test_nearest_expiry_spx_to_spy_one_day_off():
    assert nearest_expiry("20270114", ["20261218", "20270115", "20270319"]) == "20270115"


def test_nearest_expiry_spx_to_spy_another_month():
    assert nearest_expiry("20261217", ["20261218", "20270115"]) == "20261218"


def test_nearest_expiry_tie_break_prefers_forward():
    # 20270113 and 20270115 are both 1 day from 20270114: the forward one wins
    assert nearest_expiry("20270114", ["20270113", "20270115"]) == "20270115"


def test_nearest_expiry_empty_available_raises():
    with pytest.raises(MarketDataUnavailableError):
        nearest_expiry("20270114", [])


def test_expiries_in_dte_range():
    from datetime import date
    from tailhedge.ibkr import expiries_in_dte_range

    today = date(2026, 7, 21)
    exps = ["20260821", "20261016", "20261120", "20270115", "20270730"]
    # DTE: 31, 87, 122, 178, 374 → within [90, 180] only 20261120 and 20270115 remain
    assert expiries_in_dte_range(exps, today) == ["20261120", "20270115"]
    assert expiries_in_dte_range(exps, today, lo=30, hi=90) == ["20260821", "20261016"]


def test_puts_in_strike_range():
    from tailhedge.ibkr import puts_in_strike_range

    def _cd(strike, right="P"):
        return SimpleNamespace(contract=SimpleNamespace(right=right, strike=strike))

    details = [_cd(4000.0), _cd(4500.0), _cd(4875.0), _cd(5000.0), _cd(4500.0, "C")]
    picked = puts_in_strike_range(details, 4125.0, 4875.0)
    assert [c.strike for c in picked] == [4500.0, 4875.0]   # no calls, sorted
