"""SQLite 持久化:schema 与单写事务。

所有改变计算输入或冻结结果的操作(证据接收、隔离裁决、成绩提交/确认、
发布、重开)都在同一把写锁保护的单事务里完成,事务内读取并固定证据水位,
因此锁定/发布与补证并发时只有一个水位能胜出,不会出现多版本混合榜单。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flights (
    flight_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    evidence_waterline INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS packets (
    packet_id TEXT PRIMARY KEY,
    flight_id TEXT NOT NULL,
    uploader_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    received_at TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    duplicates INTEGER NOT NULL,
    quarantined INTEGER NOT NULL,
    waterline INTEGER
);

CREATE TABLE IF NOT EXISTS points (
    flight_id TEXT NOT NULL,
    point_id TEXT NOT NULL,
    packet_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    device_session_id TEXT NOT NULL,
    device_seq INTEGER NOT NULL,
    device_time TEXT NOT NULL,
    received_at TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    altitude_m REAL NOT NULL,
    digest TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL,              -- accepted / quarantined
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (flight_id, point_id)
);

CREATE TABLE IF NOT EXISTS quarantine (
    quarantine_id TEXT PRIMARY KEY,
    flight_id TEXT NOT NULL,
    point_id TEXT NOT NULL,
    existing_payload TEXT NOT NULL,
    incoming_payload TEXT NOT NULL,
    incoming_hash TEXT NOT NULL,
    incoming_packet_id TEXT NOT NULL,
    status TEXT NOT NULL,              -- pending / resolved
    resolution TEXT,                   -- keep_existing / keep_incoming
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT
);

CREATE TABLE IF NOT EXISTS calibrations (
    calibration_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    version TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    altitude_offset_m REAL NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS gates (
    gate_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    kind TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    radius_m REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS no_fly_zones (
    zone_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    radius_m REAL NOT NULL,
    ceiling_m REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS segmentation_runs (
    segmentation_id TEXT PRIMARY KEY,
    flight_id TEXT NOT NULL,
    waterline INTEGER NOT NULL,
    calibration_version TEXT NOT NULL,
    altitude_offset_m REAL NOT NULL,
    point_count INTEGER NOT NULL,
    complete INTEGER NOT NULL,
    events_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legs (
    leg_id TEXT PRIMARY KEY,
    segmentation_id TEXT NOT NULL,
    flight_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    from_gate_id TEXT,
    to_gate_id TEXT,
    entered_at TEXT,
    exited_at TEXT,
    duration_s REAL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    point_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS scores (
    score_id TEXT PRIMARY KEY,
    flight_id TEXT NOT NULL,
    segmentation_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    waterline INTEGER NOT NULL,
    submitted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    total_time_s REAL NOT NULL,
    complete INTEGER NOT NULL,
    status TEXT NOT NULL,              -- frozen / locked / superseded
    supersedes TEXT
);

CREATE TABLE IF NOT EXISTS score_legs (
    score_id TEXT NOT NULL,
    leg_id TEXT NOT NULL,
    decision TEXT NOT NULL,            -- adopted / excluded
    reason TEXT NOT NULL,
    PRIMARY KEY (score_id, leg_id)
);

CREATE TABLE IF NOT EXISTS confirmations (
    confirmation_id TEXT PRIMARY KEY,
    score_id TEXT NOT NULL,
    arbitrator_id TEXT NOT NULL,
    waterline_seen INTEGER NOT NULL,
    counts INTEGER NOT NULL,           -- 1 = 所见证据版本与冻结版本一致
    created_at TEXT NOT NULL,
    UNIQUE (score_id, arbitrator_id)
);

CREATE TABLE IF NOT EXISTS rankings (
    ranking_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    waterline INTEGER NOT NULL,
    status TEXT NOT NULL,              -- current / superseded
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ranking_entries (
    ranking_id TEXT NOT NULL,
    flight_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    score_id TEXT NOT NULL,
    total_time_s REAL NOT NULL,
    complete INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    rank_delta INTEGER,                -- 相对上一版排名的变化(正数=上升)
    PRIMARY KEY (ranking_id, flight_id)
);

CREATE TABLE IF NOT EXISTS publications (
    publication_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    ranking_id TEXT NOT NULL,
    waterline INTEGER NOT NULL,
    published_at TEXT NOT NULL,
    appeal_deadline TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    flight_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    total_time_s REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assessments (
    assessment_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    trigger_packet_id TEXT,
    waterline_before INTEGER NOT NULL,
    waterline_after INTEGER NOT NULL,
    would_change INTEGER NOT NULL,
    details_json TEXT NOT NULL,
    status TEXT NOT NULL,              -- pending / reopen_approved / rejected
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT
);

CREATE TABLE IF NOT EXISTS reopens (
    reopen_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    assessment_id TEXT,
    approved_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,              -- open / closed
    created_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,                -- recompute_event / impact_assessment
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,              -- queued / running / done / failed
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT
);
"""


class Store:
    """单连接 + 可重入写锁;写事务用 BEGIN IMMEDIATE 串行化。"""

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('waterline', '0')")

    @contextmanager
    def txn(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()
