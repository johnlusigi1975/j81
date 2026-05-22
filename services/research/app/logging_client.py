"""Ships gathered intel downstream for testing — arranged by trade type.

J81's job is to gather info and hand it off *concentrated by trade type* so
the backtest app can pull "everything for even_odd" and test it as a set.

Local mode (no LOGGING_APP_URL): files are written under
  out/strategies/<trade_type>/<id>.json
  out/insights/<trade_type|general>/<id>.json
Remote mode: a single grouped payload is POSTed:
  { "by_trade_type": { "even_odd": [ ... ], "rise_fall": [ ... ] } }
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import httpx

from app.config import data_path, get_settings
from app.models import Insight, Strategy


def out_strategies() -> Path:
    return data_path("out/strategies")


def out_insights() -> Path:
    return data_path("out/insights")


def _trade_key(item) -> str:
    tt = getattr(item, "trade_type", None)
    return tt.value if tt is not None else "general"


def _group_by_trade_type(items: list) -> dict[str, list]:
    grouped: dict[str, list] = defaultdict(list)
    for it in items:
        grouped[_trade_key(it)].append(it)
    return dict(grouped)


class LoggingAppClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._url = settings.logging_app_url.rstrip("/")
        self._api_key = settings.logging_app_api_key
        self._timeout = settings.request_timeout_seconds

    async def push(self, strategies: list[Strategy]) -> int:
        if not strategies:
            return 0
        if not self._url:
            return self._write_local(out_strategies(), strategies)
        return await self._post("strategies", strategies)

    async def push_insights(self, insights: list[Insight]) -> int:
        if not insights:
            return 0
        if not self._url:
            return self._write_local(out_insights(), insights)
        return await self._post("insights", insights)

    def _write_local(self, base: Path, items: list) -> int:
        for tkey, group in _group_by_trade_type(items).items():
            out_dir = base / tkey
            out_dir.mkdir(parents=True, exist_ok=True)
            for it in group:
                (out_dir / f"{it.id}.json").write_text(
                    it.model_dump_json(indent=2)
                )
        return len(items)

    async def _post(self, path: str, items: list) -> int:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        by_tt = {
            tkey: [json.loads(it.model_dump_json()) for it in group]
            for tkey, group in _group_by_trade_type(items).items()
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._url}/{path}",
                json={"by_trade_type": by_tt},
                headers=headers,
            )
            resp.raise_for_status()
        return len(items)
