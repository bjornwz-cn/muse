#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回滚误写的视频确认结论（只动 payload，不动 status）：
vlm_video_confirm_triage.py 因 import 切换误用 model-3（经济版）作 primary，
174 行被写进 vlm_category 且 vlm_model 为经济版——按规则经济版结论绝不能
作最终结论。本脚本删掉这 174 行的 5 个 vlm 键（vlm_category/confidence/
reason/model/source），vlm_economy_* 过筛结论保留不动，之后重跑 agent-plan 确认。
核对：回滚后 0 行的 vlm_model 含 economy。"""
import json, sqlite3

DB = '/data/nas-curator.db'
KEYS = ['vlm_category', 'vlm_confidence', 'vlm_reason', 'vlm_model', 'vlm_source']

conn = sqlite3.connect(DB, timeout=30)
conn.execute('PRAGMA busy_timeout=30000')
ids = [int(x) for x in open('/data/triage_video_confirm_rqids.txt').read().replace(',', ' ').split()]
bad = []
for rid in ids:
    row = conn.execute("SELECT payload FROM review_queue WHERE id=? AND status='pending'", (rid,)).fetchone()
    if not row:
        continue
    d = json.loads(row[0])
    if 'vlm_category' in d and 'economy' in str(d.get('vlm_model') or ''):
        bad.append(rid)
print('economy vlm_category rows to roll back: %d' % len(bad), flush=True)
for rid in bad:
    d = json.loads(conn.execute("SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()[0])
    assert 'vlm_economy_category' in d, 'economy screen conclusion missing rid=%d' % rid
    for k in KEYS:
        d.pop(k, None)
    conn.execute("UPDATE review_queue SET payload=? WHERE id=? AND status='pending'",
                 (json.dumps(d, ensure_ascii=False), rid))
conn.commit()
remain = 0
for rid in ids:
    d = json.loads(conn.execute("SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()[0])
    if 'vlm_category' in d:
        remain += 1
        print('STILL HAS vlm_category rid=%d model=%s' % (rid, d.get('vlm_model')))
print('rows still with vlm_category: %d' % remain, flush=True)
assert remain == 0
conn.close()
print('ROLLBACK DONE rolled_back=%d' % len(bad), flush=True)
