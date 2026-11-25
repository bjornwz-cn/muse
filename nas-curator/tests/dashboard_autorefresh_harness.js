'use strict';
/* 复现/验证总览页任务卡片自动刷新：
   node dashboard_autorefresh_harness.js <index.html 路径>
   桩 window/document/setInterval/Curator.api：首轮 /api/dashboard 返回旧数，
   之后返回新数（模拟后台处理完一批文件）；随后手动触发捕获到的 interval 回调，
   输出触发前后卡片内容，判断是否自动更新。只读模板文件，不碰网络与 NAS。 */
const fs = require('fs');
const path = process.argv[2];
if (!path) { console.error('用法：node dashboard_autorefresh_harness.js <index.html>'); process.exit(2); }
const html = fs.readFileSync(path, 'utf-8');
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.error('未找到内联 <script>'); process.exit(2); }
const code = m[1];

function makeEl(id) {
  return {
    id, innerHTML: '', textContent: '', hidden: false,
    style: {}, classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, removeEventListener() {},
    setAttribute() {}, getAttribute() { return null; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    scrollIntoView() {},
  };
}
const els = {};
const documentStub = {
  getElementById(id) { if (!els[id]) els[id] = makeEl(id); return els[id]; },
  querySelectorAll() { return []; },
  querySelector() { return null; },
  addEventListener() {},
};
const intervals = [];
let dashCalls = 0;
const payloadA = {
  version: '0.2.1',
  libraries: [{ id: 'inbox', name: '统一收件箱', root: '/vol1/1000/收件箱', files: 3869, bytes: 23000000000, review_pending: 275 }],
  review_pending: 275, today_new: 10,
  jobs_recent: [{ id: 199, kind: 'plan', status: 'running', started_at: '2026-11-25T12:00:00', finished_at: null, progress: { done: 5, total: 10, pct: 50, pending: 5, eta_seconds: 60 } }],
  dup_reclaimable: { count: 3, bytes: 1024 },
};
const payloadB = {
  version: '0.2.1',
  libraries: [{ id: 'inbox', name: '统一收件箱', root: '/vol1/1000/收件箱', files: 57, bytes: 1300000000, review_pending: 65 }],
  review_pending: 65, today_new: 10,
  jobs_recent: [{ id: 199, kind: 'plan', status: 'done', started_at: '2026-11-25T12:00:00', finished_at: '2026-11-25T12:05:00', progress: { done: 10, total: 10, pct: 100, pending: 0, eta_seconds: 0 } }],
  dup_reclaimable: { count: 3, bytes: 1024 },
};
const CuratorStub = {
  api: async (p) => {
    if (p === '/api/dashboard') { dashCalls++; return dashCalls === 1 ? payloadA : payloadB; }
    if (p === '/api/jobs/running') return { jobs: [], scripts: [] };
    if (p === '/api/upgrade/check') return { update_available: false, latest: '0.2.1' };
    if (p === '/api/upgrade/status') return { state: 'idle' };
    throw new Error('未预期的 API 调用：' + p);
  },
  esc: (s) => String(s == null ? '' : s),
  fmtNum: (n) => (n == null ? '—' : String(n)),
  fmtBytes: (n) => (n == null ? '—' : String(n) + ' B'),
  fmtTime: (s) => String(s || '—'),
  fmtDuration: (s) => (s == null ? '—' : String(s) + '秒'),
  fmtRate: (n) => (n == null ? '—' : String(n)),
  progressBar: () => '<span>progress</span>',
  statusTag: (s) => '<span>' + s + '</span>',
  confBar: () => '',
  kindTag: (k) => '<span>' + k + '</span>',
  toast: () => {},
  showError: (el, e) => { if (el) el.innerHTML = 'ERR:' + (e && e.message); },
  refreshReviewBadge: () => {},
  bindSelectAll: () => {},
  selectedValues: () => [],
  poll: async (fn) => { await fn(); return () => {}; },
};
const sandbox = {
  document: documentStub,
  window: { location: { pathname: '/', reload() {} }, confirm: () => true, Curator: CuratorStub },
  setInterval: (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; },
  setTimeout: () => 0,
  clearInterval: () => {}, clearTimeout: () => {},
  console,
};
const vm = require('vm');
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: 'index-inline.js' });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  await sleep(100); // 等首轮 load()/loadRunning() 的 await 落定
  const snap = () => ({
    statReview: els['stat-review'] ? els['stat-review'].textContent : null,
    libCards: els['library-cards'] ? els['library-cards'].innerHTML : null,
    jobsBody: els['jobs-body'] ? els['jobs-body'].innerHTML : null,
    badge: els['review-badge'] ? String(els['review-badge'].textContent) : null,
    dashCalls,
  });
  const before = snap();
  for (const it of intervals) { it.fn(); }
  await sleep(100);
  const after = snap();
  // 再触发一次：后端数据未变（仍返回 payloadB），签名相同应跳过 DOM 重写。
  for (const it of intervals) { it.fn(); }
  await sleep(100);
  const after2 = snap();
  console.log(JSON.stringify({ intervals: intervals.map((i) => i.ms), before, after, after2 }, null, 2));
})().catch((e) => { console.error(e); process.exit(1); });
