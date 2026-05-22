"""M Pro — the Even/Odd confidence engine (Twinmil-style, on J81's own brain).

Each cycle it pulls the Analyser's 10-market scan, picks the market with the
strongest even/odd edge under the chosen mode, and fires one Even/Odd contract.
It honours the account's take-profit / loss-limit, a cooldown between trades,
and an OPTIONAL martingale step (raise stake after a loss) — which is hard-capped
by the account's max stake, so it can never run away.

Honest by design: if no market's edge clears `min_gap`, it sits out (status
"scanning · no edge"). Even/Odd is ~50/50 long-run; this hunts short skews.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import get_settings
from app.executor import execute_manual_trade, goal_status
from app.store import get_store

TICK_SECONDS = 8

DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "auto",       # even | odd | auto
    "reverse": False,     # bet against the bias (contrarian)
    "min_gap": 15.0,      # only trade if the top market's even/odd gap ≥ this
    "base_stake": 1.0,
    "step_mult": 2.0,     # martingale multiplier per losing step
    "max_steps": 0,       # 0 = flat staking (martingale OFF). >0 enables it.
    "cooldown": 20,       # seconds between trades
    "duration": 1,        # ticks per Even/Odd contract
}


def merge_config(cfg: dict | None) -> dict:
    out = dict(DEFAULT_CONFIG)
    if cfg:
        out.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
    return out


def plan_trade(scan: dict, config: dict, step: int) -> dict | None:
    """Pure: given a scan + config + martingale step, return a trade plan or
    None (sit out). Stake here is the *desired* stake; the executor still caps
    it to the account's max — that's the runaway guard for martingale."""
    ranked = [r for r in (scan.get("ranked") or []) if r.get("ready")]
    if not ranked:
        return None
    mode = (config.get("mode") or "auto").lower()
    if mode == "even":
        cands = [r for r in ranked if r["direction"] == "even"]
    elif mode == "odd":
        cands = [r for r in ranked if r["direction"] == "odd"]
    else:
        cands = ranked
    if not cands:
        return None
    pick = cands[0]  # already sorted by gap desc
    if pick["gap"] < float(config.get("min_gap", 15)):
        return None

    direction = pick["direction"]
    if config.get("reverse"):
        direction = "odd" if direction == "even" else "even"

    base = float(config.get("base_stake", 1.0))
    if int(config.get("max_steps", 0)) > 0:
        stake = round(base * (float(config.get("step_mult", 2.0)) ** step), 2)
    else:
        stake = base
    return {
        "symbol": pick["symbol"], "name": pick["name"], "prediction": direction,
        "stake": stake, "gap": pick["gap"], "quality": pick["quality"],
        "even": pick["even"], "odd": pick["odd"], "step": step,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MProEngine:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.last_scan: dict | None = None
        self.state: dict[str, dict] = {}   # per-account runtime state
        self.alive = False

    def ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            self.alive = True

    def _st(self, account_id: str) -> dict:
        return self.state.setdefault(account_id, {
            "step": 0, "last_trade_id": None, "last_trade_ts": 0.0,
            "status": "idle", "last_pick": None, "last_outcome": None, "trades": 0,
        })

    async def _scan(self) -> dict | None:
        url = get_settings().analyser_url.rstrip("/") + "/scan/even_odd?count=120"
        try:
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.get(url)
                r.raise_for_status()
                return r.json()
        except Exception:
            return None

    async def _loop(self) -> None:
        while True:
            try:
                accounts = [a for a in get_store().list_accounts_public()
                            if a.get("enabled") and a.get("mpro_enabled")]
                if accounts:
                    scan = await self._scan()
                    if scan:
                        self.last_scan = scan
                        for a in accounts:
                            await self._run_account(a, scan)
                # drop state for accounts no longer in M Pro
                live_ids = {a["id"] for a in accounts}
                for sid in list(self.state):
                    if sid not in live_ids:
                        self.state.pop(sid, None)
            except asyncio.CancelledError:
                self.alive = False
                raise
            except Exception:
                pass
            await asyncio.sleep(TICK_SECONDS)

    async def _run_account(self, acct_public: dict, scan: dict) -> None:
        store = get_store()
        acct = store.get_internal(acct_public["id"])
        if not acct:
            return
        st = self._st(acct["id"])
        cfg = merge_config(acct_public.get("mpro_config"))

        # 1. settle bookkeeping: did the last trade finish? update martingale step.
        if st["last_trade_id"]:
            row = next((t for t in store.list_trades(account_id=acct["id"], limit=5)
                        if t["id"] == st["last_trade_id"]), None)
            if row:
                outcome = row.get("outcome")
                if outcome in ("won", "lost", "dry_run", "error", "settled"):
                    if outcome == "lost":
                        st["step"] = min(st["step"] + 1, int(cfg.get("max_steps", 0)))
                    elif outcome == "won":
                        st["step"] = 0
                    st["last_outcome"] = outcome
                    st["last_trade_id"] = None
                else:
                    st["status"] = "waiting for contract to settle…"
                    return  # still open — wait

        # 2. goals (take-profit / loss-limit)
        blocked, why = goal_status(acct)
        if blocked:
            st["status"] = why
            return

        # 3. cooldown
        remaining = cfg["cooldown"] - (time.monotonic() - st["last_trade_ts"])
        if remaining > 0:
            st["status"] = f"cooldown {int(remaining)}s"
            return

        # 4. plan from the scan
        plan = plan_trade(scan, cfg, st["step"])
        st["last_pick"] = plan or {"name": (scan.get("top") or {}).get("name"),
                                   "note": "no edge ≥ min_gap"}
        if not plan:
            st["status"] = "scanning · no edge yet"
            return

        # 5. fire one Even/Odd contract
        res = await execute_manual_trade(
            acct, trade_type="even_odd", symbol=plan["symbol"],
            prediction=plan["prediction"], stake=plan["stake"],
            duration=int(cfg.get("duration", 1)), duration_unit="t",
        )
        st["last_trade_ts"] = time.monotonic()
        st["trades"] += 1
        if res.get("trade_id"):
            st["last_trade_id"] = res["trade_id"]
        st["status"] = (
            f"traded {plan['prediction'].upper()} on {plan['name']} "
            f"(gap {plan['gap']}%, ${plan['stake']}) · {res.get('outcome')}"
        )

    def status(self) -> dict:
        store = get_store()
        accts = {a["id"]: a for a in store.list_accounts_public()}
        out = []
        for aid, st in self.state.items():
            a = accts.get(aid, {})
            out.append({
                "account_id": aid,
                "account": a.get("deriv_account_id"),
                "is_demo": a.get("is_demo"),
                "status": st["status"],
                "step": st["step"],
                "trades": st["trades"],
                "last_outcome": st["last_outcome"],
                "last_pick": st["last_pick"],
                "profit_today": a.get("profit_today", 0.0),
                "take_profit": a.get("take_profit"),
                "config": merge_config(a.get("mpro_config")),
            })
        return {"alive": self.alive, "accounts": out,
                "scan": self.last_scan, "at": _now()}


engine = MProEngine()
