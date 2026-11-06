# app/agent/tools/dashboard_template.py
# Template HTML cố định của dashboard. Dữ liệu được nhúng dưới dạng JSON vào chỗ
# __PAYLOAD__ và mọi chữ đều được đưa vào trang bằng textContent, nên nội dung lấy từ
# database (tên cửa hàng, tên món...) không thể chèn mã vào trang.
# Chart.js được nginx (dashboards-web) phục vụ cùng máy chủ tại /chart.umd.min.js, không
# nạp từ CDN; không tải được hoặc vẽ lỗi thì biểu đồ rơi về dạng bảng số liệu.
#
# Mỗi dashboard con (tab) mang theo MỘT dataset chi tiết + khai báo bộ lọc, KPI, biểu đồ.
# Trình duyệt tự lọc, cộng gộp và vẽ lại khi người xem đổi bộ lọc (không gọi lại server).
# Từ 2 dashboard trở lên thì hiện cột nút chọn dashboard bên trái (điện thoại: hàng ngang phía trên);
# bộ lọc của tab đang xem nằm trên đầu trang cạnh tiêu đề. Biểu đồ của mỗi tab
# chỉ được vẽ lần đầu khi tab đó được mở (Chart.js cần khung đang hiển thị để đo kích thước).
# Giao diện: nền sáng/tối theo máy người xem, lưới 12 cột (1 cột trên điện thoại).

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
    --bg: #F2F4F1; --panel: #FFFFFF; --ink: #18241F; --muted: #5E6B64; --line: #DCE1DC;
    --good: #1B7F3B; --bad: #C8202B;
    --c1: #1F6F6B; --c2: #9A5B35; --c3: #D6A23A; --c4: #5C6F8A; --c5: #B24D69; --c6: #7E8F3F;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #101614; --panel: #18211E; --ink: #E7ECE8; --muted: #9AA8A0; --line: #2A3631;
      --good: #5CC27E; --bad: #F0707A;
      --c1: #4FB3AC; --c2: #D08A5C; --c3: #E4B95A; --c4: #8FA6C6; --c5: #D77A93; --c6: #A5B76A;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  }
  main { max-width: 1440px; margin: 0 auto; padding: 20px 16px 48px; }
  .layout { display: flex; gap: 20px; align-items: flex-start; }
  .side { flex: none; width: 190px; position: sticky; top: 20px; }
  .side[hidden] { display: none; }
  .content { flex: 1 1 0; min-width: 0; }
  .top { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px 24px; flex-wrap: wrap; margin-bottom: 16px; }
  header { min-width: 0; }
  @media (max-width: 820px) {
    .layout { flex-direction: column; gap: 12px; }
    .side { width: 100%; position: static; }
  }
  h1 {
    margin: 0 0 4px; font-size: clamp(1.35rem, 4vw, 1.9rem); font-weight: 700;
    letter-spacing: -0.01em; line-height: 1.25; text-transform: uppercase;
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
  .sub { margin: 0; color: var(--muted); font-size: .875rem; }
  .filters { display: flex; gap: 10px; flex-wrap: wrap; align-items: flex-end; }
  .f label { display: block; font-size: .72rem; color: var(--muted); margin-bottom: 2px; }
  .f select, .f input {
    font: inherit; font-size: .85rem; padding: 6px 8px; min-width: 130px; max-width: 220px;
    border: 1px solid var(--line); border-radius: 6px; background: var(--panel); color: var(--ink);
  }
  .f .dates { display: flex; gap: 4px; }
  .f .dates input { min-width: 0; width: 132px; }
  .f select:focus-visible, .f input:focus-visible, .reset:focus-visible { outline: 2px solid var(--brand, var(--c1)); outline-offset: 1px; }
  .reset {
    font: inherit; font-size: .85rem; padding: 6px 12px; cursor: pointer;
    border: 1px solid var(--line); border-radius: 6px; background: var(--panel); color: var(--ink);
  }

  .kpis { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); margin-bottom: 16px; }
  .kpis[hidden] { display: none; }
  .card, .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px; min-width: 0; }
  .card .l { color: var(--muted); font-size: .9rem; }
  .card .v { font-size: 1.9rem; font-weight: 700; line-height: 1.25; font-variant-numeric: tabular-nums; }
  .card .d { font-size: .8rem; font-weight: 600; margin-top: 2px; }
  .card .d.good { color: var(--good); }
  .card .d.bad { color: var(--bad); }
  .card .d.flat { color: var(--muted); font-weight: 400; }

  .grid { display: grid; gap: 16px; grid-template-columns: repeat(12, minmax(0, 1fr)); }
  .grid > .panel { grid-column: span var(--span, 6); }
  @media (max-width: 820px) { .grid > .panel { grid-column: 1 / -1; } }
  h2 { margin: 0 0 12px; font-size: 1rem; font-weight: 600; line-height: 1.3; }
  .chart { position: relative; height: 320px; }
  .scroll { overflow: auto; max-height: 420px; }
  table { width: 100%; border-collapse: collapse; font-size: .875rem; }
  th, td { padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: left; white-space: nowrap; }
  th { color: var(--muted); font-weight: 600; position: sticky; top: 0; background: var(--panel); }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .empty { margin: 0; color: var(--muted); }
</style>
</head>
<body>
<main>
  <div class="layout">
    <nav class="side" id="side" hidden>
      <div class="tabs" id="tabs" role="tablist" aria-orientation="vertical" aria-label="Chọn dashboard"></div>
    </nav>
    <div class="content">
      <div class="top">
        <header>
          <h1 id="title"></h1>
          <div id="subSlot"></div>
        </header>
        <div id="filterSlot"></div>
      </div>
      <div id="views"></div>
      <p class="meta" id="meta"></p>
    </div>
  </div>
</main>

<script type="application/json" id="payload">__PAYLOAD__</script>
<script src="chart.umd.min.js"></script>
<script>
(function () {
  var data = JSON.parse(document.getElementById('payload').textContent);
  var root = document.documentElement;
  var dark = !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  function pick(x) { return Array.isArray(x) ? x[dark ? 1 : 0] : x; }
  function cssv(n) { return getComputedStyle(root).getPropertyValue(n).trim(); }

  var NF = new Intl.NumberFormat('vi-VN', { maximumFractionDigits: 2 });
  var NFC = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 2 });
  var NFD = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  var NFP = new Intl.NumberFormat('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  function fmt(kind, v) {
    if (v === null || v === undefined || !isFinite(v)) return '–';
    return kind === 'number' ? NF.format(v) : kind === 'decimal' ? NFD.format(v) : NFC.format(v);
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function cellText(value) {
    return typeof value === 'number' ? NF.format(value) : (value === null || value === undefined ? '' : String(value));
  }
  function key(v) { return v === null || v === undefined ? '(trống)' : String(v); }
  function viDate(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || '');
    return m ? m[3] + '/' + m[2] + '/' + m[1] : (s || '');
  }

  // ---------- màu theo theme của từng dashboard ----------
  function themeColors(t) {
    t = t || {};
    var brand = pick(t.brand) || cssv('--c1');
    return {
      brand: brand,
      prev: pick(t.prev) || cssv('--muted'),
      palette: [brand, cssv('--c2'), cssv('--c3'), cssv('--c4'), cssv('--c5'), cssv('--c6')],
      series: t.series_colors || {}
    };
  }
  function seriesRank(tc, name) {
    var keys = Object.keys(tc.series);
    for (var k = 0; k < keys.length; k++) if (name.indexOf(keys[k]) === 0) return k;
    return keys.length;
  }
  function seriesColor(tc, name, idx) {
    var keys = Object.keys(tc.series);
    for (var k = 0; k < keys.length; k++) if (name.indexOf(keys[k]) === 0) return pick(tc.series[keys[k]]);
    return tc.palette[idx % tc.palette.length];
  }
  function applyTheme(t) {
    t = t || {};
    root.style.setProperty('--brand', pick(t.brand) || cssv('--c1'));
    root.style.setProperty('--title', pick(t.title) || cssv('--ink'));
  }

  // ---------- lọc và cộng gộp dữ liệu ----------
  function rowsFor(d, st, ky) {
    var cols = d._ci, kyi = cols.ky;
    return d.dataset.rows.filter(function (r) {
      if (kyi === undefined) { if (ky !== 'nay') return false; }
      else if (r[kyi] !== ky) return false;
      return d.filters.every(function (f) {
        var val = st[f.column];
        if (val === undefined || val === null || val === '') return true;
        var x = r[cols[f.column]];
        if (f.type === 'date') {
          var s = x === null || x === undefined ? '' : String(x);
          return (!val[0] || s >= val[0]) && (!val[1] || s <= val[1]);
        }
        return key(x) === val;
      });
    });
  }
  function groupBy(rows, xi) {
    var m = new Map();
    rows.forEach(function (r) {
      var k = key(r[xi]);
      if (!m.has(k)) m.set(k, []);
      m.get(k).push(r);
    });
    return m;
  }
  function sumCol(rows, i) {
    var s = 0, any = false;
    rows.forEach(function (r) { var x = r[i]; if (typeof x === 'number') { s += x; any = true; } });
    return any ? s : null;
  }
  function metric(rows, d, col, agg, denom) {
    var i = d._ci[col];
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
    function ord(k) {
      var m = Infinity;
      groups.forEach(function (g) {
        (g.get(k) || []).forEach(function (r) { if (typeof r[oi] === 'number' && r[oi] < m) m = r[oi]; });
      });
      return m;
    }
    keys.sort(function (a, b) {
      if (oi >= 0) { var diff = ord(a) - ord(b); if (diff) return diff; }
      return a.localeCompare(b, 'vi', { numeric: true });
    });
  }
  function unionKeys(maps) {
    var seen = {}, out = [];
    maps.forEach(function (m) { m.forEach(function (_, k) { if (!seen[k]) { seen[k] = 1; out.push(k); } }); });
    return out;
  }

  function compute(c, d, st, tc) {
    var cur = rowsFor(d, st, 'nay'), prev = rowsFor(d, st, 'truoc');
    var xi = d._ci[c.x], oi = c.x_order ? d._ci[c.x_order] : -1;
    var useCompare = !!c.compare && c.type !== 'pie' && !c.color_by && d._ci.ky !== undefined;
    var gc = groupBy(cur, xi), gp = useCompare ? groupBy(prev, xi) : new Map();
    var keys = unionKeys([gc, gp]);
    sortKeys(keys, [gc, gp], oi);
    function mk(g, col) {
      return keys.map(function (k) { var rs = g.get(k); return rs ? metric(rs, d, col, c.agg, c.denom) : null; });
    }
    var series = [];
    if (c.color_by) {
      var cb = d._ci[c.color_by], names = [], seen = {};
      cur.forEach(function (r) { var n = key(r[cb]); if (!seen[n]) { seen[n] = 1; names.push(n); } });
      names.sort(function (a, b) { return seriesRank(tc, a) - seriesRank(tc, b); });
      names.forEach(function (n, j) {
        var sub = cur.filter(function (r) { return key(r[cb]) === n; });
        series.push({ name: n, values: mk(groupBy(sub, xi), c.y[0]), color: seriesColor(tc, n, j) });
      });
    } else {
      c.y.forEach(function (col, j) {
        var single = c.y.length === 1;
        series.push({
          name: useCompare ? (single ? 'Kỳ này' : col + ' – kỳ này') : col,
          values: mk(gc, col), color: tc.palette[j % tc.palette.length]
        });
        if (useCompare) {
          series.push({
            name: single ? 'Kỳ trước' : col + ' – kỳ trước',
            values: mk(gp, col), color: tc.prev, prev: true
          });
        }
      });
    }
    if ((c.sort === 'asc' || c.sort === 'desc') && series.length && !c.color_by) {
      var base = series[0].values, idx = keys.map(function (_, i) { return i; });
      idx.sort(function (a, b) {
        var va = base[a], vb = base[b];
        if (va === null && vb === null) return 0;
        if (va === null) return 1;
        if (vb === null) return -1;
        return c.sort === 'asc' ? va - vb : vb - va;
      });
      keys = idx.map(function (i) { return keys[i]; });
      series.forEach(function (s) { s.values = idx.map(function (i) { return s.values[i]; }); });
    }
    return { labels: keys, series: series, stacked: !!c.color_by, xName: c.x };
  }

  function computeTable(c, d, st) {
    var cur = rowsFor(d, st, 'nay');
    if (!c.x) {
      return { columns: d.dataset.columns, rows: cur.slice(0, 200) };
    }
    var xi = d._ci[c.x], oi = c.x_order ? d._ci[c.x_order] : -1;
    var g = groupBy(cur, xi), keys = unionKeys([g]);
    sortKeys(keys, [g], oi);
    return {
      columns: [c.x].concat(c.y),
      rows: keys.map(function (k) {
        return [k].concat(c.y.map(function (col) { return metric(g.get(k), d, col, c.agg, c.denom); }));
      })
    };
  }

  // ---------- vẽ ----------
  function buildTable(columns, rows) {
    var wrap = el('div', 'scroll');
    var table = el('table');
    var head = el('tr');
    columns.forEach(function (c, i) {
      var numeric = rows.length > 0 && typeof rows[0][i] === 'number';
      head.appendChild(el('th', numeric ? 'num' : '', c));
    });
    var thead = el('thead'); thead.appendChild(head); table.appendChild(thead);
    var tbody = el('tbody');
    rows.forEach(function (r) {
      var tr = el('tr');
      r.forEach(function (val) { tr.appendChild(el('td', typeof val === 'number' ? 'num' : '', cellText(val))); });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody); wrap.appendChild(table);
    return wrap;
  }
  function modelAsTable(m) {
    var columns = [m.xName || 'Nhãn'].concat(m.series.map(function (s) { return s.name; }));
    var rows = m.labels.map(function (label, i) {
      return [label].concat(m.series.map(function (s) { return s.values[i]; }));
    });
    return buildTable(columns, rows);
  }

  // Nhãn số trên đầu cột/điểm, tự vẽ để không phải nạp thêm plugin
  var labelPlugin = {
    id: 'valueLabels',
    afterDatasetsDraw: function (chart, args, opts) {
      if (!opts || !opts.show) return;
      var ctx = chart.ctx;
      ctx.save();
      ctx.font = '600 10px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'bottom';
      chart.data.datasets.forEach(function (ds, di) {
        var meta = chart.getDatasetMeta(di);
        if (meta.hidden) return;
        ctx.fillStyle = ds.labelColor || opts.color;
        meta.data.forEach(function (pt, i) {
          var val = ds.data[i];
          if (val === null || val === undefined || val === 0) return;
          ctx.fillText(NFC.format(val), pt.x, pt.y - 4);
        });
      });
      ctx.restore();
    }
  };

  function buildChart(c, m, tc, view) {
    var isPie = c.type === 'pie';
    var wrap = el('div', 'chart');
    var canvas = el('canvas');
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', c.title);
    wrap.appendChild(canvas);
    var ink = cssv('--ink'), muted = cssv('--muted'), line = cssv('--line'), panel = cssv('--panel');
    var showLabels = !isPie && m.labels.length <= 31;
    var datasets = m.series.map(function (s) {
      if (isPie) {
        return { label: s.name, data: s.values,
          backgroundColor: m.labels.map(function (_, k) { return tc.palette[k % tc.palette.length]; }),
          borderColor: panel, borderWidth: 2 };
      }
      if (c.type === 'line') {
        return { label: s.name, data: s.values, borderColor: s.color, backgroundColor: s.color,
          borderWidth: 2, tension: 0.2, pointRadius: 3, spanGaps: true,
          borderDash: s.prev ? [5, 4] : [], labelColor: s.prev ? muted : ink };
      }
      return { label: s.name, data: s.values, backgroundColor: s.color, borderRadius: 2,
        maxBarThickness: 34, labelColor: s.prev ? muted : ink };
    });
    var chart = new Chart(canvas, {
      type: c.type,
      data: { labels: m.labels, datasets: datasets },
      plugins: [labelPlugin],
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        layout: { padding: { top: 18 } },
        plugins: {
          valueLabels: { show: showLabels, color: ink },
          legend: {
            display: isPie || m.series.length > 1,
            position: isPie ? 'bottom' : 'top', align: isPie ? 'center' : 'end',
            labels: { color: ink, boxWidth: 10, boxHeight: 10, font: { size: 11 } }
          },
          tooltip: { callbacks: { label: function (ctx) {
            var name = isPie ? ctx.label : ctx.dataset.label;
            var value = (ctx.parsed && ctx.parsed.y !== undefined) ? ctx.parsed.y : ctx.parsed;
            return name + ': ' + NF.format(value);
          } } }
        },
        scales: isPie ? {} : {
          x: { stacked: m.stacked, ticks: { color: muted, maxRotation: 60, autoSkip: true }, grid: { display: false } },
          y: { stacked: m.stacked, beginAtZero: true, grace: '8%',
               ticks: { color: muted, callback: function (val) { return NFC.format(val); } },
               grid: { display: false, drawBorder: false }, border: { display: false } }
        }
      }
    });
    view.charts.push(chart);
    return wrap;
  }

  function hasData(m) {
    return m.labels.length > 0 && m.series.some(function (s) {
      return s.values.some(function (v) { return v !== null; });
    });
  }

  function renderKpis(view) {
    var d = view.d, box = view.kpiBox;
    box.textContent = '';
    box.hidden = d.kpis.length === 0;
    var cur = rowsFor(d, view.st, 'nay');
    var hasPrev = d._ci.ky !== undefined;
    var prev = hasPrev ? rowsFor(d, view.st, 'truoc') : [];
    d.kpis.forEach(function (k) {
      var cv = metric(cur, d, k.column, k.agg, k.denom);
      var card = el('div', 'card');
      card.appendChild(el('div', 'l', k.label));
      card.appendChild(el('div', 'v', fmt(k.format, cv)));
      if (hasPrev) {
        var pv = metric(prev, d, k.column, k.agg, k.denom);
        var ln = el('div', 'd flat');
        if (cv === null || pv === null || pv === 0) {
          ln.textContent = 'Không có số liệu kỳ trước';
        } else {
          var dl = (cv - pv) / Math.abs(pv);
          if (Math.abs(dl) < 0.0005) {
            ln.textContent = '0,0% so với kỳ trước';
          } else {
            var good = (dl > 0) === (k.good_when !== 'down');
            ln.className = 'd ' + (good ? 'good' : 'bad');
            ln.textContent = (dl > 0 ? '▲ ' : '▼ ') + NFP.format(Math.abs(dl) * 100) + '% so với kỳ trước';
          }
        }
        card.appendChild(ln);
      }
      box.appendChild(card);
    });
  }

  function renderSub(view) {
    var d = view.d, parts = [];
    d.filters.forEach(function (f) {
      if (f.type === 'date') {
        var r = view.st[f.column] || view.bounds[f.column] || ['', ''];
        var a = r[0] || (view.bounds[f.column] || [''])[0], b = r[1] || (view.bounds[f.column] || ['', ''])[1];
        if (a || b) parts.push(viDate(a) + ' – ' + viDate(b));
      } else {
        parts.push(view.st[f.column] ? view.st[f.column] : 'Tất cả ' + f.label.toLowerCase());
      }
    });
    view.sub.textContent = parts.join(' · ');
  }

  function renderCharts(view) {
    var d = view.d, tc = themeColors(d.theme);
    view.charts.forEach(function (ch) { try { ch.destroy(); } catch (e) {} });
    view.charts = [];
    view.grid.textContent = '';
    d.charts.forEach(function (c) {
      var panel = el('section', 'panel');
      panel.style.setProperty('--span', String(c.span || 6));
      panel.appendChild(el('h2', '', c.title));
      if (c.type === 'table') {
        var t = computeTable(c, d, view.st);
        if (!t.rows.length) panel.appendChild(el('p', 'empty', 'Không có dữ liệu cho mục này.'));
        else panel.appendChild(buildTable(t.columns, t.rows));
      } else {
        var m = compute(c, d, view.st, tc);
        if (!hasData(m)) {
          panel.appendChild(el('p', 'empty', 'Không có dữ liệu cho mục này.'));
        } else if (typeof Chart === 'undefined') {
          panel.appendChild(modelAsTable(m));
        } else {
          try { panel.appendChild(buildChart(c, m, tc, view)); }
          catch (e) { panel.appendChild(modelAsTable(m)); }
        }
      }
      view.grid.appendChild(panel);
    });
  }

  function refresh(view) {
    renderSub(view);
    renderKpis(view);
    renderCharts(view);
  }

  // ---------- bộ lọc ----------
  function buildFilters(view) {
    var d = view.d, bar = view.filterBar;
    view.resets = [];
    d.filters.forEach(function (f) {
      var ci = d._ci[f.column];
      var box = el('div', 'f');
      var lab = el('label', '', f.label);
      box.appendChild(lab);
      if (f.type === 'date') {
        var lo = null, hi = null, kyi = d._ci.ky;
        d.dataset.rows.forEach(function (r) {
          if (kyi !== undefined && r[kyi] !== 'nay') return;
          var s = r[ci] === null || r[ci] === undefined ? '' : String(r[ci]);
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
            view.st[f.column] = [from.value, to.value];
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
    if (d.filters.length) {
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

  // ---------- khung trang, tab ----------
  document.title = data.title;
  document.getElementById('title').textContent = data.title;
  document.getElementById('meta').textContent =
    'Tạo lúc ' + data.generated + '. Số liệu chụp tại thời điểm tạo, không tự cập nhật. Link hết hạn lúc ' + data.expires + '.';

  var dashboards = data.dashboards || [];
  var tabsEl = document.getElementById('tabs');
  var sideEl = document.getElementById('side');
  var viewsEl = document.getElementById('views');
  var subSlot = document.getElementById('subSlot');
  var filterSlot = document.getElementById('filterSlot');
  var views = [], buttons = [], rendered = [];

  function show(i, focus) {
    views.forEach(function (v, k) { v.sec.hidden = k !== i; });
    // Bộ lọc và dòng phụ đề của tab đang xem được đưa lên đầu trang, cạnh tiêu đề
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
    d._ci = {};
    d.dataset.columns.forEach(function (c, k) { d._ci[c] = k; });
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
    var view = { d: d, st: {}, bounds: {}, charts: [], sec: sec, sub: sub, filterBar: filterBar, kpiBox: kpiBox, grid: grid, resets: [] };
    buildFilters(view);
    views.push(view);

    if (dashboards.length > 1) {
      var b = el('button', 'tab', d.name);
      b.type = 'button';
      b.id = 'tab-' + i;
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-controls', 'view-' + i);
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

  if (dashboards.length > 1) sideEl.hidden = false;
  var start = parseInt((location.hash || '').slice(1), 10) - 1;
  show(start >= 0 && start < dashboards.length ? start : 0, false);
})();
</script>
</body>
</html>
"""