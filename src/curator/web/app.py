"""Flask app factory for the local review UI.

The UI always binds to 127.0.0.1: never expose it on the network.
"""
from __future__ import annotations

import threading
from pathlib import Path

from flask import Flask


def _initial_analysis_state() -> dict:
    return {
        "running": False,
        "folder": None,
        "tasks": [],
        "started_at": None,
        "finished_at": None,
        "error": None,
        "final": None,
        "cancelled": False,
    }


def create_app(db_path: str | Path = "curator.db", use_fixtures: bool = False) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = str(db_path)
    app.config["USE_FIXTURES"] = use_fixtures

    # Per-app analysis state. Lives in config so each Flask app (and each
    # test client) has its own; background threads capture a reference to
    # this dict, so they never touch `current_app`.
    app.config["_analysis_state"] = _initial_analysis_state()
    app.config["_analysis_stop"] = threading.Event()
    app.config["_commit_state"] = {
        "running": False,
        "progress": None,
        "result": None,
        "error": None,
        "started_at": None,
        "finished_at": None,
    }

    from .views import bp, close_db
    app.register_blueprint(bp)
    app.teardown_appcontext(close_db)

    return app