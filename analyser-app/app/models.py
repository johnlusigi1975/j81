"""Permissive shapes for what the Researcher sends.

We deliberately don't import the Researcher's Pydantic models — that would
couple the two systems and break the "separated systems via API" promise.
Instead we accept the smallest shape we need (the fields the Analyser
actually queries on) and keep everything else verbatim in `payload` via
extra="allow" so future Researcher changes don't break us.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Provenance(BaseModel):
    model_config = ConfigDict(extra="allow")
    platform: str | None = None
    url: str | None = None
    title: str | None = None
    author: str | None = None


class IncomingStrategy(BaseModel):
    """Whatever the Researcher posts. Extras kept verbatim."""

    model_config = ConfigDict(extra="allow")

    id: str
    name: str = ""
    trade_type: str  # rise_fall | even_odd | over_under | ...
    confidence: float | None = None
    source_language: str | None = None
    provenance: Provenance | None = None


class IncomingInsight(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    category: str | None = None
    summary: str = ""
    sentiment: str | None = None
    trade_type: str | None = None  # null = general
    source_language: str | None = None
    provenance: Provenance | None = None


class StrategyBatch(BaseModel):
    """The Researcher's grouped-by-trade-type payload."""

    by_trade_type: dict[str, list[IncomingStrategy]] = Field(default_factory=dict)


class InsightBatch(BaseModel):
    by_trade_type: dict[str, list[IncomingInsight]] = Field(default_factory=dict)


class IngestResult(BaseModel):
    received: int
    new: int
    duplicates: int
    by_trade_type: dict[str, int]
    at: str = Field(default_factory=_utcnow_iso)


class StoredStrategy(BaseModel):
    id: str
    trade_type: str
    name: str | None = None
    confidence: float | None = None
    source_url: str | None = None
    source_platform: str | None = None
    source_language: str | None = None
    status: str = "received"
    received_at: str
    payload: dict[str, Any] = Field(default_factory=dict)


class StoredInsight(BaseModel):
    id: str
    trade_type: str | None = None
    category: str | None = None
    summary: str | None = None
    sentiment: str | None = None
    source_url: str | None = None
    source_platform: str | None = None
    source_language: str | None = None
    received_at: str
    payload: dict[str, Any] = Field(default_factory=dict)
