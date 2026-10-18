"""SQLite store. Holds metadata only: photos are never copied or modified."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id            INTEGER PRIMARY KEY,
    path          TEXT NOT NULL UNIQUE,
    size          INTEGER NOT NULL,
    mtime         REAL NOT NULL,
    sha256        TEXT,
    width         INTEGER,
    height        INTEGER,
    taken_at      TEXT,
    taken_source  TEXT,
    time_known    INTEGER,
    phash         TEXT,
    sharpness     REAL,
    error         TEXT,
    scanned_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_photos_taken_at ON photos(taken_at);
CREATE INDEX IF NOT EXISTS idx_photos_sha256 ON photos(sha256);

CREATE TABLE IF NOT EXISTS groups (
    photo_id  INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
    group_id  INTEGER NOT NULL,
    run_id    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_groups_group ON groups(group_id);

-- Step 2: visual embeddings, one row per (photo, model).
-- float32 little-endian, L2-normalized -> cosine = dot product.
CREATE TABLE IF NOT EXISTS embeddings (
    photo_id    INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (photo_id, model)
);
CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model);

-- Step 2: user labels for the benchmark and (later) the personal ranker.
-- "winner" = best in its group, "forse" = keep but not sure, "rejected" = discard.
CREATE TABLE IF NOT EXISTS labels (
    id          INTEGER PRIMARY KEY,
    group_id    INTEGER NOT NULL,
    photo_id    INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    label       TEXT NOT NULL,
    reason      TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE(group_id, photo_id, label)
);
CREATE INDEX IF NOT EXISTS idx_labels_group ON labels(group_id);
CREATE INDEX IF NOT EXISTS idx_labels_photo ON labels(photo_id);

-- Step 2: benchmark results, one row per (run, method).
CREATE TABLE IF NOT EXISTS benchmark_runs (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL,
    task        TEXT NOT NULL,           -- 'scorer' (later: 'grouping')
    method      TEXT NOT NULL,
    groups      INTEGER NOT NULL,        -- groups evaluated
    hits        INTEGER NOT NULL,
    accuracy    REAL NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bench_run ON benchmark_runs(run_id);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(photos)")}
    if "time_known" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN time_known INTEGER")
        conn.commit()


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn