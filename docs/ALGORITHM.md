# How the brain decides (plain language)

Think of the brain as a **careful detective**. It never guesses. It waits until many independent clues point the
same way — and most of the time it just watches.

```
Live data → 1. Look → 2. Check 7 clues → 3. Wait for the "go" moment → 4. Safety checks
          → 5. Pick the option + set SL/targets → 6. Watch it & score it
```

## 1. Look (4 times every second)
Nifty and Sensex: the index, the nearest future, and options around the current price (ATM ± 10 strikes, 2 expiries).
Builds **1-minute candles** from live index prices.

## 2. Check 7 clues (1 point each)
For **UP** (the **DOWN** side asks the mirror questions):

| # | Clue | Simple meaning | Exact check |
|---|---|---|---|
| 1 | VWAP | Price above today's "fair price"? | futures LTP > futures ATP (exchange VWAP) |
| 2 | Aggression | Buyers pushing hard? | 5-min CVD > 0 **and** 5-min futures price change > 0 |
| 3 | Futures OI | New players joining the move? | futures price up in 5 min (long build-up if OI ↑, short covering if OI ↓) |
| 4 | Big players | Elephants buying? | net of block prints (≥ 20 lots and top 1 % of that contract's update sizes) over 5 min is bullish (futures buys, CE buys, PE sells) |
| 5 | Order book | More buyers waiting? | top-5 imbalance > +0.15 **or** resting buy/sell imbalance > +0.10 |
| 6 | Option writers | Big sellers betting on up? | put OI added − call OI added near ATM (± 2 strikes) over 15 min > 0 |
| 7 | Volatility | Option "air" not leaking? | ATM straddle change over 5 min ≥ 0 |

Needs **≥ 5 of 7** (`TBRAIN_SIGNAL_MIN_SCORE`).

## 3. The "go" moment (trigger)
A completed 1-minute candle **closes above the highest high of the previous 10 candles** (below the lowest low for DOWN).

## 4. Safety gates (any failure = no call)
- Entry window **09:25–14:45 IST**; feed fresher than **3 s**; at least **15** completed candles
- **10-min cooldown** per index; only **one open call** per index
- No **OI wall** (largest call-OI strike for UP / put-OI strike for DOWN) between price and T1
- Option liquidity: spread ≤ **2 %**, ≥ **2 lots** at the best offer, ≥ 45 min to expiry
- **Net R:R ≥ 1.5** to T1 after estimated costs (`TBRAIN_COST_PER_LOT`)

## 5. Pick the option and set the lines
- **Option:** ATM **CE** (UP) / **PE** (DOWN), nearest expiry; on expiry day after 13:30 the next expiry.
- **Stop (index):** lowest low of the last 5 candles − 0.1 × ATR (highest high + 0.1 ATR for DOWN); never closer than
  0.8 × ATR, never wider than 3 × ATR (then no call).
- **Targets (index):** T1 = entry ± **2 ×** risk, T2 = ± **3 ×** risk.
- **Premium levels:** each index level → option price with Black-76 (current IV, futures basis, minus 15 min of decay).
- **Entry:** the current **ask** (pessimistic), zone = mid … ask + 0.5 %.

## 6. Watch and score
- Index reaches **T1** → stop moves to **breakeven** (entry level).
- Then **T2**, **SL/breakeven**, or **30-min time stop** (never later than 15:20) closes the call.
- P&L per lot = (option **bid** − entry) × lot − estimated costs. R = P&L ÷ risk per lot.
- Every event is appended to `data/signals/calls.jsonl` → scoreboard (calls, T1 hit-rate, average R, by outcome).

## How it learns (next phases)
Each call stores which clues were true. After 20+ sessions we measure, per clue and per score level, what actually
won after costs; keep what works, drop what doesn't, then train regime/volatility models on the recorded parquet and
replace the rule score with **calibrated probabilities**.

## What it can't know
Exchange data is **anonymous**: we see *what* traded (size, side, price, time), never *who*. Upstox's standard feed
shows 5 depth levels and snapshot-style updates, so "big prints" are per update and may combine several trades.
No rule set knows the future; the goal is small losses, bigger wins, and honest scoring.

## v2 changes (2026-10-19, from the first live day)
First live day: 17 calls, +3.68R. Bearish calls (with the falling market) +8.86R; all 5 bullish (counter-trend) calls lost (−5.18R);
7/7 scores did worse than 5/7 (clues were correlated); SL exits averaged 5.8 premium points worse than planned.
- **MUST: VWAP** — futures on the call's side of VWAP (was just 1 of 7 points).
- **MUST: trend** — the last 60 one-minute candles moved ≥ 1.5 × ATR in the call's direction. No counter-trend calls.
- **Futures OI clue now needs fresh positions** — OI rising with price in our direction (long/short build-up).
  Short covering / long unwinding no longer score.
- **Premium stop** — the call also stops when the option's bid *and* LTP fall to the SL premium (index stop still applies).
  After T1 the premium stop moves to the entry price.
- **Correlation warning** — a same-direction call on the other index is flagged (one bet, double risk), not blocked.
- **Loss pause** — optional (`TBRAIN_LOSS_PAUSE_N`), off: it would have cost money in the replay.
Replay of the 17 day-1 calls: trend + VWAP keep 12 calls, 7 wins, +8.86R (vs +3.68R). One day only — validate over more sessions.

## v2.1 defaults (2026-10-19, full end-to-end replay)
| Setup | Calls | W/L | R | ₹/lot |
|---|---|---|---|---|
| v1 replay (15/17 live calls matched) | 16 | 7/9 | +2.96 | +561 |
| **v1 + trend + VWAP (= v2.1)** | 12 | 7/5 | **+6.35** | **+3,913** |
| v2 without premium stop | 11 | 6/5 | +3.53 | +2,778 |
| v2 (all new filters) | 12 | 5/7 | +1.09 | +375 |
| v2 + regime gate | 3 | 2/1 | +1.06 | +808 |
- Kept: **trend + VWAP** MUST filters.
- Off by default: **premium stop** (−2.4R, stopped out on option noise), **fresh futures OI** (−2.8R), **regime gate** (too strict, labels need recalibration).
- Unchanged for now: cooldown and one-open-per-index (their shadows would have made +10R / +8R, but one trend day isn't enough to loosen them).
- The filter-only estimate (+8.86R) was optimistic: blocking a call changes which later calls fire. One day only — rerun `tbrain backtest` on every new session.

## v2.2 session rules (2026-10-24, from 4 live days: 19, 21, 22, 23 Oct — 55 calls)
| Time of entry | Calls | Wins | Sum R |
|---|---|---|---|
| before 12:00 | 27 | 16 | +24.70 |
| 12:00-13:00 | 8 | 3 | +0.87 |
| 13:00-14:45 | 20 | 4 | **−11.83** (negative on every day that traded after 13:00) |

Rules tested on the same 55 calls (exact: they only remove later calls; decisions use only already-closed calls):
| Rule | Total R | ₹/lot | Notes |
|---|---|---|---|
| v2.1 (live) | +13.74 | +11,772 | |
| **no new calls after 13:00** | **+25.57** | **+22,986** | better on 19/21/23 Oct, never worse |
| drawdown stop 2R peak / 2R give-back | +21.48 | +19,900 | better 19 & 23 Oct; 3R/3R was worse on 19 Oct (sensitive) |
| skip 2nd index same direction | +8.00 | +5,092 | **rejected** — the 2nd index usually confirms trend moves |
- **Afternoon cutoff 13:00** — default on. Dashboard switch *Afternoon calls* turns calls back on until 14:45.
- **Drawdown stop** — today's closed real calls (exit order) reach a ≥ +2R peak then give back ≥ 2R → no new calls today.
  Dashboard switch *Drawdown stop*.
- Both are **gates that create shadows**, so the scorecard keeps measuring them ("afternoon cutoff", "drawdown stop").
  Review after ~10 more trading days: if blocked afternoon shadows are net positive, switch the cutoff off.
- These are discipline rules, not better predictions. In-sample (same 4 days the pattern came from): expect less than +11.8R.
- Switches live in `data/live/settings.json` (local) or the Modal Dict (hosted); the brain re-reads them every 15 s.
  Only the owner account can change them; viewers see them read-only.

## Shadow calls (the learning loop)
Every candidate that reaches the trigger with enough clues but is **blocked** (trend/VWAP filter, cooldown, open call,
OI wall, R:R, spread, depth, regime gate, loss pause) is built exactly like a real call and tracked silently to its
outcome (`data/signals/shadow.jsonl`). The **filter scorecard** (UI + `tbrain review`) sums what each blocker's
candidates would have made: negative = the filter **saved** money, positive = it **cost** money.

## Regime brain (day type)
From completed 1-min candles: **efficiency** over 60 min (net move ÷ path length), **VWAP crossings** in 60 min, and the
position vs the **initial balance** (09:15–10:15 range). `trend` if efficiency ≥ 0.25 and ≤ 2 crossings; `range` if
efficiency < 0.15 or ≥ 4 crossings; else `mixed`. Logged on every call and shadow; the optional regime gate allows
breakouts only in `trend`.
**Range-fade playbook (shadow-only):** in a `range`, a candle that tags the 60-min extreme and closes ≥ 0.5 ATR back
inside → fade toward VWAP (SL beyond the extreme + 0.3 ATR). It never issues live calls until the evidence supports it.

## Backtesting
`tbrain backtest --date D --compare` replays the recorded ticks + chain snapshots through the same brain and engine on
simulated time (snapshot every 1 s; live runs every 0.25 s). The v1 preset is checked against the live session
(fidelity = share of live calls reproduced within 2 minutes) before any v2 conclusion is trusted.
