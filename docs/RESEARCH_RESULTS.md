# Edge audit results, run 1 (2026-11-13): pre-registered price-only tests, TRAIN + VALIDATE only

Protocol: `docs/RESEARCH_PREREGISTRATION.md` (frozen before any multi-year result). Data: Upstox V3 1-minute NIFTY and SENSEX
index candles, 2022-01-03 to 2026-11-12, 0 failed months. TRAIN 613 days, VALIDATE 248 days per index. FINAL (from 2025-07-01)
was not read. Code: `src/tbrain/research/edge_tests.py`, run with `python -m tbrain.research.edge_tests`.

## Verdict
**0 of 36 tests pass.** None clears Holm-corrected significance on TRAIN, holds its sign with an interval excluding zero on
VALIDATE, and exceeds the 1.0 bp cost hurdle.

## The result that matters most: the engine's own breakout trigger, price only (H4)
| Index | Horizon | Events (train) | Mean bp per trade, train [95% CI] | Mean bp, validate [95% CI] |
|---|---|---|---|---|
| NIFTY | +5 min | 8,471 | +0.02 [-0.15, +0.19] | -0.05 [-0.29, +0.20] |
| NIFTY | +15 min | 8,471 | +0.07 [-0.22, +0.38] | +0.06 [-0.37, +0.48] |
| NIFTY | +30 min | 8,471 | +0.35 [-0.12, +0.82] | +0.22 [-0.46, +0.91] |
| SENSEX | +5 min | 8,680 | +0.06 [-0.09, +0.23] | -0.01 [-0.24, +0.22] |
| SENSEX | +15 min | 8,680 | +0.10 [-0.19, +0.41] | +0.06 [-0.35, +0.48] |
| SENSEX | +30 min | 8,680 | +0.30 [-0.16, +0.77] | -0.04 [-0.69, +0.62] |

A completed 1-minute close beyond the prior 10-minute extreme, with a 60-minute trend of at least 1.5 ATR, moves the index
by roughly +0.0 to +0.35 bp per trade over the next 5-30 minutes, before any costs. The cost hurdle is about 1.0 bp.
The trigger carries no measurable directional information on 4+ years of data, so no exit, sizing or clue tuning can turn
it into a net edge by itself. This agrees with the live null on 223 calls.

## Other findings
- H1/H2/H3 (first-30 minutes, overnight, 15-minute opening-range breakout): all intervals include zero; means within +-2 bp.
- H5 volatility regime: no consistent difference on either index across TRAIN and VALIDATE.
- H6 opening 20 minutes (09:15-09:35): NIFTY -3.67 bp (p 0.0016, Holm 0.058) and SENSEX -2.21 bp in TRAIN, both negative again in
  VALIDATE (-1.86, -1.88) with intervals including zero. Not significant after correction. It is the only pattern pointing the
  same way in both indices and both periods, so it is a hypothesis for a new pre-registered test, not a finding. Opening-minute
  option spreads and IV are the least favourable of the day, so a gross drift of this size may not survive real option costs.
- H8 Tuesday: VALIDATE interval excludes zero on both indices (about -20 bp) but TRAIN does not. With roughly 70 interval checks,
  about 3-4 would be expected to exclude zero by chance. Treated as noise unless a pre-registered repeat finds it again.
- H7 expiry splits and the two "Sat" rows in H8 (single special-session days, n=1) are informational and carry no meaning.

## Not tested here (cannot be tested on index candles)
VWAP, cumulative volume delta, futures OI, order-book imbalance, large-print and option-writer clues; any option P&L (theta,
IV, spread). The earlier research found no credible evidence for those clues either, but this run does not settle them.

# Run 2 (2026-11-13): family 2, previous-day levels and trend (14 tests, protocol frozen in commit 8d55496 before the code)
**0 of 14 pass.** TRAIN + VALIDATE only; FINAL still sealed. Cumulative tests across both families: 50.

| Test (NIFTY / SENSEX) | Train bp [95% CI] | Validate bp [95% CI] |
|---|---|---|
| P2 break of prior-day high/low, continuation | -0.59 [-2.54,+1.36] / -1.68 [-3.73,+0.36] | +3.14 [-0.44,+6.68] / +0.78 [-3.06,+4.47] |
| P3 failed break, fade | +2.06 [-0.05,+4.22] / +2.12 [-0.13,+4.36] | -1.84 [-5.65,+1.92] / -2.25 [-5.75,+1.29] |
| P4 gap fill (gap >= 0.3%) | -1.12 / -1.26 (intervals span 0) | -0.67 / -2.31 (intervals span 0) |
| P5a prior-day trend | +1.94 [-3.06,+6.96] / +3.48 [-1.55,+8.46] | +7.11 [-0.46,+14.68] / +8.03 [+0.44,+15.56] |
| P5b five-day trend | -1.26 / -2.96 (intervals span 0) | -2.27 / -2.60 (intervals span 0) |
| P6 prior close location | +1.31 / +2.65 (intervals span 0) | +6.45 / +11.40 [+0.66,+22.26] |
| P7 trend-aligned break | -0.13 / -1.59 (intervals span 0) | +4.18 [+0.12,+8.40] / +0.74 |

Reading: no test is significant on TRAIN after Holm (best raw p 0.06; Holm 0.83). Signs flip between TRAIN and VALIDATE for P2, P3,
P5b and P7 on NIFTY, which is what noise looks like. The three VALIDATE intervals that exclude zero (NIFTY P7, SENSEX P5a, SENSEX P6)
sit among 14 checks where about one is expected by chance, and they were not significant on TRAIN, so they are not findings. The only
same-sign-both-periods hint is the prior-day-trend continuation (P5a, positive on both indices in both periods, +2 to +8 bp, intervals
mostly spanning zero). That is a candidate for the sealed FINAL period only if the owner wants one more test, with real option costs.
Prior-day high, low, close and trend carry no tradable edge on this evidence.
