#!/usr/bin/env python3
"""Dedup batch for Baidu re-downloaded inbox files (NAS Curator, 2026-11-23).

Phase 1 --analyze: for each pending review row whose src is under
/vol1/1000/收件箱/, hash-compare against library namesakes (files table,
basename match under /vol1/1000/媒体库/), decode-check the retained copy,
and write a manifest. No deletions.
Phase 2 --apply: delete inbox files per manifest actions dup-identical /
inbox-damaged / gone (re-verifying size+mtime), and remove their pending
review rows. Manifest is the audit trail.

Actions:
  dup-identical   inbox bytes == library namesake bytes, library copy decodes
  inbox-damaged   hashes differ, a library namesake decodes, inbox does not
  diff-content    hashes differ, inbox also decodes (genuinely different)
  no-namesake     no library file with same basename
  gone            inbox file no longer on disk (already moved/handled)
  lib-bad         library namesake does not decode either (keep both)
  unhandled-ext   extension has no decode check (keep)
"""
import argparse, hashlib, json, os, sqlite3, sys, time
from PIL import Image
import pillow_heif
pillow_heif.register_heif_opener()
import cv2

DB = '/data/nas-curator.db'
MANIFEST = '/data/dedup_baidu_20261123.tsv'
SUMMARY = '/data/dedup_baidu_20261123_summary.json'
INBOX_PREFIX = '/vol1/1000/收件箱/'
IMG_EXTS = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp', '.gif', '.bmp', '.tif', '.tiff'}
VID_EXTS = {'.mov', '.mp4', '.m4v', '.3gp', '.avi', '.mkv', '.vob', '.wmv', '.flv'}
COLS = ['src', 'lib_path', 'size_inbox', 'mtime_inbox', 'sha_inbox', 'sha_lib', 'action', 'detail']


def _s(x):
    return '' if x is None else str(x)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def decodes(path):
    """(ok, detail) lightweight intact check. ok None = no checker for ext."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in IMG_EXTS:
            with Image.open(path) as im:
                im.load()
            return True, 'pil-ok'
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


def load_inbox_srcs(db):
    rows = db.execute(
        "SELECT DISTINCT json_extract(payload,'$.src') FROM review_queue "
        "WHERE status='pending' AND json_extract(payload,'$.src') LIKE ?",
        (INBOX_PREFIX + '%',)).fetchall()
    return sorted(r[0] for r in rows if r[0])


def load_lib_map(db):
    m = {}
    for (p,) in db.execute(
            "SELECT path FROM files WHERE path LIKE '/vol1/1000/媒体库/%'"):
        m.setdefault(os.path.basename(p), []).append(p)
    return m


def analyze(limit):
    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    srcs = load_inbox_srcs(db)
    libmap = load_lib_map(db)
    print('inbox srcs: %d, library basenames: %d' % (len(srcs), len(libmap)), flush=True)
    done = set()
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding='utf-8') as f:
            next(f, None)
            for line in f:
                parts = line.rstrip('\n').split('\t')
                if parts:
                    done.add(parts[0])
    print('already in manifest: %d' % len(done), flush=True)
    sha_cache = {}
    counts = {}
    t0 = time.time()
    n = 0
    with open(MANIFEST, 'a', encoding='utf-8') as out:
        if os.path.getsize(MANIFEST) == 0:
            out.write('\t'.join(COLS) + '\n')
        for src in srcs:
            if src in done:
                continue
            if limit and n >= limit:
                break
            n += 1
            lib_path, sha_in, sha_lib, action, detail = '', '', '', '', ''
            try:
                st = os.stat(src)
                size_in, mtime_in = st.st_size, st.st_mtime
            except FileNotFoundError:
                action, detail = 'gone', 'not-on-disk'
                size_in, mtime_in = '', ''
            else:
                cands = libmap.get(os.path.basename(src), [])
                if not cands:
                    action, detail = 'no-namesake', 'no-library-basename'
                else:
                    sha_in = sha256(src)
                    hit = None
                    for c in cands:
                        if c not in sha_cache:
                            try:
                                sha_cache[c] = sha256(c)
                            except OSError:
                                sha_cache[c] = None
                        if sha_cache[c] and sha_cache[c] == sha_in:
                            hit = c
                            break
                    if hit:
                        lib_path, sha_lib = hit, sha_cache[hit]
                        ok, det = decodes(hit)
                        if ok is True:
                            action, detail = 'dup-identical', det
                        elif ok is None:
                            action, detail = 'unhandled-ext', det
                        else:
                            action, detail = 'lib-bad', det
                    else:
                        # hashes differ: check decodability both sides
                        lib_ok, lib_det, lib_dec = False, '', None
                        for c in cands:
                            ok, det = decodes(c)
                            if ok is True:
                                lib_ok, lib_det, lib_dec = True, det, c
                                break
                            if lib_dec is None:
                                lib_dec, lib_det = c, det
                        in_ok, in_det = decodes(src)
                        if lib_dec is None:
                            lib_dec, lib_det = cands[0], 'no-candidate-checked'
                        if lib_ok and in_ok is False:
                            lib_path, sha_lib = lib_dec, sha_cache.get(lib_dec, '')
                            action, detail = 'inbox-damaged', 'lib:%s inbox:%s' % (lib_det, in_det)
                        elif in_ok is True:
                            lib_path, sha_lib = (lib_dec or ''), sha_cache.get(lib_dec, '') if lib_dec else ''
                            action, detail = 'diff-content', 'lib:%s inbox:%s' % (lib_det, in_det)
                        else:
                            lib_path = lib_dec or ''
                            action, detail = 'lib-bad', 'lib:%s inbox:%s' % (lib_det, in_det)
            out.write('\t'.join([_s(src), _s(lib_path), _s(size_in), _s(mtime_in),
                                 _s(sha_in), _s(sha_lib), _s(action), _s(detail)]) + '\n')
            counts[action] = counts.get(action, 0) + 1
            if n % 500 == 0:
                out.flush()
                print('%d/%d done (%.1fs) counts=%s' % (n, len(srcs), time.time() - t0, counts), flush=True)
    print('ANALYZE DONE n=%d elapsed=%.1fs counts=%s' % (n, time.time() - t0, counts), flush=True)
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump({'phase': 'analyze', 'n': n, 'elapsed': round(time.time() - t0, 1),
                   'counts': counts}, f, ensure_ascii=False, indent=2)


def apply(limit):
    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    rows = []
    with open(MANIFEST, encoding='utf-8') as f:
        hdr = f.readline().rstrip('\n').split('\t')
        for line in f:
            parts = line.rstrip('\n').split('\t')
            if len(parts) < len(COLS):
                parts += [''] * (len(COLS) - len(parts))
            rows.append(dict(zip(COLS, parts)))
    deletable = {'dup-identical', 'inbox-damaged', 'gone'}
    counts = {}
    t0 = time.time()
    n = 0
    for r in rows:
        if r['action'] not in deletable:
            continue
        if limit and n >= limit:
            break
        n += 1
        src = r['src']
        assert src.startswith(INBOX_PREFIX), src
        try:
            st = os.stat(src)
            if r['size_inbox'] and int(r['size_inbox']) != st.st_size:
                counts['skip-size-changed'] = counts.get('skip-size-changed', 0) + 1
                continue
            if r['mtime_inbox'] and abs(float(r['mtime_inbox']) - st.st_mtime) > 1:
                counts['skip-mtime-changed'] = counts.get('skip-mtime-changed', 0) + 1
                continue
            os.remove(src)
            counts['file-deleted'] = counts.get('file-deleted', 0) + 1
        except FileNotFoundError:
            counts['already-gone'] = counts.get('already-gone', 0) + 1
        cur = db.execute(
            "DELETE FROM review_queue WHERE status='pending' "
            "AND json_extract(payload,'$.src')=?", (src,))
        if cur.rowcount:
            counts['review-rows-deleted'] = counts.get('review-rows-deleted', 0) + cur.rowcount
        if n % 500 == 0:
            db.commit()
            print('%d/%d applied (%.1fs) counts=%s' % (n, len([x for x in rows if x['action'] in deletable]), time.time() - t0, counts), flush=True)
    db.commit()
    print('integrity:', db.execute('PRAGMA integrity_check').fetchone()[0], flush=True)
    print('pending now:', db.execute("SELECT COUNT(*) FROM review_queue WHERE status='pending'").fetchone()[0], flush=True)
    print('APPLY DONE n=%d elapsed=%.1fs counts=%s' % (n, time.time() - t0, counts), flush=True)
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump({'phase': 'apply', 'n': n, 'elapsed': round(time.time() - t0, 1),
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
