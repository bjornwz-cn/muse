#!/usr/bin/env python3
"""VLM 全量批处理 阶段 2：高置信度（>=0.85）图片按 VLM 类目自动归档。

Phase 1 --analyze: 对 status='pending' 且 vlm_confidence >= 0.85 的
review 行，按 vlm_category 查 photo_dirs 算目标路径，逐项核对源文件，
写清单 TSV。不动文件、不写 plan、不改 DB。
Phase 2 --apply: 先经 sqlite backup API 备份 DB，再建 plan + plan_items，
逐项复核（size+mtime 与清单一致、目标不存在）后 shutil.move，写
operations，review 行标 approved（decided_by='vlm-r3-apply-ge85'），
plan 标 done。

安全规则（王震裁决 2026-11-23）：
- 只处理 vlm_confidence >= 0.85 且 vlm_model 非经济版的行；<0.85 一律不动。
- VLM >=0.85 可越过 path-prior 的「低置信不跨顶层」拦截，直接自动执行。
- 源文件不存在 -> 跳过并记录；目标已存在同名文件 -> 跳过（不覆盖）；
  源已在目标位置 -> 记 already，不移动。
- 移动前复核 size+mtime 与清单一致。
- 不碰 dhash 重复项、#107 剩余项、<0.85 的行、经济版结论。

与统一 CLI apply 的区别：CLI 的 apply_plan 遇目标同名会加 __n 后缀
改名移动；本脚本按任务口径遇同名直接跳过（不覆盖、不改名），
并把 review 行标 approved 留审计。plan_items 仍建档备查。

类别 -> 目标路径（源：media_core/config.json photo_dirs，容器内实测一致）：
  人物合影/儿童成长/自拍 -> 照片/家庭与人物/<类目>
  旅行风景/城市与建筑   -> 照片/旅行与城市/<类目>
  日常生活/美食与咖啡/宠物与动物/工作与活动 -> 照片/生活记录/<类目>
  屏幕截图/文档与票据/梗图与表情 -> 照片/资料与截图/<类目>
库根：/vol1/1000/媒体库

运行位置：NAS 容器内（需要 /data 与 /vol1/1000 挂载，root 身份）。
  docker exec nas-curator python3 /data/vlm_apply.py --analyze
  docker exec nas-curator python3 /data/vlm_apply.py --apply

断点续跑：analyze 每次重写清单（stat 很快）；apply 按行查 review 状态，
已 approved 的行跳过（already-approved），重跑不重复移动。
"""
import argparse, json, os, shutil, sqlite3, sys, time

DB = '/data/nas-curator.db'
MANIFEST = '/data/vlm_r3_apply.tsv'
SUMMARY = '/data/vlm_r3_apply_summary.json'
MAP = '/data/vlm_r3_apply_map.tsv'
LIB_ROOT = '/vol1/1000/媒体库'
OWNER_UID, OWNER_GID = 1000, 1000
PROGRESS_DIR = os.path.join(os.path.dirname(os.path.abspath(DB)), 'progress')

# 0.2.0 心跳：把进度写进 /data/progress/vlm-apply.json 供任务页显示；
# 写失败绝不影响主流程（best-effort）。
try:
    sys.path.insert(0, '/app')
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None


def _hb(**kw):
    if _write_hb is None:
        return
    try:
        _write_hb('vlm-r3-apply', progress_dir=PROGRESS_DIR, **kw)
    except Exception as e:  # noqa: BLE001 - 心跳失败只打印，不中断批处理
        print('heartbeat failed: %s' % e, flush=True)

# 类别 -> 媒体库内相对目录（media_core/config.json photo_dirs，2026-11-23
# 容器内实测与仓库一致）。VLM 阶段 1 只出这 12 个照片类目。
PHOTO_DIRS = {
    '人物合影': '照片/家庭与人物/人物合影',
    '儿童成长': '照片/家庭与人物/儿童成长',
    '自拍': '照片/家庭与人物/自拍',
    '旅行风景': '照片/旅行与城市/旅行风景',
    '城市与建筑': '照片/旅行与城市/城市与建筑',
    '日常生活': '照片/生活记录/日常生活',
    '美食与咖啡': '照片/生活记录/美食与咖啡',
    '宠物与动物': '照片/生活记录/宠物与动物',
    '工作与活动': '照片/生活记录/工作与活动',
    '屏幕截图': '照片/资料与截图/屏幕截图',
    '文档与票据': '照片/资料与截图/文档与票据',
    '梗图与表情': '照片/资料与截图/梗图与表情',
}
VIDEO_DIRS = {
    '儿童成长': '视频/儿童成长',
    '旅行风景': '视频/旅行风景',
    '日常生活': '视频/日常生活',
    '聚会与夜生活': '视频/聚会与夜生活',
    '运动与活动': '视频/运动与活动',
    '屏幕录制': '视频/屏幕录制',
}
VIDEO_EXTS = {'.mp4', '.wmv', '.mov', '.avi', '.flv', '.rmvb', '.mpg',
              '.m4v', '.mkv', '.webm', '.vob'}


def _dir_map(src):
    """按源文件扩展名选目录映射：视频走 VIDEO_DIRS，其余走 PHOTO_DIRS。"""
    ext = os.path.splitext(src or '')[1].lower()
    return VIDEO_DIRS if ext in VIDEO_EXTS else PHOTO_DIRS

ECON_MARKERS = ('economy', 'econ')

COLS = ['rq_id', 'file_id', 'plan_id', 'kind', 'src', 'dst',
        'vlm_category', 'vlm_confidence', 'vlm_model',
        'size_src', 'mtime_src', 'action', 'detail']


def _s(x):
    return '' if x is None else str(x)


def is_econ(model):
    """经济版结论（或模型未知）不参与自动执行。"""
    if not model:
        return True
    m = str(model).lower()
    return any(k in m for k in ECON_MARKERS)


KIND = "low_conf"


def load_candidates(db, kind=KIND):
    """pending + kind + 有 vlm 结论 + 置信度 >= 0.85 的行（按 id 升序）。
    经济版在这里不过滤，留给 analyze 记 skip-econ（可见、可审计）。
    第三轮：不再按原始置信度区间过滤。"""
    return db.execute(
        "SELECT id, file_id, plan_id, kind, payload FROM review_queue "
        "WHERE status='pending' AND kind=? "
        "AND json_extract(payload,'$.vlm_category') IS NOT NULL "
        "AND json_extract(payload,'$.vlm_confidence') >= 0.85 "
        "ORDER BY id", (kind,)).fetchall()


def analyze(limit, kind=KIND):
    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    rows = load_candidates(db, kind)
    print('pending %s vlm>=0.85 rows: %d' % (kind, len(rows)), flush=True)
    _hb(label='VLM 第三轮高置信自动归档', kind='vlm-apply', phase='analyze',
        status='running', total=len(rows), done=0)
    counts = {}
    by_cat = {}
    seen_dst = {}  # dst -> rq_id（本轮已规划的目标，防同名冲突）
    t0 = time.time()
    n = 0
    with open(MANIFEST, 'w', encoding='utf-8') as out:
        out.write('\t'.join(COLS) + '\n')
        for rq_id, file_id, plan_id, kind, payload in rows:
            if limit and n >= limit:
                break
            n += 1
            try:
                d = json.loads(payload)
            except Exception:
                d = {}
            src = d.get('src') or ''
            cat = d.get('vlm_category') or ''
            conf = d.get('vlm_confidence')
            model = d.get('vlm_model') or ''
            dst = ''
            size_src, mtime_src = '', ''
            action, detail = '', ''
            if is_econ(model):
                action, detail = 'skip-econ', 'vlm_model=%s' % (model or 'missing')
            elif cat not in _dir_map(src):
                action, detail = 'skip-unknown-category', 'cat=%s' % cat
            elif not src:
                action, detail = 'skip-src-gone', 'no src in payload'
            else:
                dst = os.path.join(LIB_ROOT, _dir_map(src)[cat],
                                   os.path.basename(src))
                try:
                    st = os.stat(src)
                    size_src, mtime_src = st.st_size, st.st_mtime
                except FileNotFoundError:
                    action, detail = 'skip-src-gone', 'not-on-disk'
                else:
                    if not os.path.isfile(src):
                        action, detail = 'skip-src-gone', 'not-a-file'
                    elif os.path.abspath(src) == os.path.abspath(dst):
                        action, detail = 'already', 'src==dst'
                    elif dst in seen_dst:
                        action, detail = ('skip-dst-exists',
                                          'dst-claimed-by-rq-%s' % seen_dst[dst])
                    elif os.path.exists(dst):
                        action, detail = 'skip-dst-exists', 'dst-on-disk'
                    else:
                        seen_dst[dst] = rq_id
                        action, detail = 'move', ''
            out.write('\t'.join([_s(rq_id), _s(file_id), _s(plan_id), _s(kind),
                                 _s(src), _s(dst), _s(cat), _s(conf), _s(model),
                                 _s(size_src), _s(mtime_src),
                                 _s(action), _s(detail)]) + '\n')
            counts[action] = counts.get(action, 0) + 1
            if action == 'move':
                by_cat[cat] = by_cat.get(cat, 0) + 1
            if n % 500 == 0:
                print('%d/%d done (%.1fs) counts=%s'
                      % (n, len(rows), time.time() - t0, counts), flush=True)
                _hb(label='VLM 第三轮高置信自动归档', kind='vlm-apply',
                    phase='analyze', status='running', total=len(rows), done=n)
    print('ANALYZE DONE n=%d elapsed=%.1fs counts=%s'
          % (n, time.time() - t0, counts), flush=True)
    _hb(label='VLM 第三轮高置信自动归档', kind='vlm-apply', phase='analyze',
        status='done', total=len(rows), done=n)
    print('move by category: %s' % json.dumps(by_cat, ensure_ascii=False),
          flush=True)
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump({'phase': 'analyze', 'n': n,
                   'elapsed': round(time.time() - t0, 1),
                   'counts': counts, 'move_by_category': by_cat},
                  f, ensure_ascii=False, indent=2)


def _mark_approved(db, rq_id):
    cur = db.execute(
        "UPDATE review_queue SET status='approved', "
        "decided_by='vlm-r3-apply-ge85', "
        "decided_at=strftime('%Y-%m-%dT%H:%M:%S','now') "
        "WHERE id=? AND status='pending'", (rq_id,))
    return cur.rowcount


def apply(limit, kind=KIND):
    if not os.path.exists(MANIFEST):
        print('no manifest %s: run --analyze first' % MANIFEST)
        return 2
    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    # DB 备份先行（sqlite backup API，库在线也安全）。
    stamp = time.strftime('%Y%m%d-%H%M%S')
    bak = '/data/nas-curator.db.bak-%s-pre-vlm-r3-apply' % stamp
    with sqlite3.connect(bak) as dst:
        db.backup(dst)
    print('db backup:', bak, flush=True)
    # 建 plan（draft -> applying -> done）。
    cur = db.execute(
        "INSERT INTO plans (library, summary) VALUES ('media', ?)",
        (json.dumps({'kind': 'vlm-r3-apply-ge85', 'manifest': MANIFEST,
                     'backup': bak}, ensure_ascii=False),))
    plan_id = cur.lastrowid
    db.execute("UPDATE plans SET status='applying' WHERE id=?", (plan_id,))
    db.commit()
    print('plan #%d created' % plan_id, flush=True)
    rows = []
    with open(MANIFEST, encoding='utf-8') as f:
        hdr = f.readline().rstrip('\n').split('\t')
        for line in f:
            parts = line.rstrip('\n').split('\t')
            if len(parts) < len(COLS):
                parts += [''] * (len(COLS) - len(parts))
            rows.append(dict(zip(COLS, parts)))
    _hb(label='VLM 第三轮高置信自动归档', kind='vlm-apply', phase='apply',
        status='running', total=len(rows), done=0)
    counts = {}
    by_cat = {}
    t0 = time.time()
    n = 0
    with open(MAP, 'a', encoding='utf-8') as mapf:
        for r in rows:
            if limit and n >= limit:
                break
            n += 1
            rq_id = int(r['rq_id'])
            action = r['action']
            # 断点续跑：该行已处理过（上一轮 apply 已 approved）则跳过。
            st = db.execute("SELECT status FROM review_queue WHERE id=?",
                            (rq_id,)).fetchone()
            if not st:
                counts['skip-row-gone'] = counts.get('skip-row-gone', 0) + 1
                continue
            if st[0] != 'pending':
                counts['already-approved'] = counts.get('already-approved', 0) + 1
                continue
            # plan_items 建档（move/already/skip 全记，status 按结局写）。
            item = {
                'plan_id': plan_id,
                'file_id': int(r['file_id']) if r['file_id'] else None,
                'src_path': r['src'], 'dst_path': r['dst'] or None,
                'category': r['vlm_category'],
                'confidence': float(r['vlm_confidence']) if r['vlm_confidence'] else None,
                'reason': 'vlm-apply-ge85:%s' % (r['detail'] or r['action']),
                'evidence': r['vlm_model'],
                'action': 'move', 'kind': 'media',
                'size': int(r['size_src']) if r['size_src'] else None,
                'ext': os.path.splitext(r['src'])[1].lower(),
                'review_kind': kind, 'status': 'pending',
            }
            cur = db.execute(
                """INSERT INTO plan_items
                   (plan_id, file_id, src_path, dst_path, category, confidence,
                    reason, evidence, action, kind, size, ext, review_kind, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item['plan_id'], item['file_id'], item['src_path'],
                 item['dst_path'], item['category'], item['confidence'],
                 item['reason'], item['evidence'], item['action'], item['kind'],
                 item['size'], item['ext'], item['review_kind'], item['status']))
            item_id = cur.lastrowid

            def set_item(status):
                db.execute("UPDATE plan_items SET status=? WHERE id=?",
                           (status, item_id))

            if action == 'already':
                # 源已在目标位置：不移动，review 行标 approved。
                set_item('done')
                if _mark_approved(db, rq_id):
                    counts['already'] = counts.get('already', 0) + 1
                    counts['review-rows-approved'] = \
                        counts.get('review-rows-approved', 0) + 1
                db.commit()
                continue
            if action != 'move':
                # skip-*：不动文件、不标 approved，留待人工复核。
                set_item('skipped')
                counts['skip-' + action] = counts.get('skip-' + action, 0) + 1
                db.commit()
                continue
            # move：复核 size+mtime、源仍在、目标仍不存在，再移动。
            src, dst = r['src'], r['dst']
            try:
                st2 = os.stat(src)
                if r['size_src'] and int(r['size_src']) != st2.st_size:
                    set_item('failed')
                    counts['skip-size-changed'] = counts.get('skip-size-changed', 0) + 1
                    db.commit()
                    continue
                if r['mtime_src'] and abs(float(r['mtime_src']) - st2.st_mtime) > 1:
                    set_item('failed')
                    counts['skip-mtime-changed'] = counts.get('skip-mtime-changed', 0) + 1
                    db.commit()
                    continue
            except FileNotFoundError:
                set_item('skipped')
                counts['skip-src-gone'] = counts.get('skip-src-gone', 0) + 1
                db.commit()
                continue
            if os.path.exists(dst):
                set_item('skipped')
                counts['skip-dst-exists'] = counts.get('skip-dst-exists', 0) + 1
                db.commit()
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                try:
                    os.chown(dst, OWNER_UID, OWNER_GID)
                except OSError:
                    pass
            except Exception as e:
                set_item('failed')
                counts['failed-move'] = counts.get('failed-move', 0) + 1
                print('FAILED rq=%d %s -> %s : %s: %s'
                      % (rq_id, src, dst, type(e).__name__, str(e)[:120]),
                      flush=True)
                db.commit()
                continue
            db.execute(
                "INSERT INTO operations (plan_id, src_path, dst_path) "
                "VALUES (?,?,?)", (plan_id, src, dst))
            mapf.write('\t'.join([src, dst,
                                  time.strftime('%Y-%m-%dT%H:%M:%S')]) + '\n')
            mapf.flush()
            set_item('done')
            if _mark_approved(db, rq_id):
                counts['file-moved'] = counts.get('file-moved', 0) + 1
                counts['review-rows-approved'] = \
                    counts.get('review-rows-approved', 0) + 1
                by_cat[r['vlm_category']] = by_cat.get(r['vlm_category'], 0) + 1
            db.commit()
            if n % 100 == 0:
                print('%d/%d applied (%.1fs) counts=%s'
                      % (n, len(rows), time.time() - t0, counts), flush=True)
                _hb(label='VLM 第三轮高置信自动归档', kind='vlm-apply',
                    phase='apply', status='running', total=len(rows), done=n,
                    failed=counts.get('failed-move', 0))
    db.execute(
        "UPDATE plans SET status='done', summary=? WHERE id=?",
        (json.dumps({'kind': 'vlm-r3-apply-ge85', 'manifest': MANIFEST,
                     'backup': bak, 'counts': counts,
                     'moved_by_category': by_cat},
                    ensure_ascii=False), plan_id))
    db.commit()
    print('integrity:', db.execute('PRAGMA integrity_check').fetchone()[0],
          flush=True)
    print('pending now:', db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE status='pending'").fetchone()[0],
        flush=True)
    print('plan #%d items:' % plan_id, db.execute(
        "SELECT status, COUNT(*) FROM plan_items WHERE plan_id=? GROUP BY status",
        (plan_id,)).fetchall(), flush=True)
    print('APPLY DONE n=%d elapsed=%.1fs counts=%s'
          % (n, time.time() - t0, counts), flush=True)
    print('moved by category: %s' % json.dumps(by_cat, ensure_ascii=False),
          flush=True)
    _hb(label='VLM 第三轮高置信自动归档', kind='vlm-apply', phase='apply',
        status='done', total=len(rows), done=n,
        failed=counts.get('failed-move', 0))
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump({'phase': 'apply', 'plan_id': plan_id, 'backup': bak,
                   'n': n, 'elapsed': round(time.time() - t0, 1),
                   'counts': counts, 'moved_by_category': by_cat},
                  f, ensure_ascii=False, indent=2)
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--analyze', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--kind', default='low_conf',
                        help='review kind to apply (default low_conf)')
    args = ap.parse_args()
    # 按 kind 分清单/汇总/映射文件，避免 low_conf 与 blur 互相覆盖。
    global MANIFEST, SUMMARY, MAP, KIND
    KIND = args.kind
    MANIFEST = '/data/vlm_r3_apply_%s.tsv' % args.kind
    SUMMARY = '/data/vlm_r3_apply_%s_summary.json' % args.kind
    MAP = '/data/vlm_r3_apply_%s_map.tsv' % args.kind
    if args.analyze:
        analyze(args.limit, args.kind)
    elif args.apply:
        raise SystemExit(apply(args.limit, args.kind))
    else:
        ap.print_help()
        raise SystemExit(2)
