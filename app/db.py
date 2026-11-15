"""SQLite 数据访问层。

一致性设计：
- WAL 模式：读事务看到一致的已提交快照，读写互不阻塞（两个 API 实例共享同一库文件）。
- 导入用 BEGIN IMMEDIATE 单事务提交，任何一行失败整体 ROLLBACK，已有结果不变。
- 核算查询在显式只读事务（BEGIN ... COMMIT）内完成，一次核算只读同一已提交状态。
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL CHECK (kind IN ('readings', 'meter_changes')),
    source_name TEXT NOT NULL DEFAULT '',
    row_count   INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS readings (
    mp        TEXT NOT NULL,               -- 计量点
    meter     TEXT NOT NULL,               -- 表号
    ts        TEXT NOT NULL,               -- UTC 整点，格式 YYYY-MM-DDTHH:00:00Z
    cum_kwh   REAL NOT NULL,               -- 累计 kWh
    import_id INTEGER NOT NULL REFERENCES imports(id),
    PRIMARY KEY (mp, meter, ts)
);
CREATE INDEX IF NOT EXISTS idx_readings_mp_ts ON readings (mp, ts);

CREATE TABLE IF NOT EXISTS meter_changes (
    mp              TEXT NOT NULL,         -- 计量点
    change_ts       TEXT NOT NULL,         -- 换表整点 UTC
    old_meter       TEXT NOT NULL,         -- 旧表号
    new_meter       TEXT NOT NULL,         -- 新表号
    old_final_kwh   REAL NOT NULL,         -- 旧表末读数（边界读数）
    new_initial_kwh REAL NOT NULL,         -- 新表初值（边界读数）
    import_id       INTEGER NOT NULL REFERENCES imports(id),
    PRIMARY KEY (mp, change_ts)
);
CREATE INDEX IF NOT EXISTS idx_meter_changes_mp_ts ON meter_changes (mp, change_ts);
"""


def db_path() -> str:
    return os.environ.get("DB_PATH", "/data/energy.db")


def connect() -> sqlite3.Connection:
    """新建连接（autocommit 模式，事务由调用方显式控制）。"""
    conn = sqlite3.connect(db_path(), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    path = db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
