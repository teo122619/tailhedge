"""Risk-neutral density diagnostics via Breeden-Litzenberger, the second
quantitative helper module (with `pricing.py`) in the pipeline.

From a liquid put chain, fits a smooth price curve (via `pricing.bs_put_price`
over an interpolated smile) and differentiates it to get the risk-neutral CDF
and density, then reports P(S_T <= level) at a few crash levels plus the
cheapest zone of the smile per unit of tail probability. Consumed by
`advisor.py`/`advisor_cli.py` and `hedge_cli.py` as a diagnostics-only
section, never to drive selection or sizing.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from tailhedge.pricing import bs_put_price
from tailhedge.advisor import dte_days

# Below this many liquid strikes the B-L density is not reliable.
MIN_LIQUID_STRIKES = 8


def filter_liquid(chain: pd.DataFrame, max_spread_pct: float = 0.25) -> pd.DataFrame:
    """Only strikes with real bid/ask, finite IV, and a contained relative spread (no dead wings).

    bid>0 also discards IBKR sentinel values (-1). Finite IV: IBKR greeks arrive
    asynchronously, and a row with valid quotes but nan IV would break the spline.
    v1: filters on spread only; open interest isn't in the reqTickers snapshot
    (possible future refinement).
    """
    df = chain[
        (chain["bid"] > 0) & (chain["ask"] > 0) & (chain["mid"] > 0)
        & (chain["bid"] <= chain["ask"])   # reject crossed quotes: bad input for the smile fit
        & np.isfinite(chain["iv"])
    ].copy()
    df = df[(df["ask"] - df["bid"]) / df["mid"] <= max_spread_pct]
    return df.sort_values("strike").reset_index(drop=True)


def fit_smile(strikes, ivs):
    """Monotone interpolator (PCHIP) K -> IV over the observed (K, IV) points.

    extrapolate=False: outside the observed range it returns nan — never extrapolate the wings.
    """
    from scipy.interpolate import PchipInterpolator  # lazy import (only use of scipy)

    return PchipInterpolator(np.asarray(strikes, float), np.asarray(ivs, float), extrapolate=False)


def price_grid(
    liquid: pd.DataFrame, spot: float, t_years: float, r: float = 0.0, n_grid: int = 400
) -> pd.DataFrame:
    """Fine grid of SMOOTH put prices inside the observed strike range.

    BS here is used only as a read-side smoother: it converts the observed IVs
    into prices that are differentiable in K; it does not generate decision prices.
    """
    sm = fit_smile(liquid["strike"], liquid["iv"])
    K = np.linspace(float(liquid["strike"].min()), float(liquid["strike"].max()), n_grid)
    iv = np.asarray(sm(K), float)
    price = [bs_put_price(spot, float(k), t_years, float(v), r) for k, v in zip(K, iv)]
    return pd.DataFrame({"K": K, "iv": iv, "price": price})


def cdf_from_grid(grid: pd.DataFrame, t_years: float, r: float = 0.0) -> pd.DataFrame:
    """Risk-neutral CDF: P(S_T <= K) = e^{rT} · ∂P/∂K (Breeden-Litzenberger, 1st order).

    The first derivative is numerically more stable than the second: probabilities
    use this one, while the density (2nd order) only serves as a diagnostic.
    """
    K = grid["K"].to_numpy()
    P = grid["price"].to_numpy()
    cdf = np.exp(r * t_years) * np.gradient(P, K)
    return pd.DataFrame({"K": K, "cdf": np.clip(cdf, 0.0, 1.0)})


def density_from_grid(grid: pd.DataFrame, t_years: float, r: float = 0.0) -> pd.DataFrame:
    """Risk-neutral density: e^{rT} · ∂²P/∂K², floored at 0 (numerical noise)."""
    K = grid["K"].to_numpy()
    P = grid["price"].to_numpy()
    dens = np.exp(r * t_years) * np.gradient(np.gradient(P, K), K)
    return pd.DataFrame({"K": K, "density": np.clip(dens, 0.0, None)})


def prob_below(cdf: pd.DataFrame, level: float) -> float:
    """P(S_T <= level) by interpolating the CDF; nan outside the observed range (no extrapolation)."""
    K = cdf["K"].to_numpy()
    if not (K.min() <= level <= K.max()):
        return float("nan")
    return float(np.interp(level, K, cdf["cdf"].to_numpy()))


def tail_cost(liquid: pd.DataFrame, cdf: pd.DataFrame) -> pd.DataFrame:
    """Premium per unit of ITM probability: mid / P(S_T <= K), per observed strike.

    Where it's lowest, $1 of hedge buys more tail: this is the relative
    cheap/expensive measure across the smile, derived purely from observed
    prices + the derived CDF.
    """
    out = liquid[["strike", "mid"]].copy()
    out["prob_itm"] = [prob_below(cdf, float(k)) for k in out["strike"]]
    out["cost_per_prob"] = out["mid"] / out["prob_itm"]
    return out


DEFAULT_LEVELS = (-0.10, -0.20, -0.30)


def density_report(
    chain: pd.DataFrame,
    spot: float,
    expiry: str,
    today: date,
    r: float = 0.0,
    levels: tuple = DEFAULT_LEVELS,
    max_spread_pct: float = 0.25,
    symbol: str = "SPX",
    style: str = "european",
    pays_dividends: bool = False,
) -> str:
    """B-L section of the advisor report. Raises ValueError if the liquid chain is too short."""
    liquid = filter_liquid(chain[chain["expiry"] == expiry], max_spread_pct)
    if len(liquid) < MIN_LIQUID_STRIKES:
        raise ValueError(
            f"Only {len(liquid)} liquid strikes for {expiry} "
            f"(minimum {MIN_LIQUID_STRIKES}): B-L density unreliable."
        )
    t_years = dte_days(expiry, today) / 365.0
    grid = price_grid(liquid, spot, t_years, r)
    cdf = cdf_from_grid(grid, t_years, r)
    tc = tail_cost(liquid, cdf)

    lines = [f"-- Risk-neutral density (Breeden–Litzenberger) — expiry {expiry} --"]
    for lv in levels:
        level_k = spot * (1 + lv)
        p = prob_below(cdf, level_k)
        val = "outside the liquid-strike range" if p != p else f"{p:.1%}"
        lines.append(f"P({symbol} < {level_k:,.0f} = {lv:+.0%} at expiry): {val}")
    best = tc.loc[tc["cost_per_prob"].idxmin()]
    lines.append(
        f"Cheapest zone of the smile (per $ of tail): strike {best['strike']:,.0f} "
        f"(premium ${best['mid']:,.2f}, P(ITM) {best['prob_itm']:.1%}, "
        f"cost per unit of prob. ${best['cost_per_prob']:,.0f})"
    )
    div_note = (f"{symbol} dividends not modeled"
                if pays_dividends else f"{symbol} dividends ignored")
    caveat = (
        f"Caveat: risk-neutral probabilities (as priced by the market, not a real-world "
        f"estimate); {len(liquid)} liquid strikes used, wings excluded, no extrapolation; "
        f"forward with q=0 ({div_note}, ~1% drift distortion)."
    )
    if style == "american":
        caveat += (" American option: early-exercise premium ~nil on the OTM wing "
                   "used (exercise only pays deep ITM).")
    lines.append(caveat)
    return "\n".join(lines)
