"""Provisional grouping: time sessions + perceptual-hash similarity.

Photos whose time of day is trustworthy (time_known = 1) are split into sessions by time gaps.
Photos with only a trustworthy date (time_known = 0) are not placed in a time session, because
that would rely on a time we do not know: they are compared with every other photo of the same day.

Provisional because pHash catches near-identical shots (bursts, copies) well but is weaker on
the same scene with small changes: step 2 adds embeddings and a benchmark to compare methods.
"""
from __future__ import annotations

import uuid
from datetime import datetime


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


def build_groups(conn, session_gap_min: float = 30.0, max_hamming: int = 10) -> dict:
    rows = conn.execute(
        "SELECT id, taken_at, phash, COALESCE(time_known, 0) AS time_known FROM photos "
        "WHERE error IS NULL AND taken_at IS NOT NULL AND phash IS NOT NULL ORDER BY taken_at, id"
    ).fetchall()
    n = len(rows)

    # 1) sessions for photos with a trustworthy time: a pause longer than session_gap_min
    #    starts a new session.
    sessions: list[list[int]] = []  # lists of row indices
    last_t = None
    for i, r in enumerate(rows):
        if not r["time_known"]:
            continue
        t = datetime.fromisoformat(r["taken_at"])
        if last_t is None or (t - last_t).total_seconds() > session_gap_min * 60:
            sessions.append([])
        sessions[-1].append(i)
        last_t = t

    # 2) photos with only a trustworthy date are compared with every photo of the same day
    unknown_by_day: dict[str, list[int]] = {}
    known_by_day: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        bucket = known_by_day if r["time_known"] else unknown_by_day
        bucket.setdefault(r["taken_at"][:10], []).append(i)

    uf = _UnionFind(n)

    def similar(i: int, j: int) -> bool:
        return hamming(rows[i]["phash"], rows[j]["phash"]) <= max_hamming

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

    # 3) number the groups in order of first appearance and store them
    run_id = uuid.uuid4().hex[:8]
    ids: dict[int, int] = {}
    assignments: list[tuple[int, int, str]] = []
    for i, r in enumerate(rows):
        root = uf.find(i)
        if root not in ids:
            ids[root] = len(ids) + 1
        assignments.append((r["id"], ids[root], run_id))

    conn.execute("DELETE FROM groups")
    conn.executemany("INSERT INTO groups (photo_id, group_id, run_id) VALUES (?,?,?)", assignments)
    conn.commit()

    sizes = conn.execute("SELECT COUNT(*) AS n FROM groups GROUP BY group_id").fetchall()
    multi = [x["n"] for x in sizes if x["n"] > 1]
    return {
        "run_id": run_id,
        "photos": len(assignments),
        "sessions": len(sessions) + len(unknown_by_day),
        "day_buckets": len(unknown_by_day),
        "groups": len(ids),
        "groups_with_multiple": len(multi),
        "photos_in_multi_groups": sum(multi),
    }
