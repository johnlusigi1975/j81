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
from app.sharing import send_to_analyser

_IDLE_POLL_SECONDS = 15
SYSTEM_CHECK_SECONDS = 600  # 10-minute peer-watch heartbeat (brother's keeper)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AutonomousScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._send_task: asyncio.Task | None = None
        self._comms_task: asyncio.Task | None = None
        self.status: dict = {
            "loop_alive": False,
            "enabled": False,
            "cycles": 0,
            "last_run": None,
            "next_run": None,
            "last_summary": [],
            "last_error": None,
        }
        self.send_status: dict = {
            "loop_alive": False,
            "enabled": False,
            "last_send_at": None,
            "next_send_at": None,
            "last_result": None,
        }

    # --- lifecycle ---------------------------------------------------------

    def ensure_running(self) -> None:
        """Start the background loops if they aren't already alive."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            self.status["loop_alive"] = True
        if self._send_task is None or self._send_task.done():
            self._send_task = asyncio.create_task(self._send_loop())
            self.send_status["loop_alive"] = True
        if self._comms_task is None or self._comms_task.done():
            self._comms_task = asyncio.create_task(self._comms_loop())

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

                # Report the cycle outcome up to the brain (best-effort).
                try:
                    from app import comms_client
                    summary = self.status.get("last_summary") or []
                    total_s = sum(s.get("strategies", 0) for s in summary if isinstance(s, dict))
                    total_i = sum(s.get("insights", 0) for s in summary if isinstance(s, dict))
                    await comms_client.send(
                        to_app="analyser", type="report",
                        subject=f"cycle {self.status['cycles']} complete",
                        body=f"Gathered {total_s} strategies, {total_i} insights "
                             f"across {len(summary)} topics.",
                        data={"strategies": total_s, "insights": total_i},
                    )
                    # Any per-topic failures → file them with the maintenance sector.
                    for s in summary:
                        if isinstance(s, dict) and s.get("error"):
                            await comms_client.report_issue(
                                f"topic '{s.get('topic')}' failed during research",
                                severity="warning", area="researcher/pipeline",
                                detail=str(s.get("error")),
                            )
                except Exception:
                    pass

                # ~5% peer-watch: self-report productivity + advise the others.
                try:
                    from app import productivity as _prod
                    await _prod.peer_watch(
                        write_recommendations=(self.status["cycles"] % 3 == 0)
                    )
                except Exception:
                    pass

                # Optional: ship the library to the analyser every N cycles.
                # Re-read config so a live edit (cycle 1 finishing → user
                # changes setting → cycle 2 picks it up) takes effect.
                share_cfg = load_config().sharing
                n = share_cfg.auto_send_every_n_cycles
                if n > 0 and self.status["cycles"] % n == 0:
                    try:
                        results = await send_to_analyser(
                            archive=share_cfg.archive_after_send
                        )
                        self.send_status["last_send_at"] = _now().isoformat()
                        self.send_status["last_result"] = {
                            "trigger": f"every-{n}-cycles",
                            "destinations": [r.model_dump() for r in results],
                            "sent_to": sum(1 for r in results if r.sent),
                            "failed": sum(1 for r in results if not r.sent),
                        }
                    except Exception as exc:
                        self.send_status["last_result"] = {
                            "trigger": f"every-{n}-cycles",
                            "error": repr(exc),
                        }

                await self._interruptible_sleep(
                    cfg.autonomous.interval_seconds
                )
            except asyncio.CancelledError:
                self.status["loop_alive"] = False
                raise
            except Exception as exc:  # never let the loop die
                self.status["last_error"] = repr(exc)
                try:
                    from app import comms_client
                    await comms_client.report_issue(
                        "research loop raised an exception",
                        severity="error", area="researcher/scheduler", detail=repr(exc),
                    )
                except Exception:
                    pass
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

        # Priority mode (set on the hub): when on, only research the two simple
        # trade types so the tree masters them first.
        from app import comms_client
        prio = await comms_client.get_priority()
        priority_types = set(prio.get("trade_types") or []) if prio.get("enabled") else None

        pipeline = ResearchPipeline()
        base_per_source = cfg.autonomous.max_results_per_source
        weights = cfg.trade_type_weights or {}
        for topic in cfg.topics:
            if not topic.enabled:
                continue
            if priority_types is not None and topic.trade_type.value not in priority_types:
                continue
            # Respect the brain's balance command: scale results per source by
            # the trade-type weight (default 1.0), capped to a sane ceiling.
            weight = float(weights.get(topic.trade_type.value, 1.0))
            per_source = max(1, min(round(base_per_source * weight), 15))
            request = ResearchRequest(
                trade_type=topic.trade_type,
                query=topic.query,
                sources=sources,
                focus=focus,
                hashtags=topic.hashtags,
                max_results_per_source=per_source,
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

    # --- comms loop: obey the brain, report back --------------------------

    async def _comms_loop(self) -> None:
        """Poll the brain's inbox every 30s. Apply balance commands, answer
        questions other systems raise (the ~10% research budget), ack all.
        Also runs the steady 10-minute peer-watch heartbeat so the Researcher
        stays a 'brother's keeper' even when research itself is infrequent."""
        import time
        from app import comms_client
        last_peer_watch = 0.0
        while True:
            try:
                messages = await comms_client.inbox()
                if messages:
                    handled = self._apply_comms(messages)  # balance cmds etc.
                    await self._answer_questions(messages)  # the 10% q&a
                    await comms_client.ack(handled)
                now = time.monotonic()
                if now - last_peer_watch >= SYSTEM_CHECK_SECONDS:
                    last_peer_watch = now
                    try:
                        from app import productivity as _prod
                        await _prod.peer_watch(write_recommendations=True)
                    except Exception:
                        pass
                    try:
                        from app import self_study
                        await self_study.study_once()
                    except Exception:
                        pass
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(30)

    async def _answer_questions(self, messages: list[dict]) -> None:
        """Spend a slice of research power answering questions/requests other
        systems raise. Capped to one question per poll to keep it ~10% — the
        bulk of capacity stays on the autonomous topic sweep."""
        questions = [
            m for m in messages
            if m.get("type") in ("question", "request") and m.get("from_app") != "researcher"
        ]
        if not questions:
            return
        from app import comms_client
        from app.models import ResearchRequest
        from app.pipeline import ResearchPipeline

        cfg = load_config()
        sources = cfg.sources.enabled()
        focus = cfg.focus.enabled()
        default_tt = cfg.topics[0].trade_type.value if cfg.topics else None
        if not sources or not focus:
            return  # can't research right now; leave for a later poll

        pipeline = ResearchPipeline()
        m = questions[0]  # one per poll = the ~10% budget
        data = m.get("data") or {}
        query = " ".join(filter(None, [m.get("subject"), m.get("body")])).strip()
        query = query or data.get("query")
        tt = data.get("trade_type") or default_tt
        if not query or not tt:
            return
        try:
            resp = await pipeline.run(ResearchRequest(
                trade_type=tt, query=query, sources=sources, focus=focus,
                hashtags=[], max_results_per_source=2,  # small = ~10% of effort
            ))
            n_s = len(resp.strategies); n_i = len(resp.insights)
            if n_s == 0 and n_i == 0:
                # 0-yield run: log it quietly to study, don't broadcast a noise
                # "found 0 strategies" answer to the peer — that just spammed the
                # maintenance log without helping anyone.
                await comms_client.log_study(
                    "learning",
                    f"Question '{query[:60]}' produced 0 strategies/0 insights from "
                    f"{resp.documents_found} sources — extraction needs softening.",
                    topic=(m.get('subject') or 'q&a'), source="researcher/answer",
                )
            else:
                await comms_client.send(
                    to_app=m.get("from_app", "analyser"), type="answer",
                    subject=f"answer: {m.get('subject') or 'your question'}",
                    body=(f"Researched '{query[:80]}': {resp.documents_found} sources, "
                          f"{n_s} strategies, {n_i} insights pushed to the library."),
                    data={"question_id": m.get("id"), "query": query},
                )
        except Exception as exc:
            await comms_client.report_issue(
                f"couldn't answer question: {query[:60]}",
                severity="warning", area="researcher/answer", detail=repr(exc),
            )

    def _apply_comms(self, messages: list[dict]) -> list[str]:
        """Process inbound messages from the brain. Returns ids to ack."""
        cfg = load_config()
        changed = False
        handled: list[str] = []
        for m in messages:
            handled.append(m["id"])
            if m.get("type") == "command" and m.get("subject") == "balance search":
                data = m.get("data") or {}
                tt = data.get("trade_type")
                weight = data.get("weight")
                if tt and weight is not None:
                    cfg.trade_type_weights[tt] = float(weight)
                    changed = True
            # advice/grade/report are informational — they show on the
            # dashboard; no behavioural change needed here.
        if changed:
            save_config(cfg)
        return handled

    # --- auto-send-to-analyser loop ---------------------------------------

    async def _send_loop(self) -> None:
        while True:
            try:
                share = load_config().sharing
                self.send_status["enabled"] = share.auto_send_to_analyser
                if not share.auto_send_to_analyser:
                    await asyncio.sleep(_IDLE_POLL_SECONDS)
                    continue

                results = await send_to_analyser(archive=share.archive_after_send)
                self.send_status["last_send_at"] = _now().isoformat()
                self.send_status["last_result"] = {
                    "destinations": [r.model_dump() for r in results],
                    "sent_to": sum(1 for r in results if r.sent),
                    "failed": sum(1 for r in results if not r.sent),
                }
                self.send_status["next_send_at"] = (
                    _now()
                    + timedelta(seconds=share.auto_send_interval_seconds)
                ).isoformat()

                await asyncio.sleep(share.auto_send_interval_seconds)
            except asyncio.CancelledError:
                self.send_status["loop_alive"] = False
                raise
            except Exception as exc:  # never let the loop die
                self.send_status["last_result"] = {
                    "destination": "analyser",
                    "sent": False,
                    "error": repr(exc),
                }
                await asyncio.sleep(_IDLE_POLL_SECONDS)


scheduler = AutonomousScheduler()
