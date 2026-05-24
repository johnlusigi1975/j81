"""The 30-minute strategy cycle: backtest every trade-type/market candidate,
prove the survivors, push them to the BOT's strategy store, then auto-clear.

Acceptance bar (the user's, with the honest fix):
  * 5 independent samples of 100 trades each (5 consecutive, non-overlapping
    windows of real tick history).
  * EVERY window must win >= WIN_BAR% (default 70).
  * AND total net P/L across all 500 trades must be POSITIVE (EV gate).

The EV gate matters because on Deriv synthetics you can win 70%+ and still LOSE
money (DIFFERS wins ~90% but pays ~1.05x). Win-rate alone would "prove"
money-losing strategies. Synthetics are an audited RNG with a house edge, so in
practice almost nothing passes — that's the truth, and the cycle reports it
honestly rather than fabricating an edge.

Backtest payouts use the documented standard table (lab._FALLBACK_PAYOUT) so the
accept/reject decision is fast and deterministic; the live lab uses real
proposal payouts for the watch-view.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from app.deriv import fetch_ticks
from app.even_odd import last_digits
from app.lab import VARIANTS, _FALLBACK_PAYOUT, _won_at
from app.scanner import SCAN_SYMBOLS

WIN_BAR = 70.0          # each window must hit this win-rate
SAMPLES = 5             # ...this many times
TRADES_PER = 100        # ...over this many trades each
TICKS = 5000            # one history pull per market covers all variants
CYCLE_SECONDS = 1800    # 30 minutes


def _simulate(prices, digits, variant, payout, lo, hi, max_trades) -> dict:
    """Replay non-overlapping contracts in ticks[lo:hi]; return win-rate + P/L."""
    dur = variant["dur"]
    idx = lo + dur
    wins = pnl = n = 0
    while idx < hi and n < max_trades:
        won, _ = _won_at(variant, prices, digits, idx)
        pnl += (payout - 1.0) if won else -1.0
        wins += 1 if won else 0
        n += 1
        idx += dur
    return {"trades": n, "wins": wins,
            "win_rate": round(100.0 * wins / n, 1) if n else 0.0,
            "pnl": round(pnl, 4)}


def acceptance_test(prices, digits, variant) -> dict:
    """Run the 5×100 acceptance test for one variant over a tick history."""
    payout = _FALLBACK_PAYOUT.get(variant["ct"], 1.94)
    n = len(prices)
    block = n // SAMPLES
    windows = []
    for s in range(SAMPLES):
        windows.append(_simulate(prices, digits, variant, payout,
                                 s * block, (s + 1) * block, TRADES_PER))
    enough = all(w["trades"] >= TRADES_PER for w in windows)
    total_pnl = round(sum(w["pnl"] for w in windows), 4)
    avg_wr = round(sum(w["win_rate"] for w in windows) / len(windows), 1)
    all_win = all(w["win_rate"] >= WIN_BAR for w in windows)
    proven = bool(enough and all_win and total_pnl > 0)
    return {"label": variant["label"], "fam": variant["fam"], "payout": payout,
            "windows": windows, "avg_win_rate": avg_wr, "net_pnl": total_pnl,
            "passed_winrate": all_win, "passed_ev": total_pnl > 0,
            "proven": proven}


async def run_cycle(push: bool = True) -> dict:
    """Test all variants on all markets, prove survivors, push them to the Bot,
    then auto-clear the analyser's working data. Returns the run report."""
    from app.store import get_store
    started = datetime.now(timezone.utc).isoformat()
    tested: list[dict] = []
    proven: list[dict] = []
    for code, name in SCAN_SYMBOLS:
        try:
            data = await fetch_ticks(code, count=TICKS)
        except Exception:
            continue
        prices = data.get("prices") or []
        if len(prices) < SAMPLES * TRADES_PER + 10:
            continue
        digits = last_digits(prices, int(data.get("pip_size") or 2))
        for v in VARIANTS:
            res = acceptance_test(prices, digits, v)
            res["symbol"], res["market"] = code, name
            tested.append(res)
            if res["proven"]:
                proven.append({
                    "trade_type": v["fam"], "symbol": code, "market": name,
                    "label": v["label"], "contract_type": v["ct"],
                    "barrier": v["barrier"], "duration": v["dur"],
                    "win_rate": res["avg_win_rate"], "net_pnl": res["net_pnl"],
                    "samples": SAMPLES, "trades": SAMPLES * TRADES_PER,
                })

    pushed = 0
    if push and proven:
        pushed = await _push_to_bot(proven)

    # AUTO-CLEAR: winners are now in the Bot, so wipe the analyser's scratch...
    cleared = {}
    try:
        store = get_store()
        cleared = store.reset_working_data()
        store.vacuum()
    except Exception as exc:
        cleared = {"error": str(exc)}
    # ...and tell the Researcher to clear its accumulated files too (best-effort).
    cleared["researcher"] = await _clear_researcher()

    # rank the near-misses so the UI can show what came closest
    tested.sort(key=lambda r: (r["proven"], r["avg_win_rate"], r["net_pnl"]),
                reverse=True)
    report = {
        "started": started,
        "finished": datetime.now(timezone.utc).isoformat(),
        "tested": len(tested),
        "proven_count": len(proven),
        "pushed_to_bot": pushed,
        "proven": proven,
        "top": tested[:12],
        "cleared": cleared,
        "bar": f"{int(WIN_BAR)}% win each of {SAMPLES}×{TRADES_PER} trades + net P/L > 0",
    }
    _set_last(report)
    return report


async def _clear_researcher() -> str:
    """Ask the Researcher to deep-clear its out/ files each cycle. Best-effort."""
    import httpx
    from app.config import get_settings
    url = (get_settings().researcher_url or "").rstrip("/")
    if not url:
        return "no researcher_url"
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.post(f"{url}/maintenance/cleanup?deep=true")
            r.raise_for_status()
            return "ok"
    except Exception as exc:
        return f"unreachable: {exc!r}"[:80]


async def _push_to_bot(proven: list[dict]) -> int:
    """POST proven strategies to the Bot's strategy store. Best-effort."""
    import httpx
    from app.config import get_settings
    url = (get_settings().bot_url or "").rstrip("/")
    if not url:
        return 0
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(f"{url}/strategies/proven", json={"strategies": proven})
            r.raise_for_status()
            return int((r.json() or {}).get("saved", len(proven)))
    except Exception:
        return 0


# ----- in-memory last-report + the 30-minute background runner ----------------

_LAST: dict = {"started": None, "tested": 0, "proven_count": 0, "proven": [],
               "top": [], "note": "no cycle has run yet"}
_NEXT_TS: float = 0.0


def _set_last(report: dict) -> None:
    global _LAST, _NEXT_TS
    _LAST = report
    _NEXT_TS = time.time() + CYCLE_SECONDS


def status() -> dict:
    secs = max(0, int(_NEXT_TS - time.time())) if _NEXT_TS else None
    return {**_LAST, "next_in_seconds": secs, "cycle_seconds": CYCLE_SECONDS}


class CycleRunner:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        await asyncio.sleep(20)  # let the service settle after boot
        while True:
            try:
                await run_cycle(push=True)
            except Exception:
                pass
            await asyncio.sleep(CYCLE_SECONDS)


runner = CycleRunner()
