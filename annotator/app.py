"""Flask application for slice annotation hand-off."""

from __future__ import annotations

import json
import os
import sqlite3

from flask import (
    Flask,
    Response,
    current_app,
    g,
    jsonify,
    render_template,
    request,
)

from . import db
from .parsing import ImportPayloadError, MAX_TILES, parse_import_text
from .sample_data import SAMPLE_IMPORT

DEFAULT_DB = os.environ.get("ANNOTATOR_DB", os.path.join(os.getcwd(), "data", "annotator.sqlite3"))
SAMPLE_NAME = "sample-surface.json"


def create_app(db_path: str | None = None, *, testing: bool = False) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or DEFAULT_DB
    app.config["TESTING"] = testing
    app.config["SAMPLE_IMPORT"] = SAMPLE_IMPORT

    os.makedirs(os.path.dirname(os.path.abspath(app.config["DB_PATH"])), exist_ok=True)
    db.init_db(app.config["DB_PATH"])

    def get_conn() -> sqlite3.Connection:
        if "conn" not in g:
            g.conn = db.connect(app.config["DB_PATH"])
        return g.conn

    @app.teardown_appcontext
    def close_conn(_exc):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    def current_state() -> dict:
        conn = get_conn()
        active = db.get_active_revision(conn)
        rejected = db.latest_rejected(conn)
        state = {
            "has_revision": active is not None,
            "revision": db.revision_view(conn, active) if active else None,
            "rejected": None,
        }
        # Only surface a rejected import if it is newer than the active
        # revision; an older failure is no longer relevant.
        if rejected is not None and (active is None or rejected["id"] > active["id"]):
            state["rejected"] = {
                "id": rejected["id"],
                "created_at": rejected["created_at"],
                "conflicts": json.loads(rejected["conflicts_json"] or "[]"),
            }
        return state

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            state=current_state(),
            max_tiles=MAX_TILES,
            sample=SAMPLE_IMPORT,
        )

    @app.get("/api/state")
    def api_state():
        return jsonify(current_state())

    @app.post("/api/import")
    def api_import():
        if request.is_json:
            # Read the raw body so malformed JSON produces a precise parser
            # error rather than a generic "empty" message.
            text = request.get_data(as_text=True)
        elif "file" in request.files and request.files["file"].filename:
            text = request.files["file"].read().decode("utf-8", errors="replace")
        else:
            text = request.form.get("payload", "")
        if not text.strip():
            return jsonify({"ok": False, "kind": "payload", "errors": ["导入内容为空。"]}), 400
        try:
            parsed = parse_import_text(text)
        except ImportPayloadError as exc:
            return jsonify({"ok": False, "kind": "payload", "errors": exc.errors}), 400
        try:
            result = db.import_revision(get_conn(), parsed.cracks, parsed.tiles)
        except Exception as exc:  # pragma: no cover - defensive
            current_app.logger.exception("import failed")
            return jsonify({"ok": False, "kind": "server", "errors": [str(exc)]}), 500
        status = 200 if result["ok"] else 409
        return jsonify({"ok": result["ok"], "kind": "plan", **result}), status

    @app.post("/api/confirm")
    def api_confirm():
        payload = request.get_json(silent=True) or {}
        tile = payload.get("tile")
        token = payload.get("token")
        if not isinstance(tile, int) or isinstance(tile, bool):
            return jsonify({"ok": False, "kind": "stale", "errors": ["缺少切片编号。"]}), 400
        if not isinstance(token, str) or not token:
            return jsonify({"ok": False, "kind": "stale", "errors": ["缺少页面版本令牌，请刷新后再确认。"]}), 409
        try:
            result = db.confirm_tile(get_conn(), tile, token)
        except db.StaleConfirmation as exc:
            return (
                jsonify(
                    {
                        "ok": False,
                        "kind": "stale",
                        "errors": ["页面显示的不是现行版本，请刷新后再确认。"],
                        "current_token": exc.current_fingerprint,
                    }
                ),
                409,
            )
        except db.NoActiveRevision:
            return jsonify({"ok": False, "kind": "stale", "errors": ["当前没有生效的清单。"]}), 409
        except db.UnknownTile:
            return jsonify({"ok": False, "kind": "stale", "errors": ["该切片不在现行版本中。"]}), 409
        return jsonify({"ok": True, **result})

    @app.get("/api/manifest")
    def api_manifest():
        conn = get_conn()
        data = db.manifest(conn)
        if data is None:
            return jsonify({"ok": False, "errors": ["当前没有生效的清单。"]}), 404
        body = json.dumps(data, ensure_ascii=False, indent=2)
        return Response(
            body,
            mimetype="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="slice-annotations-r{data["revision"]}.json"'
            },
        )

    @app.get("/api/sample")
    def api_sample():
        body = json.dumps(SAMPLE_IMPORT, ensure_ascii=False, indent=2)
        return Response(
            body,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{SAMPLE_NAME}"'},
        )

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    return app
