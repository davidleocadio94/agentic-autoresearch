"""ASCII drawing for the hot-and-cold target game."""

from __future__ import annotations

import math


_W = 61
_H = 45
_CX = _W // 2
_CY = 28
_RX = 28
_RY = 13


def _blank_grid() -> list[list[str]]:
    return [[" " for _ in range(_W)] for _ in range(_H)]


def _put(grid: list[list[str]], x: int, y: int, ch: str) -> None:
    if 0 <= y < _H and 0 <= x < _W:
        grid[y][x] = ch


def _put_str(grid: list[list[str]], x: int, y: int, s: str) -> None:
    for i, ch in enumerate(s):
        _put(grid, x + i, y, ch)


def _draw_ellipse_outline(
    grid: list[list[str]], cx: int, cy: int, rx: int, ry: int, ch: str
) -> None:
    for y in range(cy - ry - 1, cy + ry + 2):
        for x in range(cx - rx - 1, cx + rx + 2):
            if 0 <= y < _H and 0 <= x < _W:
                dx = (x - cx) / rx
                dy = (y - cy) / ry
                d = dx * dx + dy * dy
                if 0.92 <= d <= 1.08:
                    _put(grid, x, y, ch)


def _draw_line(
    grid: list[list[str]], x0: int, y0: int, x1: int, y1: int, ch: str
) -> None:
    steps = max(abs(x1 - x0), abs(y1 - y0)) * 2 + 1
    for i in range(steps + 1):
        t = i / steps
        x = round(x0 + (x1 - x0) * t)
        y = round(y0 + (y1 - y0) * t)
        if grid[y][x] == " ":
            _put(grid, x, y, ch)


def _hour_position(hour: int, rx: int, ry: int) -> tuple[int, int]:
    angle = math.radians(-90 + hour * 30)
    x = _CX + int(round(math.cos(angle) * (rx - 4)))
    y = _CY + int(round(math.sin(angle) * (ry - 2)))
    return x, y


def _draw_hand(
    grid: list[list[str]], hour: float, length_frac: float, ch: str
) -> None:
    angle = math.radians(-90 + hour * 30)
    end_x = _CX + int(round(math.cos(angle) * (_RX - 5) * length_frac))
    end_y = _CY + int(round(math.sin(angle) * (_RY - 3) * length_frac))
    _draw_line(grid, _CX, _CY, end_x, end_y, ch)


def _draw_minute_ticks(grid: list[list[str]]) -> None:
    inner_rx = _RX - 2
    inner_ry = _RY - 1
    for m in range(60):
        if m % 5 == 0:
            continue
        angle = math.radians(-90 + m * 6)
        x = _CX + int(round(math.cos(angle) * inner_rx))
        y = _CY + int(round(math.sin(angle) * inner_ry))
        if 0 <= y < _H and 0 <= x < _W and grid[y][x] in (" ", "."):
            _put(grid, x, y, "'" if abs(math.sin(angle)) > 0.5 else ".")


def _draw_subdial(grid: list[list[str]]) -> None:
    sx, sy = _CX, _CY + 6
    rx, ry = 5, 2
    for y in range(sy - ry - 1, sy + ry + 2):
        for x in range(sx - rx - 1, sx + rx + 2):
            if 0 <= y < _H and 0 <= x < _W:
                dx = (x - sx) / rx
                dy = (y - sy) / ry
                d = dx * dx + dy * dy
                if 0.85 <= d <= 1.15 and grid[y][x] == " ":
                    _put(grid, x, y, "-" if abs(dy) > abs(dx) else "|")
    # sub-dial markers and tiny seconds hand
    _put(grid, sx, sy - ry, "'")
    _put(grid, sx, sy + ry, ".")
    _put(grid, sx - rx, sy, "-")
    _put(grid, sx + rx, sy, "-")
    _put(grid, sx + 2, sy - 1, "/")
    _put(grid, sx, sy, "*")


def _build() -> str:
    grid = _blank_grid()

    _draw_ellipse_outline(grid, _CX, _CY, _RX, _RY, "#")
    _draw_ellipse_outline(grid, _CX, _CY, _RX - 1, _RY - 1, "#")
    _draw_ellipse_outline(grid, _CX, _CY, _RX - 4, _RY - 2, ".")

    _draw_minute_ticks(grid)

    labels = {
        12: "XII",
        1: "I",
        2: "II",
        3: "III",
        4: "IV",
        5: "V",
        6: "VI",
        7: "VII",
        8: "VIII",
        9: "IX",
        10: "X",
        11: "XI",
    }
    for hour, label in labels.items():
        x, y = _hour_position(hour, _RX, _RY)
        x -= len(label) // 2
        _put_str(grid, x, y, label)

    _draw_subdial(grid)

    _draw_hand(grid, hour=10, length_frac=0.55, ch="\\")
    _draw_hand(grid, hour=2, length_frac=0.80, ch="/")

    _put(grid, _CX, _CY, "o")

    knob_top_y = _CY - _RY - 1
    _put_str(grid, _CX - 2, knob_top_y, "[oOo]")

    # Long linked tether trailing upward, curving to the side, ending in a T-bar terminal.
    base_y = knob_top_y - 1
    path = [
        (0, 0), (1, -1), (2, -2), (3, -3), (4, -4),
        (4, -5), (3, -6), (2, -7), (1, -8), (0, -9),
        (-1, -10), (-2, -11), (-3, -12),
    ]
    glyphs = "oOoOoOoOoOoOo"
    for (dx, dy), g in zip(path, glyphs):
        _put(grid, _CX + dx, base_y + dy, g)

    # T-bar terminal at the end of the tether
    end_dx, end_dy = path[-1]
    tip_x = _CX + end_dx
    tip_y = base_y + end_dy - 1
    _put_str(grid, tip_x - 2, tip_y, "=T=")
    _put(grid, tip_x, tip_y - 1, "|")

    return "\n".join("".join(row).rstrip() for row in grid)


def draw() -> str:
    return "\n" + _build() + "\n"
