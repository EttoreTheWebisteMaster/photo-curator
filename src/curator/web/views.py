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
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import (
    Blueprint, abort, current_app, g, jsonify,
    redirect, render_template, request, send_file, url_for,
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


# ---- Analysis state accessors -------------------------------------------------------
# State lives in app.config so each Flask app (and each test client) is isolated.
# Background threads capture references to the state/stop objects; they must NEVER
# touch `current_app` (no request context there).

def _state() -> dict:
    return current_app.config["_analysis_state"]


def _stop() -> threading.Event:
    return current_app.config["_analysis_stop"]


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
    for r in rows:
        h.update(f"{r['id']}:{r['sha256'] or ''}:{r['mtime']}".encode())
    return h.hexdigest()


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
        "exposure": 0.85,
    }


def _load_group_list(conn) -> list[dict]:
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
    has_groups = conn.execute("SELECT COUNT(*) FROM group_states").fetchone()[0] > 0
    if not has_groups:
        return redirect(url_for("web.intake"))
    groups = _load_group_list(conn)
    pending = sum(1 for gr in groups if gr["status"] == "pending")
    first_pending = next((gr for gr in groups if gr["status"] == "pending"), groups[0] if groups else None)
    detail = _load_group_detail(conn, first_pending["id"]) if first_pending else None
    return render_template(
        "index.html", groups=groups, pending_count=pending,
        use_fixtures=False, initial_detail=detail,
    )


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
    last_folder = _load_setting(current_app.config["DB_PATH"], "last_folder") or ""
    return render_template(
        "intake.html",
        has_groups=has_groups,
        pending_count=pending,
        last_folder=last_folder,
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
            "tasks": [
                {"id": "scan", "label": "Reading metadata and detecting duplicates",
                 "status": "running", "count": 0, "total": total, "eta_label": "Calibrating…"},
                {"id": "group", "label": "Grouping similar shots", "status": "pending"},
            ],
            "started_at": time.time(), "finished_at": None,
            "error": None, "final": None, "cancelled": False,
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
                state["tasks"][0]["eta_label"] = snap["eta_label"]
            state["tasks"][0]["status"] = "done"
            if state["cancelled"]:
                state["tasks"][1]["status"] = "pending"
                state["final"] = {"mode": "cancelled"}
                return
            state["tasks"][1]["status"] = "running"
            time.sleep(0.3)
            state["tasks"][1]["status"] = "done"
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

    def run():
        state.clear()
        state.update({
            "running": True,
            "folder": str(folder_path),
            "tasks": [
                {"id": "scan", "label": "Reading metadata and detecting duplicates",
                 "status": "running", "count": 0, "total": 0, "eta_label": "Calibrating…"},
                {"id": "group", "label": "Grouping similar shots", "status": "pending"},
            ],
            "started_at": time.time(), "finished_at": None,
            "error": None, "final": None, "cancelled": False,
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
                state["tasks"][0]["eta_label"] = snap["eta_label"]

            scan_stats = scanmod.scan(
                folder_path, conn, progress=on_progress, stop_event=stop
            )
            state["tasks"][0]["count"] = total_files
            if stop.is_set():
                state["tasks"][0]["status"] = "done"
                state["tasks"][1]["status"] = "pending"
                state["cancelled"] = True
                state["final"] = {"mode": "cancelled"}
                return

            state["tasks"][0]["status"] = "done"
            state["tasks"][1]["status"] = "running"

            new_fp = _analysis_fingerprint(conn)
            old_fp = _load_setting(db_path, "grouping_fingerprint")
            has_groups = conn.execute("SELECT COUNT(*) FROM group_states").fetchone()[0] > 0

            if new_fp == old_fp and has_groups and not scan_stats["new"]:
                state["tasks"][1]["status"] = "done"
                state["tasks"][1]["summary"] = "no changes"
                state["final"] = {"mode": "no_changes"}
            else:
                group_stats = groupmod.build_groups(conn)
                _save_setting(db_path, "grouping_fingerprint", new_fp)
                state["tasks"][1]["status"] = "done"
                state["tasks"][1]["summary"] = f"{group_stats['groups']} groups"
                state["final"] = {
                    "mode": "fresh",
                    "photos": scan_stats["new"] + scan_stats["updated"] + scan_stats["skipped"],
                    "new_photos": scan_stats["new"],
                    "groups": group_stats["groups"],
                    "groups_with_multiple": group_stats["groups_with_multiple"],
                }
        except Exception as exc:
            state["error"] = f"{type(exc).__name__}: {exc}"
            for t in state["tasks"]:
                if t["status"] == "running":
                    t["status"] = "error"
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