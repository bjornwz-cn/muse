#!/usr/bin/env python3
"""Year-folder moveback: move categorized files from
  /vol1/1000/收件箱/媒体库/照片/<年份> and /视频/<年份>
back into the media library by final category.

Batch 3 (2026-11-24): same rules as batch 2 (plan #155), but scope is
limited to the batch-3 snapshot (/data/year_batch3_snapshot.tsv,
3,533 files locked 2026-11-24 09:00) AND to final confidence >= 0.85
(1,478 files, per 王震 2026-11-24 11:41 "有把握的就移，没有就复检").
The other 591 confirmed files are mid-confidence (< 0.85) and stay in
the review queue untouched; the 1,464 unclassified also stay.
Rows whose src is NOT in the snapshot are never touched.

Final category (same rule as year_summary_batch3.py, plus economy
exclusion): vlm_category present, vlm_confidence >= 0.72 and the VLM
model is NOT the economy model -> VLM category; else payload category
if in REAL_CATS (SigLIP real category); else unclassified.
final_confidence is the confidence of the source that supplied the
final category. Economy VLM conclusions only go to review.

Phase 1 --analyze: build manifest TSV, verify counts against the
>= 0.85 distribution table (photo 1,348 + video 130 = 1,478).
No files moved, no DB writes. For dst-exists rows analyze decides
rename vs delete-src by size + sha256 only; the retained-copy decode
check and the versioned-name computation are deferred to --apply
(which re-verifies them), so analyze stays fast.
Phase 2 --apply: sqlite backup first, create plan + plan_items,
verify (size+mtime, sha re-check for delete-src, retained-copy decode
check, rename target free) then move/rename/delete-src, write
operations, mark review rows approved (decided_by='year-moveback-b3*').
Resume-safe: already-approved rows are skipped on re-run.

Same-name handling (rules set 2026-11-20/2026-11-23):
- dst exists, sizes differ OR same size but content differs -> rename with
  version {stem}_{YYYYMMDD-HHMMSS}_{size}_{seq}{ext} (EXIF time first,
  else mtime; then size; then seq).
- dst exists, content identical -> delete source ONLY after the retained
  copy (dst) passes a decode check (PIL -> cv2, video frame, .livp zip).
- dst exists but retained copy fails decode -> skip-retained-bad.
- never overwrite any existing file.
- Large same-size files: analyze compares only head+tail 1MB as a
  pre-filter (a mismatch proves different content); apply always
  re-verifies with a full sha256 before any delete, and renames when
  the full hash disagrees. Disk reads are slow under the concurrent
  Baidu download load (~50MB/s), so full hashing is never done twice.

Safety:
- Only review_queue rows with src under 收件箱/媒体库/ and status
  pending; mid-confidence and unclassified rows never move; files
  without a review row (e.g. new Baidu downloads) never move;
  待整理/ and 待整理-视频/ are outside the src filter by construction.
- Non-media extensions (incl. credential files) -> skip-nonmedia.

Runs inside the nas-curator container (needs /data and /vol1/1000).
  docker exec nas-curator python3 /data/year_moveback_batch3.py --analyze
  docker exec -d nas-curator python3 /data/year_moveback_batch3.py --apply
"""
import argparse, hashlib, json, os, shutil, sqlite3, sys, time
from datetime import datetime

DB = '/data/nas-curator.db'
MANIFEST = '/data/year_moveback_batch3.tsv'
SUMMARY = '/data/year_moveback_batch3_summary.json'
MAP = '/data/year_moveback_batch3_map.tsv'
LIB_ROOT = '/vol1/1000/媒体库'
OWNER_UID, OWNER_GID = 1000, 1000
PROGRESS_DIR = os.path.join(os.path.dirname(os.path.abspath(DB)), 'progress')
HB_ID = 'year-moveback-batch3'

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
}
REAL_CATS = set(PHOTO_DIRS) | set(VIDEO_DIRS)
IMG_EXTS = {'.jpg', '.jpeg', '.heic', '.png', '.heif', '.webp', '.gif', '.nef'}
VID_EXTS = {'.mov', '.mp4', '.mts', '.vob', '.avi', '.wmv'}
ALL_MEDIA = IMG_EXTS | VID_EXTS

COLS = ['rq_id', 'file_id', 'plan_id', 'kind', 'src', 'dst',
        'final_category', 'final_source', 'vlm_confidence',
        'final_confidence',
        'size_src', 'mtime_src', 'size_dst', 'sha_src', 'sha_dst',
        'action', 'detail', 'new_name']

# Batch-3 >= 0.85 distribution, computed from DB 2026-11-24 11:42
# (snapshot rows with a final category and final_confidence >= 0.85;
# photo 1,348 + video 130 = 1,478. Mid-confidence 591 and unclassified
# 1,464 are NOT in this table -- they stay in the review queue.)
EXPECTED_PHOTO = {'儿童成长': 512, '人物合影': 260, '文档与票据': 164,
                  '自拍': 116, '旅行风景': 64, '城市与建筑': 53,
                  '日常生活': 30, '工作与活动': 45, '屏幕截图': 47,
                  '美食与咖啡': 45, '宠物与动物': 10, '梗图与表情': 2}
EXPECTED_VIDEO = {'儿童成长': 76, '日常生活': 9, '旅行风景': 10,
                  '运动与活动': 18, '聚会与夜生活': 14, '屏幕录制': 3}


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
    re-verifies with a full sha256 before any delete, so safety is
    unchanged. Full sha256 is used directly for files <= 2MB."""
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


def final_category(d):
    """(final, source, final_conf) or (None, None, 0.0).
    Same rule as year_summary_batch3.py, plus economy exclusion:
    VLM conclusions from the economy model (vlm_model contains
    'economy') are ignored -- they only go to review, never to a
    final category. VLM >= 0.72 (standard model only) wins, else
    SigLIP real category. final_conf is the confidence of the
    source that supplied the final category."""
    vcat = d.get('vlm_category')
    vmodel = d.get('vlm_model') or ''
    is_economy = 'economy' in vmodel
    try:
        vconf = float(d.get('vlm_confidence')) \
            if d.get('vlm_confidence') is not None else 0.0
    except Exception:
        vconf = 0.0
    if vcat and vconf >= 0.72 and not is_economy:
        return vcat, 'vlm', vconf
    cat = d.get('category')
    if cat in REAL_CATS:
        try:
            sconf = float(d.get('confidence')) \
                if d.get('confidence') is not None else 0.0
        except Exception:
            sconf = 0.0
        return cat, 'siglip', sconf
    return None, None, 0.0


SNAPSHOT = '/data/year_batch3_snapshot.tsv'


def load_rows(db):
    """Only pending rows whose src is in the batch-2 snapshot.
    Snapshot-external rows (batch-1 leftovers, new Baidu downloads)
    are never touched."""
    with open(SNAPSHOT, encoding='utf-8') as f:
        snap = {line.rstrip('\n').split('\t')[0] for line in f if line.strip()}
    rows = db.execute(
        "SELECT id, file_id, plan_id, kind, payload FROM review_queue "
        "WHERE status='pending' "
        "AND json_extract(payload,'$.src') "
        "LIKE '/vol1/1000/收件箱/媒体库/%' "
        "ORDER BY id").fetchall()
    kept = []
    for r in rows:
        try:
            d = json.loads(r[4])
        except Exception:
            continue
        if (d.get('src') or '') in snap:
            kept.append(r)
    print('snapshot-scoped rows: %d of %d pending inbox/media rows '
          '(snapshot size %d)' % (len(kept), len(rows), len(snap)), flush=True)
    return kept


def analyze(limit):
    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    rows = load_rows(db)
    print('pending inbox/media rows: %d' % len(rows), flush=True)
    _hb(label='年份文件夹移回媒体库（第三批）', kind='year-moveback-batch3',
        phase='analyze', status='running', total=len(rows), done=0)
    counts = {}
    by_cat_photo, by_cat_video = {}, {}
    claimed = set()
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
            final, fsrc, fconf = final_category(d)
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
            elif '/照片/' in src:
                media, dirs = 'photo', PHOTO_DIRS
                ext = os.path.splitext(src)[1].lower()
                if ext not in ALL_MEDIA:
                    action, detail = 'skip-nonmedia', 'ext=%s' % ext
                elif final not in dirs:
                    action, detail = 'skip-cross-category', \
                        'photo cat=%s not in photo_dirs' % final
                else:
                    dst = os.path.join(LIB_ROOT, dirs[final],
                                       os.path.basename(src))
            elif '/视频/' in src:
                media, dirs = 'video', VIDEO_DIRS
                ext = os.path.splitext(src)[1].lower()
                if ext not in ALL_MEDIA:
                    action, detail = 'skip-nonmedia', 'ext=%s' % ext
                elif final not in dirs:
                    action, detail = 'skip-cross-category', \
                        'video cat=%s not in video_dirs' % final
                else:
                    dst = os.path.join(LIB_ROOT, dirs[final],
                                       os.path.basename(src))
            else:
                action, detail = 'skip-unknown-media', 'src=%s' % src[:80]
            if dst and not action:
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
                    elif dst in claimed:
                        # 批内同名：改名（版本号在 apply 时算，避免 analyze
                        # 里重复做 EXIF 解析）。
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
                                    # Candidate only: apply re-verifies with
                                    # a full sha256 + retained-copy decode
                                    # check before any delete.
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
            if final and action in ('move', 'already', 'rename', 'delete-src',
                                    'skip-retained-bad', 'skip-unhandled-ext'):
                tgt = by_cat_photo if '/照片/' in src else by_cat_video
                tgt[final] = tgt.get(final, 0) + 1
            if n % 1000 == 0:
                print('%d/%d done (%.1fs) counts=%s'
                      % (n, len(rows), time.time() - t0, counts), flush=True)
                _hb(label='年份文件夹移回媒体库（第三批）', kind='year-moveback-batch3',
                    phase='analyze', status='running', total=len(rows), done=n)
    print('ANALYZE DONE n=%d elapsed=%.1fs counts=%s'
          % (n, time.time() - t0, counts), flush=True)
    print('categorized on-disk photo: %s'
          % json.dumps(by_cat_photo, ensure_ascii=False, sort_keys=True),
          flush=True)
    print('categorized on-disk video: %s'
          % json.dumps(by_cat_video, ensure_ascii=False, sort_keys=True),
          flush=True)
    ok = True
    for cat, exp in EXPECTED_PHOTO.items():
        got = by_cat_photo.get(cat, 0)
        if got != exp:
            print('MISMATCH photo %s: expected %d got %d' % (cat, exp, got),
                  flush=True)
            ok = False
    for cat, exp in EXPECTED_VIDEO.items():
        got = by_cat_video.get(cat, 0)
        if got != exp:
            print('MISMATCH video %s: expected %d got %d' % (cat, exp, got),
                  flush=True)
            ok = False
    for cat in by_cat_photo:
        if cat not in EXPECTED_PHOTO:
            print('EXTRA photo cat %s: %d' % (cat, by_cat_photo[cat]), flush=True)
            ok = False
    for cat in by_cat_video:
        if cat not in EXPECTED_VIDEO:
            print('EXTRA video cat %s: %d' % (cat, by_cat_video[cat]), flush=True)
            ok = False
    tot_photo = sum(by_cat_photo.values())
    tot_video = sum(by_cat_video.values())
    print('categorized on-disk total: photo %d (expected 1348), '
          'video %d (expected 130), all %d (expected 1478)'
          % (tot_photo, tot_video, tot_photo + tot_video), flush=True)
    if tot_photo != 1348 or tot_video != 130:
        ok = False
    print('DISTRIBUTION MATCH: %s' % ('YES' if ok else 'NO'), flush=True)
    _hb(label='年份文件夹移回媒体库（第三批）', kind='year-moveback-batch3',
        phase='analyze', status='done', total=len(rows), done=n)
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump({'phase': 'analyze', 'n': n,
                   'elapsed': round(time.time() - t0, 1),
                   'counts': counts,
                   'by_category_photo': by_cat_photo,
                   'by_category_video': by_cat_video,
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
    bak = '/data/nas-curator.db.bak-%s-pre-year-moveback-b3' % stamp
    with sqlite3.connect(bak) as dst:
        db.backup(dst)
    print('db backup:', bak, flush=True)
    cur = db.execute(
        "INSERT INTO plans (library, summary) VALUES ('media', ?)",
        (json.dumps({'kind': 'year-moveback-batch3', 'manifest': MANIFEST,
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
    _hb(label='年份文件夹移回媒体库（第三批）', kind='year-moveback-batch3', phase='apply',
        status='running', total=len(rows), done=0)
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
                _hb(label='年份文件夹移回媒体库（第三批）', kind='year-moveback-batch3',
                    phase='apply', status='running', total=len(rows), done=n,
                    failed=counts.get('failed-move', 0)
                    + counts.get('failed-rename', 0)
                    + counts.get('failed-delete', 0))
            # plan_items.action CHECK: ('move','rename','tag','skip','review');
            # delete-src is recorded as 'move', true action in reason.
            item_action = {'delete-src': 'move', 'rename': 'rename',
                           'move': 'move'}.get(action, 'move')
            decided = {'move': 'year-moveback-b3',
                       'rename': 'year-moveback-b3-rename',
                       'delete-src': 'year-moveback-b3-dedup',
                       'already': 'year-moveback-b3'}.get(action, 'year-moveback-b3')
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
                        # Head/tail pre-filter said "maybe identical" but
                        # the full hash disagrees: same name, different
                        # content -> rename instead of skipping.
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
                        if _mark_approved(db, rq_id, 'year-moveback-b3-rename'):
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
        (json.dumps({'kind': 'year-moveback-batch3', 'manifest': MANIFEST,
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
    _hb(label='年份文件夹移回媒体库（第三批）', kind='year-moveback-batch3', phase='apply',
        status='done', total=len(rows), done=n,
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
    args = ap.parse_args()
    if args.analyze:
        raise SystemExit(analyze(args.limit))
    elif args.apply:
        raise SystemExit(apply(args.limit))
    else:
        ap.print_help()
        raise SystemExit(2)
