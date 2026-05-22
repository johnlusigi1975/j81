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
from uuid import uuid4

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
from app.library import build_library
from app.pipeline import ResearchPipeline
from app.research_config import Connector, ResearchConfig, load_config, save_config
from app.scheduler import scheduler
from app.api_backbone import redact, test_reachability
from app.sharing import (
    FanOutResult,
    SendResult,
    send_to_analyser,
    send_to_connector,
    send_to_webhook,
    send_via_email,
)
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
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "status": "ok",
        "llm_configured": bool(settings.anthropic_api_key),
        "logging_app": "remote" if settings.logging_app_url else "local-file",
        "model": settings.extractor_model,
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
    destination: str  # "analyser" | "webhook" | "email" | "connector"
    url: str | None = None  # webhook URL
    api_key: str | None = None  # optional bearer for webhook
    email: str | None = None  # recipient for email
    connector_id: str | None = None  # for destination=connector or analyser
    archive: bool | None = None  # override config default


@app.post("/library/send")
async def library_send(req: SendLibraryRequest):
    """Manually ship the current library. Destinations:
      * `analyser`  — fan out to every enabled analyser connector
      * `connector` — send to one specific saved connector (needs connector_id)
      * `webhook`   — one-off URL (no save)
      * `email`     — SMTP attachment
    """
    dest = req.destination.lower().strip()
    if dest == "analyser":
        return await send_to_analyser(
            archive=req.archive, connector_id=req.connector_id
        )
    if dest == "connector":
        if not req.connector_id:
            raise HTTPException(
                status_code=400, detail="connector destination needs 'connector_id'"
            )
        cfg = load_config()
        c = next((c for c in cfg.connectors if c.id == req.connector_id), None)
        if c is None:
            raise HTTPException(status_code=404, detail="connector not found")
        return await send_to_connector(c, archive=req.archive)
    if dest == "webhook":
        if not req.url:
            raise HTTPException(
                status_code=400, detail="webhook destination needs a 'url'"
            )
        return await send_to_webhook(
            req.url, req.api_key or "", archive=req.archive
        )
    if dest == "email":
        if not req.email:
            raise HTTPException(
                status_code=400, detail="email destination needs an 'email'"
            )
        return await send_via_email(req.email, archive=req.archive)
    raise HTTPException(
        status_code=400,
        detail="destination must be one of: analyser, webhook, email",
    )


@app.get("/library/sharing/status")
def sharing_status() -> dict:
    """Auto-send loop status + last result (so the UI can show it)."""
    cfg = load_config().sharing
    return {"config": cfg.model_dump(), "runtime": scheduler.send_status}


# ---------------------------------------------------------------------------
# Connectors — generalized outbound-API registry
# ---------------------------------------------------------------------------


@app.get("/connectors")
def list_connectors() -> list[dict]:
    """List saved connectors. Secret fields (token, password) are masked."""
    return [redact(c) for c in load_config().connectors]


@app.post("/connectors")
def create_connector(connector: Connector) -> dict:
    """Add a new connector. The supplied `id` is replaced with a fresh one
    server-side, so each connector has a unique handle."""
    cfg = load_config()
    # always generate a server-side id, ignore any client-sent value
    fresh = connector.model_copy(update={"id": uuid4().hex[:12]})
    cfg.connectors.append(fresh)
    save_config(cfg)
    return redact(fresh)


@app.put("/connectors/{connector_id}")
def update_connector(connector_id: str, patch: dict) -> dict:
    """Patch an existing connector. Pass only the fields you want to change.
    A masked '***' value for `token`/`password` means 'keep existing'."""
    cfg = load_config()
    idx = next((i for i, c in enumerate(cfg.connectors) if c.id == connector_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="connector not found")
    current = cfg.connectors[idx].model_dump()
    # nested merge so partial updates work
    for k, v in patch.items():
        if k == "auth" and isinstance(v, dict):
            merged = {**current.get("auth", {}), **v}
            # preserve existing secrets when the client sent the mask back
            if merged.get("token") == "***":
                merged["token"] = current.get("auth", {}).get("token", "")
            if merged.get("password") == "***":
                merged["password"] = current.get("auth", {}).get("password", "")
            current["auth"] = merged
        else:
            current[k] = v
    current["id"] = connector_id  # id is immutable
    cfg.connectors[idx] = Connector.model_validate(current)
    save_config(cfg)
    return redact(cfg.connectors[idx])


@app.delete("/connectors/{connector_id}")
def delete_connector(connector_id: str) -> dict:
    cfg = load_config()
    before = len(cfg.connectors)
    cfg.connectors = [c for c in cfg.connectors if c.id != connector_id]
    if len(cfg.connectors) == before:
        raise HTTPException(status_code=404, detail="connector not found")
    save_config(cfg)
    return {"deleted": connector_id}


@app.post("/connectors/{connector_id}/test")
async def test_connector(connector_id: str) -> dict:
    cfg = load_config()
    c = next((c for c in cfg.connectors if c.id == connector_id), None)
    if c is None:
        raise HTTPException(status_code=404, detail="connector not found")
    return (await test_reachability(c)).model_dump()


@app.post("/connectors/{connector_id}/send-library")
async def send_library_to_connector(
    connector_id: str, archive: bool | None = None
) -> SendResult:
    cfg = load_config()
    c = next((c for c in cfg.connectors if c.id == connector_id), None)
    if c is None:
        raise HTTPException(status_code=404, detail="connector not found")
    return await send_to_connector(c, archive=archive)


@app.get("/config")
def get_config() -> dict:
    """The control surface. Connector secrets are masked here — manage
    connectors via /connectors to avoid round-tripping tokens through the
    browser."""
    cfg = load_config()
    out = cfg.model_dump()
    out["connectors"] = [redact(c) for c in cfg.connectors]
    return out


@app.put("/config")
def put_config(cfg: ResearchConfig) -> dict:
    """Update the control surface. The submitted `connectors` array is
    IGNORED — use the dedicated /connectors endpoints (this prevents the
    UI from overwriting connector secrets with their masked '***' form)."""
    existing = load_config()
    cfg.connectors = existing.connectors
    save_config(cfg)
    out = cfg.model_dump()
    out["connectors"] = [redact(c) for c in cfg.connectors]
    return out


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
