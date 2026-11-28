#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤3 步骤2：去重删除执行（计划＋apply，参照 #107）。
删除清单：/data/pending_triage_dedup2.tsv 中 action=='delete' 的行（52 个，
字节相同且保留份已验证可用）。
删前逐个再验：源在盘＋大小/mtime 未变＋保留份仍在盘＋大小一致＋解码通过；
任一失败则跳过不删。删除后 review 行标 approved，
decided_by='pending-triage-dedup'。
删除前 sqlite3 backup API 备份 DB。
用法：docker exec -d nas-curator python3 /data/pending_triage_dedup_apply.py --apply
"""
import argparse, csv, hashlib, json, os, sqlite3, sys, time

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
except Exception:
    cv2 = None

DB = '/data/nas-curator.db'
MANIFEST = '/data/pending_triage_dedup2.tsv'
NONE_TSV = '/data/pending_triage_none.tsv'
SUMMARY = '/data/pending_triage_dedup_summary.json'
PROGRESS_DIR = '/data/progress'
HB_ID = 'pending-triage-dedup'
DECIDED_BY = 'pending-triage-dedup'
IMG_EXTS = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp', '.gif',
            '.bmp', '.tif', '.tiff', '.jfif'}
VID_EXTS = {'.mov', '.mp4', '.m4v', '.3gp', '.avi', '.mkv', '.vob',
            '.wmv', '.flv', '.mpg', '.mts'}


def _hb(**kw):
    if _write_hb is None:
        return
    try:
        _write_hb(HB_ID, progress_dir=PROGRESS_DIR, **kw)
    except Exception as e:
        print('heartbeat failed: %s' % e, flush=True)


def decodes(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in IMG_EXTS:
            try:
                with Image.open(path) as im:
                    im.load()
                return True
            except Exception:
                arr = cv2.imread(path, cv2.IMREAD_UNCHANGED)
                return arr is not None and arr.size > 0
        if ext in VID_EXTS:
            cap = cv2.VideoCapture(path)
            ok, frame = cap.read()
            cap.release()
            return bool(ok and frame is not None and frame.size > 0)
        if ext == '.livp':
            import zipfile
            return zipfile.is_zipfile(path)
        # 文档：存在且能读出头即可（zip/pdf/ole 判头，文本可读）
        with open(path, 'rb') as f:
            head = f.read(8)
        if ext in ('.docx', '.xlsx', '.pptx', '.zip'):
            import zipfile
            return zipfile.is_zipfile(path)
        if ext == '.pdf':
            return head.startswith(b'%PDF')
        if ext in ('.doc', '.xls', '.ppt'):
            return head.startswith(b'\xd0\xcf\x11\xe0')
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if not args.apply:
        print('need --apply')
        return 2
    rows = []
    with open(MANIFEST, encoding='utf-8') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            rows.append(r)
    sizes = {}
    with open(NONE_TSV, encoding='utf-8') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            sizes[r['rq_id']] = (r['size'], r['mtime'])
    n_del = sum(1 for r in rows if r['action'] == 'delete')
    print('manifest rows: %d delete candidates: %d' % (len(rows), n_del), flush=True)

    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    stamp = time.strftime('%Y%m%d-%H%M%S')
    bak = '/data/nas-curator.db.bak-%s-pre-pending-triage-dedup' % stamp
    with sqlite3.connect(bak) as dst:
        db.backup(dst)
    print('db backup:', bak, flush=True)
    cur = db.execute(
        "INSERT INTO plans (library, summary) VALUES ('media', ?)",
        (json.dumps({'kind': 'pending-triage-dedup', 'manifest': MANIFEST,
                     'backup': bak}, ensure_ascii=False),))
    plan_id = cur.lastrowid
    db.execute("UPDATE plans SET status='applying' WHERE id=?", (plan_id,))
    db.commit()
    print('plan #%d created' % plan_id, flush=True)

    _hb(phase='apply', status='running', total=len(rows), done=0)
    counts = {}
    freed = 0
    t0 = time.time()
    for n, r in enumerate(rows, 1):
        src = r['src']
        rq_id = int(r['rq_id']) if r['rq_id'] else None
        action = r['action']
        size_list, mtime_list = sizes.get(r['rq_id'], ('', ''))
        cur = db.execute(
            """INSERT INTO plan_items
               (plan_id, src_path, category, confidence, reason, evidence,
                action, kind, size, ext, sha256, review_kind, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (plan_id, src, 'duplicate', 0.0,
             'pending-triage-dedup:%s retained=%s' % (r['reason'], r['retained']),
             r['peers_lib'] or r['peers_inbox'], 'review', 'media',
             int(size_list) if size_list else None,
             os.path.splitext(src)[1].lower(),
             r['sha256'] or None, 'dup',
             'done' if action == 'delete' else 'skipped'))
        item_id = cur.lastrowid
        if action != 'delete':
            counts['skip-' + r['reason']] = counts.get('skip-' + r['reason'], 0) + 1
            db.commit()
            continue
        # 再验：源大小/mtime＋保留份存在/大小/解码
        fail = ''
        try:
            st = os.stat(src)
            if size_list and int(size_list) != st.st_size:
                fail = 'size-changed'
            elif mtime_list and abs(int(mtime_list) - int(st.st_mtime)) > 1:
                fail = 'mtime-changed'
        except FileNotFoundError:
            fail = 'src-gone'
        retained = r['retained']
        if not fail:
            try:
                rst = os.stat(retained)
                if rst.st_size != st.st_size:
                    fail = 'retained-size-mismatch'
                elif not decodes(retained):
                    fail = 'retained-decode-fail'
            except FileNotFoundError:
                fail = 'retained-gone'
            except Exception as e:
                fail = 'retained-check-error:%s' % str(e)[:60]
        if fail:
            db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (item_id,))
            counts['skip-' + fail] = counts.get('skip-' + fail, 0) + 1
            print('SKIP delete %s : %s (retained %s)' % (src, fail, retained), flush=True)
            db.commit()
            continue
        try:
            os.remove(src)
            freed += st.st_size
        except Exception as e:
            db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (item_id,))
            counts['failed-delete'] = counts.get('failed-delete', 0) + 1
            print('FAILED delete %s : %s: %s' % (src, type(e).__name__, str(e)[:120]), flush=True)
            db.commit()
            continue
        if rq_id:
            cur2 = db.execute(
                "UPDATE review_queue SET status='approved', decided_by=?, "
                "decided_at=strftime('%Y-%m-%dT%H:%M:%S','now') "
                "WHERE id=? AND status='pending'", (DECIDED_BY, rq_id))
            if cur2.rowcount:
                counts['review-rows-approved'] = counts.get('review-rows-approved', 0) + cur2.rowcount
        counts['file-deleted'] = counts.get('file-deleted', 0) + 1
        db.commit()
        if n % 100 == 0:
            print('%d/%d applied (%.1fs) counts=%s' % (n, len(rows), time.time() - t0, counts), flush=True)
            _hb(phase='apply', status='running', total=len(rows), done=n)
    db.execute(
        "UPDATE plans SET status='done', summary=? WHERE id=?",
        (json.dumps({'kind': 'pending-triage-dedup', 'manifest': MANIFEST,
                     'backup': bak, 'counts': counts,
                     'freed_bytes': freed}, ensure_ascii=False), plan_id))
    db.commit()
    print('integrity:', db.execute('PRAGMA integrity_check').fetchone()[0], flush=True)
    print('pending now:', db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE status='pending'").fetchone()[0], flush=True)
    print('plan #%d items:' % plan_id, db.execute(
        "SELECT status, COUNT(*) FROM plan_items WHERE plan_id=? GROUP BY status",
        (plan_id,)).fetchall(), flush=True)
    print('APPLY DONE n=%d elapsed=%.1fs counts=%s freed=%d bytes (%.2f MB)'
          % (len(rows), time.time() - t0, counts, freed, freed / (1 << 20)), flush=True)
    _hb(phase='apply', status='done', total=len(rows), done=len(rows))
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump({'phase': 'apply', 'plan_id': plan_id, 'backup': bak,
                   'n': len(rows), 'elapsed': round(time.time() - t0, 1),
                   'counts': counts, 'freed_bytes': freed},
                  f, ensure_ascii=False, indent=2)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
