"""IBKR connection settings (host, port, client ID, market data type).

Sits at the base of the pipeline: every module that talks to TWS/IB Gateway
(`ibkr.py` and, through it, the CLIs) reads its connection parameters from
`IBKRConfig`, sourced from environment variables with sane paper-trading
defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class IBKRConfig:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 11
    # 4 = delayed-frozen: real-time where you have a subscription (SPX index, options),
    # delayed where you don't (SPY equity), frozen (last close) when the market is closed.
    market_data_type: int = 4

    @classmethod
    def from_env(cls) -> "IBKRConfig":
        return cls(
            host=os.environ.get("TAILHEDGE_IB_HOST", "127.0.0.1"),
            port=int(os.environ.get("TAILHEDGE_IB_PORT", "7497")),
            client_id=int(os.environ.get("TAILHEDGE_IB_CLIENT_ID", "11")),
            market_data_type=int(os.environ.get("TAILHEDGE_IB_MKT_DATA_TYPE", "4")),
        )
