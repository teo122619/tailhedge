import numpy as np
import pandas as pd
import pytest
from tailhedge.sizing import (
    build_portfolio_returns,
    estimate_beta,
    BetaResult,
    beta_window_sensitivity,
    weights_from_positions,
    hedge_notional,
    run_sizing,
    SizingReport,
    InsufficientDataError,
)
from tailhedge.data import FakePriceHistoryProvider


def test_build_portfolio_returns_weighted_simple_returns():
    idx = pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-03"])
    prices = pd.DataFrame(
        {"A": [100.0, 110.0, 121.0], "B": [100.0, 100.0, 100.0]},
        index=idx,
    )
    # A: +10%, +10%; B: 0%, 0%. Weights 50/50 -> +5%, +5%
    out = build_portfolio_returns(prices, {"A": 0.5, "B": 0.5})
    assert list(out.index) == list(idx[1:])
    np.testing.assert_allclose(out.to_numpy(), [0.05, 0.05], rtol=1e-9)


def test_build_portfolio_returns_normalizes_weights():
    idx = pd.to_datetime(["2020-01-01", "2020-01-02"])
    prices = pd.DataFrame({"A": [100.0, 110.0], "B": [100.0, 100.0]}, index=idx)
    # Unnormalized weights 1 and 1 -> normalized to 0.5/0.5 -> +5%
    out = build_portfolio_returns(prices, {"A": 1.0, "B": 1.0})
    np.testing.assert_allclose(out.to_numpy(), [0.05], rtol=1e-9)


def test_estimate_beta_recovers_known_slope():
    rng = np.random.default_rng(0)
    m = pd.Series(rng.normal(0, 0.01, 500))
    p = 1.5 * m  # exact relationship, no noise
    res = estimate_beta(p, m)
    assert isinstance(res, BetaResult)
    assert abs(res.beta - 1.5) < 1e-9
    assert abs(res.r_squared - 1.0) < 1e-9
    assert res.n_obs == 500


def test_estimate_beta_aligns_on_common_dates():
    idx = pd.date_range("2020-01-01", periods=5)
    m = pd.Series([0.01, -0.02, 0.03, 0.00, 0.01], index=idx)
    p = pd.Series([0.02, -0.04, 0.06], index=idx[:3])  # shorter
    res = estimate_beta(p, m)
    assert res.n_obs == 3
    assert abs(res.beta - 2.0) < 1e-9


def test_window_sensitivity_detects_regime_change():
    rng = np.random.default_rng(1)
    m = pd.Series(rng.normal(0, 0.01, 400))
    # beta = 0.8 on the first 300, beta = 2.0 on the last 100
    p = pd.concat([0.8 * m.iloc[:300], 2.0 * m.iloc[300:]])
    tab = beta_window_sensitivity(p, m, windows=[100, 400])
    assert list(tab.columns) == ["window", "beta", "r_squared", "n_obs"]
    beta_100 = tab.loc[tab["window"] == 100, "beta"].iloc[0]
    beta_400 = tab.loc[tab["window"] == 400, "beta"].iloc[0]
    assert abs(beta_100 - 2.0) < 1e-6      # short window = recent regime
    assert beta_400 < beta_100             # long window = averaged beta is lower


def test_weights_from_positions_normalizes():
    w = weights_from_positions({"A": 30_000.0, "B": 10_000.0})
    assert abs(w["A"] - 0.75) < 1e-12
    assert abs(w["B"] - 0.25) < 1e-12
    assert abs(sum(w.values()) - 1.0) < 1e-12


def test_hedge_notional():
    assert hedge_notional(0.8, 500_000.0) == 400_000.0


def test_run_sizing_end_to_end():
    idx = pd.date_range("2020-01-01", periods=260)
    rng = np.random.default_rng(2)
    spx_ret = rng.normal(0, 0.01, 259)
    spx_px = pd.Series(100 * np.cumprod(1 + np.r_[0, spx_ret]), index=idx)
    a_px = pd.Series(100 * np.cumprod(1 + np.r_[0, 1.2 * spx_ret]), index=idx)  # beta ~1.2
    prov = FakePriceHistoryProvider({"SPX": spx_px, "A": a_px})
    rep = run_sizing(
        prov, positions={"A": 500_000.0}, spx_ticker="SPX",
        windows=[120, 250], lookback_days=260,
    )
    assert isinstance(rep, SizingReport)
    assert rep.nav == 500_000.0
    beta_250 = rep.sensitivity.loc[rep.sensitivity["window"] == 250, "beta"].iloc[0]
    assert abs(beta_250 - 1.2) < 0.05
    # notional = beta * nav
    assert abs(rep.notional_by_window[250] - beta_250 * 500_000.0) < 1e-6


def test_estimate_beta_raises_on_insufficient_data():
    empty = pd.Series([], dtype=float)
    with pytest.raises(InsufficientDataError):
        estimate_beta(empty, empty)


def test_run_sizing_raises_clear_error_on_empty_history():
    # Reproduces the Error 162 case: reqHistoricalData returns empty for a ticker.
    idx = pd.date_range("2020-01-01", periods=300)
    spx_px = pd.Series(np.arange(300, dtype=float), index=idx)
    empty = pd.Series([], dtype=float)
    prov = FakePriceHistoryProvider({"SPX": spx_px, "A": empty})
    with pytest.raises(InsufficientDataError, match="A"):
        run_sizing(prov, positions={"A": 100.0}, spx_ticker="SPX", windows=[250], lookback_days=300)


def test_resolve_freq_window_policy():
    from tailhedge.sizing import resolve_freq_window
    assert resolve_freq_window("auto", None, has_listings=True) == ("weekly", 52)
    assert resolve_freq_window("auto", None, has_listings=False) == ("daily", 250)
    assert resolve_freq_window("weekly", 104, has_listings=False) == ("weekly", 104)
    assert resolve_freq_window("daily", None, has_listings=True) == ("daily", 250)


def test_run_sizing_rejects_unknown_freq():
    idx = pd.bdate_range("2024-01-02", periods=10)
    prov = FakePriceHistoryProvider({
        "AAA": pd.Series(100.0, index=idx), "SPX": pd.Series(100.0, index=idx)})
    with pytest.raises(ValueError, match="freq"):
        run_sizing(prov, {"AAA": 1.0}, "SPX", [5], 10, freq="monthly")


def test_run_sizing_raises_on_asymmetric_short_history():
    # One young ETF with far less history than the rest of the panel: a plain
    # joint dropna would silently truncate 200 business days of other data down
    # to the 30-day overlap. That must raise, naming the offending ticker.
    idx_long = pd.bdate_range("2023-01-02", periods=200)
    idx_short = idx_long[-30:]
    rng = np.random.default_rng(3)
    spx_px = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, 200)), index=idx_long)
    young_px = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, 30)), index=idx_short)
    prov = FakePriceHistoryProvider({"SPX": spx_px, "YOUNG": young_px})
    with pytest.raises(InsufficientDataError, match="YOUNG"):
        run_sizing(prov, positions={"YOUNG": 1_000.0}, spx_ticker="SPX",
                   windows=[10], lookback_days=200)


def test_run_sizing_equal_length_panel_does_not_raise():
    # Guard against a false positive: equal-length histories (e.g. all series
    # short but symmetric) must NOT trigger the asymmetric-history guard.
    idx = pd.bdate_range("2024-01-02", periods=30)
    rng = np.random.default_rng(4)
    spx_px = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, 30)), index=idx)
    a_px = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, 30)), index=idx)
    prov = FakePriceHistoryProvider({"SPX": spx_px, "A": a_px})
    rep = run_sizing(prov, positions={"A": 1_000.0}, spx_ticker="SPX",
                      windows=[10], lookback_days=30)
    assert isinstance(rep, SizingReport)


def test_weekly_freq_recovers_beta_hidden_by_stale_closes():
    # EU-style series: identical exposure to SPX but closes one day late.
    # Daily regression sees ~0 beta (iid returns, lag 1); weekly overlaps 4/5 days.
    rng = np.random.default_rng(0)
    n = 756
    idx = pd.bdate_range("2022-01-03", periods=n)
    spx_ret = rng.normal(0.0, 0.01, size=n)
    eu_ret = np.concatenate([[0.0], spx_ret[:-1]])  # one-day stale echo of SPX
    spx = pd.Series(100 * np.cumprod(1 + spx_ret), index=idx)
    eu = pd.Series(100 * np.cumprod(1 + eu_ret), index=idx)
    prov = FakePriceHistoryProvider({"EU": eu, "SPX": spx})

    daily = run_sizing(prov, {"EU": 1_000.0}, "SPX", [250], n, freq="daily")
    weekly = run_sizing(prov, {"EU": 1_000.0}, "SPX", [52], n, freq="weekly")

    beta_daily = float(daily.sensitivity["beta"].iloc[0])
    beta_weekly = float(weekly.sensitivity["beta"].iloc[0])
    assert abs(beta_daily) < 0.3
    assert beta_weekly > 0.55
    assert int(weekly.sensitivity["n_obs"].iloc[0]) <= 52
