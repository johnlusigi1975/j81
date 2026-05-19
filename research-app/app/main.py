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

from fastapi import Depends, FastAPI, Header, HTTPException

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
from app.library import build_library
from app.pipeline import ResearchPipeline
from app.research_config import ResearchConfig, load_config, save_config
from app.scheduler import scheduler
from app.trade_store import get_trade_store


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler.ensure_running()  # self-gates on config.autonomous.enabled
    yield


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    cfg = load_config()
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "status": "ok",
        "llm_configured": bool(settings.anthropic_api_key),
        "logging_app": "remote" if settings.logging_app_url else "local-file",
        "model": settings.extractor_model,
        "autonomous_enabled": cfg.autonomous.enabled,
    }


@app.post("/research", response_model=ResearchResponse)
async def research(request: ResearchRequest) -> ResearchResponse:
    return await ResearchPipeline().run(request)


@app.post("/study-link", response_model=StudyLinkResponse)
async def study_link(request: StudyLinkRequest) -> StudyLinkResponse:
    """Paste a link you've seen — J81 fetches it, studies the content, and
    extracts strategies/insights, arranged by trade type."""
    return await ResearchPipeline().study_url(request)


@app.get("/library")
def library() -> dict:
    """Everything gathered so far, arranged by trade type — the concentrated
    view that gets sent downstream for testing."""
    return build_library()


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
