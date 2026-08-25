"""
Assembles the single self-contained HTML dashboard page.

Chart marks are drawn server-side in `render.py`; this module owns the page
shell — theme tokens, the Summary income statement, the open-positions table,
stat tiles, legend/filter chips, the transaction table, and the one small
vanilla-JS block driving hover tooltips and the category filter. No external
CSS, JS, or font: the file opens standalone in a browser, offline, with
nothing phoned home (these are real account numbers for whoever runs this).

Category -> colour resolution lives here (`category_color`/`display_key`) so
`render.py` stays generic. The two fallback categories (Debits/Credits) fold
into one muted "Other" swatch everywhere, because the eight real categories
already spend the categorical palette's validated eight-slot ceiling.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .assets import FAVICON_DATA_URI
from .metrics import Metrics
from .model import (CATEGORY_ORDER, FALLBACK_CATEGORIES, INCOME_CATEGORIES,
                    PRIMARY_CATEGORIES, TRANSFER_CATEGORIES, Category)
from .positions import PositionsResult
from .render import esc, render_bar_chart, render_line_chart

_SLOT_VAR = {
    Category.EQUITY: "--series-1",
    Category.OPTIONS: "--series-2",
    Category.DIVIDENDS_INTEREST: "--series-3",
    Category.FEES: "--series-4",
    Category.MARGIN: "--series-5",
    Category.GOLD: "--series-6",
    Category.DEPOSITS: "--series-7",
    Category.WITHDRAW: "--series-8",
}
OTHER_COLOR = "var(--text-muted)"
# Corporate actions carry no cash, so they never appear in a chart — this
# colour is only ever a table/legend badge, not a data-bearing mark, which is
# why it can sit outside the validated eight-slot categorical palette.
CORPORATE_ACTION_COLOR = "var(--text-secondary)"
TOTAL_COLOR = "var(--text-primary)"
OTHER_KEY = "Other"


def category_color(cat: Category) -> str:
    if cat is Category.CORPORATE_ACTION:
        return CORPORATE_ACTION_COLOR
    var = _SLOT_VAR.get(cat)
    return f"var({var})" if var else OTHER_COLOR


def display_key(cat: Category) -> str:
    """Label used in charts/legend/table filtering — the two fallback
    categories both collapse to "Other"."""
    return OTHER_KEY if cat in FALLBACK_CATEGORIES else cat.value


CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7;
  --surface-1: #fcfcfb;
  --surface-2: #f2f1ed;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --text-muted: #898781;
  --grid: #e1e0d9;
  --baseline: #c3c2b7;
  --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6;
  --series-2: #eb6834;
  --series-3: #1baf7a;
  --series-4: #eda100;
  --series-5: #e87ba4;
  --series-6: #008300;
  --series-7: #4a3aa7;
  --series-8: #e34948;
  --good-text: #006300;
  --bad-text: #d03b3b;
  --warn-bg: #fff4e0;
  --warn-border: #fab219;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page: #0d0d0d;
    --surface-1: #1a1a19;
    --surface-2: #232322;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted: #898781;
    --grid: #2c2c2a;
    --baseline: #383835;
    --border: rgba(255,255,255,0.10);
    --series-1: #3987e5;
    --series-2: #d95926;
    --series-3: #199e70;
    --series-4: #c98500;
    --series-5: #d55181;
    --series-6: #008300;
    --series-7: #9085e9;
    --series-8: #e66767;
    --good-text: #0ca30c;
    --bad-text: #d03b3b;
    --warn-bg: #2a2210;
    --warn-border: #fab219;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}
.page { max-width: 1180px; margin: 0 auto; padding: 32px 20px 64px; }
header h1 { font-size: 22px; margin: 0 0 4px; }
header .meta { color: var(--text-secondary); font-size: 13px; margin: 0; }

.callout {
  margin: 18px 0; padding: 12px 16px; border-radius: 8px;
  background: var(--warn-bg); border: 1px solid var(--warn-border);
  color: var(--text-primary); font-size: 13px; line-height: 1.5;
}
.callout summary { cursor: pointer; font-weight: 600; }
.callout ul { margin: 8px 0 0; padding-left: 20px; }
.callout li { margin: 4px 0; }

.card {
  background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 12px; padding: 20px 22px; margin: 16px 0; position: relative;
}
.card h2 { font-size: 15px; margin: 0 0 2px; }
.card .sub-head { font-size: 12px; color: var(--text-secondary); margin: 0 0 14px; }

.summary-heroes { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }
@media (max-width: 700px) { .summary-heroes { grid-template-columns: 1fr; } }
.verdict { font-size: 14.5px; line-height: 1.65; margin: 0 0 18px; }
.verdict a { color: inherit; }

.calc-wrap { display: flex; flex-wrap: wrap; gap: 28px; align-items: flex-start; }
.calc-block { flex: 1 1 320px; min-width: 300px; }
.calc-block h3 {
  font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
  color: var(--text-muted); margin: 0 0 6px; font-weight: 700;
}
.calc-table { border-collapse: collapse; font-size: 13px; width: 100%; }
.calc-table td { padding: 5px 8px; }
.calc-table td:first-child { color: var(--text-secondary); }
.calc-table td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.calc-table tr.subtotal td {
  border-top: 2px solid var(--text-primary); padding-top: 8px;
  font-weight: 700; color: var(--text-primary);
}
.calc-table tr.excluded td:first-child { font-style: italic; }
.calc-table .swatch {
  width: 8px; height: 8px; border-radius: 2px; display: inline-block; margin-right: 7px;
}
.summary-note {
  font-size: 12px; color: var(--text-secondary); line-height: 1.6;
  margin: 16px 0 0; padding-top: 14px; border-top: 1px solid var(--border);
}

.kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 20px 0; }
@media (max-width: 900px) { .kpi-row { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 520px) { .kpi-row { grid-template-columns: 1fr; } }
.stat-tile {
  background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px 16px;
}
.stat-tile.inset { background: var(--surface-2); }
.stat-tile .label { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
.stat-tile .value { font-size: 22px; font-weight: 600; }
.stat-tile .sub { font-size: 11.5px; color: var(--text-muted); margin-top: 5px; line-height: 1.45; }
.stat-tile.hero .value { font-size: 30px; }
.pos { color: var(--good-text); }
.neg { color: var(--bad-text); }
.neutral { color: var(--text-primary); }

.filter-row { display: flex; flex-wrap: wrap; gap: 8px; margin: 24px 0 8px; }
.chip {
  display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px;
  border-radius: 999px; border: 1px solid var(--border); background: var(--surface-1);
  font-size: 12.5px; color: var(--text-primary); cursor: pointer; user-select: none;
  transition: opacity .12s ease;
}
.chip .swatch { width: 10px; height: 10px; border-radius: 3px; flex: none; }
.chip[data-active="false"] { opacity: 0.4; }
.chip:focus-visible { outline: 2px solid var(--series-1); outline-offset: 2px; }

.chart-wrap { width: 100%; }
.viz-svg { width: 100%; height: auto; display: block; }
.viz-grid { stroke: var(--grid); stroke-width: 1; }
.viz-baseline { stroke: var(--baseline); stroke-width: 1.4; }
.viz-tick { font-size: 10.5px; fill: var(--text-muted); }
.viz-value { font-size: 11px; fill: var(--text-secondary); font-weight: 600; }
.viz-muted { fill: var(--text-muted); font-size: 12px; }
.viz-crosshair { stroke: var(--baseline); stroke-width: 1; pointer-events: none; }
.viz-hit { cursor: crosshair; }
.viz-bar { cursor: pointer; }
.viz-bar:hover, .viz-bar:focus { opacity: 0.85; }

.tooltip {
  position: absolute; pointer-events: none; z-index: 5;
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
  padding: 8px 10px; font-size: 12px; box-shadow: 0 4px 14px rgba(0,0,0,0.18);
  min-width: 160px;
}
.tooltip .tt-title { font-weight: 600; margin-bottom: 4px; color: var(--text-primary); }
.tooltip .tt-row { display: flex; align-items: center; gap: 10px; justify-content: space-between; }
.tooltip .tt-key { display: flex; align-items: center; gap: 5px; color: var(--text-secondary); }
.tooltip .tt-key .line { width: 10px; height: 2px; flex: none; }
.tooltip .tt-val { font-weight: 600; color: var(--text-primary); font-variant-numeric: tabular-nums; }

table.data { width: 100%; border-collapse: collapse; font-size: 12.5px; }
table.data thead th {
  text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .02em;
  color: var(--text-muted); border-bottom: 1px solid var(--border); padding: 8px;
  position: sticky; top: 0; background: var(--surface-1);
}
table.data thead th.num { text-align: right; }
table.data tbody td { padding: 6px 8px; border-bottom: 1px solid var(--grid); }
table.data tbody td.num { text-align: right; font-variant-numeric: tabular-nums; }
table.data tbody tr.hidden-row { display: none; }
table.data tfoot td {
  padding: 9px 8px; border-top: 2px solid var(--text-primary);
  font-weight: 700; font-variant-numeric: tabular-nums;
}
table.data tfoot td.num { text-align: right; }
.cat-badge { display: inline-flex; align-items: center; gap: 5px; font-size: 11.5px; color: var(--text-secondary); }
.cat-badge .swatch { width: 8px; height: 8px; border-radius: 2px; flex: none; }
.table-scroll { max-height: 480px; overflow: auto; }
.fallback-flag { color: var(--text-muted); font-weight: 700; cursor: help; }
.ticker { font-weight: 600; }

footer {
  margin-top: 32px; padding-top: 16px; border-top: 1px solid var(--border);
  color: var(--text-secondary); font-size: 12.5px; line-height: 1.65;
}
footer h3 { font-size: 13px; color: var(--text-primary); margin: 18px 0 6px; }
footer ul { margin: 4px 0; padding-left: 20px; }
footer li { margin: 3px 0; }
footer code { font-size: 11.5px; }
"""

JS = """
(function () {
  document.querySelectorAll('[data-chart]').forEach(function (card) {
    var data = JSON.parse(card.querySelector('script[type="application/json"]').textContent);
    var svg = card.querySelector('svg');
    var tooltip = card.querySelector('.tooltip');
    if (data.kind === 'line') lineInteraction(card, svg, tooltip, data);
    if (data.kind === 'bar') barInteraction(card, svg, tooltip, data);
  });

  function showTooltip(tooltip, card, x, y, rows, title) {
    tooltip.replaceChildren();
    var t = document.createElement('div');
    t.className = 'tt-title';
    t.textContent = title;
    tooltip.appendChild(t);
    rows.forEach(function (r) {
      var row = document.createElement('div');
      row.className = 'tt-row';
      var key = document.createElement('div');
      key.className = 'tt-key';
      if (r.color) {
        var sw = document.createElement('span');
        sw.className = 'line';
        sw.style.background = r.color;
        key.appendChild(sw);
      }
      var label = document.createElement('span');
      label.textContent = r.label;
      key.appendChild(label);
      var val = document.createElement('div');
      val.className = 'tt-val';
      val.textContent = r.value;
      row.appendChild(key);
      row.appendChild(val);
      tooltip.appendChild(row);
    });
    tooltip.hidden = false;
    var rect = card.getBoundingClientRect();
    var left = x - rect.left + 16;
    if (left + 200 > rect.width) left = x - rect.left - 206;
    tooltip.style.left = Math.max(left, 4) + 'px';
    tooltip.style.top = Math.max(y - rect.top - 10, 4) + 'px';
  }

  function fmtMoney(v) {
    return (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString(undefined,
      { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function activeKeys() {
    var active = {};
    document.querySelectorAll('.chip[data-key]').forEach(function (chip) {
      active[chip.getAttribute('data-key')] = chip.getAttribute('data-active') !== 'false';
    });
    return active;
  }

  function applyFilter() {
    var active = activeKeys();
    document.querySelectorAll('[data-chart] [data-key]').forEach(function (el) {
      el.style.display = active[el.getAttribute('data-key')] === false ? 'none' : '';
    });
    document.querySelectorAll('#txn-table tbody tr[data-key]').forEach(function (tr) {
      tr.classList.toggle('hidden-row', active[tr.getAttribute('data-key')] === false);
    });
  }

  document.querySelectorAll('.chip[data-key]').forEach(function (chip) {
    function toggle() {
      var on = chip.getAttribute('data-active') !== 'false';
      chip.setAttribute('data-active', on ? 'false' : 'true');
      chip.setAttribute('aria-checked', on ? 'false' : 'true');
      applyFilter();
    }
    chip.addEventListener('click', toggle);
    chip.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    });
  });

  function lineInteraction(card, svg, tooltip, data) {
    var crosshair = svg.querySelector('.viz-crosshair');
    svg.querySelectorAll('.viz-hit').forEach(function (hit) {
      function show(evt) {
        var i = parseInt(hit.getAttribute('data-i'), 10);
        crosshair.setAttribute('x1', data.x_px[i]);
        crosshair.setAttribute('x2', data.x_px[i]);
        crosshair.style.display = '';
        var active = activeKeys();
        var rows = data.series
          .filter(function (s) { return active[s.key] !== false; })
          .map(function (s) {
            return { label: s.key, value: fmtMoney(s.values[i]), color: s.color };
          });
        showTooltip(tooltip, card, evt.clientX, evt.clientY, rows, data.months[i]);
      }
      hit.addEventListener('pointermove', show);
      hit.addEventListener('pointerenter', show);
      hit.addEventListener('pointerleave', function () {
        tooltip.hidden = true;
        crosshair.style.display = 'none';
      });
    });
  }

  function barInteraction(card, svg, tooltip, data) {
    svg.querySelectorAll('.viz-bar').forEach(function (bar) {
      function show(evt) {
        var b = data.bars[parseInt(bar.getAttribute('data-i'), 10)];
        var rows = [{ label: b.note || 'contribution', value: fmtMoney(b.value) }];
        if (b.count !== null && b.count !== undefined) {
          rows.push({ label: 'transactions', value: String(b.count) });
        }
        showTooltip(tooltip, card, evt.clientX, evt.clientY, rows, b.key);
      }
      bar.setAttribute('tabindex', '0');
      bar.addEventListener('pointermove', show);
      bar.addEventListener('pointerenter', show);
      bar.addEventListener('pointerleave', function () { tooltip.hidden = true; });
      bar.addEventListener('focus', function () {
        var r = bar.getBoundingClientRect();
        show({ clientX: r.left + r.width / 2, clientY: r.top });
      });
      bar.addEventListener('blur', function () { tooltip.hidden = true; });
    });
  }
})();
"""


# --------------------------------------------------------------------------
# small formatting helpers
def _money(v: float) -> str:
    return f"{'-' if v < 0 else ''}${abs(v):,.2f}"


def _signed_money(v: float) -> str:
    """Explicit + on gains — an income statement reads better when the sign of
    every line is unambiguous."""
    return f"{'-' if v < 0 else '+'}${abs(v):,.2f}"


def _sign_class(v: float) -> str:
    return "pos" if v >= 0 else "neg"


def _plural(n: float, word: str) -> str:
    return f"{n:g} {word}" if abs(n - 1) < 1e-9 else f"{n:g} {word}s"


def _stat_tile(label: str, value_html: str, sub: str, hero: bool = False,
               swatch: str | None = None, inset: bool = False) -> str:
    cls = "stat-tile" + (" hero" if hero else "") + (" inset" if inset else "")
    dot = (f'<span style="display:inline-block;width:8px;height:8px;border-radius:2px;'
           f'background:{swatch};margin-right:6px"></span>' if swatch else "")
    return (f'<div class="{cls}"><div class="label">{dot}{esc(label)}</div>'
            f'{value_html}<div class="sub">{esc(sub)}</div></div>')


def _money_tile(label: str, value: float, sub: str, hero: bool = False,
                swatch: str | None = None, neutral: bool = False,
                inset: bool = False, signed: bool = False) -> str:
    cls = "neutral" if neutral else _sign_class(value)
    text = _signed_money(value) if signed else _money(value)
    return _stat_tile(label, f'<div class="value {cls}">{text}</div>', sub,
                      hero=hero, swatch=swatch, inset=inset)


def _calc_row(label: str, value: float, color: str | None = None,
              subtotal: bool = False, excluded: bool = False,
              signed: bool = True) -> str:
    dot = f'<span class="swatch" style="background:{color}"></span>' if color else ""
    classes = " ".join(c for c in ("subtotal" if subtotal else "",
                                    "excluded" if excluded else "") if c)
    attr = f' class="{classes}"' if classes else ""
    text = _signed_money(value) if signed else _money(value)
    return (f'<tr{attr}><td>{dot}{esc(label)}</td>'
            f'<td class="num {_sign_class(value)}">{text}</td></tr>')


def _verdict_words(net_income: float) -> tuple[str, str]:
    if net_income > 0.005:
        return "pos", "a net profit"
    if net_income < -0.005:
        return "neg", "a net loss"
    return "neutral", "exactly breakeven"


# --------------------------------------------------------------------------
def _summary_section(m: Metrics) -> str:
    date_range = (f"{m.date_range[0]} to {m.date_range[1]}") if m.date_range else "no data"
    cls, phrase = _verdict_words(m.net_income)

    income_rows = []
    for cat in INCOME_CATEGORIES:
        s = m.categories[cat]
        label = f"{cat.value} — realized" if s.is_lot_matched else cat.value
        income_rows.append(_calc_row(label, s.income_total, category_color(cat)))
    if m.other_count:
        income_rows.append(_calc_row(
            f"Other (unclassified, {m.other_count})", m.other_income, OTHER_COLOR))
    income_rows.append(_calc_row("Net income", m.net_income, subtotal=True))

    cash_rows = [_calc_row("Net income", m.net_income)]
    if m.open_equity_cost_basis:
        cash_rows.append(_calc_row(
            f"Cash into open equity ({_plural(m.open_shares, 'share')})",
            -m.open_equity_cost_basis, category_color(Category.EQUITY), excluded=True))
    if abs(m.open_options_net_cash) > 0.005:
        cash_rows.append(_calc_row("Cash from open options",
                                    m.open_options_net_cash,
                                    category_color(Category.OPTIONS), excluded=True))
    for cat in TRANSFER_CATEGORIES:
        s = m.categories[cat]
        if s.count:
            cash_rows.append(_calc_row(cat.value, s.cash_total,
                                        category_color(cat), excluded=True))
    if abs(m.corporate_action_cash) > 0.005:
        cash_rows.append(_calc_row(
            "Corporate action cash", m.corporate_action_cash,
            category_color(Category.CORPORATE_ACTION), excluded=True))
    cash_rows.append(_calc_row("Total cash movement", m.total_cash_movement, subtotal=True))

    open_note = ""
    if m.open_equity_cost_basis:
        open_note = (
            f' The {_money(m.open_equity_cost_basis)} spent on the '
            f'{_plural(m.open_shares, "share")} still held is deliberately '
            f'<em>not</em> counted as a loss — that cash became stock, and it '
            f'stays out of net income until those shares are sold. See '
            f'<a href="#open-positions">Open positions</a>.')

    verdict = (
        f'<p class="verdict">Over <strong>{esc(date_range)}</strong> this account made '
        f'<strong class="{cls}">{_money(m.net_income)}</strong> — {phrase} on closed '
        f'trades, dividends and interest, after fees, margin interest and '
        f'subscriptions.{open_note}</p>')

    return f"""
  <section class="card" id="summary">
    <h2>Summary</h2>
    <p class="sub-head">Net income counts realized results only. Open positions and
      bank transfers are reported separately below, because neither is a gain or a loss.</p>
    <div class="summary-heroes">
      {_money_tile("Net income", m.net_income,
                    "Realized trading P&L + dividends/interest - fees - margin - Gold",
                    hero=True)}
      {_money_tile("Open equity positions", m.open_equity_cost_basis,
                    f"{_plural(m.open_shares, 'share')} held, valued at what they cost "
                    f"(no live market price in a statement export)",
                    hero=True, neutral=True)}
    </div>
    {verdict}
    <div class="calc-wrap">
      <div class="calc-block">
        <h3>How net income is calculated</h3>
        <table class="calc-table"><tbody>{"".join(income_rows)}</tbody></table>
      </div>
      <div class="calc-block">
        <h3>Reconciled to cash that moved</h3>
        <table class="calc-table"><tbody>{"".join(cash_rows)}</tbody></table>
      </div>
    </div>
    <p class="summary-note">The right-hand column proves the split adds up: net income,
      plus the cash parked in open positions, plus money moved to and from the bank,
      equals every dollar that crossed the account. Italic lines are the ones excluded
      from net income.</p>
  </section>"""


def _open_positions_section(m: Metrics, positions: PositionsResult) -> str:
    equity = positions.equity_holdings
    options = positions.option_holdings
    if not equity and not options:
        return """
  <section class="card" id="open-positions">
    <h2>Open positions</h2>
    <p class="sub-head">No open positions — every share and contract bought in these
      statements was also sold, so all of it is realized in net income above.</p>
  </section>"""

    rows = []
    for h in equity:
        rows.append(
            f'<tr><td class="ticker">{esc(h.instrument or h.key)}</td>'
            f'<td class="num">{h.quantity:g}</td>'
            f'<td class="num">{_money(h.avg_price)}</td>'
            f'<td class="num">{_money(h.cost_basis)}</td>'
            f'<td>{h.first_opened.isoformat() if h.first_opened else ""}</td></tr>')
    equity_table = ""
    if equity:
        equity_table = f"""
    <table class="data">
      <thead><tr>
        <th>Ticker</th><th class="num">Shares held</th><th class="num">Avg cost/share</th>
        <th class="num">Cost basis</th><th>First bought</th>
      </tr></thead>
      <tbody>{"".join(rows)}</tbody>
      <tfoot><tr>
        <td>Total ({len(equity)} {"position" if len(equity) == 1 else "positions"})</td>
        <td class="num">{m.open_shares:g}</td><td class="num"></td>
        <td class="num">{_money(m.open_equity_cost_basis)}</td><td></td>
      </tr></tfoot>
    </table>"""

    option_table = ""
    if options:
        orows = []
        for h in options:
            held = h.net_credit if h.is_credit_position else h.cost_basis
            side = "credit received" if h.is_credit_position else "debit paid"
            orows.append(
                f'<tr><td class="ticker">{esc(h.key)}</td>'
                f'<td class="num">{h.quantity:g}</td>'
                f'<td class="num">{_money(held)}</td><td>{esc(side)}</td>'
                f'<td>{h.first_opened.isoformat() if h.first_opened else ""}</td></tr>')
        option_table = f"""
    <h3 style="font-size:12.5px;margin:20px 0 6px">Open option contracts</h3>
    <table class="data">
      <thead><tr>
        <th>Contract</th><th class="num">Contracts</th><th class="num">Net cash</th>
        <th>Direction</th><th>First opened</th>
      </tr></thead>
      <tbody>{"".join(orows)}</tbody>
    </table>"""

    return f"""
  <section class="card" id="open-positions">
    <h2>Open positions</h2>
    <p class="sub-head">Shares and contracts with no matching sale — bought inside these
      statements and still held at the end of the window. Valued at cost, never at a
      live market price.</p>
    {equity_table}{option_table}
  </section>"""


def _chart_card(title: str, sub: str, svg: str, interaction: dict, chart_id: str) -> str:
    return (
        f'<div class="card" data-chart>'
        f'<h2>{esc(title)}</h2><p class="sub-head">{esc(sub)}</p>'
        f'<div class="chart-wrap">{svg}</div>'
        f'<div class="tooltip" id="tt-{chart_id}" hidden></div>'
        f'<script type="application/json">{json.dumps(interaction)}</script>'
        f'</div>')


def _legend_chips(include_other: bool) -> str:
    parts, seen = [], set()
    for c in CATEGORY_ORDER:
        key = display_key(c)
        if key in seen or (key == OTHER_KEY and not include_other):
            continue
        seen.add(key)
        parts.append(
            f'<div class="chip" data-key="{esc(key)}" data-active="true" '
            f'role="checkbox" aria-checked="true" tabindex="0">'
            f'<span class="swatch" style="background:{category_color(c)}"></span>'
            f'{esc(key)}</div>')
    parts.append(
        f'<div class="chip" data-key="Net income" data-active="true" role="checkbox" '
        f'aria-checked="true" tabindex="0"><span class="swatch" '
        f'style="background:{TOTAL_COLOR}"></span>Net income</div>')
    return "\n".join(parts)


def _transactions_table(classified) -> str:
    rows = sorted(classified, key=lambda c: (c.txn.activity_date, c.txn.source_file,
                                              c.txn.row_index), reverse=True)
    body = []
    for c in rows:
        t = c.txn
        flag = ('<span class="fallback-flag" title="unrecognised trans code — '
                'bucketed by cash sign">&nbsp;†</span>') if c.fallback else ""
        body.append(
            f'<tr data-key="{esc(display_key(c.category))}">'
            f'<td>{t.activity_date.isoformat()}</td>'
            f'<td class="ticker">{esc(t.instrument)}</td>'
            f'<td>{esc(t.description)}</td>'
            f'<td>{esc(t.trans_code)}{flag}</td>'
            f'<td class="num">{"" if t.quantity is None else f"{t.quantity:g}"}</td>'
            f'<td class="num">{"" if t.price is None else _money(t.price)}</td>'
            f'<td class="num {_sign_class(t.amount)}">{_money(t.amount)}</td>'
            f'<td><span class="cat-badge"><span class="swatch" '
            f'style="background:{category_color(c.category)}"></span>'
            f'{esc(c.category.value)}</span></td>'
            f'<td>{esc(t.source_file)}</td></tr>')
    return (
        '<table class="data" id="txn-table"><thead><tr>'
        '<th>Date</th><th>Instrument</th><th>Description</th><th>Code</th>'
        '<th class="num">Qty</th><th class="num">Price</th><th class="num">Amount</th>'
        '<th>Category</th><th>Source</th>'
        '</tr></thead><tbody>' + "\n".join(body) + '</tbody></table>')


# --------------------------------------------------------------------------
def build_page(m: Metrics, positions: PositionsResult, classified: list,
               files_read: list[str], row_errors: list[str],
               interactive: bool = False) -> str:
    """
    Render the whole page.

    `interactive` adds the statement-upload chrome, and is set only by
    `server.py`. Left false — the CLI path — not one byte of the output
    changes, so a dashboard built on the command line never ships a button
    that posts to a server the file has no way to reach.
    """
    # --- charts -----------------------------------------------------------
    line_series = [
        {"key": c.value, "color": category_color(c),
         "values": m.categories[c].income_cumulative}
        for c in INCOME_CATEGORIES
    ]
    if m.other_count:
        other_cum = [sum(m.categories[c].income_cumulative[i] for c in FALLBACK_CATEGORIES)
                     for i in range(len(m.months))]
        line_series.append({"key": OTHER_KEY, "color": OTHER_COLOR, "values": other_cum})
    line_series.append({"key": "Net income", "color": TOTAL_COLOR,
                        "values": m.net_income_cumulative, "emphasis": True})
    line_svg, line_data = render_line_chart(m.months, line_series)

    bar_cats = []
    for c in INCOME_CATEGORIES:
        s = m.categories[c]
        bar_cats.append({
            "key": c.value, "color": category_color(c), "value": s.income_total,
            "count": s.count,
            "note": "realized P&L" if s.is_lot_matched else "net cash"})
    if m.other_count:
        bar_cats.append({"key": OTHER_KEY, "color": OTHER_COLOR, "value": m.other_income,
                          "count": m.other_count, "note": "net cash"})
    bar_svg, bar_data = render_bar_chart(bar_cats)

    # --- header / callouts ------------------------------------------------
    gen_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    date_range = f"{m.date_range[0]} → {m.date_range[1]}" if m.date_range else "—"

    callouts = []
    if m.duplicates_removed:
        callouts.append(
            f"<li><strong>{m.duplicates_removed} duplicate row(s) removed.</strong> The "
            f"same date, instrument, code, quantity, price and amount appeared in more "
            f"than one source file — Robinhood's monthly exports overlap.</li>")
    if m.fallback.count:
        codes = ", ".join(f"<code>{esc(k)}</code> ({v})"
                          for k, v in list(m.fallback.by_code.items())[:12])
        callouts.append(
            f"<li><strong>{m.fallback.count} transaction(s) hit the sign-based "
            f"fallback</strong> (marked † below) because their trans code isn't in the "
            f"rule set: {codes}. They still count toward net income, by sign. Add rules "
            f"in <code>categorize.py</code> if any belongs somewhere specific.</li>")
    for w in positions.warnings[:8]:
        callouts.append(f"<li><strong>Lot matching:</strong> {esc(w)}</li>")
    if row_errors:
        callouts.append(
            f"<li><strong>{len(row_errors)} row(s) could not be parsed</strong> and were "
            f"skipped — see the run log for the file and line.</li>")
    if not m.reconciles:
        cause = ""
        if positions.unmatched_corporate_action_basis > 0.005:
            cause = (
                f" {_money(positions.unmatched_corporate_action_basis)} of it is cost "
                f"basis surrendered in a corporate action that never reached any "
                f"incoming shares — see the lot-matching note above.")
        callouts.append(
            f"<li><strong>Reconciliation is off by "
            f"{_money(m.reconciliation_error)}.</strong> Net income plus open-position "
            f"cash plus transfers should equal total cash movement exactly. It doesn't, "
            f"which means something is double-counted — treat these figures as "
            f"unreliable and report this.{cause}</li>")

    callout_html = ""
    if callouts:
        callout_html = (
            f'<details class="callout" open><summary>Data quality notes '
            f'({len(callouts)})</summary><ul>{"".join(callouts)}</ul></details>')

    kpis = []
    for c in INCOME_CATEGORIES:
        s = m.categories[c]
        if s.is_lot_matched:
            sub = f"{s.count} {'transaction' if s.count == 1 else 'transactions'} · realized only"
        else:
            sub = f"{s.count} {'transaction' if s.count == 1 else 'transactions'}"
        kpis.append(_money_tile(c.value, s.income_total, sub,
                                 swatch=category_color(c), signed=True))
    for c in TRANSFER_CATEGORIES:
        s = m.categories[c]
        kpis.append(_money_tile(
            c.value, s.cash_total,
            f"{s.count} {'transfer' if s.count == 1 else 'transfers'} · not income",
            swatch=category_color(c), neutral=True, inset=True))

    extra_css = INTERACTIVE_CSS if interactive else ""
    extra_js = INTERACTIVE_JS if interactive else ""
    actions_html = _header_actions() if interactive else ""
    dialog_html = _files_dialog() if interactive else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Robinhood Portfolio Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/png" sizes="32x32" href="{FAVICON_DATA_URI}">
<style>{CSS}{extra_css}</style>
</head>
<body>
<div class="page">
  <header>{actions_html}
    <h1>Robinhood Portfolio Dashboard</h1>
    <p class="meta">Generated {gen_ts} &middot; {len(files_read)} source file(s):
      {esc(", ".join(files_read))} &middot; {date_range}</p>
  </header>

  {callout_html}

  {_summary_section(m)}

  {_open_positions_section(m, positions)}

  <section class="kpi-row">{"".join(kpis)}</section>

  <div class="filter-row">{_legend_chips(bool(m.other_count))}</div>

  {_chart_card("Cumulative net income over time",
               "Running realized total per category, month-end resolution. Equity and "
               "Options step only when a position closes — buying does not move these "
               "lines. Dashed line is net income.",
               line_svg, line_data, "line")}

  {_chart_card("What makes up net income",
               "All-time contribution per category. Equity and Options are realized P&L "
               "on closed positions; the rest are net cash. Deposits and withdrawals are "
               "excluded — they are not income.",
               bar_svg, bar_data, "bar")}

  <div class="card">
    <h2>All transactions ({len(classified)})</h2>
    <p class="sub-head">Every row after de-duplication, newest first. Toggle a category
      chip above to filter. Amount is raw cash as it appears in the statement.</p>
    <div class="table-scroll">{_transactions_table(classified)}</div>
  </div>

  <footer>
    <h3>What "net income" means here, and what it doesn't</h3>
    <p>Net income is <strong>realized</strong>: profit and loss on positions that actually
    closed, plus dividends and interest, minus fees, margin interest and subscriptions.
    Buying a stock does not reduce it &mdash; that converts cash into an asset of equal
    value rather than spending it. Selling is what realizes a gain or a loss, and only
    for the shares sold: sell 50 of 100 shares and half the position is realized while
    the other half stays open.</p>
    <p>Closed lots are matched <strong>FIFO</strong> (first in, first out) &mdash; the
    standard default when no specific tax lot is elected. If you elected specific lots
    with Robinhood, your realized figures will differ from these.</p>
    <p><strong>There is no mark-to-market anywhere on this page.</strong> A statement
    export carries no live price, so open positions are shown at what they cost, not at
    what they are worth now. Your actual unrealized gain or loss on the
    {_plural(m.open_shares, 'share')} still held is not knowable from this input, and
    nothing here estimates it.</p>
    <h3>How categorisation works</h3>
    <p>Equity and Options come from trans code (Buy/Sell vs. BTO/STO/BTC/STC/OEXP/OASGN),
    with a description check to split an option Buy/Sell from an equity one. Margin is
    <code>INT</code> with a negative amount, or any description mentioning "margin."
    Fees is <code>AFEE</code> and other <code>*FEE</code> codes. Dividends/Interest is any
    code containing "DIV" (<code>CDIV</code>, <code>MDIV</code>, …) plus <code>INT</code>
    with a non-negative amount. Gold is <code>GOLD</code>/<code>GDBP</code>. Deposits and
    Withdraw are <code>ACH</code>/<code>RTP</code> split by sign. Anything left over is
    bucketed by sign, shown as "Other," and flagged (&dagger;) rather than silently
    trusted. Full rule table in <code>categorize.py</code>.</p>
    <h3>Limitations</h3>
    <ul>
      <li>No live prices, so no unrealized P&amp;L and no current portfolio value &mdash;
      only cost basis for what is still held.</li>
      <li>Positions opened before the earliest statement have no cost basis here; a sale
      of such shares is counted at full proceeds and flagged in the notes above.</li>
      <li>Categorisation rules are pattern-matched against commonly documented Robinhood
      trans codes and have <strong>not</strong> been verified against a live account
      export (see the header comment in <code>loader.py</code>).</li>
      <li>Duplicate detection is an exact match on every column except source file; two
      genuinely distinct same-day trades with identical size, price and instrument would
      collapse into one (see <code>dedupe.py</code>).</li>
      <li>Option assignment and exercise are treated as closing the contract; any
      resulting share movement is counted from its own Buy/Sell row.</li>
      <li>This is a record of what happened, not investment advice, tax advice, or a
      suitability assessment. Do not file taxes from these numbers.</li>
    </ul>
  </footer>
</div>{dialog_html}
<script>{JS}{extra_js}</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# Interactive chrome — only emitted when the page is served by `server.py`.
#
# A dashboard written by `./rh-dashboard build` is a file you can mail to
# someone; it must not carry an upload button that posts into the void. So
# every byte below is opt-in via `build_page(..., interactive=True)`, and the
# CLI's output stays exactly what it was.
INTERACTIVE_CSS = """
.header-actions { float: right; display: flex; gap: 8px; }
.icon-btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 7px;
  height: 34px; padding: 0 13px; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--border); background: var(--surface-1);
  color: var(--text-primary); font-size: 12.5px; font-family: inherit;
}
.icon-btn:hover { background: var(--surface-2); }
.icon-btn:focus-visible { outline: 2px solid var(--series-1); outline-offset: 2px; }
.icon-btn svg { width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-width: 1.8; }

dialog.dlg {
  width: min(620px, calc(100vw - 32px)); padding: 0; color: var(--text-primary);
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
}
dialog.dlg::backdrop { background: rgba(0,0,0,0.45); }
.dlg-body { padding: 20px 22px 22px; }
.dlg-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.dlg-head h2 { font-size: 15px; margin: 0 0 2px; }
.dlg-sub { font-size: 12px; color: var(--text-secondary); margin: 0 0 14px; line-height: 1.5; }
.dlg-close {
  border: none; background: none; color: var(--text-muted); cursor: pointer;
  font-size: 20px; line-height: 1; padding: 2px 6px; font-family: inherit;
}
.dlg-close:hover { color: var(--text-primary); }

.drop-zone {
  border: 1.5px dashed var(--baseline); border-radius: 10px; padding: 22px 16px;
  text-align: center; font-size: 13px; color: var(--text-secondary);
  background: var(--surface-2); transition: border-color .12s ease;
}
.drop-zone.dragging { border-color: var(--series-1); }
.drop-zone .link {
  border: none; background: none; padding: 0; cursor: pointer; font-family: inherit;
  font-size: 13px; color: var(--series-1); text-decoration: underline;
}

.upload-log { margin: 14px 0 0; font-size: 12.5px; line-height: 1.55; }
.upload-log li { margin: 5px 0; }
.upload-log li.ok { color: var(--good-text); }
.upload-log li.warn { color: var(--text-secondary); }
.upload-log li.err { color: var(--bad-text); }
.upload-log .why { color: var(--text-muted); }

.file-list { width: 100%; border-collapse: collapse; font-size: 12.5px; margin-top: 16px; }
.file-list th {
  text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .02em;
  color: var(--text-muted); border-bottom: 1px solid var(--border); padding: 7px 8px;
}
.file-list td { padding: 7px 8px; border-bottom: 1px solid var(--grid); }
.file-list td.num { text-align: right; font-variant-numeric: tabular-nums; }
.file-list .del {
  border: none; background: none; cursor: pointer; color: var(--text-muted);
  font-family: inherit; font-size: 12px; text-decoration: underline; padding: 0;
}
.file-list .del:hover { color: var(--bad-text); }
"""

_UPLOAD_ICON = ('<svg viewBox="0 0 24 24" aria-hidden="true">'
                '<path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5" stroke-linecap="round" '
                'stroke-linejoin="round"/>'
                '<path d="M4 16v2.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V16" '
                'stroke-linecap="round"/></svg>')


def _header_actions() -> str:
    return (
        '\n    <div class="header-actions">'
        f'<button type="button" class="icon-btn" id="open-files" '
        f'aria-haspopup="dialog" title="Add or remove statement files">'
        f'{_UPLOAD_ICON}<span>Statements</span></button>'
        '</div>')


def _files_dialog() -> str:
    return """
<dialog class="dlg" id="files-dialog" aria-labelledby="files-dialog-title">
  <div class="dlg-body">
    <div class="dlg-head">
      <div>
        <h2 id="files-dialog-title">Statement files</h2>
      </div>
      <button type="button" class="dlg-close" id="close-files" aria-label="Close">&times;</button>
    </div>
    <p class="dlg-sub">Every <code>*.csv</code> here is read, deduplicated and
      rebuilt into the dashboard. Overlapping monthly exports are safe to add &mdash;
      rows repeated across files collapse, rows repeated within one file do not.</p>
    <div class="drop-zone" id="drop-zone">
      <input type="file" id="file-input" accept=".csv,text/csv" multiple hidden>
      Drop statement CSVs here, or
      <button type="button" class="link" id="browse-files">choose a file</button>
    </div>
    <ul class="upload-log" id="upload-log" hidden></ul>
    <table class="file-list" id="file-list"><tbody></tbody></table>
  </div>
</dialog>"""


INTERACTIVE_JS = """
(function () {
  var dialog = document.getElementById('files-dialog');
  var log = document.getElementById('upload-log');
  var input = document.getElementById('file-input');
  var zone = document.getElementById('drop-zone');
  var dirty = false;

  document.getElementById('open-files').addEventListener('click', function () {
    log.hidden = true;
    log.replaceChildren();
    refresh();
    dialog.showModal();
  });
  document.getElementById('close-files').addEventListener('click', function () {
    dialog.close();
  });
  // Reloading only on close keeps the dialog usable for several uploads in a
  // row; the page behind it is stale until then, so never skip this.
  dialog.addEventListener('close', function () { if (dirty) location.reload(); });

  document.getElementById('browse-files').addEventListener('click', function () {
    input.click();
  });
  input.addEventListener('change', function () { send(input.files); input.value = ''; });

  ['dragenter', 'dragover'].forEach(function (ev) {
    zone.addEventListener(ev, function (e) {
      e.preventDefault();
      zone.classList.add('dragging');
    });
  });
  ['dragleave', 'drop'].forEach(function (ev) {
    zone.addEventListener(ev, function (e) {
      e.preventDefault();
      zone.classList.remove('dragging');
    });
  });
  zone.addEventListener('drop', function (e) { send(e.dataTransfer.files); });

  function note(cls, text, why) {
    var li = document.createElement('li');
    li.className = cls;
    li.textContent = text;
    if (why) {
      var span = document.createElement('span');
      span.className = 'why';
      span.textContent = ' — ' + why;
      li.appendChild(span);
    }
    log.appendChild(li);
    log.hidden = false;
  }

  function send(files) {
    Array.prototype.forEach.call(files, function (file) {
      var body = new FormData();
      body.append('file', file, file.name);
      fetch('api/upload', { method: 'POST', body: body })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (res.status === 'saved') {
            note('ok', res.saved_as + ' added', res.detail);
            dirty = true;
          } else if (res.status === 'duplicate') {
            note('warn', file.name, res.detail);
          } else {
            note('err', file.name + ' rejected', res.detail);
          }
          refresh();
        })
        .catch(function (err) { note('err', file.name + ' failed', String(err)); });
    });
  }

  function refresh() {
    fetch('api/files')
      .then(function (r) { return r.json(); })
      .then(function (res) { render(res.files || []); })
      .catch(function () { /* dialog still works for uploading */ });
  }

  function render(files) {
    var body = document.querySelector('#file-list tbody');
    body.replaceChildren();
    if (!files.length) {
      var empty = document.createElement('tr');
      var cell = document.createElement('td');
      cell.colSpan = 3;
      cell.textContent = 'No statement files yet.';
      empty.appendChild(cell);
      body.appendChild(empty);
      return;
    }
    files.forEach(function (f) {
      var tr = document.createElement('tr');
      var name = document.createElement('td');
      name.textContent = f.name;
      var rows = document.createElement('td');
      rows.className = 'num';
      rows.textContent = f.rows + ' rows';
      var act = document.createElement('td');
      act.className = 'num';
      var del = document.createElement('button');
      del.type = 'button';
      del.className = 'del';
      del.textContent = 'remove';
      del.addEventListener('click', function () { remove(f.name); });
      act.appendChild(del);
      tr.appendChild(name);
      tr.appendChild(rows);
      tr.appendChild(act);
      body.appendChild(tr);
    });
  }

  function remove(name) {
    fetch('api/files/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name })
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res.status === 'deleted') {
          note('ok', name + ' removed');
          dirty = true;
        } else {
          note('err', name, res.detail);
        }
        refresh();
      });
  }
})();
"""
