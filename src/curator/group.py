"""Provisional grouping: time sessions + similarity (pHash and/or embeddings).

Methods:
  - "phash"      : Hamming distance on the 64-bit perceptual hash (step 1 behaviour).
  - "embeddings" : cosine similarity of DINOv2 / CLIP vectors. Better on the same scene
                   with small changes (bursts, minor framing, small lighting shifts).
  - "combined"   : group if pHash says "near-identical" OR embeddings say "same scene".
                   Best of both; slightly slower than "phash".
"""
from __future__ import annotations

import uuid
from datetime import datetime

from .embeddings import load_embedding


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def compute_assignments(conn, *, session_gap_min: float = 30.0,
                        method: str = "phash", model: str | None = None,
                        max_hamming: int = 10, cosine_threshold: float = 0.20):
    """Return ([(photo_id, group_id), ...], meta) without writing to the groups table."""
    if method not in ("phash", "embeddings", "combined"):
        raise ValueError(f"Unknown method {method!r}; use phash/embeddings/combined")
    if method in ("embeddings", "combined") and not model:
        raise ValueError(f"method={method!r} requires model=")

    rows = conn.execute(
        "SELECT id, taken_at, phash, COALESCE(time_known, 0) AS time_known FROM photos "
        "WHERE error IS NULL AND taken_at IS NOT NULL "
        "  AND COALESCE(is_utility, 0) = 0 "
        "ORDER BY taken_at, id"
    ).fetchall()
    n = len(rows)

    vecs = {}
    if method in ("embeddings", "combined"):
        for r in rows:
            v = load_embedding(conn, r["id"], model)
            if v is not None:
                vecs[r["id"]] = v

    def similar(i: int, j: int) -> bool:
        a, b = rows[i], rows[j]
        phash_ok = (
            a["phash"] and b["phash"]
            and hamming(a["phash"], b["phash"]) <= max_hamming
        )
        if method == "phash":
            return bool(phash_ok)
        va, vb = vecs.get(a["id"]), vecs.get(b["id"])
        emb_ok = (
            va is not None and vb is not None
            and float(va @ vb) >= (1.0 - cosine_threshold)
        )
        if method == "embeddings":
            return emb_ok
        return bool(phash_ok or emb_ok)

    sessions: list[list[int]] = []
    last_t = None
    for i, r in enumerate(rows):
        if not r["time_known"]:
            continue
        t = datetime.fromisoformat(r["taken_at"])
        if last_t is None or (t - last_t).total_seconds() > session_gap_min * 60:
            sessions.append([])
        sessions[-1].append(i)
        last_t = t

    unknown_by_day: dict[str, list[int]] = {}
    known_by_day: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        bucket = known_by_day if r["time_known"] else unknown_by_day
        bucket.setdefault(r["taken_at"][:10], []).append(i)

    uf = _UnionFind(n)
    for sess in sessions:
        for a in range(len(sess)):
            for b in range(a + 1, len(sess)):
                if similar(sess[a], sess[b]):
                    uf.union(sess[a], sess[b])
    for day, unknown in unknown_by_day.items():
        for i in unknown:
            for j in unknown + known_by_day.get(day, []):
                if j != i and similar(i, j):
                    uf.union(i, j)

    ids: dict[int, int] = {}
    assignments: list[tuple[int, int]] = []
    for i, r in enumerate(rows):
        root = uf.find(i)
        if root not in ids:
            ids[root] = len(ids) + 1
        assignments.append((r["id"], ids[root]))

    sizes = [0] * len(ids)
    for _, gid in assignments:
        sizes[gid - 1] += 1
    multi = [s for s in sizes if s > 1]
    meta = {
        "photos": len(assignments),
        "sessions": len(sessions) + len(unknown_by_day),
        "day_buckets": len(unknown_by_day),
        "groups": len(ids),
        "groups_with_multiple": len(multi),
        "photos_in_multi_groups": sum(multi),
    }
    return assignments, meta


def build_groups(conn, *, session_gap_min: float = 30.0,
                 method: str = "phash", model: str | None = None,
                 max_hamming: int = 10, cosine_threshold: float = 0.20) -> dict:
    """Group by session and similarity, then persist to `groups` + `group_states`."""
    assignments, meta = compute_assignments(
        conn,
        session_gap_min=session_gap_min,
        method=method,
        model=model,
        max_hamming=max_hamming,
        cosine_threshold=cosine_threshold,
    )
    run_id = uuid.uuid4().hex[:8]
    conn.execute("DELETE FROM groups")
    conn.executemany(
        "INSERT INTO groups (photo_id, group_id, run_id) VALUES (?,?,?)",
        [(pid, gid, run_id) for pid, gid in assignments],
    )

    # Step 3: rebuild group_states. Regenerating groups invalidates prior decisions.
    # `assignments` is already ordered by (taken_at, id), so the first entry per
    # group_id is the deterministic default winner.
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("DELETE FROM group_states")
    first_by_group: dict[int, int] = {}
    for pid, gid in assignments:
        first_by_group.setdefault(gid, pid)
    conn.executemany(
        "INSERT INTO group_states (group_id, status, current_winner_photo_id, updated_at) "
        "VALUES (?, 'pending', ?, ?)",
        [(gid, pid, now) for gid, pid in sorted(first_by_group.items())],
    )
    conn.commit()
    return {"run_id": run_id, **meta}