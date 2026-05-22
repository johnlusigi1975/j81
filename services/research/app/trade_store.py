"""Multi-tenant trade journal backed by SQLite (stdlib only).

Any external bot/system registers once to get an API key, then POSTs its
trades. Trades are stored per account, deduped on the bot's own
`external_id`, and queryable with filters + aggregate stats.

SQLite with WAL is plenty for a self-hosted VPS recorder. All access is
guarded by a process lock and the schema is created on first use. FastAPI
runs the calling endpoints in a threadpool, so blocking is fine.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.config import data_path, get_settings
from app.models import (
    Account,
    AccountCredentials,
    IncomingTrade,
    TradeRecord,
    TradeStats,
)

_COLUMNS = (
    "external_id symbol trade_type direction stake payout profit "
    "entry_price exit_price entry_time exit_time status currency"
).split()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


class TradeStore:
    def __init__(self, db_path: str | None = None) -> None:
        path = db_path or get_settings().trades_db_path
        self._path = data_path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self._path, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id         TEXT PRIMARY KEY,
                    name       TEXT NOT NULL,
                    key_hash   TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id          TEXT PRIMARY KEY,
                    account_id  TEXT NOT NULL,
                    external_id TEXT,
                    symbol      TEXT,
                    trade_type  TEXT,
                    direction   TEXT,
                    stake       REAL,
                    payout      REAL,
                    profit      REAL,
                    entry_price REAL,
                    exit_price  REAL,
                    entry_time  TEXT,
                    exit_time   TEXT,
                    status      TEXT,
                    currency    TEXT,
                    raw         TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_dedupe
                    ON trades(account_id, external_id);
                CREATE INDEX IF NOT EXISTS ix_trades_acct_sym
                    ON trades(account_id, symbol);
                CREATE INDEX IF NOT EXISTS ix_trades_acct_time
                    ON trades(account_id, recorded_at);
                """
            )
            self._conn.commit()

    # --- accounts ----------------------------------------------------------

    def register_account(self, name: str) -> AccountCredentials:
        account_id = uuid4().hex
        api_key = secrets.token_urlsafe(32)
        created = _utcnow_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO accounts(id,name,key_hash,created_at) "
                "VALUES (?,?,?,?)",
                (account_id, name, _hash_key(api_key), created),
            )
            self._conn.commit()
        return AccountCredentials(
            id=account_id, name=name, created_at=created, api_key=api_key
        )

    def authenticate(self, api_key: str) -> str | None:
        row = self._conn.execute(
            "SELECT id FROM accounts WHERE key_hash=?",
            (_hash_key(api_key),),
        ).fetchone()
        return row["id"] if row else None

    def get_account(self, account_id: str) -> Account | None:
        row = self._conn.execute(
            "SELECT id,name,created_at FROM accounts WHERE id=?",
            (account_id,),
        ).fetchone()
        if not row:
            return None
        return Account(
            id=row["id"], name=row["name"], created_at=row["created_at"]
        )

    # --- trades ------------------------------------------------------------

    def record_trades(
        self, account_id: str, trades: list[IncomingTrade]
    ) -> tuple[int, int]:
        recorded = 0
        with self._lock:
            for t in trades:
                payload = t.model_dump()  # known + extra (lossless)
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO trades("
                    "account_id,id," + ",".join(_COLUMNS) + ",raw,recorded_at"
                    ") VALUES (?,?," + ",".join("?" * len(_COLUMNS)) + ",?,?)",
                    (
                        account_id,
                        uuid4().hex,
                        *[getattr(t, c) for c in _COLUMNS],
                        json.dumps(payload, default=str),
                        _utcnow_iso(),
                    ),
                )
                recorded += cur.rowcount
            self._conn.commit()
        return recorded, len(trades) - recorded

    def list_trades(
        self,
        account_id: str,
        *,
        symbol: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TradeRecord]:
        clauses = ["account_id=?"]
        params: list = [account_id]
        if symbol:
            clauses.append("symbol=?")
            params.append(symbol)
        if since:
            clauses.append("recorded_at>=?")
            params.append(since)
        if until:
            clauses.append("recorded_at<=?")
            params.append(until)
        params.extend([max(1, min(limit, 1000)), max(0, offset)])
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE "
            + " AND ".join(clauses)
            + " ORDER BY recorded_at DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def stats(
        self,
        account_id: str,
        *,
        symbol: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> TradeStats:
        clauses = ["account_id=?"]
        params: list = [account_id]
        if symbol:
            clauses.append("symbol=?")
            params.append(symbol)
        if since:
            clauses.append("recorded_at>=?")
            params.append(since)
        if until:
            clauses.append("recorded_at<=?")
            params.append(until)
        rows = self._conn.execute(
            "SELECT profit,status,symbol,stake FROM trades WHERE "
            + " AND ".join(clauses),
            params,
        ).fetchall()

        wins = losses = 0
        total_profit = total_stake = 0.0
        by_symbol: dict[str, int] = {}
        for r in rows:
            won, lost = self._won_lost(r["profit"], r["status"])
            wins += won
            losses += lost
            total_profit += r["profit"] or 0.0
            total_stake += r["stake"] or 0.0
            sym = r["symbol"] or "(unknown)"
            by_symbol[sym] = by_symbol.get(sym, 0) + 1

        total = len(rows)
        decided = wins + losses
        return TradeStats(
            account_id=account_id,
            total=total,
            wins=wins,
            losses=losses,
            win_rate=round(wins / decided, 4) if decided else 0.0,
            total_profit=round(total_profit, 6),
            total_stake=round(total_stake, 6),
            by_symbol=by_symbol,
        )

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _won_lost(profit, status) -> tuple[int, int]:
        if profit is not None:
            if profit > 0:
                return 1, 0
            if profit < 0:
                return 0, 1
            return 0, 0
        s = (status or "").strip().lower()
        if s in {"won", "win"}:
            return 1, 0
        if s in {"lost", "loss", "lose"}:
            return 0, 1
        return 0, 0

    @staticmethod
    def _row_to_trade(r: sqlite3.Row) -> TradeRecord:
        return TradeRecord(
            id=r["id"],
            account_id=r["account_id"],
            external_id=r["external_id"],
            symbol=r["symbol"],
            trade_type=r["trade_type"],
            direction=r["direction"],
            stake=r["stake"],
            payout=r["payout"],
            profit=r["profit"],
            entry_price=r["entry_price"],
            exit_price=r["exit_price"],
            entry_time=r["entry_time"],
            exit_time=r["exit_time"],
            status=r["status"],
            currency=r["currency"],
            raw=json.loads(r["raw"]),
            recorded_at=r["recorded_at"],
        )


_store: TradeStore | None = None


def get_trade_store() -> TradeStore:
    global _store
    if _store is None:
        _store = TradeStore()
    return _store
