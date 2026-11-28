#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit batch4_delete_verify.tsv before --apply.
Checks:
1. count of action=delete == 5154
2. every delete row is under /vol1/1000/收件箱/
3. no delete row basename is 20200614_0001.mov or a credential file
4. every delete row has a decode-fail detail and sha256_disk present
   (or no-ref)
5. no delete row overlaps the moveback list
   (/data/year_moveback_batch4.tsv)
Read-only."""
import os

MANIFEST = '/data/batch4_delete_verify.tsv'
MOVEBACK = '/data/year_moveback_batch4.tsv'

rows = []
with open(MANIFEST, encoding='utf-8') as f:
    hdr = f.readline().rstrip('\n').split('\t')
    for line in f:
        p = line.rstrip('\n').split('\t')
        if len(p) < len(hdr):
            p += [''] * (len(hdr) - len(p))
        rows.append(dict(zip(hdr, p)))

deletes = [r for r in rows if r['action'] == 'delete']
print('manifest rows:', len(rows), '| delete:', len(deletes))

not_inbox = [r['src'] for r in deletes
             if not r['src'].startswith('/vol1/1000/收件箱/')]
print('delete rows NOT under 收件箱/:', len(not_inbox))
for s in not_inbox[:5]:
    print('  ', s)

CRED = {'ApiKey.txt', 'OpenClaw-CF令牌-取用后请删除.txt',
        '思源S3同步配置.txt', '火山AccessKey.txt'}
bad = [r['src'] for r in deletes
       if os.path.basename(r['src']) == '20200614_0001.mov'
       or os.path.basename(r['src']) in CRED]
print('delete rows hitting 20200614/credential:', len(bad))
for s in bad[:5]:
    print('  ', s)

no_detail = [r['src'] for r in deletes
             if not r['detail'].startswith('decode-fail:')]
print('delete rows without decode-fail detail:', len(no_detail))

no_hash = [r['src'] for r in deletes if not r['sha256_disk']]
print('delete rows without sha256_disk:', len(no_hash))

# overlap with moveback list (src is first column)
mb = set()
with open(MOVEBACK, encoding='utf-8') as f:
    f.readline()
    for line in f:
        p = line.rstrip('\n').split('\t')
        if p:
            mb.add(p[0])
overlap = [r['src'] for r in deletes if r['src'] in mb]
print('delete rows overlapping moveback list:', len(overlap))
for s in overlap[:5]:
    print('  ', s)

print('AUDIT PASS' if (len(deletes) == 5154 and not not_inbox and not bad
                       and not no_detail and not no_hash and not overlap)
      else 'AUDIT FAIL')
