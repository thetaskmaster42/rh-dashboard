"""
Inline SVG chart builders for the dashboard.

Follows the same hand-rolled-SVG approach as `Production/Options/optionsuite/
render.py` and `KnowledgeBase/Options/code/render.py`: no charting library,
plain coordinate math. The difference here is these charts are embedded
*inline* in the HTML (not written as standalone .svg files), so marks can
reference CSS custom properties (`var(--series-1)`) and repaint for dark mode
without regenerating anything — the theme tokens live in `dashboard.py`.

Each render function returns `(svg_markup, interaction_data)`. The SVG carries
the static drawing; `interaction_data` is a plain dict the page's one shared
`<script>` block uses to drive the hover crosshair / tooltip — coordinate math
is computed once here in Python rather than duplicated in JS.

This module knows nothing about categories — `dashboard.py` resolves each
series/bar to a display key and a CSS color expression (`var(--series-3)`,
`var(--text-muted)`, ...) before calling in here, so the category→color
mapping lives in exactly one place.
"""
from __future__ import annotations

import math

LINE_W, LINE_H = 900, 380
LINE_PAD = dict(l=76, r=24, t=28, b=44)

BAR_W, BAR_H = 960, 360
BAR_PAD = dict(l=76, r=24, t=28, b=58)


def esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _money(v: float, compact: bool = True) -> str:
    sign = "-" if v < 0 else ""
    v = abs(v)
    if compact and v >= 1000:
        return f"{sign}${v/1000:.1f}k"
    return f"{sign}${v:,.0f}"


def _ticks(lo: float, hi: float, n: int) -> list[float]:
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / max(n, 1)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    step = min([m * mag for m in (1, 2, 2.5, 5, 10)], key=lambda s: abs(s - raw))
    start = math.floor(lo / step) * step
    out, v = [], start
    while v <= hi + 1e-9 and len(out) < 40:
        if v >= lo - 1e-9:
            out.append(round(v, 6))
        v += step
    return out


# ---------------------------------------------------------------------------
def render_line_chart(months: list[str], series: list[dict]) -> tuple[str, dict]:
    """
    `series`: [{"key": "Equity", "color": "var(--series-1)", "values": [...],
                "emphasis": False}, ...]. An `emphasis` entry (the Total
    overlay) is drawn thicker and dashed, in whatever `color` it's given
    (dashboard.py passes ink) rather than a category hue.
    """
    P = LINE_PAD
    plot_w, plot_h = LINE_W - P["l"] - P["r"], LINE_H - P["t"] - P["b"]

    if not months:
        return (f'<svg viewBox="0 0 {LINE_W} {LINE_H}" width="{LINE_W}" height="{LINE_H}">'
                 f'<text x="{LINE_W/2}" y="{LINE_H/2}" text-anchor="middle" '
                 f'class="viz-muted">no data</text></svg>'), {}

    all_vals = [v for s in series for v in s["values"]] or [0.0]
    ymin, ymax = min(0.0, min(all_vals)), max(0.0, max(all_vals))
    span = max(ymax - ymin, 1.0)
    ymin, ymax = ymin - span * 0.08, ymax + span * 0.08

    n = len(months)

    def px(i: int) -> float:
        return P["l"] if n == 1 else P["l"] + i / (n - 1) * plot_w

    def py(v: float) -> float:
        return P["t"] + plot_h - (v - ymin) / (ymax - ymin) * plot_h

    o = [f'<svg class="viz-svg linechart" viewBox="0 0 {LINE_W} {LINE_H}" '
         f'width="{LINE_W}" height="{LINE_H}" preserveAspectRatio="xMidYMid meet">']

    for v in _ticks(ymin, ymax, 5):
        y = py(v)
        o.append(f'<line x1="{P["l"]}" y1="{y:.1f}" x2="{LINE_W-P["r"]}" y2="{y:.1f}" '
                  f'class="viz-grid"/>')
        o.append(f'<text x="{P["l"]-8}" y="{y+3.5:.1f}" text-anchor="end" '
                  f'class="viz-tick">{_money(v)}</text>')

    step = max(1, n // 8)
    for i, mk in enumerate(months):
        if i % step and i != n - 1:
            continue
        x = px(i)
        o.append(f'<text x="{x:.1f}" y="{LINE_H-P["b"]+18}" text-anchor="middle" '
                  f'class="viz-tick">{mk}</text>')

    zero_y = py(0)
    o.append(f'<line x1="{P["l"]}" y1="{zero_y:.1f}" x2="{LINE_W-P["r"]}" y2="{zero_y:.1f}" '
              f'class="viz-baseline"/>')

    for s in series:
        pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(s["values"]))
        colour = s["color"]
        dash = ' stroke-dasharray="7 5"' if s.get("emphasis") else ""
        width = "3" if s.get("emphasis") else "2"
        key = esc(s["key"])
        o.append(f'<polyline class="viz-series" data-key="{key}" points="{pts}" fill="none" '
                  f'stroke="{colour}" stroke-width="{width}"{dash} stroke-linejoin="round" '
                  f'stroke-linecap="round"/>')
        lx, ly = px(n - 1), py(s["values"][-1])
        o.append(f'<circle class="viz-series" data-key="{key}" cx="{lx:.1f}" cy="{ly:.1f}" '
                  f'r="4.5" fill="{colour}" style="stroke:var(--surface-1);stroke-width:2"/>')

    # invisible per-month hit columns + a crosshair line the shared script moves
    for i, mk in enumerate(months):
        x = px(i)
        band_l = P["l"] if i == 0 else (px(i - 1) + x) / 2
        band_r = LINE_W - P["r"] if i == n - 1 else (x + px(i + 1)) / 2
        o.append(f'<rect class="viz-hit" data-i="{i}" x="{band_l:.1f}" y="{P["t"]}" '
                  f'width="{max(band_r-band_l,1):.1f}" height="{plot_h:.1f}" '
                  f'fill="transparent"/>')
    o.append(f'<line class="viz-crosshair" x1="0" y1="{P["t"]}" x2="0" '
              f'y2="{LINE_H-P["b"]}" style="display:none"/>')
    o.append("</svg>")

    interaction = {
        "kind": "line",
        "months": months,
        "x_px": [round(px(i), 1) for i in range(n)],
        "series": [{"key": s["key"], "color": s["color"],
                     "values": [round(v, 2) for v in s["values"]]}
                    for s in series],
    }
    return "\n".join(o), interaction


# ---------------------------------------------------------------------------
def render_bar_chart(categories: list[dict]) -> tuple[str, dict]:
    """`categories`: [{"key": "Equity", "color": "var(--series-1)", "value": ..., "count": ...}, ...]"""
    P = BAR_PAD
    plot_w, plot_h = BAR_W - P["l"] - P["r"], BAR_H - P["t"] - P["b"]

    vals = [c["value"] for c in categories] or [0.0]
    ymin, ymax = min(0.0, min(vals)), max(0.0, max(vals))
    span = max(ymax - ymin, 1.0)
    ymin, ymax = ymin - span * 0.12, ymax + span * 0.12

    def py(v: float) -> float:
        return P["t"] + plot_h - (v - ymin) / (ymax - ymin) * plot_h

    n = len(categories)
    band = plot_w / max(n, 1)
    bar_w = min(64.0, band * 0.5)

    o = [f'<svg class="viz-svg barchart" viewBox="0 0 {BAR_W} {BAR_H}" '
         f'width="{BAR_W}" height="{BAR_H}" preserveAspectRatio="xMidYMid meet">']

    for v in _ticks(ymin, ymax, 5):
        y = py(v)
        o.append(f'<line x1="{P["l"]}" y1="{y:.1f}" x2="{BAR_W-P["r"]}" y2="{y:.1f}" '
                  f'class="viz-grid"/>')
        o.append(f'<text x="{P["l"]-8}" y="{y+3.5:.1f}" text-anchor="end" '
                  f'class="viz-tick">{_money(v)}</text>')

    zero_y = py(0)
    o.append(f'<line x1="{P["l"]}" y1="{zero_y:.1f}" x2="{BAR_W-P["r"]}" y2="{zero_y:.1f}" '
              f'class="viz-baseline"/>')

    bars_data = []
    for i, c in enumerate(categories):
        cx = P["l"] + band * (i + 0.5)
        v = c["value"]
        top, bot = (py(v), zero_y) if v >= 0 else (zero_y, py(v))
        h = max(bot - top, 1.5)
        x = cx - bar_w / 2
        o.append(f'<rect class="viz-bar" data-key="{esc(c["key"])}" data-i="{i}" '
                  f'x="{x:.1f}" y="{top:.1f}" width="{bar_w:.1f}" height="{h:.1f}" rx="4" '
                  f'fill="{c["color"]}"/>')
        label_y = top - 8 if v >= 0 else bot + 16
        o.append(f'<text x="{cx:.1f}" y="{label_y:.1f}" text-anchor="middle" '
                  f'class="viz-value">{_money(v)}</text>')
        # long two-word names ("Dividends/Interest") wrap onto a second tspan
        # rather than overlapping the neighbouring bar's label
        key = c["key"]
        if "/" in key and len(key) > 10:
            top_word, bot_word = key.split("/", 1)
            o.append(f'<text x="{cx:.1f}" y="{BAR_H-P["b"]+17}" text-anchor="middle" '
                      f'class="viz-tick">'
                      f'<tspan x="{cx:.1f}">{esc(top_word)}/</tspan>'
                      f'<tspan x="{cx:.1f}" dy="12">{esc(bot_word)}</tspan></text>')
        else:
            o.append(f'<text x="{cx:.1f}" y="{BAR_H-P["b"]+18}" text-anchor="middle" '
                      f'class="viz-tick">{esc(key)}</text>')
        bars_data.append({"key": c["key"], "value": round(v, 2),
                           "count": c.get("count"), "note": c.get("note")})

    o.append("</svg>")
    interaction = {"kind": "bar", "bars": bars_data}
    return "\n".join(o), interaction
