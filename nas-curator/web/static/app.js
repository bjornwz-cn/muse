/* NAS Curator 前端共用 JS：fetch 封装、轮询、批量选择、置信度条渲染。 */
(function () {
  'use strict';

  /* ---------- 基础工具 ---------- */
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function fmtBytes(n) {
    if (n == null || isNaN(n)) return '—';
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var i = 0, v = Number(n);
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return (v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)) + ' ' + units[i];
  }

  function fmtTime(s) {
    if (!s) return '—';
    var d = new Date(s);
    if (isNaN(d.getTime())) return String(s);
    return d.toLocaleString('zh-CN', { hour12: false });
  }

  function fmtNum(n) {
    if (n == null || isNaN(n)) return '—';
    return Number(n).toLocaleString('zh-CN');
  }

  /* ---------- 任务进度（0.2.0） ---------- */
  function fmtDuration(sec) {
    if (sec == null || isNaN(sec) || sec < 0) return '—';
    sec = Math.round(sec);
    if (sec < 60) return sec + '秒';
    var m = Math.floor(sec / 60);
    if (m < 60) return m + '分钟';
    var h = Math.floor(m / 60);
    return h + '小时' + (m % 60 ? (m % 60) + '分' : '');
  }

  function fmtRate(n) {
    if (n == null || isNaN(n) || n <= 0) return '—';
    return (n >= 100 ? Math.round(n) : (Math.round(n * 10) / 10)) + ' 项/分钟';
  }

  /* 进度条 + 计数 + ETA：p 为后端 progress 摘要；总数未知时只显示
     已完成数与“—”，绝不编造百分比与 ETA。 */
  function progressBar(p) {
    if (!p) return '<span class="muted">—</span>';
    var total = p.total, done = p.done || 0;
    var pct = p.pct;
    var bar = (pct == null)
      ? '<span class="prog-bar"><span class="prog-fill unknown"></span></span>'
      : '<span class="prog-bar"><span class="prog-fill" style="width:' +
        Math.max(0, Math.min(100, pct)) + '%"></span></span>';
    var num = (total == null)
      ? ('已完成 ' + fmtNum(done))
      : (fmtNum(done) + ' / ' + fmtNum(total));
    var extra = [];
    if (p.pending != null && p.pending > 0) extra.push('待处理 ' + fmtNum(p.pending));
    if (p.skipped) extra.push('跳过 ' + fmtNum(p.skipped));
    if (p.failed) extra.push('问题 ' + fmtNum(p.failed));
    var eta = 'ETA ' + fmtDuration(p.eta_seconds);
    return '<span class="prog">' + bar + '<span class="prog-text">' + esc(num) +
      (pct == null ? '' : ' · ' + pct + '%') +
      (extra.length ? ' · ' + extra.join(' · ') : '') +
      ' · ' + eta + '</span></span>';
  }

  /* ---------- fetch 封装 ---------- */
  async function api(path, opts) {
    opts = opts || {};
    var res = await fetch(path, {
      method: opts.method || 'GET',
      headers: { 'Content-Type': 'application/json' },
      body: opts.body ? JSON.stringify(opts.body) : undefined
    });
    var data = null;
    try { data = await res.json(); } catch (e) { /* 非 JSON 响应 */ }
    if (!res.ok) {
      var msg = (data && (data.detail || data.message || data.error)) || ('请求失败：HTTP ' + res.status);
      var err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  /* ---------- 提示条 ---------- */
  var toastTimer = null;
  function toast(msg, isErr) {
    var el = document.getElementById('toast');
    if (!el) return;
    el.textContent = msg;
    el.classList.toggle('err', !!isErr);
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, 3500);
  }

  function showError(container, err) {
    if (!container) return;
    container.innerHTML = '<p class="error">加载失败：' + esc(err.message || err) + '</p>';
  }

  /* ---------- 置信度条 ---------- */
  function confBar(conf) {
    var c = Number(conf);
    if (isNaN(c)) return '<span class="muted">—</span>';
    var pct = Math.round(Math.max(0, Math.min(1, c)) * 100);
    var cls = c >= 0.85 ? '' : (c >= 0.6 ? 'mid' : 'low');
    return '<span class="conf"><span class="conf-bar"><span class="conf-fill ' + cls +
      '" style="width:' + pct + '%"></span></span><span class="conf-num">' +
      pct + '%</span></span>';
  }

  function kindTag(kind) {
    var labels = {
      low_conf: '低置信', dup: '重复', blur: '模糊',
      ambiguous: '待确认', sensitive: '敏感', shell: '空壳',
      unreadable: '不可读', unsupported: '不支持', no_text: '无正文'
    };
    return '<span class="tag kind-' + esc(kind || '') + '">' + esc(labels[kind] || kind || '—') + '</span>';
  }

  function statusTag(status) {
    var labels = {
      pending: '待处理', running: '进行中', done: '已完成', failed: '失败',
      approved: '已批准', rejected: '已拒绝', rolled_back: '已回滚',
      draft: '草稿', applying: '执行中', queued: '排队中', created: '已创建'
    };
    return '<span class="tag status-' + esc(status || '') + '">' + esc(labels[status] || status || '—') + '</span>';
  }

  /* ---------- 复核待处理数徽标 + 顶部版本号 ---------- */
  async function refreshReviewBadge() {
    var badge = document.getElementById('review-badge');
    if (!badge) return;
    try {
      var d = await api('/api/dashboard');
      var n = Number(d.review_pending) || 0;
      badge.textContent = n;
      badge.hidden = n <= 0;
      /* 0.1.5：顶部导航常驻版本号（与总览页同一来源 /api/dashboard 的 version） */
      var ver = document.getElementById('nav-version');
      if (ver && d.version) ver.textContent = '当前版本 ' + d.version;
    } catch (e) {
      badge.hidden = true; /* 后端未就绪时不显示错误徽标 */
    }
  }

  /* ---------- 顶部新版本提示 ---------- */
  async function refreshNavUpdate() {
    var link = document.getElementById('nav-update');
    if (!link) return;
    try {
      var d = await api('/api/upgrade/check');
      if (d.update_available && d.latest) {
        link.textContent = '有新版 ' + d.latest;
        link.hidden = false;
      } else {
        link.hidden = true; /* 无新版本不打扰 */
      }
    } catch (e) {
      link.hidden = true; /* 检查失败时不打扰 */
    }
  }

  /* ---------- 批量选择 ---------- */
  function bindSelectAll(selectAllId, itemClass) {
    var all = document.getElementById(selectAllId);
    if (!all) return;
    all.addEventListener('change', function () {
      document.querySelectorAll('.' + itemClass).forEach(function (cb) { cb.checked = all.checked; });
    });
  }

  function selectedValues(itemClass) {
    var out = [];
    document.querySelectorAll('.' + itemClass + ':checked').forEach(function (cb) { out.push(cb.value); });
    return out;
  }

  /* ---------- 轮询 ---------- */
  function poll(fn, opts) {
    opts = opts || {};
    var interval = opts.intervalMs || 2000;
    var maxTries = opts.maxTries || 300;
    var tries = 0;
    var timer = setInterval(async function () {
      tries++;
      try {
        var done = await fn();
        if (done || tries >= maxTries) clearInterval(timer);
      } catch (e) {
        if (tries >= maxTries) clearInterval(timer);
        else if (opts.onError) opts.onError(e);
      }
    }, interval);
    return function stop() { clearInterval(timer); };
  }

  /* ---------- 导航高亮 + 徽标 ---------- */
  document.addEventListener('DOMContentLoaded', function () {
    var path = window.location.pathname.replace(/\/$/, '') || '/';
    document.querySelectorAll('.nav a[data-nav]').forEach(function (a) {
      var href = a.getAttribute('href').replace(/\/$/, '') || '/';
      if (href === path) a.classList.add('active');
    });
    refreshReviewBadge();
    refreshNavUpdate();
  });

  /* 导出给各页内联脚本用 */
  window.Curator = {
    api: api, esc: esc, fmtBytes: fmtBytes, fmtTime: fmtTime, fmtNum: fmtNum,
    fmtDuration: fmtDuration, fmtRate: fmtRate, progressBar: progressBar,
    toast: toast, showError: showError, confBar: confBar, kindTag: kindTag,
    statusTag: statusTag, refreshReviewBadge: refreshReviewBadge,
    bindSelectAll: bindSelectAll, selectedValues: selectedValues, poll: poll
  };
})();
