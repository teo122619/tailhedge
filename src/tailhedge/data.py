"""Price-history abstraction for the beta/sizing pipeline.

Defines the `PriceHistoryProvider` protocol (a ticker + lookback -> daily
closes) plus two implementations: an in-memory fake for tests and a CSV-backed
one for offline sizing runs. `sizing.py` consumes this protocol; `ibkr.py`
supplies the live IBKR-backed implementation.
"""

from __future__ import annotations

from typing import Protocol

import pandas as pd


class PriceHistoryProvider(Protocol):
    def daily_closes(self, ticker: str, lookback_days: int) -> pd.Series: ...


class FakePriceHistoryProvider:
    """In-memory provider for tests: dict ticker -> close-price series."""

    def __init__(self, data: dict[str, pd.Series]):
        self._data = data

    def daily_closes(self, ticker: str, lookback_days: int) -> pd.Series:
        return self._data[ticker].iloc[-lookback_days:]


class CsvPriceHistoryProvider:
    """Provider from a wide CSV: a `date` column plus one price column per ticker."""

    def __init__(self, csv_path: str):
        df = pd.read_csv(csv_path, parse_dates=["date"]).set_index("date").sort_index()
        self._df = df

    def daily_closes(self, ticker: str, lookback_days: int) -> pd.Series:
        return self._df[ticker].iloc[-lookback_days:]
