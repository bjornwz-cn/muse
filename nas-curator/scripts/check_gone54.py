#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Investigate the 54 src-gone rows in the batch4 moveback manifest.
For each skip-src-gone row: find where the file went via operations
(plan #160 single-file moves), check if it exists at that destination,
and whether that destination equals the moveback dst.
Read-only: no files moved, no DB writes."""
import json, os, sqlite3

DB = '/data/nas-curator.db'
MANIFEST = '/data/year_moveback_batch4.tsv'

db = sqlite3.connect(DB)
db.execute('PRAGMA busy_timeout=30000')
gone = []
with open(MANIFEST, encoding='utf-8') as f:
    hdr = f.readline().rstrip('\n').split('\t')
    for line in f:
        p = line.rstrip('\n').split('\t')
        if len(p) < len(hdr):
            p += [''] * (len(hdr) - len(p))
        r = dict(zip(hdr, p))
        if r.get('action') == 'skip-src-gone':
            gone.append(r)
print('skip-src-gone rows:', len(gone))
# operations lookup: src -> (plan_id, dst, sha256)
ops = {}
for plan_id, src, dst, sha in db.execute(
        'SELECT plan_id, src_path, dst_path, sha256 FROM operations'):
    ops.setdefault(src, []).append((plan_id, dst, sha))
n_in_ops = 0
n_dst_exists = 0
n_dst_eq_moveback = 0
detail = []
for r in gone:
    src = r['src']
    dst_mb = r['dst']
    hits = ops.get(src, [])
    entry = {'src': src, 'dst_moveback': dst_mb,
             'final_category': r.get('final_category'),
             'final_source': r.get('final_source'),
             'ops': hits}
    if hits:
        n_in_ops += 1
        for plan_id, dst, sha in hits:
            if os.path.exists(dst):
                n_dst_exists += 1
                entry['dst_exists'] = dst
                if os.path.abspath(dst) == os.path.abspath(dst_mb):
                    n_dst_eq_moveback += 1
                    entry['dst_eq_moveback'] = True
                break
        else:
            entry['dst_exists'] = None
    detail.append(entry)
print('in operations table:', n_in_ops)
print('dst exists on disk:', n_dst_exists)
print('dst == moveback dst:', n_dst_eq_moveback)
print('by ops plan:', {})
from collections import Counter
print(Counter(h[0] for e in detail for h in e['ops']))
for e in detail[:10]:
    print(json.dumps(e, ensure_ascii=False))
with open('/data/batch4_gone54.json', 'w', encoding='utf-8') as f:
    json.dump({'n': len(gone), 'in_ops': n_in_ops,
               'dst_exists': n_dst_exists,
               'dst_eq_moveback': n_dst_eq_moveback,
               'detail': detail}, f, ensure_ascii=False, indent=1)
print('written /data/batch4_gone54.json')
