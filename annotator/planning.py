"""Annotation planning.

Given one original image's numbered crack boxes and numbered slice boxes,
decide on which slices each crack is a positive example, and where the
remaining overlapping parts become "ignore" regions.

Rules implemented here:

* A crack is labelled positive on exactly one slice, and that slice must
  fully contain the crack box.
* Every other slice with a positive-area overlap with that crack gets an
  ``ignore`` annotation covering exactly the overlap, expressed in slice
  local coordinates.
* The number of slices carrying at least one positive is minimised.
* Annotations of slices confirmed before the current revision took effect
  ("pinned" slices) are frozen snapshots and are copied verbatim.  They are
  exempt from the automatic ignore rule because changing them is forbidden.
  An import is rejected with conflicts only when the new crack table makes a
  frozen positive impossible to interpret (removed/changed crack, no longer
  contained, duplicate positives) or when a crack has no unpinned slice that
  can host its positive.
"""

from __future__ import annotations

from dataclasses import dataclass

from .geometry import Rect

POSITIVE = "positive"
IGNORE = "ignore"


@dataclass(frozen=True)
class Annotation:
    crack: int
    kind: str
    rect: Rect

    def __post_init__(self) -> None:
        if self.kind not in (POSITIVE, IGNORE):
            raise ValueError(f"unknown annotation kind: {self.kind}")

    def sort_key(self) -> tuple:
        return (self.crack, self.kind, self.rect.x, self.rect.y, self.rect.w, self.rect.h)

    def to_dict(self) -> dict:
        return {"crack": self.crack, "kind": self.kind, "rect": self.rect.to_list()}


@dataclass(frozen=True)
class Conflict:
    code: str
    message: str
    crack: int | None = None
    tile: int | None = None

    def to_dict(self) -> dict:
        out: dict = {"code": self.code, "message": self.message}
        if self.crack is not None:
            out["crack"] = self.crack
        if self.tile is not None:
            out["tile"] = self.tile
        return out


class PlanConflict(Exception):
    def __init__(self, conflicts: list[Conflict]):
        super().__init__("; ".join(c.message for c in conflicts))
        self.conflicts = conflicts


@dataclass
class Plan:
    annotations: dict[int, list[Annotation]]
    positive_tiles: list[int]

    def sorted_annotation_dicts(self, tile: int) -> list[dict]:
        return [a.to_dict() for a in self.annotations.get(tile, [])]


def _minimum_cover(
    tile_numbers: list[int], masks: list[int], full: int
) -> list[int] | None:
    """Smallest subset of tiles covering every remaining crack.

    ``masks[i]`` is the bitmask of remaining cracks fully contained by
    ``tile_numbers[i]``.  Tiles are in ascending number order, so the DFS
    returns the lexicographically first minimum cover; that makes output
    deterministic and easier to review.
    """
    n = len(tile_numbers)
    suffix = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix[i] = suffix[i + 1] | masks[i]
    if suffix[0] != full:
        return None

    def dfs(start: int, left: int, covered: int, chosen: list[int]) -> list[int] | None:
        if left == 0:
            return list(chosen) if covered == full else None
        if n - start < left:
            return None
        if (covered | suffix[start]) != full:
            return None
        last = n - left
        for i in range(start, last + 1):
            result = dfs(i + 1, left - 1, covered | masks[i], chosen + [i])
            if result is not None:
                return result
        return None

    for size in range(0, n + 1):
        result = dfs(0, size, 0, [])
        if result is not None:
            return [tile_numbers[i] for i in result]
    return None


def _validate_pinned(
    cracks: dict[int, Rect],
    tiles: dict[int, Rect],
    pinned: dict[int, list[Annotation]],
    pinned_rects: dict[int, Rect],
    conflicts: list[Conflict],
) -> dict[int, int]:
    """Check frozen positives; return crack -> pinned tile holding it.

    Frozen ignores are intentionally *not* revalidated: they are history and
    must be preserved verbatim, even if the crack table changed around them.
    """
    holders: dict[int, int] = {}
    for tile_number in sorted(pinned):
        tile = tiles.get(tile_number)
        if tile is None:
            conflicts.append(
                Conflict(
                    "pinned_tile_missing",
                    f"已确认的切片 #{tile_number} 在新切片表中不存在，无法原样保留标注。",
                    tile=tile_number,
                )
            )
            continue
        frozen_rect = pinned_rects.get(tile_number)
        if frozen_rect is not None and frozen_rect != tile:
            conflicts.append(
                Conflict(
                    "pinned_tile_moved",
                    f"已确认的切片 #{tile_number} 的框从 {frozen_rect.to_list()} 变为 "
                    f"{tile.to_list()}；切片内坐标已失效，无法原样保留标注。",
                    tile=tile_number,
                )
            )
            continue
        seen: set[int] = set()
        for ann in pinned[tile_number]:
            crack_number = ann.crack
            if crack_number in seen:
                conflicts.append(
                    Conflict(
                        "pinned_duplicate_annotation",
                        f"已确认切片 #{tile_number} 对裂缝 #{crack_number} 有重复标注。",
                        crack=crack_number,
                        tile=tile_number,
                    )
                )
                continue
            seen.add(crack_number)
            if ann.kind != POSITIVE:
                continue
            crack = cracks.get(crack_number)
            if crack is None:
                conflicts.append(
                    Conflict(
                        "pinned_positive_unknown_crack",
                        f"已确认切片 #{tile_number} 的正例引用了新裂缝框表中不存在的"
                        f"裂缝 #{crack_number}，无法生成一致的标注清单。",
                        crack=crack_number,
                        tile=tile_number,
                    )
                )
                continue
            if not tile.contains(crack):
                conflicts.append(
                    Conflict(
                        "pinned_positive_outside",
                        f"已确认切片 #{tile_number} 的正例裂缝 #{crack_number} "
                        "不再完整落在该切片内。",
                        crack=crack_number,
                        tile=tile_number,
                    )
                )
                continue
            expected = crack.translated(-tile.x, -tile.y)
            if ann.rect != expected:
                conflicts.append(
                    Conflict(
                        "pinned_positive_mismatch",
                        f"已确认切片 #{tile_number} 上裂缝 #{crack_number} 的正例框"
                        "与新裂缝框表不一致，无法原样保留。",
                        crack=crack_number,
                        tile=tile_number,
                    )
                )
                continue
            other = holders.get(crack_number)
            if other is not None and other != tile_number:
                conflicts.append(
                    Conflict(
                        "pinned_duplicate_positive",
                        f"裂缝 #{crack_number} 在已确认切片 #{other} 和 #{tile_number} "
                        "上同时被标为正例。",
                        crack=crack_number,
                        tile=tile_number,
                    )
                )
            else:
                holders[crack_number] = tile_number
    return holders


def build_plan(
    cracks: dict[int, Rect],
    tiles: dict[int, Rect],
    pinned: dict[int, list[Annotation]] | None = None,
    pinned_rects: dict[int, Rect] | None = None,
) -> Plan:
    """Build a complete annotation plan or raise :class:`PlanConflict`.

    ``pinned`` maps slice numbers to their frozen annotation snapshots and
    ``pinned_rects`` records the slice rectangle each snapshot was made
    against (used to reject slices that moved or resized).
    """
    pinned = pinned or {}
    pinned_rects = pinned_rects or {}
    conflicts: list[Conflict] = []

    pinned_positive_cracks = {
        ann.crack
        for anns in pinned.values()
        for ann in anns
        if ann.kind == POSITIVE
    }
    uncoverable: set[int] = set()
    for number in sorted(cracks):
        if number in pinned_positive_cracks:
            # A pinned positive gets a more specific conflict in _validate_pinned.
            continue
        if not any(tile.contains(cracks[number]) for tile in tiles.values()):
            uncoverable.add(number)
            conflicts.append(
                Conflict(
                    "crack_uncoverable",
                    f"裂缝 #{number} 无法完整放入任何切片，无法生成正例。",
                    crack=number,
                )
            )

    holders = _validate_pinned(cracks, tiles, pinned, pinned_rects, conflicts)

    # Cracks already positive on a frozen slice keep that assignment; every
    # other crack must be hosted by a slice that has never been confirmed.
    remaining = [n for n in sorted(cracks) if n not in holders]
    unpinned_tiles = sorted(n for n in tiles if n not in pinned)
    chosen: list[int] = []
    if remaining:
        masks: list[int] = []
        usable: list[int] = []
        for tile_number in unpinned_tiles:
            mask = 0
            for index, crack_number in enumerate(remaining):
                if tiles[tile_number].contains(cracks[crack_number]):
                    mask |= 1 << index
            if mask:
                usable.append(tile_number)
                masks.append(mask)
        full = (1 << len(remaining)) - 1
        cover = _minimum_cover(usable, masks, full)
        if cover is None:
            for crack_number in remaining:
                if crack_number in uncoverable:
                    continue
                if not any(
                    tiles[t].contains(cracks[crack_number]) for t in unpinned_tiles
                ):
                    conflicts.append(
                        Conflict(
                            "crack_not_hostable",
                            f"裂缝 #{crack_number} 没有可放置正例的未确认切片"
                            "（已确认切片不允许改动）。",
                            crack=crack_number,
                        )
                    )
        else:
            chosen = cover

    if conflicts:
        raise PlanConflict(conflicts)

    assignment = dict(holders)
    for crack_number in remaining:
        for tile_number in chosen:
            if tiles[tile_number].contains(cracks[crack_number]):
                assignment[crack_number] = tile_number
                break

    annotations: dict[int, list[Annotation]] = {}
    for tile_number in sorted(tiles):
        if tile_number in pinned:
            annotations[tile_number] = sorted(
                pinned[tile_number], key=Annotation.sort_key
            )
            continue
        tile = tiles[tile_number]
        slice_annotations: list[Annotation] = []
        for crack_number in sorted(cracks):
            crack = cracks[crack_number]
            overlap = tile.intersection(crack)
            if overlap is None:
                continue
            if assignment.get(crack_number) == tile_number:
                slice_annotations.append(
                    Annotation(
                        crack_number,
                        POSITIVE,
                        crack.translated(-tile.x, -tile.y),
                    )
                )
            else:
                slice_annotations.append(
                    Annotation(
                        crack_number,
                        IGNORE,
                        overlap.translated(-tile.x, -tile.y),
                    )
                )
        annotations[tile_number] = sorted(
            slice_annotations, key=Annotation.sort_key
        )

    positive_tiles = sorted(
        {
            tile_number
            for tile_number, anns in annotations.items()
            if any(a.kind == POSITIVE for a in anns)
        }
    )
    return Plan(annotations=annotations, positive_tiles=positive_tiles)


def canonical_annotations(annotations: list[Annotation]) -> list[dict]:
    return [a.to_dict() for a in sorted(annotations, key=Annotation.sort_key)]


def carried_over_overlaps(
    cracks: dict[int, Rect],
    tiles: dict[int, Rect],
    pinned: dict[int, list[Annotation]],
) -> list[dict]:
    """Non-blocking warnings about overlaps left unlabelled on frozen slices.

    Frozen slices are copied verbatim, so a crack that overlaps one but is
    hosted elsewhere gains no ``ignore`` there even though the automatic rule
    would add one.  Import still succeeds (preservation is mandatory), but
    reviewers should know the frozen slice has an unlabelled overlap.
    """
    warnings: list[dict] = []
    for tile_number in sorted(pinned):
        tile = tiles.get(tile_number)
        if tile is None:
            continue
        seen = {a.crack for a in pinned[tile_number]}
        for crack_number in sorted(cracks):
            if crack_number in seen:
                continue
            if tile.intersection(cracks[crack_number]) is None:
                continue
            warnings.append(
                {
                    "code": "frozen_overlap_unlabelled",
                    "tile": tile_number,
                    "crack": crack_number,
                    "message": (
                        f"已确认切片 #{tile_number} 与裂缝 #{crack_number} 重叠，"
                        "但为保持原标注未新增忽略区域。"
                    ),
                }
            )
    return warnings
