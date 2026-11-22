"""SQLite persistence for revisions and confirmations.

A *revision* is an immutable import of crack boxes and slice boxes plus the
plan computed from them.  Exactly one revision is ``active`` at a time.
Confirmations snapshot the annotations a reviewer saw; snapshots taken on
earlier revisions are *pinned* and must survive later imports unchanged.

The *first* confirmation of a slice is authoritative for both its original
slice frame and its annotations.  Legacy volumes did not store the frame in
``confirmations``; it is recovered from the immutable revision the
confirmation was recorded on.  If the active revision's frame no longer
matches that first frame, the slice is *drifted*: its confirmation and the
whole manifest download are paused until the original frame is re-imported.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .planning import (
    Annotation,
    Conflict,
    PlanConflict,
    build_plan,
    carried_over_overlaps,
)
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


def _confirmation_rect(conn: sqlite3.Connection, row: sqlite3.Row) -> Rect | None:
    """Recover the original-image slice rect a confirmation was made against.

    Databases created before ``tile_rect_json`` existed have ``NULL`` in that
    column, so the rect is recovered from the revision the confirmation was
    recorded on.  That revision's ``tiles_json`` is immutable and is exactly
    the slice frame the reviewer saw.  Only if even that is unavailable do we
    return ``None`` (unknown original frame).
    """
    raw = row["tile_rect_json"]
    if raw:
        value = json.loads(raw)
        if value is not None:
            return Rect(*value)
    revision = conn.execute(
        "SELECT tiles_json FROM revisions WHERE id = ?", (row["revision_id"],)
    ).fetchone()
    if revision is None:
        return None
    tile_rect = json.loads(revision["tiles_json"]).get(str(row["tile"]))
    return Rect(*tile_rect) if tile_rect is not None else None


def load_pinned(conn: sqlite3.Connection) -> dict[int, PinnedTile]:
    """First confirmation snapshot per tile, across all revisions.

    The first confirmation of a slice is authoritative for both its
    original-image slice rect and its annotations: later confirmations only
    repeat that snapshot.  Picking the *first* row (instead of the newest) is
    what makes a legacy volume whose rect was not snapshotted recover the
    frame the reviewer actually confirmed, even if the slice has since
    drifted and been re-confirmed.
    """
    rows = conn.execute(
        """
        SELECT c.tile, c.revision_id, c.tile_rect_json, c.annotations_json
        FROM confirmations c
        JOIN (
            SELECT tile, MIN(id) AS first
            FROM confirmations GROUP BY tile
        ) first_conf ON first_conf.tile = c.tile AND first_conf.first = c.id
        ORDER BY c.tile
        """
    ).fetchall()
    pinned: dict[int, PinnedTile] = {}
    for row in rows:
        pinned[row["tile"]] = PinnedTile(
            tile=row["tile"],
            rect=_confirmation_rect(conn, row),
            annotations=[
                Annotation(crack=item["crack"], kind=item["kind"], rect=Rect(*item["rect"]))
                for item in json.loads(row["annotations_json"])
            ],
            revision_id=row["revision_id"],
        )
    return pinned


@dataclass
class TileDrift:
    """A confirmed slice whose current frame differs from its first confirmation."""

    tile: int
    original_rect: Rect | None
    current_rect: Rect | None

    def to_dict(self) -> dict:
        if self.original_rect is None:
            message = (
                f"已确认切片 #{self.tile} 缺少首次确认时的切片框记录，无法核对当前位置。"
            )
        elif self.current_rect is None:
            message = (
                f"已确认切片 #{self.tile} 不在现行版本中；首次确认的原框为 "
                f"{self.original_rect.to_list()}。"
            )
        else:
            message = (
                f"已确认切片 #{self.tile} 的切片框已错位：首次确认的原框为 "
                f"{self.original_rect.to_list()}，现行版为 {self.current_rect.to_list()}。"
            )
        return {
            "tile": self.tile,
            "original_rect": self.original_rect.to_list() if self.original_rect else None,
            "current_rect": self.current_rect.to_list() if self.current_rect else None,
            "message": message,
        }


def drift_against(
    pinned: dict[int, PinnedTile], tiles: dict[int, Rect]
) -> list[TileDrift]:
    """Confirmed slices whose frame in *tiles* differs from their first snapshot.

    Works for any revision's tile table, not just the active one, and is a
    pure function of stored data so a restart reaches the same verdict.
    """
    drift: list[TileDrift] = []
    for tile_number in sorted(pinned):
        original = pinned[tile_number].rect
        current = tiles.get(tile_number)
        if original != current:
            drift.append(
                TileDrift(tile=tile_number, original_rect=original, current_rect=current)
            )
    return drift


def active_drift(conn: sqlite3.Connection, active: sqlite3.Row) -> list[TileDrift]:
    return drift_against(
        load_pinned(conn),
        {int(k): Rect(*v) for k, v in json.loads(active["tiles_json"]).items()},
    )


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


class ConfirmationPaused(Exception):
    """A confirmation is paused because the active revision is misaligned."""

    def __init__(self, drift: TileDrift):
        super().__init__(drift.to_dict()["message"])
        self.drift = drift

    def to_dict(self) -> dict:
        return self.drift.to_dict()


def confirm_tile(conn: sqlite3.Connection, tile: int, token: str) -> dict:
    """Record a confirmation atomically.

    The token pins the exact revision and annotation set the reviewer saw.
    If a newer revision became active in between, the token is stale.  When
    the active revision has a misaligned confirmed slice, confirming that
    slice is paused until the original frame is restored.
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
        drift = {d.tile: d for d in active_drift(conn, active)}
        if tile in drift:
            raise ConfirmationPaused(drift[tile])
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
        # A confirmed slice whose first frame cannot be recovered at all cannot
        # be checked for movement.  Refuse rather than silently accept a frame
        # that might invalidate its slice-local annotations.
        unknown_frame = sorted(set(pinned_tiles) - set(pinned_rects))
        try:
            if unknown_frame:
                raise PlanConflict(
                    [
                        Conflict(
                            "pinned_tile_unknown_frame",
                            f"已确认切片 #{t} 缺少首次确认时的切片框记录，"
                            "无法核对是否移动；请先恢复该切片的原框记录。",
                            tile=t,
                        )
                        for t in unknown_frame
                    ]
                )
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
    # A pinned tile's first confirmation predates this revision; its
    # annotations are carried over verbatim and count as confirmed.  A tile
    # confirmed *within* this revision is confirmed but not frozen.
    pinned_tiles = {
        t: p for t, p in latest.items() if p.revision_id != revision["id"]
    }
    drift = (
        active_drift(conn, revision) if revision["status"] == "active" else []
    )
    drifted = {d.tile: d for d in drift}
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
                "drifted": tile_number in drifted,
                "original_rect": (
                    drifted[tile_number].original_rect.to_list()
                    if tile_number in drifted and drifted[tile_number].original_rect
                    else None
                ),
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
        "drift": [d.to_dict() for d in drift],
        "drift_paused": bool(drift),
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
    if view["drift"]:
        # The whole manifest is paused while any confirmed slice is misaligned:
        # its local-coordinate annotations no longer describe the slice file.
        return {
            "blocked": True,
            "revision": active["id"],
            "fingerprint": active["fingerprint"],
            "drift": view["drift"],
        }
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
