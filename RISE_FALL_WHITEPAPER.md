# J81 Trade Desk — Rise/Fall Section White Paper

**Version 1.0 · Honest live-data interface for Deriv's Rise/Fall (CALL/PUT) contracts on synthetic volatility indices.**

---

## 1. Executive Summary

The Rise/Fall section is the J81 Trade Desk's live trading interface for Deriv's **CALL / PUT** contracts on the 10 synthetic volatility indices (R_10, R_25, R_50, R_75, R_100, 1HZ10V, 1HZ25V, 1HZ50V, 1HZ75V, 1HZ100V). It combines real-time tick streaming, a price chart with projected volatility cones, a multi-window market read, a 10-market scanner with z-score confidence ranking, two modes of automated trading, auto best-market selection, fast same-socket buy + settlement, and a per-trade expected-value (EV) strip.

Everything is built on one commitment: **be honest.** Deriv's synthetics are an audited random-number generator (RNG) with a built-in house edge — no tool predicts the next tick, and the Rise/Fall section is positioned accordingly as **observation, timing, and risk discipline**, not prediction.

---

## 2. Honest Framing (Read This First)

- Deriv synthetic indices are **independent ~50/50 ticks**. Empirical check on 1,000+ tick windows: P(up | prev up) ≈ 49.2%, P(up | prev down) ≈ 49.0% — no exploitable momentum.
- Rise/Fall pays a typical **~1.94×** multiplier per $1 staked → break-even win-rate ≈ **51.55%**, when the real win-rate is **50%**. The gap is the **house edge** (~1.5–3% per trade).
- Therefore: **no algorithm, indicator, streak counter, or "confidence" reading can give a sustained edge on Rise/Fall.** The Rise/Fall section is a sharp instrument for *seeing* the market and *managing risk*, not for *beating* it.
- The system's own EV strip and proven-strategy store make this honest in real time: EV reads negative, the proven store stays empty.

---

## 3. Architecture

```
                ┌──────────────────────────────────────┐
                │  Deriv public WS (no auth)           │
                │  wss://api.derivws.com/.../ws/public │
                └──────────────────┬───────────────────┘
                                   │ live ticks
       ┌───────────────────────────┴────────────────────────────┐
       │                                                        │
 Browser LiveWS (single symbol)                 Analyser scanner (Python)
 - 140-tick rolling buffer                      - 120-tick per market
 - renderChart  (price + cones)                 - z-score confidence
 - renderDeepRead  (multi-window + chips)       - ranked top-N
       │                                                        │
 Browser RFScan (10 markets parallel)             ▲ /scan/rise_fall (HTTP)
 - 60-tick per-market buffer                            │
 - ranked table + confidence gauge                      │
 - sets window._rfTop / _rfConf / _rfReady              │
       │                                          server-side RF loop:
       │                                          trading_loop._run_rf_account
       │                                                        │
   user taps RISE / FALL                                        │
       │                                                        │
       └──► bot POST /trade/manual ───► executor.execute_manual_trade
                                              │
                                              ▼
                                  Deriv NEW Options API
                            (OTP-WS → proposal → buy → poc/settle)
                                              │
                                              ▼
                              inline won / lost + profit returned
```

Key implementation files:

- UI ([bot-app/app/web/index.html](bot-app/app/web/index.html))
  - Rise/Fall panel container: [`#rf-panel`](bot-app/app/web/index.html#L534)
  - 10-market scanner panel: [`#rf-scan-panel`](bot-app/app/web/index.html#L551)
  - Multi-window deeper read: [`renderDeepRead`](bot-app/app/web/index.html#L963)
  - Single-symbol live WS: [`LiveWS`](bot-app/app/web/index.html#L1059)
  - Price chart + cones: [`renderChart`](bot-app/app/web/index.html#L1132)
  - 10-market scanner: [`RFScan`](bot-app/app/web/index.html#L1172)
  - Manual trade dispatch: [`placeTrade`](bot-app/app/web/index.html#L1326)
  - Foreground auto loop: [`rfAutoTick`](bot-app/app/web/index.html#L1552)
  - Server-side auto enable: [`rfStartServer`](bot-app/app/web/index.html#L1521)
- Server ([bot-app/app/...](bot-app/app/main.py))
  - Pre-trade EV math: [`_structural_winprob` + `/quote`](bot-app/app/main.py#L739)
  - Server RF auto runner: [`TradingLoop._run_rf_account`](bot-app/app/trading_loop.py#L201)
  - Auth + retry wrapper: [`tokens.with_fresh_token`](bot-app/app/tokens.py)
  - Same-socket settlement: [`deriv_new.buy(settle_wait=…)`](bot-app/app/deriv_new.py)
  - Live execution: [`executor._live_buy` / `execute_manual_trade`](bot-app/app/executor.py)
- Analyser
  - Per-market score (z-confidence): [`scanner._rf_score`](analyser-app/app/scanner.py#L63)
  - Multi-market scan: [`scanner.scan_rise_fall`](analyser-app/app/scanner.py#L82)
  - HTTP endpoint: [`/scan/rise_fall`](analyser-app/app/main.py#L351)
  - Market list: [`SCAN_SYMBOLS`](analyser-app/app/scanner.py#L19)

---

## 4. UI Components

### 4.1 Live Price Chart (LiveWS + `renderChart`)
- 600×120 SVG, redrawn on each new tick (~1 / sec on 1HZ* markets, ~0.5 / sec on R_*).
- **Left 64%** of the canvas = real Deriv price history (white line, last 80 ticks).
- **Right 36%** = forward **volatility cones**:
  - Green polygon = the +N√t band above the current spot (the "rise zone").
  - Red polygon = the −N√t band below (the "fall zone").
  - These are **NOT a direction prediction.** They show the symmetric ± range the price is likely to wander in over the contract's duration, sized by the recent local volatility (std of last ~40 moves).
- Above the chart: real-time stats line — `up X% · down Y% · run N▲` — over the last 80 ticks.

### 4.2 Deeper Live Read (`renderDeepRead`)
A row of chips, updated per tick from the LiveWS buffer:
| Chip | What it shows |
|---|---|
| **10t up** | up% over last 10 ticks (green if ≥55%, red if ≤45%) |
| **30t up** | up% over last 30 ticks |
| **60t up** | up% over last 60 ticks |
| **streak** | consecutive ups (▲) or downs (▼) ending at the current tick |
| **momentum** | net price change over the last 20 ticks (as % of price) |
| **volatility** | label — `calm` / `active` / `wild` from per-tick std relative to price |
| **range** | where current price sits in the recent 60-tick high–low band (%) |

Plus a one-line honest read: e.g.,
> *"short-term upward lean; 4 in a row up · active volatility. Ticks are ~50/50 — use this to time entries, not to predict."*

### 4.3 10-Market Scanner (RFScan)
- One WebSocket to Deriv's public WS, subscribing to all 10 markets in parallel.
- Per-market 60-tick rolling buffer.
- Score = `|up% − 50|` (raw deviation from fair).
- Renders a ranked table (top market highlighted) and a confidence gauge (0–99).
- Exposes `window._rfTop`, `window._rfReady`, `window._rfConf` for the foreground auto loop.
- Render cadence: every 2 s (intentionally throttled to reduce mobile DOM churn).

### 4.4 Auto-volatility (Best-Market)
A checkbox in `#rf-scan-panel` (`#autovol`, default ON) that auto-syncs the trade-symbol selector (`#t-symbol`) to the top-ranked market on every scan render. The `#t-symbol` is disabled while auto-volatility is on, so both manual taps and auto trades follow the strongest live signal.

> **Honest note:** "strongest live signal" = strongest **recent observed deviation**. The next tick is still ~50/50. This optimises *which market you watch*, not *whether you can predict*.

---

## 5. Trade Execution Path

### 5.1 Manual (Tap RISE/FALL)
1. Frontend (`placeTrade`): builds the trade body from `#t-symbol`, `#t-stake`, `#t-duration`, plus button's `data-dir`. **Fire-and-forget** — the button does not lock; you can place the next trade immediately.
2. Bot `POST /trade/manual` → `execute_manual_trade(account, ...)`.
3. Money safety on manual trades: **DRY_RUN** master switch, $0.35 minimum stake — and that's it. Take-profit, stop-loss, daily cap apply only to the autonomous loop (where they belong). The human pressing the button is the authorization.
4. `_live_buy` → `tokens.with_fresh_token(account_id, _do)`:
   - `_do(token)`: `deriv_new.request_otp_ws(token, loginid)` → OTP'd WS URL (with 429 backoff: 1.5 s → 3 s → 5 s).
   - `deriv_new.buy(ws_url, settle_wait=…)`:
     1. Send `{proposal:1, contract_type:CALL/PUT, underlying_symbol, duration, stake, basis:stake}` → receive `proposal.id` + `ask_price`.
     2. Send `{buy: proposal.id, price: ask_price}` → receive `buy` (contract_id, payout, buy_price).
     3. **Same-socket settlement watch**: send `{proposal_open_contract:1, contract_id, subscribe:1}`; loop reads until `poc.is_sold` (bounded by `settle_wait`, capped at 6 s for snappy returns).
   - If settled inline → `buy["_settled"] = {is_sold, status, profit, payout, app_markup_amount}`.
5. `execute_manual_trade` persists via `store.record_trade` → if `_settled` present, `store.settle_trade(...)` writes won/lost + profit immediately and returns the final outcome to the frontend.
6. Frontend banner: 🎉 **WON +$X** / ✗ **LOST −$X** / *result appears below as it settles ↓*; `refreshLive()` updates the balance bar and "Live results" strip.

### 5.2 Foreground Auto (this tab) — `rfAutoTick` every 2 s
- Pre-requisite: in `#auto-bg` checkbox UNCHECKED.
- Reads `window._rfTop`, `window._rfReady`, `window._rfConf`. If `confidence ≥ window.RF_THRESHOLD` (default 60), places a trade through `/trade/manual` using `_rfTop.code` (market), direction from up%≥50, `stake` from the auto-config, `duration` from `#t-duration`.
- Stops on rounds cap, "left Rise/Fall" tab navigation, or manual Stop.

### 5.3 Server-side ("VPS") Auto — `TradingLoop._run_rf_account`
- Enabled per-account by `rfStartServer` PATCHing `rf_config:{enabled:true, min_conf, stake, duration}` on the account row.
- The trading loop runs every `TRADE_POLL_SECONDS` (default 120 s). For each enabled account, in order:
  1. `_run_proven_account(acct)` — primary if `proven_auto` is set on the account.
  2. `_run_rf_account(acct)`:
     - `goal_status(acct)` → if take-profit hit or loss-limit reached → set `rf_config.enabled=false`, `enabled=false`, log "stopped" with the reason.
     - `get_rise_fall_scan()` → analyser `/scan/rise_fall` (with the bot's analyser proxy / direct call).
     - Top market + its z-score confidence.
     - If `confidence ≥ rf_config.min_conf` → `execute_manual_trade(trade_type="rise_fall", symbol=top.symbol, direction=top.direction, ...)`.
     - Otherwise logs "waiting — confidence X% < Y%".
- Runs 24/7 on the host: survives the user closing their browser.

### 5.4 EV Strip (Per-Trade Math)
Above the RISE/FALL buttons sits a live strip rendered from `/quote`:
> 📐 Payout **1.95×** · break-even **51.3%** · win chance ~**50%** · EV **−$0.05** · **negative EV — house edge**

- `payout`: real Deriv `proposal` (no auth, no account).
- `win_prob`: structural, **50%** for Rise/Fall.
- `expected_value`: `0.5 × stake × (payout − 1) − 0.5 × stake`.
- `break_even_pct`: `100 × 1/payout`.
- Updates on change of market / stake / ticks (debounced ~400 ms).

---

## 6. Math Reference

| Quantity | Formula | Rise/Fall value |
|---|---|---|
| Win probability `p` | structural | **0.5** |
| Payout multiplier `r` | live `proposal.payout / stake` | ~**1.94** |
| Break-even win-rate | `1/r` | **51.55%** |
| Edge | `p − 1/r` | **−1.55%** |
| EV per $1 | `p·(r−1) − (1−p)` | **−0.03** |
| Kelly fraction | `(p·(r−1) − (1−p)) / (r−1)` | **0** (no edge) |
| Risk of ruin (20 units, no edge) | gambler's-ruin | **≈ 1.0** |
| Scanner confidence | `clip(|2·ups − mv|/√mv × (100/3), 0, 99)` | 70%+ occurs ~3.6% of scans |

---

## 7. Safety & Robustness

- **DRY_RUN master switch** — all real-money paths gated; default `true` in code, currently `false` on the deployed bot (live).
- **Per-trade money safety:**
  - Manual: $0.35 minimum (Deriv's rule); the **exact stake the user typed** is used (no global cap).
  - Auto loops: stake = `rf_config.stake` (or proven strategy's), `goal_status` (take-profit / loss-limit), `max_trades_per_day` (rounds cap), `account.enabled` opt-in.
- **OAuth resilience:**
  - `tokens.get_access_token` refreshes proactively within 120 s of expiry.
  - `tokens.with_fresh_token(account_id, fn)` retries once on any auth error (401/403/expired).
  - Deriv's Ory rejected `offline_access` for this app (silent refresh dormant); sessions last ~1 h, then the UI shows **"Reconnect Deriv ↻"** — a graceful, non-fatal prompt.
- **Rate-limit resilience:**
  - OTP request: 429-aware backoff (1.5 s, 3 s, 5 s) before raising.
  - Server-side settle throttle: ≥6 s between real settle passes per account.
  - Client post-trade poll: reduced burst (3 spaced checks).
  - Balance endpoint: cached 8 s; never 502s — returns `{balance: null, needs_reconnect}` on auth failure (no error storm).
- **Loop crash safety:** every background loop (`TradingLoop`, analyser monitor, scheduler, strategy cycle) is wrapped in `try / except asyncio.CancelledError / except Exception` — they never die.

---

## 8. Public HTTP Surface

Bot endpoints involved in Rise/Fall:
- `POST /trade/manual` — place trade (`account_id`, `trade_type`, `symbol`, `direction`, `stake`, `duration`, `duration_unit`).
- `GET /quote` — pre-trade math: payout, break-even, win_prob, EV, verdict.
- `PATCH /accounts/{id}` — toggle server-side RF auto via `rf_config:{enabled,min_conf,stake,duration}`.
- `GET /loop/status` — trading loop heartbeat + `last_summary` (what `_run_rf_account` did this cycle).
- `GET /trades?account_id=&limit=` — recent trades for the Live Results strip.
- `POST /accounts/{id}/settle` — on-demand settlement (throttled to 6 s).
- `GET /accounts/{id}/balance` — cached live balance (8 s TTL).

Analyser endpoints:
- `GET /scan/rise_fall?count=120` — ranked market scan with z-score confidence per market.

---

## 9. Operating Modes Quick Reference

| Mode | How to start | Decides when to trade | Lives where | Survives tab close? |
|---|---|---|---|---|
| **Manual** | Tap RISE / FALL | Human | Browser → bot | n/a |
| **Foreground auto** | Auto box → "Run in background" UNchecked → Start Auto | RFScan confidence ≥ threshold | Browser tab | ❌ |
| **Server auto** ("VPS") | Auto box → "Run in background" CHECKED → Start Auto | Analyser scan confidence ≥ `rf_config.min_conf` | Bot host | ✅ |
| **Proven auto** | Auto box → "Trade proven strategies" | Bot proven-strategy store (almost always empty on RNG) | Bot host | ✅ |

---

## 10. Known Limitations (The Truth)

1. **Confidence is strength-of-deviation, not a win probability.** On RNG it routinely sits in the 30–60% range; 70%+ is rare and not predictive.
2. **Projection cones are ±volatility ranges, not directions.** Half the chart is green, half red — by design, because randomness is symmetric.
3. **Auto-volatility picks the strongest *recent* bias, not the next direction.** The next tick remains ~50/50 regardless of how the gauge looks.
4. **The proven-strategy store will usually stay empty for Rise/Fall.** That's not a bug; it's the EV-gated bar correctly rejecting noise on RNG.
5. **Real demo performance over many trades:** a recent live run on `DOT91992522` settled to **−$6,121.36**. That is the house edge made visible — not a malfunction.
6. **No refresh tokens for this app** (Deriv's Ory does not grant `offline_access` to this client), so sessions last ~1 h. Code is ready to use refresh tokens the moment Deriv enables them.

---

## 11. Performance Envelope

- LiveWS: per-tick render. Chart redraw ≈ 1–2 ms; deep-read chip rebuild ≈ <1 ms.
- RFScan: 10 markets, 1 WS, render every 2 s.
- Manual trade → result on screen: typically **1.5–3 s** for a 1-tick contract (inline settlement on the buy socket).
- Server trading loop: 120 s cadence; one scan + (up to) one trade per enabled account per cycle.

---

## 12. Roadmap Hooks

- **Token refresh** is fully implemented; flips on automatically the moment Deriv permits `offline_access`. No code change needed there.
- **Cycle proven-strategy auto** is already wired to Rise/Fall variants — when any market × variant passes the 5×100 + EV>0 bar (rare), the bot will trade it automatically via `_run_proven_account`.
- **Webhook auto-issue** (Stripe) for paid access is in place; the desk gates after Demo/Real account choice (paywall comes after the user picks an account).

---

## 13. Summary

The Rise/Fall section is, in effect, an **honest, fast, well-instrumented trading terminal**: world-class visibility into a market that cannot be predicted. Its value is in *seeing* (live read, scanner), *executing* (fast same-socket settlement, no rate-limit storms, fire-and-forget UI), and *disciplining* (EV strip, stop-loss/take-profit, daily caps). It does not — and cannot — beat the house edge on an audited RNG. Users who treat it as a precision tool for risk-aware speculation are well served. Users seeking a "win-rate machine" will lose, and that's true on every Rise/Fall platform.

---

*Document version 1.0. For implementation questions, see the file:line references in §3.*
