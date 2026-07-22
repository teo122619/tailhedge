"""Read-only IBKR/TWS gateway layer: contracts, spot, option chains, account.

Wraps `ib_async` (imported lazily so pure logic and tests don't need a live
connection) behind `IBKRConnection` and a set of fetch functions — spot price,
VIX term structure, put chains by band or absolute strike range, and the
current hedge book from the account portfolio. Sits between `config.py` and
the rest of the pipeline: `data.py`'s IBKR-backed provider, `sizing.py`,
`advisor.py`/`advisor_cli.py`, and `hedge_cli.py`/`snapshot_cli.py` all read
market data through here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

# NB: ib_async is imported lazily inside the broker functions (at the bottom of
# the file), so this module and the pure tests don't require it installed/connected.

_INDEX_EXCHANGE = {
    "SPX": "CBOE",
    "VIX": "CBOE",
    "VIX3M": "CBOE",
    "VIX6M": "CBOE",
    "VIX9D": "CBOE",
}


@dataclass(frozen=True)
class ContractSpec:
    sec_type: str
    symbol: str
    exchange: str
    currency: str = "USD"


def resolve_contract(ticker: str, exchange: str | None = None,
                     currency: str | None = None) -> ContractSpec:
    t = ticker.upper()
    if exchange or currency:
        # Declared listing (portfolio sheet columns): always a stock/ETF line.
        return ContractSpec("STK", t, exchange or "SMART", (currency or "USD").upper())
    if t in _INDEX_EXCHANGE:
        return ContractSpec("IND", t, _INDEX_EXCHANGE[t], "USD")
    return ContractSpec("STK", t, "SMART", "USD")


def duration_str(lookback_days: int) -> str:
    if lookback_days <= 365:
        return f"{lookback_days} D"
    return f"{math.ceil(lookback_days / 365)} Y"


def bars_to_series(bars) -> pd.Series:
    dates = pd.to_datetime([b.date for b in bars])
    closes = [float(b.close) for b in bars]
    return pd.Series(closes, index=dates, name="close").sort_index()


def mid(bid, ask) -> float:
    """Mid of bid/ask discarding IBKR sentinels (None / values <= 0, e.g. -1)."""
    vals = [float(v) for v in (bid, ask) if v is not None and v > 0]
    return sum(vals) / len(vals) if vals else float("nan")


def organize_put_chain(chain_tickers, spot: float) -> pd.DataFrame:
    """Puts only, fixed columns, sorted by (expiry, strike). moneyness = strike/spot."""
    rows = []
    for tk in chain_tickers:
        c = tk.contract
        if c.right != "P":
            continue
        mg = tk.modelGreeks

        def _greek(attr):
            v = getattr(mg, attr, None) if mg is not None else None
            return float(v) if v is not None else float("nan")

        rows.append(
            {
                "expiry": c.lastTradeDateOrContractMonth,
                "strike": float(c.strike),
                "right": c.right,
                "iv": _greek("impliedVol"),
                "delta": _greek("delta"),
                "vega": _greek("vega"),
                "gamma": _greek("gamma"),
                "mid": mid(tk.bid, tk.ask),
                # bid/ask raw (IBKR sentinels -1 included): discarded downstream by filter_liquid (density.py)
                "bid": float(tk.bid) if tk.bid is not None else float("nan"),
                "ask": float(tk.ask) if tk.ask is not None else float("nan"),
                "moneyness": float(c.strike) / spot,
            }
        )
    df = pd.DataFrame(
        rows,
        columns=[
            "expiry", "strike", "right", "iv", "delta", "vega", "gamma",
            "mid", "bid", "ask", "moneyness",
        ],
    )
    return df.sort_values(["expiry", "strike"]).reset_index(drop=True)


# --- Live path (lazy ib_async import) ---------------------------------


def to_ib_contract(spec: ContractSpec):
    from ib_async import Index, Stock  # lazy import

    if spec.sec_type == "IND":
        return Index(spec.symbol, spec.exchange, spec.currency)
    return Stock(spec.symbol, spec.exchange, spec.currency)


class IBKRConnection:
    """Context manager: connects/disconnects an ib_async session."""

    def __init__(self, config=None):
        from tailhedge.config import IBKRConfig

        self.config = config or IBKRConfig.from_env()
        self._ib = None

    def __enter__(self):
        from ib_async import IB  # lazy import

        self._ib = IB()
        self._ib.connect(self.config.host, self.config.port, clientId=self.config.client_id)
        # delayed-frozen (4) by default: real-time where subscribed, delayed elsewhere,
        # frozen (last close) when the market is closed.
        self._ib.reqMarketDataType(self.config.market_data_type)
        return self._ib

    def __exit__(self, *exc):
        if self._ib is not None:
            self._ib.disconnect()


def _candidate_listings(ib, symbol: str, currency: str | None) -> str:
    """Best-effort ' Candidates: ...' suffix from reqContractDetails; '' on any failure."""
    try:
        from ib_async import Stock
        details = ib.reqContractDetails(Stock(symbol, "", currency or ""))
        pairs = sorted({f"{d.contract.primaryExchange or d.contract.exchange}/"
                        f"{d.contract.currency}" for d in details})
        return f" Candidates: {', '.join(pairs)}." if pairs else ""
    except Exception:
        return ""


class IBKRPriceHistoryProvider:
    """Implements PriceHistoryProvider on top of real IBKR data."""

    def __init__(self, ib, listings: dict[str, tuple[str | None, str | None]] | None = None):
        self._ib = ib
        self._listings = listings or {}

    def daily_closes(self, ticker: str, lookback_days: int) -> pd.Series:
        exch, cur = self._listings.get(ticker, (None, None))
        contract = to_ib_contract(resolve_contract(ticker, exch, cur))
        self._ib.qualifyContracts(contract)
        if not getattr(contract, "conId", 0):
            raise MarketDataUnavailableError(
                f"Ticker '{ticker}' not recognized by IBKR as "
                f"{contract.secType}/{contract.exchange}/{contract.currency}. "
                "For non-US instruments, fill the exchange and currency columns in the "
                "portfolio sheet (e.g. SXR8 / IBIS / EUR)."
                + _candidate_listings(self._ib, contract.symbol, cur)
            )
        bars = self._ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration_str(lookback_days),
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
        )
        return bars_to_series(bars).iloc[-lookback_days:]


@dataclass
class MarketSnapshot:
    spot: float
    vix_term: dict
    put_chain: pd.DataFrame
    expiry: str


def _ticker_price(tk) -> float:
    try:
        p = tk.marketPrice()
    except Exception:
        p = None
    if p is None or (isinstance(p, float) and (p != p or p <= 0)):
        p = getattr(tk, "close", None)
    return float(p) if p is not None else float("nan")


def fetch_spot(ib, symbol: str = "SPX") -> float:
    contract = to_ib_contract(resolve_contract(symbol))
    ib.qualifyContracts(contract)
    (tk,) = ib.reqTickers(contract)
    return _ticker_price(tk)


def fetch_vix_term_structure(ib, symbols=("VIX", "VIX3M", "VIX6M")) -> dict:
    out: dict = {}
    for sym in symbols:
        try:
            contract = to_ib_contract(resolve_contract(sym))
            ib.qualifyContracts(contract)
            (tk,) = ib.reqTickers(contract)
            price = _ticker_price(tk)
            out[sym] = None if price != price else price  # nan -> None
        except Exception:
            out[sym] = None
    return out


class MarketDataUnavailableError(ValueError):
    """Expiry/contracts not available on the market (e.g. non-existent expiry)."""


def _params_for_class(params, trading_class):
    return [p for p in params if getattr(p, "tradingClass", None) == trading_class]


def available_expiries(params, trading_class) -> list[str]:
    exps: set[str] = set()
    for p in _params_for_class(params, trading_class):
        exps |= set(getattr(p, "expirations", []))
    return sorted(exps)


def nearest_expiry(target: str, available: list[str]) -> str:
    """The expiry in `available` (YYYYMMDD) with the smallest day gap from `target`.

    SPX (AM, Thursday) and SPY (PM, 3rd Friday) offset the monthlies by ~1 day: when
    falling back to SPY, the SPY expiry nearest to the selected SPX expiry is chosen.
    On a tie, the nearer one going forward (>= target) wins, then the first in order.
    """
    from datetime import datetime
    if not available:
        raise MarketDataUnavailableError(
            f"No expiry available to map {target}."
        )
    t = datetime.strptime(target, "%Y%m%d").date()
    def _key(e):
        d = (datetime.strptime(e, "%Y%m%d").date() - t).days
        return (abs(d), 0 if d >= 0 else 1, e)   # tie-break: forward first, then lexicographic
    return min(available, key=_key)


def ensure_expiry_available(params, trading_class, expiry) -> None:
    """Raises MarketDataUnavailableError if the expiry doesn't exist for the tradingClass.

    Cheap validation (a single reqSecDefOptParams) with the message + --list-expiries hint,
    before querying the chain.
    """
    exps = available_expiries(params, trading_class)
    if expiry not in exps:
        sample = ", ".join(exps[:10]) if exps else "(none)"
        raise MarketDataUnavailableError(
            f"Expiry {expiry} not available for tradingClass {trading_class}. "
            f"Valid expiries (first ones): {sample}. Use --list-expiries for the full list "
            "(remember: SPX monthlies expire on the 3rd Friday)."
        )


def otm_puts_in_band(details, spot: float, band: float) -> list:
    """From a list of ContractDetails (reqContractDetails), the OTM put contracts in the band.

    The contracts arrive already qualified (conId populated) and restricted to the strikes
    ACTUALLY quoted for that expiry: no phantom strikes from a union, no
    storm of Error 200. Band = [ (1-band)*spot , spot ].
    """
    lo, hi = (1 - band) * spot, spot
    puts = [
        cd.contract
        for cd in details
        if cd.contract.right == "P" and lo <= float(cd.contract.strike) <= hi
    ]
    return sorted(puts, key=lambda c: float(c.strike))


def expiries_in_dte_range(expiries, today, lo: int = 90, hi: int = 180) -> list[str]:
    """The expiries within the cycle's entry window. Sorted ascending."""
    from datetime import datetime

    def _dte(e):
        return (datetime.strptime(e, "%Y%m%d").date() - today).days

    return sorted(e for e in expiries if lo <= _dte(e) <= hi)


def puts_in_strike_range(details, lo: float, hi: float) -> list:
    """Like otm_puts_in_band but with ABSOLUTE bounds: needed for the deep band
    of the moneyness-based selection (e.g. [0.55, 0.65]·spot), where the ceiling isn't the spot."""
    puts = [
        cd.contract
        for cd in details
        if cd.contract.right == "P" and lo <= float(cd.contract.strike) <= hi
    ]
    return sorted(puts, key=lambda c: float(c.strike))


def collect_greeks(ib, contracts, timeout=8):
    """Streams reqMktData on all contracts, waits for the model greeks to
    populate (they arrive on a later tick that reqTickers/snapshot would NOT wait for),
    then cancels the streaming. Returns the tickers (with any greeks still nan for
    illiquid strikes: discarded downstream by candidate_table/filter_liquid). timeout in
    seconds, polls every 1 s; exits as soon as ALL the greeks are ready."""
    try:
        tickers = [ib.reqMktData(c, "", False, False) for c in contracts]
        for _ in range(timeout):
            ib.sleep(1.0)
            if all(t.modelGreeks is not None and t.modelGreeks.delta is not None
                   for t in tickers):
                break
        return tickers
    finally:
        for c in contracts:
            ib.cancelMktData(c)


def fetch_put_chain(
    ib, underlying="SPX", expiry="", band=0.15, trading_class="SPX", greek_timeout: int = 8
) -> pd.DataFrame:
    spot = fetch_spot(ib, underlying)
    und = to_ib_contract(resolve_contract(underlying))
    ib.qualifyContracts(und)
    params = ib.reqSecDefOptParams(und.symbol, "", und.secType, und.conId)
    ensure_expiry_available(params, trading_class, expiry)

    from ib_async import Option  # lazy import

    # A single reqContractDetails (strike=0) -> ALL the real strikes for the expiry,
    # already qualified. Replaces the old "build the union of strikes and qualify":
    # that one also asked for strikes that didn't exist for the expiry (step 5 vs 25)
    # -> a storm of Error 200.
    template = Option(underlying, expiry, 0, "P", "SMART", tradingClass=trading_class)
    details = ib.reqContractDetails(template)
    contracts = otm_puts_in_band(details, spot, band)
    if not contracts:
        raise MarketDataUnavailableError(
            f"No {underlying} puts quoted for expiry {expiry} within the "
            f"{band:.0%} band (tradingClass {trading_class})."
        )
    tickers = collect_greeks(ib, contracts, greek_timeout)
    return organize_put_chain(tickers, spot)


def fetch_market_snapshot(ib, expiry: str, band: float = 0.15, trading_class: str = "SPX") -> MarketSnapshot:
    spot = fetch_spot(ib, "SPX")
    vix_term = fetch_vix_term_structure(ib)
    put_chain = fetch_put_chain(ib, "SPX", expiry, band, trading_class)
    return MarketSnapshot(spot=spot, vix_term=vix_term, put_chain=put_chain, expiry=expiry)


def list_option_params(ib, underlying: str = "SPX"):
    """Raw reqSecDefOptParams for the underlying (to discover expiries/tradingClass)."""
    und = to_ib_contract(resolve_contract(underlying))
    ib.qualifyContracts(und)
    return ib.reqSecDefOptParams(und.symbol, "", und.secType, und.conId)


def fetch_puts_multi(
    ib, spot: float, underlying: str, expiries, strike_lo: float, strike_hi: float,
    trading_class: str, greek_timeout: int = 8,
) -> pd.DataFrame:
    """Multi-expiry put chain within the ABSOLUTE strike band (moneyness-based
    selection). One reqContractDetails per expiry (real strikes, already
    qualified — same anti-Error-200 pattern as fetch_put_chain), then a SINGLE
    round of greeks streaming across all the contracts together."""
    from ib_async import Option  # lazy import

    contracts = []
    for expiry in expiries:
        template = Option(underlying, expiry, 0, "P", "SMART",
                          tradingClass=trading_class)
        details = ib.reqContractDetails(template)
        contracts += puts_in_strike_range(details, strike_lo, strike_hi)
    if not contracts:
        raise MarketDataUnavailableError(
            f"No {underlying} puts in strikes [{strike_lo:,.0f}, "
            f"{strike_hi:,.0f}] on expiries {list(expiries)} "
            f"(tradingClass {trading_class})."
        )
    tickers = collect_greeks(ib, contracts, greek_timeout)
    return organize_put_chain(tickers, spot)


def read_hedge_book(ib, today, symbols=None, exclude_conids=()):
    """The hedge book from the IBKR account (read-only): ib.portfolio() → classify_hedge."""
    from tailhedge.lifecycle import HEDGE_SYMBOLS, classify_hedge

    return classify_hedge(ib.portfolio(), today,
                          symbols=symbols or HEDGE_SYMBOLS,
                          exclude_conids=exclude_conids)


def summarize_option_params(params) -> str:
    """Readable summary of reqSecDefOptParams: one line per (exchange, tradingClass)."""
    lines = []
    for p in sorted(params, key=lambda x: (getattr(x, "exchange", ""), getattr(x, "tradingClass", ""))):
        exps = sorted(getattr(p, "expirations", []))
        preview = ", ".join(exps[:8])
        lines.append(
            f"exchange={p.exchange} tradingClass={p.tradingClass}  "
            f"{len(exps)} expiries; first: {preview}"
        )
    return "\n".join(lines)
