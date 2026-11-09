"""Squarified Treemap 布局（Bruls, Huizing, van Wijk 2000），纯计算，不依赖界面库。"""
from __future__ import annotations

Rect = tuple[float, float, float, float]


def _worst(row: list[float], side: float) -> float:
    total = sum(row)
    if total <= 0 or side <= 0:
        return float("inf")
    side2 = side * side
    total2 = total * total
    return max(side2 * max(row) / total2, total2 / (side2 * min(row)))


def squarify(values: list[float], x: float, y: float, w: float, h: float) -> list[Rect]:
    """把 values 按面积比例铺满 (x, y, w, h)，返回与输入顺序一一对应的矩形。

    输入按从大到小排序时长宽比最好；值 <= 0 的项返回零面积矩形。
    """
    out: list[Rect] = [(x, y, 0.0, 0.0)] * len(values)
    order = [i for i, v in enumerate(values) if v > 0]
    total = sum(values[i] for i in order)
    if not order or total <= 0 or w <= 0 or h <= 0:
        return out
    scale = w * h / total
    areas = [values[i] * scale for i in order]
    i = 0
    n = len(areas)
    while i < n:
        side = min(w, h)
        row = [areas[i]]
        j = i + 1
        worst = _worst(row, side)
        while j < n:
            candidate = row + [areas[j]]
            cand_worst = _worst(candidate, side)
            if cand_worst > worst:
                break
            row = candidate
            worst = cand_worst
            j += 1
        row_total = sum(row)
        if w >= h:
            col_w = row_total / h if h else 0.0
            cy = y
            for k, area in enumerate(row):
                rh = area / col_w if col_w else 0.0
                out[order[i + k]] = (x, cy, col_w, rh)
                cy += rh
            x += col_w
            w -= col_w
        else:
            row_h = row_total / w if w else 0.0
            cx = x
            for k, area in enumerate(row):
                rw = area / row_h if row_h else 0.0
                out[order[i + k]] = (cx, y, rw, row_h)
                cx += rw
            y += row_h
            h -= row_h
        i = j
    return out
