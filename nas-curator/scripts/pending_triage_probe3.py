#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤3 步骤3前置探针（只读）：文档 doc_model、空文本扩展名分布、
去重 52 个的 kind 分布、剩余待确认行现状、视频帧提取可行性抽查。"""
import csv, json, sqlite3, os
from collections import Counter

DB = '/data/nas-curator.db'
conn = sqlite3.connect(DB, timeout=30)
rows = conn.execute("select id, kind, payload from review_queue where status='pending'").fetchall()
conn.close()
none_ids = set()
with open('/data/pending_triage_none.tsv', encoding='utf-8') as f:
    for r in csv.DictReader(f, delimiter='\t'):
        none_ids.add(int(r['rq_id']))
dedup_del = set()
with open('/data/pending_triage_dedup2.tsv', encoding='utf-8') as f:
    for r in csv.DictReader(f, delimiter='\t'):
        if r['action'] == 'delete':
            dedup_del.add(int(r['rq_id']))
print('dedup deleted rq:', len(dedup_del))

# 文档 doc_model
doc_model = Counter()
doc_reason = Counter()
empty_ext = Counter()
for rid, kind, pl in rows:
    if rid not in none_ids:
        continue
    try:
        p = json.loads(pl)
    except Exception:
        continue
    src = p.get('src', '')
    if (p.get('doc_party') == '无法判断'):
        doc_model[p.get('doc_model', '') or '(empty)'] += 1
    if (p.get('reason', '') or '').startswith('empty-text'):
        empty_ext[os.path.splitext(src)[1].lower()] += 1
print('doc 无法判断 doc_model:', dict(doc_model))
print('doc empty-text ext:', dict(empty_ext))

# 剩余 none 行（扣除已删 damaged 368 + dedup 52）
verify = {}
with open('/data/pending_triage_verify.tsv', encoding='utf-8') as f:
    for r in csv.DictReader(f, delimiter='\t'):
        verify[r['rq_id']] = r
dead_none = {rid for rid, r in verify.items() if r['verdict'] == 'dead' and r['scope'] == 'none'}
print('dead none-scope:', len(dead_none))
rem = []
with open('/data/pending_triage_none.tsv', encoding='utf-8') as f:
    for r in csv.DictReader(f, delimiter='\t'):
        if int(r['rq_id']) in dead_none or int(r['rq_id']) in dedup_del:
            continue
        rem.append(r)
print('remaining none rows:', len(rem))
print('remaining by kind x bucket:', dict(Counter((r['skind'], r['bucket']) for r in rem)))
print('remaining photos verdict:', Counter(verify.get(r['rq_id'], {}).get('verdict', 'not-in-verify') for r in rem if r['skind'] == '照片'))
print('remaining videos verdict:', Counter(verify.get(r['rq_id'], {}).get('verdict', 'not-in-verify') for r in rem if r['skind'] == '视频'))
print('remaining docs:', sum(1 for r in rem if r['skind'] == '文档'))
# dedup 52 kind
with open('/data/pending_triage_none.tsv', encoding='utf-8') as f:
    nm = {r['rq_id']: r for r in csv.DictReader(f, delimiter='\t')}
print('dedup52 by skind:', Counter(nm[str(k)]['skind'] for k in dedup_del if str(k) in nm))
# frame_failed remaining (not dead, not missing)
ff = []
with open('/data/midconf_video_frame_failed.tsv', encoding='utf-8') as f:
    for r in csv.DictReader(f, delimiter='\t'):
        ff.append(r)
ff_rem = [r for r in ff if verify.get(r['rq_id'], {}).get('verdict') == 'cv2-ok']
print('frame_failed cv2-ok remaining:', len(ff_rem))
# 凭据 3 行状态
for rid in (138941, 138943, 139432):
    st = [s for (i, k, s) in [(a, b, c) for a, b, c in rows] if i == rid]
    print('cred', rid, 'still pending:', bool(st))
