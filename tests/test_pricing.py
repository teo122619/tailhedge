from tailhedge.pricing import norm_cdf, bs_put_price, bs_put_delta


def test_norm_cdf_known_values():
    assert abs(norm_cdf(0.0) - 0.5) < 1e-12
    assert abs(norm_cdf(1.96) - 0.9750) < 1e-3
    assert abs(norm_cdf(-1.96) - 0.0250) < 1e-3


def test_bs_put_price_atm_known():
    # S=K=100, T=1, sigma=0.20, r=q=0 -> theoretical put ~ 7.9656
    assert abs(bs_put_price(100, 100, 1.0, 0.20) - 7.9656) < 1e-3


def test_bs_put_price_intrinsic_at_or_after_expiry():
    assert bs_put_price(90, 100, 0.0, 0.20) == 10.0   # ITM: intrinsic
    assert bs_put_price(110, 100, 0.0, 0.20) == 0.0    # OTM: zero
    assert bs_put_price(90, 100, 0.5, 0.0) == 10.0     # zero vol: intrinsic


def test_bs_put_delta_atm():
    # ATM, S=K=100, T=1, sigma=0.2, r=q=0 -> delta ~ -0.4602
    assert abs(bs_put_delta(100, 100, 1.0, 0.20) - (-0.4602)) < 1e-3


def test_bs_put_delta_bounds():
    assert bs_put_delta(100, 60, 0.5, 0.20) > -0.05    # deep OTM -> ~0
    assert bs_put_delta(100, 200, 0.5, 0.20) < -0.95   # deep ITM -> ~-1


def test_bs_put_delta_matches_finite_difference():
    S, K, T, sig = 100.0, 90.0, 0.5, 0.25
    h = 1e-3
    fd = (bs_put_price(S + h, K, T, sig) - bs_put_price(S - h, K, T, sig)) / (2 * h)
    assert abs(bs_put_delta(S, K, T, sig) - fd) < 1e-4
