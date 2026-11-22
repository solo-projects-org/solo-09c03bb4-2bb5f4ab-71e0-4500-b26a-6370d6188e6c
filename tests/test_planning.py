from __future__ import annotations

import pytest

from annotator.geometry import Rect
from annotator.planning import (
    PlanConflict,
    build_plan,
    IGNORE,
    POSITIVE,
)


def tile_annotations(plan, tile, kind=None):
    anns = plan.annotations[tile]
    return [a for a in anns if kind is None or a.kind == kind]


def test_minimum_cover_prefers_single_tile():
    cracks = {1: Rect(0, 0, 10, 10), 2: Rect(90, 0, 10, 10)}
    tiles = {
        1: Rect(0, 0, 100, 50),
        2: Rect(0, 0, 50, 50),
    }
    plan = build_plan(cracks, tiles)
    assert plan.positive_tiles == [1]
    # Tile 2 only overlaps crack 1, so it carries that single ignore region.
    assert [(a.crack, a.kind) for a in plan.annotations[2]] == [(1, IGNORE)]


def test_lexicographically_first_minimum_cover_is_deterministic():
    cracks = {1: Rect(0, 0, 10, 10), 2: Rect(100, 0, 10, 10), 3: Rect(200, 0, 10, 10)}
    tiles = {
        1: Rect(0, 0, 120, 40),
        2: Rect(80, 0, 80, 40),
        3: Rect(180, 0, 80, 40),
    }
    plan = build_plan(cracks, tiles)
    assert plan.positive_tiles == [1, 3]


def test_positive_uses_slice_local_coordinates():
    cracks = {7: Rect(120, 210, 30, 12)}
    tiles = {3: Rect(100, 200, 200, 100)}
    plan = build_plan(cracks, tiles)
    (ann,) = plan.annotations[3]
    assert ann.rect.to_list() == [20, 10, 30, 12]
    assert ann.kind == POSITIVE


def test_ignore_covers_only_intersection_in_local_coords():
    cracks = {1: Rect(0, 0, 100, 20)}
    tiles = {
        1: Rect(0, 0, 100, 50),   # holds the positive
        2: Rect(40, 0, 120, 50),  # overlaps x 40..100
    }
    plan = build_plan(cracks, tiles)
    assert plan.positive_tiles == [1]
    (ignored,) = plan.annotations[2]
    assert ignored.kind == IGNORE
    assert ignored.rect.to_list() == [0, 0, 60, 20]


def test_touching_edges_do_not_create_ignore():
    cracks = {1: Rect(0, 0, 10, 10)}
    tiles = {
        1: Rect(0, 0, 10, 10),
        2: Rect(10, 0, 20, 10),  # shares only an edge
    }
    plan = build_plan(cracks, tiles)
    assert plan.annotations[2] == []


def test_crack_not_contained_anywhere_conflicts():
    cracks = {1: Rect(0, 0, 100, 10)}
    tiles = {1: Rect(0, 0, 50, 50)}
    with pytest.raises(PlanConflict) as exc:
        build_plan(cracks, tiles)
    codes = {c.code for c in exc.value.conflicts}
    assert "crack_uncoverable" in codes


def test_pinned_snapshot_preserved_verbatim_even_with_new_ignore_overlap():
    tiles = {1: Rect(0, 0, 100, 100), 2: Rect(80, 0, 100, 100)}
    cracks_old = {1: Rect(0, 0, 50, 10)}
    old = build_plan(cracks_old, tiles)
    snapshot = old.annotations[1]
    assert snapshot[0].kind == POSITIVE
    assert old.positive_tiles == [1]

    # Crack 2 overlaps frozen slice 1 (x 90..100) but is only fully contained
    # by free slice 2.
    cracks_new = {1: Rect(0, 0, 50, 10), 2: Rect(90, 20, 30, 30)}
    plan = build_plan(cracks_new, tiles, pinned={1: snapshot})
    # Confirmed slice keeps its snapshot unchanged: no new ignore added even
    # though crack 2 overlaps it.
    assert plan.annotations[1] == snapshot
    assert plan.positive_tiles == [1, 2]


def test_pinned_positive_conflict_when_crack_box_changes():
    cracks_old = {1: Rect(0, 0, 50, 10)}
    tiles = {1: Rect(0, 0, 100, 100)}
    snapshot = build_plan(cracks_old, tiles).annotations[1]
    cracks_new = {1: Rect(0, 0, 70, 10)}
    with pytest.raises(PlanConflict) as exc:
        build_plan(cracks_new, tiles, pinned={1: snapshot})
    assert "pinned_positive_mismatch" in {c.code for c in exc.value.conflicts}


def test_pinned_positive_conflict_when_crack_no_longer_contained():
    cracks_old = {1: Rect(0, 0, 50, 10)}
    tiles = {1: Rect(0, 0, 100, 100)}
    snapshot = build_plan(cracks_old, tiles).annotations[1]
    cracks_new = {1: Rect(80, 0, 50, 10)}
    with pytest.raises(PlanConflict) as exc:
        build_plan(cracks_new, tiles, pinned={1: snapshot})
    codes = {c.code for c in exc.value.conflicts}
    assert "pinned_positive_outside" in codes


def test_partially_contained_crack_is_never_positive():
    cracks = {1: Rect(0, 0, 100, 20)}
    tiles = {
        1: Rect(0, 0, 60, 50),    # partial
        2: Rect(40, 0, 120, 50),  # partial (covers x 40..160, not 0..100)
        3: Rect(0, 0, 200, 50),   # full containment
    }
    plan = build_plan(cracks, tiles)
    assert plan.positive_tiles == [3]
    assert all(a.kind == IGNORE for a in plan.annotations[1])
    assert all(a.kind == IGNORE for a in plan.annotations[2])
    assert plan.annotations[1][0].rect.to_list() == [0, 0, 60, 20]
    assert plan.annotations[2][0].rect.to_list() == [0, 0, 60, 20]


def test_each_crack_has_exactly_one_positive():
    cracks = {
        1: Rect(10, 10, 20, 5),
        2: Rect(60, 30, 20, 5),
        3: Rect(120, 50, 20, 5),
    }
    tiles = {
        1: Rect(0, 0, 80, 100),
        2: Rect(50, 0, 80, 100),
        3: Rect(100, 0, 80, 100),
    }
    plan = build_plan(cracks, tiles)
    positives = [
        (tile, ann.crack)
        for tile, anns in plan.annotations.items()
        for ann in anns
        if ann.kind == POSITIVE
    ]
    assert sorted(c for _, c in positives) == [1, 2, 3]
    assert len(positives) == 3


def test_ignore_rect_is_exact_intersection():
    crack = Rect(50, 40, 100, 40)
    tiles = {
        1: Rect(0, 0, 200, 200),
        2: Rect(0, 0, 120, 60),
        3: Rect(120, 60, 100, 100),
    }
    plan = build_plan({1: crack}, tiles)
    assert plan.positive_tiles == [1]
    (a2,) = plan.annotations[2]
    assert a2.rect.to_list() == [50, 40, 70, 20]
    (a3,) = plan.annotations[3]
    assert a3.rect.to_list() == [0, 0, 30, 20]


def test_remaining_cracks_must_use_unconfirmed_tiles():
    cracks_old = {1: Rect(0, 0, 40, 10)}
    tiles = {1: Rect(0, 0, 100, 100), 2: Rect(100, 0, 100, 100)}
    snapshot = build_plan(cracks_old, tiles).annotations[1]
    assert snapshot[0].kind == POSITIVE

    # New crack #2 lies inside tile 2 only; tile 2 is still free.
    cracks_new = {1: Rect(0, 0, 40, 10), 2: Rect(120, 0, 40, 10)}
    plan = build_plan(cracks_new, tiles, pinned={1: snapshot})
    assert plan.positive_tiles == [1, 2]
    assert plan.annotations[1] == snapshot
