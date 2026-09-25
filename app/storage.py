"""SQLite 持久化层。

一个进程持有一条带写锁的连接。所有榜单/成绩快照都带显式证据水位，
保证“一个事务只有一个证据水位胜出”。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any

_LOCK = threading.RLock()
_conn: sqlite3.Connection | None = None

DB_PATH = os.environ.get("EVIDENCE_DB", os.path.join(os.getcwd(), "evidence.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',  -- open / locked / published
    config_json TEXT NOT NULL DEFAULT '{}',
    appeal_deadline TEXT,
    downtime_start TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS flights (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id),
    pilot_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- 证据数据包：packet_id 相同且内容哈希相同 => 归并；相同 id 不同哈希 => 隔离。
-- state: merged(已归并) / quarantined(隔离) / held(落后于锁定/发布水位)
CREATE TABLE IF NOT EXISTS packets (
    id INTEGER PRIMARY KEY,
    evidence_seq INTEGER,          -- 归并时刻分配的单调证据水位序号
    packet_id TEXT NOT NULL,       -- 上传方提供的数据包标识
    flight_id INTEGER NOT NULL REFERENCES flights(id),
    uploader_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    summary TEXT,
    state TEXT NOT NULL,
    duplicate_of INTEGER,         -- 归并到的首包 id
    received_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_packets_flight ON packets(flight_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_packet_identity ON packets(flight_id, packet_id, content_hash);

CREATE TABLE IF NOT EXISTS points (
    id INTEGER PRIMARY KEY,
    flight_id INTEGER NOT NULL REFERENCES flights(id),
    packet_id INTEGER NOT NULL REFERENCES packets(id),
    device_session TEXT NOT NULL,  -- 设备会话（启动标识）；重启即新会话
    device_seq INTEGER,           -- 设备自身序号，可能回退，绝不用于全局排序
    device_time TEXT NOT NULL,    -- 设备时间（完整 ISO 时间，可跨午夜）
    received_at TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    altitude REAL,
    point_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_points_flight ON points(flight_id);

-- 成绩版本：candidate(裁判候选即冻结) / confirmed(仲裁确认) / alternate(重开替代) / superseded
CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY,
    flight_id INTEGER NOT NULL REFERENCES flights(id),
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    judge_id TEXT NOT NULL,
    evidence_watermark INTEGER NOT NULL,
    total_seconds REAL,
    leg_json TEXT NOT NULL,       -- 各航段采纳/排除与理由快照
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(flight_id, version)
);

CREATE TABLE IF NOT EXISTS arbitrations (
    id INTEGER PRIMARY KEY,
    score_id INTEGER NOT NULL REFERENCES scores(id),
    arbiter_id TEXT NOT NULL,
    seen_watermark INTEGER NOT NULL,  -- 仲裁人只确认自己看到的证据版本
    decision TEXT NOT NULL,           -- confirmed / rejected
    comment TEXT,
    decided_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- 仲裁人查看证据时登记自己看到的水位；确认时只能确认该版本。
CREATE TABLE IF NOT EXISTS arbiter_views (
    id INTEGER PRIMARY KEY,
    score_id INTEGER NOT NULL REFERENCES scores(id),
    arbiter_id TEXT NOT NULL,
    seen_watermark INTEGER NOT NULL,
    seen_fingerprint TEXT NOT NULL,
    viewed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(score_id, arbiter_id)
);

-- 榜单快照：draft / locked / published；alternative=重开后的替代推演。
-- 每个快照绑定单一证据水位。
CREATE TABLE IF NOT EXISTS rankings (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id),
    watermark INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS ranking_rows (
    id INTEGER PRIMARY KEY,
    ranking_id INTEGER NOT NULL REFERENCES rankings(id),
    flight_id INTEGER NOT NULL,
    score_id INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    total_seconds REAL,
    prev_rank INTEGER,            -- 相对上一发布榜单的名次
    rank_change INTEGER
);

-- 发布后补证的影响评估：pending / approved / rejected
CREATE TABLE IF NOT EXISTS assessments (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id),
    flight_id INTEGER NOT NULL REFERENCES flights(id),
    packet_id INTEGER NOT NULL REFERENCES packets(id),
    state TEXT NOT NULL DEFAULT 'pending',
    impact_json TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id),
    flight_id INTEGER,
    trigger TEXT NOT NULL,        -- pre_publish_evidence / reopen / rebuild
    status TEXT NOT NULL,         -- queued / running / done / failed
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id),
    flight_id INTEGER,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path: str | None = None) -> None:
    global _conn
    with _LOCK:
        if _conn is not None:
            _conn.close()
        _conn = connect(path)
        _conn.executescript(SCHEMA)
        _conn.commit()


def db() -> sqlite3.Connection:
    if _conn is None:
        init_db()
    assert _conn is not None
    return _conn


def lock() -> threading.RLock:
    return _LOCK


# --- 小工具 -----------------------------------------------------------------

def now() -> str:
    return db().execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now') AS t"
    ).fetchone()["t"]


def insert(table: str, **fields: Any) -> int:
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    cur = db().execute(
        f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(fields.values())
    )
    return int(cur.lastrowid)


def fetchone(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return db().execute(sql, params).fetchone()


def fetchall(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return list(db().execute(sql, params).fetchall())


def commit() -> None:
    db().commit()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(text: str | None) -> Any:
    return json.loads(text) if text else None
