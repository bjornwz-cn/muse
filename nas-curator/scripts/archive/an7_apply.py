#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""an7: move C-class 211 files from 收件箱/待复核/ to party top-level folders per an6 list.
- Manifest: /data/an7_manifest.json (rid, filename, dest key)
- Destinations (literal per an6 list): work2 -> 工作文件/2-上海润心/, work1 -> 工作文件/1-王震/,
  work4 -> 工作文件/4-辽宁器械/, fam -> 家庭文件/, fam_wz -> 家庭文件/王震/
- Credential (139406 用户信息.csv): move only. sha256 computed for the operations record
  (hash only, same precedent as an4 credentials); content NEVER read/printed/recorded.
- Collision: identical sha -> mark approved as dup (no move); different content -> versioned __1/__2.
- .eml never renamed (no renaming applied at all in this batch; only collision versioning).
Usage: docker exec nas-curator python3 /data/an7_apply.py [--dry-run|--apply]
"""
import hashlib, json, os, shutil, sqlite3, sys
from datetime import datetime
from collections import Counter

DB = '/data/nas-curator.db'
MANIFEST = '/data/an7_manifest.json'
WORK_ROOT = '/vol1/1000/工作文件'
FAM_ROOT = '/vol1/1000/家庭文件'
OWNER_UID, OWNER_GID = 1000, 1000
DEST_DIRS = {
    'work2': os.path.join(WORK_ROOT, '2-上海润心'),
    'work1': os.path.join(WORK_ROOT, '1-王震'),
    'work4': os.path.join(WORK_ROOT, '4-辽宁器械'),
    'fam': FAM_ROOT,
    'fam_wz': os.path.join(FAM_ROOT, '王震'),
}
CRED_RID = 139406

def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

def backup_db():
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    dst = f'/data/nas-curator.db.bak_an7_cclass_{ts}'
    s = sqlite3.connect(DB); d = sqlite3.connect(dst)
    s.backup(d); d.close(); s.close()
    print('DB backed up ->', dst, flush=True)
    return dst

def versioned_name(dst_dir, name):
    base, ext = os.path.splitext(name)
    cand = os.path.join(dst_dir, name)
    if not os.path.exists(cand):
        return cand
    i = 1
    while True:
        cand = os.path.join(dst_dir, f'{base}__{i}{ext}')
        if not os.path.exists(cand):
            return cand
        i += 1

def main():
    dry = '--dry-run' in sys.argv
    apply = '--apply' in sys.argv
    if not dry and not apply:
        print('usage: an7_apply.py [--dry-run|--apply]')
        return 2
    manifest = json.load(open(MANIFEST, encoding='utf-8'))
    print(f'manifest entries: {len(manifest)}', flush=True)
    db = sqlite3.connect(DB, timeout=60)
    db.execute('PRAGMA busy_timeout=60000')
    moves = []  # (rid, src, dst, dest_key)
    skipped = []
    for m in manifest:
        rid, fname, key = m['rid'], m['filename'], m['dest']
        row = db.execute("SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()
        if not row:
            skipped.append((rid, 'no-review-row')); continue
        p = json.loads(row[0])
        status = db.execute("SELECT status FROM review_queue WHERE id=?", (rid,)).fetchone()[0]
        if status != 'pending':
            skipped.append((rid, f'status={status}')); continue
        src = p.get('src') or p.get('src_path') or ''
        if os.path.basename(src) != fname:
            skipped.append((rid, f'basename-mismatch src={src}')); continue
        if not os.path.exists(src):
            skipped.append((rid, 'src-missing')); continue
        dst = os.path.join(DEST_DIRS[key], fname)
        moves.append((rid, src, dst, key))
    print(f'verified moves={len(moves)} skipped={len(skipped)}', flush=True)
    print('by dest:', Counter(x[3] for x in moves), flush=True)
    for s in skipped:
        print('  SKIPPED:', s, flush=True)
    if dry:
        print('DRY RUN - no changes made', flush=True)
        return 0
    backup_db()
    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    cur = db.execute("INSERT INTO plans (library, rules_version, status, summary) VALUES (?,?,?,?)",
                     ('docs', 1, 'applying', f'an7 C-class move {len(moves)} files per an6 list (2026-11-25 user order; credential 139406 to 家庭文件/王震/)'))
    plan_id = cur.lastrowid
    moved, renamed, dup_skip, failed = 0, 0, 0, []
    for rid, src, dst, key in moves:
        if not os.path.exists(src):
            failed.append((rid, src, 'src-missing-at-apply')); continue
        dst_dir = os.path.dirname(dst)
        os.makedirs(dst_dir, exist_ok=True)
        final_dst = dst
        action = 'move'
        if os.path.exists(dst):
            try:
                if sha256(src) == sha256(dst):
                    db.execute("UPDATE review_queue SET status='approved', decided_by='an7-c-dup', decided_at=? WHERE id=?", (now, rid))
                    dup_skip += 1
                    continue
            except Exception:
                pass
            final_dst = versioned_name(dst_dir, os.path.basename(dst))
            action = 'rename'
            renamed += 1
        try:
            file_sha = sha256(src)  # hash only; for 139406 content is never read/printed
            size = os.path.getsize(src)
            shutil.move(src, final_dst)
            try:
                os.chown(final_dst, OWNER_UID, OWNER_GID)
            except Exception:
                pass
            db.execute("INSERT INTO plan_items (plan_id, src_path, dst_path, category, action, kind, size, ext, sha256, reason, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (plan_id, src, final_dst, 'an7-c-class', action, 'doc', size, os.path.splitext(src)[1].lower(), file_sha, 'an7-c-class-' + key, 'done'))
            db.execute("INSERT INTO operations (plan_id, src_path, dst_path, sha256) VALUES (?,?,?,?)",
                       (plan_id, src, final_dst, file_sha))
            db.execute("UPDATE review_queue SET status='approved', decided_by='an7-c-class-move', decided_at=? WHERE id=?", (now, rid))
            moved += 1
        except Exception as e:
            failed.append((rid, src, 'move-err:' + str(e)[:100]))
        if moved % 20 == 0:
            db.commit()
    db.execute("UPDATE plans SET status='done' WHERE id=?", (plan_id,))
    db.commit()
    print(f'C-CLASS plan {plan_id} done: moved={moved} renamed={renamed} dup_skip={dup_skip} failed={len(failed)}', flush=True)
    for f_ in failed:
        print('  FAILED:', f_, flush=True)
    db.close()
    return 0

if __name__ == '__main__':
    sys.exit(main())
