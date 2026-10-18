"""Benchmark harness: which scorer (or, later, which grouper) matches your labels?

Scorer benchmark:
  - For every group with at least one 'winner' label, run every scorer.
  - A hit means the scorer picked a photo labeled 'winner' in that group.
  - Accuracy = hits / groups evaluated.

Built-in scorers for step 2 (measurable only; aesthetics come in step 4):
  - sharpness      : highest variance-of-Laplacian (from step 1)
  - phash_centroid : photo closest (on average) to the others by pHash
  - emb_centroid   : photo closest to the group's embedding centroid
  - random         : uniform baseline (should be ~1/N)
"""
from __future__ import annotations

import random
import uuid
from datetime import datetime
from typing import Callable

import numpy as np

from .embeddings import load_embedding
from .group import hamming


Scorer = Callable[[list[dict], dict], "int | None"]


def scorer_sharpness(rows, ctx):
    valid = [r for r in rows if r.get("sharpness") is not None]
    return max(valid, key=lambda r: r["sharpness"])["id"] if valid else None


def scorer_random(rows, ctx):
    return random.choice(rows)["id"] if rows else None


def scorer_phash_centroid(rows, ctx):
    valid = [r for r in rows if r.get("phash")]
    if len(valid) < 2:
        return valid[0]["id"] if valid else None
    best, best_d = None, None
    for r in valid:
        d = sum(hamming(r["phash"], o["phash"]) for o in valid if o["id"] != r["id"])
        if best_d is None or d < best_d:
            best, best_d = r, d
    return best["id"]


def scorer_emb_centroid(rows, ctx):
    conn, model = ctx["conn"], ctx["model"]
    if not model:
        return None
    ids, vecs = [], []
    for r in rows:
        v = load_embedding(conn, r["id"], model)
        if v is not None:
            ids.append(r["id"])
            vecs.append(v)
    if not ids:
        return None
    M = np.stack(vecs)
    centroid = M.mean(axis=0)
    norm = np.linalg.norm(centroid)
    if norm == 0:
        return None
    centroid /= norm
    return ids[int(np.argmax(M @ centroid))]


BUILTIN_SCORERS: dict[str, Scorer] = {
    "sharpness": scorer_sharpness,
    "phash_centroid": scorer_phash_centroid,
    "random": scorer_random,
}


def default_scorers(model: str | None = None) -> dict[str, Scorer]:
    s = dict(BUILTIN_SCORERS)
    if model:
        s["emb_centroid"] = scorer_emb_centroid
    return s


def run_scorer_benchmark(conn, scorers: dict[str, Scorer], *,
                         model: str | None = None,
                         min_group_size: int = 2) -> dict[str, dict]:
    group_photos: dict[int, list[dict]] = {}
    for r in conn.execute(
        "SELECT g.group_id AS gid, p.* FROM groups g "
        "JOIN photos p ON p.id = g.photo_id "
        "WHERE p.error IS NULL ORDER BY g.group_id, p.id"
    ):
        group_photos.setdefault(r["gid"], []).append(dict(r))

    winners: dict[int, set[int]] = {}
    for r in conn.execute("SELECT group_id, photo_id FROM labels WHERE label = 'winner'"):
        winners.setdefault(r["group_id"], set()).add(r["photo_id"])

    ctx = {"conn": conn, "model": model}
    results = {name: {"hits": 0, "total": 0} for name in scorers}

    for gid, rows in group_photos.items():
        if len(rows) < min_group_size or gid not in winners:
            continue
        winner_ids = winners[gid] & {r["id"] for r in rows}
        if not winner_ids:
            continue
        for name, fn in scorers.items():
            try:
                picked = fn(rows, ctx)
            except Exception:
                picked = None
            results[name]["total"] += 1
            if picked in winner_ids:
                results[name]["hits"] += 1

    for r in results.values():
        r["accuracy"] = (r["hits"] / r["total"]) if r["total"] else 0.0
    return results


def save_benchmark(conn, task: str, results: dict, run_id: str | None = None) -> str:
    run_id = run_id or uuid.uuid4().hex[:8]
    now = datetime.now().isoformat(timespec="seconds")
    rows = [
        (run_id, task, name, r["total"], r["hits"], r["accuracy"], now)
        for name, r in results.items()
    ]
    conn.executemany(
        "INSERT INTO benchmark_runs (run_id, task, method, groups, hits, accuracy, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return run_id