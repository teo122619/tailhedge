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
        "--windows", "100", "--lookback", "120",
        "--returns-freq", "weekly",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "window=" in out

    # 120 business days resampled W-FRI give 24 weekly closes -> 23 weekly
    # returns. A silently-daily run (freq dropped) would print n_obs=100
    # instead, so this discriminates broken `freq` wiring from a passing run.
    sensitivity_lines = out.splitlines()
    header_idx = next(
        i for i, line in enumerate(sensitivity_lines)
        if line.split() == ["window", "beta", "r_squared", "n_obs"]
    )
    n_obs = sensitivity_lines[header_idx + 1].split()[-1]
    assert n_obs == "23"
