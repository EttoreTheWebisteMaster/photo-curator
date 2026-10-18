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
    taken_at      TEXT,          -- ISO 8601, local time as written in the EXIF / file name
    taken_source  TEXT,          -- exif_original | exif_datetime | filename_datetime |
                                 -- filename_date_mtime | filename_date | file_mtime
    time_known    INTEGER,       -- 1 = time of day is trustworthy, 0 = only the date is usable
    phash         TEXT,          -- perceptual hash (hex, 64 bit)
    sharpness     REAL,          -- variance of the Laplacian over the whole image (provisional)
    error         TEXT,          -- set when the file could not be read
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
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade databases created by older versions."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(photos)")}
    if "time_known" not in cols:  # added in 0.2.0; old rows get re-scanned automatically
        conn.execute("ALTER TABLE photos ADD COLUMN time_known INTEGER")
        conn.commit()


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn
