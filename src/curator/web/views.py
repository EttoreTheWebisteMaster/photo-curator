"""Routes. Real SQLite backend for the review UI.

- Reads: /api/groups (light list), /api/groups/<id> (detail with photos).
- Writes: choose/confirm/delete/restore on group_states, mirrored to labels
  (so the benchmark keeps working unchanged).
- Photos: /photos/<id>/thumb (cached 800px) and /photos/<id>/full (original).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from flask import (
    Blueprint, abort, current_app, g, jsonify,
    render_template, request, send_file,
)

from curator import db as dbmod
from .fixtures import get_fake_groups
from .progress import ProgressEstimator
from .thumbs import thumb_for

bp = Blueprint(
    "web",
    __name__,
    static_folder="static",
    template_folder="templates",
    static_url_path="/static",
)

_analysis_state = {
    "running": False, "progress": None, "started_at": None,
    "finished_at": None, "error": None, "current_task": None,
}


def get_db():
    if "db" not in g:
        g.db = dbmod.connect(current_app.config["DB_PATH"])
    return g.db


def close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


# ---- Serialisation ------------------------------------------------------------------

def _thumb_url(photo_id: int) -> str:
    return f"/photos/{photo_id}/thumb"


def _full_url(photo_id: int) -> str:
    return f"/photos/{photo_id}/full"


def _photo_payload(row) -> dict:
    return {
        "id": row["id"],
        "thumb": _thumb_url(row["id"]),
        "url": _full_url(row["id"]),
        "sharpness": row["sharpness"] or 0.0,
        "exposure": 0.85,  # TODO: real exposure from step 3
    }


def _load_group_list(conn) -> list[dict]:
    """Light list for the sidebar: one row per group, no per-photo payload."""
    rows = conn.execute(
        """
        SELECT
            gs.group_id,
            gs.status,
            gs.current_winner_photo_id,
            gs.updated_at,
            COUNT(p.id) AS photo_count,
            MIN(p.taken_at) AS taken_at
        FROM group_states gs
        JOIN groups g ON g.group_id = gs.group_id
        JOIN photos p ON p.id = g.photo_id
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
            "thumb": _thumb_url(r["current_winner_photo_id"]) if r["current_winner_photo_id"] else None,
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
    return {
        "id": group_id,
        "status": state["status"],
        "current_winner": state["current_winner_photo_id"],
        "photos": [_photo_payload(p) for p in photos],
    }


# ---- Pages --------------------------------------------------------------------------

@bp.route("/")
def index():
    if current_app.config["USE_FIXTURES"]:
        groups = get_fake_groups()
        for g in groups:
            g["photo_count"] = len(g["photos"])
        pending = sum(1 for g in groups if g["status"] == "pending")
        # Fixtures already carry photos; serve everything server-side as before.
        return render_template(
            "index.html", groups=groups, pending_count=pending,
            use_fixtures=True, initial_detail=None,
        )

    conn = get_db()
    groups = _load_group_list(conn)
    pending = sum(1 for g in groups if g["status"] == "pending")
    first_pending = next((g for g in groups if g["status"] == "pending"), groups[0] if groups else None)
    detail = _load_group_detail(conn, first_pending["id"]) if first_pending else None
    return render_template(
        "index.html", groups=groups, pending_count=pending,
        use_fixtures=False, initial_detail=detail,
    )


# ---- API: groups ---------------------------------------------------------------------

@bp.route("/api/groups")
def api_groups():
    if current_app.config["USE_FIXTURES"]:
        return jsonify(get_fake_groups())
    return jsonify(_load_group_list(get_db()))


@bp.route("/api/groups/<int:group_id>")
def api_group_detail(group_id: int):
    detail = _load_group_detail(get_db(), group_id)
    if detail is None:
        abort(404)
    return jsonify(detail)


def _sync_winner_label(conn, group_id: int, photo_id: int) -> None:
    """Mirror the chosen winner into `labels` so `curator benchmark` keeps working."""
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("DELETE FROM labels WHERE group_id = ? AND label = 'winner'", (group_id,))
    conn.execute(
        "INSERT INTO labels (group_id, photo_id, label, reason, created_at) "
        "VALUES (?, ?, 'winner', NULL, ?)",
        (group_id, photo_id, now),
    )


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
    if row["current_winner_photo_id"] is None:
        return jsonify({"error": "no winner chosen"}), 400
    conn.execute(
        "UPDATE group_states SET status = 'completed', decided_at = ?, updated_at = ? "
        "WHERE group_id = ?",
        (now, now, group_id),
    )
    conn.commit()
    return jsonify({"ok": True, "group_id": group_id, "status": "completed"})


@bp.route("/api/groups/<int:group_id>/delete", methods=["POST"])
def api_delete(group_id: int):
    if current_app.config["USE_FIXTURES"]:
        return jsonify({"ok": True, "group_id": group_id, "status": "deleted"})
    conn = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE group_states SET status = 'deleted', decided_at = ?, updated_at = ? "
        "WHERE group_id = ?",
        (now, now, group_id),
    )
    conn.execute("DELETE FROM labels WHERE group_id = ? AND label = 'winner'", (group_id,))
    conn.commit()
    return jsonify({"ok": True, "group_id": group_id, "status": "deleted"})


@bp.route("/api/groups/<int:group_id>/restore", methods=["POST"])
def api_restore(group_id: int):
    if current_app.config["USE_FIXTURES"]:
        return jsonify({"ok": True, "group_id": group_id, "status": "pending"})
    conn = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE group_states SET status = 'pending', decided_at = NULL, updated_at = ? "
        "WHERE group_id = ?",
        (now, group_id),
    )
    conn.execute("DELETE FROM labels WHERE group_id = ? AND label = 'winner'", (group_id,))
    conn.commit()
    return jsonify({"ok": True, "group_id": group_id, "status": "pending"})


# ---- Photos --------------------------------------------------------------------------

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


# ---- Analysis status (Sprint 1 stub, unchanged) --------------------------------------

@bp.route("/api/analyze/status")
def analyze_status():
    return jsonify(_analysis_state)


@bp.route("/api/analyze/simulate", methods=["POST"])
def analyze_simulate():
    if _analysis_state["running"]:
        return jsonify({"error": "already running"}), 409
    data = request.get_json(silent=True) or {}
    total = int(data.get("total", 318))

    def run():
        _analysis_state.update(
            running=True, progress=None, error=None,
            started_at=time.time(), finished_at=None,
            current_task="Reading metadata",
        )
        try:
            est = ProgressEstimator(total=total)
            for i in range(total):
                time.sleep(0.03)
                _analysis_state["progress"] = est.update(i + 1)
        except Exception as exc:
            _analysis_state["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            _analysis_state.update(running=False, finished_at=time.time(), current_task=None)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "total": total})