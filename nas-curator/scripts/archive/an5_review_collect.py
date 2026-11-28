#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""an5: collect the 275 pending review_queue rows' files into
/vol1/1000/收件箱/待复核/ (王震 2026-11-25 指令）.
- MOVE: pending rows whose src is an existing FILE -> 待复核/ (same name;
  same-name collisions get __1/__2 versioned names, both kept).
  review_queue rows stay status='pending'; only payload src/src_path is
  updated to the new location (plus prev_src + moved_to_review_at).
- SKIP (stay pending, untouched): 6 missing-source rows + 1 row pointing
  at a directory (150143, payload already says "不处理；注记").
- Plan + apply flow: DB backup first, plan row, plan_items + operations
  rows, plan status=done.
Usage: docker exec nas-curator python3 /data/an5_review_collect.py [--dry-run|--apply]
"""
import hashlib, json, os, shutil, sqlite3, sys
from collections import defaultdict
from datetime import datetime

DB = '/data/nas-curator.db'
INBOX = '/vol1/1000/收件箱'
REVIEW_DIR = os.path.join(INBOX, '待复核')
OWNER_UID, OWNER_GID = 1000, 1000

def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

def backup_db():
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    dst = f'/data/nas-curator.db.bak_an5_review_{ts}'
    src_conn = sqlite3.connect(DB)
    dst_conn = sqlite3.connect(dst)
    src_conn.backup(dst_conn)
    dst_conn.close(); src_conn.close()
    print('DB backed up ->', dst, flush=True)
    return dst

def get_src(p):
    return p.get('src') or p.get('src_path') or ''

def versioned_name(dst_dir, name):
    base, ext = os.path.splitext(name)
    cand = os.path.join(dst_dir, name)
    if not os.path.exists(cand):
        return cand, 'move'
    i = 1
    while True:
        cand = os.path.join(dst_dir, f'{base}__{i}{ext}')
        if not os.path.exists(cand):
            return cand, 'rename'
        i += 1

def main():
    dry = '--dry-run' in sys.argv
    apply = '--apply' in sys.argv
    if not dry and not apply:
        print('usage: an5_review_collect.py [--dry-run|--apply]')
        return 2
    db = sqlite3.connect(DB, timeout=60)
    db.execute('PRAGMA busy_timeout=60000')
    rows = db.execute("SELECT id, payload FROM review_queue WHERE status='pending'").fetchall()
    moves, skips = [], []
    for rid, payload in rows:
        try:
            p = json.loads(payload)
        except Exception:
            p = {}
        src = get_src(p)
        if not src:
            skips.append((rid, src, 'no-src-in-payload'))
        elif not os.path.exists(src):
            skips.append((rid, src, 'src-missing'))
        elif not os.path.isfile(src):
            skips.append((rid, src, 'src-is-dir-not-file'))
        else:
            moves.append((rid, src, p))
    print(f'pending={len(rows)} to-move={len(moves)} skips={len(skips)}', flush=True)
    for rid, src, why in skips:
        print(f'  SKIP {rid} [{why}] {src}', flush=True)
    # collisions
    by_name = defaultdict(list)
    for rid, src, p in moves:
        by_name[os.path.basename(src)].append(rid)
    coll = {n: v for n, v in by_name.items() if len(v) > 1}
    print('basename collisions:', {n: len(v) for n, v in coll.items()}, flush=True)
    if dry:
        print('DRY RUN - no changes made', flush=True)
        return 0
    # --- apply ---
    backup_db()
    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    os.makedirs(REVIEW_DIR, exist_ok=True)
    cur = db.execute("INSERT INTO plans (library, rules_version, status, summary) VALUES (?,?,?,?)",
                     ('review', 1, 'applying', f'an5 collect {len(moves)} pending files into 收件箱/待复核 (王震 2026-11-25 指令）'))
    plan_id = cur.lastrowid
    moved, renamed, failed = 0, 0, []
    for rid, src, p in moves:
        if not os.path.isfile(src):
            failed.append((rid, src, 'src-gone-at-apply'))
            continue
        final_dst, action = versioned_name(REVIEW_DIR, os.path.basename(src))
        if action == 'rename':
            renamed += 1
        try:
            file_sha = sha256(src)
            size = os.path.getsize(src)
            shutil.move(src, final_dst)
            try:
                os.chown(final_dst, OWNER_UID, OWNER_GID)
            except Exception:
                pass
            cat = (p.get('category') or '待复核')
            db.execute("INSERT INTO plan_items (plan_id, src_path, dst_path, category, action, kind, size, ext, sha256, reason, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (plan_id, src, final_dst, cat, action, 'review-collect', size, os.path.splitext(src)[1].lower(), file_sha, '集中到待复核文件夹', 'done'))
            db.execute("INSERT INTO operations (plan_id, src_path, dst_path, sha256) VALUES (?,?,?,?)",
                       (plan_id, src, final_dst, file_sha))
            # update payload: new src, keep prev_src + timestamp; status stays pending
            p['prev_src'] = src
            p['moved_to_review_at'] = now
            if 'src' in p:
                p['src'] = final_dst
            if 'src_path' in p:
                p['src_path'] = final_dst
            db.execute("UPDATE review_queue SET payload=? WHERE id=?", (json.dumps(p, ensure_ascii=False), rid))
            moved += 1
        except Exception as e:
            failed.append((rid, src, 'move-err:' + str(e)[:120]))
        if moved % 100 == 0:
            db.commit()
            print(f'  moved {moved}/{len(moves)}', flush=True)
    db.execute("UPDATE plans SET status='done' WHERE id=?", (plan_id,))
    db.commit()
    print(f'COLLECT plan {plan_id} done: moved={moved} renamed={renamed} failed={len(failed)} skips={len(skips)}', flush=True)
    for f_ in failed:
        print('  FAILED:', f_, flush=True)
    db.close()
    return 0

if __name__ == '__main__':
    sys.exit(main())
