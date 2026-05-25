"""J81 Bot SQLite store.

Three tables:
  * accounts  — one row per Deriv account a user has authorised. Tokens
                are stored Fernet-encrypted; the plaintext never touches disk.
  * trades    — every trade attempt (DRY_RUN or live), with full payload
                and outcome for audit + earnings calculation.
  * audit     — administrative actions (OAuth grants, enable/disable, key
                rotations) so there's a permanent trail of who did what.

Security stance: any code path that reads a token MUST go through
`decrypted_token_for()`. Logging or returning tokens via the API is a bug.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from app.config import data_path, get_settings


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fernet() -> Fernet:
    key = get_settings().bot_encryption_key
    if not key:
        raise RuntimeError(
            "BOT_ENCRYPTION_KEY is not set — generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"` and put it in .env"
        )
    return Fernet(key.encode())


class BotStore:
    def __init__(self, db_path: str | None = None) -> None:
        path = Path(db_path) if db_path else data_path("data/bot.db")
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
                CREATE TABLE IF NOT EXISTS accounts (
                    id                      TEXT PRIMARY KEY,
                    deriv_account_id        TEXT NOT NULL UNIQUE,  -- e.g. CR123456
                    currency                TEXT,
                    encrypted_token         BLOB NOT NULL,          -- Fernet ciphertext
                    label                   TEXT,                   -- user-facing label
                    enabled                 INTEGER NOT NULL DEFAULT 0,  -- opt-in to autotrade
                    max_stake_per_trade     REAL,                   -- per-user override
                    max_trades_per_day      INTEGER,
                    min_confidence          REAL,
                    allowed_trade_types     TEXT,                   -- JSON list, NULL = any
                    allowed_symbols         TEXT,                   -- JSON list, NULL = any
                    created_at              TEXT NOT NULL,
                    updated_at              TEXT NOT NULL,
                    last_trade_at           TEXT
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id              TEXT PRIMARY KEY,
                    account_id      TEXT NOT NULL,                  -- our internal id
                    deriv_account   TEXT NOT NULL,                  -- e.g. CR123456
                    symbol          TEXT,
                    trade_type      TEXT,
                    direction       TEXT,
                    prediction      INTEGER,
                    duration        INTEGER,
                    duration_unit   TEXT,
                    stake           REAL,
                    confidence      REAL,
                    decision_id     TEXT,                           -- the Analyser decision id
                    decision_payload TEXT,                          -- JSON snapshot
                    is_dry_run      INTEGER NOT NULL,               -- 1 if not live
                    deriv_contract_id TEXT,                          -- live trades only
                    outcome         TEXT,                            -- pending|won|lost|error
                    profit          REAL,
                    payout          REAL,                            -- real/quoted contract payout
                    buy_price       REAL,                            -- actual price paid (live)
                    markup_earned   REAL,                            -- your slice (real once settled)
                    error           TEXT,
                    created_at      TEXT NOT NULL,
                    settled_at      TEXT,
                    FOREIGN KEY (account_id) REFERENCES accounts(id)
                );
                CREATE INDEX IF NOT EXISTS ix_trades_account ON trades(account_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS ix_trades_outcome ON trades(outcome);

                CREATE TABLE IF NOT EXISTS audit (
                    id          TEXT PRIMARY KEY,
                    actor       TEXT,
                    action      TEXT NOT NULL,
                    detail      TEXT,
                    created_at  TEXT NOT NULL
                );

                -- Strategies the Analyser PROVED (70% win ×100 ×5 + net P/L>0)
                -- and pushed here. This is the durable winners' store: the
                -- analyser/researcher get auto-cleared each cycle, these stay.
                CREATE TABLE IF NOT EXISTS proven_strategies (
                    id            TEXT PRIMARY KEY,   -- natural key: symbol|label
                    trade_type    TEXT,
                    symbol        TEXT,
                    market        TEXT,
                    label         TEXT,
                    contract_type TEXT,
                    barrier       TEXT,
                    duration      INTEGER,
                    win_rate      REAL,
                    net_pnl       REAL,
                    samples       INTEGER,
                    trades        INTEGER,
                    payload       TEXT,               -- full JSON snapshot
                    created_at    TEXT NOT NULL,
                    updated_at    TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_proven_tt ON proven_strategies(trade_type);
                """
            )
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created. SQLite has
        no 'ADD COLUMN IF NOT EXISTS', so we try each and ignore the error
        if it's already there. Called under self._lock by _init_schema."""
        for table, col, decl in (
            ("trades", "payout", "REAL"),
            ("trades", "buy_price", "REAL"),
            # Per-account session goals (the simple "stop when…" controls).
            ("accounts", "take_profit", "REAL"),       # stop after +$X profit today
            ("accounts", "daily_loss_limit", "REAL"),  # stop after -$Y loss today
            ("accounts", "mpro_enabled", "INTEGER"),   # M Pro auto-cycle on/off
            ("accounts", "mpro_config", "TEXT"),       # JSON: mode, reverse, stake, step…
            ("accounts", "platform", "TEXT"),          # 'legacy' (authorize+buy) | 'new' (OTP-WS)
            ("accounts", "session_id", "TEXT"),        # browser session that connected this account
            ("accounts", "rf_config", "TEXT"),         # JSON: server-side Rise/Fall gated auto {enabled,min_conf,stake,duration}
            ("accounts", "proven_auto", "INTEGER"),    # 1 = auto-trade the analyser's PROVEN strategies
            ("accounts", "refresh_token", "BLOB"),     # Fernet-encrypted OAuth refresh token (new platform)
            ("accounts", "token_expires_at", "TEXT"),  # ISO time the access token expires (for proactive refresh)
        ):
            try:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # column already exists

    # --------------------------------------------------------------- accounts

    def _expiry_iso(self, expires_in: int | None) -> str | None:
        if not expires_in:
            return None
        from datetime import timedelta
        return (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()

    def upsert_account(
        self,
        *,
        deriv_account_id: str,
        token: str,
        currency: str | None = None,
        label: str | None = None,
        platform: str = "legacy",
        session_id: str | None = None,
        refresh_token: str | None = None,
        expires_in: int | None = None,
    ) -> str:
        """Encrypt+store. If the deriv_account_id is already present, the
        token is rotated and the row's updated_at bumped. Returns our id.
        `platform` is 'legacy' (PAT/legacy-OAuth → authorize+buy) or 'new'
        (OAuth2 PKCE → OTP-WS) and decides how the executor places trades.
        `refresh_token`/`expires_in` (new platform) enable auto-renewal so a
        session survives past the ~1h access-token expiry."""
        f = _fernet()
        encrypted = f.encrypt(token.encode())
        enc_refresh = f.encrypt(refresh_token.encode()) if refresh_token else None
        expires_at = self._expiry_iso(expires_in)
        now = _now_iso()
        settings = get_settings()
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM accounts WHERE deriv_account_id=?",
                (deriv_account_id,),
            ).fetchone()
            if row:
                # COALESCE keeps an existing refresh token if this sign-in didn't
                # return a new one (e.g. legacy re-link), so we never lose it.
                self._conn.execute(
                    "UPDATE accounts SET encrypted_token=?, currency=?, "
                    "label=COALESCE(?, label), platform=?, "
                    "refresh_token=COALESCE(?, refresh_token), "
                    "token_expires_at=?, "
                    "session_id=COALESCE(?, session_id), updated_at=? WHERE id=?",
                    (encrypted, currency, label, platform, enc_refresh, expires_at,
                     session_id, now, row["id"]),
                )
                acct_id = row["id"]
            else:
                acct_id = uuid4().hex
                self._conn.execute(
                    "INSERT INTO accounts("
                    "id, deriv_account_id, currency, encrypted_token, label, "
                    "enabled, max_stake_per_trade, max_trades_per_day, "
                    "min_confidence, platform, session_id, refresh_token, "
                    "token_expires_at, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,0,?,?,?,?,?,?,?,?,?)",
                    (
                        acct_id,
                        deriv_account_id,
                        currency,
                        encrypted,
                        label or deriv_account_id,
                        settings.default_max_stake_per_trade,
                        settings.default_max_trades_per_day,
                        settings.default_min_confidence,
                        platform,
                        session_id,
                        enc_refresh,
                        expires_at,
                        now,
                        now,
                    ),
                )
            self._record_audit(
                "account_token_stored",
                detail=f"deriv_account_id={deriv_account_id}",
            )
            self._conn.commit()
        return acct_id

    def account_owned_by(self, account_id: str, session_id: str | None) -> bool:
        """True if this account is usable by the given browser session: it
        matches the session, OR it's unclaimed (NULL session — e.g. connected
        before per-session binding, or the cookie drifted). Unclaimed accounts
        are claimed to the caller so future calls are clean."""
        row = self._conn.execute(
            "SELECT session_id FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        if row is None:
            return False
        owner = row["session_id"]
        if owner is None and session_id:   # claim it for this session
            with self._lock:
                self._conn.execute("UPDATE accounts SET session_id=? WHERE id=?",
                                   (session_id, account_id))
                self._conn.commit()
            return True
        return owner is None or owner == session_id

    def list_accounts_public(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Public view — token is NEVER returned, only `has_token: True`.
        When `session_id` is given, only that browser session's accounts are
        returned (client-facing privacy). Called with no arg internally (e.g.
        the autotrade loop) to see every account."""
        cols = (
            "SELECT id, deriv_account_id, currency, label, enabled, "
            "max_stake_per_trade, max_trades_per_day, min_confidence, "
            "allowed_trade_types, allowed_symbols, take_profit, daily_loss_limit, "
            "mpro_enabled, mpro_config, rf_config, proven_auto, platform, "
            "token_expires_at, created_at, updated_at, last_trade_at FROM accounts "
        )
        if session_id is not None:
            # Claim any unclaimed (NULL) accounts to this session so a returning
            # owner whose cookie drifted still sees + can use their accounts.
            with self._lock:
                self._conn.execute(
                    "UPDATE accounts SET session_id=? WHERE session_id IS NULL",
                    (session_id,))
                self._conn.commit()
            rows = self._conn.execute(
                cols + "WHERE session_id=? ORDER BY created_at DESC", (session_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(cols + "ORDER BY created_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["enabled"] = bool(d["enabled"])
            d["has_token"] = True
            d["allowed_trade_types"] = json.loads(d["allowed_trade_types"] or "null")
            d["allowed_symbols"] = json.loads(d["allowed_symbols"] or "null")
            # Demo prefixes: VRT/VRTC (legacy) and DOT (new Options); real
            # new accounts are ROT.
            d["is_demo"] = (d["deriv_account_id"] or "").upper().startswith(("VRT", "DOT"))
            d["kind"] = "demo" if d["is_demo"] else "REAL MONEY"
            d["profit_today"] = self.profit_today(d["id"])
            d["mpro_enabled"] = bool(d.get("mpro_enabled"))
            d["mpro_config"] = json.loads(d.get("mpro_config") or "null")
            d["rf_config"] = json.loads(d.get("rf_config") or "null")
            d["proven_auto"] = bool(d.get("proven_auto"))
            d["platform"] = d.get("platform") or "legacy"
            out.append(d)
        return out

    def get_internal(self, account_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        return dict(row) if row else None

    def decrypted_token_for(self, account_id: str) -> str | None:
        """The only path that returns plaintext. Internal use only."""
        row = self._conn.execute(
            "SELECT encrypted_token FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        if not row:
            return None
        try:
            return _fernet().decrypt(row["encrypted_token"]).decode()
        except InvalidToken:
            return None  # key changed — token unreadable; surface higher up

    def decrypted_refresh_for(self, account_id: str) -> str | None:
        """The account's OAuth refresh token (plaintext), or None. Internal use."""
        row = self._conn.execute(
            "SELECT refresh_token FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        if not row or row["refresh_token"] is None:
            return None
        try:
            return _fernet().decrypt(row["refresh_token"]).decode()
        except InvalidToken:
            return None

    def update_token(self, account_id: str, *, access: str,
                     refresh: str | None = None, expires_in: int | None = None) -> None:
        """Rotate an account's access token (and refresh token, if Deriv rotated
        it) after an OAuth refresh. Keeps the old refresh token if none returned."""
        f = _fernet()
        enc_access = f.encrypt(access.encode())
        enc_refresh = f.encrypt(refresh.encode()) if refresh else None
        with self._lock:
            self._conn.execute(
                "UPDATE accounts SET encrypted_token=?, "
                "refresh_token=COALESCE(?, refresh_token), token_expires_at=?, "
                "updated_at=? WHERE id=?",
                (enc_access, enc_refresh, self._expiry_iso(expires_in),
                 _now_iso(), account_id),
            )
            self._conn.commit()

    def update_account_settings(
        self,
        account_id: str,
        *,
        enabled: bool | None = None,
        max_stake_per_trade: float | None = None,
        max_trades_per_day: int | None = None,
        min_confidence: float | None = None,
        allowed_trade_types: list[str] | None = None,
        allowed_symbols: list[str] | None = None,
        label: str | None = None,
        take_profit: float | None = None,
        daily_loss_limit: float | None = None,
        mpro_enabled: bool | None = None,
        mpro_config: dict | None = None,
        rf_config: dict | None = None,
        proven_auto: bool | None = None,
    ) -> bool:
        sets: list[str] = []
        params: list[Any] = []

        def _add(col: str, val: Any) -> None:
            if val is not None:
                sets.append(f"{col}=?")
                params.append(val)

        _add("enabled", 1 if enabled else 0 if enabled is not None else None)
        _add("max_stake_per_trade", max_stake_per_trade)
        _add("max_trades_per_day", max_trades_per_day)
        _add("min_confidence", min_confidence)
        _add("label", label)
        _add("take_profit", take_profit)
        _add("daily_loss_limit", daily_loss_limit)
        _add("mpro_enabled", 1 if mpro_enabled else 0 if mpro_enabled is not None else None)
        _add("proven_auto", 1 if proven_auto else 0 if proven_auto is not None else None)
        if mpro_config is not None:
            sets.append("mpro_config=?")
            params.append(json.dumps(mpro_config))
        if rf_config is not None:
            sets.append("rf_config=?")
            params.append(json.dumps(rf_config))
        if allowed_trade_types is not None:
            sets.append("allowed_trade_types=?")
            params.append(json.dumps(allowed_trade_types))
        if allowed_symbols is not None:
            sets.append("allowed_symbols=?")
            params.append(json.dumps(allowed_symbols))
        if not sets:
            return False
        sets.append("updated_at=?")
        params.append(_now_iso())
        params.append(account_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE accounts SET {','.join(sets)} WHERE id=?", params
            )
            self._record_audit("account_settings_updated", detail=account_id)
            self._conn.commit()
        return cur.rowcount > 0

    def delete_account(self, account_id: str) -> bool:
        with self._lock:
            # Drop dependent trades first — the trades.account_id FK (with
            # foreign_keys=ON) would otherwise block the delete once the
            # account has any trade history.
            self._conn.execute(
                "DELETE FROM trades WHERE account_id=?", (account_id,)
            )
            cur = self._conn.execute(
                "DELETE FROM accounts WHERE id=?", (account_id,)
            )
            self._record_audit("account_deleted", detail=account_id)
            self._conn.commit()
        return cur.rowcount > 0

    # ----------------------------------------------------------------- trades

    def record_trade(self, t: dict[str, Any]) -> str:
        trade_id = uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO trades("
                "id, account_id, deriv_account, symbol, trade_type, direction, "
                "prediction, duration, duration_unit, stake, confidence, "
                "decision_id, decision_payload, is_dry_run, deriv_contract_id, "
                "outcome, payout, buy_price, markup_earned, error, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    trade_id,
                    t["account_id"],
                    t["deriv_account"],
                    t.get("symbol"),
                    t.get("trade_type"),
                    t.get("direction"),
                    t.get("prediction"),
                    t.get("duration"),
                    t.get("duration_unit"),
                    t.get("stake"),
                    t.get("confidence"),
                    t.get("decision_id"),
                    json.dumps(t.get("decision_payload") or {}, default=str),
                    1 if t.get("is_dry_run") else 0,
                    t.get("deriv_contract_id"),
                    t.get("outcome", "pending"),
                    t.get("payout"),
                    t.get("buy_price"),
                    t.get("markup_earned"),
                    t.get("error"),
                    _now_iso(),
                ),
            )
            self._conn.execute(
                "UPDATE accounts SET last_trade_at=? WHERE id=?",
                (_now_iso(), t["account_id"]),
            )
            self._conn.commit()
        return trade_id

    def list_trades(
        self, *, account_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if account_id:
            clauses.append("account_id=?")
            params.append(account_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(limit, 1000)))
        rows = self._conn.execute(
            f"SELECT * FROM trades {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["is_dry_run"] = bool(d["is_dry_run"])
            d["decision_payload"] = json.loads(d.get("decision_payload") or "{}")
            out.append(d)
        return out

    # ----------------------------------------------------- proven strategies
    def save_proven_strategy(self, s: dict[str, Any]) -> str:
        """Upsert one proven strategy from the Analyser cycle. Natural key is
        symbol|label so re-proving the same one refreshes its stats, not dupes."""
        sid = f"{s.get('symbol','')}|{s.get('label','')}"
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                """INSERT INTO proven_strategies
                   (id, trade_type, symbol, market, label, contract_type, barrier,
                    duration, win_rate, net_pnl, samples, trades, payload,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     win_rate=excluded.win_rate, net_pnl=excluded.net_pnl,
                     samples=excluded.samples, trades=excluded.trades,
                     payload=excluded.payload, updated_at=excluded.updated_at""",
                (sid, s.get("trade_type"), s.get("symbol"), s.get("market"),
                 s.get("label"), s.get("contract_type"), s.get("barrier"),
                 s.get("duration"), s.get("win_rate"), s.get("net_pnl"),
                 s.get("samples"), s.get("trades"), json.dumps(s), now, now),
            )
            self._conn.commit()
        return sid

    def list_proven_strategies(self, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM proven_strategies ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(limit, 1000)),),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_pending_live_trades(self) -> list[dict[str, Any]]:
        """Live trades whose contract hasn't settled yet — the settler
        polls Deriv for these and updates outcome/profit."""
        rows = self._conn.execute(
            "SELECT id, account_id, deriv_account, deriv_contract_id, stake "
            "FROM trades WHERE is_dry_run=0 AND outcome='pending' "
            "AND deriv_contract_id IS NOT NULL"
        ).fetchall()
        return [dict(r) for r in rows]

    def settle_trade(
        self,
        trade_id: str,
        *,
        outcome: str,
        profit: float | None,
        markup_earned: float | None = None,
    ) -> None:
        """Mark a live trade settled. If Deriv reported the real
        app_markup_amount, overwrite the buy-time estimate with it so
        earnings totals reflect what was actually credited."""
        with self._lock:
            if markup_earned is not None:
                self._conn.execute(
                    "UPDATE trades SET outcome=?, profit=?, markup_earned=?, "
                    "settled_at=? WHERE id=?",
                    (outcome, profit, markup_earned, _now_iso(), trade_id),
                )
            else:
                self._conn.execute(
                    "UPDATE trades SET outcome=?, profit=?, settled_at=? WHERE id=?",
                    (outcome, profit, _now_iso(), trade_id),
                )
            self._conn.commit()

    def trades_today(self, account_id: str) -> int:
        from datetime import date
        today = date.today().isoformat()
        return self._conn.execute(
            "SELECT COUNT(*) FROM trades WHERE account_id=? "
            "AND substr(created_at,1,10)=?",
            (account_id, today),
        ).fetchone()[0]

    def profit_today(self, account_id: str) -> float:
        """Realized profit (settled live trades) for this account, today. Used
        by the take-profit / loss-limit goals. Dry-run trades never settle, so
        they contribute 0 — the goals act on real/demo-live trades."""
        from datetime import date
        today = date.today().isoformat()
        row = self._conn.execute(
            "SELECT COALESCE(SUM(profit),0) FROM trades WHERE account_id=? "
            "AND substr(created_at,1,10)=? AND profit IS NOT NULL",
            (account_id, today),
        ).fetchone()
        return float(row[0] or 0.0)

    def stats(self) -> dict[str, Any]:
        total_trades = self._conn.execute(
            "SELECT COUNT(*) FROM trades"
        ).fetchone()[0]
        accounts_total = self._conn.execute(
            "SELECT COUNT(*) FROM accounts"
        ).fetchone()[0]
        accounts_enabled = self._conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE enabled=1"
        ).fetchone()[0]
        dry_run_count = self._conn.execute(
            "SELECT COUNT(*) FROM trades WHERE is_dry_run=1"
        ).fetchone()[0]
        live_count = self._conn.execute(
            "SELECT COUNT(*) FROM trades WHERE is_dry_run=0"
        ).fetchone()[0]
        markup = self._conn.execute(
            "SELECT COALESCE(SUM(markup_earned),0) FROM trades"
        ).fetchone()[0]
        return {
            "accounts_total": accounts_total,
            "accounts_enabled": accounts_enabled,
            "trades_total": total_trades,
            "trades_dry_run": dry_run_count,
            "trades_live": live_count,
            "markup_earned_total": round(markup or 0.0, 4),
        }

    # ----------------------------------------------------------------- audit

    def _record_audit(self, action: str, *, actor: str | None = None,
                      detail: str | None = None) -> None:
        # Called under self._lock by callers, so don't re-lock.
        self._conn.execute(
            "INSERT INTO audit(id, actor, action, detail, created_at) "
            "VALUES (?,?,?,?,?)",
            (uuid4().hex, actor or "system", action, detail, _now_iso()),
        )


_store: BotStore | None = None


def get_store() -> BotStore:
    global _store
    if _store is None:
        _store = BotStore()
    return _store
