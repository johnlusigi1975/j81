"""J81 Analyser — Phase 1 HTTP service.

Receives strategies and insights from the J81 Researcher, persists them
to SQLite, and serves a local homepage showing what's been received,
grouped by trade type.

  POST /strategies     — Researcher pushes here
  POST /insights       — Researcher pushes here
  GET  /health
  GET  /stats          — totals + grouped counts
  GET  /strategies     — list (filters: trade_type, status, limit, offset)
  GET  /insights       — list (filters: trade_type, category, limit, offset)
  GET  /strategies/by_trade_type
  GET  /insights/by_trade_type
  GET  /               — homepage
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse

from pydantic import BaseModel, Field

from app.backtest import backtest_all
from app.comms import grade_research_batch
from app.config import APP_NAME, APP_VERSION, PRIORITY_TRADE_TYPES, get_settings
from app.decisions import decide as run_decide, research_gaps
from app.deriv import DerivError, fetch_ticks
from app.expressions import ExpressionError, evaluate_series
from app.indicators import compute as compute_indicator
from app.market_data import (
    cache_stats,
    ensure_candles,
    get_candles,
    live_digit_stats,
)
from app.monitor import monitor
from app import productivity
from app.research_feedback import FeedbackError, dispatch_gaps
from app.models import (
    IngestResult,
    InsightBatch,
    StoredInsight,
    StoredStrategy,
    StrategyBatch,
)
from app.store import get_store

@asynccontextmanager
async def lifespan(_: FastAPI):
    # Self-heal a near-full disk on boot: trim cached candles + acked comms and
    # compact. Best-effort so a bad cleanup never blocks startup.
    try:
        store = get_store()
        store.prune_comms()
        with store._lock:  # candles are pure cache — safe to drop, re-fetched
            store._conn.execute("DELETE FROM candles")
            store._conn.commit()
        store.vacuum()
    except Exception:
        pass
    monitor.ensure_running()  # 24/7 self-report + peer-watch
    try:
        from app.cycle import runner as cycle_runner
        cycle_runner.ensure_running()  # 30-min backtest→prove→push→auto-clear cycle
    except Exception:
        pass
    yield


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)

# The comms dashboard on the Researcher/Bot homepages reads this app's
# /comms/* cross-origin, so allow local origins (and any host for the
# deployed case — comms data is non-sensitive system chatter).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://.*",
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_HOMEPAGE = Path(__file__).parent / "web" / "index.html"


def require_token(authorization: str = Header(default="")) -> None:
    """Optional bearer guard. If INCOMING_API_KEY is unset, this is a no-op
    (open mode — fine for single-user local dev). If it's set, both POST
    endpoints require Authorization: Bearer <that>."""
    expected = get_settings().incoming_api_key
    if not expected:
        return
    token = authorization.removeprefix("Bearer ").strip()
    if token != expected:
        raise HTTPException(401, "invalid or missing api key")


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    """Serve the homepage with no-cache headers so a refresh on any device
    always gets the latest version (without this, browsers serve a stale copy
    of the SPA until their own cache expires)."""
    resp = FileResponse(_HOMEPAGE, media_type="text/html")
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/health")
def health() -> dict:
    s = get_settings()
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "status": "ok",
        "auth_required": bool(s.incoming_api_key),
        "priority_mode": get_store().get_state("priority_mode") == "1",
    }


# ---------------------------------------------------------------------------
# Priority mode — focus the whole tree on Rise/Fall + Even/Odd (master first)
# ---------------------------------------------------------------------------


@app.get("/priority")
def priority_get() -> dict:
    return {
        "enabled": get_store().get_state("priority_mode") == "1",
        "trade_types": list(PRIORITY_TRADE_TYPES),
    }


class PriorityBody(BaseModel):
    enabled: bool


@app.post("/priority")
def priority_set(body: PriorityBody) -> dict:
    get_store().set_state("priority_mode", "1" if body.enabled else "0")
    # Tell the other systems via the bus so they can echo it on their pages.
    try:
        from app.comms import emit
        emit(to_app="all", type="command", subject="priority mode",
             body=("Priority ON — focus only on Rise/Fall + Even/Odd."
                   if body.enabled else "Priority OFF — back to all trade types."),
             data={"priority_mode": body.enabled,
                   "trade_types": list(PRIORITY_TRADE_TYPES)},
             from_app="analyser")
    except Exception:
        pass
    return {"enabled": body.enabled, "trade_types": list(PRIORITY_TRADE_TYPES)}


@app.post(
    "/strategies",
    response_model=IngestResult,
    dependencies=[Depends(require_token)],
)
def ingest_strategies(batch: StrategyBatch) -> IngestResult:
    attempted, new, per_tt = get_store().record_strategies(batch.by_trade_type)
    return IngestResult(
        received=attempted,
        new=new,
        duplicates=attempted - new,
        by_trade_type=per_tt,
    )


@app.post(
    "/insights",
    response_model=IngestResult,
    dependencies=[Depends(require_token)],
)
def ingest_insights(batch: InsightBatch) -> IngestResult:
    attempted, new, per_tt = get_store().record_insights(batch.by_trade_type)
    return IngestResult(
        received=attempted,
        new=new,
        duplicates=attempted - new,
        by_trade_type=per_tt,
    )


@app.get("/stats")
def stats() -> dict:
    return get_store().stats()


@app.get("/strategies", response_model=list[StoredStrategy])
def list_strategies(
    trade_type: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[StoredStrategy]:
    return get_store().list_strategies(
        trade_type=trade_type, status=status, limit=limit, offset=offset
    )


@app.get("/insights", response_model=list[StoredInsight])
def list_insights(
    trade_type: str | None = None,
    category: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[StoredInsight]:
    return get_store().list_insights(
        trade_type=trade_type, category=category, limit=limit, offset=offset
    )


@app.get("/strategies/by_trade_type")
def strategies_grouped() -> dict[str, list[StoredStrategy]]:
    store = get_store()
    out: dict[str, list[StoredStrategy]] = {}
    for tt in store.stats()["strategies"]["by_trade_type"]:
        out[tt] = store.list_strategies(trade_type=tt, limit=1000)
    return out


@app.get("/insights/by_trade_type")
def insights_grouped() -> dict[str, list[StoredInsight]]:
    store = get_store()
    out: dict[str, list[StoredInsight]] = {}
    for tt in store.stats()["insights"]["by_trade_type"]:
        # store.list_insights treats trade_type="general" as IS NULL
        out[tt] = store.list_insights(trade_type=tt, limit=1000)
    return out


# ---------------------------------------------------------------------------
# Phase 2.1: Deriv market data
# ---------------------------------------------------------------------------


class DataFetchRequest(BaseModel):
    """Pull historical Deriv candles into the local cache."""

    symbol: str = Field(
        description="Deriv symbol, e.g. R_100, R_75, frxEURUSD, 1HZ100V"
    )
    granularity: int = Field(
        default=60,
        description="seconds per candle (60,120,300,600,900,1800,3600,7200,14400,28800,86400)",
    )
    days: float = Field(
        default=7.0, ge=0.1, le=60.0, description="how many days of history"
    )


@app.post("/data/fetch")
async def data_fetch(req: DataFetchRequest) -> dict:
    try:
        return await ensure_candles(
            symbol=req.symbol,
            granularity=req.granularity,
            days=req.days,
        )
    except DerivError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # network / WS failures
        raise HTTPException(status_code=502, detail=f"deriv fetch failed: {exc!r}")


@app.get("/data/candles")
def data_candles(
    symbol: str,
    granularity: int = 60,
    start: int | None = None,
    end: int | None = None,
    limit: int = 5000,
) -> list[dict]:
    return get_candles(
        symbol=symbol, granularity=granularity, start=start, end=end, limit=limit
    )


@app.get("/data/cache_stats")
def data_cache_stats() -> dict:
    return cache_stats()


@app.get("/scan/even_odd")
async def scan_even_odd_endpoint(count: int = 120) -> dict:
    """Rank all synthetic markets by their current even/odd edge (real ticks).
    Powers the Bot's 'M Pro' scanner + auto-cycle engine."""
    from app.scanner import scan_even_odd
    try:
        return await scan_even_odd(count=count)
    except Exception as exc:
        raise HTTPException(502, f"scan failed: {exc!r}")


@app.get("/even_odd/analyze")
async def even_odd_analyze(
    symbol: str = "R_100",
    count: int = 200,
    strategy: str = "reversion",
    z_enter: float = 2.0,
    streak_enter: int = 6,
    min_samples: int = 60,
) -> dict:
    """Full Even/Odd analytics for ONE market: descriptive stats (even/odd %,
    z-score, runs/randomness test, streaks, entropy) + a gated signal with
    confidence. Honest: synthetics are an RNG, so this filters & disciplines
    rather than predicts. Callable by the Bot (M Pro) and the Researcher."""
    from app import even_odd
    try:
        data = await fetch_ticks(symbol, count=max(20, min(count, 2000)))
    except DerivError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"deriv ticks fetch failed: {exc!r}")
    digits = even_odd.last_digits(data["prices"], data["pip_size"])
    sig = even_odd.even_odd_signal(
        digits, strategy=strategy, z_enter=z_enter,
        streak_enter=streak_enter, min_samples=min_samples)
    return {"symbol": symbol, "count": len(digits), "strategy": strategy, **sig}


@app.get("/even_odd/backtest")
async def even_odd_backtest(
    symbol: str = "R_100",
    count: int = 2000,
    window: int = 120,
    strategy: str = "reversion",
    z_enter: float = 2.0,
    streak_enter: int = 6,
    min_samples: int = 60,
    payout: float = 1.92,
) -> dict:
    """Honest walk-forward backtest of an Even/Odd config on real history, so
    expected win-rate (~50% minus spread) is visible before risking money.
    The Researcher uses this to tune thresholds and set realistic odds."""
    from app import even_odd
    try:
        data = await fetch_ticks(symbol, count=max(window + 50, min(count, 5000)))
    except DerivError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"deriv ticks fetch failed: {exc!r}")
    digits = even_odd.last_digits(data["prices"], data["pip_size"])
    result = even_odd.backtest(
        digits, payout=payout, window=window, strategy=strategy,
        z_enter=z_enter, streak_enter=streak_enter, min_samples=min_samples)
    return {"symbol": symbol, "ticks": len(digits), "window": window,
            "strategy": strategy, **result}


@app.get("/scan/rise_fall")
async def scan_rise_fall_endpoint(count: int = 120) -> dict:
    """Rank all synthetic markets for Rise/Fall by live confidence (z-score of
    the recent up/down split). Powers the Bot's server-side gated auto-runner."""
    from app.scanner import scan_rise_fall
    try:
        return await scan_rise_fall(count=count)
    except Exception as exc:
        raise HTTPException(502, f"scan failed: {exc!r}")


# ---------------------------------------------------------------------------
# Live Testing Lab — paper-trade $1 across ALL trade types on live ticks so you
# can WATCH the analyser try everything. In-memory only (never touches disk).
# ---------------------------------------------------------------------------


@app.post("/lab/tick")
async def lab_tick(symbol: str = "R_100") -> dict:
    """Run one round: a $1 paper trade for every trade type on `symbol`'s latest
    ticks, scored with real payouts. The UI polls this to stream the action."""
    from app import lab
    try:
        return await lab.run_round(symbol)
    except Exception as exc:
        raise HTTPException(502, f"lab round failed: {exc!r}")


@app.get("/lab/stats")
def lab_stats() -> dict:
    from app import lab
    return lab.snapshot()


@app.post("/lab/reset")
def lab_reset() -> dict:
    from app import lab
    return lab.reset()


# ---------------------------------------------------------------------------
# Calculator — the shared fast math engine the systems call when they need
# numbers (EV, break-even, Kelly, risk-of-ruin) or any safe arithmetic.
# ---------------------------------------------------------------------------


class CalcEval(BaseModel):
    expr: str
    vars: dict | None = None


@app.post("/calc/eval")
def calc_eval(body: CalcEval) -> dict:
    """Evaluate one arithmetic expression safely (math + trading helpers)."""
    from app import calc
    try:
        return {"expr": body.expr, "result": calc.safe_eval(body.expr, body.vars)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/calc/trade")
def calc_trade(stake: float = 1.0, payout: float = 1.95, win_prob: float = 0.5) -> dict:
    """Full pre-trade math: EV, break-even win-rate, edge, Kelly stake, verdict."""
    from app import calc
    out = calc.trade_summary(stake=stake, payout=payout, win_prob=win_prob)
    out["risk_of_ruin_20u"] = calc.risk_of_ruin(win_prob, payout, 20)
    return out


# ---------------------------------------------------------------------------
# Strategy cycle — backtest→prove(70%×100×5 + EV)→push to bot→auto-clear (30 min)
# ---------------------------------------------------------------------------


@app.get("/cycle/status")
def cycle_status() -> dict:
    from app import cycle
    return cycle.status()


@app.post("/cycle/run")
async def cycle_run(push: bool = True) -> dict:
    """Run one cycle now (instead of waiting for the 30-min timer)."""
    from app import cycle
    try:
        return await cycle.run_cycle(push=push)
    except Exception as exc:
        raise HTTPException(502, f"cycle failed: {exc!r}")


@app.post("/cycle/pause")
def cycle_pause() -> dict:
    """Pause the 30-min automatic cycle — the runner keeps spinning but skips
    work until /cycle/resume is called. Used while restructuring the tree."""
    from app import cycle
    return cycle.pause()


@app.post("/cycle/resume")
def cycle_resume() -> dict:
    from app import cycle
    return cycle.resume()


@app.get("/even_odd/payouts")
async def even_odd_payouts(stake: float = 1.0, duration: int = 1) -> dict:
    """Compare the LIVE Even/Odd payout across all 10 synthetic markets and
    name the best one. This is the single genuine EV lever for Even/Odd: the
    digits are an audited uniform RNG (no predictable bias — verified over
    thousands of ticks), but Deriv quotes slightly different payouts per
    market, so always trading the highest-payout market recovers real EV.
    The Bot's M Pro / decision can call this to choose where to trade."""
    from app.deriv import fetch_proposal_payout
    from app.scanner import SCAN_SYMBOLS
    rows = []
    for code, name in SCAN_SYMBOLS:
        try:
            q = await fetch_proposal_payout(code, contract_type="DIGITEVEN",
                                            duration=duration, stake=stake)
            rows.append({"symbol": code, "name": name, **q})
        except Exception as exc:
            rows.append({"symbol": code, "name": name, "payout_pct": None,
                         "error": str(exc)[:80]})
    ranked = sorted([r for r in rows if r.get("payout_pct")],
                    key=lambda r: r["payout_pct"], reverse=True)
    best = ranked[0] if ranked else None
    return {
        "stake": stake, "duration": duration, "best": best, "ranked": ranked,
        "note": "Higher payout% = lower house edge. Even/Odd is ~50/50 on an "
                "audited RNG; payout selection is the only real EV edge.",
    }


@app.get("/data/digit_stats")
async def data_digit_stats(symbol: str, count: int = 1000) -> dict:
    """Real-tick last-digit distribution — the honest basis for digit
    trades (even/odd, over/under, matches/differs). Reads actual ticks,
    not candle closes."""
    try:
        return await live_digit_stats(symbol, count=count)
    except DerivError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"deriv ticks fetch failed: {exc!r}")


# ---------------------------------------------------------------------------
# Phase 2.2: indicators + safe expression evaluation
# ---------------------------------------------------------------------------


class IndicatorSpec(BaseModel):
    ref: str = Field(description="how the expression references this, e.g. 'rsi'")
    type: str = Field(description="RSI | SMA | EMA | MACD | BBANDS | STOCH | ATR")
    params: dict[str, float | int] = Field(default_factory=dict)


class IndicatorComputeRequest(BaseModel):
    symbol: str
    granularity: int = 60
    indicators: list[IndicatorSpec]
    limit: int = Field(
        default=500, ge=10, le=50000,
        description="how many of the most-recent candles to compute on",
    )


def _load_candles_or_404(symbol: str, granularity: int, limit: int):
    rows = get_candles(symbol=symbol, granularity=granularity, limit=limit)
    if not rows:
        raise HTTPException(
            404,
            f"no cached candles for {symbol} @ {granularity}s — pull some via /data/fetch first",
        )
    return rows


@app.post("/indicators/compute")
def indicators_compute(req: IndicatorComputeRequest) -> dict:
    candles = _load_candles_or_404(req.symbol, req.granularity, req.limit)
    series: dict[str, dict] = {}
    for spec in req.indicators:
        try:
            series[spec.ref] = compute_indicator(candles, spec.type, dict(spec.params))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    return {
        "symbol": req.symbol,
        "granularity": req.granularity,
        "candles": len(candles),
        "indicators": series,
        "epochs": [c["epoch"] for c in candles],
    }


class ExpressionTestRequest(BaseModel):
    symbol: str
    granularity: int = 60
    indicators: list[IndicatorSpec] = Field(default_factory=list)
    expression: str = Field(
        description="boolean rule, e.g. 'rsi < 30 and close > open' or 'last_digit == 0'"
    )
    limit: int = Field(default=500, ge=10, le=50000)


@app.post("/expressions/test")
def expression_test(req: ExpressionTestRequest) -> dict:
    """Evaluate `req.expression` per bar against cached candles + the
    indicators you list. Returns the boolean series + a count of True
    bars so you can sanity-check a rule before backtesting it."""
    candles = _load_candles_or_404(req.symbol, req.granularity, req.limit)
    indicator_values: dict[str, dict] = {}
    for spec in req.indicators:
        try:
            indicator_values[spec.ref] = compute_indicator(
                candles, spec.type, dict(spec.params)
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    try:
        signals = evaluate_series(req.expression, candles, indicator_values)
    except ExpressionError as exc:
        raise HTTPException(400, str(exc))
    true_count = sum(1 for s in signals if s)
    last_30 = [
        {"epoch": candles[i]["epoch"], "signal": signals[i]}
        for i in range(max(0, len(signals) - 30), len(signals))
    ]
    return {
        "symbol": req.symbol,
        "granularity": req.granularity,
        "expression": req.expression,
        "bars_evaluated": len(signals),
        "true_signals": true_count,
        "signal_rate": round(true_count / len(signals), 4) if signals else 0.0,
        "last_30_bars": last_30,
    }


# ---------------------------------------------------------------------------
# Phase 2.3: the brain — backtest, decide, request more research
# ---------------------------------------------------------------------------


class BacktestRunRequest(BaseModel):
    """Run a backtest across all stored strategies (or one)."""

    strategy_id: str | None = None
    symbol: str | None = Field(
        default=None,
        description="override the symbol; default uses each strategy's own",
    )
    granularity: int = 60


@app.post("/backtest/run")
def backtest_run(req: BacktestRunRequest) -> dict:
    store = get_store()
    if req.strategy_id:
        rows = [r for r in store.list_strategies_raw() if r["id"] == req.strategy_id]
        if not rows:
            raise HTTPException(404, f"strategy {req.strategy_id} not found")
    else:
        rows = store.list_strategies_raw()
    if not rows:
        return {"tested": 0, "results": [], "note": "no strategies in the DB"}
    results = backtest_all(rows, symbol=req.symbol, granularity=req.granularity)
    for r in results:
        if "error" not in r:
            store.record_backtest(r)
    # The brain grades the Researcher's batch + balances future search.
    try:
        grade_research_batch(results)
    except Exception:
        pass  # comms must never break the backtest
    return {
        "tested": len(results),
        "survived": sum(1 for r in results if r.get("status") == "survived"),
        "rejected": sum(1 for r in results if r.get("status") == "rejected"),
        "inconclusive": sum(1 for r in results if r.get("status") == "inconclusive"),
        "results": results,
    }


@app.get("/backtests")
def backtests_list(limit: int = 200) -> list[dict]:
    return get_store().list_backtests(limit=limit)


class DecideRequest(BaseModel):
    symbol: str
    trade_type: str | None = None
    granularity: int = 60


_DIGIT_TYPES = {"even_odd", "over_under", "matches_differs"}


@app.post("/decide")
async def decide_endpoint(req: DecideRequest) -> dict:
    decision = run_decide(
        req.symbol, trade_type=req.trade_type, granularity=req.granularity
    )
    # For digit trades, attach the REAL last-tick distribution. The candle
    # backtest is a proxy; this is what the digit actually settles on, so
    # it's the truest evidence for/against the call. Best-effort only.
    relevant = (req.trade_type in _DIGIT_TYPES) or (
        decision.get("trade_type") in _DIGIT_TYPES
    )
    if relevant:
        try:
            decision["live_digit_stats"] = await live_digit_stats(req.symbol)
        except Exception:
            decision["live_digit_stats"] = None
    return decision


@app.get("/decisions")
def decisions_list(
    symbol: str | None = None, trade_only: bool = False, limit: int = 100
) -> list[dict]:
    return get_store().list_decisions(symbol=symbol, trade_only=trade_only, limit=limit)


@app.get("/research-gaps")
def research_gaps_endpoint() -> dict:
    """What the brain wants the Researcher to find next."""
    gaps = research_gaps()
    return {"gaps": gaps, "count": len(gaps)}


@app.post("/research-gaps/dispatch")
async def research_gaps_dispatch() -> dict:
    """Push the current gaps to the Researcher. Closes the loop."""
    gaps = research_gaps()
    if not gaps:
        return {"sent": 0, "failed": 0, "note": "no gaps to dispatch"}
    try:
        return await dispatch_gaps(gaps)
    except FeedbackError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"researcher unreachable: {exc!r}")


# ---------------------------------------------------------------------------
# Phase 4: inter-app comms bus — the Analyser is the hub
# ---------------------------------------------------------------------------


class CommsMessageIn(BaseModel):
    from_app: str            # researcher | analyser | bot
    to_app: str = "all"      # researcher | analyser | bot | all
    type: str                # command | advice | grade | report | request
    subject: str = ""
    body: str = ""
    grade: float | None = None
    data: dict = Field(default_factory=dict)


@app.post("/comms/send")
def comms_send(msg: CommsMessageIn) -> dict:
    """Any app posts a message to the bus."""
    mid = get_store().add_comms(msg.model_dump())
    return {"id": mid}


@app.get("/comms/log")
def comms_log(limit: int = 100) -> list[dict]:
    return get_store().list_comms(limit=limit)


@app.get("/comms/inbox")
def comms_inbox(for_app: str, unacked_only: bool = True) -> list[dict]:
    return get_store().comms_inbox(for_app, unacked_only=unacked_only)


class CommsAck(BaseModel):
    ids: list[str]


@app.post("/comms/ack")
def comms_ack(body: CommsAck) -> dict:
    return {"acked": get_store().comms_ack(body.ids)}


@app.get("/comms/scoreboard")
def comms_scoreboard() -> dict:
    return get_store().comms_scoreboard()


@app.get("/comms/recommendations")
def comms_recommendations(limit: int = 50) -> list[dict]:
    """The cross-system 'comment section' — what each system recommends the
    others do to lift productivity."""
    return get_store().list_recommendations(limit=limit)


# ---------------------------------------------------------------------------
# Productivity registry — each system reports its rate; peers read + advise
# ---------------------------------------------------------------------------


class ProductivityReport(BaseModel):
    app: str                       # researcher | analyser | bot
    score: float                   # 0-100 self-assessed
    summary: str = ""
    metrics: dict = Field(default_factory=dict)


@app.post("/productivity/report")
def productivity_report(rep: ProductivityReport) -> dict:
    """A system pushes its latest self-assessed productivity here."""
    get_store().upsert_productivity(
        rep.app, score=rep.score, summary=rep.summary, metrics=rep.metrics
    )
    return {"ok": True, "app": rep.app, "score": rep.score}


@app.get("/productivity")
def productivity_all() -> dict:
    """Every system's latest productivity rate — the shared scoreboard."""
    rows = get_store().list_productivity()
    return {
        "systems": rows,
        "average": round(sum(r["score"] or 0 for r in rows) / len(rows), 1) if rows else 0.0,
    }


@app.get("/productivity/self")
def productivity_self() -> dict:
    """The Analyser's own live productivity (computed on demand)."""
    return productivity.compute_self()


@app.get("/monitor")
def monitor_status() -> dict:
    """The brain's background heartbeat: how often it self-checks (every
    SYSTEM_CHECK_SECONDS) and what the last 10-minute refresh produced."""
    return monitor.status


# ---------------------------------------------------------------------------
# J81 Maintenance sector — systems file issues; export them for the AI to fix
# ---------------------------------------------------------------------------


class MaintenanceReport(BaseModel):
    from_app: str
    summary: str
    severity: str = "info"          # info | warning | error | critical
    area: str | None = None
    detail: str | None = None


@app.post("/maintenance/report")
def maintenance_report(issue: MaintenanceReport) -> dict:
    mid = get_store().add_maintenance(issue.model_dump())
    return {"id": mid}


@app.get("/maintenance/issues")
def maintenance_issues(status: str | None = "open", limit: int = 200) -> list[dict]:
    return get_store().list_maintenance(status=status, limit=limit)


class MaintenanceResolve(BaseModel):
    ids: list[str]


@app.post("/maintenance/resolve")
def maintenance_resolve(body: MaintenanceResolve) -> dict:
    return {"resolved": get_store().resolve_maintenance(body.ids)}


@app.post("/maintenance/cleanup")
def maintenance_cleanup() -> dict:
    """AUTO-CLEAR: wipe the analyser's working data (candles, decisions,
    backtests, gathered strategies/insights, finished study, acked comms) and
    compact the DB. Proven strategies live in the BOT, so this is safe — it's
    what keeps the 1GB disk from filling. Run it now to reclaim a full disk, and
    it's the same call the 30-minute cycle makes each loop."""
    store = get_store()
    before = store.db_size_bytes()
    wiped = store.reset_working_data()
    store.vacuum()
    after = store.db_size_bytes()
    return {"wiped": wiped, "bytes_before": before, "bytes_after": after,
            "reclaimed_mb": round((before - after) / 1e6, 2)}


# ---------------------------------------------------------------------------
# Brain self-study — each system learns how to improve + proposes enhancements
# ---------------------------------------------------------------------------


class StudyEntry(BaseModel):
    app: str
    kind: str                       # question | learning | enhancement
    body: str
    topic: str | None = None
    source: str | None = None


@app.post("/study/log")
def study_log(entry: StudyEntry) -> dict:
    sid = get_store().add_study(
        entry.app, entry.kind, entry.body, topic=entry.topic, source=entry.source
    )
    return {"id": sid}


@app.get("/study")
def study_list(app: str | None = None, kind: str | None = None,
               status: str | None = "open", limit: int = 200) -> list[dict]:
    return get_store().list_study(app=app, kind=kind, status=status, limit=limit)


class StudyResolve(BaseModel):
    ids: list[str]


@app.post("/study/resolve")
def study_resolve(body: StudyResolve) -> dict:
    return {"resolved": get_store().resolve_study(body.ids)}


@app.get("/study/export", response_class=PlainTextResponse)
def study_export() -> str:
    """Every system's self-study ENHANCEMENT proposals as one plaintext block —
    copy this and paste it to the AI (Claude) to actually implement the
    improvements the systems worked out for themselves."""
    items = get_store().list_study(kind="enhancement", status="open", limit=500)
    if not items:
        return ("No enhancement proposals yet. The systems' self-students write "
                "them as they study — check back after a few 10-minute cycles.")
    lines = [
        "J81 SELF-STUDY — enhancement proposals for the AI (Claude) to implement",
        f"({len(items)} open) — each system worked these out about itself",
        "=" * 64, "",
    ]
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. [{it['app'].upper()}] {it['body']}")
        if it.get("topic"):
            lines.append(f"   focus: {it['topic']}")
        if it.get("source"):
            lines.append(f"   based on: {it['source']}")
        lines.append(f"   filed: {it['created_at']}  id: {it['id']}")
        lines.append("")
    return "\n".join(lines)


@app.get("/maintenance/export", response_class=PlainTextResponse)
def maintenance_export() -> str:
    """All open issues as one plaintext block — copy this and paste it to the
    AI (Claude) to fix and update the systems."""
    issues = get_store().list_maintenance(status="open", limit=500)
    if not issues:
        return "No open maintenance issues. All systems nominal. ✅"
    lines = [
        "J81 MAINTENANCE — open issues for the AI to fix",
        f"({len(issues)} open) generated for copy-paste into Claude",
        "=" * 60,
        "",
    ]
    for i, it in enumerate(issues, 1):
        lines.append(f"{i}. [{it['severity'].upper()}] {it['from_app']} — {it['summary']}")
        if it.get("area"):
            lines.append(f"   area: {it['area']}")
        if it.get("detail"):
            lines.append(f"   detail: {it['detail']}")
        lines.append(f"   filed: {it['created_at']}  id: {it['id']}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run(app, host=s.host, port=s.port)


if __name__ == "__main__":
    main()
