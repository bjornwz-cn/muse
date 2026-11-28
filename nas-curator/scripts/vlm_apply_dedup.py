#!/usr/bin/env python3
"""VLM 阶段 2 续：359 个目标同名遗留项清理。

来源：计划 #148 的 skipped plan_items（目标已有同名文件，未覆盖）。
构成（2026-11-23 实查）：349 个 dst-on-disk + 10 个批内同名冲突；
实时比对大小：335 个同大小、24 个大小不同；全部是图片
（.heic 292、.jpg 54、.jpeg 10、.gif 2、.png 1）。

Phase 1 --analyze：逐项核对源/目标仍在，写清单 TSV。不动文件、不改 DB。
  同大小 -> 算源与目标 sha256：
    相同 -> 核对保留份（目标）完整可用（PIL 解码，失败再试 cv2；
            视频抽帧；.livp 验 zip），通过 -> delete-src；
            保留份解码失败 -> skip-retained-bad（不动，留人工）。
    不同 -> rename（同名不同内容）。
  大小不同 -> rename（同名不同内容）。
  目标已不在 -> move（原目标空出来了，按阶段 2 原动作移入）。
  源与目标同一 inode -> already（同一文件，只标 approved）。
Phase 2 --apply：先经 sqlite backup API 备份 DB，再建 plan +
  plan_items，逐项复核（sha256 重算、保留份重验、改名目标仍空闲）
  后执行：delete-src 删源文件，rename 按 11-20 规则改名移入，
  review 行标 approved（delete 用 decided_by='vlm-apply-ge85-dedup'，
  rename/move 用 'vlm-apply-ge85-rename'），行不删、留审计。

改名规则（王震 2026-11-20 定）：同名不同内容改名加版本号，版本号
先按文档创建日期时间（照片取 EXIF 拍摄时间，取不到用文件 mtime）、
再按文件大小、最后标顺序号。格式：
  {stem}_{YYYYMMDD-HHMMSS}_{size}_{seq}{ext}
顺序号从 1 起，候选名已存在则递增，直到空闲。

安全规则（王震裁决）：
- 删重复副本须哈希相同之外、核对保留份完整可用，只比哈希不够。
- 绝不覆盖任何已存在的文件；改名目标必须空闲才移动。
- 不碰 <0.85 的行、dhash 重复项、#107 剩余项、百度网盘回传。

运行位置：NAS 容器内（需要 /data 与 /vol1/1000 挂载，root 身份）。
  docker exec nas-curator python3 /data/vlm_apply_dedup.py --analyze
  docker exec nas-curator python3 /data/vlm_apply_dedup.py --apply

断点续跑：apply 按行查 review 状态，已 approved 的行跳过
（already-approved），重跑不重复删/移。
"""
import argparse, hashlib, json, os, re, shutil, sqlite3, time
from datetime import datetime
from PIL import Image
import pillow_heif
pillow_heif.register_heif_opener()
import cv2

DB = '/data/nas-curator.db'
MANIFEST = '/data/vlm_apply_dedup.tsv'
SUMMARY = '/data/vlm_apply_dedup_summary.json'
MAP = '/data/vlm_apply_dedup_map.tsv'
LIB_ROOT = '/vol1/1000/媒体库'
OWNER_UID, OWNER_GID = 1000, 1000
PLAN_ID_SRC = 148

IMG_EXTS = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp', '.gif',
            '.bmp', '.tif', '.tiff', '.jfif'}
VID_EXTS = {'.mov', '.mp4', '.m4v', '.3gp', '.avi', '.mkv', '.vob',
            '.wmv', '.flv'}

COLS = ['rq_id', 'file_id', 'plan_id', 'kind', 'src', 'dst',
        'vlm_category', 'vlm_confidence', 'vlm_model',
        'size_src', 'mtime_src', 'size_dst', 'sha_src', 'sha_dst',
        'action', 'detail', 'new_name']


def _s(x):
    return '' if x is None else str(x)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def decodes(path):
    """(ok, detail) 保留份完整可用检查。ok None = 该扩展无检查器。
    图片：PIL 先行（已注册 pillow_heif，可读 HEIC），失败再试 cv2；
    视频：cv2 读一帧；.livp：验 zip。"""
    ext = os.path.splitext(path)[1].lower()
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
            frame = None
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
    """文档创建日期时间：照片取 EXIF 拍摄时间，取不到用文件 mtime。"""
    try:
        with Image.open(path) as im:
            exif = im._getexif()
        if exif:
            for tag in (36867, 36868, 306):  # DateTimeOriginal, Digitized, DateTime
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
    """11-20 规则改名：{stem}_{YYYYMMDD-HHMMSS}_{size}_{seq}{ext}。
    seq 从 1 起，候选名已存在（磁盘或本批已占）则递增。"""
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


def load_skipped(db):
    """计划 #148 的 skipped 项，与 review_queue 按 payload src 关联，
    只取 review 仍 pending 的行（按 id 升序）。"""
    items = db.execute(
        "SELECT src_path, dst_path, reason FROM plan_items "
        "WHERE plan_id=? AND status='skipped' ORDER BY id",
        (PLAN_ID_SRC,)).fetchall()
    out = []
    for src, dst, reason in items:
        r = db.execute(
            "SELECT id, file_id, plan_id, kind, payload FROM review_queue "
            "WHERE status='pending' AND json_extract(payload,'$.src')=? "
            "ORDER BY id LIMIT 1", (src,)).fetchone()
        if not r:
            out.append({'rq_id': None, 'file_id': None, 'plan_id': None,
                        'kind': '', 'src': src, 'dst': dst,
                        'vlm_category': '', 'vlm_confidence': '',
                        'vlm_model': '', 'reason148': reason,
                        'orphan': True})
            continue
        rq_id, file_id, plan_id, kind, payload = r
        try:
            d = json.loads(payload)
        except Exception:
            d = {}
        out.append({'rq_id': rq_id, 'file_id': file_id, 'plan_id': plan_id,
                    'kind': kind, 'src': src, 'dst': dst,
                    'vlm_category': d.get('vlm_category') or '',
                    'vlm_confidence': d.get('vlm_confidence'),
                    'vlm_model': d.get('vlm_model') or '',
                    'reason148': reason, 'orphan': False})
    return out


def analyze(limit):
    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    items = load_skipped(db)
    print('plan #148 skipped items: %d' % len(items), flush=True)
    counts = {}
    claimed = set()  # 本批改名已占用的目标路径
    t0 = time.time()
    n = 0
    with open(MANIFEST, 'w', encoding='utf-8') as out:
        out.write('\t'.join(COLS) + '\n')
        for it in items:
            if limit and n >= limit:
                break
            n += 1
            rq_id = it['rq_id']
            src, dst = it['src'], it['dst']
            size_src = mtime_src = size_dst = sha_src = sha_dst = ''
            action, detail, new_name = '', '', ''
            if it['orphan']:
                action, detail = 'skip-orphan', 'no pending review row'
            else:
                try:
                    st = os.stat(src)
                    size_src, mtime_src = st.st_size, st.st_mtime
                except FileNotFoundError:
                    action, detail = 'skip-src-gone', 'not-on-disk'
                else:
                    if not os.path.isfile(src):
                        action, detail = 'skip-src-gone', 'not-a-file'
                    elif not os.path.exists(dst):
                        # 目标已不在：原目标空出来了，按阶段 2 原动作移入。
                        action, detail = 'move', 'dst-freed'
                    else:
                        try:
                            same_file = os.path.samefile(src, dst)
                        except OSError:
                            same_file = False
                        if same_file:
                            action, detail = 'already', 'same-inode'
                        else:
                            size_dst = os.stat(dst).st_size
                            if size_src != size_dst:
                                new_name, _ = versioned_name(
                                    os.path.dirname(dst), src, claimed)
                                action = 'rename'
                                detail = 'size-diff:%d-vs-%d' % (size_src, size_dst)
                            else:
                                sha_src = sha256(src)
                                sha_dst = sha256(dst)
                                if sha_src != sha_dst:
                                    new_name, _ = versioned_name(
                                        os.path.dirname(dst), src, claimed)
                                    action = 'rename'
                                    detail = 'same-size-diff-hash'
                                else:
                                    ok, dec = decodes(dst)
                                    if ok is True:
                                        action, detail = 'delete-src', 'sha-same+dst-' + dec
                                    elif ok is False:
                                        action, detail = 'skip-retained-bad', 'dst-' + dec
                                    else:
                                        action, detail = 'skip-unhandled-ext', 'dst-' + dec
            out.write('\t'.join([_s(rq_id), _s(it['file_id']), _s(it['plan_id']),
                                 _s(it['kind']), _s(src), _s(dst),
                                 _s(it['vlm_category']), _s(it['vlm_confidence']),
                                 _s(it['vlm_model']), _s(size_src), _s(mtime_src),
                                 _s(size_dst), _s(sha_src), _s(sha_dst),
                                 _s(action), _s(detail), _s(new_name)]) + '\n')
            counts[action] = counts.get(action, 0) + 1
            if n % 100 == 0:
                print('%d/%d done (%.1fs) counts=%s'
                      % (n, len(items), time.time() - t0, counts), flush=True)
    print('ANALYZE DONE n=%d elapsed=%.1fs counts=%s'
          % (n, time.time() - t0, counts), flush=True)
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump({'phase': 'analyze', 'n': n,
                   'elapsed': round(time.time() - t0, 1),
                   'counts': counts},
                  f, ensure_ascii=False, indent=2)


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
    bak = '/data/nas-curator.db.bak-%s-pre-vlm-dedup' % stamp
    with sqlite3.connect(bak) as dst:
        db.backup(dst)
    print('db backup:', bak, flush=True)
    cur = db.execute(
        "INSERT INTO plans (library, summary) VALUES ('media', ?)",
        (json.dumps({'kind': 'vlm-apply-ge85-dedup', 'manifest': MANIFEST,
                     'backup': bak, 'src_plan': PLAN_ID_SRC},
                    ensure_ascii=False),))
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
    counts = {}
    claimed = set()
    t0 = time.time()
    n = 0
    with open(MAP, 'a', encoding='utf-8') as mapf:
        for r in rows:
            if limit and n >= limit:
                break
            n += 1
            rq_id = int(r['rq_id']) if r['rq_id'] else None
            action = r['action']
            if rq_id is None:
                counts['skip-no-rq'] = counts.get('skip-no-rq', 0) + 1
                continue
            st = db.execute("SELECT status FROM review_queue WHERE id=?",
                            (rq_id,)).fetchone()
            if not st:
                counts['skip-row-gone'] = counts.get('skip-row-gone', 0) + 1
                continue
            if st[0] != 'pending':
                counts['already-approved'] = counts.get('already-approved', 0) + 1
                continue
            # plan_items.action 有 CHECK 约束，只允许
            # ('move','rename','tag','skip','review')，无 'delete'；
            # delete-src 也记 'move'（与 vlm_apply.py 对 already/skip
            # 行的做法一致），真实动作以 reason 字段与 operations 为准。
            item_action = {'delete-src': 'move', 'rename': 'rename',
                           'move': 'move'}.get(action, 'move')
            cur = db.execute(
                """INSERT INTO plan_items
                   (plan_id, file_id, src_path, dst_path, category, confidence,
                    reason, evidence, action, kind, size, ext, review_kind, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (plan_id,
                 int(r['file_id']) if r['file_id'] else None,
                 r['src'], r['dst'] or None,
                 r['vlm_category'],
                 float(r['vlm_confidence']) if r['vlm_confidence'] else None,
                 'vlm-apply-ge85-dedup:%s' % (r['detail'] or action),
                 r['vlm_model'], item_action, 'media',
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
                if _mark_approved(db, rq_id, 'vlm-apply-ge85-dedup'):
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
                # 目标已空：复核源仍在、目标仍不存在，再移入。
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
                if _mark_approved(db, rq_id, 'vlm-apply-ge85-rename'):
                    counts['file-moved'] = counts.get('file-moved', 0) + 1
                    counts['review-rows-approved'] = \
                        counts.get('review-rows-approved', 0) + 1
                db.commit()
                continue
            if action == 'delete-src':
                # 删源前重算哈希、重验保留份，两项都通过才删。
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
                        set_item('skipped')
                        counts['skip-hash-changed'] = \
                            counts.get('skip-hash-changed', 0) + 1
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
                if _mark_approved(db, rq_id, 'vlm-apply-ge85-dedup'):
                    counts['file-deleted'] = counts.get('file-deleted', 0) + 1
                    counts['review-rows-approved'] = \
                        counts.get('review-rows-approved', 0) + 1
                db.commit()
                continue
            if action == 'rename':
                # 改名前重算版本号（目录状态可能已变），目标必须空闲。
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
                if _mark_approved(db, rq_id, 'vlm-apply-ge85-rename'):
                    counts['file-renamed'] = counts.get('file-renamed', 0) + 1
                    counts['review-rows-approved'] = \
                        counts.get('review-rows-approved', 0) + 1
                db.commit()
                continue
            # 未知 action：不动，留待复核。
            set_item('skipped')
            counts['skip-unknown-action'] = counts.get('skip-unknown-action', 0) + 1
            db.commit()
            if n % 100 == 0:
                print('%d/%d applied (%.1fs) counts=%s'
                      % (n, len(rows), time.time() - t0, counts), flush=True)
    db.execute(
        "UPDATE plans SET status='done', summary=? WHERE id=?",
        (json.dumps({'kind': 'vlm-apply-ge85-dedup', 'manifest': MANIFEST,
                     'backup': bak, 'counts': counts},
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
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump({'phase': 'apply', 'plan_id': plan_id, 'backup': bak,
                   'n': n, 'elapsed': round(time.time() - t0, 1),
                   'counts': counts},
                  f, ensure_ascii=False, indent=2)
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--analyze', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()
    if args.analyze:
        analyze(args.limit)
    elif args.apply:
        raise SystemExit(apply(args.limit))
    else:
        ap.print_help()
        raise SystemExit(2)
