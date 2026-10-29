# app/agent/tools/dashboard_template.py
# Template HTML cố định của dashboard. Dữ liệu được nhúng dưới dạng JSON vào chỗ
# __PAYLOAD__ và mọi chữ đều được đưa vào trang bằng textContent, nên nội dung lấy từ
# database (tên món, ghi chú khách...) không thể chèn mã vào trang.
# Chart.js được nginx (dashboards-web) phục vụ cùng máy chủ tại /chart.umd.min.js, không
# nạp từ CDN; không tải được hoặc vẽ lỗi thì biểu đồ rơi về dạng bảng số liệu.
# Giao diện: nền sáng/tối theo máy người xem, 1 cột trên điện thoại, 2 cột trên màn hình rộng.

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
    --c1: #1F6F6B; --c2: #9A5B35; --c3: #D6A23A; --c4: #5C6F8A; --c5: #B24D69; --c6: #7E8F3F;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #101614; --panel: #18211E; --ink: #E7ECE8; --muted: #9AA8A0; --line: #2A3631;
      --c1: #4FB3AC; --c2: #D08A5C; --c3: #E4B95A; --c4: #8FA6C6; --c5: #D77A93; --c6: #A5B76A;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  }
  main { max-width: 1100px; margin: 0 auto; padding: 20px 16px 48px; }
  header { margin-bottom: 20px; }
  h1 { margin: 0 0 4px; font-size: clamp(1.35rem, 4vw, 1.9rem); font-weight: 650; letter-spacing: -0.01em; line-height: 1.25; }
  .meta { margin: 0; color: var(--muted); font-size: .875rem; }
  .grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(min(100%, 460px), 1fr)); }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px; min-width: 0; }
  .panel.wide { grid-column: 1 / -1; }
  h2 { margin: 0 0 12px; font-size: 1rem; font-weight: 600; line-height: 1.3; }
  .chart { position: relative; height: 300px; }
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
  <header>
    <h1 id="title"></h1>
    <p class="meta" id="meta"></p>
  </header>
  <div class="grid" id="grid"></div>
</main>

<script type="application/json" id="payload">__PAYLOAD__</script>
<script src="chart.umd.min.js"></script>
<script>
(function () {
  var data = JSON.parse(document.getElementById('payload').textContent);
  var css = getComputedStyle(document.documentElement);
  var v = function (name) { return css.getPropertyValue(name).trim(); };
  var palette = ['--c1', '--c2', '--c3', '--c4', '--c5', '--c6'].map(v);
  var nf = new Intl.NumberFormat('vi-VN', { maximumFractionDigits: 2 });
  var nfc = new Intl.NumberFormat('vi-VN', { notation: 'compact', maximumFractionDigits: 1 });

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function cell(value) {
    return typeof value === 'number' ? nf.format(value) : (value === null || value === undefined ? '' : String(value));
  }

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
      r.forEach(function (val) { tr.appendChild(el('td', typeof val === 'number' ? 'num' : '', cell(val))); });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody); wrap.appendChild(table);
    return wrap;
  }

  function chartAsTable(c) {
    var columns = [c.x_name || 'Nhãn'].concat(c.series.map(function (s) { return s.name; }));
    var rows = c.labels.map(function (label, i) {
      return [label].concat(c.series.map(function (s) { return s.values[i]; }));
    });
    return buildTable(columns, rows);
  }

  function buildChart(c) {
    var isPie = c.type === 'pie';
    var wrap = el('div', 'chart');
    var canvas = el('canvas');
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', c.title);
    wrap.appendChild(canvas);
    var ink = v('--ink'), muted = v('--muted'), line = v('--line'), panel = v('--panel');
    var datasets = c.series.map(function (s, i) {
      var color = palette[i % palette.length];
      if (isPie) {
        return { label: s.name, data: s.values,
          backgroundColor: c.labels.map(function (_, k) { return palette[k % palette.length]; }),
          borderColor: panel, borderWidth: 2 };
      }
      if (c.type === 'line') {
        return { label: s.name, data: s.values, borderColor: color, backgroundColor: color,
          borderWidth: 2, tension: 0.2, pointRadius: 3 };
      }
      return { label: s.name, data: s.values, backgroundColor: color, borderRadius: 3 };
    });
    new Chart(canvas, {
      type: c.type,
      data: { labels: c.labels, datasets: datasets },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        plugins: {
          legend: { display: isPie || c.series.length > 1, position: 'bottom', labels: { color: ink, boxWidth: 12 } },
          tooltip: { callbacks: { label: function (ctx) {
            var name = isPie ? ctx.label : ctx.dataset.label;
            var value = (ctx.parsed && ctx.parsed.y !== undefined) ? ctx.parsed.y : ctx.parsed;
            return name + ': ' + nf.format(value);
          } } }
        },
        scales: isPie ? {} : {
          x: { ticks: { color: muted, maxRotation: 60, autoSkip: true }, grid: { display: false } },
          y: { beginAtZero: true, ticks: { color: muted, callback: function (val) { return nfc.format(val); } }, grid: { color: line } }
        }
      }
    });
    return wrap;
  }

  document.title = data.title;
  document.getElementById('title').textContent = data.title;
  document.getElementById('meta').textContent =
    'Tạo lúc ' + data.generated + '. Số liệu chụp tại thời điểm tạo, không tự cập nhật. Link hết hạn lúc ' + data.expires + '.';

  var grid = document.getElementById('grid');
  data.charts.forEach(function (c) {
    var panel = el('section', c.type === 'table' ? 'panel wide' : 'panel');
    panel.appendChild(el('h2', '', c.title));
    var hasData = c.type === 'table' ? c.rows.length > 0 : c.labels.length > 0;
    if (!hasData) {
      panel.appendChild(el('p', 'empty', 'Không có dữ liệu cho mục này.'));
    } else if (c.type === 'table') {
      panel.appendChild(buildTable(c.columns, c.rows));
    } else if (typeof Chart === 'undefined') {
      panel.appendChild(chartAsTable(c));
    } else {
      try {
        panel.appendChild(buildChart(c));
      } catch (e) {
        panel.appendChild(chartAsTable(c));
      }
    }
    grid.appendChild(panel);
  });
})();
</script>
</body>
</html>
"""