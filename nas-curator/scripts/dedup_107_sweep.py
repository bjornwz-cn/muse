#!/usr/bin/env python3
"""Sweep plan #107 dup:sha256 rows (NAS Curator, 2026-11-23).

Phase 1 --analyze: for each pending dup:sha256 review row in plan #107,
verify the src and a retained copy are byte-identical (disk sha256) and the
retained copy decodes, then write a manifest. No deletions, no DB writes.
Phase 2 --apply: re-verify (src size+mtime, retained exists+size+decodes),
delete the src file, mark the review row approved. DB backup first.

IMPORTANT (2026-11-23 live findings, supersede the design doc's premise):
- The files table does NOT cover moved files: only 467/2313 recorded
  evidence paths still exist, and the moved retained copies are NOT in the
  files table at their new paths. So sha256 MUST be computed from disk and
  retained copies found via a filesystem basename index, NOT the files table.
- The recorded evidence path is stale for ~80% of rows (the curator moves
  files after the plan is made). A retained copy is located by basename
  search under /vol1/1000/媒体库/ and verified by disk sha256.
- A retained copy counts only if it is under /vol1/1000/媒体库/ (library).
  Inbox-only copies do not count (transient, may be deduped away).
- dhash rows (9,085) are NOT touched by this script.

Actions (analyze):
  delete               src + retained are byte-identical, retained decodes
  skip-src-gone        src not on disk
  skip-no-retained     no live library copy with matching sha256 found
  skip-retained-bad    a hash-matching retained copy exists but does not decode
  skip-unhandled-ext   retained copy ext has no decode check
  skip-same-path       degenerate (evidence == src)
"""
import argparse, hashlib, json, os, sqlite3, subprocess, sys, time
from PIL import Image
import pillow_heif
pillow_heif.register_heif_opener()
import cv2

DB = '/data/nas-curator.db'
MANIFEST = '/data/dedup_107_sweep.tsv'
SUMMARY = '/data/dedup_107_sweep_summary.json'
LIB_PREFIX = '/vol1/1000/媒体库/'
IMG_EXTS = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp', '.gif',
            '.bmp', '.tif', '.tiff', '.jfif'}
VID_EXTS = {'.mov', '.mp4', '.m4v', '.3gp', '.avi', '.mkv', '.vob',
            '.wmv', '.flv'}
COLS = ['rq_id', 'src', 'evidence', 'retained_path', 'size_src',
        'mtime_src', 'sha_src', 'size_retained', 'mtime_retained',
        'sha_retained', 'action', 'detail']


def _s(x):
    return '' if x is None else str(x)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def decodes(path):
    """(ok, detail) lightweight intact check. ok None = no checker for ext.
    Images: PIL first, cv2 fallback (truncated JPEGs often still read via cv2).
    Videos: cv2 reads one frame."""
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


def build_basename_index():
    """basename -> [paths] for every regular file under the library.
    One find traversal; used to locate retained copies whose recorded
    evidence path went stale after the curator moved the file."""
    t0 = time.time()
    out = subprocess.run(['find', LIB_PREFIX, '-type', 'f', '-print0'],
                         capture_output=True, check=True)
    idx = {}
    n = 0
    for raw in out.stdout.split(b'\0'):
        if not raw:
            continue
        p = raw.decode('utf-8', 'surrogateescape')
        idx.setdefault(os.path.basename(p), []).append(p)
        n += 1
    print('basename index: %d files, %d distinct basenames (%.1fs)'
          % (n, len(idx), time.time() - t0), flush=True)
    return idx


def load_sha256_rows(db):
    return db.execute(
        "SELECT id, json_extract(payload,'$.src'), "
        "json_extract(payload,'$.evidence') FROM review_queue "
        "WHERE plan_id=107 AND status='pending' "
        "AND json_extract(payload,'$.reason')='dup:sha256' "
        "ORDER BY id").fetchall()


def analyze(limit):
    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    rows = load_sha256_rows(db)
    print('dup:sha256 pending rows: %d' % len(rows), flush=True)
    idx = build_basename_index()
    done = set()
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding='utf-8') as f:
            next(f, None)
            for line in f:
                parts = line.rstrip('\n').split('\t')
                if parts:
                    done.add(parts[0])
    print('already in manifest: %d' % len(done), flush=True)
    counts = {}
    t0 = time.time()
    n = 0
    with open(MANIFEST, 'a', encoding='utf-8') as out:
        if os.path.getsize(MANIFEST) == 0:
            out.write('\t'.join(COLS) + '\n')
        for rq_id, src, evidence in rows:
            if str(rq_id) in done:
                continue
            if limit and n >= limit:
                break
            n += 1
            retained, size_src, mtime_src, sha_src = '', '', '', ''
            size_ret, mtime_ret, sha_ret = '', '', ''
            action, detail = '', ''
            try:
                if not src or not evidence:
                    action, detail = 'skip-no-retained', 'missing src/evidence in payload'
                elif src == evidence:
                    action, detail = 'skip-same-path', 'evidence == src'
                else:
                    try:
                        st = os.stat(src)
                        size_src, mtime_src = st.st_size, st.st_mtime
                    except FileNotFoundError:
                        action, detail = 'skip-src-gone', 'not-on-disk'
                    else:
                        if not os.path.isfile(src):
                            action, detail = 'skip-src-gone', 'not-a-file'
                        else:
                            sha_src = sha256(src)
                            # candidate retained copies: recorded path first,
                            # then same-basename files under the library.
                            cands = []
                            if os.path.isfile(evidence):
                                cands.append(evidence)
                            for p in idx.get(os.path.basename(evidence), []):
                                if p != src and p != evidence and p.startswith(LIB_PREFIX):
                                    cands.append(p)
                            # de-dup candidates, keep order
                            seen = set()
                            uniq = []
                            for p in cands:
                                if p not in seen:
                                    seen.add(p)
                                    uniq.append(p)
                            best_fail = None  # (action, detail) of closest miss
                            for cand in uniq:
                                try:
                                    cst = os.stat(cand)
                                except FileNotFoundError:
                                    continue
                                if not os.path.isfile(cand):
                                    continue
                                sha_c = sha256(cand)
                                if sha_c != sha_src:
                                    best_fail = ('skip-no-retained',
                                                 'basename-candidate hash differs')
                                    continue
                                ok, det = decodes(cand)
                                if ok is True:
                                    retained = cand
                                    size_ret, mtime_ret = cst.st_size, cst.st_mtime
                                    sha_ret = sha_c
                                    action = 'delete'
                                    detail = 'retained:' + det
                                    break
                                if ok is None:
                                    best_fail = ('skip-unhandled-ext', det)
                                else:
                                    best_fail = ('skip-retained-bad', det)
                            if not action:
                                if best_fail:
                                    action, detail = best_fail
                                else:
                                    action, detail = ('skip-no-retained',
                                                      'no live library copy found')
            except Exception as e:
                action, detail = ('skip-no-retained',
                                  'analyze-error:%s:%s' % (type(e).__name__, str(e)[:80]))
            out.write('\t'.join([_s(rq_id), _s(src), _s(evidence), _s(retained),
                                 _s(size_src), _s(mtime_src), _s(sha_src),
                                 _s(size_ret), _s(mtime_ret), _s(sha_ret),
                                 _s(action), _s(detail)]) + '\n')
            out.flush()
            counts[action] = counts.get(action, 0) + 1
            if n % 100 == 0:
                print('%d/%d done (%.1fs) counts=%s'
                      % (n, len(rows), time.time() - t0, counts), flush=True)
    print('ANALYZE DONE n=%d elapsed=%.1fs counts=%s'
          % (n, time.time() - t0, counts), flush=True)
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump({'phase': 'analyze', 'n': n,
                   'elapsed': round(time.time() - t0, 1),
                   'counts': counts}, f, ensure_ascii=False, indent=2)


def apply(limit):
    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    # DB backup first (sqlite backup API: safe while the DB is live).
    stamp = time.strftime('%Y%m%d-%H%M%S')
    bak = '/data/nas-curator.db.bak-%s-pre107sweep' % stamp
    with sqlite3.connect(bak) as dst:
        db.backup(dst)
    print('db backup:', bak, flush=True)
    rows = []
    with open(MANIFEST, encoding='utf-8') as f:
        hdr = f.readline().rstrip('\n').split('\t')
        for line in f:
            parts = line.rstrip('\n').split('\t')
            if len(parts) < len(COLS):
                parts += [''] * (len(COLS) - len(parts))
            rows.append(dict(zip(COLS, parts)))
    counts = {}
    t0 = time.time()
    n = 0
    deletable = [r for r in rows if r['action'] == 'delete']
    for r in deletable:
        if limit and n >= limit:
            break
        n += 1
        src, retained = r['src'], r['retained_path']
        src_gone = False
        try:
            st = os.stat(src)
            if r['size_src'] and int(r['size_src']) != st.st_size:
                counts['skip-size-changed'] = counts.get('skip-size-changed', 0) + 1
                continue
            if r['mtime_src'] and abs(float(r['mtime_src']) - st.st_mtime) > 1:
                counts['skip-mtime-changed'] = counts.get('skip-mtime-changed', 0) + 1
                continue
        except FileNotFoundError:
            src_gone = True
        # retained copy must still be live and decodable, whether or not
        # the src is still there (if the src is already gone, the retained
        # copy is what keeps the content alive).
        try:
            rst = os.stat(retained)
            if r['size_retained'] and int(r['size_retained']) != rst.st_size:
                counts['skip-retained-changed'] = counts.get('skip-retained-changed', 0) + 1
                continue
            ok, det = decodes(retained)
            if ok is not True:
                counts['skip-retained-bad'] = counts.get('skip-retained-bad', 0) + 1
                continue
        except FileNotFoundError:
            counts['skip-retained-gone'] = counts.get('skip-retained-gone', 0) + 1
            continue
        if src_gone:
            counts['already-gone'] = counts.get('already-gone', 0) + 1
        else:
            os.remove(src)
            counts['file-deleted'] = counts.get('file-deleted', 0) + 1
        cur = db.execute(
            "UPDATE review_queue SET status='approved', "
            "decided_by='sweep-107-sha256', "
            "decided_at=strftime('%Y-%m-%dT%H:%M:%S','now') "
            "WHERE id=? AND status='pending'", (int(r['rq_id']),))
        if cur.rowcount:
            counts['review-rows-approved'] = counts.get('review-rows-approved', 0) + cur.rowcount
        db.commit()
        if n % 100 == 0:
            print('%d/%d applied (%.1fs) counts=%s'
                  % (n, len(deletable), time.time() - t0, counts), flush=True)
    print('integrity:', db.execute('PRAGMA integrity_check').fetchone()[0], flush=True)
    print('pending now:', db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE status='pending'").fetchone()[0], flush=True)
    print('plan 107 dup pending now:', db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE plan_id=107 AND status='pending' "
        "AND kind='dup'").fetchone()[0], flush=True)
    print('APPLY DONE n=%d elapsed=%.1fs counts=%s'
          % (n, time.time() - t0, counts), flush=True)
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump({'phase': 'apply', 'n': n,
                   'elapsed': round(time.time() - t0, 1),
                   'counts': counts}, f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--analyze', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()
    if args.analyze:
        analyze(args.limit)
    elif args.apply:
        apply(args.limit)
    else:
        ap.print_help()
        sys.exit(2)
