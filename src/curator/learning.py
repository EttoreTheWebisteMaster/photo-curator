"""Personal-taste learning: store what the user rejects, flag what looks similar.

Design decisions:
  - Store ONE centroid per rejected group (not per photo), to keep the table tiny.
  - Threshold is deliberately conservative (0.82) — we'd rather miss a bad photo
    than annoy the user by flagging a good one.
  - Flagged photos are excluded from groups, but they appear in /suggested where
    the user can review, keep, or reject them quickly.
  - Utility rejects (screenshots) are NOT stored as reject examples — they teach
    the model "screenshot-like", not "user's taste", which are different signals.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np

from .embeddings import load_embedding

DEFAULT_THRESHOLD = 0.82


def store_reject_centroids(conn, commit_id: str, model: str = "dinov2_vits14") -> int:
    """After a commit, store a centroid for every group the user deleted.

    Called from commit.execute_commit() on success. Skips groups without embeddings,
    and groups already stored for this commit_id.
    """
    existing = {r[0] for r in conn.execute(
        "SELECT source FROM reject_examples WHERE commit_id=?", (commit_id,)
    )}
    if "group" in existing:
        return 0

    deleted_groups = conn.execute(
        "SELECT gs.group_id FROM group_states gs WHERE gs.status='deleted'"
    ).fetchall()

    stored = 0
    now = datetime.now().isoformat(timespec="seconds")
    for (gid,) in deleted_groups:
        vecs: list[np.ndarray] = []
        for (pid,) in conn.execute(
            "SELECT photo_id FROM groups WHERE group_id=?", (gid,)
        ):
            v = load_embedding(conn, pid, model)
            if v is not None:
                vecs.append(v)
        if not vecs:
            continue
        centroid = np.mean(vecs, axis=0)
        norm = float(np.linalg.norm(centroid))
        if norm == 0:
            continue
        centroid = (centroid / norm).astype(np.float32)
        conn.execute(
            "INSERT INTO reject_examples "
            "(commit_id, source, model, dim, vector, n_photos, created_at) "
            "VALUES (?, 'group', ?, ?, ?, ?, ?)",
            (commit_id, model, len(centroid), centroid.tobytes(), len(vecs), now),
        )
        stored += 1
    conn.commit()
    return stored


def store_suggested_reject(conn, photo_id: int, model: str = "dinov2_vits14") -> bool:
    """Store a confirmed rejection as a reject example.

    Also marks the photo as user_reject=1 so it does not get re-flagged as a
    candidate next time (it's the source of the example, not a new match).
    """
    v = load_embedding(conn, photo_id, model)
    if v is None:
        return False
    v = (v / np.linalg.norm(v)).astype(np.float32)
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO reject_examples "
        "(commit_id, source, model, dim, vector, n_photos, created_at) "
        "VALUES (NULL, 'suggested', ?, ?, ?, 1, ?)",
        (model, len(v), v.tobytes(), now),
    )
    conn.execute(
        "UPDATE photos SET user_reject = 1, rejected_reviewed_at = ? WHERE id = ?",
        (now, photo_id),
    )
    conn.commit()
    return True


def flag_similar_to_rejects(conn, model: str = "dinov2_vits14",
                            threshold: float = DEFAULT_THRESHOLD) -> int:
    """Flag photos whose embedding is close to any stored reject centroid.

    Photos already flagged, user_reject, is_utility, or committed are skipped.
    Returns count of newly flagged photos.
    """
    examples = conn.execute(
        "SELECT id, dim, vector FROM reject_examples WHERE model=?", (model,)
    ).fetchall()
    if not examples:
        return 0

    ex_ids: list[int] = []
    ex_vecs: list[np.ndarray] = []
    for ex in examples:
        ex_ids.append(ex["id"])
        ex_vecs.append(np.frombuffer(ex["vector"], dtype=np.float32, count=ex["dim"]).copy())
    matrix = np.stack(ex_vecs)  # (n_examples, dim)

    candidates = conn.execute(
        "SELECT p.id FROM photos p "
        "LEFT JOIN photo_suggestions ps ON ps.photo_id = p.id "
        "WHERE p.error IS NULL "
        "  AND ps.photo_id IS NULL "
        "  AND COALESCE(p.is_utility, 0) = 0 "
        "  AND COALESCE(p.user_reject, 0) = 0 "
        "  AND COALESCE(p.suggested_reject, 0) = 0 "
        "  AND p.committed_at IS NULL"
    ).fetchall()

    now = datetime.now().isoformat(timespec="seconds")
    flagged = 0
    for (pid,) in candidates:
        v = load_embedding(conn, pid, model)
        if v is None:
            continue
        sims = matrix @ v
        idx = int(np.argmax(sims))
        best = float(sims[idx])
        if best >= threshold:
            conn.execute(
                "INSERT INTO photo_suggestions "
                "(photo_id, example_id, similarity, computed_at) "
                "VALUES (?, ?, ?, ?)",
                (pid, ex_ids[idx], best, now),
            )
            conn.execute(
                "UPDATE photos SET suggested_reject = 1 WHERE id = ?", (pid,)
            )
            flagged += 1
    conn.commit()
    return flagged


def dismiss_suggestion(conn, photo_id: int) -> bool:
    """User disagrees with the suggestion: photo goes back into groups next analysis."""
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "UPDATE photo_suggestions SET dismissed_at = ? WHERE photo_id = ?",
        (now, photo_id),
    )
    conn.execute("UPDATE photos SET suggested_reject = 0 WHERE id = ?", (photo_id,))
    conn.commit()
    return cur.rowcount > 0


def confirm_suggestion(conn, photo_id: int, model: str = "dinov2_vits14") -> bool:
    """User agrees: mark user_reject, store photo as a reject example."""
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE photos SET user_reject = 1, rejected_reviewed_at = ? WHERE id = ?",
        (now, photo_id),
    )
    conn.execute(
        "UPDATE photo_suggestions SET dismissed_at = ? WHERE photo_id = ?",
        (now, photo_id),
    )
    conn.commit()
    store_suggested_reject(conn, photo_id, model)
    return True