from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFilter

from curator import benchmark, db, embeddings, group, scan


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


def _fake_embedder(seed: int = 0) -> embeddings.Embedder:
    """Deterministic 8-dim embedder for tests; avoids importing torch."""
    class _Fake(embeddings.Embedder):
        name = "fake"

        @property
        def dim(self) -> int:
            return 8

        def embed_batch(self, images):
            rng = np.random.default_rng(seed)
            v = rng.standard_normal((len(images), 8)).astype(np.float32)
            return v / np.linalg.norm(v, axis=-1, keepdims=True)

    return _Fake()


def _insert_photo(conn, name: str, sharpness: float) -> int:
    """Bare-bones row used by benchmark tests (no files on disk)."""
    cur = conn.execute(
        "INSERT INTO photos (path, size, mtime, taken_at, taken_source, time_known, "
        "sharpness, phash, scanned_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (f"/fake/{name}.jpg", 1, 0.0, "2026-10-01T10:00:00", "file_mtime", 1,
         sharpness, "0000000000000000", "2026-10-01T00:00:00"),
    )
    return cur.lastrowid


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


# ---- Step 1 tests (unchanged) -------------------------------------------------------------

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


# ---- Step 2: embeddings storage, grouping method, benchmark -------------------------------

def test_embedding_storage_roundtrip(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO photos (path, size, mtime, scanned_at) VALUES (?,?,?,?)",
        ("/fake.jpg", 1, 0.0, "2026-01-01T00:00:00"),
    )
    conn.commit()
    vec = np.random.default_rng(0).standard_normal(384).astype(np.float32)
    vec /= np.linalg.norm(vec)
    embeddings.save_embedding(conn, 1, "test", vec)
    loaded = embeddings.load_embedding(conn, 1, "test")
    np.testing.assert_allclose(loaded, vec, rtol=1e-6)


def test_embedding_is_upsert(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO photos (path, size, mtime, scanned_at) VALUES (?,?,?,?)",
        ("/fake.jpg", 1, 0.0, "2026-01-01T00:00:00"),
    )
    conn.commit()
    v1 = np.ones(8, dtype=np.float32) / np.sqrt(8)
    v2 = np.zeros(8, dtype=np.float32)
    v2[0] = 1.0
    embeddings.save_embedding(conn, 1, "test", v1)
    embeddings.save_embedding(conn, 1, "test", v2)
    np.testing.assert_allclose(embeddings.load_embedding(conn, 1, "test"), v2)


def test_embed_folder_with_fake_embedder_is_incremental(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(3):
        Image.new("RGB", (64, 48), color=(i * 40, 0, 0)).save(root / f"p{i}.jpg")

    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)

    stats = embeddings.embed_folder(conn, "fake", embedder=_fake_embedder())
    assert stats["embedded"] == 3 and stats["candidates"] == 3 and stats["errors"] == 0
    for pid in (1, 2, 3):
        v = embeddings.load_embedding(conn, pid, "fake")
        assert v is not None and v.shape == (8,)

    stats = embeddings.embed_folder(conn, "fake", embedder=_fake_embedder())
    assert stats["embedded"] == 0 and stats["candidates"] == 0


def test_group_by_embeddings(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(3):
        Image.new("RGB", (64, 48), color=(200, 100, 50 + i)).save(root / f"a{i}.jpg")
    Image.new("RGB", (64, 48), color=(50, 100, 200)).save(root / "b0.jpg")

    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)

    # Hand-crafted embeddings: a* cluster on e1, b0 on e3.
    pids = {Path(r["path"]).name: r["id"] for r in conn.execute("SELECT id, path FROM photos")}
    for i, name in enumerate(("a0.jpg", "a1.jpg", "a2.jpg")):
        v = np.array([1.0, 0.01 * i, 0.0, 0.0] + [0.0] * 380, dtype=np.float32)
        embeddings.save_embedding(conn, pids[name], "test", v / np.linalg.norm(v))
    v = np.array([0.0, 0.0, 1.0, 0.0] + [0.0] * 380, dtype=np.float32)
    embeddings.save_embedding(conn, pids["b0.jpg"], "test", v)
    conn.commit()

    group.build_groups(conn, method="embeddings", model="test", cosine_threshold=0.2)
    g = {Path(r["path"]).name: r["group_id"] for r in conn.execute(
        "SELECT p.path, g.group_id FROM groups g JOIN photos p ON p.id = g.photo_id")}
    assert g["a0.jpg"] == g["a1.jpg"] == g["a2.jpg"]
    assert g["b0.jpg"] != g["a0.jpg"]


def test_combined_method_needs_model(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    with pytest.raises(ValueError):
        group.build_groups(conn, method="combined")


def test_scorer_benchmark_hits_labeled_winner(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    ids = [_insert_photo(conn, f"p{s}", s) for s in (100.0, 300.0, 50.0)]
    for pid in ids:
        conn.execute("INSERT INTO groups (photo_id, group_id, run_id) VALUES (?,?,?)",
                     (pid, 1, "r1"))
    conn.execute(
        "INSERT INTO labels (group_id, photo_id, label, created_at) VALUES (?,?,?,?)",
        (1, ids[1], "winner", "2026-10-01T00:00:00"),
    )
    conn.commit()

    res = benchmark.run_scorer_benchmark(conn, {"sharpness": benchmark.scorer_sharpness})
    assert res["sharpness"] == {"hits": 1, "total": 1, "accuracy": 1.0}


def test_scorer_benchmark_counts_misses(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    ids = [_insert_photo(conn, f"p{s}", s) for s in (100.0, 300.0, 50.0)]
    for pid in ids:
        conn.execute("INSERT INTO groups (photo_id, group_id, run_id) VALUES (?,?,?)",
                     (pid, 1, "r1"))
    conn.execute(  # the *least* sharp one is the labeled winner -> sharpness must miss
        "INSERT INTO labels (group_id, photo_id, label, created_at) VALUES (?,?,?,?)",
        (1, ids[2], "winner", "2026-10-01T00:00:00"),
    )
    conn.commit()

    res = benchmark.run_scorer_benchmark(conn, {"sharpness": benchmark.scorer_sharpness})
    assert res["sharpness"] == {"hits": 0, "total": 1, "accuracy": 0.0}


# ---- Step 3 (Sprint 1): web UI skeleton --------------------------------------------------

def test_web_app_serves_index_with_fixtures():
    from curator.web.app import create_app
    app = create_app(use_fixtures=True)
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.data.decode()
    # Sidebar is client-rendered from initial-data JSON; assert on the payload.
    assert '"useFixtures": true' in body
    assert '"groups":' in body


def test_web_api_endpoints_return_ok():
    from curator.web.app import create_app
    app = create_app(use_fixtures=True)
    client = app.test_client()
    r = client.post("/api/groups/1/choose", json={"photo_id": "g1b"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    r = client.post("/api/groups/1/confirm")
    assert r.status_code == 200 and r.get_json()["status"] == "completed"
    r = client.post("/api/groups/1/delete")
    assert r.status_code == 200 and r.get_json()["status"] == "deleted"


# ---- Step 3 (Sprint 2 refinements) --------------------------------------------------------

def test_progress_estimator_warms_up_then_estimates():
    from curator.web.progress import ProgressEstimator
    import time

    est = ProgressEstimator(total=1000, min_samples=20)

    # 19 updates: not enough samples yet → still calibrating
    for i in range(19):
        snap = est.update(i + 1)
    assert snap["eta_label"] == "Calibrating…"
    assert snap["eta_seconds"] is None

    # 20th update and beyond: enough samples → estimating
    for i in range(19, 119):
        time.sleep(0.005)  # ~200 updates/s → rate is meaningful
        snap = est.update(i + 1)
    assert snap["eta_seconds"] is not None
    assert snap["eta_seconds"] > 0
    assert "min remaining" in snap["eta_label"] or "s remaining" in snap["eta_label"]


def test_progress_estimator_never_zero_before_end():
    from curator.web.progress import ProgressEstimator
    import time
    est = ProgressEstimator(total=100, min_samples=5)
    for i in range(99):
        time.sleep(0.005)
        est.update(i + 1)
    snap = est.update(99)
    # even at 99/100, ETA must be > 0 (never "0s remaining" while bar < 100%)
    if snap["eta_seconds"] is not None:
        assert snap["eta_seconds"] >= 0


def test_analyze_status_endpoint_shape():
    from curator.web.app import create_app
    app = create_app(use_fixtures=True)
    client = app.test_client()
    r = client.get("/api/analyze/status")
    assert r.status_code == 200
    body = r.get_json()
    assert set(body.keys()) >= {"running", "tasks", "error", "final"}


def test_analyze_endpoint_fixtures_mode():
    from curator.web.app import create_app
    app = create_app(use_fixtures=True)
    client = app.test_client()
    r = client.post("/api/analyze", json={"total": 50})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_empty_db_redirects_to_intake(tmp_path):
    from curator import db
    from curator.web.app import create_app
    db.connect(tmp_path / "t.db").close()
    app = create_app(db_path=str(tmp_path / "t.db"), use_fixtures=False)
    client = app.test_client()
    r = client.get("/")
    assert r.status_code in (301, 302, 303, 307, 308)
    assert "/intake" in r.headers["Location"]


def test_analysis_route_redirects_when_idle(tmp_path):
    from curator import db
    from curator.web.app import create_app
    db.connect(tmp_path / "t.db").close()
    app = create_app(db_path=str(tmp_path / "t.db"), use_fixtures=False)
    client = app.test_client()
    r = client.get("/analysis")
    assert r.status_code in (301, 302, 303, 307, 308)
    assert "/intake" in r.headers["Location"]


def test_real_analysis_end_to_end(tmp_path):
    import time
    from curator import db
    from curator.web.app import create_app
    from PIL import Image

    root = tmp_path / "photos"
    root.mkdir()
    Image.new("RGB", (64, 48), (200, 100, 50)).save(root / "a.jpg")
    Image.new("RGB", (64, 48), (10, 20, 30)).save(root / "b.jpg")

    app = create_app(db_path=str(tmp_path / "t.db"), use_fixtures=False)
    client = app.test_client()

    r = client.post("/api/analyze", json={"folder": str(root)})
    assert r.status_code == 200
    assert r.get_json()["mode"] == "real"

    # Poll until analysis completes (max 10s)
    status = None
    for _ in range(100):
        status = client.get("/api/analyze/status").get_json()
        if not status["running"]:
            break
        time.sleep(0.1)

    assert status is not None
    assert not status["running"]
    assert status["error"] is None
    assert status["final"]["mode"] in ("fresh", "no_changes")
    assert status["final"].get("groups", 0) >= 1

    # Verify group_states were populated
    conn = db.connect(tmp_path / "t.db")
    n = conn.execute("SELECT COUNT(*) FROM group_states").fetchone()[0]
    assert n >= 1
    conn.close()

    # Re-run same folder: fingerprint matches → no_changes
    r = client.post("/api/analyze", json={"folder": str(root)})
    for _ in range(100):
        status = client.get("/api/analyze/status").get_json()
        if not status["running"]:
            break
        time.sleep(0.1)
    assert status["final"]["mode"] == "no_changes"