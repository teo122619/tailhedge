"""Bimonthly cycle model: hedge-book bookkeeping, roll and budget rules.

Classifies the IBKR account's long SPX/SPY/XSP puts into a `HedgePosition`
book, decides which ones are due to roll (DTE <= 30), computes the cycle
budget (NAV x pct / cycles-per-year) net of what the surviving book still
carries — the anti-chasing rule — and formats the resulting "what to do
today" run report. Sits between `ibkr.py` (which supplies the raw account
book) and `hedge_cli.py`, which drives the daily/bimonthly cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from tailhedge.advisor import dte_days

# Only long PUTS on these underlyings are classified as hedge; any call-based
# strategy on the same account is excluded by construction (right == "P").
HEDGE_SYMBOLS = ("SPX", "SPY", "XSP")


@dataclass(frozen=True)
class HedgePosition:
    """A hedge put read from the IBKR account (read-only). market_value = the IBKR
    mark for the whole position (qty included), used for the target model and rolls.
    avg_cost = IBKR averageCost: per contract and ALREADY × multiplier, so
    avg_cost * qty is the total premium paid for the position."""
    symbol: str
    expiry: str
    strike: float
    qty: int
    market_value: float
    avg_cost: float
    con_id: int
    dte: int
    multiplier: int = 100


def classify_hedge(items, today: date, symbols=HEDGE_SYMBOLS,
                   exclude_conids=()) -> list[HedgePosition]:
    """Filters the IBKR portfolio: long puts on SPX/SPY/XSP are classified as hedge.

    items: ib_async PortfolioItem (or a fake with the same shape).
    exclude_conids: manual override for ambiguous positions.
    """
    out = []
    for it in items:
        c = it.contract
        if getattr(c, "secType", "") != "OPT" or getattr(c, "right", "") != "P":
            continue
        if it.position <= 0 or c.symbol not in symbols or c.conId in exclude_conids:
            continue
        raw_mult = str(getattr(c, "multiplier", "") or "").strip()
        expiry = c.lastTradeDateOrContractMonth
        try:
            dte = dte_days(expiry, today)
        except ValueError:
            raise ValueError(
                f"Malformed expiry '{expiry}' on {c.symbol} (conId {int(c.conId)}): "
                "expected IBKR format YYYYMMDD."
            )
        out.append(HedgePosition(
            symbol=c.symbol, expiry=expiry,
            strike=float(c.strike), qty=int(it.position),
            market_value=float(it.marketValue), avg_cost=float(it.averageCost),
            con_id=int(c.conId), dte=dte,
            multiplier=int(raw_mult) if raw_mult else 100,
        ))
    return sorted(out, key=lambda p: (p.dte, p.symbol, p.strike))


def roll_due(book: list[HedgePosition], roll_dte: int = 30) -> list[HedgePosition]:
    """The puts to sell TODAY: DTE <= roll_dte. No profit target, no stop."""
    return [p for p in book if p.dte <= roll_dte]


def cycle_budget(nav_total: float, annual_pct: float, cycles_per_year: int = 6) -> float:
    """Cycle target: T = NAV × pct / cycles_per_year. Current NAV, not the initial one."""
    return nav_total * annual_pct / cycles_per_year


def available_budget(target: float, keep_book) -> float:
    """Spendable budget = target − MTM of the book that remains (target model).

    Includes anti-chasing: after a crash the swollen book exceeds the target
    and the cycle does NOT buy until positions roll off.
    """
    return max(0.0, target - sum(p.market_value for p in keep_book))


def next_check_date(today: date, keep_book, roll_dte: int = 30,
                    cycle_days: int = 61) -> date:
    """Next recommended check: min between the next cycle (~2 months)
    and the day the first put in the book drops to roll_dte. Never before tomorrow."""
    cands = [today + timedelta(days=cycle_days)]
    cands += [today + timedelta(days=p.dte - roll_dte)
              for p in keep_book if p.dte > roll_dte]
    return max(min(cands), today + timedelta(days=1))


def format_roll_ticket(p: HedgePosition) -> str:
    """Sell ticket (roll at DTE <= 30). Ticket-only: proposes, does not execute.
    The estimated proceeds are the IBKR mark of the position (no new fetch)."""
    return (
        f"SELL {p.qty} × {p.symbol} {p.expiry} {p.strike:,.0f} Put — DTE {p.dte} (roll)\n"
        f"  Estimated proceeds ~${p.market_value:,.0f} (IBKR mark).\n"
        f"  Cycle rule: reinvest the proceeds in equity the same day."
    )


def format_book(book: list[HedgePosition], target: float) -> str:
    """The hedge book read from the account, with total MTM vs the cycle target."""
    if not book:
        return f"Hedge book empty. Cycle target: ${target:,.0f}."
    lines = [
        f"{p.qty} × {p.symbol} {p.expiry} {p.strike:,.0f} Put | DTE {p.dte} | "
        f"MTM ${p.market_value:,.0f} (cost ${p.avg_cost * p.qty:,.0f})"
        for p in book
    ]
    tot = sum(p.market_value for p in book)
    lines.append(f"Total MTM ${tot:,.0f} vs cycle target ${target:,.0f}.")
    return "\n".join(lines)


def format_run_report(
    today: date, rolls: list[HedgePosition], keep: list[HedgePosition],
    target: float, available: float, buy_section: str, next_check: date,
    diagnostics: str = "", sizing_note: str = "",
) -> str:
    """The "what do I do today" report: ACTIONS, BOOK, optional DIAGNOSTICS."""
    parts = [f"=== Tail-hedge — cycle run | {today.isoformat()} ==="]
    if sizing_note:
        parts.append(sizing_note)
    parts.append("\n== ACTIONS ==")
    for p in rolls:
        parts.append(format_roll_ticket(p))
    parts.append(buy_section)
    parts.append("\n== BOOK ==")
    parts.append(format_book(rolls + keep, target))
    parts.append(f"Available cycle budget (post-roll): ${available:,.0f}.")
    if diagnostics:
        parts.append("\n== DIAGNOSTICS ==")
        parts.append(diagnostics)
    parts.append(f"\nNext recommended check: {next_check.isoformat()}.")
    return "\n".join(parts)
