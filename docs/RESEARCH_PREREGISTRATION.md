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
(none)
