import pytest

from tailhedge.hedge_cli import _build_parser, parse_pair


def test_parse_pair_band_and_dte():
    assert parse_pair("0.35,0.45", "band", float) == (0.35, 0.45)
    assert parse_pair("90,180", "dte", int) == (90, 180)
    with pytest.raises(ValueError, match="band"):
        parse_pair("0.45,0.35", "band", float)      # lo >= hi
    with pytest.raises(ValueError, match="band"):
        parse_pair("0.35", "band", float)            # single value


def test_parser_defaults_from_spec():
    p = _build_parser()
    a = p.parse_args(["--notional", "500000"])
    assert a.budget_pct == 0.01
    assert a.band == "0.35,0.45"
    assert a.dte_range == "90,180"
    assert a.roll_dte == 30
    assert a.cycles_per_year == 6
    assert a.exclude_conids == ""
    assert a.force_underlying is None


def test_parser_budget_pct_unit_error():
    p = _build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["--notional", "500000", "--budget-pct", "1.0"])


def test_parser_rejects_zero_cycles_per_year():
    # 0 would reach cycle_budget (÷0) and round(365/0): reject it at the argparse layer.
    p = _build_parser()
    with pytest.raises(SystemExit) as exc:
        p.parse_args(["--notional", "500000", "--cycles-per-year", "0"])
    assert exc.value.code == 2


def test_nan_mark_position_is_clean_not_anti_chasing(monkeypatch, capsys, tmp_path):
    """A position with a non-finite mark (market closed / no data) must fail loudly
    naming the conId — NOT be silently read as a swollen book (anti-chasing)."""
    import tailhedge.hedge_cli as hc
    import tailhedge.ibkr as ibkr
    from tailhedge.lifecycle import HedgePosition

    class FakeConn:
        def __enter__(self):
            return object()
        def __exit__(self, *a):
            return False

    nan_pos = HedgePosition(symbol="SPX", expiry="20270115", strike=4800.0, qty=2,
                            market_value=float("nan"), avg_cost=7000.0, con_id=777, dte=120)
    monkeypatch.setattr(ibkr, "IBKRConnection", FakeConn)
    monkeypatch.setattr(ibkr, "read_hedge_book",
                        lambda ib, today, exclude_conids=(): [nan_pos])

    rc = hc.main(["--notional", "500000", "--out-dir", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "777" in err                    # names the offending conId
    assert "anti-chasing" not in err
    assert "Traceback" not in err


def test_connection_error_is_clean(monkeypatch, capsys):
    import tailhedge.hedge_cli as hc

    class BoomConnection:
        def __enter__(self):
            raise ConnectionRefusedError("refused")
        def __exit__(self, *a):
            return False

    monkeypatch.setattr("tailhedge.ibkr.IBKRConnection", BoomConnection)
    rc = hc.main(["--notional", "500000"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Cannot connect to TWS/IB Gateway" in err
    assert "Traceback" not in err
