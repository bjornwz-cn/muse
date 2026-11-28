#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""an5: delete empty folders under /vol1/1000/收件箱/ after collecting the
pending files into 待复核/ (王震 2026-11-25 指令）.
- Computes the full closure bottom-up: a dir is deletable when, after
  removing all deletable subdirs, it has no remaining entries (files
  block; skipped/system dirs block).
- NEVER deletes: 收件箱 root itself, 待复核/ and its subtree, any dir
  whose name starts with '@' or '.@' (NAS system dirs), symlinked dirs.
- Plan + apply flow: DB backup first, plan row, plan_items rows
  (action='rmdir-empty'), no operations rows, plan status=done.
Usage: docker exec nas-curator python3 /data/an5_empty_cleanup.py [--dry-run|--apply]
"""
import os, sqlite3, sys
from datetime import datetime

DB = '/data/nas-curator.db'
INBOX = '/vol1/1000/收件箱'
REVIEW_DIR = os.path.join(INBOX, '待复核')

def is_system_dir(path):
    name = os.path.basename(path)
    return name.startswith('@') or name.startswith('.@')

def compute():
    """Return (deletable, skipped) where deletable is a list of dirs that
    would be empty after bottom-up deletion, deepest first."""
    deletable = []
    deletable_set = set()
    skipped = []
    for root, dirs, files in os.walk(INBOX, topdown=False):
        if root == INBOX:
            continue
        if root == REVIEW_DIR or root.startswith(REVIEW_DIR + os.sep):
            continue
        if os.path.islink(root):
            skipped.append((root, 'symlink'))
            continue
        if is_system_dir(root):
            skipped.append((root, 'system-dir'))
            continue
        try:
            entries = os.listdir(root)
        except Exception as e:
            skipped.append((root, 'listdir-err:' + str(e)[:80]))
            continue
        blocking = [e for e in entries if os.path.join(root, e) not in deletable_set]
        if not blocking:
            deletable.append(root)
            deletable_set.add(root)
    # deepest first for deletion order
    deletable.sort(key=lambda p: p.count(os.sep), reverse=True)
    return deletable, skipped

def backup_db():
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    dst = f'/data/nas-curator.db.bak_an5_empty_{ts}'
    src_conn = sqlite3.connect(DB)
    dst_conn = sqlite3.connect(dst)
    src_conn.backup(dst_conn)
    dst_conn.close(); src_conn.close()
    print('DB backed up ->', dst, flush=True)
    return dst

def main():
    dry = '--dry-run' in sys.argv
    apply = '--apply' in sys.argv
    if not dry and not apply:
        print('usage: an5_empty_cleanup.py [--dry-run|--apply]')
        return 2
    deletable, skipped = compute()
    print(f'empty dirs to delete: {len(deletable)}', flush=True)
    for d in deletable[:20]:
        print('  DEL:', d, flush=True)
    if len(deletable) > 20:
        print(f'  ... and {len(deletable) - 20} more (full list in plan_items after apply)', flush=True)
    print(f'skipped (kept): {len(skipped)}', flush=True)
    for d, why in skipped:
        print(f'  KEEP [{why}]: {d}', flush=True)
    if dry:
        print('DRY RUN - no changes made', flush=True)
        return 0
    # --- apply ---
    backup_db()
    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    db = sqlite3.connect(DB, timeout=60)
    db.execute('PRAGMA busy_timeout=60000')
    cur = db.execute("INSERT INTO plans (library, rules_version, status, summary) VALUES (?,?,?,?)",
                     ('review', 1, 'applying', f'an5 清理收件箱空文件夹 {len(deletable)} 个 (王震 2026-11-25 指令）'))
    plan_id = cur.lastrowid
    deleted, failed = 0, []
    for d in deletable:
        # defensive re-check: truly empty right before rmdir
        try:
            if os.path.islink(d) or is_system_dir(d):
                failed.append((d, 'became-protected'))
                continue
            if os.listdir(d):
                failed.append((d, 'not-empty-at-delete'))
                continue
            os.rmdir(d)
            db.execute("INSERT INTO plan_items (plan_id, src_path, dst_path, category, action, kind, size, ext, reason, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (plan_id, d, '', '', 'rmdir-empty', 'empty-dir', 0, '', '空文件夹清理', 'done'))
            deleted += 1
        except Exception as e:
            failed.append((d, 'rmdir-err:' + str(e)[:120]))
        if deleted % 500 == 0:
            db.commit()
    db.execute("UPDATE plans SET status='done' WHERE id=?", (plan_id,))
    db.commit()
    print(f'CLEANUP plan {plan_id} done: deleted={deleted} failed={len(failed)}', flush=True)
    for f_ in failed:
        print('  FAILED:', f_, flush=True)
    db.close()
    return 0

if __name__ == '__main__':
    sys.exit(main())
