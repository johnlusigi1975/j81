"""J81 Deriv Researcher — HTTP service.

  POST /research          run a one-off research job
  POST /study-link        give it a URL; it fetches, studies, and extracts
  GET  /library           everything found, arranged by trade type
  GET  /health            liveness + configuration summary
  GET  /config            view the control surface (sources/focus/topics)
  PUT  /config            replace the control surface (validated)
  GET  /autonomous/status scheduler state + last cycle summary
  POST /autonomous/start   turn the 24/7 self-research loop ON
  POST /autonomous/stop    turn the 24/7 self-research loop OFF

The autonomous loop starts with the process and self-gates on
config.autonomous.enabled, so on a VPS you keep the process alive (systemd/
tmux) and flip it on/off via /autonomous/* or by editing the config file.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.config import APP_NAME, APP_VERSION, get_settings
from app.models import (
    Account,
    AccountCredentials,
    AccountRegistration,
    ResearchRequest,
    ResearchResponse,
    StudyLinkRequest,
    StudyLinkResponse,
    TradeIngestRequest,
    TradeIngestResult,
    TradeRecord,
    TradeStats,
)
from app import connections as conn_store
from app.connections import ApiConnectionInput, ApiConnectionPublic, send_payload, test_connection
from app.library import build_library
from app.pipeline import ResearchPipeline
from app.research_config import ResearchConfig, load_config, save_config
from app.scheduler import scheduler
from app.sharing import SendResult, _archive_current, _counts, _now_iso, send_to_analyser, send_to_webhook, send_via_email
from app.trade_store import get_trade_store


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler.ensure_running()  # self-gates on config.autonomous.enabled
    yield


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)

_HOMEPAGE = Path(__file__).parent / "web" / "index.html"


@app.get("/", include_in_schema=False)
def homepage() -> FileResponse:
    return FileResponse(_HOMEPAGE, media_type="text/html")


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    cfg = load_config()
    provider = (settings.llm_provider or "anthropic").strip().lower()
    if provider == "google":
        llm_configured = bool(settings.google_api_key)
        active_model = settings.gemini_model
    else:
        llm_configured = bool(settings.anthropic_api_key)
        active_model = settings.extractor_model
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "status": "ok",
        "llm_provider": provider,
        "llm_configured": llm_configured,
        "model": active_model,
        "logging_app": "remote" if settings.logging_app_url else "local-file",
        "autonomous_enabled": cfg.autonomous.enabled,
        "source_apis": {
            "reddit_oauth": bool(
                settings.reddit_client_id and settings.reddit_client_secret
            ),
            "youtube_data_api": bool(settings.youtube_api_key),
            "tavily_web_search": bool(settings.tavily_api_key),
        },
    }


@app.post("/research", response_model=ResearchResponse)
async def research(request: ResearchRequest) -> ResearchResponse:
    return await ResearchPipeline().run(request)


@app.post("/study-link", response_model=StudyLinkResponse)
async def study_link(request: StudyLinkRequest) -> StudyLinkResponse:
    """Paste a link you've seen — J81 fetches it, studies the content, and
    extracts strategies/insights, arranged by trade type."""
    return await ResearchPipeline().study_url(request)


class ResearchRequestIn(BaseModel):
    """Inbound from the J81 Analyser when it spots a gap and wants the
    Researcher to fetch more on a topic."""

    topic_name: str
    query: str
    trade_type: str | None = None
    why: str | None = None
    priority: str | None = "medium"


@app.post("/research-requests")
def research_requests_create(req: ResearchRequestIn) -> dict:
    """Analyser asks the Researcher to add a topic. We dedupe by name and
    persist; the next autonomous cycle will pick it up automatically."""
    from app.models import TradeType
    from app.research_config import ResearchTopic
    try:
        tt = TradeType(req.trade_type) if req.trade_type else TradeType.RISE_FALL
    except ValueError:
        raise HTTPException(400, f"invalid trade_type: {req.trade_type!r}")
    cfg = load_config()
    if any(t.name == req.topic_name for t in cfg.topics):
        return {"accepted": False, "reason": "duplicate topic_name",
                "topics": len(cfg.topics)}
    cfg.topics.append(
        ResearchTopic(
            name=req.topic_name,
            query=req.query,
            trade_type=tt,
            hashtags=["#deriv"],
            enabled=True,
        )
    )
    save_config(cfg)
    return {
        "accepted": True,
        "topic_name": req.topic_name,
        "topics": len(cfg.topics),
        "note": "added; the next autonomous cycle will pick this up",
    }


@app.get("/library")
def library() -> dict:
    """Everything gathered so far, arranged by trade type — the concentrated
    view that gets sent downstream for testing."""
    return build_library()


@app.get("/library.json", include_in_schema=False)
def library_download() -> JSONResponse:
    """Same payload as /library, but with a download disposition so a
    browser saves it as j81-library.json."""
    return JSONResponse(
        content=build_library(),
        headers={
            "Content-Disposition": 'attachment; filename="j81-library.json"'
        },
    )


# ---------------------------------------------------------------------------
# Share the library to other places
# ---------------------------------------------------------------------------


class SendLibraryRequest(BaseModel):
    destination: str  # "analyser" | "connection" | "webhook" | "email"
    url: str | None = None  # webhook URL
    api_key: str | None = None  # optional bearer for webhook
    email: str | None = None  # recipient for email
    connection_id: str | None = None  # used with destination="connection"
    archive: bool | None = None  # override config default


class SendBatchResult(BaseModel):
    """Uniform response shape for /library/send. analyser fans out, the
    others are single-destination — wrapping both in a list keeps the
    frontend simple."""

    results: list[SendResult]
    sent: int
    failed: int


def _summarise(results: list[SendResult]) -> SendBatchResult:
    return SendBatchResult(
        results=results,
        sent=sum(1 for r in results if r.sent),
        failed=sum(1 for r in results if not r.sent),
    )


def _record_manual_send(results: list[SendResult]) -> None:
    """Mirror a manual send into the scheduler's send_status so the homepage's
    status panel reflects it (the panel reads scheduler.send_status). Without
    this, a manual send left the panel showing the last *auto* send."""
    sent_to = sum(1 for r in results if r.sent)
    scheduler.send_status["last_result"] = {
        "trigger": "manual",
        "destinations": [r.model_dump() for r in results],
        "sent_to": sent_to,
        "failed": sum(1 for r in results if not r.sent),
    }
    if sent_to:
        scheduler.send_status["last_send_at"] = _now_iso()


@app.post("/library/send", response_model=SendBatchResult)
async def library_send(req: SendLibraryRequest) -> SendBatchResult:
    """Ship the current library to one of the supported destinations.
      * "analyser"   -> fan-out: env LOGGING_APP_URL + every enabled connection
      * "connection" -> a single saved connection (req.connection_id)
      * "webhook"    -> ad-hoc URL (req.url, optional req.api_key)
      * "email"      -> SMTP (req.email)"""
    dest = req.destination.lower().strip()
    if dest == "analyser":
        results = await send_to_analyser(archive=req.archive)
    elif dest == "connection":
        if not req.connection_id:
            raise HTTPException(400, "connection destination needs 'connection_id'")
        results = await _send_to_single_connection(req.connection_id, req.archive)
    elif dest == "webhook":
        if not req.url:
            raise HTTPException(400, "webhook destination needs 'url'")
        results = [await send_to_webhook(req.url, req.api_key or "", archive=req.archive)]
    elif dest == "email":
        if not req.email:
            raise HTTPException(400, "email destination needs 'email'")
        results = [await send_via_email(req.email, archive=req.archive)]
    else:
        raise HTTPException(
            400, "destination must be one of: analyser, connection, webhook, email"
        )
    _record_manual_send(results)
    return _summarise(results)


async def _send_to_single_connection(
    connection_id: str, archive: bool | None
) -> list[SendResult]:
    """Send the current library to one specific saved connection. Archives
    only on a successful send (matches the fan-out policy)."""
    c = conn_store.get_internal(connection_id)
    if c is None:
        raise HTTPException(404, "connection not found")
    payload = build_library()
    s_count, i_count = _counts(payload)
    if s_count == 0 and i_count == 0:
        return [
            SendResult(
                destination=f"connection:{c.name}",
                sent=False,
                strategies_sent=0,
                insights_sent=0,
                error="library is empty — nothing to send",
            )
        ]
    ok, err = await send_payload(c, payload)
    result = SendResult(
        destination=f"connection:{c.name}",
        sent=ok,
        strategies_sent=s_count if ok else 0,
        insights_sent=i_count if ok else 0,
        sent_at=_now_iso() if ok else None,
        error=err,
    )
    if archive is None:
        archive = load_config().sharing.archive_after_send
    if archive and ok:
        import asyncio
        await asyncio.to_thread(_archive_current)
        result.archived = True
    return [result]


# ---------------------------------------------------------------------------
# Analyser connections — the multi-destination API backbone
# ---------------------------------------------------------------------------


@app.get("/connections", response_model=list[ApiConnectionPublic])
def connections_list() -> list[ApiConnectionPublic]:
    return conn_store.list_public()


@app.post("/connections", response_model=ApiConnectionPublic)
def connections_create(body: ApiConnectionInput) -> ApiConnectionPublic:
    return conn_store.create(body)


@app.put("/connections/{conn_id}", response_model=ApiConnectionPublic)
def connections_update(conn_id: str, body: ApiConnectionInput) -> ApiConnectionPublic:
    updated = conn_store.update(conn_id, body)
    if updated is None:
        raise HTTPException(404, "connection not found")
    return updated


@app.delete("/connections/{conn_id}")
def connections_delete(conn_id: str) -> dict:
    if conn_id == "__env__":
        raise HTTPException(400, "the LOGGING_APP_URL connection is managed via env vars")
    if not conn_store.delete(conn_id):
        raise HTTPException(404, "connection not found")
    return {"deleted": conn_id}


@app.post("/connections/{conn_id}/test")
async def connections_test(conn_id: str) -> dict:
    c = conn_store.get_internal(conn_id)
    if c is None:
        if conn_id == "__env__":
            from app.connections import env_connection
            c = env_connection()
        if c is None:
            raise HTTPException(404, "connection not found")
    ok, err = await test_connection(c)
    return {"ok": ok, "error": err, "base_url": c.base_url}


@app.post("/connections/{conn_id}/send", response_model=SendBatchResult)
async def connections_send_now(conn_id: str) -> SendBatchResult:
    """Send the current library to one specific connection regardless of
    its enabled / auto-send flags. Doesn't archive (manual one-off)."""
    return _summarise(await _send_to_single_connection(conn_id, archive=False))


# ---------------------------------------------------------------------------
# One-shot setup endpoint — lets the homepage receive the Anthropic API key
# from the user without them needing to touch a terminal. Refuses to run
# once a key is already configured (you'd edit .env manually after that).
# ---------------------------------------------------------------------------

import os
import re

ENV_FILE = Path(__file__).parent.parent / ".env"
_KEY_RE = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{20,}$")


class SetupKeyRequest(BaseModel):
    api_key: str


@app.post("/setup/anthropic-key")
def setup_anthropic_key(body: SetupKeyRequest) -> dict:
    """Write the user's Anthropic API key into research-app/.env.
    One-shot: refuses if a key is already configured. After first call,
    edit .env directly to change keys."""
    settings = get_settings()
    if settings.anthropic_api_key:
        raise HTTPException(
            status_code=409,
            detail="An API key is already configured. To change it, edit "
            "research-app/.env directly.",
        )
    key = body.api_key.strip()
    if not _KEY_RE.match(key):
        raise HTTPException(
            status_code=400,
            detail="That doesn't look like an Anthropic API key. It should "
            "start with 'sk-ant-' and be at least ~28 chars long.",
        )

    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Preserve any existing lines if the file exists (it shouldn't, since we
    # gated on llm_configured == False above, but be defensive).
    existing = ""
    if ENV_FILE.exists():
        existing = ENV_FILE.read_text()
        if "ANTHROPIC_API_KEY" in existing:
            raise HTTPException(409, "ANTHROPIC_API_KEY already present in .env")
    new_content = (
        existing.rstrip() + "\n" if existing else ""
    ) + f"ANTHROPIC_API_KEY={key}\nEXTRACTOR_MODEL=claude-opus-4-7\n"
    ENV_FILE.write_text(new_content)
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass

    # Bust the settings cache so the next /health request reflects the new key
    # without waiting for the uvicorn --reload restart to land.
    get_settings.cache_clear()
    return {"saved": True, "path": str(ENV_FILE)}


_GOOGLE_KEY_RE = re.compile(r"^AIza[0-9A-Za-z_\-]{30,}$")


@app.post("/setup/gemini-key")
def setup_gemini_key(body: SetupKeyRequest) -> dict:
    """Save the user's free Google (Gemini) API key into research-app/.env,
    switch LLM_PROVIDER to google, and turn on the 10-minute research cadence
    — all in one go. This is the cheap path for frequent cycles. The key never
    leaves the machine. One-shot: refuses if a Google key is already set."""
    settings = get_settings()
    if settings.google_api_key:
        raise HTTPException(
            409, "A Google API key is already configured. Edit research-app/.env to change it."
        )
    key = body.api_key.strip()
    if not _GOOGLE_KEY_RE.match(key):
        raise HTTPException(
            400,
            "That doesn't look like a Google AI Studio key — it should start "
            "with 'AIza'. Get a free one at https://aistudio.google.com/apikey.",
        )

    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = ENV_FILE.read_text() if ENV_FILE.exists() else ""
    if "GOOGLE_API_KEY" in existing:
        raise HTTPException(409, "GOOGLE_API_KEY already present in .env")
    # Drop any prior LLM_PROVIDER line; we set it to google below.
    kept = [ln for ln in existing.splitlines() if not ln.startswith("LLM_PROVIDER=")]
    base = "\n".join(kept).rstrip()
    new_content = (
        (base + "\n" if base else "")
        + f"GOOGLE_API_KEY={key}\nGEMINI_MODEL=gemini-2.5-flash\nLLM_PROVIDER=google\n"
    )
    ENV_FILE.write_text(new_content)
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass

    # Bust the cache so the next research cycle builds its pipeline on Gemini
    # (no restart needed — a fresh ResearchPipeline reads settings each cycle).
    get_settings.cache_clear()
    # Now that research is cheap, switch on the 10-minute cadence.
    cfg = load_config()
    cfg.autonomous.interval_seconds = 600
    cfg.autonomous.enabled = True
    save_config(cfg)
    return {
        "saved": True,
        "provider": "google",
        "model": "gemini-2.5-flash",
        "research_interval_seconds": 600,
        "note": "Switched to Gemini + 10-minute research. Free tier covers this.",
    }


@app.get("/library/sharing/status")
def sharing_status() -> dict:
    """Auto-send loop status + last result (so the UI can show it)."""
    cfg = load_config().sharing
    return {"config": cfg.model_dump(), "runtime": scheduler.send_status}


@app.get("/config", response_model=ResearchConfig)
def get_config() -> ResearchConfig:
    return load_config()


@app.put("/config", response_model=ResearchConfig)
def put_config(cfg: ResearchConfig) -> ResearchConfig:
    save_config(cfg)
    return cfg


@app.get("/autonomous/status")
def autonomous_status() -> dict:
    cfg = load_config()
    return {"config": cfg.autonomous.model_dump(), "runtime": scheduler.status}


@app.post("/autonomous/start")
async def autonomous_start() -> dict:
    await scheduler.enable()
    return {"config": load_config().autonomous.model_dump(), "runtime": scheduler.status}


@app.post("/autonomous/stop")
async def autonomous_stop() -> dict:
    await scheduler.disable()
    return {"config": load_config().autonomous.model_dump(), "runtime": scheduler.status}


# ---------------------------------------------------------------------------
# Connected-bot trade recording
# ---------------------------------------------------------------------------


def require_account(authorization: str = Header(default="")) -> str:
    """Resolve the calling bot's account from its API key."""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing api key")
    account_id = get_trade_store().authenticate(token)
    if account_id is None:
        raise HTTPException(status_code=401, detail="invalid api key")
    return account_id


@app.post("/accounts/register", response_model=AccountCredentials)
def register_account(body: AccountRegistration) -> AccountCredentials:
    """Register a bot/system. The api_key is returned ONCE — store it; we
    only keep a hash."""
    return get_trade_store().register_account(body.name)


@app.get("/accounts/me", response_model=Account)
def whoami(account_id: str = Depends(require_account)) -> Account:
    acct = get_trade_store().get_account(account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="account not found")
    return acct


@app.post("/trades", response_model=TradeIngestResult)
def ingest_trades(
    body: TradeIngestRequest, account_id: str = Depends(require_account)
) -> TradeIngestResult:
    recorded, dupes = get_trade_store().record_trades(
        account_id, body.trades
    )
    return TradeIngestResult(
        recorded=recorded, duplicates_skipped=dupes, account_id=account_id
    )


@app.get("/trades", response_model=list[TradeRecord])
def list_trades(
    account_id: str = Depends(require_account),
    symbol: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[TradeRecord]:
    return get_trade_store().list_trades(
        account_id,
        symbol=symbol,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )


@app.get("/trades/stats", response_model=TradeStats)
def trade_stats(
    account_id: str = Depends(require_account),
    symbol: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> TradeStats:
    return get_trade_store().stats(
        account_id, symbol=symbol, since=since, until=until
    )


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
