"""24/7 autonomous research loop.

A single background task runs for the life of the process (so on a VPS you
just keep the process alive with systemd / tmux / pm2). Each cycle it
re-reads research_config.json, and if `autonomous.enabled` is true it runs
every enabled topic through the pipeline, then sleeps for the configured
interval. Toggling `autonomous.enabled` (via /config or /autonomous/*) takes
effect on the next check without a restart.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.models import ResearchRequest
from app.pipeline import ResearchPipeline
from app.research_config import load_config, save_config

_IDLE_POLL_SECONDS = 15


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AutonomousScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.status: dict = {
            "loop_alive": False,
            "enabled": False,
            "cycles": 0,
            "last_run": None,
            "next_run": None,
            "last_summary": [],
            "last_error": None,
        }

    # --- lifecycle ---------------------------------------------------------

    def ensure_running(self) -> None:
        """Start the background loop if it isn't already alive."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            self.status["loop_alive"] = True

    async def enable(self) -> None:
        cfg = load_config()
        cfg.autonomous.enabled = True
        save_config(cfg)
        self.status["enabled"] = True
        self.ensure_running()

    async def disable(self) -> None:
        cfg = load_config()
        cfg.autonomous.enabled = False
        save_config(cfg)
        self.status["enabled"] = False

    # --- the loop ----------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                cfg = load_config()
                self.status["enabled"] = cfg.autonomous.enabled
                if not cfg.autonomous.enabled:
                    await asyncio.sleep(_IDLE_POLL_SECONDS)
                    continue

                await self._run_cycle(cfg)

                self.status["cycles"] += 1
                self.status["last_run"] = _now().isoformat()
                self.status["next_run"] = (
                    _now()
                    + timedelta(seconds=cfg.autonomous.interval_seconds)
                ).isoformat()
                self.status["last_error"] = None

                await self._interruptible_sleep(
                    cfg.autonomous.interval_seconds
                )
            except asyncio.CancelledError:
                self.status["loop_alive"] = False
                raise
            except Exception as exc:  # never let the loop die
                self.status["last_error"] = repr(exc)
                await asyncio.sleep(_IDLE_POLL_SECONDS)

    async def _run_cycle(self, cfg) -> None:
        sources = cfg.sources.enabled()
        focus = cfg.focus.enabled()
        summary: list[dict] = []

        if not sources or not focus:
            self.status["last_summary"] = [
                {"note": "no sources or no focus modes enabled"}
            ]
            return

        pipeline = ResearchPipeline()
        for topic in cfg.topics:
            if not topic.enabled:
                continue
            request = ResearchRequest(
                trade_type=topic.trade_type,
                query=topic.query,
                sources=sources,
                focus=focus,
                hashtags=topic.hashtags,
                max_results_per_source=cfg.autonomous.max_results_per_source,
            )
            try:
                resp = await pipeline.run(request)
                summary.append(
                    {
                        "topic": topic.name,
                        "documents": resp.documents_found,
                        "strategies": len(resp.strategies),
                        "insights": len(resp.insights),
                        "pushed": resp.pushed,
                        "insights_pushed": resp.insights_pushed,
                        "errors": [e.platform.value for e in resp.errors],
                    }
                )
            except Exception as exc:
                summary.append({"topic": topic.name, "error": repr(exc)})

        self.status["last_summary"] = summary

    async def _interruptible_sleep(self, total: int) -> None:
        """Sleep up to `total`s but wake early if autonomous is turned off."""
        waited = 0
        while waited < total:
            chunk = min(_IDLE_POLL_SECONDS, total - waited)
            await asyncio.sleep(chunk)
            waited += chunk
            if not load_config().autonomous.enabled:
                return


scheduler = AutonomousScheduler()
