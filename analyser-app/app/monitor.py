"""Analyser background monitor — the brain's self-care + peer-watch + refresh.

Two cadences, one loop:
  * Every TICK (60s): self-report productivity so the dashboard stays live.
  * Every SYSTEM_CHECK (10 min): be a "brother's keeper" — advise the peers,
    then REFRESH to produce: auto-backtest every stored strategy (so newly
    arrived strategies actually get tested instead of sitting idle) and grade
    the Researcher's batch (which also sends it balance commands).

This is what makes the brain keep working between HTTP requests — no need for
a human to press "backtest" for the tree to stay alive.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from app import productivity
from app.store import get_store

TICK_SECONDS = 30                   # self-report every 30s so the maintenance panel stays live
SYSTEM_CHECK_SECONDS = 300          # advise peers + refresh every 5 min (was 10) — steady chatter without spam
SELF_STUDY_SECONDS  = 1800          # self-study every 30 min — heavier (burns Gemini quota), much rarer than refresh
_TICKS_PER_CHECK = max(1, SYSTEM_CHECK_SECONDS // TICK_SECONDS)
_TICKS_PER_STUDY = max(1, SELF_STUDY_SECONDS  // TICK_SECONDS)
_MAX_REFRESH_SYMBOLS = 12           # cover all common synthetics so none stay untested


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Monitor:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.status: dict = {
            "loop_alive": False,
            "ticks": 0,
            "last_run": None,
            "last_self_score": None,
            "last_recommendations_written": 0,
            "last_refresh": None,
            "last_error": None,
            "system_check_seconds": SYSTEM_CHECK_SECONDS,
        }

    def ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            self.status["loop_alive"] = True

    async def _loop(self) -> None:
        while True:
            try:
                self._self_report()
                self.status["ticks"] += 1
                self.status["last_run"] = _now()
                # Every ~5 minutes: advise peers + refresh to produce info.
                if self.status["ticks"] % _TICKS_PER_CHECK == 0:
                    self.status["last_recommendations_written"] = (
                        productivity.write_peer_recommendations()
                    )
                    await self._produce_refresh()
                    get_store().prune_comms()  # keep the bus lean for big runs
                # Every ~30 minutes: self-study (heavier, burns Gemini quota).
                if self.status["ticks"] % _TICKS_PER_STUDY == 0:
                    await self._self_study()
                self.status["last_error"] = None
            except asyncio.CancelledError:
                self.status["loop_alive"] = False
                raise
            except Exception as exc:  # never die
                self.status["last_error"] = repr(exc)
                try:
                    from app.comms import emit
                    emit(to_app="all", type="report", subject="monitor error",
                         body=repr(exc), from_app="analyser")
                except Exception:
                    pass
            await asyncio.sleep(TICK_SECONDS)

    def _self_report(self) -> None:
        me = productivity.compute_self()
        get_store().upsert_productivity(
            me["app"], score=me["score"], summary=me["summary"], metrics=me["metrics"]
        )
        self.status["last_self_score"] = me["score"]

    async def _produce_refresh(self) -> None:
        """The 10-minute refresh: test every stored strategy on fresh-ish data
        and grade the Researcher. Best-effort and bounded so it stays smooth."""
        from app.backtest import backtest_all
        from app.comms import grade_research_batch
        from app.market_data import ensure_candles

        store = get_store()
        rows = store.list_strategies_raw()
        if not rows:
            self.status["last_refresh"] = {"tested": 0, "note": "no strategies yet"}
            return

        # Which symbols do the strategies need? Refresh a small, capped set so
        # backtests run on recent candles instead of stale/empty caches.
        symbols: list[str] = []
        for r in rows:
            payload = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
            for s in (payload.get("symbols") or []):
                if s and s not in symbols:
                    symbols.append(s)
        symbols = symbols[:_MAX_REFRESH_SYMBOLS] or ["R_100"]
        for sym in symbols:
            try:
                await ensure_candles(sym, 60, days=1.0)
            except Exception:
                pass  # a failed fetch just means we backtest on what's cached

        results = backtest_all(rows, granularity=60)
        recorded = 0
        for r in results:
            if "error" not in r:
                store.record_backtest(r)
                recorded += 1
        try:
            grade_research_batch(results)  # also sends balance commands upstream
        except Exception:
            pass
        self.status["last_refresh"] = {
            "tested": recorded,
            "survived": sum(1 for r in results if r.get("status") == "survived"),
            "symbols": symbols,
            "at": _now(),
        }

    async def _self_study(self) -> None:
        """Run the brain's self-student: diagnose weaknesses, ask the Researcher
        for study material, file enhancement proposals for the AI."""
        try:
            from app import self_study
            self.status["last_self_study"] = await self_study.study_once()
        except Exception as exc:
            self.status["last_self_study"] = {"error": repr(exc)}


monitor = Monitor()
