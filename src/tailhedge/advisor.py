"""Core strategy layer: instrument model, put selection, sizing, and reporting.

Defines the SPX/SPY `Instrument` model, the three model-free comparison
lenses (intrinsic-at-crash, vega-per-premium, gamma-per-premium), both
selection modes — delta-targeted (`select_candidate`) and Spitznagel-style
moneyness-band selection with affordability slide (`select_by_moneyness`) —
and the `Ticket`/budget-ladder machinery that turns a chosen put into an
order-ticket text. Sits between the data layer (`ibkr.py`, `portfolio.py`,
`sizing.py`) and the CLIs (`advisor_cli.py`, `hedge_cli.py`), which call into
here to build their reports; `density.py` supplies the B-L diagnostics
appended to those reports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd


def dte_days(expiry: str, today: date) -> int:
    """Calendar days to expiry, IBKR format 'YYYYMMDD'."""
    exp = datetime.strptime(expiry, "%Y%m%d").date()
    return (exp - today).days


def premium_budget(annual_pct: float, notional: float, dte: int) -> float:
    """Premium budget in $ over the put's horizon (v1: the put's full life)."""
    return annual_pct * notional * (dte / 365.0)


DEFAULT_CRASHES = (-0.20, -0.30, -0.40)

# Below this fraction of unspent budget, contract granularity doesn't bite:
# coverage scales effectively linearly with the budget (SPY regime / large NAV).
_NEGLIGIBLE_WASTE = 0.10


@dataclass(frozen=True)
class Instrument:
    """Hedge instrument. The single place where SPX vs SPY knowledge lives."""
    symbol: str          # used for IBKR fetches and report labels
    trading_class: str   # passed to fetch_put_chain
    multiplier: int
    style: str           # "european" | "american"
    pays_dividends: bool
    note: str            # short label shown in the ticket


SPX = Instrument("SPX", "SPX", 100, "european", False, "cash-settled, European")
SPY = Instrument("SPY", "SPY", 100, "american", True, "American, pays dividends")
INSTRUMENTS = {"SPX": SPX, "SPY": SPY}


def candidate_table(
    chain: pd.DataFrame, spot: float, today: date, crashes: tuple = DEFAULT_CRASHES
) -> pd.DataFrame:
    """Comparison table: 3 model-free lenses for each quoted put.

    - intrNN = max(K - spot*(1+c), 0) / mid — intrinsic value at the crash per $
      of premium: a lower bound on the put's value in the crash → conservative
      ranking by construction.
    - vega_per_prem = vega / mid — appreciation per vol point per $ of premium.
    - gamma_per_prem = dollar-gamma for a 1% move / mid = 0.5*gamma*(0.01*spot)^2 / mid.
    Real greeks from the IBKR chain; rows without mid or delta are dropped.
    """
    df = chain[(chain["mid"] > 0) & chain["delta"].notna()].reset_index(drop=True).copy()
    df["dte"] = [dte_days(e, today) for e in df["expiry"]]
    for c in crashes:
        s_crash = spot * (1 + c)
        df[f"intr{abs(int(round(c * 100)))}"] = (df["strike"] - s_crash).clip(lower=0.0) / df["mid"]
    df["vega_per_prem"] = df["vega"] / df["mid"]
    df["gamma_per_prem"] = 0.5 * df["gamma"] * (0.01 * spot) ** 2 / df["mid"]
    return df


def select_candidate(table: pd.DataFrame, expiry: str, target_delta: float) -> pd.Series:
    """The put in the chosen expiry with the REAL delta closest to the target."""
    sl = table[table["expiry"] == expiry]
    if sl.empty:
        raise ValueError(f"No candidates for expiry {expiry}.")
    return sl.loc[(sl["delta"] - target_delta).abs().idxmin()]


def contracts_for_budget(budget: float, mid_price: float, multiplier: int = 100) -> int:
    """Whole contracts affordable with the budget (floor; 0 if the premium exceeds the budget)."""
    if mid_price <= 0:
        return 0
    return max(0, int(budget // (mid_price * multiplier)))


@dataclass(frozen=True)
class Ticket:
    expiry: str
    strike: float
    mid: float
    contracts: int
    premium_total: float
    budget: float
    notional: float          # equity notional covered (β·portfolio)
    spot: float
    delta: float
    vega: float
    dte: int
    multiplier: int = 100
    nav_total: float = 0.0    # budget base (total NAV); default = notional in build_ticket
    symbol: str = "SPX"   # instrument (default SPX: backward-compat with all existing tests)


def build_ticket(
    row: pd.Series, budget: float, notional: float, spot: float,
    multiplier: int = 100, nav_total: float | None = None, symbol: str = "SPX",
    sizing_price: float | None = None,
) -> Ticket:
    # sizing_price (e.g. the ask): quantity respects the price you'd actually
    # pay crossing the spread; the mid stays the price shown in the ticket.
    px = float(row["mid"]) if sizing_price is None else sizing_price
    n = contracts_for_budget(budget, px, multiplier)
    nv = notional if nav_total is None else nav_total
    return Ticket(
        expiry=str(row["expiry"]), strike=float(row["strike"]), mid=float(row["mid"]),
        contracts=n, premium_total=n * float(row["mid"]) * multiplier,
        budget=budget, notional=notional, spot=spot,
        delta=float(row["delta"]), vega=float(row["vega"]), dte=int(row["dte"]),
        multiplier=multiplier, nav_total=nv, symbol=symbol,
    )


def ticket_for(
    chain: pd.DataFrame, spot: float, today: date, expiry: str, target_delta: float,
    budget: float, notional: float, multiplier: int = 100,
    nav_total: float | None = None, symbol: str = "SPX",
) -> Ticket:
    """Combines selection + sizing for an already-chosen chain.

    Used by the SPX pre-check in main() to read its .contracts; run_advisor
    composes selection and sizing on its own. Propagates select_candidate's
    ValueError if the expiry has no candidates.
    """
    table = candidate_table(chain, spot, today)
    row = select_candidate(table, expiry, target_delta)
    return build_ticket(row, budget, notional, spot, multiplier,
                        nav_total=nav_total, symbol=symbol)


def choose_instrument(spx_contracts: int) -> str:
    """Rule: use SPX as long as it buys ≥1 contract, otherwise fall back to SPY."""
    return "SPX" if spx_contracts >= 1 else "SPY"


def budget_ladder(
    row: pd.Series, pcts: list[float], nav_total: float, notional: float, spot: float,
    multiplier: int = 100, crashes: tuple = DEFAULT_CRASHES,
) -> pd.DataFrame:
    """Compares several budget levels on the SAME strike (--budget-pcts feature).

    At a fixed strike the premium per contract is constant, so coverage is a
    STEP-LINEAR function of the budget: floor(budget / premium). Between two
    trigger points, raising the pct buys nothing — the `unused` column measures
    exactly the authorized budget that stays unspent. Reuses build_ticket: no
    synthetic price.
    """
    recs = []
    for pct in pcts:
        budget = premium_budget(pct, nav_total, int(row["dte"]))
        t = build_ticket(row, budget, notional, spot, multiplier, nav_total=nav_total)
        rec = {
            "pct": pct, "budget": budget, "contracts": t.contracts,
            "premium": t.premium_total, "unused": budget - t.premium_total,
        }
        for c in crashes:
            intr = t.contracts * max(t.strike - spot * (1 + c), 0.0) * multiplier
            key = abs(int(round(c * 100)))
            rec[f"mult{key}"] = intr / t.premium_total if t.premium_total > 0 else 0.0
            rec[f"cover{key}"] = intr / (notional * abs(c)) if notional > 0 else float("nan")
        recs.append(rec)
    return pd.DataFrame(recs)


def trigger_pcts(
    mid: float, nav_total: float, dte: int, n: int = 2, multiplier: int = 100,
) -> list[float]:
    """The only budget pcts where coverage actually changes: the ones that buy
    the k-th contract (k = 1..n). Between one trigger point and the next, the
    extra budget stays unspent. Invert premium_budget: pct_k = k*premium / (nav*dte/365)."""
    if mid <= 0 or nav_total <= 0 or dte <= 0:
        return []
    base = nav_total * (dte / 365.0)
    out = []
    for k in range(1, n + 1):
        pct = k * mid * multiplier / base
        # the round-trip pct -> premium_budget -> floor must give exactly k:
        # absorbs representation error by stepping up to the next float
        for _ in range(4):
            if contracts_for_budget(premium_budget(pct, nav_total, dte), mid, multiplier) >= k:
                break
            pct = math.nextafter(pct, math.inf)
        out.append(pct)
    return out


def format_budget_comparison(
    row: pd.Series, pcts: list[float], nav_total: float, notional: float, spot: float,
    multiplier: int = 100, crashes: tuple = DEFAULT_CRASHES, symbol: str = "SPX",
) -> str:
    """Comparison table of budget levels on the selected strike."""
    lad = budget_ladder(row, pcts, nav_total, notional, spot, multiplier, crashes)
    keys = [abs(int(round(c * 100))) for c in crashes]
    deep = keys[-2] if len(keys) >= 2 else keys[-1]   # reference crash for the coverage column

    head = (f"-- BUDGET COMPARISON — {symbol} {row['expiry']} {float(row['strike']):,.0f} "
            f"Put @ ~{float(row['mid']):,.2f} --")
    cols = (f"{'pct':>7} {'budget $':>10} {'ctr':>4} {'premium $':>10} {'unused $':>11}"
            + "".join(f"{f'−{k}%':>7}" for k in keys) + f"{f'covers@−{deep}%':>13}")
    lines = [head, "", cols]
    for _, r in lad.iterrows():
        cells = f"{r['pct']:>6.2%} {r['budget']:>10,.0f} {int(r['contracts']):>4}"
        if r["contracts"] == 0:
            lines.append(cells + f"{'—':>11}{r['unused']:>12,.0f}"
                         + " " * (7 * len(keys)) + f"{'insufficient':>13}")
            continue
        cells += f" {r['premium']:>10,.0f} {r['unused']:>11,.0f}"
        cells += "".join(f"{r[f'mult{k}']:>6,.1f}x" for k in keys)
        cover = r[f"cover{deep}"]
        cells += f"{'n/a' if pd.isna(cover) else f'~{cover:.0%}':>13}"
        lines.append(cells)

    # How much granularity bites: authorized budget that stays unspent, at worst.
    # Only among levels that actually buy something: below the 1st contract the
    # budget is insufficient, not wasted, and would push the diagnosis to 100%
    # by definition.
    spent = lad[(lad["contracts"] >= 1) & (lad["budget"] > 0)]
    waste = (spent["unused"] / spent["budget"]).max() if not spent.empty else None
    per_ctr = float(row["mid"]) * multiplier
    lines.append("")
    if waste is None:
        # no level reaches the 1st contract: there is no waste to measure,
        # there is a minimum threshold to reach. That's the only useful information.
        first = trigger_pcts(per_ctr / multiplier, nav_total, int(row["dte"]), 1, multiplier)
        need = f"{first[0]:.2%}" if first else "n/a"
        lines += [
            f"None of these levels buys a contract: below {need} the budget",
            f"does not reach the premium (~${per_ctr:,.0f}/ctr). You need at least "
            f"{need}, a shorter expiry,",
            "or a more granular underlying (XSP/SPY).",
            f"TRIGGER POINTS: {need} buys the 1st contract, then every additional {need}.",
        ]
    elif waste < _NEGLIGIBLE_WASTE:
        lines += [
            f"Negligible granularity (~${per_ctr:,.0f}/ctr): coverage scales",
            f"almost linearly with the budget, at most {waste:.0%} goes unspent.",
        ]
    else:
        # the trigger points are exact multiples of the first (pct_k = k*pct_1): closed form
        first = trigger_pcts(per_ctr / multiplier, nav_total, int(row["dte"]), 1, multiplier)
        step = f"{first[0]:.2%}" if first else "n/a"
        lines += [
            f"TRIGGER POINTS: {step} buys the 1st contract, then every additional {step}.",
            "Between two trigger points coverage does NOT change: at a fixed strike the",
            "premium per contract is constant, so extra budget = unspent budget",
            f"(here up to {waste:.0%}). Granularity (~${per_ctr:,.0f}/ctr) dominates "
            "the pct choice.",
        ]
    return "\n".join(lines)


def format_ticket(
    t: Ticket, crashes: tuple = DEFAULT_CRASHES, budget_label: str | None = None,
) -> str:
    """Text order ticket. Ticket-only: it proposes, never executes.

    budget_label: in the cycle, the budget is the residual of the bimonthly
    target, not a budget "over N days" — pass a label to replace that
    portion. Default None: line unchanged (byte-identical backward-compat).
    """
    lines = ["-- ORDER TICKET (proposal: execute manually in TWS) --"]
    if t.contracts == 0:
        need = t.mid * t.multiplier
        # on SPY, "a more granular underlying (XSP/SPY)" would suggest SPY as
        # an alternative to itself: the granular hint only applies to the
        # other leg (SPX).
        hint = ("a shorter expiry" if t.symbol == "SPY"
                else "a more granular underlying (XSP/SPY) or a shorter expiry")
        lines.append(
            f"⚠ Budget ${t.budget:,.0f} insufficient for 1 {t.symbol} contract "
            f"(needs ~${need:,.0f}): consider {hint}."
        )
        return "\n".join(lines)
    budget_part = (f"budget ${t.budget:,.0f} over {t.dte} days" if budget_label is None
                   else budget_label)
    lines += [
        f"Buy {t.contracts} × {t.symbol} {t.expiry} {t.strike:,.0f} Put @ ~{t.mid:,.2f} (mid)",
        f"Total premium: ${t.premium_total:,.0f} = {t.premium_total / t.nav_total:.2%} of "
        f"total NAV (${t.nav_total:,.0f}) | {budget_part}",
        f"Equity notional covered: ${t.notional:,.0f}",
        f"Put goes ITM below {t.symbol} {t.strike:,.0f} ({t.strike / t.spot - 1:+.1%} from spot {t.spot:,.2f})",
    ]
    for c in crashes:
        s_crash = t.spot * (1 + c)
        intr = t.contracts * max(t.strike - s_crash, 0.0) * t.multiplier
        ratio = intr / t.premium_total
        line = (f"At {t.symbol} {c:+.0%} ({s_crash:,.0f}): intrinsic ${intr:,.0f} "
                f"({ratio:,.1f}x premium)")
        if t.notional > 0:
            cover = intr / (t.notional * abs(c))
            line += f" — covers ~{cover:.0%} of the equity loss at {c:+.0%}"
        lines.append(line)
    if math.isnan(t.delta) or math.isnan(t.vega):
        lines.append("Delta n/a | Vega n/a (greeks unavailable for this strike)")
    else:
        lines.append(f"Delta {t.delta:+.3f} | Vega {t.vega:,.2f}/pt per contract")
    return "\n".join(lines)


def choose_expiry(rows: pd.DataFrame, center_dte: int = 135) -> pd.Series:
    """Among expiries quoting the same strike: tightest relative spread first,
    then |DTE − band center|, then the shortest (deterministic tie-break —
    the original lambdaclass engine let data order win by default)."""
    r = rows[rows["bid"] <= rows["ask"]].copy()   # drop crossed quotes (negative spread sorts first)
    r["spread_rel"] = (r["ask"] - r["bid"]) / r["mid"]
    r["_dist"] = (r["dte"] - center_dte).abs()
    r = r.sort_values(["spread_rel", "_dist", "dte"], kind="mergesort")
    return r.iloc[0].drop("_dist")


def select_by_moneyness(
    chain: pd.DataFrame, spot: float, today: date, budget: float,
    band: tuple[float, float] = (0.35, 0.45),
    dte_range: tuple[int, int] = (90, 180),
    multiplier: int = 100, max_spread_pct: float = 0.25,
) -> pd.Series:
    """Spitznagel-style selection: the highest strike in the OTM band that the
    budget can afford, among expiries in the DTE window. Delta is not a criterion.

    Filter order: band+DTE → liquidity (real bid/ask, spread within bounds;
    IV is NOT required — only the B-L density needs it) → affordability
    (ask×mult ≤ budget, the lambdaclass engine's "slide") → max strike →
    choose_expiry.
    """
    df = chain.copy()
    df["dte"] = [dte_days(e, today) for e in df["expiry"]]
    lo_k, hi_k = spot * (1 - band[1]), spot * (1 - band[0])
    eps = 1e-9 * spot   # the band is inclusive: absorb float error at the edges
    elig = df[
        (df["dte"] >= dte_range[0]) & (df["dte"] <= dte_range[1])
        & (df["strike"] >= lo_k - eps) & (df["strike"] <= hi_k + eps)
        & (df["bid"] > 0) & (df["ask"] > 0) & (df["mid"] > 0)
        & (df["bid"] <= df["ask"])   # reject crossed quotes: mid <= ask keeps premium_total <= budget
    ]
    elig = elig[(elig["ask"] - elig["bid"]) / elig["mid"] <= max_spread_pct]
    if elig.empty:
        raise ValueError(
            f"No liquid puts in the {band[0]:.0%}–{band[1]:.0%} OTM band "
            f"with DTE {dte_range[0]}–{dte_range[1]}."
        )
    afford = elig[elig["ask"] * multiplier <= budget]
    if afford.empty:
        cheapest = float((elig["ask"] * multiplier).min())
        raise ValueError(
            f"Cycle budget ${budget:,.0f} below the cheapest contract "
            f"in the band (~${cheapest:,.0f}): skip this cycle or raise the pct."
        )
    top = afford[afford["strike"] == afford["strike"].max()]
    return choose_expiry(top, center_dte=(dte_range[0] + dte_range[1]) // 2)
