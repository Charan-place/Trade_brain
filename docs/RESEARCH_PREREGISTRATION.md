# Edge audit: pre-registered protocol (frozen 2026-11-13, before any multi-year data was looked at)

Purpose: decide whether any price-only idea has a statistically defensible net edge on NIFTY/SENSEX before any strategy
change is built. Written first so the answer cannot be shaped by the data. Edits after the first look at the data must be
appended below the "Amendments" line with a date and a reason, never made in place.

## Data
- 1-minute NIFTY 50 and SENSEX index candles from Upstox Historical Candle V3 (Jan 2022 to latest complete day), plus India VIX.
  Index candles carry no volume, so this protocol cannot test VWAP, CVD, OI, order-book or option clues. Those are tested
  separately on recorded tick days and on the forward per-bar feature store.
- Chronological split, fixed now: TRAIN 2022-01-01..2024-06-30, VALIDATE 2024-07-01..2025-06-30, FINAL 2025-07-01..latest.
  FINAL is read exactly once, after every decision below is made. A failed FINAL result is reported, not re-run.

## Hypotheses (each with exactly the parameters shown; no other values may be tried)
- H1 Opening-30-minute return (09:15-09:45) predicts the 15:00-15:30 return. Sign of the relationship, daily.
- H2 Overnight return (previous close to 09:15 open) predicts the day's 09:45-15:15 return and the 15:00-15:30 return.
- H3 Opening-range breakout: first close beyond the 15-minute opening range, held to 15:15 or to +30 minutes, whichever is first.
- H4 Engine skeleton, price only: a completed 1-minute close beyond the prior 10-minute high/low AND the 60-minute move
  at least 1.5x the 14-bar ATR in the same direction. Forward index return at +5, +15, +30 minutes.
- H5 Volatility gate: H1 and H4 effects conditional on trailing 20-day realised-vol percentile above 50 vs below 50, compared
  with 1,000 random gates keeping the same number of days.
- H6 Time-of-day drift: mean 1-minute return by bucket (09:15-09:35, 09:35-12:00, 12:00-14:00, 14:00-15:00, 15:00-15:30).
- H7 Expiry day vs other days (NIFTY Tuesday or holiday-shifted, SENSEX Thursday) for H1, H3, H4.
- H8 Day-of-week effect on the day's return.

## Procedure
1. Fit nothing on VALIDATE or FINAL. TRAIN is used to measure, not to tune. No threshold sweeps.
2. Per test: mean per-trade return in index points and in bp, day-block bootstrap 95% interval (resample days, 20,000 draws),
   and a random-entry null (same days, same minutes, random direction, same holding rule) with its p-value.
3. Multiple testing: every hypothesis x horizon x index is one test in a single family. Holm-Bonferroni at 0.05 across the
   whole family. The count of tests is reported next to every p-value.
4. A hypothesis PASSES only if: it survives Holm on TRAIN; the same sign and an interval excluding zero hold on VALIDATE;
   and the mean exceeds the cost hurdle below. Only then is FINAL opened, once.
5. Cost hurdle: about 2.5 index points per round trip for an option buyer (about 72 rupees/lot, lot 65, delta about 0.5,
   plus spread) and a separately computed hurdle for a futures trader once verified rates are confirmed. Gross edge below the
   hurdle is reported as "no net edge" even when statistically significant.
6. Every result is published, including failures. No test is dropped after being run.

## Trials already consumed on the 17-day live sample (counted against any claim made from it)
Score threshold 5 vs 6; regime gate (1-bar and 3-bar confirm); vol-spike filter (opening baseline, then trailing median);
event-calendar gate; direction check (several noise-filter variants); real vs flat cost model; one-clue-short shadowing;
three mode presets; opening-window cutoffs. At least a dozen variants were examined on the same 17 days, so none of the
"improvements" found there count as validated.

## Amendments
**2026-11-13 (before any multi-year result was produced; only a 10-day API sanity check had been seen)** - implementation
details fixed so they cannot be chosen after the fact:
- Entries are at the NEXT bar's open after the signal bar closes; exits at a bar close. No signal bar's own price is used.
- H4: 10-minute cooldown after any event (mirrors the engine), signals between 09:45 and 14:45, 14-bar mean true range as
  ATR, 60-minute move measured as close[t] - close[t-59] (floored at the first bar of the day), horizons 5/15/30 bars.
- H3: first close beyond the 09:15-09:29 range after 09:30, at most one trade per day, exit at the earlier of +30 bars or 15:14.
- H1/H2 targets: last 30 minutes = open(15:00) to close(15:29); day move = open(09:45) to close(15:14).
- Direction p-values: two-sided, from 10,000 random-direction (sign-flip) draws. The day-block bootstrap interval must
  also exclude zero. Both are required.
- H5/H7 use 1,000 label shuffles across days. Volatility regime = trailing 20-day close-to-close std versus the
  expanding median of all earlier days' values (no look-ahead).
- Cost hurdle is applied in basis points so both indices share it: 1.0 bp per round trip (2.5 NIFTY points at about 24,500).
- Expiry calendar for H7 is an UNVERIFIED assumption (NIFTY Thursday before 2025-09-01 and Tuesday after; SENSEX Friday
  before 2025-09-01 and Thursday after; a nominal expiry with no candles moves to the previous trading day). H7 is
  informational only and gates nothing.
- Days with fewer than 360 session bars (half days, Muhurat, data gaps) are excluded and counted in the report.
- The loader refuses to read FINAL-period dates unless explicitly opened.

## Family 2: previous-day levels and trend (frozen 2026-11-13, before any code or result for this family exists)
Requested by the owner: use the prior day's high, low, close and trend. Same data, same splits, same procedure, same pass rule
as family 1 (Holm over this family on TRAIN, same sign with interval excluding zero on VALIDATE, mean >= 1.0 bp in both).
FINAL stays sealed. One parameter value per hypothesis; no sweeps. PDH/PDL/PDC = previous loaded trading day's high, low and last close.
Tests in this family: 7 hypotheses x 2 indices = 14. Cumulative tests across all families: 36 + 14 = 50 (reported next to every p-value).

- P2 Prior-day extreme breakout, continuation: first bar t >= 09:30 (and <= 14:45) with close above PDH and previous close <= PDH
  (fresh cross) goes long; first fresh close below PDL goes short; whichever comes first; one trade per day.
  Entry next bar open, exit at close of min(t+30, 15:14).
- P3 Prior-day extreme failed break, fade: first bar t in the same window with high > PDH but close < PDH goes short; low < PDL
  but close > PDL goes long; first in time; one trade per day. Same entry/exit as P2.
- P4 Gap fill: gap = open(09:15)/PDC - 1; if |gap| >= 0.3% trade against the gap. Entry open(09:45), exit close(12:00 bar, i.e. 11:59).
- P5a Prior-day trend: sign of PDC / (the close before it) - 1 predicts today. Entry open(09:45), exit close(15:14).
- P5b Five-day trend: sign of PDC / (close 5 trading days before PDC) - 1 predicts today. Same entry/exit.
- P6 Prior-day close location: CLV = (PDC - PDL) / (PDH - PDL). CLV >= 0.8 goes long, <= 0.2 goes short, else no trade.
  Same entry/exit as P5a.
- P7 Trend-aligned prior-day breakout: P2 only when the five-day trend sign agrees with the break direction.
The first loaded day of each split has no prior day and is skipped. Direction p-values and intervals as in family 1.
Expected honest outcome stated in advance: the hurdle is 1.0 bp, so a small statistically significant effect is still reported as "no net edge".

## Family 3: order-flow clues on strictly forward data (frozen 2026-11-13, before the code exists)
The live recorder stores one feature row per index per minute (price, VWAP, futures OI, CVD, buy share, order-book imbalance,
big-print flow, option-chain fields, and the seven clue flags per side). Family 3 tests whether any single clue predicts the
next 15 minutes. Only days from 2026-11-16 onward count (the 16 days recorded before the freeze were already looked at while
tuning and are used for pipeline checks only; no p-value on them is a result).
- Signal: at minute t, clue k fires long if the bullish side's flag k is true and the bearish side's flag k is false; short if the reverse.
  Seven clues (vwap, aggression, fut_oi, big_players, order_book, writers, volatility) x NIFTY/SENSEX = 14 tests, one Holm family.
- Entry at the NEXT minute's recorded spot, exit at the recorded spot 15 minutes later (rows within +-1 minute; otherwise skipped).
  After a trade for clue k on an index, no new trade for that clue/index for 15 minutes (so trades do not overlap).
- Same statistics as family 1 (day-block bootstrap interval, sign-flip null, Holm) and same pass rule: Holm p < 0.05, interval excluding
  zero, |mean| >= 1.0 bp.
- Sample rule: no test statistic is computed until 60 trading days (counting only days from 2026-11-16) are on file. Before that the tool
  prints day counts only. It is evaluated once at 60 days. A failure is reported; extending the sample needs a new dated pre-registration.

## Gate A: what a signal profile must show on paper before real money (frozen 2026-11-13)
Applies to one profile (conservative, balanced or aggressive) at a frozen configuration, scored on calls issued on or after the day the
real-cost build is deployed and a start date is written in the deploy note. All of the following must hold, otherwise the profile stays OFF:
1. At least 30 trading days and 150 closed calls.
2. Real-cost model in force (cost_est_per_lot >= 60 on at least 95% of calls); flat-40 results are rejected as invalid.
3. Mean net P&L per call > 0 and the day-block bootstrap 95% lower bound > 0.
4. Mean net P&L per call stays > 0 after removing the 3 best calls.
5. No single day contributes more than 40% of total net profit.
Passing Gate A permits live trading at ONE lot with a hard daily loss cap; it does not permit scaling. Failing it is reported plainly.

**Amendment to family 3, 2026-11-13 (no forward day has been recorded yet, so no result exists):** the `volatility` clue is set on both
the bullish and bearish side at the same time, so it carries no direction and cannot be tested as long or short. It is dropped.
Family 3 is 6 clues x 2 indices = 12 tests (cumulative tests across all families: 36 + 14 + 12 = 62).
