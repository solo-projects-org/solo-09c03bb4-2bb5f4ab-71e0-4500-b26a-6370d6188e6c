"""Integer-pixel rectangle helpers.

All boxes handled by the application are axis-aligned integer-pixel
rectangles.  A rectangle is *valid* only when it has positive width and
height; empty intersections are therefore represented by ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    def __post_init__(self) -> None:
        if self.w <= 0 or self.h <= 0:
            raise ValueError("rect width and height must be positive")

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def area(self) -> int:
        return self.w * self.h

    def contains(self, other: "Rect") -> bool:
        """True when *other* lies entirely inside this rectangle.

        Touching an edge is allowed; the whole box is still inside.
        """
        return (
            self.x <= other.x
            and self.y <= other.y
            and other.right <= self.right
            and other.bottom <= self.bottom
        )

    def intersection(self, other: "Rect") -> "Rect | None":
        """Return the positive-area intersection, or ``None``."""
        left = max(self.x, other.x)
        top = max(self.y, other.y)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return None
        return Rect(left, top, right - left, bottom - top)

    def translated(self, dx: int, dy: int) -> "Rect":
        return Rect(self.x + dx, self.y + dy, self.w, self.h)

    def to_list(self) -> list[int]:
        return [self.x, self.y, self.w, self.h]

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}
