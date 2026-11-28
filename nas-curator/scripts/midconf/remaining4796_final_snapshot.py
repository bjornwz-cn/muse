#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""remaining-4796 收尾快照（只读，不改任何清单文件）：
当前 pending 照片最终 band 分布、1,413 行状态、加密 25 行状态、排除项状态。
用法：docker exec nas-curator python3 /data/remaining4796_final_snapshot.py
"""
import sqlite3, json, os

DB = '/data/nas-curator.db'
IDS = '/data/remaining4796_72_85_ids.txt'
PHOTO = {'.jpg', '.jpeg', '.heic', '.heif', '.png', '.gif', '.tiff',
         '.cr2', '.webp', '.nef', '.bmp'}
REAL = {"人物合影", "自拍", "儿童成长", "旅行风景", "城市与建筑", "美食与咖啡",
        "日常生活", "宠物与动物", "工作与活动", "屏幕截图", "文档与票据", "梗图与表情"}

def final_of(d):
    vm = str(d.get('vlm_model') or '')
    if 'vlm_category' in d and 'economy' not in vm.lower():
        try:
            vc = float(d.get('vlm_confidence') or 0)
        except Exception:
            vc = 0.0
        if vc >= 0.72:
            return d['vlm_category'], vc, 'vlm'
    try:
        c = float(d.get('confidence') or 0)
    except Exception:
        c = 0.0
    return d.get('category'), c, 'siglip'

db = sqlite3.connect(DB)
print('integrity:', db.execute('PRAGMA integrity_check').fetchone()[0])
print('counts:', dict(db.execute('SELECT status, COUNT(*) FROM review_queue GROUP BY status').fetchall()))
print('max plan:', db.execute('SELECT MAX(id) FROM plans').fetchone()[0])
rows = db.execute("SELECT id, payload FROM review_queue WHERE status='pending'").fetchall()
band = {'ge85': 0, 'b72_85': 0, 'b35_72': 0, 'none': 0}
n_photo = 0
ge85_ids = []
for rid, p in rows:
    d = json.loads(p)
    src = d.get('src', '') or ''
    if os.path.splitext(src)[1].lower() not in PHOTO:
        continue
    n_photo += 1
    fc, fconf, fsrc = final_of(d)
    if fconf >= 0.85:
        band['ge85'] += 1
        ge85_ids.append(rid)
    elif fconf >= 0.72:
        band['b72_85'] += 1
    elif fconf >= 0.35:
        band['b35_72'] += 1
    else:
        band['none'] += 1
print('pending photos now: %d band %s' % (n_photo, json.dumps(band, ensure_ascii=False)))
print('pending-photo ge85 ids (%d): %s' % (len(ge85_ids), json.dumps(ge85_ids)))
ids = [int(x) for x in open(IDS, encoding='utf-8').read().split() if x.strip()]
st = {}
for rid in ids:
    r = db.execute('SELECT status FROM review_queue WHERE id=?', (rid,)).fetchone()
    st[r[0]] = st.get(r[0], 0) + 1
print('my 1413 rows status:', st)
enc_ok = 0
for rid in range(138768, 138793):
    r = db.execute('SELECT status, decided_by, payload FROM review_queue WHERE id=?', (rid,)).fetchone()
    d = json.loads(r[2])
    if r[0] == 'approved' and r[1] == 'remaining-delete-encrypted' and not os.path.exists(d.get('src', '')):
        enc_ok += 1
print('enc25 approved+gone:', enc_ok)
with open('/data/remaining4796_final_snapshot.json', 'w', encoding='utf-8') as f:
    json.dump({'pending_photos': n_photo, 'band': band, 'ge85_ids': ge85_ids,
               'my1413_status': st, 'enc25_ok': enc_ok}, f, ensure_ascii=False, indent=2)
print('wrote /data/remaining4796_final_snapshot.json')
