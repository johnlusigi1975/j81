"""Researcher brain self-student.

The Researcher is the system with web access, so its self-student doesn't ask a
peer — it studies its own improvement topics directly with a tiny search, logs
what it learned, and files enhancement proposals for the AI (Claude). All
best-effort and capped so it stays a small slice of the Researcher's effort.
"""

from __future__ import annotations

from typing import Any

APP = "researcher"


def diagnose() -> list[dict[str, Any]]:
    """Name concrete ways the Researcher could do better, from its own metrics."""
    from app import productivity

    m = productivity.compute_self()["metrics"]
    out: list[dict[str, Any]] = []

    if not m.get("autonomous_enabled"):
        out.append({
            "topic": "stay-producing",
            "query": "",
            "enhancement": "Autonomous research is OFF — turn it on for continuous output.",
        })
    if m.get("output_last_cycle", 0) < 5:
        out.append({
            "topic": "boost-yield",
            "query": ("best deriv synthetic indices strategy sources youtube "
                      "channels reddit communities high signal 2024"),
            "enhancement": ("Low yield per cycle. Curate higher-signal sources, "
                            "widen hashtags, and soften the extraction prompt to "
                            "capture partial strategies."),
        })
    if m.get("has_error"):
        out.append({
            "topic": "fix-source-errors",
            "query": "",
            "enhancement": "Recent cycle errors — harden the failing source adapters (timeouts/parsing).",
        })
    if not out:
        out.append({
            "topic": "extraction-quality",
            "query": "extract trading strategy rules from text structured output dedupe techniques",
            "enhancement": ("Improve extraction precision: dedupe near-duplicate "
                            "strategies and parse numeric thresholds more reliably."),
        })
    return out


async def study_once(max_self_research: int = 1) -> dict:
    """File enhancement proposals + study the top weakness online (capped)."""
    from app import comms_client

    weaknesses = diagnose()
    researched = 0
    for w in weaknesses:
        await comms_client.log_study(
            "enhancement", w["enhancement"], topic=w["topic"], source="self-diagnosis")
        if w.get("query") and researched < max_self_research:
            await comms_client.log_study(
                "question", w["query"], topic=w["topic"], source="self-research")
            learning = await _self_research(w["query"])
            if learning:
                await comms_client.log_study(
                    "learning", learning, topic=w["topic"], source="own research")
            researched += 1
    return {"weaknesses": len(weaknesses), "self_researched": researched}


async def _self_research(query: str) -> str | None:
    """A tiny one-result-per-source search on an improvement topic (Gemini free
    tier covers this). Returns a one-line learning summary, or None on failure."""
    try:
        from app.models import ResearchRequest
        from app.pipeline import ResearchPipeline
        from app.research_config import load_config

        cfg = load_config()
        sources, focus = cfg.sources.enabled(), cfg.focus.enabled()
        tt = cfg.topics[0].trade_type.value if cfg.topics else None
        if not sources or not focus or not tt:
            return None
        resp = await ResearchPipeline().run(ResearchRequest(
            trade_type=tt, query=query, sources=sources, focus=focus,
            hashtags=[], max_results_per_source=1,
        ))
        return (f"Studied '{query[:60]}': {resp.documents_found} sources, "
                f"{len(resp.strategies)} strategies, {len(resp.insights)} insights found.")
    except Exception:
        return None
