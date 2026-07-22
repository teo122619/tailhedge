# Strategy: why these rules

`tailhedge` is convex insurance for an equity portfolio. It buys long, deep
out-of-the-money puts that are cheap most of the time and repay heavily in a fast crash,
in the spirit of Mark Spitznagel's tail-hedging approach. The aim is not to beat the
index — over a full multi-cycle history a hedge like this tends to be roughly cost-neutral
in return terms. The aim is to cut the worst drawdowns at a contained, predictable cost.
This document explains the reasoning behind each rule the tool enforces.

## 1. Moneyness, not delta

The strike is chosen by **moneyness**: the tool looks only at puts whose strike sits in a
band **35–45% below spot** (configurable), and within that band it takes the **highest
strike the current budget can actually pay for**. If the ask on the top strike exceeds the
available budget, it slides down one strike at a time until an affordable contract is
found — an "affordability slide."

Why moneyness rather than a delta target? Delta is a model-dependent quantity that drifts
with volatility: the same "−10 delta" put is a very different distance from spot in a calm
market than in a stressed one. Fixing the *distance to spot* keeps the payoff geometry
constant across regimes and makes the position's convexity the deliberate, stable choice.
Deep-OTM strikes are where a dollar of premium buys the most convexity: their price is
dominated by the volatility and skew that explode in a crash, so they multiply hardest
exactly when the portfolio needs it. The tool still reports the real delta of the chosen
strike, but purely as information — it never drives the selection.

## 2. Expiry selection

Among the listed expiries the tool admits only those in a **90–180 day window**. This is
an *eligibility* range, not a target: long enough that the position is not dominated by
near-dated time decay, short enough that it stays responsive to a move and can be rolled
before it withers.

When several expiries quote the winning strike, the tie is broken deterministically:

1. **Tightest relative bid-ask spread** first — the most liquid, least slippage-prone
   contract.
2. Then the expiry **closest to the center of the window (135 days)**.
3. Then the **shortest** remaining expiry.

Making the tie-break explicit removes a subtle source of nondeterminism: left to the raw
data order, two runs on the same chain could pick different contracts. Anchoring on
liquidity first means the ticket you execute is the one you can actually fill cleanly.

## 3. Budget model

Spending is governed by a **per-cycle target**, not a lump sum. The annual hedging budget
is a percentage of **current NAV**; that annual figure is divided into cycles (six per
year by default, i.e. bimonthly) to give a cycle target `T = NAV × pct / cycles`. What the
tool is allowed to spend this cycle is the target **minus the current mark-to-market of
the hedge positions already on the book**.

Two consequences follow deliberately from that subtraction:

- **The budget tracks NAV.** Because the percentage is applied to *current* NAV, the hedge
  scales down as the portfolio shrinks and up as it grows — the insurance premium stays a
  constant fraction of what is being insured.
- **Anti-chasing is automatic.** Right after a crash the existing puts are worth a great
  deal, so the book's mark-to-market can meet or exceed the cycle target on its own. When
  it does, the available budget is zero and the tool **buys nothing** — it refuses to chase
  protection when it is most expensive and you are already covered. New purchases resume
  only once those inflated positions roll off. This is the opposite of the common mistake
  of loading up on puts after volatility has already spiked.

## 4. Roll discipline

A hedge put is **sold when it reaches 30 days to expiry**, and for no other reason. There
is **no profit target and no stop**. A put that has appreciated is not harvested early, and
a put that has bled is not cut — it is simply rolled at the 30-day mark.

The discipline is deliberate. Profit targets are especially destructive at these strike
depths: monetizing a deep-OTM put after a partial move throws away most of the convexity
you paid for, precisely the payoff that only fully materializes in the tail. Holding to the
roll line keeps the option's gamma alive through the window where a crash would do the most
for you. When a position is rolled, the tool's ticket recommends **reinvesting the sale
proceeds back into equity the same day**, so the roll does not quietly de-risk the book:
the hedge is refreshed and the equity exposure it protects is kept intact.

## 5. Sizing

How much to hedge is set by a **beta regression**. The tool regresses the daily returns of
your stock holdings against SPX to estimate the portfolio's β, then reports the
**SPX-equivalent notional** it carries, `β × stock value`. This β·stocks figure — not the
raw dollar value of the stocks — is the exposure the puts are sized to cover, because it
reflects how much the holdings actually move with the index a crash would hit. Beta is
reported across several look-back windows so you can see how stable it is rather than
trusting a single number, and the regression R² tells you how much of the portfolio's risk
SPX explains in the first place.

Sizing the coverage and setting the spend are two separate anchors on purpose: the
**coverage** is driven by β·stocks (the real market risk), while the **budget** is a
percentage of **total NAV** (including the bonds, alternatives and cash that dilute your
overall risk). Insuring the equity risk while spending a fraction of the whole book keeps
the premium honest relative to everything you own.

## 6. The diagnostics: three model-free lenses and Breeden–Litzenberger

Every number that enters a decision comes from observed market prices; the tool never
invents a price. To judge candidate puts it uses three **model-free lenses**, each
normalized per dollar of premium so structures are directly comparable:

- **Intrinsic-at-crash** — `max(K − S_crash, 0) / mid` at −20/−30/−40% crash scenarios.
  This is a *lower bound* on the put's crash value (the true price will be higher, thanks to
  remaining time value and the volatility spike), which makes the ranking conservative by
  construction.
- **Vega-per-premium** — `vega / mid`, how much the put appreciates per volatility point.
  This captures exactly the spike the intrinsic lens ignores.
- **Gamma-per-premium** — dollar-gamma for a 1% move divided by premium, how fast the
  convexity accelerates near the strike.

The greeks are the **real greeks from the live IBKR chain**, not values from an internal
model.

The report also infers the market's own **risk-neutral density** from the option smile via
**Breeden–Litzenberger**: fit a monotone spline to the observed implied volatilities,
reprice smoothly across strikes, and read the probability distribution off the curvature of
the put-price curve. From it the tool prints the market-implied probabilities of SPX
finishing 10/20/30% down, and the point on the smile where a dollar buys the most tail
probability.

These probabilities carry explicit caveats, and they matter:

- They are **risk-neutral**, i.e. the probabilities *priced by the market*, which embed a
  crash-risk premium — not a real-world forecast of what will happen.
- The density is computed **only where the chain is liquid**; the illiquid wings are
  excluded and **never extrapolated**, so probabilities far into the tail are simply
  reported as outside the observed range rather than guessed.
- The forward assumes a **zero dividend yield** (`q = 0`), a small drift distortion of
  about 1% on indices that pay dividends.

Together the lenses rank *what to buy* from conservative, observed payoffs, while the
Breeden–Litzenberger section tells you *how the market is pricing the tail* you are buying
into — both grounded entirely in real quotes.
