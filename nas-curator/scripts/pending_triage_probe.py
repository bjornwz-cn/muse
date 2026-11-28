#!/usr/bin/env python3
import sqlite3, json
c = sqlite3.connect('/data/nas-curator.db')
print('tables:', [r[0] for r in c.execute("select name from sqlite_master where type='table'")])
for t in ['plan_items', 'plans']:
    try:
        print(t, [d[1] for d in c.execute('PRAGMA table_info(%s)' % t).fetchall()])
    except Exception as e:
        print(t, 'ERR', e)
for rid in (35293,):
    p = json.loads(c.execute("select payload from review_queue where id=?", (rid,)).fetchone()[0])
    print('DUP ROW', rid, json.dumps(p, ensure_ascii=False))
# doc_party distinct values in none set
rows = c.execute("select id, payload from review_queue where status='pending'").fetchall()
from collections import Counter
dp = Counter()
for rid, pl in rows:
    try:
        p = json.loads(pl)
    except Exception:
        continue
    if (p.get('src') or '').startswith('/vol1/1000/收件箱/'):
        dp[p.get('doc_party', '')] += 1
print('doc_party values (inbox pending):', dp.most_common(12))
# credential-ish files in none set? search src names
import re
cred = []
for rid, pl in rows:
    try:
        p = json.loads(pl)
    except Exception:
        continue
    src = p.get('src') or ''
    if src.startswith('/vol1/1000/收件箱/') and re.search(r'credential|AccessKey|accesskey|secret|密钥|凭据', src, re.I):
        cred.append((rid, src))
print('credential-name rows in inbox pending:', len(cred))
for x in cred[:10]:
    print(' ', x)
# 20200614 in inbox pending?
n14 = []
for rid, pl in rows:
    try:
        p = json.loads(pl)
    except Exception:
        continue
    if '20200614' in (p.get('src') or ''):
        n14.append((rid, p.get('src'), ))
print('20200614 rows:', n14)
# rq 133420 full payload
p = json.loads(c.execute("select payload from review_queue where id=133420").fetchone()[0])
print('RQ133420 FULL:', json.dumps(p, ensure_ascii=False))
