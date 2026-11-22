from __future__ import annotations

import json
import sqlite3

import pytest

from annotator import create_app
from annotator.parsing import parse_import_text


LEGACY_SCHEMA = """
CREATE TABLE revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    cracks_json TEXT NOT NULL,
    tiles_json TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    positive_tiles_json TEXT NOT NULL,
    conflicts_json TEXT,
    fingerprint TEXT NOT NULL
);
CREATE TABLE confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL,
    tile INTEGER NOT NULL,
    annotations_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    confirmed_at TEXT NOT NULL
);
"""


def make_legacy_db(path):
    """Create a pre-tile_rect_json database (an upgraded old data volume)."""
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.commit()
    conn.close()
    return str(path)


def connect_raw(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture()
def app(tmp_path):
    return create_app(str(tmp_path / "test.sqlite3"), testing=True)


@pytest.fixture()
def client(app):
    return app.test_client()


def do_import(client, cracks, tiles, expect=200):
    res = client.post("/api/import", json={"cracks": cracks, "tiles": tiles})
    assert res.status_code == expect, res.get_json()
    return res.get_json()


def tile_by_id(state, tile_id):
    return next(t for t in state["revision"]["tiles"] if t["id"] == tile_id)


BASE_CRACKS = [
    {"id": 1, "x": 0, "y": 0, "w": 40, "h": 10},
    {"id": 2, "x": 150, "y": 20, "w": 30, "h": 10},
]
BASE_TILES = [
    {"id": 1, "x": 0, "y": 0, "w": 100, "h": 100},
    {"id": 2, "x": 100, "y": 0, "w": 100, "h": 100},
]


def test_import_and_download_manifest(client):
    data = do_import(client, BASE_CRACKS, BASE_TILES)
    assert data["ok"] is True
    assert data["positive_tiles"] == [1, 2]

    res = client.get("/api/manifest")
    assert res.status_code == 200
    manifest = res.get_json()
    assert manifest["format"] == "slice-annotations/v1"
    assert manifest["revision"] == data["revision"]
    tile1 = next(t for t in manifest["tiles"] if t["id"] == 1)
    assert tile1["annotations"] == [{"crack": 1, "kind": "positive", "rect": [0, 0, 40, 10]}]
    assert res.headers["Content-Disposition"].endswith(f'r{data["revision"]}.json"')


def test_minimal_positive_slices(client):
    cracks = [
        {"id": 1, "x": 10, "y": 10, "w": 20, "h": 5},
        {"id": 2, "x": 30, "y": 20, "w": 20, "h": 5},
    ]
    tiles = [
        {"id": 1, "x": 0, "y": 0, "w": 100, "h": 100},
        {"id": 2, "x": 0, "y": 0, "w": 60, "h": 60},
    ]
    data = do_import(client, cracks, tiles)
    assert data["positive_tiles"] == [1]


def test_confirmation_roundtrip_and_idempotency(client):
    do_import(client, BASE_CRACKS, BASE_TILES)
    state = client.get("/api/state").get_json()
    tile1 = tile_by_id(state, 1)

    first = client.post("/api/confirm", json={"tile": 1, "token": tile1["token"]})
    assert first.status_code == 200
    assert first.get_json()["already"] is False

    second = client.post("/api/confirm", json={"tile": 1, "token": tile1["token"]})
    assert second.status_code == 200
    assert second.get_json()["already"] is True

    state = client.get("/api/state").get_json()
    assert tile_by_id(state, 1)["confirmed"] is True
    # Confirmed within the current revision: not frozen/carried over.
    assert tile_by_id(state, 1)["pinned"] is False
    assert tile_by_id(state, 2)["confirmed"] is False


def test_stale_token_rejected_after_new_import(client):
    do_import(client, BASE_CRACKS, BASE_TILES)
    stale_token = tile_by_id(client.get("/api/state").get_json(), 2)["token"]

    # A new revision changes tile 2's annotations.
    do_import(
        client,
        BASE_CRACKS + [{"id": 3, "x": 120, "y": 50, "w": 20, "h": 20}],
        BASE_TILES,
    )
    res = client.post("/api/confirm", json={"tile": 2, "token": stale_token})
    assert res.status_code == 409
    body = res.get_json()
    assert body["kind"] == "stale"
    assert body["current_token"] != stale_token

    state = client.get("/api/state").get_json()
    assert tile_by_id(state, 2)["confirmed"] is False


def test_confirm_uses_current_token_after_reimport(client):
    do_import(client, BASE_CRACKS, BASE_TILES)
    do_import(
        client,
        BASE_CRACKS + [{"id": 3, "x": 120, "y": 50, "w": 20, "h": 20}],
        BASE_TILES,
    )
    state = client.get("/api/state").get_json()
    tile2 = tile_by_id(state, 2)
    res = client.post("/api/confirm", json={"tile": 2, "token": tile2["token"]})
    assert res.status_code == 200


def test_confirmed_slice_snapshot_survives_reimport(client):
    # Slices overlap so a new crack can touch a frozen slice yet be hosted by
    # the free one.
    tiles = [
        {"id": 1, "x": 0, "y": 0, "w": 100, "h": 100},
        {"id": 2, "x": 80, "y": 0, "w": 100, "h": 100},
    ]
    cracks = [
        {"id": 1, "x": 0, "y": 0, "w": 40, "h": 10},
        {"id": 2, "x": 120, "y": 20, "w": 30, "h": 10},
    ]
    data = do_import(client, cracks, tiles)
    old_rev = data["revision"]
    state = client.get("/api/state").get_json()
    tile1_before = tile_by_id(state, 1)
    client.post("/api/confirm", json={"tile": 1, "token": tile1_before["token"]})

    # Crack 3 lies in x 90..110: it overlaps frozen tile 1 (x 0..100) but is
    # only fully contained by free tile 2 (x 80..180).
    data2 = do_import(
        client,
        cracks + [{"id": 3, "x": 90, "y": 60, "w": 20, "h": 20}],
        tiles,
    )
    assert data2["ok"] is True
    assert data2["pinned_tiles"] == [1]
    assert data2["revision"] != old_rev
    assert data2["positive_tiles"] == [1, 2]

    state = client.get("/api/state").get_json()
    tile1_after = tile_by_id(state, 1)
    # Frozen slice keeps its old snapshot verbatim; no ignore for crack 3.
    assert tile1_after["annotations"] == tile1_before["annotations"]
    assert tile1_after["pinned"] is True
    # The unlabelled overlap on the frozen slice is surfaced as a warning.
    warnings = state["revision"]["warnings"]
    assert any(
        w["tile"] == 1 and w["crack"] == 3 and w["code"] == "frozen_overlap_unlabelled"
        for w in warnings
    )
    tile2_after = tile_by_id(state, 2)
    pos3 = [a for a in tile2_after["annotations"] if a["crack"] == 3]
    assert pos3 == [{"crack": 3, "kind": "positive", "rect": [10, 60, 20, 20]}]

    manifest = client.get("/api/manifest").get_json()
    m_tile1 = next(t for t in manifest["tiles"] if t["id"] == 1)
    assert m_tile1["annotations"] == tile1_before["annotations"]


def test_conflict_keeps_old_manifest_active(client):
    first = do_import(client, BASE_CRACKS, BASE_TILES)
    old_manifest = client.get("/api/manifest").get_json()

    # Crack 1 shrinks so its confirmed positive box no longer matches.
    client.post(
        "/api/confirm",
        json={"tile": 1, "token": tile_by_id(client.get("/api/state").get_json(), 1)["token"]},
    )
    bad = do_import(
        client,
        [
            {"id": 1, "x": 0, "y": 0, "w": 30, "h": 10},
            {"id": 2, "x": 150, "y": 20, "w": 30, "h": 10},
        ],
        BASE_TILES,
        expect=409,
    )
    assert bad["ok"] is False
    assert bad["conflicts"]

    # Old revision still active, and its annotation content is unchanged.
    state = client.get("/api/state").get_json()
    assert state["revision"]["id"] == first["revision"]
    assert state["revision"]["fingerprint"] == first["fingerprint"]
    current_manifest = client.get("/api/manifest").get_json()
    old_by_tile = {t["id"]: t["annotations"] for t in old_manifest["tiles"]}
    new_by_tile = {t["id"]: t["annotations"] for t in current_manifest["tiles"]}
    assert new_by_tile == old_by_tile
    assert current_manifest["revision"] == old_manifest["revision"]
    assert tile_by_id(state, 1)["confirmed"] is True


def test_no_active_revision_after_only_conflicts(client):
    res = client.post(
        "/api/import",
        json={
            "cracks": [{"id": 1, "x": 0, "y": 0, "w": 500, "h": 10}],
            "tiles": BASE_TILES,
        },
    )
    assert res.status_code == 409
    state = client.get("/api/state").get_json()
    assert state["has_revision"] is False
    assert state["rejected"]["conflicts"][0]["code"] == "crack_uncoverable"
    assert client.get("/api/manifest").status_code == 404


def test_confirmation_rejected_without_active_revision(client):
    client.post(
        "/api/import",
        json={"cracks": [{"id": 1, "x": 0, "y": 0, "w": 500, "h": 10}], "tiles": BASE_TILES},
    )
    res = client.post("/api/confirm", json={"tile": 1, "token": "whatever"})
    assert res.status_code == 409
    assert res.get_json()["kind"] == "stale"


@pytest.mark.parametrize(
    "bad_cracks",
    [
        [{"id": 1, "x": 0.0, "y": 0, "w": 10, "h": 10}],
        [{"id": 1, "x": "0", "y": 0, "w": 10, "h": 10}],
        [{"id": 1, "x": 0, "y": 0, "w": 0, "h": 10}],
        [{"id": 1, "x": 0, "y": 0, "w": True, "h": 10}],
        [{"id": 0, "x": 0, "y": 0, "w": 10, "h": 10}],
    ],
)
def test_import_rejects_non_integer_or_invalid_geometry(client, bad_cracks):
    res = client.post("/api/import", json={"cracks": bad_cracks, "tiles": BASE_TILES})
    assert res.status_code == 400
    assert res.get_json()["kind"] == "payload"


def test_import_rejects_more_than_twenty_tiles(client):
    tiles = [{"id": i + 1, "x": i * 10, "y": 0, "w": 100, "h": 100} for i in range(21)]
    res = client.post("/api/import", json={"cracks": BASE_CRACKS, "tiles": tiles})
    assert res.status_code == 400
    assert any("20" in msg for msg in res.get_json()["errors"])


def test_import_rejects_duplicate_ids(client):
    res = client.post(
        "/api/import",
        json={
            "cracks": [
                {"id": 1, "x": 0, "y": 0, "w": 10, "h": 10},
                {"id": 1, "x": 20, "y": 0, "w": 10, "h": 10},
            ],
            "tiles": BASE_TILES,
        },
    )
    assert res.status_code == 400


def test_index_page_renders(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "裂缝切片标注交接" in res.get_data(as_text=True)


def test_legacy_database_without_tile_rect_migrates(tmp_path):
    """A DB created before tile_rect_json existed still opens and works."""
    path = make_legacy_db(tmp_path / "legacy.sqlite3")

    app = create_app(str(path), testing=True)
    client = app.test_client()
    do_import(client, BASE_CRACKS, BASE_TILES)
    state = client.get("/api/state").get_json()
    tile1 = tile_by_id(state, 1)
    res = client.post("/api/confirm", json={"tile": 1, "token": tile1["token"]})
    assert res.status_code == 200


def test_sample_import_is_valid(client):
    res = client.get("/api/sample")
    assert res.status_code == 200
    parsed = parse_import_text(res.get_data(as_text=True))
    assert len(parsed.tiles) <= 20


def test_interleaved_stale_confirm_then_reimport_then_confirm(client):
    """Correction/confirmation interleaving end-to-end.

    1. Import revision A, load it (reviewer sees A).
    2. Reviewer confirms tile 2 of A.
    3. A correction is imported (revision B) while nobody re-renders tile 1.
    4. Reviewer submits tile 1 using A's token -> rejected as stale.
    5. Reviewer refreshes, now sees B, confirms tile 1 -> accepted.
    6. Tile 2's confirmed snapshot from A survives into B unchanged.
    """
    tiles = [
        {"id": 1, "x": 0, "y": 0, "w": 100, "h": 100},
        {"id": 2, "x": 80, "y": 0, "w": 100, "h": 100},
    ]
    cracks = [
        {"id": 1, "x": 0, "y": 0, "w": 40, "h": 10},
        {"id": 2, "x": 120, "y": 20, "w": 30, "h": 10},
    ]
    do_import(client, cracks, tiles)
    state_a = client.get("/api/state").get_json()
    t2_a = tile_by_id(state_a, 2)
    t1_a = tile_by_id(state_a, 1)

    assert client.post("/api/confirm", json={"tile": 2, "token": t2_a["token"]}).status_code == 200

    # Crack 3 sits only inside free tile 1, so tile 1's plan changes (its old
    # token goes stale) while frozen tile 2 is untouched.
    new_cracks = cracks + [{"id": 3, "x": 20, "y": 60, "w": 20, "h": 20}]
    do_import(client, new_cracks, tiles)
    stale = client.post("/api/confirm", json={"tile": 1, "token": t1_a["token"]})
    assert stale.status_code == 409
    assert stale.get_json()["kind"] == "stale"

    state_b = client.get("/api/state").get_json()
    t1_b = tile_by_id(state_b, 1)
    fresh = client.post("/api/confirm", json={"tile": 1, "token": t1_b["token"]})
    assert fresh.status_code == 200
    assert fresh.get_json()["already"] is False

    state_b2 = client.get("/api/state").get_json()
    assert tile_by_id(state_b2, 2)["annotations"] == t2_a["annotations"]
    assert tile_by_id(state_b2, 2)["confirmed"] is True
    assert tile_by_id(state_b2, 1)["confirmed"] is True


def test_confirmation_of_new_crack_lands_on_unpinned_slice(client):
    """A correction that adds a crack must host it on a slice not yet confirmed."""
    tiles = [
        {"id": 1, "x": 0, "y": 0, "w": 100, "h": 100},
        {"id": 2, "x": 80, "y": 0, "w": 100, "h": 100},
    ]
    cracks = [
        {"id": 1, "x": 0, "y": 0, "w": 40, "h": 10},
        {"id": 2, "x": 120, "y": 20, "w": 30, "h": 10},
    ]
    do_import(client, cracks, tiles)
    state = client.get("/api/state").get_json()
    # Freeze slice 1 only; slice 2 remains available.
    t1 = tile_by_id(state, 1)
    client.post("/api/confirm", json={"tile": 1, "token": t1["token"]})

    # Crack 3 overlaps slice 1 but is only fully contained by slice 2.
    res = do_import(
        client,
        cracks + [{"id": 3, "x": 90, "y": 40, "w": 20, "h": 20}],
        tiles,
    )
    assert res["positive_tiles"] == [1, 2]
    state = client.get("/api/state").get_json()
    pos = [a for a in tile_by_id(state, 2)["annotations"] if a["crack"] == 3]
    assert pos == [{"crack": 3, "kind": "positive", "rect": [10, 40, 20, 20]}]
    # Frozen tile 1 keeps exactly its old snapshot: no ignore for crack 3.
    assert tile_by_id(state, 1)["annotations"] == t1["annotations"]
    assert tile_by_id(state, 1)["pinned"] is True


def test_moved_confirmed_slice_conflicts(client):
    """A confirmed slice that moves or resizes cannot keep local-coordinate annotations."""
    do_import(
        client,
        [{"id": 1, "x": 0, "y": 0, "w": 40, "h": 10}],
        [{"id": 1, "x": 0, "y": 0, "w": 100, "h": 100}],
    )
    state = client.get("/api/state").get_json()
    t1 = tile_by_id(state, 1)
    assert client.post("/api/confirm", json={"tile": 1, "token": t1["token"]}).status_code == 200

    res = client.post(
        "/api/import",
        json={
            "cracks": [{"id": 1, "x": 0, "y": 0, "w": 40, "h": 10}],
            "tiles": [{"id": 1, "x": 10, "y": 0, "w": 100, "h": 100}],
        },
    )
    assert res.status_code == 409
    codes = {c["code"] for c in res.get_json()["conflicts"]}
    assert "pinned_tile_moved" in codes
    # Old revision still served.
    assert client.get("/api/manifest").status_code == 200


def test_new_crack_with_no_free_slice_conflicts(client):
    """If every slice that can host a crack is frozen, the import conflicts."""
    do_import(client, BASE_CRACKS, BASE_TILES)
    state = client.get("/api/state").get_json()
    for tile in state["revision"]["tiles"]:
        client.post("/api/confirm", json={"tile": tile["id"], "token": tile["token"]})

    res = client.post(
        "/api/import",
        json={
            "cracks": BASE_CRACKS + [{"id": 3, "x": 30, "y": 30, "w": 20, "h": 20}],
            "tiles": BASE_TILES,
        },
    )
    assert res.status_code == 409
    codes = {c["code"] for c in res.get_json()["conflicts"]}
    assert codes == {"crack_not_hostable"}
    # Old manifest is still served.
    assert client.get("/api/manifest").status_code == 200


# --- Upgraded legacy volumes: ignore-only confirmed slice must not move ----


def _ignore_only_setup(client):
    """A confirmed slice whose only annotation is an ignore region.

    Crack 1 straddles slices 1 and 2 and is hosted by slice 2, so slice 1's
    confirmation contains nothing but an ignore box.  Slice 2 is confirmed
    too, so recovery can be shown to keep the other slice's progress.
    """
    cracks = [{"id": 1, "x": 80, "y": 20, "w": 40, "h": 20}]
    tiles = [
        {"id": 1, "x": 0, "y": 0, "w": 100, "h": 100},
        {"id": 2, "x": 60, "y": 0, "w": 100, "h": 100},
    ]
    data = do_import(client, cracks, tiles)
    state = client.get("/api/state").get_json()
    tile1 = tile_by_id(state, 1)
    tile2 = tile_by_id(state, 2)
    assert [a["kind"] for a in tile1["annotations"]] == ["ignore"]
    assert [a["kind"] for a in tile2["annotations"]] == ["positive"]
    res = client.post("/api/confirm", json={"tile": 1, "token": tile1["token"]})
    assert res.status_code == 200
    res = client.post("/api/confirm", json={"tile": 2, "token": tile2["token"]})
    assert res.status_code == 200
    return cracks, tiles, tile1


def _strip_tile_rects(path):
    """Emulate a volume written before tile_rect_json was snapshotted."""
    conn = connect_raw(path)
    conn.execute("UPDATE confirmations SET tile_rect_json = NULL")
    conn.commit()
    conn.close()


def test_legacy_volume_refuses_to_move_confirmed_ignore_slice(tmp_path):
    path = make_legacy_db(tmp_path / "legacy.sqlite3")
    app = create_app(str(path), testing=True)
    client = app.test_client()
    cracks, tiles, tile1_before = _ignore_only_setup(client)
    _strip_tile_rects(path)
    # Simulate an app restart after the old volume upgrade.
    client = create_app(str(path), testing=True).test_client()

    moved = [
        {"id": 1, "x": 10, "y": 0, "w": 100, "h": 100},
        {"id": 2, "x": 60, "y": 0, "w": 100, "h": 100},
    ]
    res = client.post("/api/import", json={"cracks": cracks, "tiles": moved})
    assert res.status_code == 409
    codes = {c["code"] for c in res.get_json()["conflicts"]}
    assert "pinned_tile_moved" in codes

    # Original frame and ignore annotation are still served unchanged.
    manifest = client.get("/api/manifest")
    assert manifest.status_code == 200
    tile1 = next(t for t in manifest.get_json()["tiles"] if t["id"] == 1)
    assert tile1["rect"] == [0, 0, 100, 100]
    assert tile1["annotations"] == tile1_before["annotations"]

    # Restoring the original frame is accepted and the confirmation survives.
    restored = do_import(client, cracks, tiles)
    assert restored["pinned_tiles"] == [1, 2]
    state = client.get("/api/state").get_json()
    tile1 = tile_by_id(state, 1)
    assert tile1["rect"] == [0, 0, 100, 100]
    assert tile1["pinned"] is True
    assert tile1["confirmed"] is True
    assert tile1["annotations"] == tile1_before["annotations"]


def _emulate_old_bug_drift(path):
    """Rewrite the DB the way the pre-fix code left an already-misaligned volume.

    The first confirmation (legacy, no rect snapshot) is kept; a second
    confirmation re-records the *moved* frame, which is what re-clicking
    "确认沿用此切片的标注" produced under the old bug.
    """
    conn = connect_raw(path)
    conn.execute("UPDATE confirmations SET tile_rect_json = NULL")
    conn.execute("UPDATE revisions SET status = 'superseded' WHERE status = 'active'")
    moved_tiles = {"1": [10, 0, 100, 100], "2": [60, 0, 100, 100]}
    plan = {
        "1": [{"crack": 1, "kind": "ignore", "rect": [80, 20, 20, 20]}],
        "2": [{"crack": 1, "kind": "positive", "rect": [20, 20, 40, 20]}],
    }
    cursor = conn.execute(
        "INSERT INTO revisions (created_at, status, cracks_json, tiles_json,"
        " plan_json, positive_tiles_json, conflicts_json, fingerprint)"
        " VALUES ('2026-01-01', 'active', ?, ?, ?, ?, NULL, 'drifted')",
        (
            json.dumps({"1": [80, 20, 40, 20]}),
            json.dumps(moved_tiles),
            json.dumps(plan),
            json.dumps([2]),
        ),
    )
    revision_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO confirmations (revision_id, tile, tile_rect_json, annotations_json,"
        " fingerprint, confirmed_at) VALUES (?, 1, ?, ?, 'drifted', '2026-01-01')",
        (
            revision_id,
            json.dumps([10, 0, 100, 100]),
            json.dumps(plan["1"]),
        ),
    )
    conn.commit()
    conn.close()


def test_already_drifted_volume_lists_drift_pauses_confirm_and_manifest(tmp_path):
    path = make_legacy_db(tmp_path / "drifted.sqlite3")
    client = create_app(str(path), testing=True).test_client()
    cracks, tiles, tile1_before = _ignore_only_setup(client)
    _emulate_old_bug_drift(path)

    # Restart to prove the verdict comes from the database, not memory.
    client = create_app(str(path), testing=True).test_client()
    state = client.get("/api/state").get_json()
    rev = state["revision"]
    assert rev["drift_paused"] is True
    drift = rev["drift"]
    assert [d["tile"] for d in drift] == [1]
    assert drift[0]["original_rect"] == [0, 0, 100, 100]
    assert drift[0]["current_rect"] == [10, 0, 100, 100]

    tile1 = tile_by_id(state, 1)
    assert tile1["drifted"] is True
    assert tile1["original_rect"] == [0, 0, 100, 100]
    assert tile1["annotations"] == tile1_before["annotations"]

    # Confirming the drifted slice is paused, even with its current token.
    paused = client.post("/api/confirm", json={"tile": 1, "token": tile1["token"]})
    assert paused.status_code == 409
    assert paused.get_json()["kind"] == "paused"

    # The whole manifest download is paused, not just the drifted tile.
    blocked = client.get("/api/manifest")
    assert blocked.status_code == 409
    assert blocked.get_json()["kind"] == "paused"
    assert blocked.get_json()["drift"][0]["tile"] == 1

    # Re-importing the original frames repairs it; confirmation and the other
    # slices' review progress are preserved and the manifest downloads again.
    repaired = do_import(client, cracks, tiles)
    assert repaired["pinned_tiles"] == [1, 2]
    state = client.get("/api/state").get_json()
    assert state["revision"]["drift_paused"] is False
    tile1 = tile_by_id(state, 1)
    assert tile1["rect"] == [0, 0, 100, 100]
    assert tile1["pinned"] is True
    assert tile1["confirmed"] is True
    assert tile1["annotations"] == tile1_before["annotations"]
    manifest = client.get("/api/manifest")
    assert manifest.status_code == 200
    assert manifest.get_json()["tiles"][0]["annotations"] == tile1_before["annotations"]
    # The other slice's review progress is kept too.
    tile2 = tile_by_id(state, 2)
    assert tile2["pinned"] is True
    assert tile2["confirmed"] is True


def test_failed_repair_keeps_drifted_active_revision(tmp_path):
    """A failed correction keeps the drifted revision active and still paused."""
    path = make_legacy_db(tmp_path / "drifted.sqlite3")
    client = create_app(str(path), testing=True).test_client()
    cracks, tiles, _ = _ignore_only_setup(client)
    _emulate_old_bug_drift(path)

    # Repair attempt still moves the slice: it must conflict and change nothing.
    wrong = [
        {"id": 1, "x": 20, "y": 0, "w": 100, "h": 100},
        {"id": 2, "x": 60, "y": 0, "w": 100, "h": 100},
    ]
    res = client.post("/api/import", json={"cracks": cracks, "tiles": wrong})
    assert res.status_code == 409
    assert {c["code"] for c in res.get_json()["conflicts"]} == {"pinned_tile_moved"}

    state = client.get("/api/state").get_json()
    assert state["revision"]["drift_paused"] is True
    assert state["revision"]["tiles"][0]["rect"] == [10, 0, 100, 100]
    assert client.get("/api/manifest").status_code == 409

    # The DB-derived verdict survives a restart.
    client = create_app(str(path), testing=True).test_client()
    assert client.get("/api/state").get_json()["revision"]["drift_paused"] is True
