#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch4 close-out step 1: delete damaged files (NAS Curator, 2026-11-24).

Delete list: /data/damaged_delete_candidates_20261124.tsv
(12,911 rows: stepc 11,591 + gap 690 + batch4 630; all verified on disk
by step-C, 43 VLM-readable files already excluded).

Phase 1 --verify: for each list row, per-file re-verification
(exists + size + sha256 vs plan_items reference if available + decode
check). Only files that FAIL to decode are marked delete. Files that
decode are skipped (usable after all) with the reason recorded.
Exclusions (never deleted, checked explicitly):
  20200614_0001.mov (damaged sole copy, user has not decided),
  4 credential files (reason='credential', pending归库口径),
  encrypted files (__CLOUDSYNC_ENC__ blobs, reason contains 加密),
  43 VLM-readable files (VLM could read them, excluded by step-C).
No deletions, no DB writes in this phase.
Phase 2 --apply: sqlite backup first, create plan + plan_items
(action='review', decided_by='batch4-delete-damaged'), re-verify
size+mtime, delete the file, mark the review row approved.
Resume-safe: already-approved rows are skipped on re-run.
No operations rows are written for deletes (no meaningful dst_path;
follows the #107 sweep precedent -- manifest + plan_items + review
decided_by is the audit trail).

Runs inside the nas-curator container (needs /data and /vol1/1000).
  docker exec nas-curator python3 /data/batch4_delete.py --verify
  docker exec -d nas-curator python3 /data/batch4_delete.py --apply
"""
import argparse, hashlib, json, os, sqlite3, sys, time

DB = '/data/nas-curator.db'
DELETE_LIST = '/data/damaged_delete_candidates_20261124.tsv'
MANIFEST = '/data/batch4_delete_verify.tsv'
SUMMARY = '/data/batch4_delete_summary.json'
PROGRESS_DIR = os.path.join(os.path.dirname(os.path.abspath(DB)), 'progress')
HB_ID = 'batch4-delete'

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

IMG_EXTS = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp', '.gif',
            '.bmp', '.tif', '.tiff', '.jfif'}
VID_EXTS = {'.mov', '.mp4', '.m4v', '.3gp', '.avi', '.mkv', '.vob',
            '.wmv', '.flv'}

COLS = ['src', 'size_list', 'set', 'reason_list', 'rq_id', 'size_disk',
        'mtime_disk', 'sha256_disk', 'sha256_ref', 'action', 'detail']


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


def decodes(path):
    """(ok, detail) lightweight intact check. ok None = no checker for ext.
    Images: PIL first, cv2 fallback. Videos: cv2 reads one frame."""
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


def load_exclusions(db):
    """Return (excluded_set, counts): paths that must never be deleted."""
    excluded = set()
    counts = {}
    # 20200614_0001.mov by basename (damaged sole copy, undecided)
    for (p,) in db.execute(
            "SELECT json_extract(payload,'$.src') FROM review_queue "
            "WHERE json_extract(payload,'$.src') LIKE '%20200614_0001.mov'"):
        if p:
            excluded.add(p)
            counts['excl-20200614'] = counts.get('excl-20200614', 0) + 1
    # 4 credential files (reason exactly 'credential')
    for (p,) in db.execute(
            "SELECT json_extract(payload,'$.src') FROM review_queue "
            "WHERE json_extract(payload,'$.reason')='credential'"):
        if p:
            excluded.add(p)
            counts['excl-credential'] = counts.get('excl-credential', 0) + 1
    # encrypted blobs (reason/payload contains 加密)
    for (p,) in db.execute(
            "SELECT json_extract(payload,'$.src') FROM review_queue "
            "WHERE payload LIKE '%加密%'"):
        if p:
            excluded.add(p)
            counts['excl-encrypted'] = counts.get('excl-encrypted', 0) + 1
    # 43 VLM-readable files: stepc rows with a VLM conclusion but marked
    # damaged by the register (reason contains decode_failed)
    n43 = 0
    for (p,) in db.execute(
            "SELECT json_extract(payload,'$.src') FROM review_queue "
            "WHERE payload LIKE '%stepc%' "
            "AND json_extract(payload,'$.vlm_category') IS NOT NULL "
            "AND json_extract(payload,'$.reason') LIKE '%decode_failed%'"):
        if p:
            excluded.add(p)
            n43 += 1
    counts['excl-vlm-readable'] = n43
    return excluded, counts


def load_sha_refs(db):
    """src -> sha256 from plan_items (reference hash, if any)."""
    refs = {}
    for src, sha in db.execute(
            "SELECT src_path, sha256 FROM plan_items "
            "WHERE sha256 IS NOT NULL AND sha256 != ''"):
        if src not in refs:
            refs[src] = sha
    return refs


def load_rq_map(db):
    """src -> review_queue id for pending rows."""
    m = {}
    for rid, src in db.execute(
            "SELECT id, json_extract(payload,'$.src') FROM review_queue "
            "WHERE status='pending'"):
        if src and src not in m:
            m[src] = rid
    return m


def verify(limit):
    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    excluded, excl_counts = load_exclusions(db)
    print('exclusion sets:', excl_counts, 'total excluded paths:',
          len(excluded), flush=True)
    refs = load_sha_refs(db)
    print('plan_items sha256 refs:', len(refs), flush=True)
    rq_map = load_rq_map(db)
    print('pending review srcs:', len(rq_map), flush=True)
    rows = []
    with open(DELETE_LIST, encoding='utf-8') as f:
        hdr = f.readline().rstrip('\n').split('\t')
        for line in f:
            parts = line.rstrip('\n').split('\t')
            if len(parts) < 5:
                continue
            rows.append(parts)
    print('delete list rows: %d' % len(rows), flush=True)
    _hb(label='第四批收尾：删除损坏文件', kind='batch4-delete',
        phase='verify', status='running', total=len(rows), done=0)
    counts = {}
    freed = 0
    t0 = time.time()
    n = 0
    with open(MANIFEST, 'w', encoding='utf-8') as out:
        out.write('\t'.join(COLS) + '\n')
        for parts in rows:
            if limit and n >= limit:
                break
            n += 1
            src, size_list, dset, reason_list = parts[0], parts[1], parts[2], parts[3]
            rq_id = rq_map.get(src, '')
            size_disk, mtime_disk, sha_disk, sha_ref = '', '', '', refs.get(src, '')
            action, detail = '', ''
            try:
                if src in excluded:
                    action, detail = 'skip-excluded', 'in exclusion set'
                else:
                    try:
                        st = os.stat(src)
                        size_disk, mtime_disk = st.st_size, st.st_mtime
                    except FileNotFoundError:
                        action, detail = 'skip-src-gone', 'not-on-disk'
                    else:
                        if not os.path.isfile(src):
                            action, detail = 'skip-src-gone', 'not-a-file'
                        elif size_list and int(size_list) != st.st_size:
                            action, detail = 'skip-size-changed', \
                                'list=%s disk=%d' % (size_list, st.st_size)
                        else:
                            ok, det = decodes(src)
                            if ok is True:
                                action, detail = 'skip-readable', \
                                    'decodes:' + det
                            elif ok is None:
                                action, detail = 'skip-unhandled-ext', det
                            else:
                                # confirmed damaged: record hash, compare
                                # with reference when available
                                sha_disk = sha256(src)
                                if sha_ref and sha_ref != sha_disk:
                                    action, detail = 'skip-hash-mismatch', \
                                        'ref=%s.. disk=%s..' % (sha_ref[:12], sha_disk[:12])
                                else:
                                    action, detail = 'delete', \
                                        'decode-fail:' + det + \
                                        (' hash-match' if sha_ref else ' no-ref')
                                    freed += st.st_size
            except Exception as e:
                action, detail = ('skip-error',
                                  '%s:%s' % (type(e).__name__, str(e)[:80]))
            out.write('\t'.join([_s(src), _s(size_list), _s(dset),
                                 _s(reason_list), _s(rq_id), _s(size_disk),
                                 _s(mtime_disk), _s(sha_disk), _s(sha_ref),
                                 _s(action), _s(detail)]) + '\n')
            counts[action] = counts.get(action, 0) + 1
            if n % 500 == 0:
                out.flush()
                print('%d/%d verified (%.1fs) counts=%s'
                      % (n, len(rows), time.time() - t0, counts), flush=True)
                _hb(label='第四批收尾：删除损坏文件', kind='batch4-delete',
                    phase='verify', status='running', total=len(rows), done=n)
    print('VERIFY DONE n=%d elapsed=%.1fs counts=%s freed_if_deleted=%d bytes (%.2f GB)'
          % (n, time.time() - t0, counts, freed, freed / (1 << 30)), flush=True)
    _hb(label='第四批收尾：删除损坏文件', kind='batch4-delete',
        phase='verify', status='done', total=len(rows), done=n)
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump({'phase': 'verify', 'n': n,
                   'elapsed': round(time.time() - t0, 1),
                   'counts': counts,
                   'freed_if_deleted_bytes': freed,
                   'exclusion_counts': excl_counts}, f, ensure_ascii=False, indent=2)
    return 0


def apply(limit):
    if not os.path.exists(MANIFEST):
        print('no manifest %s: run --verify first' % MANIFEST)
        return 2
    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    stamp = time.strftime('%Y%m%d-%H%M%S')
    bak = '/data/nas-curator.db.bak-%s-pre-batch4-delete' % stamp
    with sqlite3.connect(bak) as dst:
        db.backup(dst)
    print('db backup:', bak, flush=True)
    cur = db.execute(
        "INSERT INTO plans (library, summary) VALUES ('media', ?)",
        (json.dumps({'kind': 'batch4-delete-damaged', 'manifest': MANIFEST,
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
    _hb(label='第四批收尾：删除损坏文件', kind='batch4-delete',
        phase='apply', status='running', total=len(rows), done=0)
    counts = {}
    freed = 0
    t0 = time.time()
    n = 0
    for r in rows:
        if limit and n >= limit:
            break
        n += 1
        src = r['src']
        action = r['action']
        rq_id = int(r['rq_id']) if r['rq_id'] else None
        if n % 500 == 0:
            print('%d/%d applied (%.1fs) counts=%s'
                  % (n, len(rows), time.time() - t0, counts), flush=True)
            _hb(label='第四批收尾：删除损坏文件', kind='batch4-delete',
                phase='apply', status='running', total=len(rows), done=n)
        # plan_items: action CHECK allows only move/rename/tag/skip/review.
        # A delete decision is recorded as action='review'.
        item_action = 'review'
        cur = db.execute(
            """INSERT INTO plan_items
               (plan_id, src_path, category, confidence, reason, evidence,
                action, kind, size, ext, sha256, review_kind, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (plan_id, src, 'damaged', 0.0,
             'batch4-delete:%s' % (r['detail'] or action),
             r['reason_list'], item_action, 'media',
             int(r['size_disk']) if r['size_disk'] else None,
             os.path.splitext(src)[1].lower(),
             r['sha256_disk'] or None, 'low_conf',
             'done' if action == 'delete' else 'skipped'))
        if action != 'delete':
            counts['skip-' + action] = counts.get('skip-' + action, 0) + 1
            db.commit()
            continue
        # re-verify size+mtime before deleting
        try:
            st = os.stat(src)
            if r['size_disk'] and int(r['size_disk']) != st.st_size:
                db.execute("UPDATE plan_items SET status='failed' WHERE id=?",
                           (cur.lastrowid,))
                counts['skip-size-changed'] = counts.get('skip-size-changed', 0) + 1
                db.commit()
                continue
            if r['mtime_disk'] and abs(float(r['mtime_disk']) - st.st_mtime) > 1:
                db.execute("UPDATE plan_items SET status='failed' WHERE id=?",
                           (cur.lastrowid,))
                counts['skip-mtime-changed'] = counts.get('skip-mtime-changed', 0) + 1
                db.commit()
                continue
        except FileNotFoundError:
            db.execute("UPDATE plan_items SET status='skipped' WHERE id=?",
                       (cur.lastrowid,))
            counts['already-gone'] = counts.get('already-gone', 0) + 1
            db.commit()
            continue
        try:
            os.remove(src)
            freed += st.st_size
        except Exception as e:
            db.execute("UPDATE plan_items SET status='failed' WHERE id=?",
                       (cur.lastrowid,))
            counts['failed-delete'] = counts.get('failed-delete', 0) + 1
            print('FAILED delete %s : %s: %s'
                  % (src, type(e).__name__, str(e)[:120]), flush=True)
            db.commit()
            continue
        if rq_id:
            cur2 = db.execute(
                "UPDATE review_queue SET status='approved', "
                "decided_by='batch4-delete-damaged', "
                "decided_at=strftime('%Y-%m-%dT%H:%M:%S','now') "
                "WHERE id=? AND status='pending'", (rq_id,))
            if cur2.rowcount:
                counts['review-rows-approved'] = \
                    counts.get('review-rows-approved', 0) + cur2.rowcount
        counts['file-deleted'] = counts.get('file-deleted', 0) + 1
        db.commit()
    db.execute(
        "UPDATE plans SET status='done', summary=? WHERE id=?",
        (json.dumps({'kind': 'batch4-delete-damaged', 'manifest': MANIFEST,
                     'backup': bak, 'counts': counts,
                     'freed_bytes': freed}, ensure_ascii=False), plan_id))
    db.commit()
    print('integrity:', db.execute('PRAGMA integrity_check').fetchone()[0],
          flush=True)
    print('pending now:', db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE status='pending'").fetchone()[0],
        flush=True)
    print('plan #%d items:' % plan_id, db.execute(
        "SELECT status, COUNT(*) FROM plan_items WHERE plan_id=? GROUP BY status",
        (plan_id,)).fetchall(), flush=True)
    print('APPLY DONE n=%d elapsed=%.1fs counts=%s freed=%d bytes (%.2f GB)'
          % (n, time.time() - t0, counts, freed, freed / (1 << 30)), flush=True)
    _hb(label='第四批收尾：删除损坏文件', kind='batch4-delete',
        phase='apply', status='done', total=len(rows), done=n)
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump({'phase': 'apply', 'plan_id': plan_id, 'backup': bak,
                   'n': n, 'elapsed': round(time.time() - t0, 1),
                   'counts': counts, 'freed_bytes': freed},
                  f, ensure_ascii=False, indent=2)
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--verify', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()
    if args.verify:
        raise SystemExit(verify(args.limit))
    elif args.apply:
        raise SystemExit(apply(args.limit))
    else:
        ap.print_help()
        raise SystemExit(2)
