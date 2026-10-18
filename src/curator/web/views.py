"""Routes. Sprint 1: reads fixtures and returns fake OKs for actions.
Sprint 3 replaces `_load_groups` and the API bodies with real DB writes."""
from __future__ import annotations

import threading
import time

from flask import Blueprint, current_app, jsonify, render_template, request

from .fixtures import get_fake_groups
from .progress import ProgressEstimator

bp = Blueprint(
    "web",
    __name__,
    static_folder="static",
    template_folder="templates",
    static_url_path="/static",
)

# Shared in-memory analysis state. Sprint 3: persist to SQLite if resume is needed.
_analysis_state = {
    "running": False,
    "progress": None,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "current_task": None,
}


def _load_groups() -> list[dict]:
    if current_app.config["USE_FIXTURES"]:
        return get_fake_groups()
    return get_fake_groups()


@bp.route("/")
def index():
    groups = _load_groups()
    pending = sum(1 for g in groups if g["status"] == "pending")
    return render_template(
        "index.html",
        groups=groups,
        pending_count=pending,
        use_fixtures=current_app.config["USE_FIXTURES"],
    )


# ---- Analysis status ---------------------------------------------------------------

@bp.route("/api/analyze/status")
def analyze_status():
    """Polled by the frontend every 2 seconds."""
    return jsonify(_analysis_state)


@bp.route("/api/analyze/simulate", methods=["POST"])
def analyze_simulate():
    """Sprint 1 stub: fake analysis to test the UI. Removed in Sprint 3."""
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
                time.sleep(0.03)  # ~30 ms/photo → 318 photos ≈ 10 s
                _analysis_state["progress"] = est.update(i + 1)
        except Exception as exc:
            _analysis_state["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            _analysis_state.update(
                running=False,
                finished_at=time.time(),
                current_task=None,
            )

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "total": total})


# ---- Group actions (Sprint 3 will write these to SQLite) -----------------------------

@bp.route("/api/groups/<int:group_id>/choose", methods=["POST"])
def choose(group_id: int):
    data = request.get_json(silent=True) or {}
    return jsonify({"ok": True, "group_id": group_id, "photo_id": data.get("photo_id")})


@bp.route("/api/groups/<int:group_id>/confirm", methods=["POST"])
def confirm(group_id: int):
    return jsonify({"ok": True, "group_id": group_id, "status": "completed"})


@bp.route("/api/groups/<int:group_id>/delete", methods=["POST"])
def delete_group(group_id: int):
    return jsonify({"ok": True, "group_id": group_id, "status": "deleted"})