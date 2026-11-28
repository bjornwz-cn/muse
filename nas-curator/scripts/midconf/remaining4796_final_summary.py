#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""remaining-4796 任务 1 收尾汇总（只读）：对 72-85 带 1,413 行按 keep-best 后
最终结论统计 band 与 ≥0.85 清单；同时核对全 pending 照片的最终 band 分布。
用法：docker exec nas-curator python3 /data/remaining4796_final_summary.py
"""
import sqlite3, json, os
from collections import Counter

DB = '/data/nas-curator.db'
IDS = '/data/remaining4796_72_85_ids.txt'
OLD = '/data/remaining4796_72_85_vlm_old.json'
REAL = {"人物合影", "自拍", "儿童成长", "旅行风景", "城市与建筑", "美食与咖啡",
        "日常生活", "宠物与动物", "工作与活动", "屏幕截图", "文档与票据", "梗图与表情"}
PHOTO = {'.jpg', '.jpeg', '.heic', '.heif', '.png', '.gif', '.tiff',
         '.cr2', '.webp', '.nef', '.bmp'}

def final_of(d):
    vm = str(d.get('vlm_model') or '')
    if 'vlm_category' in d and 'economy' not in vm.lower():
        try:
            vc = float(d.get('vlm_confidence') or 0)
        except Exception:
            vc = 0.0
        if vc >= 0.72:
            return d['vlm_category'], vc, 'vlm:' + vm
    try:
        c = float(d.get('confidence') or 0)
    except Exception:
        c = 0.0
    return d.get('category'), c, 'siglip'

db = sqlite3.connect(DB)
ids = [int(x) for x in open(IDS, encoding='utf-8').read().split() if x.strip()]
old = json.load(open(OLD, encoding='utf-8'))
band = Counter()
ge85 = []
new_ge85 = []
cats = Counter()
models = Counter()
for rid in ids:
    d = json.loads(db.execute(
        "SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()[0])
    fc, fconf, fsrc = final_of(d)
    if fconf >= 0.85:
        band['ge85'] += 1
        ge85.append(rid)
        oconf = float(old[str(rid)].get('vlm_confidence') or 0)
        if fconf >= 0.85 and oconf < 0.85:
            new_ge85.append(rid)
    elif fconf >= 0.72:
        band['b72_85'] += 1
    elif fconf >= 0.35:
        band['b35_72'] += 1
    else:
        band['none'] += 1
    cats[str(fc)] += 1
    models[str(d.get('vlm_model'))] += 1
print('72-85 band final:', json.dumps(dict(band), ensure_ascii=False))
print('final ge85: %d ; newly ge85 (old<0.85): %d' % (len(ge85), len(new_ge85)))
print('newly ge85 rq_ids:', json.dumps(new_ge85))
print('final cats:', json.dumps(dict(cats), ensure_ascii=False))
print('final vlm models:', json.dumps(dict(models), ensure_ascii=False))
# 全 pending 照片最终 band（与建清单时同口径）
rows = db.execute("SELECT id, payload FROM review_queue WHERE status='pending'").fetchall()
allband = Counter()
n_photo = 0
for rid, p in rows:
    d = json.loads(p)
    src = d.get('src', '') or ''
    if os.path.splitext(src)[1].lower() not in PHOTO:
        continue
    n_photo += 1
    fc, fconf, fsrc = final_of(d)
    if fconf >= 0.85:
        allband['ge85'] += 1
    elif fconf >= 0.72:
        allband['b72_85'] += 1
    elif fconf >= 0.35:
        allband['b35_72'] += 1
    else:
        allband['none'] += 1
print('all pending photos: %d final band %s' % (n_photo, json.dumps(dict(allband), ensure_ascii=False)))
print('integrity:', db.execute('PRAGMA integrity_check').fetchone()[0])
print('review counts:', dict(db.execute(
    "SELECT status, COUNT(*) FROM review_queue GROUP BY status").fetchall()))
print('max plan:', db.execute('SELECT MAX(id) FROM plans').fetchone()[0])
with open('/data/remaining4796_final_summary.json', 'w', encoding='utf-8') as f:
    json.dump({'band_72_85_final': dict(band), 'ge85_final': ge85,
               'ge85_newly': new_ge85, 'final_cats': dict(cats),
               'final_vlm_models': dict(models),
               'all_pending_photos': n_photo, 'all_band': dict(allband)},
              f, ensure_ascii=False, indent=2)
print('wrote /data/remaining4796_final_summary.json')
