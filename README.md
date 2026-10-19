# Trade Brain — Nifty 50 + Sensex options

A personal research tool that **records** live Nifty 50 (NSE) and Sensex (BSE) option data from Upstox, **analyses**
it (own IV/Greeks, OI, order flow, big prints), shows everything in a **live brain UI**, and issues **signal-only
calls** (option, expiry, entry, stop loss, targets, reasons) that it then tracks and scores.

> **It never places orders.** You decide and trade manually. The signal engine is **EXPERIMENTAL** (rule-based, no
> track record yet). Calls are probabilities, never certainties. Not investment advice. Personal use only —
> sharing or selling calls to others requires SEBI Research Analyst registration.

---

## Contents
1. [How it works](#1-how-it-works)
2. [One-time setup](#2-one-time-setup)
3. [Daily routine](#3-daily-routine-the-runbook)
4. [Reading the UI](#4-reading-the-ui)
5. [Taking a trade from a call](#5-taking-a-trade-from-a-call-manual)
6. [Commands](#6-commands)
7. [Configuration](#7-configuration-env)
8. [Data on disk](#8-data-on-disk)
9. [Troubleshooting](#9-troubleshooting)
10. [Development](#10-development)
11. [Roadmap](#11-roadmap)

---

## 1. How it works

```
 Upstox market feed (websocket, TLS-verified)
        │  ticks: LTP, 5-level depth, OI, volume, IV, Greeks, buy/sell qty
        ▼
 ┌─ Recorder (tbrain record) ───────────────────────────────────────────────┐
 │  • saves every update → data/ticks/*.parquet   (+ option chain each 60 s) │
 │  • Brain, every 250 ms:                                                    │
 │      Senses   own Black-76 IV/Greeks, ITM/ATM/OTM, PCR, max pain, walls,   │
 │               1-min candles, CVD, order-book imbalance, big prints         │
 │      Judgement 7-signal confluence + trigger + safety gates → CALL         │
 │      Mirror   tracks each call (T1 → breakeven, T2 / SL / time stop),      │
 │               scores it → data/signals/calls.jsonl                         │
 │  • UI server (127.0.0.1:8765) pushes each snapshot to the browser (SSE)   │
 └────────────────────────────────────────────────────────────────────────────┘
        ▼
 Browser: live candles + SL/target lines, trade window with live P&L, OI ladder,
          IV smile, order flow, big-prints tape, sound + desktop alerts
```

The exact decision rules, in plain language, are in **[docs/ALGORITHM.md](docs/ALGORITHM.md)**.
The full long-term plan is in **[docs/PLAN.md](docs/PLAN.md)**.

---

## 2. One-time setup

**You need:** a Mac (or Linux), an **Upstox** account with F&O enabled, and ~15 minutes.

### 2.1 Install uv (Python manager)
```bash
brew install uv          # or see https://docs.astral.sh/uv/
```

### 2.2 Get the code and install
```bash
git clone <your-repo-url> trade_brain_algorithm
cd trade_brain_algorithm
uv sync                  # creates .venv and installs everything (Python is fetched automatically)
uv run pytest -q         # optional: all tests should pass, no internet or keys needed
```

### 2.3 Create the Upstox developer app (free)
1. Open **https://account.upstox.com/developer/apps** → *Create New App*.
2. **App Name:** anything (e.g. `brain_trading`).
3. **Redirect URL:** `http://localhost:5000/upstox_redirect` *(must match `.env` exactly)*.
4. Leave **Postback URL** and **Notifier Webhook** empty.
5. Create → copy the **API Key** and **API Secret**.

> Upstox allows one active API app per user. Market data is free; this project never uses order APIs.

### 2.4 Configure secrets
```bash
cp .env.example .env
open -e .env             # paste UPSTOX_API_KEY and UPSTOX_API_SECRET, save
```
`.env` is git-ignored. Never paste the key/secret into chat, issues or screenshots.

### 2.5 First check (works any time, even when the market is closed)
Run these **one at a time** (zsh doesn't accept `# comments` pasted after commands):
```bash
uv run tbrain login --manual
uv run tbrain instruments
uv run tbrain probe --seconds 30
uv run tbrain ui
```
- `login --manual`: opens the Upstox login URL. After logging in the browser lands on
  `localhost:5000/upstox_redirect?code=...` (a 403 / blank page is normal on a Mac — AirPlay uses port 5000).
  **Copy that full URL from the address bar and paste it into the terminal.** You should see `VALID`.
- `instruments`: should list NIFTY (lot 65) and SENSEX (lot 20) expiries.
- `probe`: connects to the live feed for 30 s and saves a sample.
- `ui`: opens the brain UI on the probe data (mode **DEMO**). Ctrl+C to stop.

---

## 3. Daily routine (the runbook)

Upstox requires a **fresh login every day**.

| Time (IST) | Do this |
|---|---|
| **08:40–08:55** | Plug in the charger, keep the lid open. In Terminal: `cd ~/Desktop/personal/trade_brain_algorithm` |
| | `uv run tbrain login --manual` → log in → paste the redirect URL → `VALID` |
| | `caffeinate -i uv run tbrain record` → the **UI opens automatically** at http://127.0.0.1:8765 |
| | In the UI click **🔕 Alerts off → 🔔 Alerts on** (browsers need one click to allow sound) and allow notifications |
| **09:00** | Recorder connects (it waits until 09:00 by itself) |
| **09:15** | Market opens → candles start building, header shows **LIVE**, data-age badge goes green |
| **≈09:30** | Earliest possible call (needs 15 one-minute candles; entry window is 09:25–14:45) |
| **During the day** | Watch *Brain thinking* to see why there is / isn't a call. On a call: chime + notification + banner |
| **15:35** | Recorder stops by itself (or Ctrl+C any time — data is saved) |
| **After close** | `uv run tbrain report` → coverage and gaps · `uv run tbrain review` → what worked / leaked today |

What the two processes are:
- **Backend** = `tbrain record` (recorder + brain + signal engine + UI server, all in one process).
- **UI** = your browser tab at `http://127.0.0.1:8765` (local only). Close/reopen it anytime; recording continues.
- When *not* recording, `uv run tbrain ui` shows the last session (mode **LAST SESSION**) or a probe replay (**DEMO**).

`caffeinate -i` keeps the Mac awake while recording. Closing the lid still sleeps the Mac (unless on an external display).

---

## 4. Reading the UI

| Panel | What it tells you |
|---|---|
| **Header** | Mode (LIVE / LAST SESSION / DEMO), market status, feed health, **data age** (screen age · exchange age · Upstox→us delay). Green < 1.5 s, amber < 4 s, red beyond |
| **Tiles** | Spot, future & basis, **our** ATM IV vs Upstox IV, ATM straddle (market-implied move), PCR, max pain + OI walls, 25Δ skew, India VIX |
| **Live chart** | 1-min candles from index ticks, VWAP (futures, basis-adjusted), ▲▼ where calls were issued, and the active call's **entry / SL / T1 / T2** lines |
| **Trade window** | The call: `BUY CE · NIFTY 25100 CE 2026-10-27`, entry zone, SL, T1, T2 (premium **and** index level), live **₹ P&L per lot** and R, time stop, every reason, and the exact big-print evidence; plus the option's own premium candles with the same lines |
| **Brain thinking · live** | Both directions scored /7 with ✓, the trigger, and which **gate** blocks a call right now |
| **Brain layers** | What is active: Senses on, Judgement *learning* (experimental rules), ML layers off until trained |
| **OI ladder** | Call/put OI per strike, ITM/ATM/OTM shading, ◆ walls, ΔOI, our IV, LTP; hover for bid/ask, spread, Greeks, build-up |
| **Volatility smile** | Our IV by strike (lines) vs Upstox IV (hollow dots) |
| **Order flow + big prints** | Top-5 order-book imbalance, resting buy vs sell qty, aggressor buy share (5 min), session CVD, and a tape of block-size prints |

Note: Upstox's own IV prices off spot, so it shows a large artificial call/put gap; **use ours** (Black-76 on the
put-call-parity forward).

---

## 5. Taking a trade from a call (manual)

The brain only suggests. If **you** choose to act on a call:

1. **Check it is still valid:** status **OPEN**, data-age badge green, and the option's price is inside the **entry zone**.
   If price has already run past the zone, skip it — chasing changes the risk/reward.
2. **Find the exact contract** in your broker app: underlying, **strike**, **CE/PE**, **expiry date** exactly as shown.
3. **Size:** set `TBRAIN_RISK_PER_TRADE` in `.env` and the UI shows how many lots fit your risk. Never size up because
   a call "looks sure".
4. **Enter** with a **limit** buy inside the entry zone (avoid market orders on options — spreads can be wide).
5. **Protect immediately:** place a **stop-loss sell** at the SL premium, and a **limit sell** at T1 (or use your
   broker's bracket / OCO / GTT feature if it offers one).
6. **At T1 (🔔):** the brain moves its stop to **breakeven** — do the same (modify your SL to your entry price), and
   either book profit or keep a portion for **T2**.
7. **Time stop:** if neither SL nor target is hit by the time shown, the brain closes the call — consider exiting.
8. **After the day:** the scoreboard records every call's outcome after estimated costs, so you can judge the rules on
   evidence, not feelings.

---

## 6. Commands

| Command | What it does |
|---|---|
| `uv run tbrain login [--manual]` | Daily Upstox login; saves the token to `.secrets/` (never printed) |
| `uv run tbrain check` | Is today's token valid? |
| `uv run tbrain instruments [--refresh]` | Download/inspect NIFTY + SENSEX contracts (expiries, lots, index keys) |
| `uv run tbrain probe [--seconds 30]` | Short live test; saves raw decoded samples to `data/probe/` |
| `caffeinate -i uv run tbrain record` | Full session: waits for 09:00, records, runs the brain + engine + UI, stops 15:35 |
| `uv run tbrain record --now --seconds 120` | Test run right now (ignores market hours) |
| `uv run tbrain record --no-ui` / `--no-browser` / `--ui-port 8766` | Variants |
| `uv run tbrain ui [--demo]` | UI without recording (last session or probe replay) |
| `uv run tbrain report [--date YYYY-MM-DD]` | Coverage + gap report for a recorded day |
| `uv run tbrain review [--date YYYY-MM-DD \| --all]` | Post-mortem of calls: by direction, hour, score, regime, outcome; stop/exit quality; **filter scorecard** from shadow calls |
| `uv run tbrain backtest --date YYYY-MM-DD [--compare]` | Replay a recorded day through the real brain + engine (presets v1 / v2 / v2.1 / v2.1+regime; default v2.1); fidelity check vs the live session |

---

## 7. Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `UPSTOX_API_KEY`, `UPSTOX_API_SECRET` | — | From your Upstox developer app |
| `UPSTOX_REDIRECT_URI` | `http://localhost:5000/upstox_redirect` | Must equal the app's Redirect URL |
| `TBRAIN_COST_PER_LOT` | `40` | Your **full** round-trip cost per lot (₹). Placeholder — set yours |
| `TBRAIN_RISK_PER_TRADE` | `0` | Max ₹ risk per call; enables the lot-size hint |
| `TBRAIN_SIGNAL_MIN_SCORE` | `5` | Conditions (of 7) that must agree; higher = fewer, stricter calls |
| `TBRAIN_REQUIRE_VWAP` | `1` | v2: futures must be on the call's side of VWAP |
| `TBRAIN_TREND_K` | `1.5` | v2: last-60-min index move must be ≥ k × ATR in the call's direction (`off` disables) |
| `TBRAIN_LOSS_PAUSE_N` | `0` | Pause an index after N consecutive SLs (off by default) |
| `TBRAIN_REGIME_GATE` | `0` | Breakout calls only in a `trend` regime |
| `TBRAIN_PREMIUM_STOP` | `0` | Also stop when the option bid+LTP hit the SL premium |
| `TBRAIN_FUT_OI_FRESH` | `0` | Futures-OI clue needs OI rising with price |
| `TBRAIN_N_STRIKES` / `TBRAIN_N_EXPIRIES` | `10` / `2` | Chain width recorded |
| `TBRAIN_UNDERLYINGS` | `NIFTY:NSE_FO,SENSEX:BSE_FO` | What to record |
| `TBRAIN_RISK_FREE` | `0.065` | Discount rate for Black-76 (placeholder) |

---

## 8. Data on disk

All runtime data is **git-ignored**.
```
data/ticks/date=YYYY-MM-DD/*.parquet   every feed update (one row per instrument per message)
data/chain/date=YYYY-MM-DD/*.parquet   REST option-chain snapshots (prev_oi, PCR, POP)
data/signals/calls.jsonl               every call + T1/close events (the scoreboard)
data/sessions/session_*.json           per-session manifest (subscriptions, health, gaps)
data/live/state.json                   last brain snapshot (for `tbrain ui` after hours)
data/instruments/contracts_*.json      filtered NIFTY/SENSEX contracts per day
.secrets/upstox_token.json             today's access token (chmod 600)
```
Back up `data/` yourself (e.g. an external drive) — it is the dataset future models learn from.

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `unrecognized arguments: # ...` | zsh doesn't treat pasted `#` as a comment — run commands without the comments |
| Login page ends on blank / **403** at `localhost:5000` | Normal (macOS AirPlay owns port 5000). Use `login --manual` and paste the address-bar URL |
| `state mismatch` | The pasted URL came from an older login; run `login --manual` again and use the new URL |
| `Token exchange failed: 403 ... 1010` | Cloudflare blocked the client signature; already handled (SDK User-Agent). Update the code and retry |
| `No token` / `EXPIRED` | Daily login needed: `uv run tbrain login --manual` |
| `Port 8765 busy` | The recorder already serves the UI — just open http://127.0.0.1:8765 |
| UI says **feed offline** / data age red | Check internet; the recorder auto-reconnects. Persistent → Ctrl+C and restart `record` |
| No calls all day | Expected on many days — open *Brain thinking* to see the failing conditions / gates |
| Mac slept, gaps in data | Use `caffeinate -i`, keep lid open, charger in |

---

## 10. Development

```bash
uv sync                     # install
uv run pytest -q            # tests: offline, no broker keys (CI runs these on every push)
```
Layout:
```
src/tbrain/
  brokers/upstox/   auth (daily OAuth), feed (TLS-verified V3 websocket), normalize, instruments, rest, http
  collector/        recorder (orchestration), store (parquet), health
  analytics/        black76 (IV/Greeks), chain (moneyness, PCR, max pain, walls, build-up)
  brain/            state (snapshot), candles, features (rolling history), demo (probe replay)
  signals/          engine (confluence rules, levels, tracking, scoreboard)
  ui/               server (SSE push) + static/index.html
tests/              unit + scenario + latency tests
docs/               PLAN.md, ALGORITHM.md
```
Security notes: TLS verification is **on** for the feed (the official SDK's feeder disables it; we reuse only its
protobuf schema). Secrets live only in `.env` / `.secrets/`. The UI binds to `127.0.0.1` only.

---

## 11. Roadmap
- [x] Phase 0–1: Upstox adapter, recorder, health, tests
- [x] Phase 2: own IV/Greeks, chain analytics, brain UI (push, 250 ms), live candles
- [x] Signal engine v1 (EXPERIMENTAL) with SL/targets, live P&L, scoreboard, alerts
- [x] Signal filters v2 (trend + VWAP required, fresh-OI clue, premium stop) — validated by replay
- [x] Shadow calls (every blocked candidate tracked → per-filter scorecard) + `tbrain review`
- [x] Replay backtester over recorded parquet (`tbrain backtest`) with fidelity check vs live
- [x] Regime brain (trend / range / mixed: efficiency, VWAP crosses, initial balance) + range-fade playbook in shadow
- [ ] Record 20+ sessions → evaluate each condition's real hit-rate; ML meta-labelling on shadow + live candidates
- [ ] Regime + volatility models, calibrated probabilities (replace rule score)
- [ ] Always-on server deployment (Mumbai VM) with a phone-friendly login link

---

*Personal research tool. Options trading carries substantial risk of loss. Nothing here is investment advice.*
