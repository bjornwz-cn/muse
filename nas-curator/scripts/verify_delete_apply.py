#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Independent disk verification after batch4 delete --apply.
Checks:
1. a sample of action=delete rows are gone from disk
2. a sample of action=skip-readable rows are still on disk
3. 20200614_0001.mov and the 4 credential files are still on disk
4. DB: plan #161 status, review rows approved with
   decided_by='batch4-delete-damaged', integrity
Read-only except DB reads."""
import os, sqlite3, random

MANIFEST = '/data/batch4_delete_verify.tsv'
DB = '/data/nas-curator.db'

rows = []
with open(MANIFEST, encoding='utf-8') as f:
    hdr = f.readline().rstrip('\n').split('\t')
    for line in f:
        p = line.rstrip('\n').split('\t')
        if len(p) < len(hdr):
            p += [''] * (len(hdr) - len(p))
        rows.append(dict(zip(hdr, p)))

deletes = [r['src'] for r in rows if r['action'] == 'delete']
skips = [r['src'] for r in rows if r['action'] == 'skip-readable']
random.seed(20261124)
sample_del = random.sample(deletes, 200)
sample_skip = random.sample(skips, 200)

gone = sum(1 for s in sample_del if not os.path.exists(s))
still_there_del = [s for s in sample_del if os.path.exists(s)]
present = sum(1 for s in sample_skip if os.path.exists(s))
missing_skip = [s for s in sample_skip if not os.path.exists(s)]
print('delete sample: %d/200 gone, %d still present' % (gone, len(still_there_del)))
for s in still_there_del[:5]:
    print('  STILL THERE:', s)
print('skip-readable sample: %d/200 still present, %d missing' % (present, len(missing_skip)))
for s in missing_skip[:5]:
    print('  MISSING:', s)

# 20200614 + credential files must still exist
db = sqlite3.connect(DB)
db.execute('PRAGMA busy_timeout=30000')
must_keep = []
for (p,) in db.execute(
        "SELECT json_extract(payload,'$.src') FROM review_queue "
        "WHERE json_extract(payload,'$.src') LIKE '%20200614_0001.mov' "
        "OR json_extract(payload,'$.reason')='credential'"):
    if p:
        must_keep.append(p)
print('must-keep files in DB:', len(must_keep))
for p in must_keep:
    print('  %s exists=%s' % (p, os.path.exists(p)))

print('DB integrity:', db.execute('PRAGMA integrity_check').fetchone()[0])
print('plan #161:',
      db.execute("SELECT id, status FROM plans WHERE id=161").fetchone())
print('plan #161 items:',
      db.execute("SELECT status, COUNT(*) FROM plan_items WHERE plan_id=161 GROUP BY status").fetchall())
print('review approved by batch4-delete-damaged:',
      db.execute("SELECT COUNT(*) FROM review_queue WHERE decided_by='batch4-delete-damaged'").fetchone()[0])
print('pending now:',
      db.execute("SELECT COUNT(*) FROM review_queue WHERE status='pending'").fetchone()[0])
