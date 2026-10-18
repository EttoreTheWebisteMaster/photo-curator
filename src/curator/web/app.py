"""Flask app factory for the local review UI.

The UI always binds to 127.0.0.1: never expose it on the network.
"""
from __future__ import annotations

from pathlib import Path

from flask import Flask


def create_app(db_path: str | Path = "curator.db", use_fixtures: bool = False) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = str(db_path)
    app.config["USE_FIXTURES"] = use_fixtures

    from .views import bp
    app.register_blueprint(bp)

    return app