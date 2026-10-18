from __future__ import annotations

import json

from curator import db, ranker


def _photo(conn, name: str, *, sharpness: float, brightness: float = 0.5) -> int:
    cur = conn.execute(
        "INSERT INTO photos (path, size, mtime, width, height, sharpness, brightness, "
        "shadow_clip, highlight_clip, taken_at, scanned_at) "
        "VALUES (?, 1, 0, 1200, 800, ?, ?, 0, 0, '2026-10-01T10:00:00', "
        "'2026-10-01T10:00:00')",
        (f"/fake/{name}.jpg", sharpness, brightness),
    )
    return int(cur.lastrowid)


def test_keeper_ranker_learns_from_confirmed_pairwise_choices(tmp_path):
    conn = db.connect(tmp_path / "ranker.db")
    for group_num in range(3):
        winner = _photo(conn, f"winner-{group_num}", sharpness=1000 + group_num * 10)
        alternative = _photo(conn, f"alternative-{group_num}", sharpness=20 + group_num)
        ids = sorted((winner, alternative))
        conn.execute(
            "INSERT INTO group_feedback "
            "(group_key, photo_ids, winner_photo_id, active, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (
                ranker.group_key(ids),
                json.dumps(ids),
                winner,
                f"2026-10-0{group_num + 1}T10:00:00",
            ),
        )
    conn.commit()

    high = _photo(conn, "candidate-high", sharpness=1400)
    low = _photo(conn, "candidate-low", sharpness=2)
    candidate_rows = conn.execute(
        "SELECT id, width, height, sharpness, brightness, shadow_clip, highlight_clip "
        "FROM photos WHERE id IN (?, ?) ORDER BY id",
        (high, low),
    ).fetchall()

    result = ranker.recommend_keeper(conn, candidate_rows)
    assert result["status"] == "ready"
    assert result["photo_id"] == high


def test_explicit_discard_model_uses_only_explicit_keep_reject_feedback(tmp_path):
    conn = db.connect(tmp_path / "discard.db")
    rejects = [
        _photo(conn, f"reject-{i}", sharpness=100, brightness=0.1)
        for i in range(3)
    ]
    keeps = [
        _photo(conn, f"keep-{i}", sharpness=100, brightness=0.9)
        for i in range(3)
    ]
    for photo_id in rejects:
        conn.execute(
            "INSERT INTO user_feedback (photo_id, source, label, active, created_at) "
            "VALUES (?, 'suggested', 'reject', 1, '2026-10-01T10:00:00')",
            (photo_id,),
        )
    for photo_id in keeps:
        conn.execute(
            "INSERT INTO user_feedback (photo_id, source, label, active, created_at) "
            "VALUES (?, 'system', 'keep', 1, '2026-10-01T10:00:00')",
            (photo_id,),
        )
    conn.commit()

    candidate_reject = _photo(conn, "candidate-reject", sharpness=100, brightness=0.05)
    candidate_keep = _photo(conn, "candidate-keep", sharpness=100, brightness=0.95)
    ranked = ranker.rank_explicit_discard_candidates(
        conn, [candidate_keep, candidate_reject]
    )
    assert ranked["status"] == "ready"
    assert ranked["photo_ids"] == [candidate_reject, candidate_keep]


def test_ranker_stays_cold_without_enough_feedback(tmp_path):
    conn = db.connect(tmp_path / "cold.db")
    winner = _photo(conn, "winner", sharpness=500)
    alternative = _photo(conn, "alternative", sharpness=20)
    ids = sorted((winner, alternative))
    conn.execute(
        "INSERT INTO group_feedback (group_key, photo_ids, winner_photo_id, created_at) "
        "VALUES (?, ?, ?, '2026-10-01T10:00:00')",
        (ranker.group_key(ids), json.dumps(ids), winner),
    )
    conn.commit()
    candidates = conn.execute(
        "SELECT id, width, height, sharpness, brightness, shadow_clip, highlight_clip "
        "FROM photos WHERE id IN (?, ?)",
        (winner, alternative),
    ).fetchall()
    assert ranker.recommend_keeper(conn, candidates)["status"] == "insufficient_feedback"
