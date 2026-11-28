#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check batch4 doc-164 payload fields + 工作文件 structure (read-only)."""
import sqlite3, json, os
from collections import Counter

c = sqlite3.connect('/data/nas-curator.db')
rows = c.execute(
    "SELECT payload FROM review_queue WHERE "
    "json_extract(payload,'$.batch')='batch4' AND "
    "json_extract(payload,'$.doc_party') IN "
    "('1-王震','2-上海润心','3-北京供应链','4-辽宁器械')").fetchall()
print('doc164 count:', len(rows))
print('subdir dist:', Counter(json.loads(p).get('doc_subdir') for (p,) in rows))
print('category dist:', Counter(json.loads(p).get('category') for (p,) in rows))
print('confidence min/max:', min(float(json.loads(p).get('confidence') or 0) for (p,) in rows),
      max(float(json.loads(p).get('confidence') or 0) for (p,) in rows))
for (p,) in rows[:3]:
    d = json.loads(p)
    print({k: d.get(k) for k in ('src', 'category', 'confidence', 'doc_party',
                                 'doc_subdir', 'vlm_category', 'vlm_confidence')})
print('src prefixes:', Counter(
    (json.loads(p).get('src') or '').split('/')[3] for (p,) in rows))
print('workfile top:', sorted(os.listdir('/vol1/1000/工作文件/')))
for party in ('1-王震', '2-上海润心', '3-北京供应链', '4-辽宁器械'):
    p = '/vol1/1000/工作文件/' + party
    print(party, 'exists:', os.path.isdir(p),
          'subdirs:', sorted(os.listdir(p))[:18] if os.path.isdir(p) else [])
