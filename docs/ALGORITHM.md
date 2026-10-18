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
