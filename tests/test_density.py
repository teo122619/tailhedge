import math
from datetime import date

import numpy as np
import pandas as pd
import pytest
import scipy.integrate

from tailhedge.pricing import bs_put_price, norm_cdf
from tailhedge.density import filter_liquid, fit_smile, price_grid, cdf_from_grid, density_from_grid, prob_below, tail_cost, density_report

SPOT, SIGMA, T = 5000.0, 0.20, 0.5


def _flat_chain(strikes, sigma=SIGMA, spread=0.04, expiry="20270115"):
    """Synthetic flat-IV chain: mid = exact BS price -> known lognormal density."""
    rows = []
    for k in strikes:
        m = bs_put_price(SPOT, float(k), T, sigma)
        rows.append({
            "expiry": expiry, "strike": float(k), "right": "P", "iv": sigma,
            "delta": -0.1, "vega": 1.0, "gamma": 1e-4, "mid": m,
            "bid": m * (1 - spread / 2), "ask": m * (1 + spread / 2), "moneyness": k / SPOT,
        })
    return pd.DataFrame(rows)


def test_filter_liquid_drops_dead_and_wide_quotes():
    chain = _flat_chain([4500, 4600])
    chain.loc[0, "bid"] = -1.0                      # IBKR sentinel: dead quote
    wide = _flat_chain([4700])
    wide.loc[0, "ask"] = wide.loc[0, "mid"] * 2.0   # huge spread
    out = filter_liquid(pd.concat([chain, wide], ignore_index=True), max_spread_pct=0.25)
    assert list(out["strike"]) == [4600.0]


def test_filter_liquid_drops_crossed_quotes():
    # crossed quote (bid > ask, negative spread) sneaks past the spread ceiling;
    # a B-L smile fit on it is garbage-in, so it must be dropped.
    chain = _flat_chain([4500, 4600])
    chain.loc[0, "bid"] = chain.loc[0, "ask"] * 1.5   # bid > ask
    out = filter_liquid(chain)
    assert list(out["strike"]) == [4600.0]


def test_filter_liquid_drops_nonfinite_iv():
    # IBKR greeks arrive asynchronously: valid bid/ask but IV not yet computed (nan) ->
    # the row must NOT reach the spline (PCHIP rejects non-finite y)
    chain = _flat_chain([4500, 4600])
    chain.loc[0, "iv"] = float("nan")
    out = filter_liquid(chain)
    assert list(out["strike"]) == [4600.0]


def test_fit_smile_flat_is_flat_and_does_not_extrapolate():
    sm = fit_smile([4000.0, 4500.0, 5000.0], [SIGMA, SIGMA, SIGMA])
    assert float(sm(4250.0)) == pytest.approx(SIGMA)
    assert np.isnan(float(sm(3000.0)))  # outside observed range: no extrapolation


def test_price_grid_reproduces_bs_on_flat_smile():
    liquid = _flat_chain(np.arange(3500, 5001, 50))
    grid = price_grid(liquid, SPOT, T)
    assert grid["K"].min() == 3500.0 and grid["K"].max() == 5000.0
    # on the flat smile, the smooth re-pricing matches BS at the grid center
    i = (grid["K"] - 4500.0).abs().idxmin()
    expected = bs_put_price(SPOT, float(grid.loc[i, "K"]), T, SIGMA)
    assert grid.loc[i, "price"] == pytest.approx(expected, rel=1e-6)


def _d2(K):
    return (math.log(SPOT / K) - 0.5 * SIGMA**2 * T) / (SIGMA * math.sqrt(T))


def test_cdf_matches_lognormal_closed_form():
    liquid = _flat_chain(np.arange(3500, 5001, 50))
    cdf = cdf_from_grid(price_grid(liquid, SPOT, T), T)
    # closed form: P(S_T <= K) = Phi(-d2). At K=4500: ~0.250
    for K in (4000.0, 4500.0, 4900.0):
        assert prob_below(cdf, K) == pytest.approx(norm_cdf(-_d2(K)), abs=5e-3)


def test_prob_below_outside_range_is_nan():
    liquid = _flat_chain(np.arange(3500, 5001, 50))
    cdf = cdf_from_grid(price_grid(liquid, SPOT, T), T)
    assert math.isnan(prob_below(cdf, 3400.0))  # below min strike: no extrapolation


def test_density_nonnegative_and_integrates_to_cdf_mass():
    liquid = _flat_chain(np.arange(3500, 5001, 50))
    grid = price_grid(liquid, SPOT, T)
    dens = density_from_grid(grid, T)
    cdf = cdf_from_grid(grid, T)
    assert (dens["density"] >= 0).all()
    mass = scipy.integrate.trapezoid(dens["density"], dens["K"])
    expected = prob_below(cdf, 5000.0) - prob_below(cdf, 3500.0)
    assert mass == pytest.approx(expected, abs=0.01)


def test_tail_cost_matches_closed_form():
    liquid = _flat_chain(np.arange(3500, 5001, 50))
    cdf = cdf_from_grid(price_grid(liquid, SPOT, T), T)
    tc = tail_cost(liquid, cdf)
    row = tc.loc[tc["strike"] == 4500.0].iloc[0]
    expected = bs_put_price(SPOT, 4500.0, T, SIGMA) / norm_cdf(-_d2(4500.0))
    assert row["cost_per_prob"] == pytest.approx(expected, rel=0.03)


def test_density_report_contents():
    chain = _flat_chain(np.arange(3500, 5001, 50), expiry="20270115")
    out = density_report(chain, spot=SPOT, expiry="20270115", today=date(2026, 7, 14))
    assert "Breeden–Litzenberger" in out
    assert "P(SPX <" in out
    assert "-10%" in out and "-30%" in out
    assert "risk-neutral" in out.lower()          # caveat: prices are market-implied, not real-world
    assert "cheap" in out.lower()                 # cheapest zone of the smile
    assert "extrapolation" in out                 # caveat: wings
    assert "q=0" in out  # caveat: dividends ignored in v1


def test_density_report_raises_on_illiquid_chain():
    chain = _flat_chain([4500, 4600, 4700], expiry="20270115")  # 3 < MIN_LIQUID_STRIKES
    with pytest.raises(ValueError, match="liquid"):
        density_report(chain, spot=SPOT, expiry="20270115", today=date(2026, 7, 14))


def test_density_report_caveat_adapts_to_american_dividend_instrument():
    chain = _flat_chain(np.arange(3500, 5001, 50), expiry="20270115")
    out = density_report(chain, spot=SPOT, expiry="20270115", today=date(2026, 7, 14),
                         symbol="SPY", style="american", pays_dividends=True)
    assert "P(SPY <" in out
    assert "American option" in out
    assert "SPY dividends not modeled" in out   # should not read as "dividends modeled"


def test_density_report_caveat_default_is_spx_european():
    chain = _flat_chain(np.arange(3500, 5001, 50), expiry="20270115")
    out = density_report(chain, spot=SPOT, expiry="20270115", today=date(2026, 7, 14))
    assert "P(SPX <" in out
    assert "SPX dividends ignored" in out
    assert "American option" not in out
