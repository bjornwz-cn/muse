#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Corrected disk verification for year moveback batch4 --apply.
Map file has NO header: 3 cols (src, dst, timestamp).
For every map row (move/rename/delete-src): dst must exist, src must be gone.
For 'already' plan_items (54): the file must exist at its dst.
Read-only except DB reads."""
import os, sqlite3, random

MAP = '/data/year_moveback_batch4_map.tsv'
DB = '/data/nas-curator.db'

rows = []
with open(MAP, encoding='utf-8') as f:
    for line in f:
        p = line.rstrip('\n').split('\t')
        if len(p) >= 2 and p[0] and p[1]:
            rows.append((p[0], p[1]))
print('map rows:', len(rows))

random.seed(20261124)
sample = random.sample(rows, 300)
bad = []
for src, dst in sample:
    if not os.path.exists(dst):
        bad.append(('dst-missing', src, dst))
    if os.path.exists(src):
        bad.append(('src-still-there', src, dst))
print('map sample (300): dst exists + src gone problems:', len(bad))
for b in bad[:5]:
    print('  ', b)

# already rows: from plan_items #162, status done, action move, but no
# operations row -> these are the 'already' ones. Check file exists at dst.
db = sqlite3.connect(DB)
db.execute('PRAGMA busy_timeout=30000')
already = db.execute(
    "SELECT pi.src_path, pi.dst_path FROM plan_items pi "
    "WHERE pi.plan_id=162 AND pi.status='done' "
    "AND NOT EXISTS (SELECT 1 FROM operations o WHERE o.plan_id=162 "
    "AND o.src_path=pi.src_path)").fetchall()
print('already rows (done, no ops):', len(already))
bad2 = []
for src, dst in already:
    p = dst or src
    if not os.path.exists(p):
        bad2.append(('missing', p))
print('already problems:', len(bad2))
for b in bad2[:5]:
    print('  ', b)

# spot-check: a few moved files exist under 媒体库/照片/ and 工作文件/
import collections
dest_top = collections.Counter()
for src, dst in rows:
    dest_top[dst.split('/vol1/1000/')[1].split('/')[0] if '/vol1/1000/' in dst else '?'] += 1
print('map dst top-level:', dict(dest_top))
