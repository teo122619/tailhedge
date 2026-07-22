from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from tailhedge.lifecycle import (
    HEDGE_SYMBOLS, HedgePosition, available_budget, classify_hedge, cycle_budget,
    format_book, format_roll_ticket, format_run_report, next_check_date, roll_due,
)

TODAY = date(2026, 7, 21)


def _item(symbol="SPX", right="P", sec_type="OPT", position=2.0, expiry="20270115",
          strike=4800.0, con_id=1, market_value=15000.0, avg_cost=7000.0, mult="100"):
    contract = SimpleNamespace(
        secType=sec_type, right=right, symbol=symbol, strike=strike,
        lastTradeDateOrContractMonth=expiry, conId=con_id, multiplier=mult,
    )
    return SimpleNamespace(contract=contract, position=position,
                           marketValue=market_value, averageCost=avg_cost)


def test_classify_keeps_only_long_puts_on_hedge_underlyings():
    items = [
        _item(),                                   # real hedge
        _item(right="C", con_id=2),                # call (GEX): out
        _item(symbol="AAPL", con_id=3),            # unrelated underlying: out
        _item(position=-1.0, con_id=4),            # short put: out
        _item(sec_type="STK", con_id=5),           # stock: out
    ]
    book = classify_hedge(items, TODAY)
    assert len(book) == 1
    p = book[0]
    assert isinstance(p, HedgePosition)
    assert (p.symbol, p.qty, p.strike) == ("SPX", 2, 4800.0)
    assert p.dte == (date(2027, 1, 15) - TODAY).days
    assert p.multiplier == 100


def test_classify_exclusion_by_conid_and_sort_by_dte():
    items = [
        _item(expiry="20271217", con_id=10),
        _item(expiry="20261016", con_id=11),
        _item(expiry="20270115", con_id=12),
    ]
    book = classify_hedge(items, TODAY, exclude_conids=(10,))
    assert [p.con_id for p in book] == [11, 12]   # sorted by ascending DTE


def test_classify_raises_on_malformed_expiry():
    # a malformed expiry must produce a message naming the contract, not the raw
    # "time data ... does not match format" from strptime.
    item = _item(expiry="2026", con_id=555)
    with pytest.raises(ValueError, match="conId 555") as exc:
        classify_hedge([item], TODAY)
    assert "does not match" not in str(exc.value)
    assert "2026" in str(exc.value)


def test_classify_xsp_recognized_and_missing_multiplier():
    items = [_item(symbol="XSP", mult="", con_id=7)]
    book = classify_hedge(items, TODAY)
    assert book[0].symbol == "XSP" and book[0].multiplier == 100
    assert "XSP" in HEDGE_SYMBOLS


def _pos(dte, mv=1000.0, con_id=1):
    return HedgePosition(symbol="SPX", expiry="20270115", strike=4800.0, qty=1,
                         market_value=mv, avg_cost=900.0, con_id=con_id, dte=dte)


def test_roll_due_triggers_at_dte_30_inclusive():
    book = [_pos(29, con_id=1), _pos(30, con_id=2), _pos(31, con_id=3)]
    assert [p.con_id for p in roll_due(book)] == [1, 2]


def test_cycle_budget_bimonthly_target():
    # T = NAV × pct / cycles_per_year — 1%/yr on 500k, 6 cycles → ~$833
    assert cycle_budget(500_000, 0.01) == 500_000 * 0.01 / 6
    assert cycle_budget(500_000, 0.01, cycles_per_year=12) == 500_000 * 0.01 / 12


def test_available_budget_and_anti_chasing():
    target = 5_000.0
    assert available_budget(target, []) == 5_000.0
    assert available_budget(target, [_pos(90, mv=1_200.0)]) == 3_800.0
    # swollen post-crash book: MTM > target → ZERO purchases (anti-chasing)
    assert available_budget(target, [_pos(90, mv=25_000.0)]) == 0.0


def test_next_check_min_between_cycle_and_first_roll():
    today = date(2026, 7, 21)
    # no positions: next cycle (61 days)
    assert next_check_date(today, []) == today + timedelta(days=61)
    # a put reaches DTE 30 in 10 days: it wins
    assert next_check_date(today, [_pos(40)]) == today + timedelta(days=10)
    # never in the past/today: clamp to tomorrow
    assert next_check_date(today, [_pos(31)]) == today + timedelta(days=1)


def test_format_roll_ticket_sells_and_reminds_reinvestment():
    p = _pos(28, mv=4_200.0)
    out = format_roll_ticket(p)
    assert "SELL 1 × SPX 20270115 4,800 Put" in out
    assert "DTE 28" in out and "4,200" in out
    assert "reinvest" in out.lower()          # recommendation, never automatic


def test_format_book_total_vs_target():
    book = [_pos(120, mv=1_000.0, con_id=1), _pos(60, mv=500.0, con_id=2)]
    out = format_book(book, target=5_000.0)
    assert "1,500" in out and "5,000" in out   # total MTM vs cycle target
    assert out.count("Put") == 2


def test_format_run_report_sections_and_no_action():
    today = date(2026, 7, 21)
    out = format_run_report(
        today, rolls=[], keep=[_pos(120, mv=5_500.0)], target=5_000.0,
        available=0.0, buy_section="No purchase: book at/above cycle target "
        "(anti-chasing).", next_check=date(2026, 9, 20),
    )
    assert "== ACTIONS ==" in out and "== BOOK ==" in out
    assert "anti-chasing" in out
    assert "2026-09-20" in out                  # next recommended check
    assert "== DIAGNOSTICS ==" not in out       # optional section: absent if empty


def test_format_run_report_with_roll_purchase_and_diagnostics():
    today = date(2026, 7, 21)
    out = format_run_report(
        today, rolls=[_pos(25, mv=800.0, con_id=9)], keep=[],
        target=5_000.0, available=4_200.0,
        buy_section="Buy 2 × SPX 20270115 4,875 Put @ ~3.00 (mid)",
        next_check=date(2026, 9, 20),
        diagnostics="-- B-L --\nP(-10%) = 12%", sizing_note="Sizing: beta 1.0",
    )
    assert out.index("SELL") < out.index("Buy")   # rolls first, then the purchase
    assert "== DIAGNOSTICS ==" in out and "P(-10%)" in out
    assert "Sizing: beta 1.0" in out
