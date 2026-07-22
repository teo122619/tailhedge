from types import SimpleNamespace

import pandas as pd
import pytest

from tailhedge.ibkr import fetch_spot, fetch_vix_term_structure, MarketSnapshot


class _IBTickers:
    """Mock: maps symbol -> marketPrice via reqTickers."""

    def __init__(self, prices):
        self._prices = prices

    def qualifyContracts(self, *contracts):
        return list(contracts)

    def reqTickers(self, *contracts):
        out = []
        for c in contracts:
            price = self._prices.get(c.symbol)
            out.append(SimpleNamespace(contract=c, marketPrice=lambda p=price: p, close=price))
        return out


def test_fetch_spot():
    ib = _IBTickers({"SPX": 7500.0})
    assert fetch_spot(ib, "SPX") == 7500.0


def test_fetch_vix_term_structure_handles_missing():
    ib = _IBTickers({"VIX": 15.0, "VIX3M": 17.0})  # VIX6M missing -> None
    term = fetch_vix_term_structure(ib, symbols=("VIX", "VIX3M", "VIX6M"))
    assert term["VIX"] == 15.0
    assert term["VIX3M"] == 17.0
    assert term["VIX6M"] is None


def test_market_snapshot_dataclass_shape():
    snap = MarketSnapshot(
        spot=7500.0, vix_term={"VIX": 15.0}, put_chain=pd.DataFrame(), expiry="20260918"
    )
    assert snap.spot == 7500.0
    assert snap.expiry == "20260918"


def _param(tc, expirations, strikes, exchange="SMART"):
    return SimpleNamespace(
        exchange=exchange, tradingClass=tc, expirations=set(expirations), strikes=set(strikes)
    )


def test_ensure_expiry_available_ok_for_valid():
    from tailhedge.ibkr import ensure_expiry_available

    params = [_param("SPX", {"20261218", "20260320"}, {6000, 7000})]
    ensure_expiry_available(params, "SPX", "20261218")  # does not raise


def test_ensure_expiry_available_raises_on_invalid():
    from tailhedge.ibkr import ensure_expiry_available, MarketDataUnavailableError

    params = [_param("SPX", {"20261218"}, {6000, 7000})]
    with pytest.raises(MarketDataUnavailableError, match="20261217"):
        ensure_expiry_available(params, "SPX", "20261217")


def _cd(strike, right="P"):
    # mimics ContractDetails: .contract with right/strike/conId (already qualified)
    return SimpleNamespace(
        contract=SimpleNamespace(right=right, strike=float(strike), conId=int(strike))
    )


def test_otm_puts_in_band_filters_and_sorts():
    from tailhedge.ibkr import otm_puts_in_band

    details = [_cd(7200), _cd(7175), _cd(7000), _cd(8000), _cd(7500, right="C")]
    # spot 7500, band 0.05 -> [7125, 7500]: inside 7175, 7200; outside 7000 (below), 8000 (above); call discarded
    out = otm_puts_in_band(details, spot=7500.0, band=0.05)
    assert [c.strike for c in out] == [7175.0, 7200.0]
    assert all(c.right == "P" for c in out)


class _IBStreaming:
    """Fake: reqMktData returns a ticker stub whose greeks populate after N sleeps."""

    def __init__(self, contracts, ready_after=2):
        self._ready_after = ready_after
        self.sleep_count = 0
        self.cancelled = []
        self._tickers = {id(c): SimpleNamespace(contract=c, modelGreeks=None) for c in contracts}

    def reqMktData(self, c, *a, **k):
        return self._tickers[id(c)]

    def sleep(self, _):
        self.sleep_count += 1
        if self.sleep_count >= self._ready_after:
            for t in self._tickers.values():
                t.modelGreeks = SimpleNamespace(delta=-0.1, impliedVol=0.2, vega=1.0, gamma=0.001)

    def cancelMktData(self, c):
        self.cancelled.append(c)


def _contracts(n):
    return [SimpleNamespace(strike=float(7000 + i)) for i in range(n)]


def test_collect_greeks_returns_one_ticker_per_contract_with_greeks():
    from tailhedge.ibkr import collect_greeks

    contracts = _contracts(3)
    ib = _IBStreaming(contracts, ready_after=2)
    tickers = collect_greeks(ib, contracts, timeout=8)
    assert len(tickers) == 3
    assert all(t.modelGreeks is not None and t.modelGreeks.delta is not None for t in tickers)


def test_collect_greeks_breaks_early_once_ready():
    from tailhedge.ibkr import collect_greeks

    contracts = _contracts(2)
    ib = _IBStreaming(contracts, ready_after=2)
    collect_greeks(ib, contracts, timeout=8)
    assert ib.sleep_count == 2


def test_collect_greeks_cancels_streaming_for_every_contract():
    from tailhedge.ibkr import collect_greeks

    contracts = _contracts(3)
    ib = _IBStreaming(contracts, ready_after=2)
    collect_greeks(ib, contracts, timeout=8)
    assert len(ib.cancelled) == 3
    assert all(c in ib.cancelled for c in contracts)


def test_collect_greeks_times_out_if_greeks_never_populate():
    from tailhedge.ibkr import collect_greeks

    contracts = _contracts(2)
    ib = _IBStreaming(contracts, ready_after=99)  # not ready within the timeout
    tickers = collect_greeks(ib, contracts, timeout=3)
    assert ib.sleep_count == 3
    assert len(tickers) == 2
    assert len(ib.cancelled) == 2  # cancelled regardless (finally)


def test_summarize_option_params():
    from tailhedge.ibkr import summarize_option_params

    params = [
        SimpleNamespace(
            exchange="SMART",
            tradingClass="SPX",
            expirations={"20260918", "20261218", "20260320"},
            strikes={5000, 6000},
        ),
        SimpleNamespace(
            exchange="SMART",
            tradingClass="SPXW",
            expirations={"20260731", "20260807"},
            strikes={5000},
        ),
    ]
    out = summarize_option_params(params)
    assert "SPX" in out and "SPXW" in out
    assert "20260918" in out  # SPX expiry sorted and shown
    assert "3 expiries" in out  # SPX has 3


def _fake_option_ticker(expiry, strike, bid=2.0, ask=2.4):
    contract = SimpleNamespace(right="P", strike=strike,
                               lastTradeDateOrContractMonth=expiry)
    greeks = SimpleNamespace(impliedVol=0.3, delta=-0.05, vega=1.0, gamma=0.001)
    return SimpleNamespace(contract=contract, modelGreeks=greeks, bid=bid, ask=ask)


class _IBMulti:
    """Fake: reqContractDetails per expiry + streaming reqMktData."""

    def __init__(self, strikes_by_expiry):
        self._by_exp = strikes_by_expiry

    def reqContractDetails(self, template):
        exp = template.lastTradeDateOrContractMonth
        return [SimpleNamespace(contract=SimpleNamespace(
                    right="P", strike=k, lastTradeDateOrContractMonth=exp))
                for k in self._by_exp.get(exp, [])]

    def reqMktData(self, c, *a):
        return _fake_option_ticker(c.lastTradeDateOrContractMonth, c.strike)

    def sleep(self, s):
        pass

    def cancelMktData(self, c):
        pass


def test_fetch_puts_multi_merges_expiries_in_band():
    from tailhedge.ibkr import fetch_puts_multi

    ib = _IBMulti({"20261118": [4000.0, 4500.0, 4875.0],
                   "20270113": [4500.0, 4875.0, 5100.0]})
    chain = fetch_puts_multi(ib, 7500.0, "SPX", ["20261118", "20270113"],
                             strike_lo=4125.0, strike_hi=4875.0, trading_class="SPX")
    assert set(chain["expiry"]) == {"20261118", "20270113"}
    assert chain["strike"].between(4125.0, 4875.0).all()
    assert len(chain) == 4      # 4500+4875 for each expiry
    assert (chain["moneyness"] == chain["strike"] / 7500.0).all()


def test_fetch_puts_multi_empty_band_raises_clean_error():
    from tailhedge.ibkr import MarketDataUnavailableError, fetch_puts_multi

    ib = _IBMulti({"20261118": [6000.0]})
    with pytest.raises(MarketDataUnavailableError):
        fetch_puts_multi(ib, 7500.0, "SPX", ["20261118"], 4125.0, 4875.0, "SPX")


def test_read_hedge_book_filters_via_classify():
    from datetime import date
    from tailhedge.ibkr import read_hedge_book

    put = SimpleNamespace(contract=SimpleNamespace(
        secType="OPT", right="P", symbol="SPY", strike=440.0,
        lastTradeDateOrContractMonth="20270115", conId=42, multiplier="100"),
        position=3.0, marketValue=2_100.0, averageCost=650.0)
    call = SimpleNamespace(contract=SimpleNamespace(
        secType="OPT", right="C", symbol="SPX", strike=6000.0,
        lastTradeDateOrContractMonth="20260722", conId=43, multiplier="100"),
        position=-1.0, marketValue=-500.0, averageCost=200.0)
    ib = SimpleNamespace(portfolio=lambda: [put, call])
    book = read_hedge_book(ib, date(2026, 7, 21))
    assert len(book) == 1 and book[0].symbol == "SPY" and book[0].qty == 3
