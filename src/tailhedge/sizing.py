"""Beta sizing: how much SPX-equivalent notional the portfolio needs hedged.

Regresses portfolio returns (built from `data.py`-provided price histories and
current position weights) against SPX returns across one or more lookback
windows, and turns the resulting beta into a notional-to-hedge figure. Feeds
`advisor.py`/`advisor_cli.py` and `hedge_cli.py` when run with `--portfolio`;
exposed standalone via `cli.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from tailhedge.data import PriceHistoryProvider


class InsufficientDataError(ValueError):
    """Historical data missing or insufficient for the calculation (e.g. reqHistoricalData empty)."""


def build_portfolio_returns(prices: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    """Daily portfolio returns series (weighted simple returns).

    `prices` has a date index and one close-price column per ticker; `weights`
    are the current weights (normalized internally to sum to 1). Returns the
    aligned series with no NaN.
    """
    w = pd.Series(weights, dtype=float)
    w = w / w.sum()
    rets = prices[w.index].pct_change().dropna(how="any")
    port = rets.to_numpy() @ w.to_numpy()
    return pd.Series(port, index=rets.index, name="portfolio")


@dataclass(frozen=True)
class BetaResult:
    beta: float
    alpha: float
    r_squared: float
    n_obs: int


def estimate_beta(portfolio_returns: pd.Series, spx_returns: pd.Series) -> BetaResult:
    """OLS (with intercept) of `portfolio` on `spx`, aligned on common dates.

    beta = SPX-equivalent exposure; r_squared = share of risk explained by SPX.
    """
    df = pd.concat({"p": portfolio_returns, "m": spx_returns}, axis=1).dropna()
    n = len(df)
    if n < 2:
        raise InsufficientDataError(
            f"At least 2 aligned observations are needed to estimate beta, got {n}."
        )
    p = df["p"].to_numpy()
    m = df["m"].to_numpy()
    cov = np.cov(p, m, ddof=1)
    beta = float(cov[0, 1] / cov[1, 1])
    alpha = float(p.mean() - beta * m.mean())
    r = float(np.corrcoef(p, m)[0, 1])
    return BetaResult(beta=beta, alpha=alpha, r_squared=r * r, n_obs=n)


def beta_window_sensitivity(
    portfolio_returns: pd.Series, spx_returns: pd.Series, windows: list[int]
) -> pd.DataFrame:
    """Estimate beta/R² over multiple windows (last `window` days each).

    Shows the sensitivity of beta to the window instead of fixing a single one.
    """
    rows = []
    for w in windows:
        res = estimate_beta(portfolio_returns.iloc[-w:], spx_returns.iloc[-w:])
        rows.append(
            {"window": w, "beta": res.beta, "r_squared": res.r_squared, "n_obs": res.n_obs}
        )
    return pd.DataFrame(rows, columns=["window", "beta", "r_squared", "n_obs"])


def weights_from_positions(market_values: dict[str, float]) -> dict[str, float]:
    """Normalize the current market values into weights (sum to 1)."""
    total = sum(market_values.values())
    return {k: v / total for k, v in market_values.items()}


def hedge_notional(beta: float, nav: float) -> float:
    """SPX-equivalent notional to hedge: beta * NAV."""
    return beta * nav


@dataclass(frozen=True)
class SizingReport:
    nav: float
    sensitivity: pd.DataFrame
    notional_by_window: dict[int, float]
    spx_ticker: str


def run_sizing(
    provider: PriceHistoryProvider,
    positions: dict[str, float],
    spx_ticker: str,
    windows: list[int],
    lookback_days: int,
    nav: float | None = None,
) -> SizingReport:
    """Orchestrator: downloads history via the provider, estimates beta/R² by
    window, and the SPX-equivalent notional to hedge.

    `positions` are the current market values per ticker; if `nav` is None, uses
    the sum of the market values.
    """
    nav = float(sum(positions.values())) if nav is None else float(nav)
    weights = weights_from_positions(positions)
    cols = {t: provider.daily_closes(t, lookback_days) for t in positions}
    cols[spx_ticker] = provider.daily_closes(spx_ticker, lookback_days)
    empty = [t for t, s in cols.items() if s is None or len(s) == 0]
    if empty:
        raise InsufficientDataError(
            "No historical data received from IBKR for: "
            + ", ".join(empty)
            + ". Likely a connection/subscription issue — e.g. Error 162 "
            "'Trading TWS session is connected from a different IP address' "
            "(close other IBKR sessions: mobile app, web, other Gateways; "
            "then logout/login in TWS and retry)."
        )
    prices = pd.DataFrame(cols).dropna(how="any")
    if len(prices) < 2:
        raise InsufficientDataError(
            f"Histories misaligned or too short (usable rows: {len(prices)}). "
            "Check that all tickers have enough overlapping history."
        )
    port_ret = build_portfolio_returns(prices, weights)
    spx_ret = prices[spx_ticker].pct_change().dropna()
    sens = beta_window_sensitivity(port_ret, spx_ret, windows)
    notional = {
        int(row.window): hedge_notional(float(row.beta), nav)
        for row in sens.itertuples()
    }
    return SizingReport(
        nav=nav, sensitivity=sens, notional_by_window=notional, spx_ticker=spx_ticker
    )
