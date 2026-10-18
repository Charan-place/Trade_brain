# NIFTY + SENSEX OPTIONS "BRAIN" — MASTER PLAN (v1.1 · 2026-10-19)

Owner: Charan · Scope: Nifty 50 (NSE) + Sensex (BSE) index options ONLY · Personal use only
Status: PLAN ONLY. No code, no live orders until Phase 6 gates are passed.

---------------------------------------------------------------------
## 0. READ THIS FIRST — what "perfect" can and cannot mean
---------------------------------------------------------------------
1. **Nobody can predict "the next move correctly".** Not us, not hedge funds. Intraday index direction is
   close to a coin flip with a tiny, unstable edge. What we CAN build is a system that outputs
   **calibrated probabilities** (e.g. "58% up over 15 min, 90% interval X–Y") and only trades when
   expected value AFTER costs is positive, with hard loss limits.
2. **Volatility is far more predictable than direction.** Vol clusters and mean-reverts; direction doesn't.
   Options P&L is mostly vol + theta + gamma, so the brain's strongest legs are: vol forecast vs implied,
   regime detection, structure selection, cost control, and risk discipline — direction is the weakest leg.
3. **Base rate is brutal.** SEBI's own published studies found roughly 9 in 10 individual F&O traders lose
   money. Costs (brokerage, STT, exchange fees, GST, slippage) are the main killer. Design for costs first.
4. **Retail cannot win a latency race.** Our edge is NOT speed. Decisions are 1-min to 30-min horizon.
5. **"Buy vs sell, and how much" is only partly observable.** The exchange feed shows trades/quotes, NOT who
   the buyer/seller was or the aggressor side. We INFER flow (tick rule / Lee-Ready, depth imbalance,
   OI change + price). Retail feeds are also snapshot-based (several updates/sec), not every trade.
6. Success = a **validated, honest, risk-controlled** system. If it fails validation, the correct output
   is "no edge found", and that is a win versus losing real money.
7. Not financial advice. I am not a licensed advisor. Start with money you can afford to lose entirely.

---------------------------------------------------------------------
## 1. GOAL & SUCCESS METRICS
---------------------------------------------------------------------
Primary objective: positive **net-of-cost** expected value per trade with bounded drawdown.

Model-quality metrics (not "accuracy"):
- Calibration: Brier score, ECE (target ECE < 0.03 out-of-sample)  [proposed threshold]
- Skill vs baseline: log-loss better than "always base rate" and than naive momentum
- Vol forecast: QLIKE / MSE vs HAR-RV baseline

Strategy metrics (all net of realistic costs + slippage):
- Profit factor > 1.3 out-of-sample, survives +50% cost stress   [proposed]
- Deflated Sharpe ratio > 0 after multiple-testing correction
- Max drawdown within pre-set budget; worst day within daily-loss limit
- Paper vs backtest divergence < 20%                              [proposed]

Go/no-go gates are in section 11. Thresholds are proposals — tune them BEFORE seeing results and write them down.

---------------------------------------------------------------------
## 2. MARKET + REGULATORY FACTS (verify against official sources before coding)
---------------------------------------------------------------------
Verified from sources read on 2026-10-18 (see section 14):
- Nifty 50 lot size: **65** (revised from 75; effective for contracts expiring from Jan 2026). [verify on NSE]
- Sensex (BSE) options lot size: **20** (AlgoTest page checked 28 Sep 2026; raised from 10 via BSE notice 20241021-13). [verify on BSE]
- CONFIRMED from Upstox instrument master (2026-10-19): NIFTY lot 65, SENSEX lot 20; NIFTY weekly expiries on Tuesdays
  (with Monday dates in the file, e.g. 2026-10-19 and 2026-11-09 - presumably holiday-shifted; verify), SENSEX on Thursdays.
  Index keys: NSE_INDEX|Nifty 50, BSE_INDEX|SENSEX. Instrument file: complete.json.gz works.
- Data source decided: **Upstox** (market data only; no orders). `full` mode on the standard plan = LTPC + 5-level depth + OI + Greeks + tbq/tsq (per Upstox Market Data Feed V3 docs); 30-level depth needs Upstox Plus.
- SEBI retail algo framework (core circular 4 Feb 2025; full framework mandatory for brokers from 1 Apr 2026):
  - Below **10 orders/sec (OPS)** you are a regular API user, no individual algo registration.
  - Above 10 OPS: algo must be registered with the exchange.
  - **API keys bound to a static IP.** Angel One: from 1 Apr 2026 orders accepted only from registered IP(s);
    IP change allowed at most once per calendar week; one IP maps to one client.
  - Every algo order carries an exchange Algo-ID from 1 Apr 2026.
  - **Market and IOC order types are prohibited for algo orders** (per Angel One's page) -> limit orders only. Confirm with your broker.
  - Personal-use own-code bot: allowed as a regular API user; don't run it for/sell it to others without
    the broker / white-box / SEBI RA rules (black-box providers need an RA licence).
Not verified (check before relying): Nifty weekly expiry day (believed Tuesday), strike interval near ATM
(believed 50), exact STT/charges schedule (changed recently — pull from your broker's charge calculator,
do NOT hardcode from memory), margin rules, trading hours 09:15–15:30 IST (pre-open 09:00–09:08).

Our bot design assumption: <= 2 orders/sec, limit orders only, static IP on a Mumbai VPS (AWS ap-south-1 + Elastic IP).

---------------------------------------------------------------------
## 3. DATA — WHAT EXISTS, WHAT DOESN'T, WHAT KEYS YOU CREATE
---------------------------------------------------------------------
### 3.1 Live + historical market data (the core)
| Source | What you get | Cost (as read 2026-10-18) | Notes |
|---|---|---|---|
| **Upstox API** | Option chain endpoint, live websocket, historical candles incl. expired F&O contracts, Greeks | Upstox support reply: no extra permission/paid plan for F&O data | Best free-to-start path. Token refresh daily. Check depth levels + websocket limits in docs |
| **Dhan API** | Trading APIs free; Data APIs (real-time feed + historical) Rs 499/mo as listed | Trading free / Data Rs 499 | Rate limits listed: 25 orders/s, 5000 orders/day, OHLC 10 req/s, 20k req/day. Check 20-level depth + option chain greeks in docs |
| **Zerodha Kite Connect** | Personal plan: orders only, NO data. Connect plan: websocket + historical | Rs 500/app/month for Connect | Credits from Oct 2026 if brokerage >= Rs 2,000/mo |
| **Angel One SmartAPI** | Orders + data | Pricing not confirmed in what I read | Static IP mandatory from 1 Apr 2026 |
| **NSE official EOD files** | F&O bhavcopy (OHLC, OI, volume per contract), India VIX history, participant-wise OI/volume, FII/DII | Free | EOD only. Official source for backfill + daily context |
| **TradingView** | Charts, alerts, Pine scripts | No official public market-data API (as far as I know). Webhook alerts need a paid plan | Use for eyeballing/validation only, NOT as the data backbone |
| NSE website option-chain JSON | Unofficial scraped endpoint | - | Terms-of-service risk, rate-blocked, unreliable. Not in this plan |

Decision needed from you: **which broker account do you already have?** The data layer is built behind an adapter
so we can swap brokers, but Phase 1 needs ONE primary feed.

### 3.2 Macro / cross-asset / news (free tier friendly)
- India VIX, GIFT Nifty, Bank Nifty, top-10 Nifty heavyweights (from the same broker feed)
- USDINR, Brent, US index futures, US 10y, DXY: free providers (FRED API key is free; yfinance is unofficial — use only for research, not live)
- Event calendar: RBI policy, CPI/IIP, US Fed/NFP, Budget, results dates of heavyweights, holidays
- News: NewsAPI (you already used it) + RSS (RBI, SEBI, major Indian business feeds) -> LLM event-risk flags (NOT a trading signal at first)

### 3.3 The data gap (important)
Deep intraday history with option-chain snapshots, depth and flow is NOT freely available. So:
**Phase 1 starts a recorder that stores every chain/tick snapshot from day 1.** Our own recorded dataset becomes the edge
asset. Until we have months of it, early models train on broker candles + bhavcopy and are labelled "low-fidelity".
Paid history vendors exist if you later want to buy history; evaluate then.

Storage estimate: ~100 instruments x ~1 snapshot/s x ~22,500 s/day ≈ 2.3M rows/day ≈ tens of MB/day in Parquet. Trivial.

### 3.4 API keys / accounts you will create (I never need trading keys until Phase 6)
1. Broker data key (Upstox / Dhan / Kite Connect — your choice). **Create READ-ONLY / data scope first.**
2. Static IP: AWS Elastic IP on a Mumbai instance, whitelisted in the broker console (needed before any order API use).
3. FRED API key (free) for US yields.
4. NewsAPI key (you have one).
5. Telegram bot token + chat id (alerts, kill switch).
6. Anthropic API key (optional: event-risk flags, post-trade analysis).
**Security rule:** keys live ONLY in a local `.env` (gitignored) / AWS Secrets Manager. Never paste keys/tokens into chat,
tweets, or repos. Order-placing permission is enabled in Phase 6 only.

---------------------------------------------------------------------
## 4. ARCHITECTURE
---------------------------------------------------------------------
```
 Broker WS/REST ──> [Collector] ──> Redis Streams (hot, last N min) ──> [Feature Engine] ──> [Model Server]
                         │                                                    │                  │
                         └──> Parquet/TimescaleDB (cold, forever) <───────────┘                  ▼
 NSE EOD files, FRED, News ──> [Batch ingest] ──────────────────────────────────>  [Strategy: structure + EV]
                                                                                           │
                                          [Deterministic Risk Engine]  <───────────────────┘
                                                    │  (can veto everything, kill switch)
                                                    ▼
                                         [Execution: limit orders only]  ──> Broker
                                                    │
                          [Monitor: Grafana/Streamlit dashboard + Telegram]  <── all services
```
Principles:
- **Fast loop is plain deterministic Python (asyncio).** LLMs are never in the order path. An LLM/classifier "approver" cannot be the
  sole risk gate: probabilistic output is not authorization, and can be prompt-injected.
- Everything is **replayable**: the same recorded stream can be replayed through live code to get identical decisions (backtest = replay).
- Point-in-time correctness: every feature stamped with the exchange timestamp it was knowable at.
- Stack: Python 3.12, asyncio, Redis Streams, Parquet + DuckDB (research) and PostgreSQL/TimescaleDB (ops), LightGBM, PyTorch (challengers),
  scikit-learn/statsmodels/arch, MLflow (experiments), Docker, AWS ap-south-1 VPS, Grafana or Streamlit, Telegram alerts.
- LangGraph (your stack) is appropriate for the slow supervisory layer: daily plan, event-risk review, post-trade reports — not for tick-time decisions.
- Latency budget: feature+model+risk < 250 ms per decision; irrelevant for 1-min+ horizons, so no co-location.

---------------------------------------------------------------------
## 5. FEATURE CATALOGUE ("analyses everything")
---------------------------------------------------------------------
### A. Moneyness (ITM / ATM / OTM) — per strike, per expiry
- Calls: ITM if K < S, ATM if K ≈ S, OTM if K > S. Puts: ITM if K > S, OTM if K < S.
- Use **forward** F (from put-call parity at ATM: F = K + e^{rT}(C − P)), not spot, for pricing.
- Log-moneyness  m = ln(K/F). Standardised moneyness  z = m / (σ_ATM · √T)  -> comparable across days/expiries.
- Delta buckets (≈10Δ, 25Δ, 40Δ, ATM, 60Δ…) so features mean the same thing every day.
- Per-bucket: premium, intrinsic vs time value, volume, OI, ΔOI, spread, depth, IV, greeks.
- ITM/ATM/OTM **share of volume and OI**, call-vs-put, per bucket — shows where traders are positioning (hedgers vs speculators).

### B. Greeks, IV and volatility
- Model: **Black-76** on the forward (Nifty options are European, cash-settled). C = e^{-rT}[F·N(d1) − K·N(d2)], d1 = [ln(F/K) + ½σ²T]/(σ√T).
- Per strike: IV (solve with Newton/Brent), delta, gamma, vega, theta, vanna, volga. Compute our own; don't trust vendor IV blindly.
- IV surface per expiry: fit **SVI** (or SABR) for a smooth skew; arbitrage checks (butterfly/calendar).
- Features: ATM IV, 25Δ risk-reversal (skew), butterfly (smile curvature), term-structure slope, IV change over 1/5/15 min,
  IV percentile/rank (rolling), India VIX level + change + VIX-vs-ATM-IV spread.
- Realised vol: close-to-close, Parkinson, Garman–Klass, Yang–Zhang, 1-min realised variance (RV), bipower variation (jump-robust), jump flags.
- **Variance risk premium** = IV² − E[RV²] (using our forecast). Core edge candidate.
- **HAR-RV** baseline: RV_{t+1} = b0 + bd·RV_d + bw·RV_w + bm·RV_m.
- Expiry-day dynamics: DTE, hours-to-expiry, gamma concentration near ATM, pin risk, theta burn rate.

### C. Order flow / "buy vs sell and how much"
Reality: infer, don't observe.
- **Trade classification**: Lee–Ready / tick rule: price >= ask -> buy-initiated; <= bid -> sell-initiated; else compare with midpoint, then tick direction.
- **Signed volume + CVD (cumulative volume delta)** — computed mainly on **Nifty futures** (most liquid, cleanest flow proxy), and on the ATM option legs.
- **Trade size**: volume delta between snapshots; flag block prints (> k·rolling median); average-traded-price drift vs LTP.
- **Order-book imbalance**: OBI = (ΣQ_bid − ΣQ_ask)/(ΣQ_bid + ΣQ_ask) over top-k levels; **microprice** = (P_ask·Q_bid + P_bid·Q_ask)/(Q_bid + Q_ask); queue/depth change (adds, pulls).
- **OI x price build-up map** (per strike and for futures): price↑+OI↑ = long build-up; price↓+OI↑ = short build-up; price↑+OI↓ = short covering; price↓+OI↓ = long unwinding.
- **Chain positioning**: PCR (OI and volume), max pain, OI walls (call-OI cluster = resistance proxy, put-OI cluster = support proxy), ΔOI shifts/roll of walls through the day.
- **GEX-style gamma exposure (heuristic)**: Σ γ_i · OI_i · lot · S² · 0.01 · sign(call +, put −). WARNING: assumes dealer side; India has no public dealer-position data -> treat as a soft feature, validate or drop.
- **Participant data (EOD, free from NSE)**: FII/DII/Pro/Client index-futures & options OI and net positions -> prior-day context features, not intraday.
- Optional: VPIN (order-flow toxicity) as a regime/risk feature.

### D. Liquidity & cost
- Quoted spread (abs, % of premium), top-k depth in lots and Rs, depth imbalance, spread/ATR.
- Amihud illiquidity, Kyle's lambda (price impact per signed volume) estimated rolling.
- **Per-strike slippage model**: expected fill vs mid as a function of spread, depth, order size in lots, time-of-day. Used in EVERY backtest and decision.
- Liquidity filter: strikes below spread/depth thresholds are untradeable by rule.

### E. Price action / time / context
- Returns at multiple horizons, ATR, VWAP deviation, opening gap vs GIFT Nifty, opening-range (first 15 min) breakout state, pivot/prior-day levels, trend strength (ADX/Hurst), mean-reversion z-scores.
- Intraday seasonality (U-shaped volume/vol), minute-of-day, day-of-week, DTE, event flags (RBI/CPI/Fed/Budget/results), holiday proximity.
- Cross-asset: Bank Nifty, top-10 heavyweights' returns weighted by index weight (index leadership), USDINR, Brent, US futures, global risk proxies.
- News/event risk (LLM-extracted, categorical): "scheduled high-impact event within next X min", "breaking headline" -> used for **risk gating**, later maybe features.

---------------------------------------------------------------------
## 6. THE BRAIN — MODEL STACK
---------------------------------------------------------------------
Think "committee of specialists + a referee", not one giant network.

1. **Regime model** (what kind of day/hour is this?)
   Hidden Markov / Markov-switching on [RV, IV level, trend strength, liquidity, event flag] -> {trend, range, high-vol, event, illiquid}.
   Every other model is evaluated PER regime; no regime with demonstrated edge -> no trade.
2. **Volatility model (primary alpha candidate)**
   Predict realised variance over next 15/60 min and to-close. Stack: HAR-RV + GJR-GARCH baselines + LightGBM on chain/flow features.
   Output: forecast distribution; compare vs ATM/strike IV -> vol edge signal (IV rich = sell-premium candidate, IV cheap = buy-premium candidate).
3. **Direction / return-distribution model**
   - Labels: **triple-barrier** (upper = +k·σ_t, lower = −k·σ_t, vertical = horizon) + **meta-labeling** (a second model decides "act or skip" on the first model's signal).
   - Baseline: **LightGBM** on the feature catalogue, horizons 5/15/30 min and to-close; quantile outputs for the return distribution.
   - Calibration layer: isotonic/Platt; **conformal prediction** intervals so "uncertain" is a first-class output.
   - Challengers (only if baseline shows life): temporal CNN / Temporal Fusion Transformer on short order-book windows. Deep nets must beat LightGBM out-of-sample to be promoted.
4. **Structure selector (turns forecasts into a trade)**
   For each candidate structure, simulate P&L under the model's predicted return/vol distribution (Monte Carlo from fitted dynamics or bootstrapped historical paths) INCLUDING costs and slippage:
   long call/put (ATM / 1-strike OTM), debit spreads, credit spreads, iron fly/condor, expiry-day straddle variants.
   Choose max (EV − λ·tail-risk) subject to max-loss, margin, liquidity filters. **Prefer defined-risk structures; no naked short options** in v1.
5. **Referee = deterministic risk engine** (section 8). Can veto any model. Models never touch order code directly.
6. **LLM layer (slow path only)**: event/news risk flags, daily pre-market brief, post-trade explanations, weekly review. Never in the order loop.

Model governance: every model has a versioned artifact, training-data hash, validation report, and a rollback. Retrain on a schedule; auto-demote on calibration drift.

---------------------------------------------------------------------
## 7. EXECUTION
---------------------------------------------------------------------
- Limit orders only (market/IOC not allowed for algos per broker rules; confirm). Price = mid ± tick logic with a max chase budget.
- Slice by lots if depth is thin; cancel/replace with a cap; order-state machine (new/partial/filled/rejected/cancelled).
- Reconcile positions + orders with broker every N seconds; mismatch -> halt.
- Track **implementation shortfall** (decision price vs fill) per trade; feeds back into the slippage model.
- Idempotent order IDs; no duplicate sends on reconnect; clock sync (NTP).
- Square-off logic before close; never carry unintended overnight risk in v1.

---------------------------------------------------------------------
## 8. RISK ENGINE (deterministic code, highest priority module)
---------------------------------------------------------------------
Hard limits (values to be set by you; placeholders are proposals):
- Max loss per trade <= 0.5–1% of capital. Max daily loss <= 2% -> auto-stop for the day. Max weekly loss -> auto-stop for the week.
- Max lots, max open positions, max premium at risk, margin buffer.
- Position sizing: **fractional Kelly capped (<= 0.25 Kelly)**, further capped by the loss limits above.
- Time rules: no entries in first 5 min or last 15 min unless a validated regime says otherwise; auto square-off before close.
- Event blackouts around scheduled releases; reduced size on expiry day unless validated.
- **Data watchdog**: stale feed (> N s), crossed/invalid quotes, IV solver failures, clock skew -> flatten/halt.
- **Model watchdog**: calibration drift, regime unknown, feature outliers -> no-trade.
- **Kill switch**: Telegram command + dashboard button + file flag; cancels all open orders and flattens.
- Broker rejection storm / order-rate anomaly -> halt. Max OPS cap far below the 10 OPS threshold.
- Everything logged immutably (audit trail) — also useful for tax/compliance records.

---------------------------------------------------------------------
## 9. VALIDATION & BACKTESTING PROTOCOL (where most bots lie to their owners)
---------------------------------------------------------------------
- Backtest = **replay** of recorded/point-in-time data through the SAME code as live.
- **Purged + embargoed walk-forward** and **combinatorial purged CV** (López de Prado) — no overlapping-label leakage.
- No look-ahead: IV, OI and EOD fields only used after they were actually published.
- Costs: brokerage, STT, exchange/SEBI fees, GST, stamp duty from the CURRENT schedule + slippage model + partial fills + rejection.
  Stress at +50% costs and at 2x spread.
- Multiple-testing control: log EVERY experiment; report **Deflated Sharpe**, probability of backtest overfitting (PBO), White's reality check / SPA; bootstrap CIs.
- Stress windows: election-day crash (Jun 2024), Budget days, RBI/Fed days, expiry days, gap-open days, the worst weeks in the sample.
- Pre-register the go/no-go thresholds BEFORE running the final test. One shot at the final holdout; if used, it's burned.
- **Paper trading** on the live feed with pessimistic fills (cross the spread) — min 4–8 weeks.
- **Micro-live** (1 lot, smallest structure) — min 8 weeks, compared daily to paper/backtest expectations.
- Scale only by pre-set rules (e.g. +25% size after N weeks within expected bands, auto-reduce on breach).

---------------------------------------------------------------------
## 10. THE "UI THAT SPEAKS IT ALL" — dashboard spec
---------------------------------------------------------------------
Real-time, from the recorded stream (Streamlit/Grafana first, custom React later if worth it):
1. **Command bar**: regime badge, feed health/latency, risk state (green/amber/red), kill switch, daily P&L vs loss limit.
2. **Option-chain heatmap**: strikes x (OI, ΔOI, volume, IV, spread) with ITM/ATM/OTM shading and OI walls marked.
3. **IV surface** (3D/2D) + skew curve + term structure, with "yesterday/open" overlays.
4. **Flow panel**: CVD (futures + ATM legs), OBI, microprice, block prints tape, build-up map.
5. **Brain panel**: direction probability gauge with calibrated interval, vol forecast vs IV (edge), per-regime reliability, why-now feature contributions (SHAP).
6. **Trade panel**: candidate structures with EV, max loss, costs; chosen trade; fills vs plan (slippage).
7. **Risk panel**: exposure (net delta/gamma/vega/theta), margin, limits used.
8. **Post-session report**: auto-generated PnL attribution (delta/vega/theta/costs), model calibration plots, anomalies.

---------------------------------------------------------------------
## 11. ROADMAP, DELIVERABLES, EXIT GATES  (realistic: ~5–6 months before meaningful live size)
---------------------------------------------------------------------
**Phase 0 — Foundations (week 1)**
 Decide broker; create READ-ONLY data key; AWS Mumbai VPS + Elastic IP; repo + CI; confirm contract specs/charges from NSE + broker.
 Gate: can authenticate and stream one Nifty tick to a log.

**Phase 1 — Recorder + backfill (weeks 2–3)**
 Collector (spot, futures, option chain ATM±N strikes x 2 expiries, depth, OI, volume) -> Redis -> Parquet; reconnect/heartbeat logic; NSE bhavcopy + VIX + participant OI backfill; broker candle backfill incl. expired contracts if available.
 Gate: 5 consecutive full sessions recorded with < 0.5% gaps and verified timestamps. **Leave it running from now on.**

**Phase 2 — Analytics engine + dashboard (weeks 3–5)**
 Black-76 pricer, IV solver, SVI surface, greeks, moneyness buckets, chain features, flow features (tick rule, CVD, OBI, microprice), liquidity/slippage estimator; dashboard v1.
 Gate: our IV/greeks reconcile with broker/vendor values within tolerance; arbitrage checks pass; dashboard live.

**Phase 3 — Labels, baselines, honest backtester (weeks 5–9)**
 Replay engine, cost model, triple-barrier labels, HAR-RV + LightGBM baselines, calibration, purged CV, experiment tracking.
 Gate: a baseline beats naive benchmarks out-of-sample in at least one regime, net of costs — or we document "no edge" and rethink features/horizons.

**Phase 4 — Vol model + structure selector (weeks 9–12)**
 Vol-forecast stack, VRP signal, Monte Carlo EV engine, strike/structure chooser, liquidity filters.
 Gate: pre-registered out-of-sample test meets section-1 thresholds under cost stress.

**Phase 5 — Risk engine + paper trading (weeks 12–20, includes 4–8 live weeks)**
 Full risk module, watchdogs, kill switch, Telegram, execution simulator with pessimistic fills.
 Gate: paper results within expected band; zero unhandled-failure incidents in the last 3 weeks.

**Phase 6 — Micro-live (weeks 20–28+)**
 Enable order scope on a separate key; static IP whitelisted; 1 lot; limit orders; daily reconciliation.
 Gate to scale: pre-registered criteria met; otherwise stay small or stop.

**Phase 7 — Operate & improve (ongoing)**
 Retrain schedule, drift monitoring, weekly review, post-mortems, new features via the same gated process.

---------------------------------------------------------------------
## 12. PROPOSED REPO LAYOUT
---------------------------------------------------------------------
```
trade_brain_algorithm/
  PLAN.md
  .env.example            # names only, no secrets
  pyproject.toml  Dockerfile  docker-compose.yml
  configs/                # risk limits, instruments, thresholds (versioned)
  src/tbrain/
    brokers/              # adapters: upstox.py dhan.py kite.py (swap-able)
    collector/            # ws client, normalizer, recorder, health
    data/                 # parquet/timescale IO, nse_eod, macro, news
    analytics/            # black76.py iv_solver.py svi.py greeks.py moneyness.py
    features/             # chain.py flow.py liquidity.py vol.py context.py
    labels/               # triple_barrier.py meta_labels.py
    models/               # regime/ vol/ direction/ calibration/ registry.py
    strategy/             # structures.py ev_engine.py selector.py
    risk/                 # limits.py watchdogs.py killswitch.py sizing.py
    execution/            # order_fsm.py slicer.py reconcile.py
    backtest/             # replay.py costs.py cv.py metrics.py reports.py
    monitor/              # dashboard/ alerts/ reports/
    llm/                  # event_risk.py daily_brief.py postmortem.py  (slow path only)
  tests/                  # unit + replay regression tests + property tests for pricer
  notebooks/              # research only
  docs/                   # decisions log, experiment log, runbook
```

---------------------------------------------------------------------
## 13. OPEN DECISIONS / WHAT I NEED FROM YOU
---------------------------------------------------------------------
1. Which broker account do you hold (Zerodha / Upstox / Dhan / Angel One / Fyers / other)?
2. Capital you'd risk in Phase 6 (sets position and loss limits — number stays on your machine/config, not needed in chat).
3. Horizon preference: intraday only (square-off daily), or also allow carrying? (v1 recommendation: intraday only.)
4. Budget: free-only data vs willing to pay ~Rs 500/mo for a data plan, and later for historical vendors.
5. Weekly expiry day trading: include or exclude in v1? (Higher gamma, higher risk; recommend separate validated sub-strategy later.)
6. Compute: AWS VPS in Mumbai OK? (needed for static IP + uptime.)

---------------------------------------------------------------------
## 14. SOURCES READ (2026-10-18) — re-verify before relying
---------------------------------------------------------------------
- Sahi — SEBI algo trading rules 2026: https://www.sahi.com/blogs/sebi-algo-trading-rules-2026-what-every-retail-trader-must-know-before-april
- Angel One — SmartAPI changes from 1 Apr 2026: https://oga-prod.angelone.in/news/market-updates/what-s-changing-in-angel-one-s-smartapi-access-from-april-1-2026
- Zerodha — Kite API charges: https://support.zerodha.com/category/trading-and-markets/kite-api/articles/what-are-the-charges-for-kite-apis
- Upstox community — F&O API access: https://community.upstox.com/t/how-to-enable-api-access-for-fetching-fno-futures-options-data-option-chain-historical-live-data/14000
- Dhan — trading APIs: https://dhanhq.co/trading-apis  (docs: https://docs.dhanhq.co/)
- Sahi — Nifty lot size 2026: https://www.sahi.com/blogs/nifty-lot-size-2026-bank-nifty-sensex
- Primary documents to read yourself: SEBI circular SEBI/HO/MIRSD/MIRSD-PoD/P/2025/0000013 and NSE retail-algo circulars.

---------------------------------------------------------------------
## 15. DECISION (2026-10-19): SIGNAL-ONLY MODE — the bot NEVER places orders
---------------------------------------------------------------------
The brain only SUGGESTS trades. Charan decides and executes manually. Consequences:
- No order code, no order-scope API key, no static IP needed. Phase 6 becomes "live signals", not "micro-live orders".
- Personal use only: publishing/selling these calls to others would need SEBI Research Analyst registration.

### What a signal contains
| Field | Example | How it is computed |
|---|---|---|
| Instrument | NIFTY 23150 CE 27-Oct | structure selector: best EV after costs among liquid strikes (spread/depth filter) |
| Direction / thesis | Long call, "bullish 15–30 min" | direction + vol models, regime filter |
| Entry zone | 61.5 – 63.0 (limit) | current bid/ask + max chase; signal void if price runs past it |
| Stop loss | premium 52.0 (= underlying 23090) | thesis-invalidation level on the UNDERLYING (forecast lower quantile / swing level, >= k x noise), converted to premium by full re-pricing (Black-76 incl. theta + IV move), not just delta |
| Targets | T1 70.0 · T2 78.0 (= underlying 23190 / 23230) | forecast upper quantiles, same re-pricing |
| Time stop | exit by 10:45 if neither hit | forecast horizon; theta makes stale option trades bleed |
| Confidence | 61% (calibrated) | probability of hitting T1 before SL, from validated model; shown with its historical hit-rate |
| Edge | EV +₹420/lot after costs, R:R 1.8 | simulation incl. brokerage/STT/fees/slippage |
| Size hint | max 1 lot for ₹X risk | from user's risk-per-trade setting |
| Why | "put writing at 23100, CE short covering, flow +0.4, IV cheap vs forecast" | top feature contributions |
| Invalidation | "cancel if NIFTY < 23090 before entry" | |

### Rules for issuing a signal (all must pass)
- Model is VALIDATED for the current regime (else UI shows "observing, no calls").
- Calibrated P(T1 before SL) >= threshold AND EV after costs > 0 AND R:R >= 1.5 AND spread/depth OK.
- No scheduled high-impact event inside the horizon; data feed healthy.
- Never "sure": every call shows its probability and the model's live track record.

### Every signal is auto-scored (the honest scoreboard)
Each signal is logged at issue time and tracked tick-by-tick: hit T1/T2/SL/time-stop, MFE/MAE, slippage-adjusted P&L.
The UI shows the running hit-rate, avg R, and calibration ("calls at 60% won 58%"). This paper track record is the only
evidence that decides whether the signals deserve trust.

---------------------------------------------------------------------
## 16. v1 SIGNAL ENGINE SHIPPED (2026-10-19) — rule-based, EXPERIMENTAL
---------------------------------------------------------------------
Rules + gates as in README. It is a *hypothesis*, not a validated model: promotion criteria stay those of section 1/9.
Decision path is deterministic code (no LLM): auditable, millisecond-fast, nothing invented. An LLM may later write
post-trade summaries from the same numbers, never decide. Exchange data is anonymous: evidence shows WHAT traded
(size, side, time, price), never WHO.
