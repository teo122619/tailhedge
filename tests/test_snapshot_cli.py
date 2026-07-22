import pandas as pd

from tailhedge.ibkr import MarketSnapshot
from tailhedge.snapshot_cli import format_snapshot


def test_format_snapshot_contains_key_fields():
    chain = pd.DataFrame(
        {
            "expiry": ["20260918", "20260918"],
            "strike": [6000.0, 6500.0],
            "right": ["P", "P"],
            "iv": [0.28, 0.22],
            "delta": [-0.10, -0.20],
            "mid": [16.0, 31.0],
            "moneyness": [0.80, 0.867],
        }
    )
    snap = MarketSnapshot(
        spot=7500.0,
        vix_term={"VIX": 15.0, "VIX3M": 17.0, "VIX6M": None},
        put_chain=chain,
        expiry="20260918",
    )
    out = format_snapshot(snap)
    assert "7,500" in out or "7500" in out
    assert "VIX" in out
    assert "6000" in out and "6500" in out
    assert "20260918" in out
    assert "n/a" in out  # VIX6M missing


def test_format_snapshot_warns_on_nan_spot():
    snap = MarketSnapshot(
        spot=float("nan"), vix_term={"VIX": None}, put_chain=pd.DataFrame(), expiry="20261218"
    )
    out = format_snapshot(snap)
    assert "not available" in out.lower()
