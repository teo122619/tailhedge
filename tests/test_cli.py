from pathlib import Path

import numpy as np
import pandas as pd

from tailhedge.cli import main

FIX = Path(__file__).parent / "fixtures"


def test_cli_runs_and_reports(capsys):
    rc = main([
        "--prices", str(FIX / "prices.csv"),
        "--positions", str(FIX / "positions.csv"),
        "--spx", "SPX",
        "--windows", "5",
        "--lookback", "10",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "beta" in out.lower()
    assert "notional" in out.lower()


def test_cli_weekly_freq_runs_on_csv(tmp_path, capsys):
    rng = np.random.default_rng(1)
    idx = pd.bdate_range("2024-01-01", periods=120)
    prices = pd.DataFrame({
        "date": idx,
        "SPX": 100 * np.cumprod(1 + rng.normal(0, 0.01, 120)),
        "AAA": 100 * np.cumprod(1 + rng.normal(0, 0.01, 120)),
    })
    prices.to_csv(tmp_path / "prices.csv", index=False)
    pd.DataFrame({"ticker": ["AAA"], "market_value": [1000.0]}).to_csv(
        tmp_path / "positions.csv", index=False)
    rc = main([
        "--prices", str(tmp_path / "prices.csv"),
        "--positions", str(tmp_path / "positions.csv"),
        "--windows", "12", "--lookback", "120",
        "--returns-freq", "weekly",
    ])
    assert rc == 0
    assert "window=" in capsys.readouterr().out
