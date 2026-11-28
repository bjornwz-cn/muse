#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤3 移回快照（只读）：从 /data/batch5_moveback_candidates.tsv（198 行，
analyze_pending 口径最终 ≥0.85）建 /data/triage_moveback_snapshot.tsv
（src/size/mtime/kind），逐个 stat 核对在盘。"""
import csv, os

CAND = '/data/batch5_moveback_candidates.tsv'
OUT = '/data/triage_moveback_snapshot.tsv'
rows = []
with open(CAND, encoding='utf-8') as f:
    for r in csv.DictReader(f, delimiter='\t'):
        rows.append(r)
print('candidates: %d' % len(rows), flush=True)
assert len(rows) == 198, len(rows)
missing = []
with open(OUT, 'w', encoding='utf-8') as f:
    for r in rows:
        src = r['src']
        try:
            st = os.stat(src)
        except FileNotFoundError:
            missing.append(r['rq_id'])
            continue
        f.write('%s\t%d\t%d\t%s\n' % (src, st.st_size, int(st.st_mtime), r['kind']))
print('snapshot rows: %d missing: %s' % (len(rows) - len(missing), missing), flush=True)
assert not missing
print('SNAPSHOT DONE 198')
