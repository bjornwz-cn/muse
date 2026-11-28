#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Independent disk verification after year moveback batch4 --apply.
Checks:
1. DB: plan #162 status, plan_items counts, operations rows for #162,
   review rows approved with decided_by year-moveback-b4*, integrity
2. map file: sample move/rename rows -- dst exists, src gone
3. map file: sample delete-src rows -- src gone, dst exists
4. sample already rows -- src==dst file exists
Read-only except DB reads."""
import os, sqlite3, random

MAP = '/data/year_moveback_batch4_map.tsv'
DB = '/data/nas-curator.db'

db = sqlite3.connect(DB)
db.execute('PRAGMA busy_timeout=30000')
print('DB integrity:', db.execute('PRAGMA integrity_check').fetchone()[0])
print('plan #162:',
      db.execute("SELECT id, status FROM plans WHERE id=162").fetchone())
print('plan #162 items:',
      db.execute("SELECT status, COUNT(*) FROM plan_items WHERE plan_id=162 GROUP BY status").fetchall())
print('operations rows for #162:',
      db.execute("SELECT COUNT(*) FROM operations WHERE plan_id=162").fetchone()[0])
print('review approved by year-moveback-b4*:',
      db.execute("SELECT COUNT(*) FROM review_queue WHERE decided_by LIKE 'year-moveback-b4%'").fetchone()[0])
print('pending now:',
      db.execute("SELECT COUNT(*) FROM review_queue WHERE status='pending'").fetchone()[0])
print('operations rows with plan 162 and rolled_back:',
      db.execute("SELECT COUNT(*) FROM operations WHERE plan_id=162 AND rolled_back_at IS NOT NULL").fetchone()[0])

rows = []
with open(MAP, encoding='utf-8') as f:
    hdr = f.readline().rstrip('\n').split('\t')
    for line in f:
        p = line.rstrip('\n').split('\t')
        if len(p) < len(hdr):
            p += [''] * (len(hdr) - len(p))
        rows.append(dict(zip(hdr, p)))
print('map rows:', len(rows))
import collections
print('map by action:', collections.Counter(r.get('action', '') for r in rows))

random.seed(20261124)
def sample(action, n):
    pool = [r for r in rows if r.get('action') == action]
    return random.sample(pool, min(n, len(pool)))

# move/rename: dst exists, src gone
bad = []
for r in sample('move', 100) + sample('rename', 50):
    src, dst = r.get('src', ''), r.get('dst', '')
    if not os.path.exists(dst):
        bad.append(('dst-missing', src, dst))
    if os.path.exists(src):
        bad.append(('src-still-there', src, dst))
print('move/rename sample problems:', len(bad))
for b in bad[:5]:
    print('  ', b)

# delete-src: src gone, dst exists
bad2 = []
for r in sample('delete-src', 100):
    src, dst = r.get('src', ''), r.get('dst', '')
    if os.path.exists(src):
        bad2.append(('src-still-there', src, dst))
    if not os.path.exists(dst):
        bad2.append(('dst-missing', src, dst))
print('delete-src sample problems:', len(bad2))
for b in bad2[:5]:
    print('  ', b)

# already: file exists (src==dst)
bad3 = []
for r in sample('already', 54):
    p = r.get('dst', '') or r.get('src', '')
    if not os.path.exists(p):
        bad3.append(('missing', p))
print('already sample problems:', len(bad3))
for b in bad3[:5]:
    print('  ', b)
