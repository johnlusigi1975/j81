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
SYSTEM_CHECK_SECONDS = 60   # 1-min peer-watch heartbeat → live tree chatter in the maintenance panel


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _proven_to_params(s: dict) -> tuple[str, str | None, str | None]:
    """Map a proven strategy's Deriv contract_type back to execute_manual_trade
    params: (trade_type, direction, prediction). Mirrors the lab's variant set."""
    ct = (s.get("contract_type") or "").upper()
    b = s.get("barrier")
    bs = str(b) if b is not None else None
    table = {
        "CALL":       ("rise_fall", "up", None),
        "PUT":        ("rise_fall", "down", None),
        "DIGITEVEN":  ("even_odd", None, "even"),
        "DIGITODD":   ("even_odd", None, "odd"),
        "DIGITOVER":  ("over_under", "over", bs or "4"),
        "DIGITUNDER": ("over_under", "under", bs or "5"),
        "DIGITMATCH": ("matches_differs", "matches", bs or "0"),
        "DIGITDIFF":  ("matches_differs", "differs", bs or "0"),
    }
    return table.get(ct, (s.get("trade_type") or "rise_fall", "up", None))


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
            # Primary auto mode: trade the strategies the Analyser PROVED.
            if await self._run_proven_account(acct, summary):
                continue
            # Brain-driven auto: every cycle, trade the library's recommended
            # setup (Even/Odd on the best-payout market) with no fear-based
            # confidence gate. Safety still applies (TP/SL, daily cap).
            if await self._run_brain_account(acct, summary):
                continue
            # Server-side confidence-gated Rise/Fall runner (the "VPS" mode).
            if await self._run_rf_account(acct, summary):
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

    async def _run_brain_account(self, acct: dict, summary: list) -> bool:
        """Brain-driven auto: every cycle, place ONE Even/Odd trade on the
        library's best-payout market, at the stake the user set (anti-martingale,
        capped). No fear-based confidence gate — the system acts on the documented
        EV lever (payout/market selection) and lets discipline (TP/SL/daily-cap)
        be the only guardrail. Honest framing: this trades MORE, not better — but
        with the one real edge applied and the safety rails intact."""
        if not acct.get("brain_auto"):
            return False
        from app.executor import execute_manual_trade, goal_status
        import httpx, random
        store = get_store()
        blocked, why = goal_status(acct)
        if blocked:
            store.update_account_settings(acct["id"], brain_auto=False, enabled=False)
            summary.append({"account": acct["deriv_account_id"], "outcome": "stopped", "reason": why})
            return True
        # Best-payout market from the analyser library (cached server-side).
        best_symbol = "R_25"  # safe default — historically high-payout for Even/Odd
        try:
            url = get_settings().analyser_url.rstrip("/") + "/deriv/library"
            async with httpx.AsyncClient(timeout=8.0) as c:
                lib = (await c.get(url)).json()
            best = ((lib or {}).get("live") or {}).get("best_even_odd_market") or {}
            if best.get("symbol"):
                best_symbol = best["symbol"]
        except Exception:
            pass
        # Stake = the user's per-trade ceiling (the bot already caps this).
        stake = float(acct.get("max_stake_per_trade") or 0.35)
        if stake < 0.35:
            stake = 0.35
        # Direction: random — odds are 50/50; momentum is gambler's fallacy on RNG.
        side = random.choice(["even", "odd"])
        try:
            res = await execute_manual_trade(
                acct, trade_type="even_odd", symbol=best_symbol,
                direction=None, prediction=side, stake=stake,
                duration=1, duration_unit="t")
        except Exception as exc:
            summary.append({"account": acct["deriv_account_id"], "outcome": "error", "reason": repr(exc)})
            return True
        if res.get("outcome") == "skipped":  # daily cap / goal reached → stop
            store.update_account_settings(acct["id"], brain_auto=False, enabled=False)
        summary.append({
            "account": acct["deriv_account_id"], "symbol": best_symbol,
            "outcome": res.get("outcome"), "reason": res.get("reason") or res.get("error"),
            "strategy": f"brain_auto: best-payout Even/Odd · {side}",
        })
        return True

    async def _run_proven_account(self, acct: dict, summary: list) -> bool:
        """Trade the strategies the Analyser PROVED (≥70% win ×100 ×5 + net P/L>0)
        and pushed to our strategy store. Picks the best-EV proven strategy and
        places one trade per cycle, honouring stake/daily-cap/goal/DRY_RUN. If the
        store is empty (the honest norm on RNG markets), it just waits. Returns
        True if it handled this account."""
        if not acct.get("proven_auto"):
            return False
        from app.executor import execute_manual_trade, goal_status
        store = get_store()
        blocked, why = goal_status(acct)
        if blocked:  # win target or loss limit reached → switch off
            store.update_account_settings(acct["id"], proven_auto=False, enabled=False)
            summary.append({"account": acct["deriv_account_id"], "outcome": "stopped", "reason": why})
            return True
        proven = store.list_proven_strategies(limit=50)
        if not proven:
            summary.append({"account": acct["deriv_account_id"], "outcome": "waiting",
                            "reason": "no proven strategies yet — the cycle hasn't found an edge"})
            return True
        # Best expected value first (net P/L), then win-rate.
        proven.sort(key=lambda s: ((s.get("net_pnl") or 0), (s.get("win_rate") or 0)), reverse=True)
        best = proven[0]
        tt, direction, prediction = _proven_to_params(best)
        stake = float(acct.get("max_stake_per_trade") or 0.35) or 0.35
        try:
            res = await execute_manual_trade(
                acct, trade_type=tt, symbol=best.get("symbol") or "R_100",
                direction=direction, prediction=prediction, stake=stake,
                duration=int(best.get("duration") or 5), duration_unit="t")
        except Exception as exc:
            summary.append({"account": acct["deriv_account_id"], "outcome": "error", "reason": repr(exc)})
            return True
        if res.get("outcome") == "skipped":  # cap / goal reached → stop
            store.update_account_settings(acct["id"], proven_auto=False, enabled=False)
        summary.append({"account": acct["deriv_account_id"], "symbol": best.get("symbol"),
                        "outcome": res.get("outcome"), "reason": res.get("reason") or res.get("error"),
                        "strategy": best.get("label"), "win_rate": best.get("win_rate")})
        return True

    async def _run_rf_account(self, acct: dict, summary: list) -> bool:
        """Server-side Rise/Fall confidence-gated auto (runs 24/7 on the host —
        the client can close their screen). Asks the Analyser to scan all 10
        markets, and only trades the top one when its confidence >= the user's
        threshold. Honours rounds (max_trades_per_day), take-profit & loss-limit
        via execute_manual_trade. Returns True if it handled this account."""
        raw = acct.get("rf_config")
        try:
            cfg = json.loads(raw) if isinstance(raw, str) else (raw or None)
        except Exception:
            cfg = None
        if not cfg or not cfg.get("enabled"):
            return False
        from app.executor import get_rise_fall_scan, execute_manual_trade, goal_status
        store = get_store()
        blocked, why = goal_status(acct)
        if blocked:  # take-profit or loss-limit hit → switch the runner off
            store.update_account_settings(acct["id"], rf_config={**cfg, "enabled": False}, enabled=False)
            summary.append({"account": acct["deriv_account_id"], "outcome": "stopped", "reason": why})
            return True
        try:
            scan = await get_rise_fall_scan()
        except Exception:
            scan = {}
        top = (scan or {}).get("top")
        if not top:
            summary.append({"account": acct["deriv_account_id"], "outcome": "waiting", "reason": "scan warming up"})
            return True
        min_conf = float(cfg.get("min_conf") or 70)
        conf = float(top.get("confidence") or 0)
        if conf < min_conf:
            summary.append({"account": acct["deriv_account_id"], "outcome": "waiting",
                            "reason": f"confidence {conf:.0f}% < {min_conf:.0f}%"})
            return True
        try:
            res = await execute_manual_trade(
                acct, trade_type="rise_fall", symbol=top["symbol"], direction=top["direction"],
                stake=float(cfg.get("stake") or 0.35), duration=int(cfg.get("duration") or 5),
                duration_unit="t")
        except Exception as exc:
            summary.append({"account": acct["deriv_account_id"], "outcome": "error", "reason": repr(exc)})
            return True
        if res.get("outcome") == "skipped":  # rounds cap / goal reached → stop
            store.update_account_settings(acct["id"], rf_config={**cfg, "enabled": False}, enabled=False)
        summary.append({"account": acct["deriv_account_id"], "symbol": top["symbol"],
                        "outcome": res.get("outcome"), "reason": res.get("reason") or res.get("error"),
                        "confidence": conf, "direction": top["direction"]})
        return True

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
        DRY_RUN trades are never live so they're never in this set. Reuses the
        same per-account settler the on-demand /settle endpoint uses, so the
        loop and the UI settle contracts identically."""
        if get_settings().dry_run:
            return
        from app.executor import settle_pending_for_account
        store = get_store()
        account_ids = {t["account_id"] for t in store.list_pending_live_trades()}
        for account_id in account_ids:
            try:
                await settle_pending_for_account(account_id)
            except Exception:
                continue

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
