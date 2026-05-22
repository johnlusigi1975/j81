"""SQLite-backed store for incoming strategies/insights.

Schema is intentionally flat: a few queryable columns + the full original
payload as a JSON blob. Indexed by (trade_type, received_at) for the
homepage's grouped-by-trade-type view.

Deduplication: INSERT OR IGNORE on the `id` column means the Researcher
can safely re-send the same strategy (e.g. after a network blip during
fan-out); duplicates are silently skipped.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import data_path
from app.models import (
    IncomingInsight,
    IncomingStrategy,
    StoredInsight,
    StoredStrategy,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AnalyserStore:
    def __init__(self, db_path: str | None = None) -> None:
        path = Path(db_path) if db_path else data_path("data/analyser.db")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS strategies (
                    id              TEXT PRIMARY KEY,
                    trade_type      TEXT NOT NULL,
                    name            TEXT,
                    confidence      REAL,
                    source_url      TEXT,
                    source_platform TEXT,
                    source_language TEXT,
                    payload         TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'received',
                    received_at     TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_strat_tt
                    ON strategies(trade_type);
                CREATE INDEX IF NOT EXISTS ix_strat_received
                    ON strategies(received_at);
                CREATE INDEX IF NOT EXISTS ix_strat_status
                    ON strategies(status);

                CREATE TABLE IF NOT EXISTS insights (
                    id              TEXT PRIMARY KEY,
                    trade_type      TEXT,
                    category        TEXT,
                    summary         TEXT,
                    sentiment       TEXT,
                    source_url      TEXT,
                    source_platform TEXT,
                    source_language TEXT,
                    payload         TEXT NOT NULL,
                    received_at     TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_ins_tt
                    ON insights(trade_type);
                CREATE INDEX IF NOT EXISTS ix_ins_received
                    ON insights(received_at);
                CREATE INDEX IF NOT EXISTS ix_ins_cat
                    ON insights(category);

                /* Phase 2.1: cached market-data candles from Deriv.
                   Composite PK ensures the same (symbol, granularity, epoch)
                   triple is unique — re-fetching the same window is idempotent. */
                CREATE TABLE IF NOT EXISTS candles (
                    symbol       TEXT NOT NULL,
                    granularity  INTEGER NOT NULL,
                    epoch        INTEGER NOT NULL,
                    open         REAL NOT NULL,
                    high         REAL NOT NULL,
                    low          REAL NOT NULL,
                    close        REAL NOT NULL,
                    PRIMARY KEY (symbol, granularity, epoch)
                );
                CREATE INDEX IF NOT EXISTS ix_candles_lookup
                    ON candles(symbol, granularity, epoch DESC);

                /* Phase 2.3: backtest results — one row per strategy * backtest run. */
                CREATE TABLE IF NOT EXISTS backtest_results (
                    id            TEXT PRIMARY KEY,
                    strategy_id   TEXT NOT NULL,
                    symbol        TEXT NOT NULL,
                    granularity   INTEGER NOT NULL,
                    trade_count   INTEGER,
                    wins          INTEGER,
                    losses        INTEGER,
                    win_rate      REAL,
                    total_pnl     REAL,
                    profit_factor REAL,
                    status        TEXT,           -- survived | rejected | inconclusive | error
                    details       TEXT NOT NULL,  -- full JSON result blob
                    tested_at     TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_bt_strategy ON backtest_results(strategy_id);
                CREATE INDEX IF NOT EXISTS ix_bt_status   ON backtest_results(status);

                /* Phase 2.3: decision log — what the brain output, when, why. */
                CREATE TABLE IF NOT EXISTS decisions (
                    id                       TEXT PRIMARY KEY,
                    symbol                   TEXT NOT NULL,
                    trade_type               TEXT,
                    direction                TEXT,
                    prediction               INTEGER,
                    duration                 INTEGER,
                    duration_unit            TEXT,
                    confidence               REAL NOT NULL,
                    rationale                TEXT,
                    contributing_strategies  TEXT,         -- JSON array of strategy IDs
                    market_context           TEXT,         -- JSON snapshot
                    is_trade                 INTEGER NOT NULL DEFAULT 0,  -- 0 = no-trade, 1 = trade
                    created_at               TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_dec_symbol ON decisions(symbol, created_at DESC);
                CREATE INDEX IF NOT EXISTS ix_dec_trade  ON decisions(is_trade);

                /* Phase 4: inter-app comms bus. The Analyser is the hub;
                   Researcher and Bot POST here and poll their inbox. */
                CREATE TABLE IF NOT EXISTS comms (
                    id          TEXT PRIMARY KEY,
                    from_app    TEXT NOT NULL,   -- researcher | analyser | bot
                    to_app      TEXT NOT NULL,   -- researcher | analyser | bot | all
                    type        TEXT NOT NULL,   -- command | advice | grade | report | request
                    subject     TEXT,
                    body        TEXT,
                    grade       REAL,            -- 0-10, when type='grade'
                    data        TEXT,            -- JSON structured payload
                    acked       INTEGER NOT NULL DEFAULT 0,
                    created_at  TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_comms_to   ON comms(to_app, acked);
                CREATE INDEX IF NOT EXISTS ix_comms_time ON comms(created_at DESC);

                /* Productivity registry: each system reports its own latest
                   self-assessed productivity here; peers read it to grade each
                   other and write recommendations. One row per app (upserted). */
                CREATE TABLE IF NOT EXISTS productivity (
                    app         TEXT PRIMARY KEY,   -- researcher | analyser | bot
                    score       REAL,               -- 0-100 self-assessed rate
                    summary     TEXT,               -- one-line human summary
                    metrics     TEXT,               -- JSON of the raw numbers
                    updated_at  TEXT NOT NULL
                );

                /* J81 Maintenance sector: every system files its problems here.
                   The homepage exports these as a copy-paste block for the AI
                   to fix. status: open | resolved. */
                CREATE TABLE IF NOT EXISTS maintenance (
                    id          TEXT PRIMARY KEY,
                    from_app    TEXT NOT NULL,
                    severity    TEXT NOT NULL DEFAULT 'info',  -- info|warning|error|critical
                    area        TEXT,                          -- subsystem/file/feature
                    summary     TEXT NOT NULL,
                    detail      TEXT,
                    status      TEXT NOT NULL DEFAULT 'open',
                    created_at  TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_maint_status ON maintenance(status, created_at DESC);

                /* Brain self-study: each system's "learn how to be better" log.
                   kind=question  -> what it wants to study (sent to Researcher)
                   kind=learning  -> what it learned back from the Researcher
                   kind=enhancement -> a concrete proposal for the AI (Claude). */
                CREATE TABLE IF NOT EXISTS study (
                    id          TEXT PRIMARY KEY,
                    app         TEXT NOT NULL,
                    kind        TEXT NOT NULL,   -- question | learning | enhancement
                    topic       TEXT,            -- short weakness key (dedupe)
                    body        TEXT NOT NULL,
                    source      TEXT,            -- e.g. researcher answer / metric
                    status      TEXT NOT NULL DEFAULT 'open',  -- open | done
                    created_at  TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_study_app ON study(app, kind, status, created_at DESC);

                /* Small key/value store for runtime flags like priority_mode. */
                CREATE TABLE IF NOT EXISTS app_state (
                    k          TEXT PRIMARY KEY,
                    v          TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------ ingest

    def record_strategies(
        self, batch: dict[str, list[IncomingStrategy]]
    ) -> tuple[int, int, dict[str, int]]:
        attempted = 0
        inserted = 0
        per_tt: dict[str, int] = defaultdict(int)
        now = _now_iso()
        with self._lock:
            for tt, items in batch.items():
                for s in items:
                    attempted += 1
                    payload = s.model_dump()
                    prov = payload.get("provenance") or {}
                    cur = self._conn.execute(
                        "INSERT OR IGNORE INTO strategies("
                        "id,trade_type,name,confidence,source_url,"
                        "source_platform,source_language,payload,received_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            s.id,
                            tt,
                            s.name or None,
                            s.confidence,
                            (prov.get("url") if isinstance(prov, dict) else None),
                            (prov.get("platform") if isinstance(prov, dict) else None),
                            s.source_language,
                            json.dumps(payload, default=str),
                            now,
                        ),
                    )
                    if cur.rowcount:
                        inserted += 1
                        per_tt[tt] += 1
            self._conn.commit()
        return attempted, inserted, dict(per_tt)

    def record_insights(
        self, batch: dict[str, list[IncomingInsight]]
    ) -> tuple[int, int, dict[str, int]]:
        attempted = 0
        inserted = 0
        per_tt: dict[str, int] = defaultdict(int)
        now = _now_iso()
        with self._lock:
            for tt, items in batch.items():
                tt_key = tt if tt != "general" else None
                for i in items:
                    attempted += 1
                    payload = i.model_dump()
                    prov = payload.get("provenance") or {}
                    cur = self._conn.execute(
                        "INSERT OR IGNORE INTO insights("
                        "id,trade_type,category,summary,sentiment,"
                        "source_url,source_platform,source_language,"
                        "payload,received_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            i.id,
                            tt_key,
                            i.category,
                            i.summary or None,
                            i.sentiment,
                            (prov.get("url") if isinstance(prov, dict) else None),
                            (prov.get("platform") if isinstance(prov, dict) else None),
                            i.source_language,
                            json.dumps(payload, default=str),
                            now,
                        ),
                    )
                    if cur.rowcount:
                        inserted += 1
                        per_tt[tt] += 1
            self._conn.commit()
        return attempted, inserted, dict(per_tt)

    # ------------------------------------------------------------------- query

    def list_strategies(
        self,
        *,
        trade_type: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[StoredStrategy]:
        clauses: list[str] = []
        params: list[Any] = []
        if trade_type:
            clauses.append("trade_type=?")
            params.append(trade_type)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([max(1, min(limit, 1000)), max(0, offset)])
        rows = self._conn.execute(
            f"SELECT * FROM strategies {where} "
            "ORDER BY received_at DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [self._row_to_strategy(r) for r in rows]

    def list_insights(
        self,
        *,
        trade_type: str | None = None,
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[StoredInsight]:
        """trade_type='general' filters to rows with NULL trade_type;
        any other string filters exactly; omit to return all."""
        clauses: list[str] = []
        params: list[Any] = []
        if trade_type == "general":
            clauses.append("trade_type IS NULL")
        elif trade_type:
            clauses.append("trade_type=?")
            params.append(trade_type)
        if category:
            clauses.append("category=?")
            params.append(category)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([max(1, min(limit, 1000)), max(0, offset)])
        rows = self._conn.execute(
            f"SELECT * FROM insights {where} "
            "ORDER BY received_at DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [self._row_to_insight(r) for r in rows]

    def stats(self) -> dict:
        s_total = self._conn.execute(
            "SELECT COUNT(*) FROM strategies"
        ).fetchone()[0]
        i_total = self._conn.execute(
            "SELECT COUNT(*) FROM insights"
        ).fetchone()[0]
        s_by_tt = {
            r[0]: r[1]
            for r in self._conn.execute(
                "SELECT trade_type, COUNT(*) FROM strategies "
                "GROUP BY trade_type ORDER BY trade_type"
            ).fetchall()
        }
        i_by_tt = {
            (r[0] or "general"): r[1]
            for r in self._conn.execute(
                "SELECT trade_type, COUNT(*) FROM insights "
                "GROUP BY trade_type ORDER BY trade_type"
            ).fetchall()
        }
        s_by_status = {
            r[0]: r[1]
            for r in self._conn.execute(
                "SELECT status, COUNT(*) FROM strategies "
                "GROUP BY status"
            ).fetchall()
        }
        return {
            "strategies": {
                "total": s_total,
                "by_trade_type": s_by_tt,
                "by_status": s_by_status,
            },
            "insights": {
                "total": i_total,
                "by_trade_type": i_by_tt,
            },
        }

    # --------------------------------------------------------------- candles

    def upsert_candles(
        self,
        symbol: str,
        granularity: int,
        candles: list[dict[str, Any]],
    ) -> int:
        """Insert candles, dedup on (symbol, granularity, epoch).
        Returns how many rows were actually new."""
        if not candles:
            return 0
        rows = [
            (
                symbol,
                granularity,
                int(c["epoch"]),
                float(c["open"]),
                float(c["high"]),
                float(c["low"]),
                float(c["close"]),
            )
            for c in candles
        ]
        with self._lock:
            before = self._conn.execute(
                "SELECT COUNT(*) FROM candles WHERE symbol=? AND granularity=?",
                (symbol, granularity),
            ).fetchone()[0]
            self._conn.executemany(
                "INSERT OR IGNORE INTO candles("
                "symbol,granularity,epoch,open,high,low,close) "
                "VALUES (?,?,?,?,?,?,?)",
                rows,
            )
            after = self._conn.execute(
                "SELECT COUNT(*) FROM candles WHERE symbol=? AND granularity=?",
                (symbol, granularity),
            ).fetchone()[0]
            self._conn.commit()
        return after - before

    def list_candles(
        self,
        symbol: str,
        granularity: int,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        clauses = ["symbol=?", "granularity=?"]
        params: list[Any] = [symbol, granularity]
        if start is not None:
            clauses.append("epoch>=?")
            params.append(int(start))
        if end is not None:
            clauses.append("epoch<=?")
            params.append(int(end))
        params.append(max(1, min(limit, 50000)))
        rows = self._conn.execute(
            "SELECT epoch,open,high,low,close FROM candles WHERE "
            + " AND ".join(clauses)
            + " ORDER BY epoch ASC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def candle_cache_stats(self) -> dict[str, Any]:
        rows = self._conn.execute(
            "SELECT symbol, granularity, COUNT(*) AS n, "
            "MIN(epoch) AS first_epoch, MAX(epoch) AS last_epoch "
            "FROM candles GROUP BY symbol, granularity "
            "ORDER BY symbol, granularity"
        ).fetchall()
        return {
            "series": [
                {
                    "symbol": r["symbol"],
                    "granularity": r["granularity"],
                    "candles": r["n"],
                    "first_epoch": r["first_epoch"],
                    "last_epoch": r["last_epoch"],
                }
                for r in rows
            ],
            "total_candles": sum(r["n"] for r in rows),
        }

    # --------------------------------------------------------- backtest store

    def list_strategies_raw(
        self, *, only_status: str | None = None
    ) -> list[dict]:
        """Strategies as raw dict rows — used by the backtest engine which
        needs the full payload JSON to read rules/indicators."""
        clauses: list[str] = []
        params: list[Any] = []
        if only_status:
            clauses.append("status=?")
            params.append(only_status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT id, name, trade_type, payload FROM strategies {where}",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def record_backtest(self, result: dict) -> None:
        """Persist one backtest result row + flip the strategy.status if
        the result lets us decide survived/rejected."""
        from uuid import uuid4
        rid = uuid4().hex
        status = result.get("status", "error")
        with self._lock:
            self._conn.execute(
                "INSERT INTO backtest_results("
                "id, strategy_id, symbol, granularity, trade_count, wins, losses, "
                "win_rate, total_pnl, profit_factor, status, details, tested_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rid,
                    result.get("strategy_id"),
                    result.get("symbol"),
                    result.get("granularity"),
                    result.get("trade_count"),
                    result.get("wins"),
                    result.get("losses"),
                    result.get("win_rate"),
                    result.get("total_pnl"),
                    result.get("profit_factor"),
                    status,
                    json.dumps(result, default=str),
                    _now_iso(),
                ),
            )
            if status in ("survived", "rejected") and result.get("strategy_id"):
                self._conn.execute(
                    "UPDATE strategies SET status=? WHERE id=?",
                    (status, result["strategy_id"]),
                )
            self._conn.commit()

    def list_backtests(self, *, limit: int = 200) -> list[dict]:
        rows = self._conn.execute(
            "SELECT br.*, s.name AS strategy_name "
            "FROM backtest_results br LEFT JOIN strategies s ON s.id = br.strategy_id "
            "ORDER BY br.tested_at DESC LIMIT ?",
            (max(1, min(limit, 1000)),),
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_backtest_per_strategy(self) -> dict[str, dict]:
        """Most-recent backtest result per strategy_id, keyed by id."""
        rows = self._conn.execute(
            "SELECT br.* FROM backtest_results br "
            "INNER JOIN (SELECT strategy_id, MAX(tested_at) AS t "
            "            FROM backtest_results GROUP BY strategy_id) m "
            "ON br.strategy_id = m.strategy_id AND br.tested_at = m.t"
        ).fetchall()
        return {r["strategy_id"]: dict(r) for r in rows}

    # ---------------------------------------------------------- decisions

    def record_decision(self, d: dict) -> str:
        from uuid import uuid4
        did = uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO decisions("
                "id,symbol,trade_type,direction,prediction,duration,duration_unit,"
                "confidence,rationale,contributing_strategies,market_context,"
                "is_trade,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    did,
                    d["symbol"],
                    d.get("trade_type"),
                    d.get("direction"),
                    d.get("prediction"),
                    d.get("duration"),
                    d.get("duration_unit"),
                    float(d["confidence"]),
                    d.get("rationale"),
                    json.dumps(d.get("contributing_strategies") or []),
                    json.dumps(d.get("market_context") or {}, default=str),
                    1 if d.get("is_trade") else 0,
                    _now_iso(),
                ),
            )
            self._conn.commit()
        return did

    def list_decisions(
        self, *, symbol: str | None = None, trade_only: bool = False, limit: int = 100
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol=?")
            params.append(symbol)
        if trade_only:
            clauses.append("is_trade=1")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(limit, 1000)))
        rows = self._conn.execute(
            f"SELECT * FROM decisions {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["contributing_strategies"] = json.loads(d.get("contributing_strategies") or "[]")
            d["market_context"] = json.loads(d.get("market_context") or "{}")
            d["is_trade"] = bool(d["is_trade"])
            out.append(d)
        return out

    # ------------------------------------------------------------------ comms

    def add_comms(self, msg: dict) -> str:
        from uuid import uuid4
        mid = uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO comms(id,from_app,to_app,type,subject,body,grade,"
                "data,acked,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    mid,
                    msg["from_app"],
                    msg.get("to_app", "all"),
                    msg["type"],
                    msg.get("subject"),
                    msg.get("body"),
                    msg.get("grade"),
                    json.dumps(msg.get("data") or {}, default=str),
                    0,
                    _now_iso(),
                ),
            )
            self._conn.commit()
        return mid

    def list_comms(self, *, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM comms ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 1000)),),
        ).fetchall()
        return [self._comms_row(r) for r in rows]

    def comms_inbox(self, for_app: str, *, unacked_only: bool = True) -> list[dict]:
        clauses = ["(to_app=? OR to_app='all')"]
        params: list[Any] = [for_app]
        if unacked_only:
            clauses.append("acked=0")
        rows = self._conn.execute(
            "SELECT * FROM comms WHERE " + " AND ".join(clauses)
            + " ORDER BY created_at ASC",
            params,
        ).fetchall()
        return [self._comms_row(r) for r in rows]

    def comms_ack(self, ids: list[str]) -> int:
        if not ids:
            return 0
        with self._lock:
            q = ",".join("?" * len(ids))
            cur = self._conn.execute(
                f"UPDATE comms SET acked=1 WHERE id IN ({q})", ids
            )
            self._conn.commit()
        return cur.rowcount

    def comms_scoreboard(self) -> dict:
        """Average grade each app GIVES and RECEIVES, plus message counts."""
        given = {
            r[0]: {"avg_grade": round(r[1], 2), "count": r[2]}
            for r in self._conn.execute(
                "SELECT from_app, AVG(grade), COUNT(*) FROM comms "
                "WHERE type='grade' AND grade IS NOT NULL GROUP BY from_app"
            ).fetchall()
        }
        received = {
            r[0]: {"avg_grade": round(r[1], 2), "count": r[2]}
            for r in self._conn.execute(
                "SELECT to_app, AVG(grade), COUNT(*) FROM comms "
                "WHERE type='grade' AND grade IS NOT NULL GROUP BY to_app"
            ).fetchall()
        }
        totals = {
            r[0]: r[1]
            for r in self._conn.execute(
                "SELECT from_app, COUNT(*) FROM comms GROUP BY from_app"
            ).fetchall()
        }
        return {
            "grades_given": given,
            "grades_received": received,
            "messages_sent": totals,
        }

    @staticmethod
    def _comms_row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["acked"] = bool(d["acked"])
        d["data"] = json.loads(d.get("data") or "{}")
        return d

    def list_recommendations(self, *, limit: int = 50) -> list[dict]:
        """Recommendations are comms of type 'recommendation' — the feed each
        system writes to advise the others on lifting productivity."""
        rows = self._conn.execute(
            "SELECT * FROM comms WHERE type='recommendation' "
            "ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        return [self._comms_row(r) for r in rows]

    # ----------------------------------------------------------- productivity

    def upsert_productivity(
        self, app: str, *, score: float, summary: str, metrics: dict
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO productivity(app,score,summary,metrics,updated_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(app) DO UPDATE SET "
                "score=excluded.score, summary=excluded.summary, "
                "metrics=excluded.metrics, updated_at=excluded.updated_at",
                (app, score, summary, json.dumps(metrics or {}, default=str), _now_iso()),
            )
            self._conn.commit()

    def list_productivity(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM productivity ORDER BY app"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["metrics"] = json.loads(d.get("metrics") or "{}")
            out.append(d)
        return out

    # ----------------------------------------------------------- maintenance

    def add_maintenance(self, issue: dict) -> str:
        from uuid import uuid4
        mid = uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO maintenance(id,from_app,severity,area,summary,"
                "detail,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    mid,
                    issue.get("from_app", "unknown"),
                    issue.get("severity", "info"),
                    issue.get("area"),
                    issue["summary"],
                    issue.get("detail"),
                    "open",
                    _now_iso(),
                ),
            )
            self._conn.commit()
        return mid

    def list_maintenance(self, *, status: str | None = "open", limit: int = 200) -> list[dict]:
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(limit, 1000)))
        rows = self._conn.execute(
            f"SELECT * FROM maintenance {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_maintenance(self, ids: list[str]) -> int:
        if not ids:
            return 0
        with self._lock:
            q = ",".join("?" * len(ids))
            cur = self._conn.execute(
                f"UPDATE maintenance SET status='resolved', resolved_at=? "
                f"WHERE id IN ({q})",
                [_now_iso(), *ids],
            )
            self._conn.commit()
        return cur.rowcount

    # ----------------------------------------------------------- self-study

    def add_study(
        self, app: str, kind: str, body: str, *,
        topic: str | None = None, source: str | None = None, dedupe: bool = True,
    ) -> str:
        """Log a self-study entry. When dedupe is on, an existing OPEN row with
        the same (app, kind, topic) is reused instead of inserting a duplicate
        — so the same enhancement idea doesn't pile up every 10 minutes."""
        from uuid import uuid4
        with self._lock:
            if dedupe and topic:
                row = self._conn.execute(
                    "SELECT id FROM study WHERE app=? AND kind=? AND topic=? "
                    "AND status='open' LIMIT 1",
                    (app, kind, topic),
                ).fetchone()
                if row:
                    return row["id"]
            sid = uuid4().hex
            self._conn.execute(
                "INSERT INTO study(id,app,kind,topic,body,source,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (sid, app, kind, topic, body, source, "open", _now_iso()),
            )
            self._conn.commit()
        return sid

    def list_study(
        self, *, app: str | None = None, kind: str | None = None,
        status: str | None = "open", limit: int = 200,
    ) -> list[dict]:
        clauses, params = [], []
        for col, val in (("app", app), ("kind", kind), ("status", status)):
            if val:
                clauses.append(f"{col}=?")
                params.append(val)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(limit, 1000)))
        rows = self._conn.execute(
            f"SELECT * FROM study {where} ORDER BY created_at DESC LIMIT ?", params
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_study(self, ids: list[str]) -> int:
        if not ids:
            return 0
        with self._lock:
            q = ",".join("?" * len(ids))
            cur = self._conn.execute(
                f"UPDATE study SET status='done' WHERE id IN ({q})", ids
            )
            self._conn.commit()
        return cur.rowcount

    # ----------------------------------------------------------- app state

    def get_state(self, k: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT v FROM app_state WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

    def set_state(self, k: str, v: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO app_state(k,v,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
                (k, v, _now_iso()),
            )
            self._conn.commit()

    # ----------------------------------------------------------- retention

    def prune_comms(self, *, keep_days: int = 14, keep_last: int = 1000) -> int:
        """Keep the comms bus lean for big-stuff runs: drop messages older than
        keep_days, but always retain the most recent keep_last regardless of age
        and never drop unacked ones (commands/requests still pending)."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM comms WHERE created_at < ? AND acked=1 AND id NOT IN "
                "(SELECT id FROM comms ORDER BY created_at DESC LIMIT ?)",
                (cutoff, keep_last),
            )
            self._conn.commit()
        return cur.rowcount

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _row_to_strategy(r: sqlite3.Row) -> StoredStrategy:
        return StoredStrategy(
            id=r["id"],
            trade_type=r["trade_type"],
            name=r["name"],
            confidence=r["confidence"],
            source_url=r["source_url"],
            source_platform=r["source_platform"],
            source_language=r["source_language"],
            status=r["status"],
            received_at=r["received_at"],
            payload=json.loads(r["payload"]),
        )

    @staticmethod
    def _row_to_insight(r: sqlite3.Row) -> StoredInsight:
        return StoredInsight(
            id=r["id"],
            trade_type=r["trade_type"],
            category=r["category"],
            summary=r["summary"],
            sentiment=r["sentiment"],
            source_url=r["source_url"],
            source_platform=r["source_platform"],
            source_language=r["source_language"],
            received_at=r["received_at"],
            payload=json.loads(r["payload"]),
        )


_store: AnalyserStore | None = None


def get_store() -> AnalyserStore:
    global _store
    if _store is None:
        _store = AnalyserStore()
    return _store
