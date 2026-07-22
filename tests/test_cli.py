from pathlib import Path
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
