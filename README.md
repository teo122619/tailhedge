# tailhedge

A ticket-only tail-risk hedging advisor for equity portfolios: it buys long, deep
out-of-the-money puts on SPX/SPY (never VIX products), in the style of Mark
Spitznagel's convex-hedging approach.

> **Ticket-only by design: this tool reads your IBKR account and market data, prints a proposal, and never sends an order. There is no order-placement code path.**

Each run answers one question — *"what do I do today?"* — and prints the answer as a
plain-text order ticket you execute yourself in TWS: sell these puts (roll), buy this
one, or do nothing. The strategy is convex insurance, not an index-beating trade: it is
meant to pay off hard in fast crashes while costing little in calm markets. The strike is
chosen by **moneyness** (a 35–45% out-of-the-money band, the highest strike your budget
can afford), the expiry sits in a 90–180 day window, purchases run on a bimonthly cycle
budget, and positions are rolled at 30 days to expiry with no profit target. Every price
that enters a decision comes from the live IBKR option chain; nothing is a model price.

## Requirements

- An **Interactive Brokers** account.
- **TWS** or **IB Gateway** running with the **API socket enabled**
  (Configure → API → Settings → *Enable ActiveX and Socket Clients*). The tool connects
  to `127.0.0.1:7497` by default (paper TWS); see [Limitations & FAQ](#limitations--faq)
  for the `TAILHEDGE_IB_*` environment variables that point it elsewhere.
- **Market-data subscriptions** for the instruments you hedge with: US index options
  for **SPX**, and/or US equity/options for **SPY**. Without a live subscription IBKR
  serves delayed or frozen data, which is enough for spot and probabilities but not for
  streaming greeks.
- **Greeks are only available while the US market is open.** Delta, vega and the implied
  volatilities that feed the Breeden–Litzenberger density are streamed live; run the tool
  during US cash-session hours or the greek-dependent sections will be empty.
- **`Error 10091` is expected noise.** When a strike falls back to delayed data (no
  subscription for that line), `ib_async` logs `Error 10091 ... Requested market data
  requires additional subscription` on stderr. It is informational: those strikes are
  simply dropped downstream and the run completes normally.

## Install

```bash
git clone https://github.com/teo122619/tailhedge.git
cd tailhedge
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q
```

The test suite is **entirely offline**: it uses synthetic chains and fake providers, so
`pytest -q` validates the installation without a broker connection. A green run means the
package imports and the pricing, sizing and density math all work end to end.

## Quickstart

With TWS or IB Gateway running, ask the tool what to do for a $500,000 equity book at a
1%/year hedging budget, discounting the risk-neutral density at a 4% rate:

```bash
python -m tailhedge.hedge_cli --notional 500000 --budget-pct 0.01 --r 0.04
```

`--notional` skips the portfolio spreadsheet and treats the number as both the equity
notional to cover and the NAV the budget is a percentage of. A representative run prints:

```text
=== Tail-hedge — cycle run | 2026-07-21 ===

== ACTIONS ==
-- ORDER TICKET (proposal: execute manually in TWS) --
Buy 1 × SPX 20261119 4,250 Put @ ~8.00 (mid)
Total premium: $800 = 0.16% of total NAV ($500,000) | cycle budget $833
Equity notional covered: $500,000
Put goes ITM below SPX 4,250 (-43.4% from spot 7,509.10)
At SPX -20% (6,007): intrinsic $0 (0.0x premium) — covers ~0% of the equity loss at -20%
At SPX -30% (5,256): intrinsic $0 (0.0x premium) — covers ~0% of the equity loss at -30%
At SPX -40% (4,505): intrinsic $0 (0.0x premium) — covers ~0% of the equity loss at -40%
Delta -0.011 | Vega 1.28/pt per contract

== BOOK ==
Hedge book empty. Cycle target: $833.
Available cycle budget (post-roll): $833.

== DIAGNOSTICS ==
-- Risk-neutral density (Breeden–Litzenberger) — expiry 20261119 --
P(SPX < 6,758 = -10% at expiry): 10.2%
P(SPX < 6,007 = -20% at expiry): 3.8%
P(SPX < 5,256 = -30% at expiry): 1.5%
Cheapest zone of the smile (per $ of tail): strike 7,495 (premium $226.25, P(ITM) 32.2%, cost per unit of prob. $702)
Caveat: risk-neutral probabilities (as priced by the market, not a real-world estimate); 227 liquid strikes used, wings excluded, no extrapolation; forward with q=0 (SPX dividends ignored, ~1% drift distortion).

Next recommended check: 2026-09-20.

Saved to runs/20260721-210710-hedge-run.txt
```

Reading it line by line:

**ACTIONS** — the operative part, one proposed order ticket.
- `Buy 1 × SPX 20261119 4,250 Put @ ~8.00 (mid)` — buy one SPX put expiring 2026-11-19,
  strike 4,250, at roughly its mid price of $8.00. The tool sizes the order at the ask
  (what you would actually pay crossing the spread) but shows the mid.
- `Total premium: $800 = 0.16% of total NAV ($500,000) | cycle budget $833` — the whole
  order costs $800 (1 contract × $8.00 × 100 multiplier), which is 0.16% of NAV, and it
  fits inside the $833 available for this cycle.
- `Equity notional covered: $500,000` — the equity exposure this hedge is meant to cover.
- `Put goes ITM below SPX 4,250 (-43.4% from spot 7,509.10)` — the strike is 43.4% below
  the current SPX spot of 7,509.10; the put has no intrinsic value until the index falls
  that far.
- The three `At SPX -20% / -30% / -40%` lines are the intrinsic-value lens: a lower bound
  on the payoff in a crash of that size. Here they are all `$0` because a 43.4%-OTM strike
  is still out of the money even after a 40% crash — by design (see
  [Limitations & FAQ](#limitations--faq)).
- `Delta -0.011 | Vega 1.28/pt per contract` — the live greeks for the chosen strike.
  Delta is informational only; it is not the selection criterion.

**BOOK** — the hedge positions read from the account. Here it is empty, so the cycle
target is the full $833 and all of it is available to spend after any rolls.

**DIAGNOSTICS** — the Breeden–Litzenberger risk-neutral density inferred from the chosen
expiry's option smile: the market-implied probabilities of SPX finishing 10/20/30% down,
and the point on the smile where a dollar of premium buys the most tail probability. The
caveat spells out the honest limits (risk-neutral, wings excluded, no extrapolation,
dividends ignored).

**Next recommended check** — the earliest of the next bimonthly cycle and the day the
first position in the book drops to the roll trigger. Every run is also written to
`./runs/` with a timestamped filename, so nothing is overwritten.

## Using your real portfolio

Instead of `--notional`, point the tool at a portfolio spreadsheet. The **first** run
with a path that does not yet exist creates a template and stops:

```bash
python -m tailhedge.hedge_cli --portfolio port.xlsx --budget-pct 0.01 --r 0.04
# Template created at port.xlsx: fill in your stocks + total NAV, then re-run.
```

Fill it in and re-run the same command. The template asks for two things:

- **Only the stocks** used for the beta regression — US-listed symbols the tool resolves
  on IBKR as `STK/SMART/USD`. Leave out bonds, alternatives and cash; they are not part
  of the equity beta.
- The **total NAV** of the portfolio, which is the base the budget percentage applies to.

From the stock table the tool runs a β regression of your holdings against SPX and reports
the **β·stocks coverage** — the SPX-equivalent notional your equity actually carries —
alongside the cycle budget computed on the total NAV. That way the hedge is sized to the
risk your stocks contribute, while the spend is anchored to the whole book.

Portfolio files are **gitignored on purpose** (`*.xlsx` is in `.gitignore`): your holdings
never end up in version control.

## Other CLIs

The lifecycle run above (`hedge_cli`) is the primary entry point. Three smaller CLIs cover
adjacent needs:

- **`python -m tailhedge.advisor_cli`** — a one-shot advisor for a chosen set of expiries.
  It prints the three model-free lenses (intrinsic-at-crash, vega-per-premium,
  gamma-per-premium) as a comparison table, the Breeden–Litzenberger section, and a
  **delta-targeted** ticket (`--target-delta`, default -0.10) rather than a
  moneyness-selected one. Useful for exploring a specific expiry in detail.
- **`python -m tailhedge.cli`** — sizing only: the β regression of a positions file
  against SPX across several windows, from a prices CSV (`--source csv`) or from IBKR
  (`--source ibkr`). No option chain, no ticket.
- **`python -m tailhedge.snapshot_cli`** — a connection test: fetches SPX spot, the VIX
  term structure and a slice of the OTM put chain, or lists the available expiries with
  `--list-expiries`. Run it first to confirm TWS/Gateway is reachable and your
  subscriptions are live.

## Limitations & FAQ

- **A 35–45% OTM put only pays beyond a ~40% crash — this is by design.** The Quickstart
  ticket shows all three crash scenarios at `$0` intrinsic: a strike 43.4% below spot is
  still out of the money after a 40% drop. This is the Spitznagel profile on purpose. The
  edge is convexity, not intrinsic value: in a real crash the position is repriced by both
  the falling spot **and** the volatility spike, so a put that looks dead in the crash-lens
  table can still multiply in price well before it goes in the money. Deep-OTM strikes buy
  the most convexity per dollar; shallower strikes would cost far more for the same budget.
- **IBKR caps concurrent market-data lines (~100 by default).** The full-smile
  Breeden–Litzenberger section streams greeks across every liquid strike of an expiry,
  which can exceed that cap on a wide SPX chain. Strikes that do not receive data in time
  are dropped and the density is computed from the rest; a `Data error` or a shorter
  liquid strike count is the visible symptom. Raising your market-data line allowance, or
  narrowing the band, mitigates it.
- **SPX vs SPY granularity.** The advisor trades **SPX or SPY only**, and
  `--force-underlying` accepts exactly those two values. SPX has a 100 multiplier on a
  ~7,500 index level, so a single contract is expensive; a small cycle budget may not
  afford even one SPX contract. The tool auto-selects SPX first and falls back to SPY (a
  ~1/10-sized, American-style, dividend-paying underlying) when SPX cannot buy a contract,
  giving finer budget granularity. XSP — the 1/10 cash-settled mini on the same index — is
  worth knowing for context: it stays in the SPX family and carries a per-contract
  notional close to SPY's (both are roughly a tenth of SPX), and any XSP puts already held
  in your account **are recognized as part of the hedge book**. The advisor does not,
  however, propose XSP purchases: it will only ever ticket an SPX or SPY put.
- **`TAILHEDGE_IB_*` environment variables** override the connection defaults:
  - `TAILHEDGE_IB_HOST` — API host (default `127.0.0.1`).
  - `TAILHEDGE_IB_PORT` — API port (default `7497`, paper TWS; live TWS is `7496`, and
    IB Gateway uses `4002` paper / `4001` live).
  - `TAILHEDGE_IB_CLIENT_ID` — API client id (default `11`); change it if another client
    already holds that id.
  - `TAILHEDGE_IB_MKT_DATA_TYPE` — market-data type (default `4`, delayed-frozen:
    real-time where subscribed, delayed otherwise, last close when the market is closed).
- **`Error 10091` on stderr is not a failure.** It flags a line that fell back to delayed
  data for lack of a subscription. The affected strikes are ignored and the run finishes;
  it is noise, not an error you need to act on.

## Disclaimer

This software is for educational and informational purposes only. It is not financial
advice. Options trading involves substantial risk of loss. You are solely responsible for
any trade you execute. The authors accept no liability for losses arising from the use of
this tool.

Released under the [MIT License](LICENSE).
