#!/usr/bin/env python3
"""Year-folder moveback: move categorized files from the inbox back into
the libraries by final category.

Midconf moveback VIDEOS step 2b (2026-11-25): scope is the 119-video
snapshot (/data/midconf_moveback_videos_snapshot.tsv) AND final
confidence >= 0.85. These are the 119 videos whose final category is a
photo category (文档与票据 39 / 自拍 21 / 城市与建筑 16 / 人物合影 14 /
工作与活动 12 / 宠物与动物 7 / 美食与咖啡 4 / 梗图与表情 3 / 屏幕截图 3);
per parent decision (a) the 9 matching directories are created under
媒体库/视频/ first, then the videos move back into them.
Source breakdown to verify: VLM 119 / SigLIP 0 / doc 0.

Final category (same rule as year_summary_batch5.py, plus economy
exclusion): vlm_category present, vlm_confidence >= 0.72 and the VLM
model is NOT the economy model -> VLM category; else kind=文档 and
doc_party is a real party (doc model not economy) -> doc_party;
else payload category if in REAL_CATS (SigLIP real category);
else unclassified. final_confidence is the confidence of the source
that supplied the final category. Economy conclusions only go to review.

Destinations:
- photo categories -> /vol1/1000/媒体库/照片/<mapped dir>
- video categories -> /vol1/1000/媒体库/视频/<mapped dir>
- party categories (doc files) -> /vol1/1000/工作文件/<party>/<subdir>
  (subdir from payload doc_subdir, validated per party, fallback
  '14-工具与其他'; per 王震 2026-11-24 08:25 party rule -- documents
  are filed by owning party under 工作文件/, not into 媒体库/).

Phase 1 --analyze: build manifest TSV, verify total 119 and the
source breakdown. No files moved, no DB writes. For dst-exists rows
analyze decides rename vs delete-src by size + sha256 only; the
retained-copy decode check and the versioned-name computation are
deferred to --apply (which re-verifies them).
Phase 2 --apply: sqlite backup first, create plan + plan_items,
verify (size+mtime, sha re-check for delete-src, retained-copy decode
check, rename target free) then move/rename/delete-src, write
operations, mark review rows approved (decided_by='year-moveback-midconf-videos*').
Resume-safe: already-approved rows are skipped on re-run.

Same-name handling (rules set 2026-11-20/2026-11-23):
- dst exists, sizes differ OR same size but content differs -> rename with
  version {stem}_{YYYYMMDD-HHMMSS}_{size}_{seq}{ext} (EXIF time first,
  else mtime; then size; then seq).
- dst exists, content identical -> delete source ONLY after the retained
  copy (dst) passes a decode check (PIL -> cv2, video frame, .livp zip).
- dst exists but retained copy fails decode -> skip-retained-bad.
- never overwrite any existing file.

Safety:
- Only review_queue rows with src in the batch-5 snapshot and status
  pending; mid-confidence and unclassified rows never move; files
  without a review row never move.
- Non-media extensions for photo/video categories -> skip-nonmedia;
  doc files use the party route only.

Runs inside the nas-curator container (needs /data and /vol1/1000).
  docker exec nas-curator python3 /data/year_moveback_midconf_videos.py --analyze
  docker exec -d nas-curator python3 /data/year_moveback_midconf_videos.py --apply
"""
import argparse, hashlib, json, os, shutil, sqlite3, sys, time
from datetime import datetime

DB = '/data/nas-curator.db'
MANIFEST = '/data/year_moveback_midconf_videos.tsv'
SUMMARY = '/data/year_moveback_midconf_videos_summary.json'
MAP = '/data/year_moveback_midconf_videos_map.tsv'
LIB_ROOT = '/vol1/1000/媒体库'
WORK_ROOT = '/vol1/1000/工作文件'
OWNER_UID, OWNER_GID = 1000, 1000
PROGRESS_DIR = os.path.join(os.path.dirname(os.path.abspath(DB)), 'progress')
HB_ID = 'year-moveback-midconf-videos'

try:
    sys.path.insert(0, '/app')
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None

try:
    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()
    import cv2
    _HAVE_IMG = True
except Exception:
    _HAVE_IMG = False

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
    # 步骤 2b (2026-11-25, 方案 a): 9 个照片类目在视频库新建同名目录后移回,
    # 保留 VLM 高置信分类, 不重映射到现有 6 个视频类目.
    '文档与票据': '视频/文档与票据',
    '自拍': '视频/自拍',
    '城市与建筑': '视频/城市与建筑',
    '人物合影': '视频/人物合影',
    '工作与活动': '视频/工作与活动',
    '宠物与动物': '视频/宠物与动物',
    '美食与咖啡': '视频/美食与咖啡',
    '梗图与表情': '视频/梗图与表情',
    '屏幕截图': '视频/屏幕截图',
}
REAL_CATS = set(PHOTO_DIRS) | set(VIDEO_DIRS)
PARTIES = {'1-王震', '2-上海润心', '3-北京供应链', '4-辽宁器械'}
# Valid doc subdirs per party (same as year_doc_batch5.py SUBDIRS).
DOC_SUBDIRS = {
    '1-王震': {'00-整理说明', '09-会议与汇报', '10-周报与工作总结',
               '12-项目资料', '14-工具与其他'},
    '2-上海润心': {'01-公司资质与证照', '02-产品与注册', '03-合同与协议',
                   '04-供应商与采购', '05-销售与业务数据', '06-库存与物流',
                   '07-财务与经营分析', '08-运营与项目', '09-会议与汇报',
                   '10-周报与工作总结', '11-人事与行政', '12-招投标',
                   '12-项目资料', '13-图片与影像', '14-工具与其他'},
    '3-北京供应链': {'01-公司资质与证照', '02-产品与注册', '03-合同与协议',
                     '04-供应商与采购', '05-销售与业务数据', '06-库存与物流',
                     '07-财务与经营分析', '08-运营与项目', '09-会议与汇报',
                     '10-周报与工作总结', '11-人事与行政', '12-招投标',
                     '13-图片与影像', '14-工具与其他'},
    '4-辽宁器械': {'01-公司资质与证照', '02-产品与注册', '03-合同与协议',
                   '04-供应商与采购', '05-销售与业务数据', '06-库存与物流',
                   '07-财务与经营分析', '08-运营与项目', '09-会议与汇报',
                   '10-周报与工作总结', '11-人事与行政', '12-招投标',
                   '13-图片与影像', '14-工具与其他', '15-临床与学术资料'},
}
DOC_FALLBACK_SUBDIR = '14-工具与其他'
IMG_EXTS = {'.jpg', '.jpeg', '.heic', '.png', '.heif', '.webp', '.gif', '.nef'}
VID_EXTS = {'.mov', '.mp4', '.mts', '.vob', '.avi', '.wmv'}
ALL_MEDIA = IMG_EXTS | VID_EXTS

COLS = ['rq_id', 'file_id', 'plan_id', 'kind', 'src', 'dst',
        'final_category', 'final_source', 'vlm_confidence',
        'final_confidence',
        'size_src', 'mtime_src', 'size_dst', 'sha_src', 'sha_dst',
        'action', 'detail', 'new_name']

# Expected batch-5 >= 0.85 totals (step-C recalculation, verified).
EXPECTED_TOTAL = 119
EXPECTED_SOURCES = {'vlm': 119}


def _s(x):
    return '' if x is None else str(x)


def _hb(**kw):
    if _write_hb is None:
        return
    try:
        _write_hb(HB_ID, progress_dir=PROGRESS_DIR, **kw)
    except Exception as e:  # noqa: BLE001
        print('heartbeat failed: %s' % e, flush=True)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def head_tail_match(a, b, size):
    """Fast pre-filter for large same-size files: compare SHA-256 of the
    first and last 1MB. A mismatch proves different content (rename);
    a match only makes the pair a delete-src CANDIDATE -- apply still
    re-verifies with a full sha256 before any delete."""
    if size <= 2 << 20:
        return sha256(a) == sha256(b), 'full-sha'
    with open(a, 'rb') as fa, open(b, 'rb') as fb:
        head_a = fa.read(1 << 20)
        head_b = fb.read(1 << 20)
        if hashlib.sha256(head_a).digest() != hashlib.sha256(head_b).digest():
            return False, 'head-diff'
        fa.seek(size - (1 << 20))
        fb.seek(size - (1 << 20))
        tail_a = fa.read(1 << 20)
        tail_b = fb.read(1 << 20)
        if hashlib.sha256(tail_a).digest() != hashlib.sha256(tail_b).digest():
            return False, 'tail-diff'
    return True, 'head-tail-match'


def decodes(path):
    """(ok, detail) retained-copy usable check. ok None = no checker."""
    ext = os.path.splitext(path)[1].lower()
    if not _HAVE_IMG:
        return None, 'no-img-libs'
    try:
        if ext in IMG_EXTS:
            try:
                with Image.open(path) as im:
                    im.load()
                return True, 'pil-ok'
            except Exception as e:
                pil_err = '%s:%s' % (type(e).__name__, str(e)[:60])
                arr = cv2.imread(path, cv2.IMREAD_UNCHANGED)
                if arr is not None:
                    return True, 'cv2-ok'
                return False, 'pil-fail-cv2-fail:' + pil_err
        if ext in VID_EXTS:
            cap = cv2.VideoCapture(path)
            ok = cap.isOpened()
            if ok:
                ok, frame = cap.read()
                ok = ok and frame is not None
            cap.release()
            return bool(ok), 'cv2-ok' if ok else 'cv2-noframe'
        if ext == '.livp':
            import zipfile
            z = zipfile.is_zipfile(path)
            return z, 'zip-ok' if z else 'not-zip'
        return None, 'unhandled-ext:' + ext
    except Exception as e:
        return False, '%s:%s' % (type(e).__name__, str(e)[:80])


def creation_dt(path):
    """EXIF shooting time first, else file mtime."""
    if _HAVE_IMG:
        try:
            with Image.open(path) as im:
                exif = im._getexif()
            if exif:
                for tag in (36867, 36868, 306):
                    v = exif.get(tag)
                    if v:
                        try:
                            return datetime.strptime(str(v).strip(),
                                                     '%Y:%m:%d %H:%M:%S')
                        except ValueError:
                            continue
        except Exception:
            pass
    return datetime.fromtimestamp(os.stat(path).st_mtime)


def versioned_name(dst_dir, src, claimed):
    stem, ext = os.path.splitext(os.path.basename(src))
    dt = creation_dt(src).strftime('%Y%m%d-%H%M%S')
    size = os.stat(src).st_size
    seq = 1
    while True:
        name = '%s_%s_%d_%d%s' % (stem, dt, size, seq, ext)
        cand = os.path.join(dst_dir, name)
        if cand not in claimed and not os.path.exists(cand):
            claimed.add(cand)
            return name, cand
        seq += 1


def final_category(d, snap_kind):
    """(final, source, final_conf) or (None, None, 0.0).
    Same rule as year_summary_batch5.py, plus economy exclusion:
    VLM >= 0.72 (standard model only) wins; else kind=文档 and doc_party
    is a real party (doc model not economy) -> doc_party; else SigLIP
    real category. final_conf is the confidence of the source that
    supplied the final category."""
    vcat = d.get('vlm_category')
    vmodel = d.get('vlm_model') or ''
    is_economy = 'economy' in str(vmodel)
    try:
        vconf = float(d.get('vlm_confidence')) \
            if d.get('vlm_confidence') is not None else 0.0
    except Exception:
        vconf = 0.0
    if vcat and vconf >= 0.72 and not is_economy:
        return vcat, 'vlm', vconf
    doc_party = d.get('doc_party') or ''
    doc_model = d.get('doc_model') or ''
    if snap_kind == '文档' and doc_party in PARTIES \
            and 'economy' not in str(doc_model):
        try:
            conf = float(d.get('confidence')) \
                if d.get('confidence') is not None else 0.0
        except Exception:
            conf = 0.0
        return doc_party, 'doc', conf
    cat = d.get('category')
    if cat in REAL_CATS:
        try:
            sconf = float(d.get('confidence')) \
                if d.get('confidence') is not None else 0.0
        except Exception:
            sconf = 0.0
        return cat, 'siglip', sconf
    return None, None, 0.0


SNAPSHOT = '/data/midconf_moveback_videos_snapshot.tsv'


def load_rows(db):
    """Only pending rows whose src is in the batch-5 snapshot.
    Returns list of (rq_id, file_id, plan_id, kind, payload, snap_kind)."""
    snap_kind = {}
    with open(SNAPSHOT, encoding='utf-8') as f:
        for line in f:
            p = line.rstrip('\n').split('\t')
            if p and len(p) >= 4:
                snap_kind[p[0]] = p[3]
    rows = db.execute(
        "SELECT id, file_id, plan_id, kind, payload FROM review_queue "
        "WHERE status='pending' "
        "AND json_extract(payload,'$.src') LIKE '/vol1/1000/收件箱/%' "
        "ORDER BY id").fetchall()
    kept = []
    for r in rows:
        try:
            d = json.loads(r[4])
        except Exception:
            continue
        src = d.get('src') or ''
        if src in snap_kind:
            kept.append((r[0], r[1], r[2], r[3], r[4], snap_kind[src]))
    print('snapshot-scoped rows: %d of %d pending inbox rows '
          '(snapshot size %d)' % (len(kept), len(rows), len(snap_kind)),
          flush=True)
    return kept


def doc_dst(d, final):
    """Destination dir for a doc file filed by party."""
    sub = (d.get('doc_subdir') or '').strip()
    if sub not in DOC_SUBDIRS.get(final, set()):
        sub = DOC_FALLBACK_SUBDIR
    return os.path.join(WORK_ROOT, final, sub)


def load_ops_map(db):
    """src -> [(plan_id, dst_path)] for every operations row.
    Used to recognise files the listener (plan #160) already moved to
    their correct library location before this moveback ran."""
    ops = {}
    for plan_id, src, dst in db.execute(
            'SELECT plan_id, src_path, dst_path FROM operations'):
        ops.setdefault(src, []).append((plan_id, dst))
    return ops


def analyze(limit):
    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    rows = load_rows(db)
    ops_map = load_ops_map(db)
    print('operations rows loaded for %d srcs' % len(ops_map), flush=True)
    print('pending snapshot rows: %d' % len(rows), flush=True)
    _hb(label='年份文件夹移回媒体库（中置信）', kind='year-moveback-midconf-videos',
        phase='analyze', status='running', total=len(rows), done=0)
    counts = {}
    by_cat_photo, by_cat_video, by_cat_doc = {}, {}, {}
    by_source = {}
    claimed = set()
    t0 = time.time()
    n = 0
    with open(MANIFEST, 'w', encoding='utf-8') as out:
        out.write('\t'.join(COLS) + '\n')
        for rq_id, file_id, plan_id, kind, payload, snap_kind in rows:
            if limit and n >= limit:
                break
            n += 1
            try:
                d = json.loads(payload)
            except Exception:
                d = {}
            src = d.get('src') or ''
            final, fsrc, fconf = final_category(d, snap_kind)
            vconf = d.get('vlm_confidence')
            dst = ''
            size_src, mtime_src, size_dst = '', '', ''
            sha_src, sha_dst = '', ''
            action, detail, new_name = '', '', ''
            if not final:
                action, detail = 'skip-unclassified', 'no final category'
            elif fconf < 0.85:
                action, detail = 'skip-mid-confidence', \
                    'final_conf=%.2f' % fconf
            elif final in PARTIES:
                if snap_kind != '文档':
                    action, detail = 'skip-party-non-doc', \
                        'party cat=%s kind=%s' % (final, snap_kind)
                else:
                    dst = os.path.join(doc_dst(d, final),
                                       os.path.basename(src))
            elif snap_kind == '照片':
                # media type decides the library branch: a photo whose
                # final category is a video-only category is a mismatch
                if final not in PHOTO_DIRS:
                    action, detail = 'skip-cross-category', \
                        'photo cat=%s not in photo_dirs' % final
                else:
                    ext = os.path.splitext(src)[1].lower()
                    if ext not in ALL_MEDIA:
                        action, detail = 'skip-nonmedia', 'ext=%s' % ext
                    else:
                        dst = os.path.join(LIB_ROOT, PHOTO_DIRS[final],
                                           os.path.basename(src))
            elif snap_kind == '视频':
                if final not in VIDEO_DIRS:
                    action, detail = 'skip-cross-category', \
                        'video cat=%s not in video_dirs' % final
                else:
                    ext = os.path.splitext(src)[1].lower()
                    if ext not in ALL_MEDIA:
                        action, detail = 'skip-nonmedia', 'ext=%s' % ext
                    else:
                        dst = os.path.join(LIB_ROOT, VIDEO_DIRS[final],
                                           os.path.basename(src))
            elif snap_kind == '文档':
                action, detail = 'skip-cross-category', \
                    'doc cat=%s not a party' % final
            else:
                action, detail = 'skip-unknown-media', 'kind=%s' % snap_kind
            if dst and not action:
                try:
                    st = os.stat(src)
                    size_src, mtime_src = st.st_size, st.st_mtime
                except FileNotFoundError:
                    # src gone: it may already have been moved to its
                    # correct library location by the listener (plan #160
                    # single-file moves). If an operations dst exists on
                    # disk in the same category directory as this
                    # moveback's dst, the file is already filed.
                    _already = None
                    for _pid, _odst in ops_map.get(src, []):
                        if _odst and os.path.exists(_odst) and \
                                os.path.dirname(os.path.abspath(_odst)) == \
                                os.path.dirname(os.path.abspath(dst)):
                            _already = (_pid, _odst)
                            break
                    if _already:
                        # record the true location (ops dst) in the
                        # manifest, not the would-be moveback dst
                        dst = _already[1]
                        action, detail = 'already', \
                            'already-moved-by-plan-%d:%s' % _already
                    else:
                        action, detail = 'skip-src-gone', 'not-on-disk'
                else:
                    if not os.path.isfile(src):
                        action, detail = 'skip-src-gone', 'not-a-file'
                    elif os.path.abspath(src) == os.path.abspath(dst):
                        action, detail = 'already', 'src==dst'
                    elif dst in claimed:
                        action, detail = 'rename', 'dst-claimed-in-batch'
                    elif os.path.exists(dst):
                        try:
                            same_file = os.path.samefile(src, dst)
                        except OSError:
                            same_file = False
                        if same_file:
                            action, detail = 'already', 'same-inode'
                        else:
                            size_dst = os.stat(dst).st_size
                            if size_src != size_dst:
                                action, detail = 'rename', \
                                    'size-diff:%d-vs-%d' % (size_src, size_dst)
                            else:
                                same, how = head_tail_match(src, dst, size_src)
                                if not same:
                                    action, detail = 'rename', \
                                        'same-size-diff-%s' % how
                                else:
                                    sha_src = sha_dst = ''
                                    action, detail = 'delete-src', \
                                        'sha-%s+dst-verify-at-apply' % how
                    else:
                        claimed.add(dst)
                        action, detail = 'move', ''
            out.write('\t'.join([_s(rq_id), _s(file_id), _s(plan_id), _s(kind),
                                 _s(src), _s(dst), _s(final), _s(fsrc),
                                 _s(vconf), _s(fconf), _s(size_src), _s(mtime_src),
                                 _s(size_dst), _s(sha_src), _s(sha_dst),
                                 _s(action), _s(detail), _s(new_name)]) + '\n')
            counts[action] = counts.get(action, 0) + 1
            if final and fconf >= 0.85:
                by_source[fsrc] = by_source.get(fsrc, 0) + 1
            if action in ('move', 'rename', 'delete-src', 'already'):
                if final in PARTIES:
                    by_cat_doc[final] = by_cat_doc.get(final, 0) + 1
                elif snap_kind == '视频':
                    by_cat_video[final] = by_cat_video.get(final, 0) + 1
                elif snap_kind == '照片':
                    by_cat_photo[final] = by_cat_photo.get(final, 0) + 1
            if n % 1000 == 0:
                print('%d/%d done (%.1fs) counts=%s'
                      % (n, len(rows), time.time() - t0, counts), flush=True)
                _hb(label='年份文件夹移回媒体库（中置信）',
                    kind='year-moveback-midconf-videos',
                    phase='analyze', status='running', total=len(rows), done=n)
    print('ANALYZE DONE n=%d elapsed=%.1fs counts=%s'
          % (n, time.time() - t0, counts), flush=True)
    print('by source (>=0.85): %s (expected %s)'
          % (json.dumps(by_source, ensure_ascii=False, sort_keys=True),
             json.dumps(EXPECTED_SOURCES, ensure_ascii=False, sort_keys=True)),
          flush=True)
    print('by category photo: %s'
          % json.dumps(by_cat_photo, ensure_ascii=False, sort_keys=True),
          flush=True)
    print('by category video: %s'
          % json.dumps(by_cat_video, ensure_ascii=False, sort_keys=True),
          flush=True)
    print('by category doc (party): %s'
          % json.dumps(by_cat_doc, ensure_ascii=False, sort_keys=True),
          flush=True)
    movable = (counts.get('move', 0) + counts.get('rename', 0)
               + counts.get('delete-src', 0) + counts.get('already', 0))
    print('movable (>=0.85, move+rename+delete-src+already): %d (expected %d)'
          % (movable, EXPECTED_TOTAL), flush=True)
    ok = (movable == EXPECTED_TOTAL and by_source == EXPECTED_SOURCES)
    print('DISTRIBUTION MATCH: %s' % ('YES' if ok else 'NO'), flush=True)
    _hb(label='年份文件夹移回媒体库（中置信）', kind='year-moveback-midconf-videos',
        phase='analyze', status='done', total=len(rows), done=n)
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump({'phase': 'analyze', 'n': n,
                   'elapsed': round(time.time() - t0, 1),
                   'counts': counts,
                   'by_source': by_source,
                   'by_category_photo': by_cat_photo,
                   'by_category_video': by_cat_video,
                   'by_category_doc': by_cat_doc,
                   'movable': movable,
                   'distribution_match': ok},
                  f, ensure_ascii=False, indent=2)
    return 0 if ok else 3


def _mark_approved(db, rq_id, decided_by):
    cur = db.execute(
        "UPDATE review_queue SET status='approved', "
        "decided_by=?, "
        "decided_at=strftime('%Y-%m-%dT%H:%M:%S','now') "
        "WHERE id=? AND status='pending'", (decided_by, rq_id))
    return cur.rowcount


def apply(limit):
    if not os.path.exists(MANIFEST):
        print('no manifest %s: run --analyze first' % MANIFEST)
        return 2
    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    stamp = time.strftime('%Y%m%d-%H%M%S')
    bak = '/data/nas-curator.db.bak-%s-pre-midconf-moveback-videos' % stamp
    with sqlite3.connect(bak) as dst:
        db.backup(dst)
    print('db backup:', bak, flush=True)
    cur = db.execute(
        "INSERT INTO plans (library, summary) VALUES ('media', ?)",
        (json.dumps({'kind': 'year-moveback-midconf-videos', 'manifest': MANIFEST,
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
    _hb(label='年份文件夹移回媒体库（中置信）', kind='year-moveback-midconf-videos',
        phase='apply', status='running', total=len(rows), done=0)
    counts = {}
    by_cat = {}
    claimed = set()
    t0 = time.time()
    n = 0
    with open(MAP, 'a', encoding='utf-8') as mapf:
        for r in rows:
            if limit and n >= limit:
                break
            n += 1
            rq_id = int(r['rq_id'])
            action = r['action']
            st = db.execute("SELECT status FROM review_queue WHERE id=?",
                            (rq_id,)).fetchone()
            if not st:
                counts['skip-row-gone'] = counts.get('skip-row-gone', 0) + 1
                continue
            if st[0] != 'pending':
                counts['already-approved'] = counts.get('already-approved', 0) + 1
                continue
            if n % 200 == 0:
                print('%d/%d applied (%.1fs) counts=%s'
                      % (n, len(rows), time.time() - t0, counts), flush=True)
                _hb(label='年份文件夹移回媒体库（中置信）',
                    kind='year-moveback-midconf-videos',
                    phase='apply', status='running', total=len(rows), done=n,
                    failed=counts.get('failed-move', 0)
                    + counts.get('failed-rename', 0)
                    + counts.get('failed-delete', 0))
            item_action = {'delete-src': 'move', 'rename': 'rename',
                           'move': 'move'}.get(action, 'move')
            decided = {'move': 'year-moveback-midconf-videos',
                       'rename': 'year-moveback-midconf-videos-rename',
                       'delete-src': 'year-moveback-midconf-videos-dedup',
                       'already': 'year-moveback-midconf-videos'}.get(action, 'year-moveback-midconf-videos')
            cur = db.execute(
                """INSERT INTO plan_items
                   (plan_id, file_id, src_path, dst_path, category, confidence,
                    reason, evidence, action, kind, size, ext, review_kind, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (plan_id,
                 int(r['file_id']) if r['file_id'] else None,
                 r['src'], r['dst'] or None,
                 r['final_category'],
                 float(r['final_confidence']) if r['final_confidence'] else None,
                 'year-moveback:%s' % (r['detail'] or action),
                 r['final_source'], item_action, 'media',
                 int(r['size_src']) if r['size_src'] else None,
                 os.path.splitext(r['src'])[1].lower(),
                 'low_conf', 'pending'))
            item_id = cur.lastrowid

            def set_item(status):
                db.execute("UPDATE plan_items SET status=? WHERE id=?",
                           (status, item_id))

            src, dst = r['src'], r['dst']
            if action == 'already':
                set_item('done')
                if _mark_approved(db, rq_id, decided):
                    counts['already'] = counts.get('already', 0) + 1
                    counts['review-rows-approved'] = \
                        counts.get('review-rows-approved', 0) + 1
                db.commit()
                continue
            if action.startswith('skip-'):
                set_item('skipped')
                counts['skip-' + action] = counts.get('skip-' + action, 0) + 1
                db.commit()
                continue
            if action == 'move':
                try:
                    st2 = os.stat(src)
                    if r['size_src'] and int(r['size_src']) != st2.st_size:
                        set_item('failed')
                        counts['skip-size-changed'] = \
                            counts.get('skip-size-changed', 0) + 1
                        db.commit()
                        continue
                    if r['mtime_src'] and abs(float(r['mtime_src']) - st2.st_mtime) > 1:
                        set_item('failed')
                        counts['skip-mtime-changed'] = \
                            counts.get('skip-mtime-changed', 0) + 1
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
                if _mark_approved(db, rq_id, decided):
                    counts['file-moved'] = counts.get('file-moved', 0) + 1
                    counts['review-rows-approved'] = \
                        counts.get('review-rows-approved', 0) + 1
                    by_cat[r['final_category']] = \
                        by_cat.get(r['final_category'], 0) + 1
                db.commit()
                continue
            if action == 'delete-src':
                try:
                    st2 = os.stat(src)
                    if r['size_src'] and int(r['size_src']) != st2.st_size:
                        set_item('failed')
                        counts['skip-size-changed'] = \
                            counts.get('skip-size-changed', 0) + 1
                        db.commit()
                        continue
                    if not os.path.exists(dst):
                        set_item('skipped')
                        counts['skip-dst-gone'] = counts.get('skip-dst-gone', 0) + 1
                        db.commit()
                        continue
                    if sha256(src) != sha256(dst):
                        new_name, new_dst = versioned_name(
                            os.path.dirname(dst), src, claimed)
                        try:
                            os.makedirs(os.path.dirname(new_dst), exist_ok=True)
                            shutil.move(src, new_dst)
                            try:
                                os.chown(new_dst, OWNER_UID, OWNER_GID)
                            except OSError:
                                pass
                        except Exception as e:
                            set_item('failed')
                            counts['failed-rename'] = \
                                counts.get('failed-rename', 0) + 1
                            print('FAILED rename rq=%d %s -> %s : %s: %s'
                                  % (rq_id, src, new_dst, type(e).__name__,
                                     str(e)[:120]), flush=True)
                            db.commit()
                            continue
                        db.execute(
                            "INSERT INTO operations (plan_id, src_path, dst_path) "
                            "VALUES (?,?,?)", (plan_id, src, new_dst))
                        mapf.write('\t'.join([src, new_dst,
                                              time.strftime('%Y-%m-%dT%H:%M:%S')]) + '\n')
                        mapf.flush()
                        set_item('done')
                        if _mark_approved(db, rq_id, 'year-moveback-midconf-videos-rename'):
                            counts['file-renamed'] = \
                                counts.get('file-renamed', 0) + 1
                            counts['review-rows-approved'] = \
                                counts.get('review-rows-approved', 0) + 1
                            by_cat[r['final_category']] = \
                                by_cat.get(r['final_category'], 0) + 1
                        db.commit()
                        continue
                    ok, dec = decodes(dst)
                    if ok is not True:
                        set_item('skipped')
                        counts['skip-retained-bad'] = \
                            counts.get('skip-retained-bad', 0) + 1
                        db.commit()
                        continue
                except FileNotFoundError:
                    set_item('skipped')
                    counts['skip-src-gone'] = counts.get('skip-src-gone', 0) + 1
                    db.commit()
                    continue
                try:
                    os.remove(src)
                except Exception as e:
                    set_item('failed')
                    counts['failed-delete'] = counts.get('failed-delete', 0) + 1
                    print('FAILED delete rq=%d %s : %s: %s'
                          % (rq_id, src, type(e).__name__, str(e)[:120]),
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
                if _mark_approved(db, rq_id, decided):
                    counts['file-deleted'] = counts.get('file-deleted', 0) + 1
                    counts['review-rows-approved'] = \
                        counts.get('review-rows-approved', 0) + 1
                db.commit()
                continue
            if action == 'rename':
                try:
                    st2 = os.stat(src)
                    if r['size_src'] and int(r['size_src']) != st2.st_size:
                        set_item('failed')
                        counts['skip-size-changed'] = \
                            counts.get('skip-size-changed', 0) + 1
                        db.commit()
                        continue
                except FileNotFoundError:
                    set_item('skipped')
                    counts['skip-src-gone'] = counts.get('skip-src-gone', 0) + 1
                    db.commit()
                    continue
                new_name, new_dst = versioned_name(
                    os.path.dirname(dst), src, claimed)
                try:
                    os.makedirs(os.path.dirname(new_dst), exist_ok=True)
                    shutil.move(src, new_dst)
                    try:
                        os.chown(new_dst, OWNER_UID, OWNER_GID)
                    except OSError:
                        pass
                except Exception as e:
                    set_item('failed')
                    counts['failed-rename'] = counts.get('failed-rename', 0) + 1
                    print('FAILED rename rq=%d %s -> %s : %s: %s'
                          % (rq_id, src, new_dst, type(e).__name__, str(e)[:120]),
                          flush=True)
                    db.commit()
                    continue
                db.execute(
                    "INSERT INTO operations (plan_id, src_path, dst_path) "
                    "VALUES (?,?,?)", (plan_id, src, new_dst))
                mapf.write('\t'.join([src, new_dst,
                                      time.strftime('%Y-%m-%dT%H:%M:%S')]) + '\n')
                mapf.flush()
                set_item('done')
                if _mark_approved(db, rq_id, decided):
                    counts['file-renamed'] = counts.get('file-renamed', 0) + 1
                    counts['review-rows-approved'] = \
                        counts.get('review-rows-approved', 0) + 1
                    by_cat[r['final_category']] = \
                        by_cat.get(r['final_category'], 0) + 1
                db.commit()
                continue
            set_item('skipped')
            counts['skip-unknown-action'] = counts.get('skip-unknown-action', 0) + 1
            db.commit()
    db.execute(
        "UPDATE plans SET status='done', summary=? WHERE id=?",
        (json.dumps({'kind': 'year-moveback-midconf-videos', 'manifest': MANIFEST,
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
    _hb(label='年份文件夹移回媒体库（中置信）', kind='year-moveback-midconf-videos',
        phase='apply', status='done', total=len(rows), done=n,
        failed=counts.get('failed-move', 0)
        + counts.get('failed-rename', 0)
        + counts.get('failed-delete', 0))
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
    ap.add_argument('--snapshot', default=SNAPSHOT,
                    help='snapshot TSV to scope the move (default: midconf snapshot)')
    args = ap.parse_args()
    SNAPSHOT = args.snapshot
    if args.analyze:
        raise SystemExit(analyze(args.limit))
    elif args.apply:
        raise SystemExit(apply(args.limit))
    else:
        ap.print_help()
        raise SystemExit(2)
