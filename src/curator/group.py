"""Provisional grouping: time sessions + similarity (pHash and/or embeddings).

Methods:
  - "phash"      : Hamming distance on the 64-bit perceptual hash (step 1 behaviour).
  - "embeddings" : cosine similarity of DINOv2 / CLIP vectors. Better on the same scene
                   with small changes (bursts, minor framing, small lighting shifts).
  - "combined"   : group if pHash says "near-identical" OR embeddings say "same scene".
                   Best of both; slightly slower than "phash".
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import uuid

from .embeddings import load_embedding
from .ranker import group_key


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))
        self.anchor = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union_if_anchors_match(
        self, a: int, b: int, similar: Callable[[int, int], bool],
    ) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        anchor_a, anchor_b = self.anchor[ra], self.anchor[rb]
        if not similar(anchor_a, anchor_b):
            return
        self.parent[rb] = ra


def _time_adaptive_cosine_threshold(dt_seconds: float) -> float:
    """Cosine threshold that adapts to the time gap between two photos.

    Empirical tuning for DINOv2 ViT-S/14:
      - same scene, small movement: cosine 0.75-0.90
      - same subject, different angle: cosine 0.65-0.80
      - different scene: cosine < 0.55

    Within a burst (<5s), "one step back" is exactly what we want to group,
    so the threshold is permissive. Minutes apart, we need very high cosine
    to be confident it's still the same scene.
    """
    if dt_seconds <= 2:    return 0.65
    if dt_seconds <= 5:    return 0.68
    if dt_seconds <= 15:   return 0.72
    if dt_seconds <= 60:   return 0.78
    if dt_seconds <= 300:  return 0.85
    return 0.90


def compute_assignments(conn, *, session_gap_min: float = 30.0,
                        method: str = "phash", model: str | None = None,
                        max_hamming: int = 10, cosine_threshold: float = 0.20,
                        progress: Callable[[int, int], None] | None = None):
    """Return ([(photo_id, group_id), ...], meta) without writing to groups table.

    Methods:
      - "phash"      : Hamming distance only
      - "embeddings" : static cosine threshold
      - "combined"   : pHash OR embeddings (static thresholds)
      - "smart"      : time-adaptive cosine + exact-pHash override (recommended)
    """
    if method not in ("phash", "embeddings", "combined", "smart"):
        raise ValueError(f"Unknown method {method!r}")
    if method in ("embeddings", "combined", "smart") and not model:
        raise ValueError(f"method={method!r} requires model=")

    if method == "smart":
        n_emb = conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE model = ?", (model,)
        ).fetchone()[0]
        if n_emb == 0:
            raise ValueError(
                f"no embeddings for model '{model}'. Run 'curator embed' first."
            )

    rows = conn.execute(
        "SELECT id, taken_at, phash, COALESCE(time_known, 0) AS time_known FROM photos "
        "WHERE error IS NULL AND taken_at IS NOT NULL "
        "  AND COALESCE(is_utility, 0) = 0 "
        "  AND COALESCE(suggested_reject, 0) = 0 "
        "  AND COALESCE(user_reject, 0) = 0 "
        "ORDER BY taken_at, id"
    ).fetchall()
    n = len(rows)

    vecs = {}
    if method in ("embeddings", "combined", "smart"):
        for r in rows:
            v = load_embedding(conn, r["id"], model)
            if v is not None:
                vecs[r["id"]] = v

    def similar(i: int, j: int) -> bool:
        a, b = rows[i], rows[j]

        if method == "smart":
            # 1. Exact-duplicate pHash override: always merge
            if a["phash"] and b["phash"] and hamming(a["phash"], b["phash"]) <= 4:
                return True
            # 2. Time-adaptive cosine
            va, vb = vecs.get(a["id"]), vecs.get(b["id"])
            if va is None or vb is None:
                return False
            t1 = datetime.fromisoformat(a["taken_at"])
            t2 = datetime.fromisoformat(b["taken_at"])
            dt = abs((t1 - t2).total_seconds())
            return float(va @ vb) >= _time_adaptive_cosine_threshold(dt)

        phash_ok = (a["phash"] and b["phash"]
                    and hamming(a["phash"], b["phash"]) <= max_hamming)
        if method == "phash":
            return bool(phash_ok)
        va, vb = vecs.get(a["id"]), vecs.get(b["id"])
        emb_ok = (va is not None and vb is not None
                  and float(va @ vb) >= (1.0 - cosine_threshold))
        if method == "embeddings":
            return emb_ok
        return bool(phash_ok or emb_ok)

    # Time sessions (unchanged)
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

    total_pairs = sum(len(sess) * (len(sess) - 1) // 2 for sess in sessions)
    total_pairs += sum(
        len(unknown) * (len(unknown) + len(known_by_day.get(day, [])) - 1)
        for day, unknown in unknown_by_day.items()
    )
    compared = 0
    progress_step = max(1, total_pairs // 100)
    if progress:
        progress(0, total_pairs)

    uf = _UnionFind(n)
    for sess in sessions:
        for a in range(len(sess)):
            for b in range(a + 1, len(sess)):
                if similar(sess[a], sess[b]):
                    uf.union_if_anchors_match(sess[a], sess[b], similar)
                compared += 1
                if progress and (
                    compared % progress_step == 0 or compared == total_pairs
                ):
                    progress(compared, total_pairs)
    for day, unknown in unknown_by_day.items():
        for i in unknown:
            for j in unknown + known_by_day.get(day, []):
                if j != i:
                    if similar(i, j):
                        uf.union_if_anchors_match(i, j, similar)
                    compared += 1
                    if progress and (
                        compared % progress_step == 0 or compared == total_pairs
                    ):
                        progress(compared, total_pairs)

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
                 max_hamming: int = 10, cosine_threshold: float = 0.20,
                 progress: Callable[[int, int], None] | None = None) -> dict:
    """Group by session and similarity, then persist to `groups` + `group_states`."""
    assignments, meta = compute_assignments(
        conn,
        session_gap_min=session_gap_min,
        method=method,
        model=model,
        max_hamming=max_hamming,
        cosine_threshold=cosine_threshold,
        progress=progress,
    )
    run_id = uuid.uuid4().hex[:8]
    previous_states = {}
    previous_rows = conn.execute(
        "SELECT gs.group_id, gs.status, gs.current_winner_photo_id, "
        "GROUP_CONCAT(g.photo_id) AS photo_ids "
        "FROM group_states gs JOIN groups g ON g.group_id = gs.group_id "
        "GROUP BY gs.group_id"
    ).fetchall()
    for row in previous_rows:
        ids = [int(pid) for pid in row["photo_ids"].split(",")]
        previous_states[group_key(ids)] = dict(row)

    conn.execute("DELETE FROM groups")
    conn.executemany(
        "INSERT INTO groups (photo_id, group_id, run_id) VALUES (?,?,?)",
        [(pid, gid, run_id) for pid, gid in assignments],
    )

    # Keep decisions only when the exact same set of photo IDs remains together.
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("DELETE FROM group_states")
    first_by_group: dict[int, int] = {}
    ids_by_group: dict[int, list[int]] = {}
    for pid, gid in assignments:
        first_by_group.setdefault(gid, pid)
        ids_by_group.setdefault(gid, []).append(pid)
    state_rows = []
    for gid, default_winner in sorted(first_by_group.items()):
        ids = ids_by_group[gid]
        key = group_key(ids)
        previous = previous_states.get(key)
        winner = (
            previous["current_winner_photo_id"]
            if previous and previous["current_winner_photo_id"] in ids
            else default_winner
        )
        status = previous["status"] if previous else "pending"
        state_rows.append((gid, key, status, winner, now))
    conn.executemany(
        "INSERT INTO group_states "
        "(group_id, group_key, status, current_winner_photo_id, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        state_rows,
    )
    conn.commit()
    return {"run_id": run_id, **meta}