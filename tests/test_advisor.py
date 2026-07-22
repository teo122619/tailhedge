from datetime import date

import pandas as pd
import pytest

from tailhedge.advisor import (
    dte_days, premium_budget, candidate_table,
    select_candidate, contracts_for_budget, build_ticket, format_ticket,
    budget_ladder, trigger_pcts, format_budget_comparison,
    ticket_for, choose_instrument
)


def test_dte_days():
    assert dte_days("20261218", date(2026, 7, 14)) == 157
    assert dte_days("20260714", date(2026, 7, 14)) == 0


def test_premium_budget_scales_with_dte():
    assert premium_budget(0.01, 5_000_000, 365) == pytest.approx(50_000.0)
    # half horizon -> half budget (v1: horizon = the put's full life)
    assert premium_budget(0.01, 5_000_000, 182) == pytest.approx(0.01 * 5e6 * 182 / 365)


def _chain_row(expiry, strike, iv, delta, vega, gamma, mid_price, bid=None, ask=None):
    return {
        "expiry": expiry, "strike": float(strike), "right": "P", "iv": iv, "delta": delta,
        "vega": vega, "gamma": gamma, "mid": mid_price,
        "bid": bid if bid is not None else mid_price * 0.98,
        "ask": ask if ask is not None else mid_price * 1.02,
        "moneyness": None,  # recomputable, not used by the advisor
    }


def _chain(rows):
    return pd.DataFrame(rows)


def test_candidate_table_three_lenses():
    chain = _chain([_chain_row("20261218", 4500, 0.25, -0.10, 8.0, 0.0004, 100.0)])
    t = candidate_table(chain, spot=5000.0, today=date(2026, 7, 14))
    row = t.iloc[0]
    assert row["dte"] == 157
    # lens 1: intrinsic at the crash / premium (lower bound)
    assert row["intr20"] == pytest.approx(5.0)    # max(4500-4000,0)/100
    assert row["intr30"] == pytest.approx(10.0)   # max(4500-3500,0)/100
    assert row["intr40"] == pytest.approx(15.0)   # max(4500-3000,0)/100
    # lens 2: vega/premium
    assert row["vega_per_prem"] == pytest.approx(0.08)
    # lens 3: dollar-gamma for a 1% move / premium = 0.5*gamma*(0.01*spot)^2/mid
    assert row["gamma_per_prem"] == pytest.approx(0.5 * 0.0004 * 50.0**2 / 100.0)


def test_candidate_table_drops_unpriced_rows():
    chain = _chain([
        _chain_row("20261218", 4500, 0.25, -0.10, 8.0, 0.0004, 100.0),
        _chain_row("20261218", 4400, 0.26, float("nan"), 7.0, 0.0003, 90.0),  # delta nan
        _chain_row("20261218", 4300, 0.27, -0.08, 6.0, 0.0003, float("nan")),  # mid nan
    ])
    t = candidate_table(chain, spot=5000.0, today=date(2026, 7, 14))
    assert list(t["strike"]) == [4500.0]


def test_select_candidate_closest_real_delta():
    chain = _chain([
        _chain_row("20261218", 4300, 0.27, -0.05, 5.0, 0.0002, 60.0),
        _chain_row("20261218", 4500, 0.25, -0.098, 8.0, 0.0004, 100.0),
        _chain_row("20261218", 4700, 0.23, -0.15, 11.0, 0.0006, 160.0),
        _chain_row("20270618", 4200, 0.26, -0.10, 12.0, 0.0003, 150.0),  # different expiry
    ])
    t = candidate_table(chain, spot=5000.0, today=date(2026, 7, 14))
    row = select_candidate(t, "20261218", target_delta=-0.10)
    assert row["strike"] == 4500.0  # -0.098 is the closest to -0.10 WITHIN the chosen expiry
    with pytest.raises(ValueError, match="20280101"):
        select_candidate(t, "20280101", target_delta=-0.10)


def test_contracts_for_budget_floor():
    assert contracts_for_budget(23_700.0, 118.5) == 2
    assert contracts_for_budget(23_000.0, 118.5) == 1
    assert contracts_for_budget(100.0, 118.5) == 0
    assert contracts_for_budget(-5_000.0, 118.5) == 0  # negative budget: never short


def test_ticket_matches_spec_example():
    # example: spot 7540, strike 6200, mid 118.5, 2 contracts, notional 5M
    chain = _chain([_chain_row("20261218", 6200, 0.24, -0.098, 9.2, 0.0003, 118.5)])
    t = candidate_table(chain, spot=7540.0, today=date(2026, 7, 14))
    ticket = build_ticket(t.iloc[0], budget=23_700.0, notional=5_000_000.0, spot=7540.0)
    out = format_ticket(ticket)
    assert "Buy 2 × SPX 20261218 6,200 Put" in out
    assert "23,700" in out                    # total premium
    assert "33,600" in out and "1.4x" in out  # intrinsic at -20%: (6200-6032)*100*2
    assert "184,400" in out and "7.8x" in out # intrinsic at -30%: (6200-5278)*100*2
    assert "execute manually" in out          # ticket-only


def test_ticket_budget_label_overrides_legacy_row():
    # in the cycle, the budget is the residual of the bimonthly target,
    # not a budget "over N days" — budget_label replaces that portion of the line.
    chain = _chain([_chain_row("20261218", 6200, 0.24, -0.098, 9.2, 0.0003, 118.5)])
    t = candidate_table(chain, spot=7540.0, today=date(2026, 7, 14))
    ticket = build_ticket(t.iloc[0], budget=23_700.0, notional=5_000_000.0, spot=7540.0)

    out_custom = format_ticket(ticket, budget_label="cycle budget $23,700")
    assert "cycle budget $23,700" in out_custom
    assert "over 157 days" not in out_custom

    out_legacy = format_ticket(ticket)  # without kwarg: legacy path unchanged
    assert "over 157 days" in out_legacy


def test_ticket_zero_contracts_warns():
    chain = _chain([_chain_row("20261218", 6200, 0.24, -0.098, 9.2, 0.0003, 118.5)])
    t = candidate_table(chain, spot=7540.0, today=date(2026, 7, 14))
    ticket = build_ticket(t.iloc[0], budget=100.0, notional=50_000.0, spot=7540.0)
    out = format_ticket(ticket)
    assert ticket.contracts == 0
    assert "insufficient" in out and "XSP" in out


def test_ticket_decouples_coverage_from_nav_total():
    # equity coverage 600k, total NAV 1M: budget/premium is read on the total NAV,
    # the drawdown coverage on the equity notional
    row = pd.Series({"expiry": "20270115", "strike": 6300.0, "mid": 60.0,
                     "delta": -0.10, "vega": 9.0, "dte": 184})
    ticket = build_ticket(row, budget=6_000.0, notional=600_000.0, spot=7500.0,
                          nav_total=1_000_000.0)
    assert ticket.contracts == 1                      # 6000 // (60*100)
    assert ticket.nav_total == 1_000_000.0
    out = format_ticket(ticket)
    assert "0.60% of total NAV" in out                # premium 6000 / 1,000,000
    assert "Equity notional covered: $600,000" in out
    # at -20% (spot->6000): intrinsic (6300-6000)*100 = 30,000; equity loss 600k*0.20=120k
    assert "covers ~25% of the equity loss" in out


# --- Budget comparison (opt-in feature --budget-pcts) ---

def _smoke_row():
    """The put from the live smoke test on 2026-07-14: SPX 20270114 6150, mid 71.05, 184 days."""
    return pd.Series({"expiry": "20270114", "strike": 6150.0, "mid": 71.05,
                      "delta": -0.100, "vega": 12.4, "dte": 184})


def test_budget_ladder_contracts_and_unused_budget():
    # base budget = nav_total * dte/365 = 2M * 184/365 = 1,008,219.18 per pct point
    lad = budget_ladder(_smoke_row(), pcts=[0.005, 0.0075, 0.01, 0.015],
                        nav_total=2_000_000.0, notional=1_200_000.0, spot=7525.0)
    assert list(lad["pct"]) == [0.005, 0.0075, 0.01, 0.015]
    assert list(lad["contracts"]) == [0, 1, 1, 2]  # premium/ctr = 71.05*100 = 7,105
    assert lad["budget"].tolist() == pytest.approx(
        [5_041.10, 7_561.64, 10_082.19, 15_123.29], abs=0.01)
    assert lad["premium"].tolist() == pytest.approx([0.0, 7_105.0, 7_105.0, 14_210.0])
    # the heart of the feature: how much authorized budget does NOT buy coverage
    assert lad["unused"].tolist() == pytest.approx(
        [5_041.10, 456.64, 2_977.19, 913.29], abs=0.01)


def test_budget_ladder_coverage_is_flat_between_triggers():
    """0.75% and 1.00% buy the same contract: same coverage, no more."""
    lad = budget_ladder(_smoke_row(), pcts=[0.0075, 0.01],
                        nav_total=2_000_000.0, notional=1_200_000.0, spot=7525.0)
    assert lad["cover30"].iloc[0] == pytest.approx(lad["cover30"].iloc[1])
    # intrinsic at -30% (spot->5267.5): (6150-5267.5)*100*1 = 88,250
    # equity loss: 1.2M * 0.30 = 360,000 -> covers 24.5%
    assert lad["cover30"].iloc[0] == pytest.approx(88_250.0 / 360_000.0, rel=1e-9)


def test_trigger_pcts_are_the_exact_thresholds_that_buy_a_contract():
    """Defining property: at the trigger pct the budget buys EXACTLY k contracts,
    an epsilon below it buys k-1. Verified via contracts_for_budget, not by
    re-deriving the formula."""
    row = _smoke_row()
    trig = trigger_pcts(mid=float(row["mid"]), nav_total=2_000_000.0,
                        dte=int(row["dte"]), n=3)
    assert len(trig) == 3
    for k, pct in enumerate(trig, start=1):
        at = premium_budget(pct, 2_000_000.0, int(row["dte"]))
        assert contracts_for_budget(at, float(row["mid"])) == k
        just_below = premium_budget(pct * (1 - 1e-9), 2_000_000.0, int(row["dte"]))
        assert contracts_for_budget(just_below, float(row["mid"])) == k - 1


def test_format_budget_comparison_table_and_triggers():
    out = format_budget_comparison(_smoke_row(), pcts=[0.005, 0.0075, 0.01, 0.015],
                                   nav_total=2_000_000.0, notional=1_200_000.0,
                                   spot=7525.0)
    assert "BUDGET COMPARISON" in out
    assert "6,150" in out                      # the strike the levels are compared on
    # 0.50%: budget 5,041 < premium 7,105 -> no coverage
    assert "insufficient" in out
    # 1.00%: buys 1 contract and leaves $2,977 unspent
    assert "7,105" in out and "2,977" in out
    # the trigger points are exact multiples of the first: stating it in closed
    # form instead of listing them avoids 23-entry rows when the premium per
    # contract is small
    assert "TRIGGER POINTS" in out
    assert "0.70%" in out and "every additional 0.70%" in out


def test_format_budget_comparison_states_the_flat_stretch():
    """The feature's operational message must be explicit, not inferred from the numbers."""
    out = format_budget_comparison(_smoke_row(), pcts=[0.0075, 0.01],
                                   nav_total=2_000_000.0, notional=1_200_000.0,
                                   spot=7525.0)
    assert "coverage does NOT change" in out


def test_waste_diagnosis_ignores_levels_that_buy_nothing():
    """A level below threshold has 'insufficient' budget, not 'wasted': including it
    would push the diagnosis to 100% every time a pct doesn't reach a contract."""
    out = format_budget_comparison(_smoke_row(), pcts=[0.005, 0.0075, 0.01, 0.0125],
                                   nav_total=2_000_000.0, notional=1_200_000.0,
                                   spot=7525.0)
    # real max waste = 5,498/12,603 = 44% (the 1.25% row), not the 100% of the 0.50% row
    assert "up to 44%" in out


def test_no_affordable_level_still_reports_triggers():
    """If no level buys a contract the measured waste is zero, but saying
    'coverage scales almost linearly' would be the exact opposite of the truth."""
    out = format_budget_comparison(_smoke_row(), pcts=[0.001, 0.002],
                                   nav_total=2_000_000.0, notional=1_200_000.0,
                                   spot=7525.0)
    assert "almost linearly" not in out
    assert "TRIGGER POINTS" in out
    # and not 'unspent budget (up to 100%)' either: it's not waste, it's that none
    # of these levels reach the first contract. That's what must be said.
    assert "100%" not in out
    assert "None of these levels" in out


def test_format_budget_comparison_when_granularity_is_negligible():
    """Premium small relative to the budget (SPY, or SPX with a large NAV): coverage
    scales almost linearly and the unspent budget is marginal. Saying 'granularity
    dominates' would be false in exactly the opposite regime from the one this note
    is written for."""
    row = pd.Series({"expiry": "20261218", "strike": 3750.0, "mid": 2.82,
                     "delta": -0.10, "vega": 8.0, "dte": 157})
    out = format_budget_comparison(row, [0.005, 0.01, 0.015], nav_total=1_000_000.0,
                                   notional=600_000.0, spot=5000.0)
    assert "dominates" not in out
    assert "almost linearly" in out


def test_trigger_pcts_scale_inversely_with_nav():
    """A doubled NAV halves the pct needed to buy the first contract."""
    small = trigger_pcts(mid=71.05, nav_total=1_000_000.0, dte=184, n=1)[0]
    big = trigger_pcts(mid=71.05, nav_total=2_000_000.0, dte=184, n=1)[0]
    assert big == pytest.approx(small / 2)


def test_instrument_registry_defines_spx_and_spy():
    from tailhedge.advisor import SPX, SPY, INSTRUMENTS
    assert SPX.symbol == "SPX" and SPX.trading_class == "SPX"
    assert SPX.multiplier == 100 and SPX.style == "european" and SPX.pays_dividends is False
    assert SPY.symbol == "SPY" and SPY.trading_class == "SPY"
    assert SPY.multiplier == 100 and SPY.style == "american" and SPY.pays_dividends is True
    assert INSTRUMENTS == {"SPX": SPX, "SPY": SPY}


def test_build_ticket_carries_symbol():
    row = pd.Series({"expiry": "20270115", "strike": 630.0, "mid": 6.0,
                     "delta": -0.10, "vega": 0.9, "dte": 184})
    t = build_ticket(row, budget=6_000.0, notional=600_000.0, spot=750.0, symbol="SPY")
    assert t.symbol == "SPY"
    # default retro-compat: without symbol it stays SPX
    t2 = build_ticket(row, budget=6_000.0, notional=600_000.0, spot=750.0)
    assert t2.symbol == "SPX"


def test_format_ticket_uses_instrument_symbol():
    row = pd.Series({"expiry": "20270115", "strike": 630.0, "mid": 6.0,
                     "delta": -0.10, "vega": 0.9, "dte": 184})
    t = build_ticket(row, budget=6_000.0, notional=600_000.0, spot=750.0, symbol="SPY")
    out = format_ticket(t)
    assert "× SPY 20270115" in out
    assert "Put goes ITM below SPY" in out
    assert "At SPY -20%" in out
    assert "SPX" not in out            # no hard-coded leftovers


def test_format_ticket_zero_contracts_names_symbol():
    row = pd.Series({"expiry": "20270115", "strike": 630.0, "mid": 6.0,
                     "delta": -0.10, "vega": 0.9, "dte": 184})
    t = build_ticket(row, budget=100.0, notional=600_000.0, spot=750.0, symbol="SPY")
    out = format_ticket(t)
    assert t.contracts == 0
    assert "1 SPY contract" in out


def test_format_ticket_zero_contracts_spy_does_not_suggest_itself():
    # on SPY, the zero-contracts ticket must not suggest SPY as an alternative to itself
    row = pd.Series({"expiry": "20270115", "strike": 630.0, "mid": 6.0,
                     "delta": -0.10, "vega": 0.9, "dte": 184})
    t = build_ticket(row, budget=100.0, notional=600_000.0, spot=750.0, symbol="SPY")
    out = format_ticket(t)
    assert "XSP/SPY" not in out
    assert "shorter expiry" in out


def test_budget_comparison_header_names_symbol():
    row = pd.Series({"expiry": "20270114", "strike": 630.0, "mid": 7.1,
                     "delta": -0.100, "vega": 1.2, "dte": 184})
    out = format_budget_comparison(row, [0.005, 0.0075, 0.01],
                                   nav_total=200_000.0, notional=120_000.0,
                                   spot=752.5, symbol="SPY")
    assert "BUDGET COMPARISON — SPY 20270114" in out


def test_choose_instrument_falls_back_to_spy_when_spx_unaffordable():
    from tailhedge.advisor import choose_instrument
    assert choose_instrument(0) == "SPY"
    assert choose_instrument(1) == "SPX"
    assert choose_instrument(5) == "SPX"


def test_ticket_for_composes_selection_and_sizing():
    from tailhedge.advisor import ticket_for
    chain = _chain([
        _chain_row("20270114", 6300, 0.24, -0.098, 9.2, 0.0003, 71.05),
        _chain_row("20270114", 6100, 0.26, -0.140, 9.8, 0.0004, 95.0),
    ])
    t = ticket_for(chain, spot=7525.0, today=date(2026, 7, 14), expiry="20270114",
                   target_delta=-0.10, budget=10_000.0, notional=2_000_000.0)
    assert t.strike == 6300.0        # delta -0.098 is the closest to -0.10
    assert t.contracts == 1          # 10_000 // (71.05*100)
    assert t.symbol == "SPX"


# Name distinct from _chain_row (helper already defined above, with a different
# signature): in Python module-level names resolve at call time, so a same-named
# _chain_row below would silently overwrite the global binding and break all the
# preceding tests that call it.
def _otm_row(expiry, strike, bid, ask, dte_unused=None, iv=0.30, delta=-0.05,
             vega=1.0, gamma=0.001, spot=7500.0):
    m = (bid + ask) / 2
    return {"expiry": expiry, "strike": strike, "right": "P", "iv": iv,
            "delta": delta, "vega": vega, "gamma": gamma, "mid": m,
            "bid": bid, "ask": ask, "moneyness": strike / spot}


def _cycle_chain(spot=7500.0):
    """35–45% OTM band on spot 7500 → allowed strikes [4125, 4875]."""
    rows = [
        # ~120-day expiry: dense grid, top strike 4875 (35% OTM)
        _otm_row("20261118", 4875.0, 2.8, 3.2),
        _otm_row("20261118", 4700.0, 2.0, 2.4),
        # ~176-day expiry: also quotes 4875, wider spread
        _otm_row("20270113", 4875.0, 3.5, 4.5),
        # outside the band above (34% OTM) and below (46% OTM): never selectable
        _otm_row("20261118", 4950.0, 3.0, 3.4),
        _otm_row("20261118", 4050.0, 1.0, 1.4),
        # expiry outside the DTE window (~250 days)
        _otm_row("20270328", 4875.0, 4.0, 4.6),
        # illiquid: bid at zero
        _otm_row("20261118", 4800.0, 0.0, 2.0),
    ]
    return pd.DataFrame(rows)


def test_select_by_moneyness_picks_max_strike_in_band():
    from tailhedge.advisor import select_by_moneyness

    today = date(2026, 7, 21)
    row = select_by_moneyness(_cycle_chain(), 7500.0, today, budget=5_000.0)
    assert row["strike"] == 4875.0
    # between 20261118 (spread ~13%) and 20270113 (spread ~25%) the tighter spread wins
    assert row["expiry"] == "20261118"
    assert 90 <= row["dte"] <= 180


def test_select_by_moneyness_rejects_crossed_quotes():
    from tailhedge.advisor import select_by_moneyness

    today = date(2026, 7, 21)
    # crossed quote: bid 11 > ask 9 (mid 10). Sizing on the ask ($900) fits the
    # $950 budget, but the ticket accounts at the mid ($1,000) → would break the
    # budget invariant. A crossed quote must be rejected as illiquid.
    chain = pd.DataFrame([_otm_row("20261118", 4800.0, 11.0, 9.0)])
    with pytest.raises(ValueError, match="[Nn]o liquid puts"):
        select_by_moneyness(chain, 7500.0, today, budget=950.0)


def test_select_by_moneyness_band_edge_inclusive():
    from tailhedge.advisor import select_by_moneyness

    today = date(2026, 7, 21)
    # spot 6000, band 0.35–0.45: the lower edge 3300 = exactly 45% OTM must be
    # eligible (the documented band is inclusive), despite the float error in
    # 6000*(1-0.45) = 3300.0000000000005.
    chain = pd.DataFrame([_otm_row("20261118", 3300.0, 2.8, 3.2, spot=6000.0)])
    row = select_by_moneyness(chain, 6000.0, today, budget=5_000.0)
    assert row["strike"] == 3300.0


def test_select_by_moneyness_affordability_slide():
    from tailhedge.advisor import select_by_moneyness

    today = date(2026, 7, 21)
    # budget $250: ask 3.2 -> $320 doesn't fit; slides to 4700 (ask 2.4 -> $240)
    row = select_by_moneyness(_cycle_chain(), 7500.0, today, budget=250.0)
    assert row["strike"] == 4700.0


def test_select_by_moneyness_budget_below_minimum_names_threshold():
    from tailhedge.advisor import select_by_moneyness

    today = date(2026, 7, 21)
    with pytest.raises(ValueError, match="240"):
        select_by_moneyness(_cycle_chain(), 7500.0, today, budget=100.0)


def test_select_by_moneyness_empty_band():
    from tailhedge.advisor import select_by_moneyness

    today = date(2026, 7, 21)
    with pytest.raises(ValueError, match="[Nn]o liquid puts"):
        select_by_moneyness(_cycle_chain(), 7500.0, today, budget=5_000.0,
                            band=(0.60, 0.70))


def test_choose_expiry_tie_break_spread_then_center_then_shortest():
    from tailhedge.advisor import choose_expiry

    rows = pd.DataFrame([
        # same strike; spread_rel: 0.10, 0.10, 0.20 — the 3rd loses immediately
        {"expiry": "20261120", "strike": 4875.0, "bid": 1.90, "ask": 2.10,
         "mid": 2.0, "dte": 122, "delta": -0.05, "vega": 1.0, "iv": 0.3,
         "gamma": 0.001, "right": "P", "moneyness": 0.65},
        {"expiry": "20270108", "strike": 4875.0, "bid": 1.90, "ask": 2.10,
         "mid": 2.0, "dte": 171, "delta": -0.05, "vega": 1.0, "iv": 0.3,
         "gamma": 0.001, "right": "P", "moneyness": 0.65},
        {"expiry": "20261002", "strike": 4875.0, "bid": 1.80, "ask": 2.20,
         "mid": 2.0, "dte": 100, "delta": -0.05, "vega": 1.0, "iv": 0.3,
         "gamma": 0.001, "right": "P", "moneyness": 0.65},
    ])
    # equal spread: |122-135|=13 < |171-135|=36 → 20261120 wins
    assert choose_expiry(rows)["expiry"] == "20261120"
    # equal distance from center too: the shortest wins
    rows2 = rows.copy()
    rows2.loc[0, "dte"] = 130   # |130-135| = 5
    rows2.loc[1, "dte"] = 140   # |140-135| = 5
    assert choose_expiry(rows2)["expiry"] == "20261120"


def test_choose_expiry_excludes_crossed_quotes():
    from tailhedge.advisor import choose_expiry

    rows = pd.DataFrame([
        # crossed quote (bid > ask): its negative spread_rel would sort FIRST and
        # win selection — it must be excluded before the sort.
        {"expiry": "20261120", "strike": 4875.0, "bid": 2.20, "ask": 1.80,
         "mid": 2.0, "dte": 122, "delta": -0.05, "vega": 1.0, "iv": 0.3,
         "gamma": 0.001, "right": "P", "moneyness": 0.65},
        {"expiry": "20270108", "strike": 4875.0, "bid": 1.90, "ask": 2.10,
         "mid": 2.0, "dte": 171, "delta": -0.05, "vega": 1.0, "iv": 0.3,
         "gamma": 0.001, "right": "P", "moneyness": 0.65},
    ])
    assert choose_expiry(rows)["expiry"] == "20270108"


def test_build_ticket_sizing_allask():
    from tailhedge.advisor import build_ticket

    row = pd.Series({"expiry": "20261118", "strike": 4875.0, "mid": 3.0,
                     "bid": 2.8, "ask": 3.2, "delta": -0.05, "vega": 1.0,
                     "dte": 120})
    # with a budget of $960: at mid (3.0 -> $300) would buy 3; at ask (3.2 -> $320) also 3;
    # with $950: mid -> 3, ask -> 2. Sizing at the ask guarantees executability.
    t_mid = build_ticket(row, 950.0, 500_000.0, 7500.0)
    t_ask = build_ticket(row, 950.0, 500_000.0, 7500.0, sizing_price=3.2)
    assert t_mid.contracts == 3
    assert t_ask.contracts == 2


def test_format_ticket_greeks_unavailable():
    from tailhedge.advisor import Ticket, format_ticket

    t = Ticket(expiry="20261118", strike=4875.0, mid=3.0, contracts=2,
               premium_total=600.0, budget=700.0, notional=500_000.0,
               spot=7500.0, delta=float("nan"), vega=float("nan"), dte=120,
               nav_total=600_000.0, symbol="SPX")
    out = format_ticket(t)
    assert "n/a" in out and "nan" not in out
