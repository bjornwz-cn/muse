#!/usr/bin/env python3
import sqlite3, json, csv, os
from collections import Counter, defaultdict

c = sqlite3.connect('/data/nas-curator.db')

# 1. none set srcs from TSV
none = {}
with open('/data/pending_triage_none.tsv', encoding='utf-8') as f:
    rd = csv.DictReader(f, delimiter='\t')
    for r in rd:
        none[r['src']] = r

# 2. batch4 verify manifest overlap
b4 = {}
with open('/data/batch4_delete_verify.tsv', encoding='utf-8') as f:
    rd = csv.DictReader(f, delimiter='\t')
    for r in rd:
        b4[r['src']] = r
ov = {s: b4[s] for s in none if s in b4}
print('none set in batch4 manifest:', len(ov))
print('  by action:', Counter(v['action'] for v in ov.values()))
print('  by bucket of overlap:', Counter(none[s]['bucket'] for s in ov))
print('  batch4 manifest total:', len(b4))

# 3. files table coverage
frows = {r[0]: (r[1], r[2]) for r in c.execute("select path, sha256, size from files")}
print('files rows:', len(frows))
cov = [s for s in none if s in frows]
print('none set covered by files table:', len(cov), 'of', len(none))
print('  by kind:', Counter(none[s]['skind'] for s in cov))
print('  by bucket:', Counter(none[s]['bucket'] for s in cov))
# sha256 match check: size in files vs tsv size
mism = sum(1 for s in cov if str(frows[s][1]) != str(none[s]['size']))
print('  size mismatch vs files table:', mism)

# 4. dup analysis: for dup-bucket rows, same-sha256 groups in files table
by_hash = defaultdict(list)
for path, (sha, size) in frows.items():
    if sha:
        by_hash[sha].append(path)
dup_rows = [r for r in none.values() if r['bucket'] == '重复(dup)']
print('dup rows:', len(dup_rows))
grp = Counter()
examples = []
for r in dup_rows:
    sha = frows.get(r['src'], (None, None))[0]
    if not sha:
        grp['no-sha-in-files'] += 1
        continue
    peers = by_hash.get(sha, [])
    n_inbox = sum(1 for p in peers if p.startswith('/vol1/1000/收件箱/'))
    n_lib = len(peers) - n_inbox
    grp['peers=%d inbox=%d lib=%d' % (len(peers), n_inbox, n_lib)] += 1
    if len(examples) < 8 and len(peers) > 1:
        examples.append((r['rq_id'], r['src'], peers))
print('dup groups:', dict(grp))
for e in examples:
    print('  EX', e[0], e[1])
    for p in e[2]:
        print('     peer:', p)

# 5. same-sha groups among none-set media rows themselves (inbox-internal dups)
inbox_hash = defaultdict(list)
for s, r in none.items():
    sha = frows.get(s, (None, None))[0]
    if sha:
        inbox_hash[sha].append(s)
multi = {h: v for h, v in inbox_hash.items() if len(v) > 1}
print('none-set internal same-sha groups:', len(multi), 'files involved:', sum(len(v) for v in multi.values()))

# 6. frame-failed 171: files-table sha + band
ff = []
with open('/data/midconf_video_frame_failed.tsv', encoding='utf-8') as f:
    rd = csv.DictReader(f, delimiter='\t')
    for r in rd:
        ff.append(r)
print('frame_failed rows:', len(ff), 'cols:', list(ff[0].keys()) if ff else None)
ff_in_files = sum(1 for r in ff if r['src'] in frows)
print('frame_failed in files table:', ff_in_files)
# their review status/band
ids = [int(r['rq_id']) for r in ff]
q = "select status, count(*) from review_queue where id in (%s) group by status" % ','.join(map(str, ids))
print('frame_failed review status:', c.execute(q).fetchall())
