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
This is a raw rule count, **not a probability or seven independent confirmations**. Several clues can reflect the same price move (for example VWAP, aggression and futures OI); a high score must not be read as calibrated confidence. The score remains experimental until it is measured against a larger out-of-sample record.

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
- Once the call has been open ≥ `direction_check_min_hold_s` (120s) AND is up ≥ `direction_check_min_r` (0.3R) -
  not just "technically positive" - two direction checks run alongside the stops (optional, `t1_direction_check`,
  off by default; either failing closes the call now and locks the current profit as **EARLY_EXIT**):
  - **Fast** (every tick): CVD AND futures price over the last `fast_check_window_s` (60s) must BOTH have moved a
    real amount against us (price past `direction_check_fast_min_move` points, not just < 0), confirmed on
    `direction_check_fast_confirms` (2) consecutive ticks - options move every tick, so a single bad tick is
    normal noise inside a healthy trend, not a reversal.
  - **Slow** (every `direction_check_interval_min`, 5 min): still on our side of VWAP, still trending ≥ `trend_k` ×
    ATR, AND at least 1 of the last 2 completed candles still with us - catches a sustained reversal the fast
    layer's short window can miss.
- Index reaches **T1** → the real broker stop moves to the actual average fill price. Signal tracking also enforces an option-premium stop at entry after T1; if the underlying stop is reached first while the option is below entry, the result is recorded as `T1_STOP` and its actual marked P&L is retained.
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

## Afternoon cutoff moved to 12:00 (2026-10-31, from 9 live days: 19-30 Oct, 111 calls)
The ~10-day review above confirmed and sharpened the original finding: the losing window starts an hour earlier
than first thought.
| Hour of entry | Calls | Wins | Pnl/lot |
|---|---|---|---|
| 09:00 | 10 | 4 | +15,297 |
| 10:00 | 25 | 9 | −127 |
| 11:00 | 27 | 19 | **+21,030** (best hour) |
| 12:00 | 19 | 5 | **−4,870** |
| 13:00 | 18 | 7 | −4,446 |
| 14:00 | 12 | 2 | **−7,924** (worst hour) |
12:00 onward cost −17,240 pnl/lot across the 9 days; 09:00-11:59 alone made +36,200. `afternoon_cutoff` moved
from 13:00 to 12:00 to match. `allow_afternoon` stays off by default (dashboard switch, same as before).
Also note by regime across the same 9 days: `trend` averaged +1.01R/call, `range` averaged −0.09R/call —
`regime_gate` recalibration is now backed by real volume and worth revisiting (it was rejected on 1 day in the
v2.1 note above for being "too strict"; 9 days of range-regime underperformance is a different kind of evidence).
- Switches live in `data/live/settings.json` (local) or the Modal Dict (hosted); the brain re-reads them every 15 s.
  Only the owner account can change them; viewers see them read-only.

## Periodic direction check (2026-11-03, live-tested by accident on 03 Nov — see below)
Idea: the engine rides every open call blind until SL/breakeven/T1/T2/time-stop, even once it's sitting on real
profit. Options move every tick, so a single slow (1-min-candle) check isn't enough - a block-print reversal can
give back the profit before a candle even closes. Added an optional two-speed re-check
(`t1_direction_check`, dashboard switch, default **off**):
- **Fast** layer, every tick: CVD AND futures price delta over `fast_check_window_s` (60s) must BOTH have moved
  against us by a real amount, confirmed on `direction_check_fast_confirms` (2) consecutive ticks - the same idea
  as the entry "aggression" clue, but noise-filtered so a single wobbling tick can't trigger it.
- **Slow** layer, every `direction_check_interval_min` (5 min): the same MUST filters as entry (VWAP side, 60-min
  trend ≥ `trend_k` × ATR) plus at least 1 of the last 2 completed candles still with us.
Neither layer runs at all until the call has been open ≥ `direction_check_min_hold_s` (120s) AND is up ≥
`direction_check_min_r` (0.3R) - "up by ₹1" is not a signal, it's noise. Either layer failing (once past that
gate) closes the call now and locks the current profit (**EARLY_EXIT**). Both checks always run and are recorded
on the call (`t1_check`) even while the switch is off, so `tbrain backtest`/the scorecard can tell us whether it
would have helped before we ever turn it on live.

**2026-11-03 incident**: the dashboard switch was manually turned on right after this shipped (along with
`allow_afternoon` and `dd_stop` also being flipped off-default) - all three reset the next morning. With the
switch on and NONE of the noise filters above in place yet (first cut: `pnl_per_lot > 0`, fast layer a bare sign
check, no min-hold, no confirms), 12 of 14 live calls exited as EARLY_EXIT, most within 1-2 minutes of entry for
₹2-₹200 - the check was firing on ordinary tick-to-tick noise inside otherwise-healthy trends, not real reversals.
Day finished +951 pnl/lot (12W/2L) but with an average win near noise level, instead of the T1/T2-sized wins the
filters it was riding past would normally produce. The four guards above (min-hold, min-R, fast-layer magnitude,
fast-layer confirms) were added in response, before any further live or backtest use.

## Volatility regime filter (2026-11-05, from a Mongo segmentation of 155 live calls across 19 Oct-4 Nov)
Idea: segmenting live calls by hour of day showed 10:xx-11:xx NIFTY/SENSEX calls underperforming badly next to
11:xx (payoff ratio 1.13 vs 1.86) - not because the 10:xx win rate was much worse, but because the average SL
distance was ~47% wider (34.9 vs 23.7 pts), so a loss cost more. Clock time was a proxy; the real driver is
intraday ATR (the engine's existing 14-bar true range average) running hot relative to its own day's baseline.
Rather than block an hour outright (loses good trades on calm days in that hour), added `vol_regime_filter`
(dashboard switch, default **off**): when current ATR >= `vol_spike_k` (1.3x) the day's trailing median true
range (all bars so far, excluding the most recent 5 so a live spike can't inflate its own baseline):
- the breakout SL floor tightens from `0.8 x ATR` to `vol_spike_sl_floor_mult` (0.6x) - smaller loss if stopped,
- the entry score bar rises by `vol_spike_min_score_bonus` (1 clue) - needs stronger confluence while it's expensive to be wrong.
`vol_ratio`/`vol_spiking`/`min_score_needed` are always computed and exposed in diagnostics, same shadow-first
pattern as every other filter here, so real evidence accumulates before the switch is ever flipped on.

**First design was wrong, caught by backtest**: the original baseline compared current ATR to the day's first
30 minutes, assuming volatility always decays after the open. Replaying 2026-10-19's real tick data showed that
assumption is false on that day - the open WAS the day's highest-ATR stretch, so the ratio never exceeded 1.0
and the filter was dead code in practice. Switched to the trailing-median design above; a trace confirmed it
then correctly flagged a real spike (SENSEX, 19 Oct 14:57-14:58, ratio up to 1.36).

**Backtest coverage gap (known, as of this writing)**: only 2026-10-19 and 2026-10-23 have the full tick+chain+
instrument-master cache needed to replay (21 and 22 Oct are missing their instrument-master snapshot, which
can't be reconstructed after the fact - Upstox only serves the CURRENT master, not a historical one). On both
usable days, the one real spike the filter caught didn't overlap a live entry, so there is no historical day
yet proving this filter changes a trade's outcome - only that it's inert and safe when no spike occurs during
a call. Shipped off-by-default specifically because of this gap; do not turn it on live without more shadow
evidence or until a backtestable day shows it actually helping.

## Regime-confirmed breakout gate (2026-11-05, from a 12-day/155-call Mongo segmentation)
Same segmentation that found the volatility pattern above also split by `regime.label`: "range" regime
averaged -29.4 pnl/call (43.8% win) vs "trend" +646.9 pnl/call (50% win) - 22x better per trade, over 73 vs
28 calls respectively. `regime_gate` (dashboard switch, default **off**) already existed in the code from the
v2.1 era but was rejected after 1 day's sample (7 calls) as "too strict" - that call is now outdated. Re-tested
with a stricter gate than the original: breakouts only fire once the market has read "trend" for
`regime_confirm_bars` (3) CONSECUTIVE 1-min bars, not a single reading - ADX/ER-style regime labels are known to
lag and flicker right at the trend/range boundary, so a single noisy reading shouldn't open or close the gate.

**Backtest on the 2 usable days (19, 23 Oct) is genuinely mixed, not a clean win**: on 19 Oct the 3-bar
confirmation delayed the day's only trend-regime entry long enough to miss it entirely (0 calls, vs 1 call/
+1,398 with a single-reading gate). On 23 Oct it performed close to the single-reading version (5 calls, 80%
win, +9,492 vs 4 calls, 100% win, +9,214). With 0-5 trades per variant this is far below any believable signal
- shipped off-by-default specifically so real evidence (ideally 30+ trades, the commonly cited floor before
trusting a backtest result) accumulates before this is ever turned on live.

## Real per-trade transaction costs (2026-11-05)
`cost_per_lot_inr` was a flat ₹40 placeholder (2 x ₹20 brokerage) from the very first version, with its own
comment admitting statutory charges (STT etc.) were NOT included. STT on options was hiked to 0.125-0.15% of
premium (charged on the SELL leg) - at a typical ATM premium (~₹150-200) this alone is ~₹15-25/lot, and once
exchange transaction charges, SEBI fees, stamp duty and GST on brokerage are added, real round-trip cost at
that premium comes to roughly ₹65-75/lot - nearly double the old flat number. Since `min_rr_net` (the entry
gate) is computed directly from this cost, understating it meant the gate was quietly more permissive than
intended; some trades that looked like they cleared 1.5R after "costs" might not have, after real costs.
Replaced the flat constant with `real_cost_per_lot(entry_px, exit_px, lot)`: STT (sell leg), exchange txn
charges + SEBI fee (both legs), stamp duty (buy leg), brokerage (both legs), GST on brokerage+exchange - all on
actual premium turnover, not notional. `cost_per_lot_inr` stays as an optional override (e.g. for a different
broker's flat-fee plan) but defaults to `None`, which uses the real formula. Backtested on both usable days
(19, 23 Oct): the real cost model didn't block any of the same calls that fired under the old flat number on
either day - the shift was a modest, honest reduction in reported pnl (19 Oct: +5,502 -> +5,350; 23 Oct:
+16,306 -> +15,963), not a different set of trades. Confirms the entries weren't on the margin of the R:R gate
on these 2 days, but the live/backtest numbers going forward are now accurate rather than quietly optimistic.

## Event calendar gate (2026-11-06)
This engine only ever BUYS premium (CE/PE) - it never sells. On a scheduled high-volatility event (RBI MPC
decision, Union Budget, US FOMC reaction morning), implied volatility typically runs up pre-announcement and
crushes after; an option buyer can be right on direction and still lose money to that IV crush. `event_calendar_gate`
(dashboard switch, default **off**) blocks NEW calls in a window around each known date:
- RBI MPC: 30 min before to 60 min after the ~10:00 IST announcement.
- Union Budget: same window around the ~11:00 IST presentation.
- US FOMC: the entire Indian trading day's entry window the morning after (the overnight move is already
  priced in by the Indian open, not something a same-day blackout window would catch).
Dates are hardcoded in `src/tbrain/signals/event_calendar.py` from each body's own official announcement -
there is no free machine-readable calendar API for any of these (confirmed: RBI's own "Advance Release
Calendar" covers SDDS data releases, not MPC dates). This needs an annual refresh once the next cycle's dates
are announced; a stale list silently blocks the wrong day or fails to block the right one.
Explicitly NOT built: a live news-sentiment feed. Research found the predictive effect is real in academic
literature but narrow (concentrated in the first hour after specific announcement types, decays within a
minute) and real institutional use runs on low-latency structured feeds (Bloomberg/Refinitiv) costing
thousands/month - infrastructure this system doesn't have. A weak retail version would add false confidence,
not real edge, so it was deliberately not built.
Backtested on the 2 usable days (19, 23 Oct, neither an event day): exact pnl match to the baseline on both
(+5,350 / +15,963) - confirms the gate is a true no-op outside a blackout window, as intended. Its real value
can only be measured on an actual event day, which isn't in the current backtestable tick data.

## 17-day review and the Conservative profile rebuilt (2026-11-12, 223 deduped live calls, 19 Oct-12 Nov)
With the three profiles running side by side since 6 Nov, the first honest full read:
- **Baseline edge is ~zero**: +43 pnl/lot per call, 8 up days vs 9 down days, worst day -6,779. After real slippage
  that is break-even. No filter combination tested makes the DAY-level result consistent - the best raise the
  per-call average to +140..+240 but still leave roughly half the days negative. Anyone reading this as "profit
  every day" is reading it wrong.
- **Conservative as first shipped issued 0 calls in 4 days**: regime gate (3-bar confirm) + vol filter blocked every
  candidate ("market is 'range'/'mixed'" x76). A profile that never trades is not a profile. Removed from it.
- **The one robust finding is a negative one**: score-5 entries lost -5,082 over 162 calls across the whole period,
  while score>=6 was positive (+14,590 over 61; but +15,509 of that came from just two days, 21 Oct and 6 Nov - so
  "score 6 wins" is fragile; "score 5 loses" is the dependable half). Removing a losing bucket is far less prone
  to overfitting than adding a winning one.
- The 5 entries issued before a regime could be read (< 20 bars, 09:36-09:50) were all stop-losses (-7,780).
- **Range-fade is not the answer** for choppy weeks: -7,893 over 175 shadows, 30% win. Stays shadow-only.
- Aggressive's direction check locked +0.5R median exits (32 of them) while its stop-losses ran the full -1R:
  62% win rate, +1,806 net. A clipped-winner / full-loser shape; the `direction_check_min_r` (0.3) is too low to
  be a trend-reversal signal and reads mostly as noise-taking. Not changed yet - no counterfactual to measure.

Changes: Conservative = Balanced + `min_score 6` + `warmup_bars 20` + event calendar (`MODE_TUNING` in
`signals/modes.py`; regime gate and vol filter removed from it). Candidates exactly one clue short of the bar are
now **shadowed** (blocker "score N < M (one clue short)") instead of silently dropped, so raising the score bar
never goes blind to what the dropped bucket does next. Balanced (the real-money path) was then also raised to
`min_score 6` on the owner's call the same day (standard 15-bar warm-up, no event calendar - that is what still
separates it from Conservative). Aggressive stays at 5: it is explicitly the volume profile. Expect roughly a
quarter of the previous call count on Balanced; every dropped score-5 candidate is still tracked as a shadow.

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
