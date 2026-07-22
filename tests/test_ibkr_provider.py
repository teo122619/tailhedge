from types import SimpleNamespace

import pytest

from tailhedge.ibkr import IBKRPriceHistoryProvider, MarketDataUnavailableError


class _FakeIB:
    def __init__(self, bars, resolves=True):
        self._bars = bars
        self._resolves = resolves
        self.last_kwargs = None
        self.hist_calls = 0

    def qualifyContracts(self, *contracts):
        # IBKR populates conId on resolved contracts and leaves 0 on unknown ones
        for c in contracts:
            c.conId = 265598 if self._resolves else 0
        return list(contracts)

    def reqHistoricalData(self, contract, **kwargs):
        self.hist_calls += 1
        self.last_kwargs = kwargs
        return self._bars


def _bars():
    return [
        SimpleNamespace(date="2020-01-01", close=100.0),
        SimpleNamespace(date="2020-01-02", close=101.0),
    ]


def test_provider_daily_closes_uses_reqhistoricaldata():
    ib = _FakeIB(_bars())
    prov = IBKRPriceHistoryProvider(ib)
    s = prov.daily_closes("SPX", lookback_days=250)
    assert list(s.to_numpy()) == [100.0, 101.0]
    assert ib.last_kwargs["durationStr"] == "250 D"
    assert ib.last_kwargs["barSizeSetting"] == "1 day"


def test_unresolved_ticker_names_the_symbol_not_the_connection():
    """A symbol not quoted on SMART/USD (e.g. a European ETF like VUSA) used to give
    Error 200 -> empty series -> a message that blamed the connection and sent you to
    close IBKR sessions. The cause should be stated where it's known: here."""
    ib = _FakeIB(_bars(), resolves=False)
    prov = IBKRPriceHistoryProvider(ib)
    with pytest.raises(MarketDataUnavailableError, match="VUSA"):
        prov.daily_closes("VUSA", lookback_days=250)
    assert ib.hist_calls == 0   # historical data isn't requested for a non-existent contract


def test_provider_uses_listing_for_contract():
    ib = _FakeIB(_bars())
    seen = {}
    orig = ib.qualifyContracts

    def spy(*contracts):
        seen["c"] = contracts[0]
        return orig(*contracts)

    ib.qualifyContracts = spy
    prov = IBKRPriceHistoryProvider(ib, listings={"SXR8": ("IBIS2", "EUR")})
    prov.daily_closes("SXR8", lookback_days=250)
    assert seen["c"].exchange == "IBIS2"
    assert seen["c"].currency == "EUR"


def test_provider_unresolved_error_mentions_listing_columns():
    ib = _FakeIB(_bars(), resolves=False)
    prov = IBKRPriceHistoryProvider(ib)
    with pytest.raises(MarketDataUnavailableError, match="exchange and currency columns"):
        prov.daily_closes("VUSA", lookback_days=250)
