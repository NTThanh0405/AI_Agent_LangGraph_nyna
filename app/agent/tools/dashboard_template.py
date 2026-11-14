# app/agent/tools/dashboard_template.py
# Template HTML cố định của dashboard + render_html() để nhúng dữ liệu AN TOÀN.
#
# Bảo mật: LUÔN dùng render_html(payload), không tự TEMPLATE.replace('__PAYLOAD__', json.dumps(...)).
# json.dumps không thoát "</script>", nên tên cửa hàng/món chứa "</script><script>..." sẽ phá
# thẻ script và chạy mã. render_html thoát < > & U+2028 U+2029, đổi Decimal/date/NaN về JSON hợp lệ.
# Mọi chữ hiển thị đều đưa vào trang bằng textContent; logo chỉ nhận data URI ảnh.
#
# Chart.js do nginx (dashboards-web) phục vụ tại /chart.umd.min.js (đường dẫn tuyệt đối);
# không tải được hoặc vẽ lỗi thì biểu đồ rơi về bảng số liệu.
#
# payload = {title, generated, expires, logo?: data URI, dashboards: [tab, ...]}
# tab     = {name, title?, theme, labels, filters, kpis, charts, dataset: {columns, rows}}
# Loại khối (charts[].type): line | bar | pie | doughnut | table | pivot | heatmap
# (xem dashboard_presets.py để biết đủ các khóa). Trình duyệt tự lọc, cộng gộp, vẽ lại khi
# đổi bộ lọc; mỗi khối có nút tải CSV; sáng/tối theo máy; lưới 12 cột; có CSS in ấn.

from __future__ import annotations

import base64
import json
import math
import re
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

TEMPLATE = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="referrer" content="no-referrer">
<title>Dashboard</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #F5F6F8; --panel: #FFFFFF; --ink: #1B1F24; --muted: #667085; --line: #E6E8EC; --head: #F5F6F8;
    --good: #1B7F3B; --bad: #C8202B;
    --c1: #1F6F6B; --c2: #9A5B35; --c3: #D6A23A; --c4: #5C6F8A; --c5: #B24D69; --c6: #7E8F3F;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #101413; --panel: #191F1D; --ink: #E7ECE8; --muted: #9AA8A0; --line: #2A3631; --head: #222A27;
      --good: #5CC27E; --bad: #F0707A;
      --c1: #4FB3AC; --c2: #D08A5C; --c3: #E4B95A; --c4: #8FA6C6; --c5: #D77A93; --c6: #A5B76A;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.5 "Segoe UI", system-ui, -apple-system, Roboto, "Helvetica Neue", Arial, sans-serif;
  }
  main { max-width: 1440px; margin: 0 auto; padding: 20px 16px 48px; }
  .layout { display: flex; gap: 20px; align-items: flex-start; }
  .side { flex: none; width: 190px; position: sticky; top: 20px; }
  .side[hidden] { display: none; }
  .content { flex: 1 1 0; min-width: 0; }
  /* Tiêu đề bên trái, bộ lọc bên phải trên cùng một hàng. Tiêu đề tự xuống dòng để nhường chỗ cho
     bộ lọc; màn hẹp (dưới 1100px) mới đưa bộ lọc xuống dưới tiêu đề. */
  .top { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px 24px; flex-wrap: nowrap; margin-bottom: 16px; }
  header { display: flex; align-items: center; gap: 14px; min-width: 0; flex: 1 0 340px; }
  #filterSlot { flex: 0 1 auto; min-width: 0; }
  #filterSlot .filters { justify-content: flex-end; gap: 8px; }
  #filterSlot .f { padding: 5px 8px 7px; }
  #filterSlot .f select { min-width: 112px; }
  #filterSlot .f .dates input { width: 122px; }
  @media (max-width: 1100px) {
    .top { flex-wrap: wrap; }
    header, #filterSlot { flex: 1 1 100%; }
    #filterSlot .filters { justify-content: flex-start; }
  }
  header img { height: 56px; width: auto; flex: none; }
  header img[hidden] { display: none; }
  @media (max-width: 820px) {
    .layout { flex-direction: column; gap: 12px; }
    .side { width: 100%; position: static; }
  }
  h1 {
    margin: 0 0 2px; font-size: clamp(1.35rem, 4vw, 1.75rem); font-weight: 700;
    letter-spacing: -0.01em; line-height: 1.2; text-transform: uppercase;
    color: var(--title, var(--ink));
  }
  .meta { margin: 20px 0 0; color: var(--muted); font-size: .8rem; }

  .tabs { display: flex; flex-direction: column; gap: 6px; }
  @media (max-width: 820px) { .tabs { flex-direction: row; overflow-x: auto; padding-bottom: 2px; } }
  .tab {
    display: block; width: 100%; text-align: left; font: inherit; font-size: .9rem; padding: 10px 14px; cursor: pointer;
    border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--ink);
  }
  @media (max-width: 820px) { .tab { width: auto; flex: none; white-space: nowrap; } }
  .tab[aria-selected="true"] { background: var(--brand, var(--c1)); border-color: var(--brand, var(--c1)); color: #fff; font-weight: 600; }
  .tab:focus-visible { outline: 2px solid var(--brand, var(--c1)); outline-offset: 2px; }

  .view[hidden] { display: none; }
  .sub { margin: 0; color: var(--muted); font-size: .85rem; font-weight: 600; }
  .filters { display: flex; gap: 10px; flex-wrap: wrap; align-items: stretch; }
  .f { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px 8px; }
  .f label { display: block; font-size: .72rem; color: var(--muted); margin-bottom: 3px; }
  .f select, .f input {
    font: inherit; font-size: .82rem; padding: 4px 6px; min-width: 130px; max-width: 220px;
    border: 1px solid var(--line); border-radius: 4px; background: var(--panel); color: var(--ink);
  }
  .f .dates { display: flex; gap: 4px; }
  .f .dates input { min-width: 0; width: 128px; }
  .f select:focus-visible, .f input:focus-visible, .reset:focus-visible, .dl:focus-visible { outline: 2px solid var(--brand, var(--c1)); outline-offset: 1px; }
  .reset {
    align-self: flex-end; font: inherit; font-size: .82rem; padding: 6px 12px; cursor: pointer;
    border: 1px solid var(--line); border-radius: 6px; background: var(--panel); color: var(--ink);
  }

  .kpis { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); margin-bottom: 16px; }
  .kpis[hidden] { display: none; }
  .card, .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; min-width: 0; }
  .card .l { color: var(--ink); font-size: .85rem; }
  .card .v { font-size: 1.75rem; font-weight: 700; line-height: 1.25; font-variant-numeric: tabular-nums; }
  .card .d { font-size: .78rem; font-weight: 600; margin-top: 2px; }
  .card .d.good { color: var(--good); }
  .card .d.bad { color: var(--bad); }
  .card .d.flat { color: var(--muted); font-weight: 400; }
  .card .h { color: var(--muted); font-size: .72rem; margin-top: 4px; }

  .grid { display: grid; gap: 16px; grid-template-columns: repeat(12, minmax(0, 1fr)); }
  .grid > .panel { grid-column: span var(--span, 6); }
  @media (max-width: 820px) { .grid > .panel { grid-column: 1 / -1; } }
  .ph { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; margin-bottom: 8px; }
  h2 { margin: 0; font-size: .95rem; font-weight: 600; line-height: 1.3; }
  .dl {
    flex: none; font: inherit; font-size: .7rem; padding: 1px 7px; cursor: pointer;
    border: 1px solid var(--line); border-radius: 6px; background: transparent; color: var(--muted);
  }
  .dl:hover { color: var(--ink); }
  .chart { position: relative; height: 320px; }
  .scroll { overflow: auto; max-height: 520px; }
  table { width: 100%; border-collapse: collapse; font-size: .82rem; }
  th, td { padding: 7px 10px; border-bottom: 1px solid var(--line); text-align: left; white-space: nowrap; }
  thead th { color: var(--ink); font-weight: 600; position: sticky; top: 0; background: var(--head); }
  thead tr + tr th { top: 33px; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  td.rh { font-weight: 600; }
  td.good { color: var(--good); }
  td.bad { color: var(--bad); }
  td.flat { color: var(--muted); }
  tfoot td { font-weight: 700; border-top: 2px solid var(--line); border-bottom: none; }
  table.pivot .gs { border-left: 1px solid var(--line); }
  table.heat { font-size: .72rem; table-layout: fixed; }
  table.heat th, table.heat td { padding: 6px 0; text-align: center; border: 1px solid var(--panel); overflow: hidden; text-overflow: ellipsis; }
  table.heat thead th { font-size: .64rem; letter-spacing: -0.02em; }
  table.heat th:first-child { width: 128px; }
  table.heat td.rh, table.heat th:first-child { text-align: left; padding-left: 8px; padding-right: 6px; }
  .empty { margin: 0; color: var(--muted); }
  .note { margin: 8px 0 0; color: var(--muted); font-size: .75rem; }
  /* ---- Tinh chỉnh giao diện: nền dịu, thẻ bo tròn có bóng nhẹ, bộ lọc gom một khung, KPI rõ thứ bậc ---- */
  h1 { font-size: clamp(1.2rem, 2.2vw, 1.5rem); }
  .card, .panel { border-radius: 12px; box-shadow: 0 1px 2px rgba(16, 24, 40, .05); }
  .card { padding: 16px 18px; border-top: 3px solid var(--brand, var(--c1)); }
  .card .l { color: var(--muted); font-size: .78rem; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; }
  .card .v { font-size: 1.9rem; margin: 2px 0 4px; }
  .card .d { display: inline-block; padding: 2px 8px; border-radius: 999px; }
  .card .d.good { background: color-mix(in srgb, var(--good) 12%, transparent); }
  .card .d.bad { background: color-mix(in srgb, var(--bad) 10%, transparent); }
  .card .d.flat { padding-left: 0; }
  .panel { padding: 16px 18px; }
  .tab { border-radius: 10px; }
  .filters { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 8px 10px;
             box-shadow: 0 1px 2px rgba(16, 24, 40, .05); gap: 4px 14px; }
  #filterSlot .f, .f { background: transparent; border: 0; padding: 0; }
  .f select, .f input { border-radius: 6px; background: var(--bg); }
  .reset { border: 0; background: transparent; color: var(--brand, var(--c1)); font-weight: 600; padding: 6px 4px; }
  @media print {
    body { background: #fff; }
    .side, #filterSlot, .dl { display: none !important; }
    .card, .panel { break-inside: avoid; }
  }
</style>
</head>
<body>
<main>
  <div class="layout">
    <nav class="side" id="side" hidden>
      <div class="tabs" id="tabs" role="tablist" aria-orientation="vertical" aria-label="Chọn trang báo cáo"></div>
    </nav>
    <div class="content">
      <div class="top">
        <header>
          <img id="logo" alt="" hidden>
          <div>
            <h1 id="title"></h1>
            <div id="subSlot"></div>
          </div>
        </header>
        <div id="filterSlot"></div>
      </div>
      <div id="views"></div>
      <p class="meta" id="meta"></p>
    </div>
  </div>
</main>

<script type="application/json" id="payload">__PAYLOAD__</script>
<script src="/chart.umd.min.js"></script>
<script>
(function () {
  'use strict';
  var data;
  try { data = JSON.parse(document.getElementById('payload').textContent); }
  catch (e) { document.getElementById('title').textContent = 'Không đọc được dữ liệu dashboard'; return; }

  var root = document.documentElement;
  var mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  var dark = !!(mq && mq.matches);
  function pick(x) { return Array.isArray(x) ? x[dark ? 1 : 0] : x; }
  function cssv(n) { return getComputedStyle(root).getPropertyValue(n).trim(); }
  var OTHER = 'Khác';

  function ok(v) { return typeof v === 'number' && isFinite(v); }

  // ---------- định dạng số: 'en' = 24.74M, 1,234 ; 'vi' = 24,74 tr, 1.234 ----------
  var FMTS = {};
  function fmtFor(style) {
    style = style === 'en' ? 'en' : 'vi';
    if (FMTS[style]) return FMTS[style];
    var L = style === 'en' ? 'en-US' : 'vi-VN';
    var N = new Intl.NumberFormat(L, { maximumFractionDigits: 2 });
    var N0 = new Intl.NumberFormat(L, { maximumFractionDigits: 0 });
    var D1 = new Intl.NumberFormat(L, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
    var D2 = new Intl.NumberFormat(L, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    var U = style === 'en' ? [[1e9, 'B'], [1e6, 'M'], [1e3, 'K']] : [[1e9, ' tỷ'], [1e6, ' tr'], [1e3, ' k']];
    function compact(v) {
      var a = Math.abs(v);
      for (var i = 0; i < U.length; i++) if (a >= U[i][0]) return N.format(v / U[i][0]) + U[i][1];
      return N.format(v);
    }
    var F = {
      full: function (kind, v) {
        if (!ok(v)) return '–';
        switch (kind) {
          case 'number': return N0.format(v);
          case 'decimal': return D2.format(v);
          case 'decimal1': return D1.format(v);
          case 'percent': return D1.format(v * 100) + '%';
          case 'vnd': return N0.format(v) + ' ₫';
          case 'minutes': return D1.format(v) + ' phút';
          default: return compact(v);
        }
      },
      short: function (kind, v) {
        if (!ok(v)) return '';
        switch (kind) {
          case 'percent': return D1.format(v * 100) + '%';
          case 'decimal': return D2.format(v);
          case 'decimal1': case 'minutes': return D1.format(v);
          case 'number': return Math.abs(v) >= 1e4 ? compact(v) : N0.format(v);
          default: return compact(v);
        }
      },
      axis: function (kind, v) {
        if (kind === 'percent') return N0.format(v * 100) + '%';
        if (kind === 'decimal' || kind === 'decimal1' || kind === 'minutes') return N.format(v);
        return Math.abs(v) < 1e3 ? N.format(v) : compact(v);
      },
      pct: function (v) { return D1.format(Math.abs(v) * 100) + '%'; },
      cell: function (v) { return typeof v === 'number' ? N.format(v) : (v === null || v === undefined ? '' : String(v)); }
    };
    FMTS[style] = F;
    return F;
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function key(v) { return v === null || v === undefined ? '(trống)' : String(v); }
  function day(v) { return v === null || v === undefined ? '' : String(v).slice(0, 10); }
  function viDate(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || '');
    return m ? m[3] + '/' + m[2] + '/' + m[1] : (s || '');
  }
  var WD = ['CN', 'T2', 'T3', 'T4', 'T5', 'T6', 'T7'];
  function dateLabel(s, mode) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || '');
    if (!m) return s;
    var wd = WD[new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])).getUTCDay()];
    return mode === 'weekday' ? wd : wd + ' ' + m[3] + '/' + m[2];
  }
  function hexAlpha(c, a) {
    var m = /^#([0-9a-f]{6})$/i.exec(c || '');
    if (!m) return c;
    var n = parseInt(m[1], 16);
    return 'rgba(' + (n >> 16) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
  }
  function colLabel(d, c, col) {
    return (c && c.y_labels && c.y_labels[col]) || (d.labels && d.labels[col]) || col;
  }
  function matchPrefix(map, k) {
    if (!map) return null;
    var ks = Object.keys(map);
    for (var i = 0; i < ks.length; i++) if (k.indexOf(ks[i]) === 0) return map[ks[i]];
    return null;
  }

  // ---------- màu theo theme ----------
  function themeColors(t) {
    t = t || {};
    var brand = pick(t.brand) || cssv('--c1');
    return {
      brand: brand,
      prev: pick(t.prev) || cssv('--muted'),
      heat: pick(t.heat) || brand,
      palette: [brand, cssv('--c2'), cssv('--c3'), cssv('--c4'), cssv('--c5'), cssv('--c6')],
      series: t.series_colors || {}
    };
  }
  function seriesRank(tc, name) {
    var keys = Object.keys(tc.series);
    for (var k = 0; k < keys.length; k++) if (name.indexOf(keys[k]) === 0) return k;
    return keys.length;
  }
  function paletteAt(tc, i) {
    var n = tc.palette.length, base = tc.palette[i % n], round = Math.floor(i / n);
    return round ? hexAlpha(base, Math.max(0.35, 1 - 0.3 * round)) : base;
  }
  function seriesColor(tc, name, idx) {
    var c = matchPrefix(tc.series, name);
    return c ? pick(c) : paletteAt(tc, idx);
  }
  function applyTheme(t) {
    t = t || {};
    root.style.setProperty('--brand', pick(t.brand) || cssv('--c1'));
    root.style.setProperty('--title', pick(t.title) || cssv('--ink'));
  }

  // ---------- lọc: một lần cho mỗi lần đổi bộ lọc ----------
  // all = mọi dòng qua bộ lọc chọn (bỏ qua lọc ngày và kỳ) — dùng cho khối scope: 'all' (vd. theo tuần)
  function filtered(view) {
    var d = view.d, ci = d._ci, kyi = ci.ky, cur = [], prev = [], all = [], sel = [], dates = [];
    d.filters.forEach(function (f) {
      var v = view.st[f.column];
      if (ci[f.column] === undefined) return;
      if (f.type === 'date') { if (v && (v[0] || v[1])) dates.push(f); }
      else if (v !== undefined && v !== null && v !== '') sel.push(f);
    });
    d.dataset.rows.forEach(function (r) {
      var j;
      for (j = 0; j < sel.length; j++) if (key(r[ci[sel[j].column]]) !== view.st[sel[j].column]) return;
      all.push(r);
      for (j = 0; j < dates.length; j++) {
        var val = view.st[dates[j].column], s = day(r[ci[dates[j].column]]);
        if ((val[0] && s < val[0]) || (val[1] && s > val[1])) return;
      }
      var k = kyi === undefined ? 'nay' : r[kyi];
      if (k === 'nay') cur.push(r); else if (k === 'truoc') prev.push(r);
    });
    return { cur: cur, prev: prev, all: all };
  }
  // scope 'all': dùng cả lịch sử; where {cột: giá trị}: lọc cố định riêng cho khối (vd. chỉ món chính)
  function scoped(c, f, d) {
    var s = c.scope === 'all' ? { cur: f.all, prev: [], all: f.all } : f;
    if (!c.where) return s;
    var conds = Object.keys(c.where).filter(function (k) { return d._ci[k] !== undefined; })
      .map(function (k) { return [d._ci[k], String(c.where[k])]; });
    function keep(r) { return conds.every(function (cd) { return key(r[cd[0]]) === cd[1]; }); }
    return { cur: s.cur.filter(keep), prev: s.prev.filter(keep), all: s.all.filter(keep) };
  }

  // ---------- cộng gộp ----------
  function groupBy(rows, kf) {
    var m = new Map();
    rows.forEach(function (r) {
      var k = kf(r);
      if (k === null) return;
      if (!m.has(k)) m.set(k, []);
      m.get(k).push(r);
    });
    return m;
  }
  function byCol(i) { return function (r) { return key(r[i]); }; }
  function sumCol(rows, i) {
    var s = 0, any = false;
    rows.forEach(function (r) { var x = r[i]; if (typeof x === 'number') { s += x; any = true; } });
    return any ? s : null;
  }
  function metric(rows, d, col, agg, denom) {
    if (!rows || !rows.length) return null;
    var i = d._ci[col];
    if (agg === 'count') return rows.length;
    if (agg === 'ratio') {
      var a = sumCol(rows, i), b = sumCol(rows, d._ci[denom]);
      return (a === null || !b) ? null : a / b;
    }
    if (agg === 'avg') {
      var s = 0, n = 0;
      rows.forEach(function (r) { var x = r[i]; if (typeof x === 'number') { s += x; n++; } });
      return n ? s / n : null;
    }
    return sumCol(rows, i);
  }
  function sortKeys(keys, groups, oi) {
    var cache = new Map();
    function ord(k) {
      if (cache.has(k)) return cache.get(k);
      var m = Infinity;
      groups.forEach(function (g) {
        (g.get(k) || []).forEach(function (r) { if (typeof r[oi] === 'number' && r[oi] < m) m = r[oi]; });
      });
      cache.set(k, m);
      return m;
    }
    keys.sort(function (a, b) {
      if (oi !== undefined && oi >= 0) { var oa = ord(a), ob = ord(b); if (oa !== ob) return oa < ob ? -1 : 1; }
      return a.localeCompare(b, 'vi', { numeric: true });
    });
  }
  function cmpVal(mode) {
    return function (va, vb) {
      if (va === null && vb === null) return 0;
      if (va === null) return 1;
      if (vb === null) return -1;
      return mode === 'asc' ? va - vb : vb - va;
    };
  }
  function sortByMetric(keys, groups, d, col, agg, denom, mode, nullAsZero) {
    var base = new Map(), cmp = cmpVal(mode);
    keys.forEach(function (k) {
      var v = metric(groups.get(k), d, col, agg, denom);
      base.set(k, (v === null && nullAsZero) ? 0 : v);
    });
    keys.sort(function (a, b) { return cmp(base.get(a), base.get(b)); });
  }
  function unionKeys(maps) {
    var seen = new Set(), out = [];
    maps.forEach(function (m) { m.forEach(function (_, k) { if (!seen.has(k)) { seen.add(k); out.push(k); } }); });
    return out;
  }
  function compareCols(c) {
    if (Array.isArray(c.compare)) return c.compare;
    return c.compare ? (c.y || []) : [];
  }

  // ---------- mô hình cho biểu đồ đường / cột / tròn ----------
  function compute(c, d, f0, tc) {
    var f = scoped(c, f0, d);
    var isPie = c.type === 'pie' || c.type === 'doughnut';
    var xi = d._ci[c.x], oi = c.x_order ? d._ci[c.x_order] : -1;
    var ys = isPie ? [c.y[0]] : c.y;
    var useCompare = compareCols(c).length > 0 && !isPie && !c.color_by && d._ci.ky !== undefined && c.scope !== 'all';
    var gc = groupBy(f.cur, byCol(xi)), gp = useCompare ? groupBy(f.prev, byCol(xi)) : new Map();
    var keys;
    if (c.x_values) {
      keys = c.x_values.map(String);           // trục cố định (vd. đủ 24 giờ), giá trị ngoài danh sách bị bỏ
    } else {
      keys = unionKeys([gc, gp]);
      sortKeys(keys, [gc, gp], oi);
    }
    var limit = c.limit || (isPie ? 6 : 0);
    var sortMode = c.sort || (limit && keys.length > limit ? 'desc' : null);
    if (!c.x_values && (sortMode === 'asc' || sortMode === 'desc')) {
      if (c.sort_by) sortByMetric(keys, gc, d, c.sort_by, 'sum', null, sortMode, c.null_as_zero);
      else sortByMetric(keys, gc, d, ys[0], c.agg, c.denom, sortMode, c.null_as_zero);
    }
    var remap = new Map();
    if (limit && keys.length > limit) {
      var withOthers = c.others !== undefined ? !!c.others : isPie;
      keys.slice(limit).forEach(function (k) { remap.set(k, withOthers ? OTHER : null); });
      keys = keys.slice(0, limit);
      if (withOthers) keys.push(OTHER);
    }
    var keySet = new Set(keys);
    function kf(r) { var k = key(r[xi]); if (remap.has(k)) k = remap.get(k); return keySet.has(k) ? k : null; }
    function vals(g, col) {
      return keys.map(function (k) {
        var v = metric(g.get(k), d, col, c.agg, c.denom);
        var dv = matchPrefix(c.x_divisor, k);      // vd. trung bình mỗi giờ: chia số giờ của khung
        return (v !== null && dv) ? v / dv : v;
      });
    }
    var gcur = groupBy(f.cur, kf), gprev = useCompare ? groupBy(f.prev, kf) : null;
    var cmpSet = compareCols(c), series = [];
    if (c.color_by && !isPie) {
      var cb = d._ci[c.color_by], buckets = new Map();
      f.cur.forEach(function (r) { var n = key(r[cb]); if (!buckets.has(n)) buckets.set(n, []); buckets.get(n).push(r); });
      var names = Array.from(buckets.keys());
      var sLimit = c.series_limit || 8;
      if (names.length > sLimit) {
        var tot = new Map(), cmpS = cmpVal('desc'), rest = [];
        names.forEach(function (n) { tot.set(n, metric(buckets.get(n), d, ys[0], c.agg, c.denom)); });
        names.sort(function (a, b) { return cmpS(tot.get(a), tot.get(b)); });
        names.slice(sLimit - 1).forEach(function (n) { rest = rest.concat(buckets.get(n)); buckets.delete(n); });
        names = names.slice(0, sLimit - 1);
        buckets.set(OTHER, rest);
      }
      names.sort(function (a, b) { return (seriesRank(tc, a) - seriesRank(tc, b)) || a.localeCompare(b, 'vi', { numeric: true }); });
      if (buckets.has(OTHER)) names.push(OTHER);
      names.forEach(function (n, j) {
        series.push({ name: n, values: vals(groupBy(buckets.get(n), kf), ys[0]),
          color: n === OTHER ? tc.prev : seriesColor(tc, n, j) });
      });
    } else {
      var single = ys.length === 1;
      ys.forEach(function (col, j) {
        var lab = colLabel(d, c, col), color = paletteAt(tc, j), cmpThis = useCompare && cmpSet.indexOf(col) >= 0;
        series.push({ name: cmpThis ? (single ? 'Kỳ này' : lab + ' – kỳ này') : lab, values: vals(gcur, col), color: color });
        if (cmpThis) {
          series.push({ name: single ? 'Kỳ trước' : lab + ' – kỳ trước', values: vals(gprev, col),
            color: single ? tc.prev : hexAlpha(color, 0.4), prev: true });
        }
      });
    }
    if (c.hide_empty) {
      var keep = keys.map(function (_, i) { return series.some(function (s) { return s.values[i] !== null && s.values[i] !== 0; }); });
      keys = keys.filter(function (_, i) { return keep[i]; });
      series.forEach(function (s) { s.values = s.values.filter(function (_, i) { return keep[i]; }); });
    }
    var mode = c.x_format === 'weekday' ? (keys.length <= 7 ? 'weekday' : 'date') : c.x_format;
    return {
      keys: keys,
      labels: keys.map(function (k) { return (mode === 'date' || mode === 'weekday') && k !== OTHER ? dateLabel(k, mode) : k; }),
      series: series,
      stacked: c.stacked !== undefined ? !!c.stacked : (!!c.color_by && c.type === 'bar'),
      xName: c.x_label || colLabel(d, null, c.x),
      format: c.format || null
    };
  }

  // ---------- bảng chỉ số: mỗi cột một cách tính, ± kỳ trước, tỉ trọng, dòng Tổng ----------
  function deltaCell(F, a, b, down) {
    if (!ok(a) || !ok(b) || b === 0) return { t: '–', num: true, cls: 'flat' };
    var dl = (a - b) / Math.abs(b);
    if (Math.abs(dl) < 0.0005) return { t: F.pct(0), num: true, cls: 'flat' };
    return { t: (dl > 0 ? '▲ ' : '▼ ') + F.pct(dl), num: true, cls: ((dl > 0) !== !!down) ? 'good' : 'bad' };
  }
  function computeTable(c, d, f0, F) {
    var f = scoped(c, f0, d);
    if (!c.x) {   // bảng dữ liệu thô
      var cols = d.dataset.columns.map(function (col) { return colLabel(d, null, col); });
      var lim0 = c.limit || 200, raw = f.cur.slice(0, lim0);
      return {
        head: [cols.map(function (t) { return { t: t }; })],
        body: raw.map(function (r) { return r.map(function (v) { return { t: F.cell(v), num: typeof v === 'number' }; }); }),
        foot: [], csv: { columns: cols, rows: raw },
        note: f.cur.length > lim0 ? 'Hiển thị ' + lim0 + ' / ' + f.cur.length + ' dòng.' : null
      };
    }
    // Khai báo cũ (y + formats + compare) đổi sang dạng columns
    var specs = c.columns || c.y.map(function (col) {
      return { label: colLabel(d, c, col), column: col, agg: c.agg, denom: c.denom,
        format: (c.formats || {})[col] || c.format || 'number', delta: compareCols(c).indexOf(col) >= 0,
        good_when: c.good_when };
    });
    var xi = d._ci[c.x], oi = c.x_order ? d._ci[c.x_order] : -1;
    var hasKy = d._ci.ky !== undefined && c.scope !== 'all';
    var gc = groupBy(f.cur, byCol(xi)), gp = hasKy ? groupBy(f.prev, byCol(xi)) : new Map();
    var keys = unionKeys([gc]);
    sortKeys(keys, [gc], oi);
    if (c.sort === 'asc' || c.sort === 'desc') {
      if (c.sort_by) sortByMetric(keys, gc, d, c.sort_by, 'sum', null, c.sort);
      else sortByMetric(keys, gc, d, specs[0].column, specs[0].agg, specs[0].denom, c.sort);
    }
    function mval(rows, s) { return metric(rows, d, s.column, s.agg, s.denom); }
    var head = [{ t: c.x_label || colLabel(d, null, c.x) }], csvCols = [head[0].t];
    specs.forEach(function (s) {
      head.push({ t: s.label, num: true }); csvCols.push(s.label);
      if (s.delta && hasKy && !s.share) {
        var dl = s.delta_label || '± kỳ trước';
        head.push({ t: dl, num: true }); csvCols.push(s.label + ' ' + dl);
      }
    });
    function line(label, cur, prev, isTotal) {
      var cells = [{ t: label, cls: isTotal ? '' : 'rh' }], raw = [label];
      specs.forEach(function (s) {
        var a = mval(cur, s);
        if (s.share) { var t = mval(f.cur, s); a = (a !== null && t) ? a / t : null; }
        cells.push({ t: F.full(s.share ? (s.format || 'percent') : s.format, a), num: true }); raw.push(a);
        if (s.delta && hasKy && !s.share) {
          var b = mval(prev, s);
          cells.push(deltaCell(F, a, b, s.good_when === 'down'));
          raw.push(ok(a) && ok(b) && b ? (a - b) / Math.abs(b) : null);
        }
      });
      return { cells: cells, raw: raw };
    }
    var lim = c.limit || 500, body = [], foot = [], csvRows = [];
    keys.slice(0, lim).forEach(function (k) {
      var l = line(c.x_format === 'date' ? dateLabel(k, 'date') : k, gc.get(k), gp.get(k), false);
      body.push(l.cells); csvRows.push(l.raw);
    });
    if (c.total && keys.length) {
      var t = line(c.total_label || 'Tổng', f.cur, f.prev, true);
      foot.push(t.cells); csvRows.push(t.raw);
    }
    return { head: [head], body: body, foot: foot, csv: { columns: csvCols, rows: csvRows },
      note: keys.length > lim ? 'Hiển thị ' + lim + ' / ' + keys.length + ' dòng.' : null };
  }

  // ---------- bảng chéo (pivot): hàng x nhóm cột, tỉ trọng theo hàng/cột, tô màu nhiệt ----------
  function heatCell(tc, x, max) {
    if (!ok(x) || !max) return {};
    var r = Math.max(0, Math.min(1, x / max));
    return { bg: hexAlpha(tc.heat, 0.08 + 0.82 * r), fg: r > 0.62 ? '#fff' : null };
  }
  function computePivot(c, d, f0, F, tc) {
    var f = scoped(c, f0, d), ri = d._ci[c.row], ci = d._ci[c.col], vs = c.values, v0 = vs[0];
    function m(rows, v) { return metric(rows, d, v.column, v.agg, v.denom); }
    var byRow = groupBy(f.cur, byCol(ri)), byC = groupBy(f.cur, byCol(ci));
    var cells = groupBy(f.cur, function (r) { return key(r[ri]) + '\u0001' + key(r[ci]); });
    var colKeys = Array.from(byC.keys());
    if (c.col_values) colKeys = c.col_values.map(String).filter(function (k) { return byC.has(k); });
    else sortByMetric(colKeys, byC, d, v0.column, v0.agg, v0.denom, 'desc');
    var rowKeys = Array.from(byRow.keys());
    if (c.row_order) sortKeys(rowKeys, [byRow], d._ci[c.row_order]);
    else sortByMetric(rowKeys, byRow, d, v0.column, v0.agg, v0.denom, c.sort === 'asc' ? 'asc' : 'desc');
    var lim = c.limit || 200, more = rowKeys.length > lim;
    rowKeys = rowKeys.slice(0, lim);
    function val(rk, ck, v) {
      var x = m(cells.get(rk + '\u0001' + ck), v);
      if (v.share && x !== null) {
        var den = v.share === 'row' ? m(byRow.get(rk), v) : m(byC.get(ck), v);
        x = den ? x / den : null;
      }
      return x;
    }
    var grid = rowKeys.map(function (rk) { return colKeys.map(function (ck) { return vs.map(function (v) { return val(rk, ck, v); }); }); });
    var maxv = vs.map(function (_, j) {
      var mx = 0;
      grid.forEach(function (row) { row.forEach(function (cell) { if (ok(cell[j]) && cell[j] > mx) mx = cell[j]; }); });
      return mx;
    });
    function kind(v) { return v.format || (v.share ? 'percent' : 'number'); }
    var h1 = [{ t: c.col_label || colLabel(d, null, c.col) }], h2 = [{ t: c.row_label || colLabel(d, null, c.row) }];
    var csvCols = [h2[0].t];
    colKeys.forEach(function (ck) {
      h1.push({ t: ck, span: vs.length, cls: 'gs' });
      vs.forEach(function (v, j) { h2.push({ t: v.label, num: true, cls: j === 0 ? 'gs' : '' }); csvCols.push(ck + ' – ' + v.label); });
    });
    var body = [], csvRows = [];
    rowKeys.forEach(function (rk, i) {
      var cellsOut = [{ t: rk, cls: 'rh' }], raw = [rk];
      colKeys.forEach(function (ck, a) {
        vs.forEach(function (v, j) {
          var x = grid[i][a][j], h = v.heat ? heatCell(tc, x, maxv[j]) : {};
          cellsOut.push({ t: ok(x) ? F.full(kind(v), x) : '', num: true, cls: j === 0 ? 'gs' : '', bg: h.bg, fg: h.fg });
          raw.push(x);
        });
      });
      body.push(cellsOut); csvRows.push(raw);
    });
    var foot = [];
    if (c.total && rowKeys.length) {
      var all = m(f.cur, v0), tc0 = [{ t: c.total_label || 'Tổng' }], tr = [c.total_label || 'Tổng'];
      colKeys.forEach(function (ck) {
        vs.forEach(function (v, j) {
          var x = m(byC.get(ck), v);
          if (v.share === 'col') x = x !== null ? 1 : null;
          else if (v.share === 'row') { var den = m(f.cur, v); x = (x !== null && den) ? x / den : null; }
          tc0.push({ t: F.full(kind(v), x), num: true, cls: j === 0 ? 'gs' : '' }); tr.push(x);
        });
      });
      foot.push(tc0); csvRows.push(tr);
    }
    return { head: [h1, h2], body: body, foot: foot, csv: { columns: csvCols, rows: csvRows },
      note: more ? 'Hiển thị ' + lim + ' dòng đầu.' : null };
  }

  // ---------- heatmap: hàng x cột (vd. thứ x giờ), ô tô đậm theo giá trị ----------
  function computeHeat(c, d, f0, F, tc) {
    var f = scoped(c, f0, d), ri = d._ci[c.row], xi = d._ci[c.x];
    var v = c.value || { column: c.y[0], agg: c.agg, denom: c.denom, format: c.format };
    var byRow = groupBy(f.cur, byCol(ri)), byX = groupBy(f.cur, byCol(xi));
    var rowKeys = Array.from(byRow.keys());
    if (c.row_order) sortKeys(rowKeys, [byRow], d._ci[c.row_order]);
    else if (c.sort) sortByMetric(rowKeys, byRow, d, v.column, v.agg, v.denom, c.sort);
    else sortKeys(rowKeys, [byRow], -1);
    var xKeys;
    if (c.x_values) xKeys = c.x_values.map(String);
    else { xKeys = Array.from(byX.keys()); sortKeys(xKeys, [byX], c.x_order ? d._ci[c.x_order] : -1); }
    var cells = groupBy(f.cur, function (r) { return key(r[ri]) + '\u0001' + key(r[xi]); });
    var max = 0;
    var grid = rowKeys.map(function (rk) {
      return xKeys.map(function (xk) {
        var x = metric(cells.get(rk + '\u0001' + xk), d, v.column, v.agg, v.denom);
        if (ok(x) && x > max) max = x;
        return x;
      });
    });
    var head = [{ t: c.row_label || colLabel(d, null, c.row) }].concat(xKeys.map(function (x) { return { t: x }; }));
    var body = rowKeys.map(function (rk, i) {
      return [{ t: rk, cls: 'rh' }].concat(grid[i].map(function (x) {
        var h = (ok(x) && x !== 0) ? heatCell(tc, x, max) : {};
        return { t: (ok(x) && x !== 0) ? F.short(v.format || 'number', x) : '', bg: h.bg, fg: h.fg };
      }));
    });
    return { head: [head], body: body, foot: [],
      csv: { columns: head.map(function (h) { return h.t; }), rows: rowKeys.map(function (rk, i) { return [rk].concat(grid[i]); }) } };
  }

  // ---------- vẽ bảng ----------
  function renderGrid(head, body, foot, tcls) {
    var wrap = el('div', 'scroll'), table = el('table', tcls || '');
    function sec(tag, rows, isHead) {
      if (!rows || !rows.length) return;
      var s = el(tag);
      rows.forEach(function (r) {
        var tr = el('tr');
        r.forEach(function (c) {
          var td = el(isHead ? 'th' : 'td', ((c.num ? 'num ' : '') + (c.cls || '')).trim(), c.t);
          if (c.span) td.colSpan = c.span;
          if (isHead) td.scope = c.span ? 'colgroup' : 'col';
          if (c.bg) td.style.backgroundColor = c.bg;
          if (c.fg) td.style.color = c.fg;
          tr.appendChild(td);
        });
        s.appendChild(tr);
      });
      table.appendChild(s);
    }
    sec('thead', head, true); sec('tbody', body, false); sec('tfoot', foot, false);
    wrap.appendChild(table);
    return wrap;
  }
  function modelColumns(m) { return [m.xName || 'Nhãn'].concat(m.series.map(function (s) { return s.name; })); }
  function modelAsTable(m, F) {
    var head = [modelColumns(m).map(function (t, i) { return { t: t, num: i > 0 }; })];
    var body = m.labels.map(function (label, i) {
      return [{ t: label }].concat(m.series.map(function (s) { return { t: F.full(m.format || 'number', s.values[i]), num: true }; }));
    });
    return renderGrid(head, body, []);
  }
  function modelCsv(m) {
    return { columns: modelColumns(m),
      rows: m.keys.map(function (k, i) { return [k].concat(m.series.map(function (s) { return s.values[i]; })); }) };
  }

  // ---------- nhãn số trên cột/điểm (tự vẽ, không cần plugin ngoài) ----------
  // Cột chồng: chỉ ghi TỔNG. Cột kỳ trước: không ghi. Đường kỳ trước: ghi màu nhạt.
  var labelPlugin = {
    id: 'valueLabels',
    afterDatasetsDraw: function (chart, args, o) {
      if (!o || !o.show) return;
      var ctx = chart.ctx, hz = chart.options.indexAxis === 'y', F = fmtFor(o.style);
      ctx.save();
      ctx.font = '600 10px "Segoe UI", system-ui, sans-serif';
      function drawAt(pt, v) {
        var t = F.short(o.kind, v);
        if (!t) return;
        if (hz) { ctx.textAlign = 'left'; ctx.textBaseline = 'middle'; ctx.fillText(t, pt.x + 4, pt.y); }
        else { ctx.textAlign = 'center'; ctx.textBaseline = 'bottom'; ctx.fillText(t, pt.x, pt.y - 5); }
      }
      if (o.stacked) {
        var totals = [], tops = [];
        chart.data.datasets.forEach(function (ds, di) {
          var meta = chart.getDatasetMeta(di);
          if (meta.hidden) return;
          meta.data.forEach(function (pt, i) {
            var v = ds.data[i];
            if (typeof v === 'number' && v) { totals[i] = (totals[i] || 0) + v; tops[i] = pt; }
          });
        });
        ctx.fillStyle = o.color;
        tops.forEach(function (pt, i) { if (pt) drawAt(pt, totals[i]); });
      } else {
        chart.data.datasets.forEach(function (ds, di) {
          var meta = chart.getDatasetMeta(di);
          if (meta.hidden || ds.noLabel) return;
          ctx.fillStyle = ds.labelColor || o.color;
          meta.data.forEach(function (pt, i) {
            var v = ds.data[i];
            if (v === null || v === undefined || v === 0) return;
            drawAt(pt, v);
          });
        });
      }
      ctx.restore();
    }
  };

  function buildChart(c, m, tc, host, style) {
    var F = fmtFor(style);
    var isPie = c.type === 'pie' || c.type === 'doughnut';
    var hz = !!c.horizontal && c.type === 'bar';
    var wrap = el('div', 'chart');
    if (hz) wrap.style.height = Math.max(c.height || 240, m.labels.length * (m.series.length > 1 ? 30 : 24) + 60) + 'px';
    else if (c.height) wrap.style.height = c.height + 'px';
    var canvas = el('canvas');
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', c.title);
    wrap.appendChild(canvas);
    host.appendChild(wrap);

    var ink = cssv('--ink'), muted = cssv('--muted'), panel = cssv('--panel'), line = cssv('--line');
    var kind = m.format;
    var labelled = m.series.filter(function (s) { return !(c.type === 'bar' && s.prev); }).length;
    var points = m.stacked ? m.labels.length : m.labels.length * labelled;
    var showLabels = !isPie && (c.labels !== undefined ? !!c.labels : points <= 40);
    var datasets = m.series.map(function (s) {
      if (isPie) {
        return { label: s.name, data: s.values,
          backgroundColor: m.keys.map(function (k, i) { return k === OTHER ? tc.prev : paletteAt(tc, i); }),
          borderColor: panel, borderWidth: 2 };
      }
      if (c.type === 'line') {
        return { label: s.name, data: s.values, borderColor: s.color, backgroundColor: s.color,
          borderWidth: 2, tension: 0.25, cubicInterpolationMode: 'monotone',
          pointRadius: m.labels.length > 45 ? 0 : 3, spanGaps: true,
          borderDash: s.prev ? [5, 4] : [], labelColor: s.prev ? muted : ink };
      }
      return { label: s.name, data: s.values, backgroundColor: s.color, borderRadius: 1,
        maxBarThickness: hz ? 14 : 34, categoryPercentage: 0.75, barPercentage: 0.9,
        noLabel: !!s.prev, labelColor: ink };
    });
    function truncLabel(v) {
      var l = String(this.getLabelForValue(v));
      return l.length > 24 ? l.slice(0, 23) + '…' : l;
    }
    // Nhãn dài trên trục ngang: xuống dòng theo từ thay vì xoay nghiêng
    function wrapLabel(v) {
      var l = String(this.getLabelForValue(v));
      if (l.length <= 10) return l;
      var lines = [], cur = '';
      l.split(' ').forEach(function (w) {
        if (cur && (cur + ' ' + w).length > 12) { lines.push(cur); cur = w; } else cur = cur ? cur + ' ' + w : w;
      });
      if (cur) lines.push(cur);
      return lines.length > 3 ? lines.slice(0, 2).concat([lines.slice(2).join(' ').slice(0, 13) + '…']) : lines;
    }
    var many = m.labels.length > 12;
    var fromZero = c.begin_at_zero !== undefined ? !!c.begin_at_zero : c.type !== 'line';
    var catAxis = { stacked: m.stacked, grid: { display: false }, border: { color: line },
      ticks: { color: hz ? ink : muted, autoSkip: !hz && many, maxRotation: hz ? 0 : (many ? 50 : 0),
               callback: hz ? truncLabel : wrapLabel,
               font: { weight: hz ? '600' : '400', size: many ? 10 : 11 } } };
    var valAxis = { stacked: m.stacked, beginAtZero: fromZero, border: { display: false },
      grid: { display: false },   // bỏ lưới gióng ngang cho thoáng; giá trị đã ghi trên cột/điểm
      ticks: { color: muted, padding: 6, callback: function (v) { return F.axis(kind, v); } } };
    if (hz) { valAxis.display = false; }
    if (fromZero) {
      valAxis.grace = '10%';
    } else {
      var lo = Infinity, hi = -Infinity;
      m.series.forEach(function (s) { s.values.forEach(function (v) { if (ok(v)) { if (v < lo) lo = v; if (v > hi) hi = v; } }); });
      if (lo <= hi) {
        var pad = (hi - lo) * 0.15 || Math.abs(hi) * 0.1 || 1;
        valAxis.suggestedMin = (lo >= 0 && lo - pad < 0) ? 0 : lo - pad;
        valAxis.suggestedMax = hi + pad;
      }
    }
    var options = {
      indexAxis: hz ? 'y' : 'x',
      responsive: true, maintainAspectRatio: false, animation: false,
      layout: { padding: { top: hz ? 2 : 16, right: hz && showLabels ? 48 : (c.type === 'line' ? 22 : 8), left: c.type === 'line' ? 6 : 0 } },
      plugins: {
        valueLabels: { show: showLabels, color: ink, kind: kind || 'compact', stacked: m.stacked, style: style },
        legend: {
          display: c.legend !== undefined ? !!c.legend : (isPie || m.series.length > 1),
          position: isPie ? 'bottom' : 'top', align: isPie ? 'center' : 'end',
          labels: { color: ink, usePointStyle: true, pointStyle: 'circle', boxWidth: 7, boxHeight: 7, font: { size: 11 } }
        },
        tooltip: { callbacks: { label: function (ctx) {
          if (isPie) {
            var tot = ctx.dataset.data.reduce(function (a, b) { return a + (typeof b === 'number' ? b : 0); }, 0);
            return ctx.label + ': ' + F.full(kind || 'number', ctx.parsed) + (tot ? ' (' + F.pct(ctx.parsed / tot) + ')' : '');
          }
          var v = hz ? ctx.parsed.x : ctx.parsed.y;
          return ctx.dataset.label + ': ' + F.full(kind && kind !== 'compact' ? kind : 'number', v);
        } } }
      },
      scales: isPie ? {} : (hz ? { x: valAxis, y: catAxis } : { x: catAxis, y: valAxis })
    };
    if (!isPie) options.interaction = { mode: 'index', intersect: false };
    return new Chart(canvas, { type: c.type, data: { labels: m.labels, datasets: datasets }, plugins: [labelPlugin], options: options });
  }

  function hasData(m) {
    return m.labels.length > 0 && m.series.some(function (s) {
      return s.values.some(function (v) { return v !== null; });
    });
  }

  function renderKpis(view, f) {
    var d = view.d, box = view.kpiBox, F = view.F;
    box.textContent = '';
    box.hidden = d.kpis.length === 0;
    var hasPrev = d._ci.ky !== undefined;
    d.kpis.forEach(function (k) {
      var cv = metric(f.cur, d, k.column, k.agg, k.denom);
      var card = el('div', 'card');
      card.appendChild(el('div', 'l', k.label));
      card.appendChild(el('div', 'v', F.full(k.format, cv)));
      if (hasPrev) {
        var pv = metric(f.prev, d, k.column, k.agg, k.denom);
        var ln = el('div', 'd flat');
        if (cv === null || pv === null || (pv === 0 && k.format !== 'percent')) {
          ln.textContent = 'Không có số liệu kỳ trước';
        } else {
          var isPct = k.format === 'percent';    // tỉ lệ: so bằng điểm %
          var dl = isPct ? cv - pv : (cv - pv) / Math.abs(pv);
          if (Math.abs(dl) < 0.0005) {
            ln.textContent = isPct ? 'Không đổi so với kỳ trước' : F.pct(0) + ' so với kỳ trước';
          } else {
            if (k.good_when !== 'neutral') {
              var good = (dl > 0) === (k.good_when !== 'down');
              ln.className = 'd ' + (good ? 'good' : 'bad');
            }
            ln.textContent = (dl > 0 ? '▲ ' : '▼ ') + F.pct(dl).replace('%', isPct ? ' điểm %' : '%') + ' so với kỳ trước';
          }
          card.title = 'Kỳ trước: ' + F.full(k.format, pv);
        }
        card.appendChild(ln);
      }
      if (k.hint) card.appendChild(el('div', 'h', k.hint));
      box.appendChild(card);
    });
  }

  function renderSub(view) {
    var d = view.d, parts = [];
    d.filters.forEach(function (f) {
      if (f.type === 'date') {
        var r = view.st[f.column] || ['', ''], bd = view.bounds[f.column] || ['', ''];
        var a = r[0] || bd[0], b = r[1] || bd[1];
        if (a || b) parts.push(viDate(a) + ' – ' + viDate(b));
      } else {
        parts.push(view.st[f.column] ? view.st[f.column] : 'Tất cả ' + f.label.toLowerCase());
      }
    });
    view.sub.textContent = parts.join(' · ');
  }

  // ---------- CSV ----------
  function csvCell(v) {
    if (v === null || v === undefined) return '';
    var s = String(v);
    if (typeof v === 'string' && /^[=+\-@\t\r]/.test(s)) s = "'" + s;   // chặn công thức khi mở bằng Excel
    return /[",\n\r;]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }
  function downloadCsv(name, columns, rows) {
    var lines = [columns.map(csvCell).join(',')].concat(rows.map(function (r) { return r.map(csvCell).join(','); }));
    var blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (String(name).replace(/[\\\/:*?"<>|]+/g, ' ').trim().slice(0, 80) || 'du-lieu') + '.csv';
    document.body.appendChild(a); a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 0);
  }

  // ---------- khối ----------
  function buildPanels(view) {
    view.panels = view.d.charts.map(function (c) {
      var panel = el('section', 'panel');
      panel.style.setProperty('--span', String(Math.min(12, Math.max(1, c.span || 6))));
      var head = el('div', 'ph');
      head.appendChild(el('h2', '', c.title));
      var p = { c: c, body: el('div'), csv: null, chart: null };
      panel.appendChild(head); panel.appendChild(p.body);
      view.grid.appendChild(panel);
      return p;
    });
  }
  var GRID_TYPES = { table: computeTable, pivot: computePivot, heatmap: computeHeat };
  function renderPanels(view, f) {
    var d = view.d, tc = themeColors(d.theme), F = view.F;
    view.panels.forEach(function (p) {
      var c = p.c, m = null;
      if (p.chart) { try { p.chart.destroy(); } catch (e) {} p.chart = null; }
      p.body.textContent = '';
      p.csv = null;
      try {
        if (GRID_TYPES[c.type]) {
          var t = GRID_TYPES[c.type](c, d, f, F, tc);
          p.csv = t.csv;
          if (!t.body.length) { p.body.appendChild(el('p', 'empty', 'Không có dữ liệu cho mục này.')); return; }
          p.body.appendChild(renderGrid(t.head, t.body, t.foot, c.type === 'table' ? '' : c.type === 'pivot' ? 'pivot' : 'heat'));
          if (t.note) p.body.appendChild(el('p', 'note', t.note));
          return;
        }
        m = compute(c, d, f, tc);
        p.csv = modelCsv(m);
        if (!hasData(m)) { p.body.appendChild(el('p', 'empty', 'Không có dữ liệu cho mục này.')); return; }
        if (typeof Chart === 'undefined') { p.body.appendChild(modelAsTable(m, F)); return; }
        p.chart = buildChart(c, m, tc, p.body, view.style);
      } catch (e) {
        p.body.textContent = '';
        if (m && hasData(m)) p.body.appendChild(modelAsTable(m, F));
        else p.body.appendChild(el('p', 'empty', 'Không hiển thị được mục này.'));
        if (window.console) console.error('[dashboard]', c.title, e);
      }
    });
  }

  function refresh(view) {
    var f = filtered(view);
    renderSub(view);
    renderKpis(view, f);
    renderPanels(view, f);
  }

  // ---------- bộ lọc ----------
  function buildFilters(view) {
    var d = view.d, bar = view.filterBar;
    view.resets = [];
    d.filters.forEach(function (f) {
      var ci = d._ci[f.column];
      if (ci === undefined) return;
      var box = el('div', 'f');
      box.appendChild(el('label', '', f.label));
      if (f.type === 'date') {
        var lo = null, hi = null, kyi = d._ci.ky;
        d.dataset.rows.forEach(function (r) {
          if (kyi !== undefined && r[kyi] !== 'nay') return;
          var s = day(r[ci]);
          if (!s) return;
          if (lo === null || s < lo) lo = s;
          if (hi === null || s > hi) hi = s;
        });
        view.bounds[f.column] = [lo || '', hi || ''];
        var wrap = el('div', 'dates');
        var from = el('input'), to = el('input');
        [from, to].forEach(function (inp) {
          inp.type = 'date';
          if (lo) inp.min = lo;
          if (hi) inp.max = hi;
          inp.addEventListener('change', function () {
            var a = from.value, b = to.value;
            if (a && b && a > b) { var t = a; a = b; b = t; from.value = a; to.value = b; }
            view.st[f.column] = [a, b];
            refresh(view);
          });
        });
        from.value = lo || ''; to.value = hi || '';
        from.setAttribute('aria-label', f.label + ' từ'); to.setAttribute('aria-label', f.label + ' đến');
        wrap.appendChild(from); wrap.appendChild(to);
        box.appendChild(wrap);
        view.resets.push(function () { from.value = lo || ''; to.value = hi || ''; });
      } else {
        var sel = el('select');
        sel.setAttribute('aria-label', f.label);
        var o0 = el('option', '', 'Tất cả'); o0.value = ''; sel.appendChild(o0);
        var vals = Array.from(new Set(d.dataset.rows.map(function (r) { return key(r[ci]); })));
        vals.sort(function (a, b) { return a.localeCompare(b, 'vi', { numeric: true }); });
        vals.forEach(function (x) { var o = el('option', '', x); o.value = x; sel.appendChild(o); });
        sel.addEventListener('change', function () { view.st[f.column] = sel.value; refresh(view); });
        box.appendChild(sel);
        view.resets.push(function () { sel.value = ''; });
      }
      bar.appendChild(box);
    });
    if (view.resets.length) {
      var rb = el('button', 'reset', 'Đặt lại');
      rb.type = 'button';
      rb.addEventListener('click', function () {
        view.st = {};
        view.resets.forEach(function (fn) { fn(); });
        refresh(view);
      });
      bar.appendChild(rb);
    }
  }

  // ---------- chuẩn hoá dữ liệu đầu vào ----------
  function prepare(d) {
    d.filters = d.filters || []; d.kpis = d.kpis || []; d.charts = d.charts || [];
    d.dataset = d.dataset || { columns: [], rows: [] };
    var cols = d.dataset.columns || [];
    d.dataset.columns = cols;
    d.dataset.rows = (d.dataset.rows || []).map(function (r) {
      return Array.isArray(r) ? r : cols.map(function (c) { return r ? r[c] : null; });
    });
    d._ci = {};
    cols.forEach(function (c, k) { d._ci[c] = k; });
    // Số đo đến dạng chuỗi (Decimal của Postgres) -> số
    var idx = new Set();
    function add(c) { if (c && d._ci[c] !== undefined) idx.add(d._ci[c]); }
    function addSpec(s) { if (s) { add(s.column); add(s.denom); } }
    d.kpis.forEach(addSpec);
    d.charts.forEach(function (c) {
      (c.y || []).forEach(add); add(c.denom); add(c.x_order); add(c.row_order); add(c.sort_by);
      (c.columns || []).forEach(addSpec); (c.values || []).forEach(addSpec); addSpec(c.value);
    });
    if (idx.size) {
      d.dataset.rows.forEach(function (r) {
        idx.forEach(function (i) {
          var x = r[i];
          if (typeof x === 'string' && x.trim() !== '' && isFinite(+x)) r[i] = +x;
        });
      });
    }
  }

  // ---------- khung trang, tab ----------
  var title = data.title || 'Dashboard';
  document.title = title;
  var titleEl = document.getElementById('title');
  titleEl.textContent = title;
  var logoEl = document.getElementById('logo');
  if (typeof data.logo === 'string' && /^data:image\/(png|jpeg|webp|gif|svg\+xml);base64,[A-Za-z0-9+\/=]+$/.test(data.logo)) {
    logoEl.src = data.logo; logoEl.hidden = false;
  }
  document.getElementById('meta').textContent =
    'Tạo lúc ' + (data.generated || '?') + '. Số liệu chụp tại thời điểm tạo, không tự cập nhật.' +
    (data.expires ? ' Link hết hạn lúc ' + data.expires + '.' : '');

  var dashboards = data.dashboards || [];
  var tabsEl = document.getElementById('tabs');
  var sideEl = document.getElementById('side');
  var viewsEl = document.getElementById('views');
  var subSlot = document.getElementById('subSlot');
  var filterSlot = document.getElementById('filterSlot');
  var views = [], buttons = [], rendered = [], current = 0;

  if (!dashboards.length) {
    viewsEl.appendChild(el('p', 'empty', 'Dashboard không có dữ liệu.'));
    return;
  }

  function show(i, focus) {
    current = i;
    views.forEach(function (v, k) { v.sec.hidden = k !== i; });
    titleEl.textContent = dashboards[i].title || title;
    subSlot.textContent = ''; subSlot.appendChild(views[i].sub);
    filterSlot.textContent = ''; filterSlot.appendChild(views[i].filterBar);
    buttons.forEach(function (b, k) {
      b.setAttribute('aria-selected', k === i ? 'true' : 'false');
      b.tabIndex = k === i ? 0 : -1;
    });
    applyTheme(dashboards[i].theme);
    if (!rendered[i]) { rendered[i] = true; refresh(views[i]); }
    if (focus && buttons[i]) buttons[i].focus();
    if (dashboards.length > 1) {
      try { history.replaceState(null, '', '#' + (i + 1)); } catch (e) {}
    }
  }

  dashboards.forEach(function (d, i) {
    prepare(d);
    var style = (d.theme && d.theme.number_style) || 'vi';
    var sec = el('div', 'view');
    sec.id = 'view-' + i;
    sec.setAttribute('role', 'tabpanel');
    sec.hidden = true;
    var sub = el('p', 'sub');
    var filterBar = el('div', 'filters');
    var kpiBox = el('div', 'kpis');
    var grid = el('div', 'grid');
    sec.appendChild(kpiBox); sec.appendChild(grid);
    viewsEl.appendChild(sec);
    var view = { d: d, style: style, F: fmtFor(style), st: {}, bounds: {}, sec: sec, sub: sub,
      filterBar: filterBar, kpiBox: kpiBox, grid: grid, resets: [], panels: [] };
    buildFilters(view);
    buildPanels(view);
    views.push(view);

    if (dashboards.length > 1) {
      var b = el('button', 'tab', d.name || ('Trang ' + (i + 1)));
      b.type = 'button';
      b.id = 'tab-' + i;
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-controls', 'view-' + i);
      sec.setAttribute('aria-labelledby', 'tab-' + i);
      b.addEventListener('click', function () { show(i, false); });
      b.addEventListener('keydown', function (ev) {
        var n = dashboards.length;
        if (ev.key === 'ArrowDown' || ev.key === 'ArrowRight') { ev.preventDefault(); show((i + 1) % n, true); }
        else if (ev.key === 'ArrowUp' || ev.key === 'ArrowLeft') { ev.preventDefault(); show((i - 1 + n) % n, true); }
      });
      tabsEl.appendChild(b);
      buttons.push(b);
    }
  });

  if (mq) {
    var onScheme = function (e) {
      dark = e.matches;
      applyTheme(dashboards[current].theme);
      views.forEach(function (v, k) {
        if (!rendered[k]) return;
        if (k === current) refresh(v); else rendered[k] = false;
      });
    };
    if (mq.addEventListener) mq.addEventListener('change', onScheme);
    else if (mq.addListener) mq.addListener(onScheme);
  }

  if (dashboards.length > 1) sideEl.hidden = false;
  var start = parseInt((location.hash || '').slice(1), 10) - 1;
  show(start >= 0 && start < dashboards.length ? start : 0, false);
})();
</script>
</body>
</html>
"""

_LOGO_RE = re.compile(r"^data:image/(png|jpeg|webp|gif|svg\+xml);base64,[A-Za-z0-9+/=]+$")
_LOGO_MIME = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp", ".gif": "gif", ".svg": "svg+xml"}


def logo_data_uri(path: str | Path) -> str:
    """Đọc file logo (png/jpg/webp/gif/svg) thành data URI để đặt vào payload['logo']."""
    p = Path(path)
    mime = _LOGO_MIME.get(p.suffix.lower())
    if not mime:
        raise ValueError(f"Định dạng logo không hỗ trợ: {p.suffix}")
    return f"data:image/{mime};base64," + base64.b64encode(p.read_bytes()).decode("ascii")


def _clean(o):
    """Đưa giá trị từ DB về kiểu JSON hợp lệ: Decimal -> float, date -> 'YYYY-MM-DD', NaN/Inf -> null."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, Decimal):
        return float(o) if o.is_finite() else None
    if isinstance(o, (datetime, date, time)):
        return o.isoformat()
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def embed_json(payload: dict) -> str:
    """JSON an toàn để đặt trong <script type="application/json">."""
    s = json.dumps(_clean(payload), ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False)
    return (
        s.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_html(payload: dict) -> str:
    """payload = {title, generated, expires, logo?, dashboards: [tab, ...]}; tab dựng bằng
    dashboard_presets.build_dashboard()/build_report() hoặc tự khai báo cùng cấu trúc."""
    logo = payload.get("logo")
    if logo is not None and not (isinstance(logo, str) and _LOGO_RE.match(logo)):
        payload = {**payload, "logo": None}
    return TEMPLATE.replace("__PAYLOAD__", embed_json(payload), 1)
