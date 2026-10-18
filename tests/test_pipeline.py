from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFilter

from curator import db, group, scan


def make_scene(seed: int, size=(640, 480)) -> Image.Image:
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 255, (12, 16, 3), dtype=np.uint8)
    img = np.asarray(Image.fromarray(base).resize(size, Image.BICUBIC)).astype(np.int16)
    texture = rng.integers(-40, 40, img.shape)  # fine detail, like a real photo
    return Image.fromarray(np.clip(img + texture, 0, 255).astype(np.uint8))


def save(img: Image.Image, path: Path, taken: str | None = None, quality=92, mtime: str | None = None):
    exif = Image.Exif()
    if taken:
        exif.get_ifd(0x8769)[36867] = taken  # DateTimeOriginal
    img.save(path, "JPEG", quality=quality, exif=exif)
    if mtime:
        ts = datetime.fromisoformat(mtime).timestamp()
        os.utime(path, (ts, ts))


def tree_hash(root: Path) -> dict:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def photos(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    a = make_scene(1)
    save(a, root / "a1.jpg", "2026:10:01 10:00:00")
    save(a.filter(ImageFilter.GaussianBlur(6)), root / "a2_blurry.jpg", "2026:10:01 10:00:05")
    save(a, root / "a3.jpg", "2026:10:01 10:00:09", quality=70)
    save(make_scene(2), root / "b1.jpg", "2026:10:01 10:05:00")      # different scene, same session
    save(a, root / "c1.jpg", "2026:10:02 18:00:00")                  # same scene, another day
    (root / "broken.jpg").write_bytes(b"this is not a jpeg")
    (root / "notes.txt").write_text("ignore me")
    return root


def test_scan_is_read_only_and_reports_errors(photos, tmp_path):
    before = tree_hash(photos)
    conn = db.connect(tmp_path / "t.db")
    stats = scan.scan(photos, conn)
    assert tree_hash(photos) == before  # no file was touched
    assert stats["seen"] == 6 and stats["errors"] == 1 and stats["new"] == 6
    row = conn.execute("SELECT * FROM photos WHERE path LIKE '%a1.jpg'").fetchone()
    assert row["taken_at"] == "2026-10-01T10:00:00"
    assert row["taken_source"] == "exif_original" and row["time_known"] == 1
    assert conn.execute("SELECT error FROM photos WHERE path LIKE '%broken.jpg'").fetchone()[0]


def test_sharpness_ranks_blurred_lower(photos, tmp_path):
    conn = db.connect(tmp_path / "t.db")
    scan.scan(photos, conn)
    s = {Path(r["path"]).name: r["sharpness"]
         for r in conn.execute("SELECT path, sharpness FROM photos WHERE error IS NULL")}
    assert s["a2_blurry.jpg"] < s["a1.jpg"] * 0.5


def test_incremental_rescan(photos, tmp_path):
    conn = db.connect(tmp_path / "t.db")
    scan.scan(photos, conn)
    stats = scan.scan(photos, conn)
    assert stats["skipped"] == 5 and stats["new"] == 0  # only the broken file is retried
    assert scan.scan(photos, conn, force=True)["skipped"] == 0


def test_grouping_by_session_and_similarity(photos, tmp_path):
    conn = db.connect(tmp_path / "t.db")
    scan.scan(photos, conn)
    res = group.build_groups(conn, session_gap_min=30, max_hamming=10)
    g = {Path(r["path"]).name: r["group_id"] for r in conn.execute(
        "SELECT p.path, g.group_id FROM groups g JOIN photos p ON p.id = g.photo_id")}
    assert g["a1.jpg"] == g["a3.jpg"]          # same scene, same session
    assert g["b1.jpg"] != g["a1.jpg"]          # different scene
    assert g["c1.jpg"] != g["a1.jpg"]          # another day = another session
    assert res["sessions"] == 2


def test_photoslibrary_is_rejected(tmp_path):
    lib = tmp_path / "Photos Library.photoslibrary" / "originals"
    lib.mkdir(parents=True)
    conn = db.connect(tmp_path / "t.db")
    with pytest.raises(scan.LibraryRejected):
        scan.scan(lib, conn)


# ---- capture date from file names (WhatsApp, Android, screenshots) -----------------------

@pytest.mark.parametrize("name, expected", [
    ("IMG-20210909-WA0026.jpg", (date(2021, 9, 9), None)),                      # WhatsApp
    ("IMG_20210909_143012.jpg", (date(2021, 9, 9), time(14, 30, 12))),           # Android camera
    ("PXL_20210909_143012345.jpg", (date(2021, 9, 9), time(14, 30, 12))),        # Pixel
    ("Screenshot 2021-09-09 at 14.30.12.png", (date(2021, 9, 9), time(14, 30, 12))),
    ("WhatsApp Image 2021-09-09 at 14.30.12.jpeg", (date(2021, 9, 9), time(14, 30, 12))),
    ("20210909_143012.jpg", (date(2021, 9, 9), time(14, 30, 12))),
    ("IMG_1234.jpg", None),                                                     # no date
    ("IMG-20210230-WA0001.jpg", None),                                          # impossible date
    ("scan_12345678.jpg", None),                                                # digits, not a date
])
def test_parse_filename_datetime(name, expected):
    assert scan.parse_filename_datetime(name) == expected


def test_whatsapp_file_without_exif(tmp_path):
    root = tmp_path / "wa"
    root.mkdir()
    # same day as the file name: time of day is taken from the modification time
    save(make_scene(3), root / "IMG-20210909-WA0001.jpg", mtime="2021-09-09T18:20:00")
    # modification date is months later (file copied): only the date from the name is trusted
    save(make_scene(4), root / "IMG-20210909-WA0026.jpg", mtime="2021-11-25T13:36:51")
    # no date anywhere in the name: modification time, flagged as unreliable
    save(make_scene(5), root / "random_name.jpg", mtime="2021-05-01T08:00:00")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    r = {Path(x["path"]).name: x for x in conn.execute("SELECT * FROM photos")}
    a, b, c = r["IMG-20210909-WA0001.jpg"], r["IMG-20210909-WA0026.jpg"], r["random_name.jpg"]
    assert (a["taken_at"], a["taken_source"], a["time_known"]) == ("2021-09-09T18:20:00", "filename_date_mtime", 1)
    assert (b["taken_at"], b["taken_source"], b["time_known"]) == ("2021-09-09T00:00:00", "filename_date", 0)
    assert (c["taken_at"][:10], c["taken_source"], c["time_known"]) == ("2021-05-01", "file_mtime", 0)


def test_unknown_time_photos_are_bucketed_by_day(tmp_path):
    root = tmp_path / "wa"
    root.mkdir()
    scene = make_scene(6)
    # same scene on the same day, modification dates far away from each other and from the day
    save(scene, root / "IMG-20210909-WA0001.jpg", mtime="2021-12-01T09:00:00")
    save(scene, root / "IMG-20210909-WA0002.jpg", mtime="2022-01-20T23:00:00")
    save(scene, root / "IMG-20210910-WA0001.jpg", mtime="2022-01-21T09:00:00")  # next day
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    res = group.build_groups(conn)
    g = {Path(x["path"]).name: x["group_id"] for x in conn.execute(
        "SELECT p.path, g.group_id FROM groups g JOIN photos p ON p.id = g.photo_id")}
    assert g["IMG-20210909-WA0001.jpg"] == g["IMG-20210909-WA0002.jpg"]
    assert g["IMG-20210910-WA0001.jpg"] != g["IMG-20210909-WA0001.jpg"]
    assert res["day_buckets"] == 2


def test_old_database_is_migrated_and_rescanned(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    save(make_scene(7), root / "IMG-20210909-WA0001.jpg", mtime="2021-09-09T12:00:00")
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)  # schema from version 0.1.0 (no time_known column)
    conn.executescript(db.SCHEMA.replace("    time_known    INTEGER,", ""))
    p = str((root / "IMG-20210909-WA0001.jpg").resolve())
    st = os.stat(p)
    conn.execute("INSERT INTO photos (path,size,mtime,taken_at,taken_source,phash,scanned_at) "
                 "VALUES (?,?,?,?,?,?,?)", (p, st.st_size, st.st_mtime, "2021-09-09T12:00:00",
                                            "file_mtime", "0" * 16, "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()
    conn = db.connect(path)  # migrates
    stats = scan.scan(root, conn)
    assert stats["updated"] == 1 and stats["skipped"] == 0  # old row is re-scanned, not skipped
    row = conn.execute("SELECT taken_source, time_known FROM photos").fetchone()
    assert (row["taken_source"], row["time_known"]) == ("filename_date_mtime", 1)


def test_unknown_time_photo_joins_known_time_photo_of_same_day(tmp_path):
    root = tmp_path / "wa"
    root.mkdir()
    scene = make_scene(11)
    save(scene, root / "IMG-20210909-WA0001.jpg", mtime="2021-09-09T10:00:00")  # time known
    save(scene, root / "IMG-20210909-WA0026.jpg", mtime="2021-11-25T13:36:51")  # only the date
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    group.build_groups(conn)
    ids = {r["group_id"] for r in conn.execute("SELECT group_id FROM groups")}
    assert len(ids) == 1
