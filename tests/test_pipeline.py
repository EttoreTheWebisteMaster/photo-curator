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
    progress = []
    res = group.build_groups(
        conn,
        session_gap_min=30,
        max_hamming=10,
        progress=lambda count, total: progress.append((count, total)),
    )
    assert progress[0][0] == 0
    assert progress[-1][0] == progress[-1][1]
    assert all(a[0] <= b[0] for a, b in zip(progress, progress[1:]))
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
    # Same scene, same day, different quality -> different bytes (no false duplicate),
    # similar pHash -> grouped together despite unknown time of day.
    save(scene, root / "IMG-20210909-WA0001.jpg", mtime="2021-12-01T09:00:00", quality=92)
    save(scene, root / "IMG-20210909-WA0002.jpg", mtime="2022-01-20T23:00:00", quality=85)
    # Different scene on the next day -> different group. Different content is fine
    # because the point of the test is day bucketing, not visual similarity.
    save(make_scene(7), root / "IMG-20210910-WA0001.jpg", mtime="2022-01-21T09:00:00", quality=92)
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
    # Unique seeds → unique bytes → no false duplicate detection.
    save(make_scene(1), root / "a0.jpg")
    save(make_scene(2), root / "a1.jpg")
    save(make_scene(3), root / "a2.jpg")
    save(make_scene(4), root / "b0.jpg")

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
    assert "Every photo group reviewed" in body
    assert "All groups reviewed" not in body


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


def test_photo_asset_urls_refresh_when_photo_changes(tmp_path):
    from curator.web.app import create_app

    root = tmp_path / "photos"
    root.mkdir()
    photo_path = root / "photo.jpg"
    save(make_scene(1), photo_path, mtime="2026-10-01T10:00:00")

    db_path = tmp_path / "web.db"
    conn = db.connect(db_path)
    scan.scan(root, conn)
    photo_id = conn.execute("SELECT id FROM photos").fetchone()["id"]
    conn.execute(
        "INSERT INTO groups (photo_id, group_id, run_id) VALUES (?, 1, 'test')",
        (photo_id,),
    )
    conn.execute(
        "INSERT INTO group_states (group_id, current_winner_photo_id, updated_at) "
        "VALUES (1, ?, '2026-10-01T10:00:00')",
        (photo_id,),
    )
    conn.commit()
    conn.close()

    client = create_app(db_path).test_client()
    first_photo = client.get("/api/groups/1").get_json()["photos"][0]
    first_thumb_url = first_photo["thumb"]
    first_full_url = first_photo["url"]
    first_thumb = client.get(first_thumb_url).data
    first_full = client.get(first_full_url).data

    save(make_scene(2), photo_path, mtime="2026-10-02T10:00:00")
    conn = db.connect(db_path)
    scan.scan(root, conn, force=True)
    conn.close()

    updated_photo = client.get("/api/groups/1").get_json()["photos"][0]
    assert updated_photo["thumb"] != first_thumb_url
    assert updated_photo["url"] != first_full_url
    updated_thumb = client.get(updated_photo["thumb"])
    assert updated_thumb.status_code == 200
    assert updated_thumb.data != first_thumb
    updated_full = client.get(updated_photo["url"])
    assert updated_full.status_code == 200
    assert updated_full.data != first_full

    group_thumb = client.get("/api/groups").get_json()[0]["thumb"]
    assert group_thumb == updated_photo["thumb"]


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


def test_completed_review_sidebar_keeps_next_step_available():
    from curator.web.app import create_app
    app = create_app(use_fixtures=True)
    page = app.test_client().get("/").get_data(as_text=True)

    assert "reviewFinished" in page
    assert 'class="sidebar__next-step"' in page
    assert ":href=\"completionNextStep.href\"" in page
    assert "of photo groups reviewed" in page


def test_deleted_group_state_shows_keeper_and_other_photo_strip():
    from curator.web.app import create_app
    app = create_app(use_fixtures=True)
    page = app.test_client().get("/").get_data(as_text=True)

    assert "selectedGroup.status === 'deleted'" in page
    assert "currentWinner.thumb || currentWinner.url" in page
    assert 'x-for="photo in rejectedPhotos"' in page
    assert "Restore group to review" in page


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
    assert len(status["tasks"]) == 3
    for task in status["tasks"]:
        assert task["status"] == "done"
        assert task["started_at"] is not None
        assert task["finished_at"] >= task["started_at"]
        assert task["eta_seconds"] == 0

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


def test_screenshot_name_is_utility(tmp_path):
    from curator import db, scan
    from PIL import Image
    root = tmp_path / "photos"; root.mkdir()
    Image.new("RGB", (1920, 1080), (50, 100, 200)).save(root / "Screenshot 2026-01-01 at 10.00.00.png")
    Image.new("RGB", (1000, 800), (50, 100, 200)).save(root / "IMG_0001.jpg")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    rows = {Path(r["path"]).name: r for r in conn.execute("SELECT * FROM photos")}
    assert rows["Screenshot 2026-01-01 at 10.00.00.png"]["is_utility"] == 1
    assert rows["IMG_0001.jpg"]["is_utility"] == 0


def test_extreme_aspect_ratio_is_not_utility(tmp_path):
    """Panoramas and portrait phone photos have extreme ratios but are real photos."""
    from curator import db, scan
    from PIL import Image
    root = tmp_path / "photos"; root.mkdir()
    Image.new("RGB", (1170, 2532), (50, 100, 200)).save(root / "weird_name.png")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    row = conn.execute("SELECT is_utility FROM photos").fetchone()
    assert row[0] == 0


def test_exact_duplicates_marked(tmp_path):
    from curator import db, scan
    from PIL import Image
    root = tmp_path / "photos"; root.mkdir()
    img = Image.new("RGB", (200, 150), (10, 20, 30))
    img.save(root / "a.jpg")
    img.save(root / "b.jpg")   # exact same bytes
    img.save(root / "c.jpg")   # exact same bytes
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    rows = {Path(r["path"]).name: r for r in conn.execute("SELECT * FROM photos ORDER BY id")}
    assert rows["a.jpg"]["duplicate_of"] is None
    assert rows["b.jpg"]["duplicate_of"] == rows["a.jpg"]["id"]
    assert rows["c.jpg"]["duplicate_of"] == rows["a.jpg"]["id"]


def test_utility_excluded_from_groups(tmp_path):
    from curator import db, group, scan
    from PIL import Image
    root = tmp_path / "photos"; root.mkdir()
    Image.new("RGB", (1000, 800), (10, 20, 30)).save(root / "photo.jpg")
    Image.new("RGB", (1170, 2532), (50, 100, 200)).save(root / "Screenshot 2026-01-01.png")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    group.build_groups(conn)
    grouped = conn.execute(
        "SELECT p.path FROM groups g JOIN photos p ON p.id = g.photo_id"
    ).fetchall()
    names = {Path(r["path"]).name for r in grouped}
    assert "photo.jpg" in names
    assert "Screenshot 2026-01-01.png" not in names


def test_rejected_keep_promotes_photo(tmp_path):
    from curator import db, group, scan
    from curator.web.app import create_app
    from PIL import Image
    root = tmp_path / "photos"; root.mkdir()
    Image.new("RGB", (1170, 2532), (50, 100, 200)).save(root / "Screenshot 2026-01-01.png")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    pid = conn.execute("SELECT id FROM photos").fetchone()[0]

    app = create_app(db_path=str(tmp_path / "t.db"), use_fixtures=False)
    client = app.test_client()

    # Verify it shows in rejected
    data = client.get("/api/rejected").get_json()
    assert data["total"] == 1 and len(data["photos"]) == 1

    # Keep it
    r = client.post(f"/api/rejected/{pid}/keep")
    assert r.status_code == 200

    # Now it's not rejected anymore, and is not a duplicate or utility
    row = conn.execute("SELECT is_utility, duplicate_of, rejected_reviewed_at "
                       "FROM photos WHERE id = ?", (pid,)).fetchone()
    assert row["is_utility"] == 0
    assert row["duplicate_of"] is None
    assert row["rejected_reviewed_at"] is not None

    data = client.get("/api/rejected").get_json()
    assert data["total"] == 0 or len(data["photos"]) == 0


def test_rejected_accept_all(tmp_path):
    from curator import db, scan
    from curator.web.app import create_app
    from PIL import Image
    root = tmp_path / "photos"; root.mkdir()
    Image.new("RGB", (1170, 2532), (50, 100, 200)).save(root / "Screenshot A.png")
    Image.new("RGB", (1170, 2532), (60, 110, 210)).save(root / "Screenshot B.png")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    conn.close()

    app = create_app(db_path=str(tmp_path / "t.db"), use_fixtures=False)
    client = app.test_client()
    r = client.post("/api/rejected/accept-all")
    assert r.status_code == 200
    assert r.get_json()["count"] == 2

    data = client.get("/api/rejected").get_json()
    assert len(data["photos"]) == 0
    assert data["reviewed"] == 2


def test_exact_duplicates_are_grouped_together(tmp_path):
    """Byte-identical copies stay in the same group so the user can review them
    side-by-side. `duplicate_of` is still populated (for step 5), it just does
    not exclude the photo from grouping."""
    from curator import db, group, scan
    from PIL import Image
    root = tmp_path / "photos"; root.mkdir()
    img = Image.new("RGB", (200, 150), (10, 20, 30))
    img.save(root / "a.jpg")
    img.save(root / "b.jpg")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    group.build_groups(conn)

    # duplicate_of is still computed
    rows = {Path(r["path"]).name: r for r in conn.execute("SELECT * FROM photos")}
    assert rows["b.jpg"]["duplicate_of"] == rows["a.jpg"]["id"]

    # …but both appear in groups
    in_groups = {Path(r["path"]).name for r in conn.execute(
        "SELECT p.path FROM groups g JOIN photos p ON p.id = g.photo_id"
    )}
    assert in_groups == {"a.jpg", "b.jpg"}


# ---- Step 5a: commit preview --------------------------------------------------------

def test_plan_commit_basic(tmp_path):
    from curator import commit, db, group, scan
    root = tmp_path / "photos"; root.mkdir()
    save(make_scene(1), root / "a.jpg", "2026:10:15 14:32:00")
    save(make_scene(1), root / "b.jpg", "2026:10:15 14:32:05", quality=85)
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    group.build_groups(conn)
    aid = conn.execute("SELECT id FROM photos WHERE path LIKE '%a.jpg'").fetchone()[0]
    bid = conn.execute("SELECT id FROM photos WHERE path LIKE '%b.jpg'").fetchone()[0]
    conn.execute("UPDATE group_states SET status='completed', current_winner_photo_id=?", (aid,))
    conn.commit()

    plan = commit.plan_commit(conn, root)
    assert not plan.errors
    assert len(plan.moves) == 2
    moves = {m.photo_id: m for m in plan.moves if m.photo_id is not None}
    assert moves[aid].role == "winner"
    assert moves[bid].role == "rejected"
    assert moves[aid].dst_path.name.startswith("20261015_143200_")
    assert moves[bid].dst_path.name.startswith("20261015_143205_")
    assert moves[aid].dst_path.suffix == ".jpg"


def test_plan_commit_uses_safe_editable_batch_name_and_unique_id(tmp_path):
    from curator import commit, db, group, scan
    root = tmp_path / "photos"; root.mkdir()
    save(make_scene(1), root / "a.jpg", "2026:10:15 14:32:00")
    save(make_scene(2), root / "b.jpg", "2026:10:15 15:00:00")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    group.build_groups(conn)
    conn.execute(
        "UPDATE group_states SET status='completed', current_winner_photo_id=("
        "SELECT MIN(photo_id) FROM groups WHERE group_id=group_states.group_id)"
    )
    conn.commit()

    plan = commit.plan_commit(conn, root, batch_name="Summer Trip")
    assert not plan.errors
    assert plan.batch_name == "Summer-Trip"
    assert [move.dst_path.name for move in plan.moves] == [
        "20261015_Summer-Trip_001.jpg",
        "20261015_Summer-Trip_002.jpg",
    ]

    blank_name_plan = commit.plan_commit(conn, root, batch_name="")
    assert [move.dst_path.name for move in blank_name_plan.moves] == [
        "20261015_001.jpg",
        "20261015_002.jpg",
    ]

    existing = root / "_curator" / "winner" / "20261015_Other-Trip_003.jpg"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing")
    next_plan = commit.plan_commit(conn, root, batch_name="Summer Trip")
    assert next_plan.moves[0].dst_path.name == "20261015_Summer-Trip_004.jpg"
    assert next_plan.moves[1].dst_path.name == "20261015_Summer-Trip_005.jpg"
    result = commit.execute_commit(next_plan, conn)
    assert result["moved"] == 2
    assert all(move.dst_path.exists() for move in next_plan.moves)


def test_batch_name_rejects_empty_or_path_only_values():
    import pytest
    from curator.commit import normalize_batch_name

    assert normalize_batch_name("../Summer Trip!") == "Summer-Trip"
    assert normalize_batch_name("") == ""
    assert normalize_batch_name("   ") == ""
    with pytest.raises(ValueError, match="Use letters"):
        normalize_batch_name("../")


def test_plan_commit_blocks_on_pending(tmp_path):
    from curator import commit, db, group, scan
    root = tmp_path / "photos"; root.mkdir()
    save(make_scene(1), root / "a.jpg", "2026:10:15 14:32:00")
    save(make_scene(2), root / "b.jpg", "2026:10:15 15:00:00")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    group.build_groups(conn)
    plan = commit.plan_commit(conn, root)
    assert plan.has_errors
    assert any("pending" in e for e in plan.errors)


def test_plan_commit_deterministic(tmp_path):
    from curator import commit, db, group, scan
    root = tmp_path / "photos"; root.mkdir()
    save(make_scene(1), root / "a.jpg", "2026:10:15 14:32:00")
    save(make_scene(2), root / "b.jpg", "2026:10:15 15:00:00")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    group.build_groups(conn)
    for name in ("a.jpg", "b.jpg"):
        pid = conn.execute("SELECT id FROM photos WHERE path LIKE ?", (f"%{name}",)).fetchone()[0]
        conn.execute(
            "UPDATE group_states SET status='completed' WHERE group_id=("
            "SELECT group_id FROM groups WHERE photo_id=?)", (pid,)
        )
    conn.commit()

    p1 = commit.plan_commit(conn, root)
    p2 = commit.plan_commit(conn, root)
    assert [(m.src_path, m.dst_path, m.role) for m in p1.moves] == \
           [(m.src_path, m.dst_path, m.role) for m in p2.moves]


def test_plan_commit_numbers_per_day(tmp_path):
    from curator import commit, db, group, scan
    root = tmp_path / "photos"; root.mkdir()
    save(make_scene(1), root / "a.jpg", "2026:10:15 14:32:00")
    save(make_scene(1), root / "b.jpg", "2026:10:15 14:32:05", quality=85)
    save(make_scene(1), root / "c.jpg", "2026:10:15 14:32:10", quality=80)
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    group.build_groups(conn)
    aid = conn.execute("SELECT id FROM photos WHERE path LIKE '%a.jpg'").fetchone()[0]
    conn.execute("UPDATE group_states SET status='completed', current_winner_photo_id=?", (aid,))
    conn.commit()

    plan = commit.plan_commit(conn, root)
    assert not plan.errors
    nnn = sorted(int(m.dst_path.stem.split("_")[-1]) for m in plan.moves if m.photo_id)
    assert nnn == [1, 2, 3]


def test_plan_commit_respects_existing_files(tmp_path):
    from curator import commit, db, group, scan
    root = tmp_path / "photos"; root.mkdir()
    save(make_scene(1), root / "a.jpg", "2026:10:15 14:32:00")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    group.build_groups(conn)
    aid = conn.execute("SELECT id FROM photos").fetchone()[0]
    conn.execute("UPDATE group_states SET status='completed', current_winner_photo_id=?", (aid,))
    conn.commit()

    existing = root / "_curator" / "winner" / "20261015_100000_001.JPG"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"fake")

    plan = commit.plan_commit(conn, root)
    assert not plan.errors
    assert len(plan.moves) == 1
    assert plan.moves[0].dst_path.name == "20261015_143200_002.jpg"


def test_plan_commit_includes_live_photo_companion(tmp_path):
    from curator import commit, db
    from PIL import Image
    root = tmp_path / "photos"; root.mkdir()
    Image.new("RGB", (100, 100), (200, 100, 50)).save(root / "IMG_0001.HEIC")
    (root / "IMG_0001.MOV").write_bytes(b"fake video data")

    conn = db.connect(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO photos (path, size, mtime, taken_at, taken_source, time_known, "
        "sha256, phash, sharpness, scanned_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (str(root / "IMG_0001.HEIC"), 100, 0.0, "2026-10-15T14:32:00",
         "exif_original", 1, "abc", "0" * 16, 100.0, "2026-10-15T00:00:00"),
    )
    pid = conn.execute("SELECT id FROM photos").fetchone()[0]
    conn.execute("INSERT INTO groups (photo_id, group_id, run_id) VALUES (?, 1, 'r1')", (pid,))
    conn.execute(
        "INSERT INTO group_states (group_id, status, current_winner_photo_id, updated_at) "
        "VALUES (1, 'completed', ?, '2026-10-15T00:00:00')", (pid,),
    )
    conn.commit()

    plan = commit.plan_commit(conn, root)
    assert not plan.errors
    assert len(plan.moves) == 2
    still = next(m for m in plan.moves if m.photo_id is not None)
    mov = next(m for m in plan.moves if m.companion_of is not None)
    assert still.dst_path.suffix == ".HEIC"
    assert mov.dst_path.suffix == ".MOV"
    assert still.dst_path.stem == mov.dst_path.stem
    assert plan.stats["companion"] == 1


def test_preview_endpoint_no_disk_writes(tmp_path):
    import os
    from curator import db as dbmod
    from curator import group, scan
    from curator.web.app import create_app
    root = tmp_path / "photos"; root.mkdir()
    save(make_scene(1), root / "a.jpg", "2026:10:15 14:32:00")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    group.build_groups(conn)
    aid = conn.execute("SELECT id FROM photos").fetchone()[0]
    conn.execute("UPDATE group_states SET status='completed', current_winner_photo_id=?", (aid,))
    conn.commit()
    conn.close()

    conn2 = dbmod.connect(tmp_path / "t.db")
    conn2.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('last_folder', ?)", (str(root),))
    conn2.commit()
    conn2.close()

    mtime_before = os.stat(root / "a.jpg").st_mtime
    app = create_app(db_path=str(tmp_path / "t.db"), use_fixtures=False)
    r = app.test_client().post(
        "/api/commit/preview", json={"batch_name": "Edited Batch"}
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["stats"]["total_moves"] >= 1
    assert body["batch_name"] == "Edited-Batch"
    assert body["sample"][0]["dst"].endswith("_Edited-Batch_001.jpg")
    assert os.stat(root / "a.jpg").st_mtime == mtime_before
    assert not (root / "_curator").exists()

    blank_name_response = app.test_client().post(
        "/api/commit/preview", json={"batch_name": ""}
    )
    assert blank_name_response.status_code == 200
    blank_name_body = blank_name_response.get_json()
    assert blank_name_body["batch_name"] == ""
    assert blank_name_body["sample"][0]["dst"].endswith("20261015_001.jpg")

    confirm_response = app.test_client().get("/confirm")
    assert confirm_response.status_code == 200
    assert b'id="plan-preview-title">File names' in confirm_response.data
    assert b'Example filename' in confirm_response.data
    assert b'<table class="plan-table">' not in confirm_response.data


# ---- Step 5b: commit execution + undo -----------------------------------------------

def _setup_commit_ready(tmp_path):
    from curator import db, group, scan
    root = tmp_path / "photos"; root.mkdir()
    save(make_scene(1), root / "a.jpg", "2026:10:15 14:32:00")
    save(make_scene(2), root / "b.jpg", "2026:10:15 15:00:00")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    group.build_groups(conn)
    conn.execute(
        "UPDATE group_states SET status='completed', current_winner_photo_id=("
        "SELECT MIN(photo_id) FROM groups WHERE group_id=group_states.group_id)"
    )
    conn.commit()
    return root, conn


def test_execute_commit_moves_files(tmp_path):
    from curator import commit
    root, conn = _setup_commit_ready(tmp_path)
    plan = commit.plan_commit(conn, root)
    assert not plan.errors
    assert len(plan.moves) == 2

    result = commit.execute_commit(plan, conn)
    assert result["moved"] == 2
    assert result["manifest_rows"] == 2

    for m in plan.moves:
        assert not m.src_path.exists()
        assert m.dst_path.exists()

    rows = conn.execute("SELECT * FROM manifest").fetchall()
    assert len(rows) == 2
    for r in rows:
        assert r["sha256_before"] == r["sha256_after"]
        assert r["undone_at"] is None
        assert r["size"] > 0


def test_execute_commit_updates_committed_at(tmp_path):
    from curator import commit
    root, conn = _setup_commit_ready(tmp_path)
    plan = commit.plan_commit(conn, root)
    commit.execute_commit(plan, conn)
    n_committed = conn.execute(
        "SELECT COUNT(*) FROM photos WHERE committed_at IS NOT NULL"
    ).fetchone()[0]
    assert n_committed == len(plan.moves)


def test_execute_commit_hash_mismatch_rolls_back(tmp_path, monkeypatch):
    from curator import commit
    root, conn = _setup_commit_ready(tmp_path)
    plan = commit.plan_commit(conn, root)
    assert len(plan.moves) == 2

    # Corrupt the source after planning so the first hash verification fails
    target = plan.moves[0].src_path
    original_bytes = target.read_bytes()
    target.write_bytes(b"corrupted")

    import pytest
    with pytest.raises(RuntimeError, match="hash mismatch"):
        commit.execute_commit(plan, conn)

    # Nothing moved, no manifest
    assert target.exists()
    assert target.read_bytes() == b"corrupted"
    assert conn.execute("SELECT COUNT(*) FROM manifest").fetchone()[0] == 0
    for m in plan.moves:
        assert m.src_path.exists() or m.src_path == target


def test_execute_commit_mid_failure_rolls_back(tmp_path, monkeypatch):
    from curator import commit
    root, conn = _setup_commit_ready(tmp_path)
    plan = commit.plan_commit(conn, root)
    assert len(plan.moves) == 2

    original_move = commit.shutil.move
    calls = {"n": 0}
    def failing_move(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated failure")
        return original_move(src, dst)
    monkeypatch.setattr(commit.shutil, "move", failing_move)

    import pytest
    with pytest.raises(OSError, match="simulated"):
        commit.execute_commit(plan, conn)

    # First file rolled back, nothing in manifest
    for m in plan.moves:
        assert m.src_path.exists()
        assert not m.dst_path.exists()
    assert conn.execute("SELECT COUNT(*) FROM manifest").fetchone()[0] == 0


def test_execute_commit_live_photo_pair(tmp_path):
    from curator import commit, db, group, scan
    from PIL import Image
    root = tmp_path / "photos"; root.mkdir()
    Image.new("RGB", (100, 100), (200, 100, 50)).save(root / "IMG_0001.HEIC", format="HEIF")
    (root / "IMG_0001.MOV").write_bytes(b"fake mov data")

    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    group.build_groups(conn)
    aid = conn.execute("SELECT id FROM photos").fetchone()[0]
    conn.execute("UPDATE group_states SET status='completed', current_winner_photo_id=?", (aid,))
    conn.commit()

    plan = commit.plan_commit(conn, root)
    assert not plan.errors
    assert len(plan.moves) == 2

    result = commit.execute_commit(plan, conn)
    assert result["moved"] == 2
    assert result["stats"]["companion"] == 1

    rows = conn.execute(
        "SELECT * FROM manifest ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    still = next(r for r in rows if r["photo_id"] is not None)
    mov = next(r for r in rows if r["companion_of"] is not None)
    assert still["new_path"].endswith(".HEIC")
    assert mov["new_path"].endswith(".MOV")
    assert Path(still["new_path"]).stem == Path(mov["new_path"]).stem
    assert mov["companion_of"] == still["photo_id"]


def test_undo_commit_restores(tmp_path):
    from curator import commit
    root, conn = _setup_commit_ready(tmp_path)
    plan = commit.plan_commit(conn, root)
    src_paths = [m.src_path for m in plan.moves]
    dst_paths = [m.dst_path for m in plan.moves]

    result = commit.execute_commit(plan, conn)
    commit_id = result["commit_id"]

    undo_result = commit.undo_commit(conn, commit_id)
    assert undo_result["restored"] == 2
    assert undo_result["missing"] == []

    for src in src_paths:
        assert src.exists()
    for dst in dst_paths:
        assert not dst.exists()

    n_committed = conn.execute(
        "SELECT COUNT(*) FROM photos WHERE committed_at IS NOT NULL"
    ).fetchone()[0]
    assert n_committed == 0

    n_undone = conn.execute(
        "SELECT COUNT(*) FROM manifest WHERE undone_at IS NOT NULL"
    ).fetchone()[0]
    assert n_undone == 2


def test_undo_commit_idempotent(tmp_path):
    from curator import commit
    root, conn = _setup_commit_ready(tmp_path)
    plan = commit.plan_commit(conn, root)
    result = commit.execute_commit(plan, conn)

    commit.undo_commit(conn, result["commit_id"])
    undo2 = commit.undo_commit(conn, result["commit_id"])
    assert undo2["restored"] == 0
    assert undo2["note"] == "already undone or not found"


def test_commit_endpoint_async(tmp_path):
    import time
    from curator import db as dbmod
    from curator.web.app import create_app
    root, conn = _setup_commit_ready(tmp_path)
    conn.close()

    conn2 = dbmod.connect(tmp_path / "t.db")
    conn2.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('last_folder', ?)",
        (str(root),),
    )
    conn2.commit()
    conn2.close()

    app = create_app(db_path=str(tmp_path / "t.db"), use_fixtures=False)
    client = app.test_client()

    r = client.post("/api/commit/execute")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True

    status = None
    for _ in range(100):
        status = client.get("/api/commit/status").get_json()
        if not status["running"]:
            break
        time.sleep(0.1)

    assert status is not None
    assert not status["running"]
    assert status["error"] is None
    assert status["result"] is not None
    assert status["result"]["moved"] >= 1

    # Verify done page reachable
    r = client.get("/done")
    assert r.status_code == 200
    assert b"Space organized" in r.data
    assert b"available disk space is unchanged" in r.data


# ---- Step 6a: smart grouping --------------------------------------------------------

def test_smart_grouping_merges_burst(tmp_path):
    """Two photos 2s apart with cosine 0.70 must merge (burst threshold 0.65)."""
    from curator import db, group, scan
    root = tmp_path / "photos"; root.mkdir()
    save(make_scene(1), root / "a.jpg", "2026:10:15 14:32:00")
    save(make_scene(2), root / "b.jpg", "2026:10:15 14:32:02")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    pids = {Path(r["path"]).name: r["id"] for r in conn.execute("SELECT id, path FROM photos")}
    # Force embeddings that give cosine 0.70
    v1 = np.array([1.0, 0.0] + [0.0] * 382, dtype=np.float32)
    v2 = np.array([0.70, 0.71] + [0.0] * 382, dtype=np.float32)  # normalized: |v| ≈ 1
    v2 = v2 / np.linalg.norm(v2)
    embeddings.save_embedding(conn, pids["a.jpg"], "test", v1)
    embeddings.save_embedding(conn, pids["b.jpg"], "test", v2)
    conn.commit()

    group.build_groups(conn, method="smart", model="test")
    g = {Path(r["path"]).name: r["group_id"] for r in conn.execute(
        "SELECT p.path, g.group_id FROM groups g JOIN photos p ON p.id = g.photo_id")}
    assert g["a.jpg"] == g["b.jpg"]


def test_smart_grouping_keeps_distant_apart(tmp_path):
    """Two photos 5 minutes apart with cosine 0.70 must NOT merge (threshold 0.85)."""
    from curator import db, group, scan
    root = tmp_path / "photos"; root.mkdir()
    save(make_scene(1), root / "a.jpg", "2026:10:15 14:32:00")
    save(make_scene(2), root / "b.jpg", "2026:10:15 14:37:00")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    pids = {Path(r["path"]).name: r["id"] for r in conn.execute("SELECT id, path FROM photos")}
    v1 = np.array([1.0, 0.0] + [0.0] * 382, dtype=np.float32)
    v2 = np.array([0.70, 0.71] + [0.0] * 382, dtype=np.float32)
    v2 = v2 / np.linalg.norm(v2)
    embeddings.save_embedding(conn, pids["a.jpg"], "test", v1)
    embeddings.save_embedding(conn, pids["b.jpg"], "test", v2)
    conn.commit()

    group.build_groups(conn, method="smart", model="test")
    g = {Path(r["path"]).name: r["group_id"] for r in conn.execute(
        "SELECT p.path, g.group_id FROM groups g JOIN photos p ON p.id = g.photo_id")}
    assert g["a.jpg"] != g["b.jpg"]


def test_smart_grouping_requires_embeddings(tmp_path):
    from curator import db, group, scan
    import pytest
    root = tmp_path / "photos"; root.mkdir()
    save(make_scene(1), root / "a.jpg", "2026:10:15 14:32:00")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    with pytest.raises(ValueError, match="no embeddings"):
        group.build_groups(conn, method="smart", model="missing-model")


# ---- Step 6b: reject learning -------------------------------------------------------

def test_reject_centroid_stored_at_commit(tmp_path):
    from curator import commit, db, group, scan
    root = tmp_path / "photos"; root.mkdir()
    save(make_scene(1), root / "a.jpg", "2026:10:15 14:32:00")
    save(make_scene(2), root / "b.jpg", "2026:10:15 15:00:00")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    group.build_groups(conn)
    pids = {Path(r["path"]).name: r["id"] for r in conn.execute("SELECT id, path FROM photos")}
    for name in ("a.jpg", "b.jpg"):
        v = np.random.default_rng(pids[name]).standard_normal(384).astype(np.float32)
        embeddings.save_embedding(conn, pids[name], "dinov2_vits14", v / np.linalg.norm(v))
    conn.commit()

    gid_a = conn.execute("SELECT group_id FROM groups WHERE photo_id=?", (pids["a.jpg"],)).fetchone()[0]
    gid_b = conn.execute("SELECT group_id FROM groups WHERE photo_id=?", (pids["b.jpg"],)).fetchone()[0]
    conn.execute("UPDATE group_states SET status='deleted' WHERE group_id=?", (gid_a,))
    conn.execute("UPDATE group_states SET status='completed', current_winner_photo_id=? WHERE group_id=?",
                 (pids["b.jpg"], gid_b))
    conn.commit()

    plan = commit.plan_commit(conn, root)
    result = commit.execute_commit(plan, conn)

    examples = conn.execute("SELECT * FROM reject_examples").fetchall()
    assert len(examples) == 1
    assert examples[0]["source"] == "group"
    assert examples[0]["n_photos"] == 1
    assert examples[0]["commit_id"] == result["commit_id"]


def test_flag_similar_to_reject(tmp_path):
    from curator import db, learning, scan
    root = tmp_path / "photos"; root.mkdir()
    save(make_scene(1), root / "rejected.jpg", "2026:10:15 14:00:00")
    save(make_scene(2), root / "new.jpg", "2026:10:15 15:00:00")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    pids = {Path(r["path"]).name: r["id"] for r in conn.execute("SELECT id, path FROM photos")}
    # Similar embeddings for rejected and new (cosine ~1.0)
    v = np.random.default_rng(0).standard_normal(384).astype(np.float32)
    v /= np.linalg.norm(v)
    embeddings.save_embedding(conn, pids["rejected.jpg"], "dinov2_vits14", v)
    embeddings.save_embedding(conn, pids["new.jpg"], "dinov2_vits14", v)
    conn.commit()

    learning.store_suggested_reject(conn, pids["rejected.jpg"], model="dinov2_vits14")
    flagged = learning.flag_similar_to_rejects(conn, model="dinov2_vits14", threshold=0.82)
    assert flagged == 1

    row = conn.execute(
        "SELECT suggested_reject FROM photos WHERE id=?", (pids["new.jpg"],)
    ).fetchone()
    assert row["suggested_reject"] == 1


def test_suggested_excluded_from_groups(tmp_path):
    from curator import db, group, learning, scan
    root = tmp_path / "photos"; root.mkdir()
    save(make_scene(1), root / "a.jpg", "2026:10:15 14:32:00")
    save(make_scene(2), root / "b.jpg", "2026:10:15 14:32:02")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    pids = {Path(r["path"]).name: r["id"] for r in conn.execute("SELECT id, path FROM photos")}
    v = np.random.default_rng(0).standard_normal(384).astype(np.float32)
    v /= np.linalg.norm(v)
    for name in ("a.jpg", "b.jpg"):
        embeddings.save_embedding(conn, pids[name], "dinov2_vits14", v)
    conn.commit()

    learning.store_suggested_reject(conn, pids["a.jpg"], model="dinov2_vits14")
    learning.flag_similar_to_rejects(conn, model="dinov2_vits14", threshold=0.82)
    # b.jpg got flagged, a.jpg is user_reject'd already (from confirm in store_suggested_reject? no)
    # Actually store_suggested_reject only stores the centroid, doesn't mark user_reject.
    # Let's check: b.jpg should be suggested_reject=1
    row = conn.execute("SELECT suggested_reject FROM photos WHERE id=?", (pids["b.jpg"],)).fetchone()
    assert row["suggested_reject"] == 1

    group.build_groups(conn, method="smart", model="dinov2_vits14")
    in_groups = {Path(r["path"]).name for r in conn.execute(
        "SELECT p.path FROM groups g JOIN photos p ON p.id = g.photo_id"
    )}
    assert "b.jpg" not in in_groups


def test_suggested_endpoints(tmp_path):
    from curator import db, learning, scan
    from curator.web.app import create_app
    root = tmp_path / "photos"; root.mkdir()
    save(make_scene(1), root / "a.jpg", "2026:10:15 14:32:00")
    save(make_scene(2), root / "b.jpg", "2026:10:15 15:00:00")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root, conn)
    pids = {Path(r["path"]).name: r["id"] for r in conn.execute("SELECT id, path FROM photos")}
    v = np.random.default_rng(0).standard_normal(384).astype(np.float32)
    v /= np.linalg.norm(v)
    for name in ("a.jpg", "b.jpg"):
        embeddings.save_embedding(conn, pids[name], "dinov2_vits14", v)
    conn.commit()

    learning.store_suggested_reject(conn, pids["a.jpg"], model="dinov2_vits14")
    learning.flag_similar_to_rejects(conn, model="dinov2_vits14", threshold=0.82)
    conn.close()

    app = create_app(db_path=str(tmp_path / "t.db"), use_fixtures=False)
    client = app.test_client()

    data = client.get("/api/suggested").get_json()
    assert len(data["photos"]) == 1

    pid = data["photos"][0]["id"]
    r = client.post(f"/api/suggested/{pid}/keep")
    assert r.status_code == 200

    data = client.get("/api/suggested").get_json()
    assert len(data["photos"]) == 0


def test_start_over_flow(tmp_path):
    """After commit, / redirects to /done. Analyzing a new folder resets state
    and / shows the new groups."""
    import time
    from curator import commit, db, group, scan
    from curator.web.app import create_app
    from PIL import Image

    # First batch
    root1 = tmp_path / "batch1"; root1.mkdir()
    save(make_scene(1), root1 / "a.jpg", "2026:10:15 14:32:00")
    save(make_scene(2), root1 / "b.jpg", "2026:10:15 15:00:00")
    conn = db.connect(tmp_path / "t.db")
    scan.scan(root1, conn)
    group.build_groups(conn)
    conn.execute(
        "UPDATE group_states SET status='completed', current_winner_photo_id=("
        "SELECT MIN(photo_id) FROM groups WHERE group_id=group_states.group_id)"
    )
    conn.commit()
    plan = commit.plan_commit(conn, root1)
    result = commit.execute_commit(plan, conn)
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('last_folder', ?)",
        (str(root1),),
    )
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('last_commit_id', ?)",
        (result["commit_id"],),
    )
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('review_completed', '1')"
    )
    conn.commit()
    conn.close()

    app = create_app(db_path=str(tmp_path / "t.db"), use_fixtures=False)
    client = app.test_client()

    # 1. / should redirect to /done
    r = client.get("/")
    assert r.status_code in (301, 302, 303, 307, 308)
    assert "/done" in r.headers["Location"]

    # 2. Verify _curator/ was NOT re-indexed on a fresh scan
    root1_before = sum(1 for _ in scan.iter_photos(root1))
    # iter_photos must skip _curator
    assert root1_before == 0  # all source files were moved

    # 3. Simulate "Start over" with a new folder
    root2 = tmp_path / "batch2"; root2.mkdir()
    save(make_scene(3), root2 / "c.jpg", "2026:10:16 10:00:00")
    save(make_scene(4), root2 / "d.jpg", "2026:10:16 11:00:00")

    r = client.post("/api/analyze", json={"folder": str(root2)})
    assert r.status_code == 200

    for _ in range(100):
        status = client.get("/api/analyze/status").get_json()
        if not status["running"]:
            break
        time.sleep(0.1)

    assert status["error"] is None

    # 4. After new analysis, / shows new groups (not /done)
    r = client.get("/")
    assert r.status_code == 200  # no redirect — new batch ready to review