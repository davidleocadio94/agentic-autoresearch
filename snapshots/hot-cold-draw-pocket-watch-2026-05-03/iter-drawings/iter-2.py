"""ASCII drawing for the hot-and-cold target game.

Iter 2 hypothesis: a single large, centered, geometrically perfect circle.
Minimal surrounding detail so the silhouette is unambiguous and the judge
can latch onto the archetypal shape. ~30 chars wide.
"""

from __future__ import annotations


def _build_circle(radius: int = 14) -> str:
    """Procedurally draw a filled-outline ASCII circle.

    Uses a 2:1 character aspect ratio (chars are ~twice as tall as wide)
    so the rendered shape is visually round, not an ellipse.
    """
    diameter = radius * 2 + 1
    rows: list[str] = []
    for y in range(-radius, radius + 1):
        line_chars: list[str] = []
        for x in range(-diameter, diameter + 1):
            # Stretch x by 0.5 to compensate for tall character cells.
            dx = x * 0.5
            dy = y
            dist_sq = dx * dx + dy * dy
            r_sq = radius * radius
            # Outline band: pixels close to the circle boundary.
            if r_sq - radius <= dist_sq <= r_sq + radius:
                line_chars.append("#")
            else:
                line_chars.append(" ")
        rows.append("".join(line_chars).rstrip())
    return "\n".join(rows)


def draw() -> str:
    circle = _build_circle(radius=14)
    return "\n" + circle + "\n"
