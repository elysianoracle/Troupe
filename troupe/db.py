"""SQLite storage. One file, WAL mode, FTS5 for memory search. No external DB server needed."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',          -- active | paused
    permissions TEXT NOT NULL DEFAULT '{}',         -- json, see guardrails.DEFAULT_PERMISSIONS
    llm_budget_usd_week REAL NOT NULL DEFAULT 10,
    spend_budget_usd_week REAL NOT NULL DEFAULT 0,
    contacts TEXT NOT NULL DEFAULT '[]',            -- json list of allowed email/phone recipients
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'dm',                -- dm | group
    lead_agent_id TEXT,
    plan TEXT NOT NULL DEFAULT '',
    teach TEXT NOT NULL DEFAULT '',                 -- json teach session, '' when off
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS room_members (
    room_id TEXT NOT NULL, agent_id TEXT NOT NULL,
    PRIMARY KEY (room_id, agent_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id TEXT NOT NULL,
    sender_type TEXT NOT NULL,                      -- user | agent | system
    sender_id TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_room ON messages(room_id, id);
CREATE TABLE IF NOT EXISTS threads (                -- append-only LLM history per (agent, room)
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    history TEXT NOT NULL DEFAULT '[]',
    last_seen_message_id INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_threads_agent_room ON threads(agent_id, room_id, active);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    source TEXT NOT NULL,                           -- user | schedule | email | sms | webhook | handoff | teach
    input TEXT NOT NULL,
    goal_id TEXT NOT NULL,                          -- tasks spawned by handoffs share a goal id
    tainted INTEGER NOT NULL DEFAULT 0,             -- saw untrusted content -> external actions need approval
    status TEXT NOT NULL DEFAULT 'queued',          -- queued | running | waiting_approval | done | failed | cancelled
    state TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT '',
    steps INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    tool_use_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',         -- pending | approved | denied
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    decided_at REAL
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, tags, content='memories', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, tags) VALUES ('delete', old.id, old.content, old.tags);
END;
CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    params TEXT NOT NULL DEFAULT '[]',
    playbook TEXT NOT NULL,
    source_trace TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(agent_id, name)
);
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    cron TEXT NOT NULL,
    prompt TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run REAL NOT NULL,
    last_run REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,                             -- llm | tool | approval | budget | trigger | system
    name TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    cost_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audit_task ON audit(task_id);
CREATE INDEX IF NOT EXISTS idx_audit_agent_ts ON audit(agent_id, ts);
CREATE TABLE IF NOT EXISTS ledger (                 -- money the agents were allowed to spend
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    merchant TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,                           -- approved | declined
    url TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS secrets (
    name TEXT PRIMARY KEY,
    ciphertext BLOB NOT NULL,
    domains TEXT NOT NULL DEFAULT '[]',             -- where type_secret may use it
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


class DB:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring older databases up to date: add new columns, drop retired ones (SQLite >= 3.35)."""
        wanted = {"ledger": [("url", "TEXT NOT NULL DEFAULT ''")]}
        retired = {"ledger": ["domain", "card_token", "last_four", "limit_cents", "charged_usd"]}
        for table, cols in wanted.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        for table, cols in retired.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name in cols:
                if name in have:
                    self.conn.execute(f"ALTER TABLE {table} DROP COLUMN {name}")

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def insert(self, table: str, row: dict[str, Any]) -> int:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self._lock:
            cur = self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))
            return cur.lastrowid

    def update(self, table: str, key: str, key_value: Any, fields: dict[str, Any]) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self.conn.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?", [*fields.values(), key_value])

    # --- small conveniences -------------------------------------------------
    def kv_get(self, key: str, default: str = "") -> str:
        row = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, value))

    def audit(self, kind: str, name: str = "", detail: Any = "", agent_id: str = "", task_id: str = "",
              cost_usd: float = 0.0) -> None:
        if not isinstance(detail, str):
            detail = json.dumps(detail, default=str)
        self.insert("audit", {"ts": now(), "agent_id": agent_id, "task_id": task_id, "kind": kind,
                              "name": name, "detail": detail[:8000], "cost_usd": cost_usd})


def loads(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return default
