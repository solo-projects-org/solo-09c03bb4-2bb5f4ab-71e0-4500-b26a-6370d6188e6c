"""SQLite persistence for revisions and confirmations.

A *revision* is an immutable import of crack boxes and slice boxes plus the
plan computed from them.  Exactly one revision is ``active`` at a time.
Confirmations snapshot the annotations a reviewer saw; snapshots taken on
earlier revisions are *pinned* and must survive later imports unchanged.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .planning import Annotation, PlanConflict, build_plan, carried_over_overlaps
from .geometry import Rect

_SCHEMA = """
CREATE TABLE IF NOT EXISTS revisions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'rejected')),
    cracks_json         TEXT NOT NULL,
    tiles_json          TEXT NOT NULL,
    plan_json           TEXT NOT NULL,
    positive_tiles_json TEXT NOT NULL,
    conflicts_json      TEXT,
    fingerprint         TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_revision
    ON revisions(status) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS confirmations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id      INTEGER NOT NULL REFERENCES revisions(id),
    tile             INTEGER NOT NULL,
    tile_rect_json   TEXT NOT NULL,
    annotations_json TEXT NOT NULL,
    fingerprint      TEXT NOT NULL,
    confirmed_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS confirmations_by_tile
    ON confirmations(tile, id DESC);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def revision_fingerprint(revision_id: int, plan: dict[int, list[Annotation]]) -> str:
    payload = {
        "revision": revision_id,
        "plan": {str(tile): [a.to_dict() for a in anns] for tile, anns in plan.items()},
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def tile_fingerprint(revision_id: int, tile: int, annotations: list[Annotation]) -> str:
    payload = {
        "revision": revision_id,
        "tile": tile,
        "annotations": [a.to_dict() for a in annotations],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path: str) -> None:
    conn = connect(path)
    try:
        conn.executescript(_SCHEMA)
        # Lightweight migration for databases created before tile rects were
        # snapshotted alongside confirmations.
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(confirmations)")
        }
        if "tile_rect_json" not in columns:
            conn.execute("ALTER TABLE confirmations ADD COLUMN tile_rect_json TEXT")
    finally:
        conn.close()


def _decode_plan(raw_plan: str) -> dict[int, list[Annotation]]:
    result: dict[int, list[Annotation]] = {}
    for tile_key, items in json.loads(raw_plan).items():
        result[int(tile_key)] = [
            Annotation(
                crack=item["crack"],
                kind=item["kind"],
                rect=Rect(*item["rect"]),
            )
            for item in items
        ]
    return result


def get_active_revision(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM revisions WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def latest_rejected(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM revisions WHERE status = 'rejected' ORDER BY id DESC LIMIT 1"
    ).fetchone()


@dataclass
class PinnedTile:
    tile: int
    rect: Rect | None
    annotations: list[Annotation]
    revision_id: int


def load_pinned(conn: sqlite3.Connection) -> dict[int, PinnedTile]:
    """Latest confirmation snapshot per tile, across all revisions.

    ``rect`` is the slice rectangle the reviewer confirmed against; it is
    used to detect slices that moved or resized since then.
    """
    rows = conn.execute(
        """
        SELECT c.tile, c.revision_id, c.tile_rect_json, c.annotations_json
        FROM confirmations c
        JOIN (
            SELECT tile, MAX(id) AS newest
            FROM confirmations GROUP BY tile
        ) latest ON latest.tile = c.tile AND latest.newest = c.id
        ORDER BY c.tile
        """
    ).fetchall()
    pinned: dict[int, PinnedTile] = {}
    for row in rows:
        rect = Rect(*json.loads(row["tile_rect_json"])) if row["tile_rect_json"] else None
        pinned[row["tile"]] = PinnedTile(
            tile=row["tile"],
            rect=rect,
            annotations=[
                Annotation(crack=item["crack"], kind=item["kind"], rect=Rect(*item["rect"]))
                for item in json.loads(row["annotations_json"])
            ],
            revision_id=row["revision_id"],
        )
    return pinned


def confirmed_tiles(conn: sqlite3.Connection, revision_id: int | None = None) -> set[int]:
    if revision_id is None:
        rows = conn.execute("SELECT DISTINCT tile FROM confirmations").fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT tile FROM confirmations WHERE revision_id = ?",
            (revision_id,),
        ).fetchall()
    return {row["tile"] for row in rows}


def confirmation_for(conn: sqlite3.Connection, revision_id: int, tile: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM confirmations WHERE revision_id = ? AND tile = ? ORDER BY id DESC LIMIT 1",
        (revision_id, tile),
    ).fetchone()


class StaleConfirmation(Exception):
    """The page the reviewer saw is no longer the current version."""

    def __init__(self, current_fingerprint: str | None):
        super().__init__("stale confirmation")
        self.current_fingerprint = current_fingerprint


class NoActiveRevision(Exception):
    pass


class UnknownTile(Exception):
    pass


def confirm_tile(conn: sqlite3.Connection, tile: int, token: str) -> dict:
    """Record a confirmation atomically.

    The token pins the exact revision and annotation set the reviewer saw.
    If a newer revision became active in between, the token is stale.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        active = get_active_revision(conn)
        if active is None:
            raise NoActiveRevision()
        plan = _decode_plan(active["plan_json"])
        if tile not in plan:
            raise UnknownTile()
        annotations = plan[tile]
        current = tile_fingerprint(active["id"], tile, annotations)
        if token != current:
            raise StaleConfirmation(current)
        existing = confirmation_for(conn, active["id"], tile)
        if existing is not None:
            conn.execute("COMMIT")
            return {
                "tile": tile,
                "confirmed_at": existing["confirmed_at"],
                "fingerprint": existing["fingerprint"],
                "revision": active["id"],
                "already": True,
            }
        timestamp = now_iso()
        tile_rect = json.loads(active["tiles_json"]).get(str(tile))
        conn.execute(
            "INSERT INTO confirmations (revision_id, tile, tile_rect_json, annotations_json,"
            " fingerprint, confirmed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                active["id"],
                tile,
                canonical_json(tile_rect),
                canonical_json([a.to_dict() for a in annotations]),
                current,
                timestamp,
            ),
        )
        conn.execute("COMMIT")
        return {
            "tile": tile,
            "confirmed_at": timestamp,
            "fingerprint": current,
            "revision": active["id"],
            "already": False,
        }
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def import_revision(
    conn: sqlite3.Connection, cracks: dict[int, Rect], tiles: dict[int, Rect]
) -> dict:
    """Validate against pinned confirmations and activate a new revision.

    On conflict the old active revision stays in place; the rejected attempt
    is recorded for inspection and its conflicts are returned.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        pinned_tiles = load_pinned(conn)
        pinned_annotations = {t: p.annotations for t, p in pinned_tiles.items()}
        pinned_rects = {
            t: p.rect for t, p in pinned_tiles.items() if p.rect is not None
        }
        try:
            plan = build_plan(cracks, tiles, pinned_annotations, pinned_rects)
        except PlanConflict as exc:
            timestamp = now_iso()
            conn.execute(
                "INSERT INTO revisions (created_at, status, cracks_json, tiles_json,"
                " plan_json, positive_tiles_json, conflicts_json, fingerprint)"
                " VALUES (?, 'rejected', ?, ?, '{}', '[]', ?, ?)",
                (
                    timestamp,
                    canonical_json({str(k): v.to_list() for k, v in cracks.items()}),
                    canonical_json({str(k): v.to_list() for k, v in tiles.items()}),
                    canonical_json([c.to_dict() for c in exc.conflicts]),
                    "rejected",
                ),
            )
            rejected_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute(
                "UPDATE revisions SET fingerprint = ? WHERE id = ?",
                (
                    hashlib.sha256(
                        canonical_json({"rejected": rejected_id, "conflicts": [c.to_dict() for c in exc.conflicts]}).encode("utf-8")
                    ).hexdigest(),
                    rejected_id,
                ),
            )
            conn.execute("COMMIT")
            return {
                "ok": False,
                "revision": rejected_id,
                "conflicts": [c.to_dict() for c in exc.conflicts],
                "active": get_active_revision(conn)["id"] if get_active_revision(conn) else None,
            }

        timestamp = now_iso()
        conn.execute("UPDATE revisions SET status = 'superseded' WHERE status = 'active'")
        fingerprint = "pending"
        cursor = conn.execute(
            "INSERT INTO revisions (created_at, status, cracks_json, tiles_json,"
            " plan_json, positive_tiles_json, conflicts_json, fingerprint)"
            " VALUES (?, 'active', ?, ?, ?, ?, NULL, ?)",
            (
                timestamp,
                canonical_json({str(k): v.to_list() for k, v in cracks.items()}),
                canonical_json({str(k): v.to_list() for k, v in tiles.items()}),
                canonical_json(
                    {str(tile): [a.to_dict() for a in anns] for tile, anns in plan.annotations.items()}
                ),
                canonical_json(plan.positive_tiles),
                fingerprint,
            ),
        )
        revision_id = cursor.lastrowid
        fingerprint = revision_fingerprint(revision_id, plan.annotations)
        conn.execute("UPDATE revisions SET fingerprint = ? WHERE id = ?", (fingerprint, revision_id))
        conn.execute("COMMIT")
        return {
            "ok": True,
            "revision": revision_id,
            "fingerprint": fingerprint,
            "positive_tiles": plan.positive_tiles,
            "pinned_tiles": sorted(pinned_tiles),
        }
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def revision_view(conn: sqlite3.Connection, revision: sqlite3.Row) -> dict:
    plan = _decode_plan(revision["plan_json"])
    confirmed = confirmed_tiles(conn, revision["id"])
    latest = load_pinned(conn) if revision["status"] == "active" else {}
    # A pinned tile's latest confirmation predates this revision; its
    # annotations are carried over verbatim and count as confirmed.  A tile
    # confirmed *within* this revision is confirmed but not frozen.
    pinned_tiles = {
        t: p for t, p in latest.items() if p.revision_id != revision["id"]
    }
    reviewed = confirmed | set(pinned_tiles)
    tiles: list[dict] = []
    crack_rows = json.loads(revision["cracks_json"])
    tile_rows = json.loads(revision["tiles_json"])
    for tile_number, rect in sorted(((int(k), v) for k, v in tile_rows.items())):
        annotations = plan.get(tile_number, [])
        tiles.append(
            {
                "id": tile_number,
                "rect": rect,
                "annotations": [a.to_dict() for a in annotations],
                "token": tile_fingerprint(revision["id"], tile_number, annotations),
                "confirmed": tile_number in reviewed,
                "pinned": tile_number in pinned_tiles,
                "confirmed_this_revision": tile_number in confirmed,
            }
        )
    return {
        "id": revision["id"],
        "created_at": revision["created_at"],
        "status": revision["status"],
        "fingerprint": revision["fingerprint"],
        "positive_tiles": json.loads(revision["positive_tiles_json"]),
        "cracks": [{"id": int(k), "rect": v} for k, v in sorted(((int(k), v) for k, v in crack_rows.items()))],
        "tiles": tiles,
        "warnings": carried_over_overlaps(
            {int(k): Rect(*v) for k, v in crack_rows.items()},
            {int(k): Rect(*v) for k, v in tile_rows.items()},
            {t: p.annotations for t, p in pinned_tiles.items()},
        ),
    }


def manifest(conn: sqlite3.Connection) -> dict | None:
    active = get_active_revision(conn)
    if active is None:
        return None
    view = revision_view(conn, active)
    return {
        "format": "slice-annotations/v1",
        "coordinate_system": "slice-local pixels, origin at slice top-left, [x, y, w, h]",
        "revision": active["id"],
        "fingerprint": active["fingerprint"],
        "generated_at": now_iso(),
        "tiles": [
            {
                "id": tile["id"],
                "rect": tile["rect"],
                "confirmed": tile["confirmed"],
                "annotations": [
                    {"crack": a["crack"], "kind": a["kind"], "rect": a["rect"]}
                    for a in tile["annotations"]
                ],
            }
            for tile in view["tiles"]
        ],
    }
