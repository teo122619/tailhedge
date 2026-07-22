"""Black-Scholes put pricing, one of the two quantitative helper modules
(with `density.py`) used by the rest of the pipeline.

Provides `bs_put_price` and `bs_put_delta` for a European put with a
continuous dividend yield. Used as a read-side smoother in `density.py` to
turn observed implied vols into a differentiable price curve, never to
generate decision prices on its own.
"""

from __future__ import annotations

import math

_SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erf (no external dependency)."""
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def _d1_d2(S, K, T, sigma, r, q):
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / v
    return d1, d1 - v


def bs_put_price(S, K, T, sigma, r: float = 0.0, q: float = 0.0) -> float:
    """Black-Scholes price of a European put on an index (continuous div yield q).

    At expiry (T<=0) or zero vol, returns the intrinsic value max(K-S, 0).
    """
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    d1, d2 = _d1_d2(S, K, T, sigma, r, q)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * math.exp(-q * T) * norm_cdf(-d1)


def bs_put_delta(S, K, T, sigma, r: float = 0.0, q: float = 0.0) -> float:
    """Delta of a European put = -e^{-qT} N(-d1) = e^{-qT} (N(d1) - 1). Negative."""
    if T <= 0 or sigma <= 0:
        return -1.0 if S < K else 0.0
    d1, _ = _d1_d2(S, K, T, sigma, r, q)
    return math.exp(-q * T) * (norm_cdf(d1) - 1.0)
