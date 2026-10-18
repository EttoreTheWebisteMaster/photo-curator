"""Local, explainable preference models trained only on explicit choices."""
from __future__ import annotations

import hashlib
import json
import math

import numpy as np

FEATURES = (
    "log_sharpness",
    "brightness",
    "shadow_clip",
    "highlight_clip",
    "log_pixels",
    "aspect_ratio",
)
MIN_KEEPER_GROUPS = 3
MIN_EXPLICIT_LABELS = 3


def group_key(photo_ids) -> str:
    members = ",".join(str(pid) for pid in sorted(photo_ids))
    return hashlib.sha256(members.encode("ascii")).hexdigest()


def photo_features(row) -> np.ndarray:
    width, height = row["width"], row["height"]
    sharpness = row["sharpness"]
    values = (
        math.log1p(max(float(sharpness), 0.0)) if sharpness is not None else np.nan,
        float(row["brightness"]) if row["brightness"] is not None else np.nan,
        float(row["shadow_clip"]) if row["shadow_clip"] is not None else np.nan,
        float(row["highlight_clip"]) if row["highlight_clip"] is not None else np.nan,
        math.log1p(max(int(width) * int(height), 0)) if width and height else np.nan,
        float(width) / height if width and height else np.nan,
    )
    return np.asarray(values, dtype=np.float64)


def _standardizer(rows: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    matrix = np.stack(rows)
    medians = np.nanmedian(matrix, axis=0)
    medians = np.where(np.isfinite(medians), medians, 0.0)
    filled = np.where(np.isfinite(matrix), matrix, medians)
    scales = filled.std(axis=0)
    scales = np.where(scales > 1e-8, scales, 1.0)
    return medians, scales


def _fit_logistic(x: np.ndarray, y: np.ndarray, sample_weight=None,
                  *, iterations: int = 800, regularization: float = 0.2) -> np.ndarray:
    if sample_weight is None:
        sample_weight = np.ones(len(y), dtype=np.float64)
    sample_weight = sample_weight / sample_weight.sum()
    weights = np.zeros(x.shape[1], dtype=np.float64)
    for _ in range(iterations):
        logits = np.clip(x @ weights, -30.0, 30.0)
        probabilities = 1.0 / (1.0 + np.exp(-logits))
        gradient = x.T @ (sample_weight * (probabilities - y))
        gradient[1:] += regularization * weights[1:]
        weights -= 0.08 * gradient
    return weights


def fit_keeper_ranker(conn) -> dict:
    """Fit pairwise logistic preferences; each reviewed group contributes equal weight."""
    feedback = conn.execute(
        "SELECT photo_ids, winner_photo_id FROM group_feedback "
        "WHERE active = 1 ORDER BY created_at, group_key"
    ).fetchall()
    groups = []
    member_ids = set()
    for record in feedback:
        ids = [int(pid) for pid in json.loads(record["photo_ids"])]
        member_ids.update(ids)
    vectors: dict[int, np.ndarray] = {}
    sorted_ids = sorted(member_ids)
    for start in range(0, len(sorted_ids), 900):
        batch = sorted_ids[start:start + 900]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT id, width, height, sharpness, brightness, shadow_clip, "
            f"highlight_clip FROM photos WHERE id IN ({placeholders})",
            batch,
        ).fetchall()
        vectors.update({row["id"]: photo_features(row) for row in rows})

    for record in feedback:
        ids = [int(pid) for pid in json.loads(record["photo_ids"])]
        winner = int(record["winner_photo_id"])
        if winner not in ids or len(ids) < 2:
            continue
        if all(pid in vectors for pid in ids):
            groups.append((ids, winner))

    group_count = len(groups)
    if group_count < MIN_KEEPER_GROUPS:
        return {"status": "insufficient_feedback", "groups": group_count}

    medians, scales = _standardizer([vectors[pid] for ids, _ in groups for pid in ids])
    pairs, weights = [], []
    for ids, winner in groups:
        alternatives = [pid for pid in ids if pid != winner]
        group_weight = 1.0 / len(alternatives)
        preferred = (np.where(np.isfinite(vectors[winner]), vectors[winner], medians) - medians) / scales
        for other in alternatives:
            alternative = (np.where(np.isfinite(vectors[other]), vectors[other], medians) - medians) / scales
            difference = preferred - alternative
            pairs.extend((np.r_[1.0, difference], np.r_[1.0, -difference]))
            weights.extend((group_weight / 2.0, group_weight / 2.0))

    coefficients = _fit_logistic(
        np.asarray(pairs), np.tile([1.0, 0.0], len(pairs) // 2),
        np.asarray(weights),
    )
    if float(np.abs(coefficients[1:]).sum()) < 1e-6:
        return {"status": "insufficient_signal", "groups": group_count}
    return {
        "status": "ready",
        "groups": group_count,
        "medians": medians,
        "scales": scales,
        "coefficients": coefficients,
    }


def recommend_keeper(conn, photo_rows: list) -> dict:
    """Return a ranked suggestion, never a user decision or automatic selection."""
    model = fit_keeper_ranker(conn)
    if model["status"] != "ready":
        return model
    features = np.stack([photo_features(row) for row in photo_rows])
    features = np.where(np.isfinite(features), features, model["medians"])
    features = (features - model["medians"]) / model["scales"]
    scores = features @ model["coefficients"][1:]
    ordered = np.argsort(-scores)
    return {
        "status": "ready",
        "photo_id": int(photo_rows[int(ordered[0])]["id"]),
        "groups": model["groups"],
        "margin": float(scores[ordered[0]] - scores[ordered[1]]) if len(ordered) > 1 else None,
    }


def explicit_discard_status(conn) -> dict:
    """Report whether explicit keep/reject history can train a separate model."""
    counts = {
        row["label"]: row["n"]
        for row in conn.execute(
            "SELECT label, COUNT(*) AS n FROM user_feedback "
            "WHERE active = 1 GROUP BY label"
        )
    }
    keeps, rejects = counts.get("keep", 0), counts.get("reject", 0)
    return {
        "status": "ready" if min(keeps, rejects) >= MIN_EXPLICIT_LABELS else "insufficient_feedback",
        "keeps": keeps,
        "rejects": rejects,
    }


def fit_explicit_discard_model(conn) -> dict:
    """Fit a distinct regularized binary model; it does not make culling decisions."""
    status = explicit_discard_status(conn)
    if status["status"] != "ready":
        return status
    rows = conn.execute(
        "SELECT p.id, p.width, p.height, p.sharpness, p.brightness, "
        "p.shadow_clip, p.highlight_clip, f.label "
        "FROM user_feedback f JOIN photos p ON p.id = f.photo_id "
        "WHERE f.active = 1 ORDER BY f.id"
    ).fetchall()
    vectors = [photo_features(row) for row in rows]
    medians, scales = _standardizer(vectors)
    matrix = np.stack([
        (np.where(np.isfinite(vector), vector, medians) - medians) / scales
        for vector in vectors
    ])
    design = np.column_stack((np.ones(len(rows)), matrix))
    labels = np.asarray([row["label"] == "reject" for row in rows], dtype=np.float64)
    return {
        **status,
        "medians": medians,
        "scales": scales,
        "coefficients": _fit_logistic(design, labels),
    }


def rank_explicit_discard_candidates(conn, photo_ids: list[int]) -> dict:
    """Prioritize existing reject suggestions without turning scores into decisions."""
    model = fit_explicit_discard_model(conn)
    if model["status"] != "ready" or not photo_ids:
        return {"status": model["status"], "photo_ids": photo_ids}
    placeholders = ",".join("?" for _ in photo_ids)
    rows = conn.execute(
        f"SELECT id, width, height, sharpness, brightness, shadow_clip, highlight_clip "
        f"FROM photos WHERE id IN ({placeholders})",
        photo_ids,
    ).fetchall()
    vectors = {row["id"]: photo_features(row) for row in rows}
    scores = {}
    for photo_id in photo_ids:
        vector = vectors.get(photo_id)
        if vector is None:
            continue
        normalized = (
            np.where(np.isfinite(vector), vector, model["medians"]) - model["medians"]
        ) / model["scales"]
        scores[photo_id] = float(model["coefficients"][0] + normalized @ model["coefficients"][1:])
    ordered = sorted(photo_ids, key=lambda pid: scores.get(pid, float("-inf")), reverse=True)
    return {"status": "ready", "photo_ids": ordered}
