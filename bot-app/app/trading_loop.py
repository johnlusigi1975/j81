"""Autonomous trading loop.

Every `TRADE_POLL_SECONDS`, walks each enabled account:
  1. Picks a symbol from the user's allow-list (or R_100 default).
  2. Asks the Analyser for a decision on that symbol.
  3. If is_trade + risk gates pass, delegates to executor.

DRY_RUN gating happens inside the executor, not here. This loop runs
identically in dry and live mode — only the executor knows the difference.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from app.config import get_settings
from app.executor import execute_decision_for_account, get_decision
from app.store import get_store

_IDLE_POLL_SECONDS = 15
SYSTEM_CHECK_SECONDS = 600  # 10-minute peer-watch heartbeat (brother's keeper)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TradingLoop:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.status: dict = {
            "loop_alive": False,
            "cycles": 0,
            "last_run": None,
            "next_run": None,
            "last_summary": [],
            "last_error": None,
        }

    def ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            self.status["loop_alive"] = True

    async def _loop(self) -> None:
        while True:
            try:
                interval = get_settings().trade_poll_seconds
                await self._run_cycle()
                self.status["cycles"] += 1
                self.status["last_run"] = _now().isoformat()
                self.status["last_error"] = None
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                self.status["loop_alive"] = False
                raise
            except Exception as exc:  # loop must never die
                self.status["last_error"] = repr(exc)
                try:
                    from app import comms_client
                    await comms_client.report_issue(
                        "trading loop raised an exception",
                        severity="error", area="bot/trading_loop", detail=repr(exc),
                    )
                except Exception:
                    pass
                await asyncio.sleep(_IDLE_POLL_SECONDS)

    async def _run_cycle(self) -> None:
        store = get_store()
        # First, settle any live contracts that have expired since last cycle.
        await self._settle_pending()

        accounts = [a for a in store.list_accounts_public() if a["enabled"]]
        if not accounts:
            self.status["last_summary"] = [{"note": "no accounts enabled"}]
            return

        # Priority mode (set on the hub): when on, only act on the two simple
        # trade types so the bot masters them first.
        from app import comms_client
        prio = await comms_client.get_priority()
        priority_types = set(prio.get("trade_types") or []) if prio.get("enabled") else None

        summary: list[dict] = []
        for acct_public in accounts:
            # Pull the internal row so we have the full settings.
            acct = store.get_internal(acct_public["id"])
            if acct is None:
                continue
            # Decide which symbol(s) to ask about.
            allowed_symbols = (
                json.loads(acct.get("allowed_symbols") or "null") or ["R_100"]
            )
            for symbol in allowed_symbols:
                decision = await get_decision(symbol)
                if decision and priority_types is not None and \
                        decision.get("trade_type") not in priority_types:
                    summary.append({
                        "account": acct["deriv_account_id"], "symbol": symbol,
                        "outcome": "skipped",
                        "reason": f"priority mode: {decision.get('trade_type')} not in {sorted(priority_types)}",
                        "is_trade": decision.get("is_trade"),
                    })
                    continue
                if not decision:
                    summary.append({
                        "account": acct["deriv_account_id"],
                        "symbol": symbol,
                        "outcome": "skipped",
                        "reason": "analyser unreachable or returned empty",
                    })
                    continue
                result = await execute_decision_for_account(acct, decision)
                summary.append({
                    "account": acct["deriv_account_id"],
                    "symbol": symbol,
                    "outcome": result.get("outcome"),
                    "reason": result.get("reason") or result.get("error"),
                    "confidence": decision.get("confidence"),
                    "is_trade": decision.get("is_trade"),
                })
                if result.get("outcome") == "error":
                    from app import comms_client
                    await comms_client.report_issue(
                        f"trade execution error on {acct['deriv_account_id']}",
                        severity="error", area="bot/executor",
                        detail=str(result.get("error")),
                    )
        self.status["last_summary"] = summary
        await self._grade_the_brain(summary)
        await self._peer_watch()

    async def _peer_watch(self) -> None:
        """~5% of effort: self-report productivity every cycle (keeps the
        dashboard live) and advise the other systems on a steady 10-minute
        cadence (smooth, not spammy) — the bot being a 'brother's keeper'."""
        import time
        now = time.monotonic()
        due = now - getattr(self, "_last_rec_ts", 0.0) >= SYSTEM_CHECK_SECONDS
        try:
            from app import productivity
            await productivity.peer_watch(write_recommendations=due)
            if due:
                self._last_rec_ts = now
                try:
                    from app import self_study
                    await self_study.study_once()
                except Exception:
                    pass
        except Exception:
            pass

    async def _settle_pending(self) -> None:
        """Poll Deriv for the outcome of live contracts that haven't settled.
        DRY_RUN trades are never live so they're never in this set."""
        if get_settings().dry_run:
            return
        from app.deriv import DerivBotError
        from app.executor import _live_check
        store = get_store()
        for t in store.list_pending_live_trades():
            token = store.decrypted_token_for(t["account_id"])
            if not token:
                continue
            acct = store.get_internal(t["account_id"]) or {}
            try:
                info = await _live_check(acct, token, t["deriv_contract_id"])
            except DerivBotError:
                continue
            if info.get("is_sold"):
                store.settle_trade(
                    t["id"],
                    outcome=info.get("status") or "settled",
                    profit=info.get("profit"),
                    markup_earned=info.get("app_markup_amount"),
                )

    async def _grade_the_brain(self, summary: list[dict]) -> None:
        """The Bot grades the Analyser on how actionable its decisions were
        this cycle, and advises if it's only ever getting no-trades."""
        from app import comms_client
        if not summary:
            return
        decided = [s for s in summary if s.get("is_trade") is not None]
        if not decided:
            return
        tradeable = [s for s in decided if s.get("is_trade")]
        # Grade: more actionable, well-confident decisions = higher mark.
        rate = len(tradeable) / len(decided)
        avg_conf = (
            sum(float(s.get("confidence") or 0) for s in tradeable) / len(tradeable)
            if tradeable else 0.0
        )
        grade = round(min(10.0, rate * 6 + avg_conf * 4), 1)
        try:
            await comms_client.send(
                to_app="analyser", type="grade",
                subject="decision actionability",
                grade=grade,
                body=(
                    f"{len(tradeable)}/{len(decided)} decisions were tradeable "
                    f"(avg confidence {avg_conf:.0%})."
                    + ("" if tradeable else
                       " I'm getting only no-trades — either tighten strategies "
                       "or lower confidence thresholds.")
                ),
                data={"tradeable": len(tradeable), "decided": len(decided)},
            )
            if not tradeable:
                await comms_client.send(
                    to_app="analyser", type="advice",
                    subject="no actionable signals",
                    body="Every decision this cycle was no-trade. Consider "
                         "backtesting more strategies or relaxing the survivor bar.",
                )
        except Exception:
            pass


loop = TradingLoop()
