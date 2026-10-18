"""Routes. Real SQLite backend for the review UI + intake + analysis.

Sprint 3b adds:
  - GET  /intake            folder picker
  - GET  /analysis          live checklist while scan+group run
  - POST /api/analyze       start analysis (fixtures mode: simulate)
  - POST /api/analyze/cancel
  - GET  /api/analyze/status
  - POST /api/pick-folder   native macOS folder picker via osascript
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from curator import embeddings as embedmod
from curator import group as groupmod
from curator import ranker as rankermod

from flask import (
    Blueprint, abort, current_app, g, jsonify,
    redirect, render_template, request, send_file, url_for,
)

from curator import db as dbmod
from .fixtures import get_fake_groups
from .progress import ProgressEstimator
from .thumbs import thumb_for

REVIEW_COMPLETED_KEY = "review_completed"

# Bump when the grouping logic or filters change, so previously-analyzed
# folders get re-grouped on the next `/api/analyze` run even if files are unchanged.
GROUPING_VERSION = "5"

bp = Blueprint(
    "web",
    __name__,
    static_folder="static",
    template_folder="templates",
    static_url_path="/static",
)


# ---- Analysis state accessors -------------------------------------------------------
# State lives in app.config so each Flask app (and each test client) is isolated.
# Background threads capture references to the state/stop objects; they must NEVER
# touch `current_app` (no request context there).

def _state() -> dict:
    return current_app.config["_analysis_state"]


def _stop() -> threading.Event:
    return current_app.config["_analysis_stop"]


def _analysis_tasks() -> list[dict]:
    return [
        {
            "id": "scan",
            "label": "Scan photo metadata",
            "description": "Read photo details and detect duplicates.",
            "status": "pending",
            "count": 0,
            "total": 0,
            "eta_seconds": None,
            "started_at": None,
            "finished_at": None,
        },
        {
            "id": "embed",
            "label": "Analyze visual similarities",
            "description": "Prepare visual details for comparing similar shots.",
            "status": "pending",
            "count": 0,
            "total": 0,
            "eta_seconds": None,
            "started_at": None,
            "finished_at": None,
        },
        {
            "id": "group",
            "label": "Build photo groups",
            "description": "Bring similar photos together for your review.",
            "status": "pending",
            "count": 0,
            "total": 0,
            "eta_seconds": None,
            "started_at": None,
            "finished_at": None,
        },
    ]


# ---- DB helpers ---------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = dbmod.connect(current_app.config["DB_PATH"])
    return g.db


def close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _save_setting(db_path: str, key: str, value: str) -> None:
    conn = dbmod.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def _load_setting(db_path: str, key: str) -> str | None:
    conn = dbmod.connect(db_path)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _analysis_fingerprint(conn) -> str:
    rows = conn.execute(
        "SELECT id, sha256, mtime FROM photos WHERE error IS NULL ORDER BY id"
    ).fetchall()
    h = hashlib.sha256()
    h.update(GROUPING_VERSION.encode())
    for r in rows:
        h.update(f"{r['id']}:{r['sha256'] or ''}:{r['mtime']}".encode())
    return h.hexdigest()


# ---- Serialisation ------------------------------------------------------------------

def _photo_url(photo_id: int, mtime: float, size: int, variant: str) -> str:
    return f"/photos/{photo_id}/{variant}?v={mtime}-{size}"


def _photo_payload(row) -> dict:
    return {
        "id": row["id"],
        "thumb": _photo_url(row["id"], row["mtime"], row["size"], "thumb"),
        "url": _photo_url(row["id"], row["mtime"], row["size"], "full"),
        "sharpness": row["sharpness"],
        "brightness": row["brightness"],
        "shadow_clip": row["shadow_clip"],
        "highlight_clip": row["highlight_clip"],
        "width": row["width"],
        "height": row["height"],
        "duplicate_of": row["duplicate_of"],
    }


def _load_group_list(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            gs.group_id,
            gs.status,
            gs.current_winner_photo_id,
            winner.size AS winner_size,
            winner.mtime AS winner_mtime,
            gs.updated_at,
            COUNT(p.id) AS photo_count,
            MIN(p.taken_at) AS taken_at
        FROM group_states gs
        JOIN groups g ON g.group_id = gs.group_id
        JOIN photos p ON p.id = g.photo_id
        LEFT JOIN photos winner ON winner.id = gs.current_winner_photo_id
        WHERE p.error IS NULL
        GROUP BY gs.group_id
        ORDER BY gs.status = 'completed', gs.group_id
        """
    ).fetchall()
    return [
        {
            "id": r["group_id"],
            "status": r["status"],
            "current_winner": r["current_winner_photo_id"],
            "photo_count": r["photo_count"],
            "taken_at": r["taken_at"],
            "thumb": (
                _photo_url(r["current_winner_photo_id"], r["winner_mtime"], r["winner_size"], "thumb")
                if r["current_winner_photo_id"] else None
            ),
        }
        for r in rows
    ]


def _load_group_detail(conn, group_id: int) -> dict | None:
    state = conn.execute(
        "SELECT * FROM group_states WHERE group_id = ?", (group_id,)
    ).fetchone()
    if state is None:
        return None
    photos = conn.execute(
        "SELECT p.* FROM groups g JOIN photos p ON p.id = g.photo_id "
        "WHERE g.group_id = ? AND p.error IS NULL ORDER BY p.id",
        (group_id,),
    ).fetchall()
    result = {
        "id": group_id,
        "status": state["status"],
        "current_winner": state["current_winner_photo_id"],
        "photos": [_photo_payload(p) for p in photos],
    }
    if state["status"] == "pending" and len(photos) > 1:
        result["recommendation"] = rankermod.recommend_keeper(conn, photos)
    else:
        result["recommendation"] = {"status": "not_applicable"}
    return result


# ---- Pages --------------------------------------------------------------------------

@bp.route("/")
def index():
    if _state()["running"]:
        return redirect(url_for("web.analysis"))

    if current_app.config["USE_FIXTURES"]:
        groups = get_fake_groups()
        for gr in groups:
            gr["photo_count"] = len(gr["photos"])
        pending = sum(1 for gr in groups if gr["status"] == "pending")
        return render_template(
            "index.html", groups=groups, pending_count=pending,
            use_fixtures=True, initial_detail=None,
        )

    conn = get_db()
    db_path = current_app.config["DB_PATH"]

    # 1. Just committed and not undone? Show /done.
    if _load_setting(db_path, REVIEW_COMPLETED_KEY) == "1":
        last_commit = _load_setting(db_path, "last_commit_id")
        if last_commit:
            active = conn.execute(
                "SELECT COUNT(*) FROM manifest WHERE commit_id=? AND undone_at IS NULL",
                (last_commit,),
            ).fetchone()[0]
            if active > 0:
                return redirect(url_for("web.done"))
        # Commit was undone or manifest removed: clear flag, fall through
        _save_setting(db_path, REVIEW_COMPLETED_KEY, "0")

    # 2. Normal review flow
    has_groups = conn.execute("SELECT COUNT(*) FROM group_states").fetchone()[0] > 0
    if has_groups:
        groups = _load_group_list(conn)
        pending = sum(1 for gr in groups if gr["status"] == "pending")
        first_pending = next((gr for gr in groups if gr["status"] == "pending"),
                             groups[0] if groups else None)
        detail = _load_group_detail(conn, first_pending["id"]) if first_pending else None

        rejected_count = conn.execute(
            "SELECT COUNT(*) FROM photos WHERE error IS NULL "
            "  AND rejected_reviewed_at IS NULL "
            "  AND COALESCE(is_utility,0)=1 "
            "  AND committed_at IS NULL"
        ).fetchone()[0]

        suggested_count = conn.execute(
            "SELECT COUNT(*) FROM photo_suggestions ps "
            "JOIN photos p ON p.id = ps.photo_id "
            "WHERE ps.dismissed_at IS NULL AND p.error IS NULL "
            "  AND p.committed_at IS NULL"
        ).fetchone()[0]
        rejected_count += suggested_count

        return render_template(
            "index.html", groups=groups, pending_count=pending,
            use_fixtures=False, initial_detail=detail,
            rejected_count=rejected_count,
            suggested_count=suggested_count,
        )

    # 3. No groups: where next?
    # Nothing scanned yet?
    total_photos = conn.execute(
        "SELECT COUNT(*) FROM photos WHERE error IS NULL AND committed_at IS NULL"
    ).fetchone()[0]
    if total_photos == 0:
        return redirect(url_for("web.intake"))

    # Photos exist but no groups: probably all utility → /rejected
    rejected_left = conn.execute(
        "SELECT COUNT(*) FROM photos WHERE error IS NULL AND committed_at IS NULL "
        "  AND rejected_reviewed_at IS NULL AND COALESCE(is_utility,0)=1"
    ).fetchone()[0]
    rejected_left += conn.execute(
        "SELECT COUNT(*) FROM photo_suggestions ps JOIN photos p ON p.id=ps.photo_id "
        "WHERE ps.dismissed_at IS NULL AND p.error IS NULL AND p.committed_at IS NULL"
    ).fetchone()[0]
    if rejected_left > 0:
        return redirect(url_for("web.rejected"))

    # Fallback: intake
    return redirect(url_for("web.intake"))


@bp.route("/intake")
def intake():
    if current_app.config["USE_FIXTURES"]:
        return redirect(url_for("web.index"))
    if _state()["running"]:
        return redirect(url_for("web.analysis"))

    conn = get_db()
    has_groups = conn.execute("SELECT COUNT(*) FROM group_states").fetchone()[0] > 0
    pending = 0
    if has_groups:
        pending = conn.execute(
            "SELECT COUNT(*) FROM group_states WHERE status='pending'"
        ).fetchone()[0]

    review_completed = (
        _load_setting(current_app.config["DB_PATH"], REVIEW_COMPLETED_KEY) == "1"
    )
    last_folder = _load_setting(current_app.config["DB_PATH"], "last_folder") or ""

    return render_template(
        "intake.html",
        has_groups=has_groups,
        pending_count=pending,
        last_folder=last_folder,
        review_completed=review_completed,
    )


@bp.route("/analysis")
def analysis():
    if current_app.config["USE_FIXTURES"]:
        return redirect(url_for("web.index"))
    s = _state()
    if not s["running"] and s["finished_at"] is None:
        return redirect(url_for("web.intake"))
    return render_template("analysis.html")


# ---- Analysis -----------------------------------------------------------------------

def _simulate_analysis(total: int, state: dict, stop: threading.Event) -> dict:
    """Fixtures-mode stub. Same task shape as real analysis so the UI is identical."""
    def run():
        state.clear()
        state.update({
            "running": True,
            "folder": "(simulated)",
            "tasks": _analysis_tasks(),
            "started_at": time.time(), "finished_at": None,
            "error": None, "final": None, "cancelled": False,
        })
        state["tasks"][0].update({
            "status": "running",
            "total": total,
            "started_at": time.time(),
        })
        est = ProgressEstimator(total=total)
        try:
            for i in range(total):
                if stop.is_set():
                    state["cancelled"] = True
                    break
                time.sleep(0.03)
                state["tasks"][0]["count"] = i + 1
                snap = est.update(i + 1)
                state["tasks"][0]["eta_seconds"] = snap["eta_seconds"]
            state["tasks"][0]["status"] = "done"
            state["tasks"][0]["finished_at"] = time.time()
            if state["cancelled"]:
                state["tasks"][1]["status"] = "pending"
                state["final"] = {"mode": "cancelled"}
                return
            state["tasks"][1]["status"] = "running"
            state["tasks"][1].update({
                "started_at": time.time(),
                "total": 1,
            })
            time.sleep(0.3)
            state["tasks"][1]["count"] = 1
            state["tasks"][1]["status"] = "done"
            state["tasks"][1]["finished_at"] = time.time()
            state["tasks"][1]["summary"] = "Visual analysis complete"
            state["tasks"][2]["status"] = "running"
            state["tasks"][2].update({
                "started_at": time.time(),
                "total": 1,
            })
            time.sleep(0.1)
            state["tasks"][2]["count"] = 1
            state["tasks"][2]["status"] = "done"
            state["tasks"][2]["finished_at"] = time.time()
            state["tasks"][2]["summary"] = f"{total // 4} groups ready"
            state["final"] = {"mode": "fresh", "groups": total // 4, "photos": total}
        except Exception as exc:
            state["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            state["running"] = False
            state["finished_at"] = time.time()

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "mode": "simulated"}


def _real_analysis(folder_path: Path, state: dict, stop: threading.Event, db_path: str) -> dict:
    from curator import group as groupmod
    from curator import scan as scanmod
    from curator import embeddings as embedmod

    def run():
        state.clear()
        state.update({
            "running": True,
            "folder": str(folder_path),
            "tasks": _analysis_tasks(),
            "started_at": time.time(), "finished_at": None,
            "error": None, "final": None, "cancelled": False,
        })
        state["tasks"][0].update({
            "status": "running",
            "started_at": time.time(),
        })
        stop.clear()

        conn = None
        try:
            conn = dbmod.connect(db_path)

            total_files = sum(1 for _ in scanmod.iter_photos(folder_path))
            state["tasks"][0]["total"] = total_files
            est = ProgressEstimator(total=max(total_files, 1))

            def on_progress(stats):
                state["tasks"][0]["count"] = stats["seen"]
                snap = est.update(stats["seen"])
                state["tasks"][0]["eta_seconds"] = snap["eta_seconds"]

            scan_stats = scanmod.scan(
                folder_path, conn, progress=on_progress, stop_event=stop
            )
            state["tasks"][0]["count"] = total_files
            state["tasks"][0]["eta_seconds"] = 0
            state["tasks"][0]["finished_at"] = time.time()
            if stop.is_set():
                state["tasks"][0]["status"] = "done"
                state["tasks"][1]["status"] = "pending"
                state["tasks"][2]["status"] = "pending"
                state["cancelled"] = True
                state["final"] = {"mode": "cancelled"}
                return

            state["tasks"][0]["status"] = "done"

            # Task 1: embeddings (optional — torch may not be installed)
            state["tasks"][1].update({
                "status": "running",
                "started_at": time.time(),
            })
            embeddings_available = False
            embed_estimator = None

            def on_embed_progress(stats):
                nonlocal embed_estimator
                task = state["tasks"][1]
                task["total"] = stats["candidates"]
                task["count"] = stats["embedded"]
                if embed_estimator is None and stats["candidates"] > 0:
                    embed_estimator = ProgressEstimator(
                        total=stats["candidates"], min_samples=5
                    )
                if embed_estimator is not None:
                    task["eta_seconds"] = embed_estimator.update(
                        stats["embedded"]
                    )["eta_seconds"]
                task["summary"] = (
                    f"{stats['embedded']} / {stats['candidates']} photos"
                )

            try:
                embed_stats = embedmod.embed_folder(
                    conn, "dinov2_vits14", batch_size=8,
                    progress=on_embed_progress,
                )
                state["tasks"][1]["count"] = embed_stats["embedded"]
                state["tasks"][1]["total"] = embed_stats["candidates"]
                state["tasks"][1]["eta_seconds"] = 0
                state["tasks"][1]["status"] = "done"
                state["tasks"][1]["finished_at"] = time.time()
                state["tasks"][1]["summary"] = (
                    f"{embed_stats['embedded']} embedded"
                    if embed_stats["embedded"] else "already embedded"
                )
                embeddings_available = True
            except ImportError:
                state["tasks"][1]["eta_seconds"] = 0
                state["tasks"][1]["status"] = "done"
                state["tasks"][1]["finished_at"] = time.time()
                state["tasks"][1]["summary"] = "torch not installed — pHash only"
            except Exception as exc:
                state["tasks"][1]["status"] = "error"
                state["tasks"][1]["finished_at"] = time.time()
                state["tasks"][1]["summary"] = f"{type(exc).__name__}: {str(exc)[:50]}"

            # Task 2: grouping
            state["tasks"][2].update({
                "status": "running",
                "started_at": time.time(),
            })
            group_estimator = None

            def on_group_progress(count, total):
                nonlocal group_estimator
                task = state["tasks"][2]
                if group_estimator is None and total > 0:
                    task["total"] = total
                    group_estimator = ProgressEstimator(total=total)
                task["count"] = count
                if group_estimator is not None:
                    task["eta_seconds"] = group_estimator.update(count)["eta_seconds"]
                task["summary"] = f"{count:,} / {total:,} comparisons"

            new_fp = _analysis_fingerprint(conn)
            old_fp = _load_setting(db_path, "grouping_fingerprint")
            has_groups = conn.execute("SELECT COUNT(*) FROM group_states").fetchone()[0] > 0

            if new_fp == old_fp and has_groups and not scan_stats["new"]:
                state["tasks"][2]["count"] = 1
                state["tasks"][2]["total"] = 1
                state["tasks"][2]["eta_seconds"] = 0
                state["tasks"][2]["status"] = "done"
                state["tasks"][2]["finished_at"] = time.time()
                state["tasks"][2]["summary"] = "no changes"
                state["final"] = {"mode": "no_changes"}
            else:
                if embeddings_available:
                    from curator.learning import flag_similar_to_rejects
                    flagged = flag_similar_to_rejects(conn, model="dinov2_vits14")
                else:
                    flagged = 0

                if embeddings_available:
                    try:
                        group_stats = groupmod.build_groups(
                            conn, method="smart", model="dinov2_vits14",
                            progress=on_group_progress,
                        )
                        grouping_method = "smart"
                    except ValueError as exc:
                        state["tasks"][2]["summary"] = f"fallback: {exc}"
                        group_stats = groupmod.build_groups(
                            conn, method="phash", progress=on_group_progress
                        )
                        grouping_method = "phash"
                else:
                    group_stats = groupmod.build_groups(
                        conn, method="phash", progress=on_group_progress
                    )
                    grouping_method = "phash"

                _save_setting(db_path, "grouping_fingerprint", new_fp)
                state["tasks"][2]["count"] = max(
                    state["tasks"][2]["total"], 1
                )
                state["tasks"][2]["total"] = max(
                    state["tasks"][2]["total"], 1
                )
                state["tasks"][2]["eta_seconds"] = 0
                state["tasks"][2]["status"] = "done"
                state["tasks"][2]["finished_at"] = time.time()
                state["tasks"][2]["summary"] = (
                    f"{group_stats['groups']} groups ({grouping_method})"
                )
                state["final"] = {
                    "mode": "fresh",
                    "photos": scan_stats["new"] + scan_stats["updated"] + scan_stats["skipped"],
                    "new_photos": scan_stats["new"],
                    "groups": group_stats["groups"],
                    "groups_with_multiple": group_stats["groups_with_multiple"],
                    "suggested": flagged,
                    "grouping_method": grouping_method,
                }
        except Exception as exc:
            state["error"] = f"{type(exc).__name__}: {exc}"
            for t in state["tasks"]:
                if t["status"] == "running":
                    t["status"] = "error"
                    t["finished_at"] = time.time()
        finally:
            if conn is not None:
                conn.close()
            state["running"] = False
            state["finished_at"] = time.time()

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "mode": "real", "folder": str(folder_path)}


@bp.route("/api/analyze", methods=["POST"])
def api_analyze():
    s = _state()
    if s["running"]:
        return jsonify({"error": "already running"}), 409

    data = request.get_json(silent=True) or {}

    if current_app.config["USE_FIXTURES"]:
        total = int(data.get("total", 318))
        return jsonify(_simulate_analysis(total, s, _stop()))

    folder = data.get("folder")
    if not folder or not str(folder).strip():
        return jsonify({"error": "folder required"}), 400
    folder_path = Path(folder).expanduser().resolve()
    if not folder_path.is_dir():
        return jsonify({"error": f"not a directory: {folder_path}"}), 400

    db_path = current_app.config["DB_PATH"]
    _save_setting(db_path, "last_folder", str(folder_path))

    db_path = current_app.config["DB_PATH"]
    _save_setting(db_path, "last_folder", str(folder_path))
    _save_setting(db_path, REVIEW_COMPLETED_KEY, "0")       # ← nuova riga
    return jsonify(_real_analysis(folder_path, s, _stop(), db_path))


@bp.route("/api/analyze/cancel", methods=["POST"])
def api_analyze_cancel():
    if not _state()["running"]:
        return jsonify({"ok": True, "note": "not running"})
    _stop().set()
    return jsonify({"ok": True})


@bp.route("/api/analyze/status")
def api_analyze_status():
    return jsonify(_state())


# ---- Native folder picker -----------------------------------------------------------

@bp.route("/api/pick-folder", methods=["POST"])
def api_pick_folder():
    if sys.platform != "darwin":
        return jsonify({"error": "native picker only on macOS"}), 400
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'POSIX path of (choose folder with prompt "Choose a folder of photos")'],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"cancelled": True})
    if result.returncode != 0 or not result.stdout.strip():
        return jsonify({"cancelled": True})
    path = result.stdout.strip().rstrip("/")
    return jsonify({"path": path})


# ---- Groups API ---------------------------------------------------------------------

@bp.route("/api/groups")
def api_groups():
    if current_app.config["USE_FIXTURES"]:
        return jsonify(get_fake_groups())
    return jsonify(_load_group_list(get_db()))


@bp.route("/api/groups/<int:group_id>")
def api_group_detail(group_id: int):
    if current_app.config["USE_FIXTURES"]:
        groups = get_fake_groups()
        gr = next((gr for gr in groups if gr["id"] == group_id), None)
        if gr is None:
            abort(404)
        return jsonify(gr)
    detail = _load_group_detail(get_db(), group_id)
    if detail is None:
        abort(404)
    return jsonify(detail)


def _sync_winner_label(conn, group_id: int, photo_id: int) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("DELETE FROM labels WHERE group_id = ? AND label = 'winner'", (group_id,))
    conn.execute(
        "INSERT INTO labels (group_id, photo_id, label, reason, created_at) "
        "VALUES (?, ?, 'winner', NULL, ?)",
        (group_id, photo_id, now),
    )


def _record_user_feedback(conn, photo_id: int, source: str, label: str, previous) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO user_feedback "
        "(photo_id, source, label, previous_is_utility, previous_duplicate_of, "
        "previous_reviewed_at, previous_user_reject, previous_suggested_reject, "
        "previous_dismissed_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            photo_id, source, label, previous["is_utility"], previous["duplicate_of"],
            previous["rejected_reviewed_at"], previous["user_reject"],
            previous["suggested_reject"], previous["dismissed_at"], now,
        ),
    )
    return int(cur.lastrowid)


def _feedback_photo(conn, photo_id: int):
    return conn.execute(
        "SELECT p.id, p.is_utility, p.duplicate_of, p.rejected_reviewed_at, "
        "p.user_reject, p.suggested_reject, ps.dismissed_at, "
        "CASE WHEN ps.photo_id IS NULL THEN 0 ELSE 1 END AS has_suggestion "
        "FROM photos p LEFT JOIN photo_suggestions ps ON ps.photo_id = p.id "
        "WHERE p.id = ?",
        (photo_id,),
    ).fetchone()


@bp.route("/api/groups/<int:group_id>/choose", methods=["POST"])
def api_choose(group_id: int):
    data = request.get_json(silent=True) or {}
    photo_id = data.get("photo_id")
    if photo_id is None:
        return jsonify({"error": "photo_id required"}), 400
    if current_app.config["USE_FIXTURES"]:
        return jsonify({"ok": True, "group_id": group_id,
                        "photo_id": photo_id, "status": "pending"})
    conn = get_db()
    state = conn.execute(
        "SELECT status FROM group_states WHERE group_id = ?", (group_id,)
    ).fetchone()
    if state is None:
        return jsonify({"error": "group no longer exists"}), 404
    if state["status"] == "deleted":
        return jsonify({"error": "group is removed from review"}), 409
    belongs = conn.execute(
        "SELECT 1 FROM groups WHERE group_id = ? AND photo_id = ?", (group_id, photo_id)
    ).fetchone()
    if belongs is None:
        return jsonify({"error": "photo not in group"}), 400
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE group_states SET current_winner_photo_id = ?, updated_at = ?, "
        "decided_at = NULL, status = 'pending' WHERE group_id = ?",
        (photo_id, now, group_id),
    )
    _sync_winner_label(conn, group_id, photo_id)
    conn.commit()
    return jsonify({"ok": True, "group_id": group_id,
                    "photo_id": photo_id, "status": "pending"})


@bp.route("/api/groups/<int:group_id>/confirm", methods=["POST"])
def api_confirm(group_id: int):
    if current_app.config["USE_FIXTURES"]:
        return jsonify({"ok": True, "group_id": group_id, "status": "completed"})
    conn = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT current_winner_photo_id FROM group_states WHERE group_id = ?", (group_id,)
    ).fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404
    status = conn.execute(
        "SELECT status FROM group_states WHERE group_id = ?", (group_id,)
    ).fetchone()
    if status["status"] == "deleted":
        return jsonify({"error": "restore this group before confirming it"}), 409
    if row["current_winner_photo_id"] is None:
        return jsonify({"error": "no winner chosen"}), 400
    members = [
        r["photo_id"] for r in conn.execute(
            "SELECT photo_id FROM groups WHERE group_id = ? ORDER BY photo_id",
            (group_id,),
        )
    ]
    state = conn.execute(
        "SELECT group_key FROM group_states WHERE group_id = ?", (group_id,)
    ).fetchone()
    key = state["group_key"] if state and state["group_key"] else rankermod.group_key(members)
    photos = conn.execute(
        "SELECT p.* FROM groups g JOIN photos p ON p.id = g.photo_id "
        "WHERE g.group_id = ? AND p.error IS NULL ORDER BY p.id",
        (group_id,),
    ).fetchall()
    recommendation = rankermod.recommend_keeper(conn, photos) if len(photos) > 1 else {}
    ai_assisted = int(recommendation.get("photo_id") == row["current_winner_photo_id"])
    conn.execute(
        "UPDATE group_states SET status = 'completed', decided_at = ?, updated_at = ? "
        "WHERE group_id = ?",
        (now, now, group_id),
    )
    conn.execute(
        "INSERT INTO group_feedback "
        "(group_key, photo_ids, winner_photo_id, ai_assisted, active, created_at) "
        "VALUES (?, ?, ?, ?, 1, ?) "
        "ON CONFLICT(group_key) DO UPDATE SET photo_ids=excluded.photo_ids, "
        "winner_photo_id=excluded.winner_photo_id, ai_assisted=excluded.ai_assisted, "
        "active=1, created_at=excluded.created_at",
        (key, json.dumps(members), row["current_winner_photo_id"], ai_assisted, now),
    )
    conn.commit()
    return jsonify({"ok": True, "group_id": group_id, "status": "completed"})


@bp.route("/api/groups/<int:group_id>/delete", methods=["POST"])
def api_delete(group_id: int):
    if current_app.config["USE_FIXTURES"]:
        return jsonify({"ok": True, "group_id": group_id, "status": "deleted"})
    conn = get_db()
    state = conn.execute(
        "SELECT status FROM group_states WHERE group_id = ?", (group_id,)
    ).fetchone()
    if state is None:
        return jsonify({"error": "group no longer exists"}), 404
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE group_states SET status = 'deleted', decided_at = ?, updated_at = ? "
        "WHERE group_id = ?",
        (now, now, group_id),
    )
    key_row = conn.execute(
        "SELECT group_key FROM group_states WHERE group_id = ?", (group_id,)
    ).fetchone()
    if key_row and key_row["group_key"]:
        conn.execute("UPDATE group_feedback SET active = 0 WHERE group_key = ?",
                     (key_row["group_key"],))
    conn.execute("DELETE FROM labels WHERE group_id = ? AND label = 'winner'", (group_id,))
    conn.commit()
    return jsonify({"ok": True, "group_id": group_id, "status": "deleted"})


@bp.route("/api/groups/<int:group_id>/restore", methods=["POST"])
def api_restore(group_id: int):
    if current_app.config["USE_FIXTURES"]:
        return jsonify({"ok": True, "group_id": group_id, "status": "pending"})
    conn = get_db()
    state = conn.execute(
        "SELECT status FROM group_states WHERE group_id = ?", (group_id,)
    ).fetchone()
    if state is None:
        return jsonify({"error": "group no longer exists"}), 404
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE group_states SET status = 'pending', decided_at = NULL, updated_at = ? "
        "WHERE group_id = ?",
        (now, group_id),
    )
    key_row = conn.execute(
        "SELECT group_key FROM group_states WHERE group_id = ?", (group_id,)
    ).fetchone()
    if key_row and key_row["group_key"]:
        conn.execute("UPDATE group_feedback SET active = 0 WHERE group_key = ?",
                     (key_row["group_key"],))
    conn.execute("DELETE FROM labels WHERE group_id = ? AND label = 'winner'", (group_id,))
    conn.commit()
    return jsonify({"ok": True, "group_id": group_id, "status": "pending"})


# ---- Rejected recovery --------------------------------------------------------------


def _load_rejected(conn, unreviewed_only: bool = True) -> list[dict]:
    where = "p.error IS NULL AND COALESCE(p.is_utility,0)=1"
    if unreviewed_only:
        where += " AND p.rejected_reviewed_at IS NULL"
    rows = conn.execute(f"SELECT p.* FROM photos p WHERE {where}",).fetchall()
    result = [
        {
            "id": r["id"],
            "thumb": _photo_url(r["id"], r["mtime"], r["size"], "thumb"),
            "url": _photo_url(r["id"], r["mtime"], r["size"], "full"),
            "reason": "Screenshot",
            "reason_detail": None,
            "source": "system",
            "taken_at": r["taken_at"],
        }
        for r in rows
    ]
    suggested_where = "ps.dismissed_at IS NULL AND p.error IS NULL"
    if unreviewed_only:
        suggested_where += " AND p.committed_at IS NULL"
    suggestions = conn.execute(
        "SELECT p.id, p.size, p.mtime, p.taken_at "
        "FROM photo_suggestions ps JOIN photos p ON p.id = ps.photo_id "
        f"WHERE {suggested_where} ORDER BY ps.similarity DESC"
    ).fetchall()
    suggestion_order = rankermod.rank_explicit_discard_candidates(
        conn, [r["id"] for r in suggestions]
    )
    if suggestion_order["status"] == "ready":
        positions = {
            photo_id: index
            for index, photo_id in enumerate(suggestion_order["photo_ids"])
        }
        suggestions = sorted(suggestions, key=lambda row: positions.get(row["id"], len(positions)))
    suggestion_detail = (
        "A visual match, prioritized by your explicit keep/reject choices. Not a verdict."
        if suggestion_order["status"] == "ready"
        else "A visual match, not a prediction of your decision."
    )
    result.extend(
        {
            "id": r["id"],
            "thumb": _photo_url(r["id"], r["mtime"], r["size"], "thumb"),
            "url": _photo_url(r["id"], r["mtime"], r["size"], "full"),
            "reason": "Similar to past rejects",
            "reason_detail": suggestion_detail,
            "source": "suggested",
            "taken_at": r["taken_at"],
        }
        for r in suggestions
    )
    unique = {}
    for photo in result:
        unique[photo["id"]] = photo
    return list(unique.values())


def _rejected_progress(conn) -> tuple[int, int]:
    candidates = {
        row[0] for row in conn.execute(
            "SELECT id FROM photos WHERE COALESCE(is_utility,0)=1 "
            "UNION SELECT photo_id FROM photo_suggestions "
            "UNION SELECT photo_id FROM user_feedback"
        )
    }
    reviewed = {
        row[0] for row in conn.execute(
            "SELECT id FROM photos WHERE rejected_reviewed_at IS NOT NULL "
            "UNION SELECT photo_id FROM photo_suggestions WHERE dismissed_at IS NOT NULL "
            "UNION SELECT photo_id FROM user_feedback WHERE active=1"
        )
    }
    return len(candidates), len(candidates & reviewed)


@bp.route("/rejected")
def rejected():
    if current_app.config["USE_FIXTURES"]:
        return redirect(url_for("web.index"))

    conn = get_db()
    remaining = len(_load_rejected(conn, unreviewed_only=True))
    return render_template("rejected.html", remaining=remaining)


@bp.route("/api/rejected")
def api_rejected():
    if current_app.config["USE_FIXTURES"]:
        return jsonify({"photos": [], "reviewed": 0, "total": 0})
    conn = get_db()
    photos = _load_rejected(conn, unreviewed_only=True)
    total, reviewed = _rejected_progress(conn)
    return jsonify({
        "photos": photos, "reviewed": reviewed, "total": total,
    })


@bp.route("/api/rejected/<int:photo_id>/keep", methods=["POST"])
def api_rejected_keep(photo_id: int):
    if current_app.config["USE_FIXTURES"]:
        return jsonify({"ok": True})
    conn = get_db()
    previous = _feedback_photo(conn, photo_id)
    if previous is None:
        return jsonify({"error": "not found"}), 404
    now = datetime.now().isoformat(timespec="seconds")
    if previous["has_suggestion"] and previous["dismissed_at"] is None:
        from curator.learning import dismiss_suggestion
        dismiss_suggestion(conn, photo_id, commit=False)
        feedback_id = _record_user_feedback(conn, photo_id, "suggested", "keep", previous)
        conn.commit()
        return jsonify({"ok": True, "photo_id": photo_id, "action": "keep",
                        "feedback_id": feedback_id})
    conn.execute(
        "UPDATE photos SET is_utility = 0, duplicate_of = NULL, "
        "rejected_reviewed_at = ? WHERE id = ?",
        (now, photo_id),
    )
    feedback_id = _record_user_feedback(conn, photo_id, "system", "keep", previous)
    conn.commit()
    return jsonify({"ok": True, "photo_id": photo_id, "action": "keep",
                    "feedback_id": feedback_id})


@bp.route("/api/rejected/<int:photo_id>/reject", methods=["POST"])
def api_rejected_reject(photo_id: int):
    if current_app.config["USE_FIXTURES"]:
        return jsonify({"ok": True})
    conn = get_db()
    previous = _feedback_photo(conn, photo_id)
    if previous is None:
        return jsonify({"error": "not found"}), 404
    if previous["has_suggestion"] and previous["dismissed_at"] is None:
        from curator.learning import confirm_suggestion
        confirm_suggestion(conn, photo_id, commit=False)
        feedback_id = _record_user_feedback(conn, photo_id, "suggested", "reject", previous)
        conn.commit()
        return jsonify({"ok": True, "photo_id": photo_id, "action": "reject",
                        "feedback_id": feedback_id})
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE photos SET rejected_reviewed_at = ? WHERE id = ?",
        (now, photo_id),
    )
    feedback_id = _record_user_feedback(conn, photo_id, "system", "reject", previous)
    conn.commit()
    return jsonify({"ok": True, "photo_id": photo_id, "action": "reject",
                    "feedback_id": feedback_id})


@bp.route("/api/rejected/accept-all", methods=["POST"])
def api_rejected_accept_all():
    if current_app.config["USE_FIXTURES"]:
        return jsonify({"ok": True, "count": 0})
    conn = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    rows = _load_rejected(conn, unreviewed_only=True)
    feedback_ids = []
    for row in rows:
        photo_id = row["id"]
        previous = _feedback_photo(conn, photo_id)
        if row["source"] == "suggested":
            from curator.learning import confirm_suggestion
            confirm_suggestion(conn, photo_id, commit=False)
        else:
            conn.execute(
                "UPDATE photos SET rejected_reviewed_at = ? WHERE id = ?",
                (now, photo_id),
            )
        feedback_ids.append(
            _record_user_feedback(conn, photo_id, row["source"], "reject", previous)
        )
    conn.commit()
    return jsonify({"ok": True, "count": len(rows), "feedback_ids": feedback_ids})


@bp.route("/api/feedback/<int:feedback_id>/undo", methods=["POST"])
def api_feedback_undo(feedback_id: int):
    if current_app.config["USE_FIXTURES"]:
        return jsonify({"ok": True})
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM user_feedback WHERE id = ? AND active = 1", (feedback_id,)
    ).fetchone()
    if row is None:
        return jsonify({"error": "feedback action not found"}), 404
    conn.execute(
        "UPDATE photos SET is_utility = ?, duplicate_of = ?, rejected_reviewed_at = ?, "
        "user_reject = ?, suggested_reject = ? WHERE id = ?",
        (
            row["previous_is_utility"], row["previous_duplicate_of"],
            row["previous_reviewed_at"], row["previous_user_reject"],
            row["previous_suggested_reject"], row["photo_id"],
        ),
    )
    conn.execute(
        "UPDATE photo_suggestions SET dismissed_at = ? WHERE photo_id = ?",
        (row["previous_dismissed_at"], row["photo_id"]),
    )
    conn.execute("UPDATE user_feedback SET active = 0 WHERE id = ?", (feedback_id,))
    conn.commit()
    return jsonify({"ok": True, "photo_id": row["photo_id"]})


# ---- Confirm (commit preview) -------------------------------------------------------

@bp.route("/confirm")
def confirm():
    if current_app.config["USE_FIXTURES"]:
        return redirect(url_for("web.index"))
    source_root = _load_setting(current_app.config["DB_PATH"], "last_folder") or ""
    curator_root = str(Path(source_root) / "_curator") if source_root else ""
    return render_template(
        "confirm.html",
        source_root=source_root,
        curator_root=curator_root,
    )


@bp.route("/api/commit/preview", methods=["POST"])
def api_commit_preview():
    if current_app.config["USE_FIXTURES"]:
        return jsonify({"stats": {}, "errors": [], "sample": [], "has_errors": False})
    from curator import commit as commitmod

    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid file-name settings."}), 400

    source_root_str = _load_setting(current_app.config["DB_PATH"], "last_folder")
    if not source_root_str:
        return jsonify({"error": "No source folder. Run intake first."}), 400
    source_root = Path(source_root_str)
    if not source_root.is_dir():
        return jsonify({"error": f"Source folder not found: {source_root}"}), 400

    try:
        plan = commitmod.plan_commit(
            get_db(), source_root, batch_name=payload.get("batch_name")
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(commitmod.plan_to_dict(plan))


# ---- Commit execution ---------------------------------------------------------------

def _commit_state() -> dict:
    return current_app.config["_commit_state"]


@bp.route("/api/commit/execute", methods=["POST"])
def api_commit_execute():
    if current_app.config["USE_FIXTURES"]:
        return jsonify({"ok": True, "mode": "fixtures"})

    from curator import commit as commitmod

    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid file-name settings."}), 400
    batch_name = payload.get("batch_name")
    if batch_name is not None:
        try:
            batch_name = commitmod.normalize_batch_name(batch_name)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    state = _commit_state()
    if state["running"]:
        return jsonify({"error": "already running"}), 409

    source_root_str = _load_setting(current_app.config["DB_PATH"], "last_folder")
    if not source_root_str:
        return jsonify({"error": "no source folder. Run intake first."}), 400
    source_root = Path(source_root_str)
    if not source_root.is_dir():
        return jsonify({"error": f"source folder not found: {source_root}"}), 400

    db_path = current_app.config["DB_PATH"]
    threading.Thread(
        target=_run_commit, args=(db_path, source_root, state, batch_name), daemon=True
    ).start()
    return jsonify({"ok": True})


def _run_commit(
    db_path: str, source_root: Path, state: dict, batch_name: str | None = None
) -> None:
    from curator import commit as commitmod
    state.clear()
    state.update({
        "running": True, "progress": None, "result": None, "error": None,
        "started_at": time.time(), "finished_at": None,
    })
    conn = None
    try:
        conn = dbmod.connect(db_path)
        plan = commitmod.plan_commit(conn, source_root, batch_name=batch_name)
        if plan.has_errors:
            state["error"] = "Cannot commit: " + "; ".join(plan.errors)
            return
        if not plan.moves:
            state["error"] = "Nothing to commit."
            return

        est = ProgressEstimator(total=len(plan.moves))

        def on_progress(done: int, total: int) -> None:
            snap = est.update(done)
            state["progress"] = {
                "count": done, "total": total,
                "eta_label": snap["eta_label"],
            }

        result = commitmod.execute_commit(plan, conn, progress=on_progress)
        _save_setting(db_path, "last_commit_id", result["commit_id"])
        _save_setting(db_path, REVIEW_COMPLETED_KEY, "1")   # ← nuova riga
        state["result"] = result
    except Exception as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            conn.close()
        state["running"] = False
        state["finished_at"] = time.time()


@bp.route("/api/commit/status")
def api_commit_status():
    return jsonify(_commit_state())


@bp.route("/api/commit/undo", methods=["POST"])
def api_commit_undo():
    if current_app.config["USE_FIXTURES"]:
        return jsonify({"ok": True, "restored": 0, "missing": []})
    from curator import commit as commitmod
    commit_id = _load_setting(current_app.config["DB_PATH"], "last_commit_id")
    if not commit_id:
        return jsonify({"error": "no commit to undo"}), 400
    try:
        result = commitmod.undo_commit(get_db(), commit_id)
        _save_setting(current_app.config["DB_PATH"], REVIEW_COMPLETED_KEY, "0")
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
    return jsonify(result)


# ---- Done page ----------------------------------------------------------------------

@bp.route("/done")
def done():
    if current_app.config["USE_FIXTURES"]:
        return redirect(url_for("web.index"))
    conn = get_db()
    commit_id = _load_setting(current_app.config["DB_PATH"], "last_commit_id")
    if not commit_id:
        return redirect(url_for("web.intake"))

    has_files = conn.execute(
        "SELECT 1 FROM manifest WHERE commit_id = ? AND undone_at IS NULL LIMIT 1",
        (commit_id,),
    ).fetchone()
    if not has_files:
        # Everything was undone; nothing to show
        return redirect(url_for("web.intake"))

    return render_template("done.html")


@bp.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    if sys.platform != "darwin":
        return jsonify({"error": "native open only on macOS"}), 400
    source = _load_setting(current_app.config["DB_PATH"], "last_folder")
    if not source:
        return jsonify({"error": "no source folder"}), 400
    target = Path(source) / "_curator" / "winner"
    if not target.exists():
        target = Path(source) / "_curator"
    if not target.exists():
        return jsonify({"error": "output folder not found"}), 404
    subprocess.run(["open", str(target)], check=False)
    return jsonify({"ok": True})


# ---- Suggested rejects (learning) ---------------------------------------------------

@bp.route("/suggested")
def suggested():
    return redirect(url_for("web.rejected"))


@bp.route("/api/suggested")
def api_suggested():
    if current_app.config["USE_FIXTURES"]:
        return jsonify({"photos": [], "total": 0})
    conn = get_db()
    rows = conn.execute(
        "SELECT p.id, p.size, p.mtime, ps.similarity "
        "FROM photo_suggestions ps JOIN photos p ON p.id = ps.photo_id "
        "WHERE ps.dismissed_at IS NULL AND p.error IS NULL "
        "ORDER BY ps.similarity DESC"
    ).fetchall()
    return jsonify({
        "photos": [
            {
                "id": r["id"],
                "thumb": _photo_url(r["id"], r["mtime"], r["size"], "thumb"),
                "url": _photo_url(r["id"], r["mtime"], r["size"], "full"),
                "similarity": round(float(r["similarity"]), 3),
            }
            for r in rows
        ],
        "total": len(rows),
    })


@bp.route("/api/suggested/<int:photo_id>/keep", methods=["POST"])
def api_suggested_keep(photo_id: int):
    if current_app.config["USE_FIXTURES"]:
        return jsonify({"ok": True})
    from curator.learning import dismiss_suggestion
    conn = get_db()
    previous = _feedback_photo(conn, photo_id)
    if (
        previous is None
        or not previous["has_suggestion"]
        or previous["dismissed_at"] is not None
    ):
        return jsonify({"error": "not found"}), 404
    if not dismiss_suggestion(conn, photo_id, commit=False):
        return jsonify({"error": "suggestion no longer exists"}), 404
    feedback_id = _record_user_feedback(conn, photo_id, "suggested", "keep", previous)
    conn.commit()
    return jsonify({"ok": True, "action": "keep", "feedback_id": feedback_id})


@bp.route("/api/suggested/<int:photo_id>/reject", methods=["POST"])
def api_suggested_reject(photo_id: int):
    if current_app.config["USE_FIXTURES"]:
        return jsonify({"ok": True})
    from curator.learning import confirm_suggestion
    conn = get_db()
    previous = _feedback_photo(conn, photo_id)
    if (
        previous is None
        or not previous["has_suggestion"]
        or previous["dismissed_at"] is not None
    ):
        return jsonify({"error": "not found"}), 404
    confirm_suggestion(conn, photo_id, commit=False)
    feedback_id = _record_user_feedback(conn, photo_id, "suggested", "reject", previous)
    conn.commit()
    return jsonify({"ok": True, "action": "reject", "feedback_id": feedback_id})


@bp.route("/api/suggested/accept-all", methods=["POST"])
def api_suggested_accept_all():
    if current_app.config["USE_FIXTURES"]:
        return jsonify({"ok": True, "count": 0})
    from curator.learning import confirm_suggestion
    conn = get_db()
    photos = conn.execute(
        "SELECT photo_id FROM photo_suggestions WHERE dismissed_at IS NULL"
    ).fetchall()
    feedback_ids = []
    for (pid,) in photos:
        previous = _feedback_photo(conn, pid)
        confirm_suggestion(conn, pid, commit=False)
        feedback_ids.append(
            _record_user_feedback(conn, pid, "suggested", "reject", previous)
        )
    conn.commit()
    return jsonify({"ok": True, "count": len(photos), "feedback_ids": feedback_ids})


# ---- Photos -------------------------------------------------------------------------

@bp.route("/photos/<int:photo_id>/thumb")
def photo_thumb(photo_id: int):
    conn = get_db()
    row = conn.execute("SELECT path, mtime, size FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if row is None:
        abort(404)
    try:
        p = thumb_for(row["path"], row["mtime"], row["size"])
    except Exception:
        abort(404)
    return send_file(p, mimetype="image/jpeg", max_age=3600)


@bp.route("/photos/<int:photo_id>/full")
def photo_full(photo_id: int):
    conn = get_db()
    row = conn.execute("SELECT path FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if row is None:
        abort(404)
    p = Path(row["path"])
    if not p.exists():
        abort(404)
    return send_file(p, max_age=3600)