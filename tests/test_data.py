import pandas as pd
from tailhedge.data import FakePriceHistoryProvider


def test_fake_provider_returns_last_n():
    s = pd.Series(range(10), index=pd.date_range("2020-01-01", periods=10), dtype=float)
    prov = FakePriceHistoryProvider({"A": s})
    out = prov.daily_closes("A", lookback_days=3)
    assert list(out.to_numpy()) == [7.0, 8.0, 9.0]
