"""SQLite 저장소: 인터페이스 정의, 메시지 로그, 실행 상태."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from .models import InterfaceDef, MessageLog, MessageStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interfaces (
    id TEXT PRIMARY KEY,
    definition TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    interface_id TEXT NOT NULL,
    status TEXT NOT NULL,
    trigger TEXT NOT NULL,
    message_key TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    payload TEXT,
    mapped TEXT,
    response TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_if_status ON messages(interface_id, status);
CREATE INDEX IF NOT EXISTS ix_messages_key ON messages(interface_id, message_key);
CREATE TABLE IF NOT EXISTS interface_state (
    interface_id TEXT PRIMARY KEY,
    last_run_at TEXT,
    last_success_at TEXT,
    last_error TEXT
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def _dump(v: Any) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False, default=str)


def _load(v: str | None) -> Any:
    return None if v is None else json.loads(v)


class Store:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def _exec(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, args)
            rows = cur.fetchall()
            self._conn.commit()
            return rows

    # ---- 인터페이스 ----
    def upsert_interface(self, d: InterfaceDef) -> None:
        self._exec(
            "INSERT INTO interfaces(id, definition, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET definition=excluded.definition, updated_at=excluded.updated_at",
            (d.id, d.model_dump_json(), now().isoformat()),
        )

    def get_interface(self, interface_id: str) -> InterfaceDef | None:
        rows = self._exec("SELECT definition FROM interfaces WHERE id=?", (interface_id,))
        return InterfaceDef.model_validate_json(rows[0]["definition"]) if rows else None

    def list_interfaces(self) -> list[InterfaceDef]:
        rows = self._exec("SELECT definition FROM interfaces ORDER BY id")
        return [InterfaceDef.model_validate_json(r["definition"]) for r in rows]

    def delete_interface(self, interface_id: str) -> bool:
        exists = self.get_interface(interface_id) is not None
        self._exec("DELETE FROM interfaces WHERE id=?", (interface_id,))
        self._exec("DELETE FROM interface_state WHERE interface_id=?", (interface_id,))
        return exists

    # ---- 실행 상태 ----
    def get_state(self, interface_id: str) -> dict[str, Any]:
        rows = self._exec("SELECT * FROM interface_state WHERE interface_id=?", (interface_id,))
        if not rows:
            return {"last_run_at": None, "last_success_at": None, "last_error": None}
        r = rows[0]
        return {"last_run_at": r["last_run_at"], "last_success_at": r["last_success_at"], "last_error": r["last_error"]}

    def record_run(self, interface_id: str, *, started_at: datetime, ok: bool, error: str | None = None) -> None:
        self._exec(
            "INSERT INTO interface_state(interface_id, last_run_at, last_success_at, last_error) VALUES(?,?,?,?) "
            "ON CONFLICT(interface_id) DO UPDATE SET last_run_at=excluded.last_run_at, "
            "last_success_at=COALESCE(excluded.last_success_at, interface_state.last_success_at), "
            "last_error=excluded.last_error",
            (interface_id, started_at.isoformat(), started_at.isoformat() if ok else None, error),
        )

    # ---- 메시지 ----
    def create_message(self, interface_id: str, trigger: str, payload: Any) -> MessageLog:
        ts = now()
        msg = MessageLog(
            id=uuid.uuid4().hex, interface_id=interface_id, status=MessageStatus.PROCESSING,
            trigger=trigger, payload=payload, created_at=ts, updated_at=ts,
        )
        self._exec(
            "INSERT INTO messages(id, interface_id, status, trigger, payload, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (msg.id, interface_id, msg.status.value, trigger, _dump(payload), ts.isoformat(), ts.isoformat()),
        )
        return msg

    def save_message(self, msg: MessageLog) -> None:
        msg.updated_at = now()
        self._exec(
            "UPDATE messages SET status=?, message_key=?, attempts=?, mapped=?, response=?, error=?, updated_at=? "
            "WHERE id=?",
            (msg.status.value, msg.message_key, msg.attempts, _dump(msg.mapped), _dump(msg.response),
             msg.error, msg.updated_at.isoformat(), msg.id),
        )

    def get_message(self, message_id: str) -> MessageLog | None:
        rows = self._exec("SELECT * FROM messages WHERE id=?", (message_id,))
        return self._row_to_msg(rows[0]) if rows else None

    def list_messages(
        self, interface_id: str | None = None, status: MessageStatus | None = None,
        key: str | None = None, limit: int = 100, offset: int = 0,
    ) -> list[MessageLog]:
        sql, args = "SELECT * FROM messages WHERE 1=1", []
        if interface_id:
            sql += " AND interface_id=?"
            args.append(interface_id)
        if status:
            sql += " AND status=?"
            args.append(status.value)
        if key:
            sql += " AND message_key=?"
            args.append(key)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        return [self._row_to_msg(r) for r in self._exec(sql, tuple(args))]

    def has_success(self, interface_id: str, key: str, exclude_id: str | None = None) -> bool:
        rows = self._exec(
            "SELECT 1 FROM messages WHERE interface_id=? AND message_key=? AND status=? AND id<>? LIMIT 1",
            (interface_id, key, MessageStatus.SUCCESS.value, exclude_id or ""),
        )
        return bool(rows)

    def stats(self) -> dict[str, dict[str, int]]:
        rows = self._exec("SELECT interface_id, status, COUNT(*) AS n FROM messages GROUP BY interface_id, status")
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["interface_id"], {})[r["status"]] = r["n"]
        return out

    @staticmethod
    def _row_to_msg(r: sqlite3.Row) -> MessageLog:
        return MessageLog(
            id=r["id"], interface_id=r["interface_id"], status=MessageStatus(r["status"]), trigger=r["trigger"],
            message_key=r["message_key"], attempts=r["attempts"], payload=_load(r["payload"]),
            mapped=_load(r["mapped"]), response=_load(r["response"]), error=r["error"],
            created_at=datetime.fromisoformat(r["created_at"]), updated_at=datetime.fromisoformat(r["updated_at"]),
        )
