#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核对 remaining.tsv 与 analyze_pending 口径的 none 集差异。"""
import sqlite3, json, os, importlib.util
spec = importlib.util.spec_from_file_location('ap', '/data/analyze_pending.py')
ap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ap)
c = sqlite3.connect('/data/nas-curator.db')
rows = c.execute("SELECT id, kind, payload FROM review_queue WHERE status='pending' "
                 "AND json_extract(payload,'$.src') LIKE '/vol1/1000/收件箱/%'").fetchall()
none_ids = set()
for rid, kind, payload in rows:
    d = json.loads(payload)
    if not os.path.exists(d.get('src') or ''):
        continue
    final, fconf = ap.final_of(d, kind)
    if final is None:
        none_ids.add(rid)
print('analyze none ids:', len(none_ids))
rem = set()
for line in open('/data/pending_triage_remaining.tsv').read().splitlines()[1:]:
    rem.add(int(line.split('\t')[0]))
print('remaining ids:', len(rem))
extra = sorted(rem - none_ids)
print('in remaining but NOT none:', len(extra), extra[:10])
missing = sorted(none_ids - rem)
print('none but NOT in remaining:', len(missing), missing[:10])
for rid in extra[:6]:
    r = c.execute('select kind, payload from review_queue where id=?', (rid,)).fetchone()
    d = json.loads(r[1])
    print('extra', rid, 'col-kind:', r[0], '| cat:', d.get('category'),
          '| conf:', d.get('confidence'), '| vlm:', d.get('vlm_category'),
          d.get('vlm_confidence'), d.get('vlm_model'),
          '| doc_party:', d.get('doc_party'), d.get('doc_model'),
          '| src exists:', os.path.exists(d.get('src') or ''))
for rid in missing[:6]:
    r = c.execute('select kind, payload from review_queue where id=?', (rid,)).fetchone()
    d = json.loads(r[1])
    print('missing', rid, 'col-kind:', r[0], '| cat:', d.get('category'),
          '| conf:', d.get('confidence'), '| vlm:', d.get('vlm_category'),
          d.get('vlm_confidence'), '| doc_party:', d.get('doc_party'))
