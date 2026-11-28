#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""remaining-4796：分析 none 带与 72-85 非真实类目行（只读）。"""
import sqlite3, json, os
from collections import Counter

DB = '/data/nas-curator.db'
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
            return d['vlm_category'], vc, 'vlm'
    try:
        c = float(d.get('confidence') or 0)
    except Exception:
        c = 0.0
    return d.get('category'), c, 'sig'

db = sqlite3.connect(DB)
rows = db.execute("SELECT id, payload FROM review_queue WHERE status='pending'").fetchall()
none = []
nonreal72 = []
for rid, p in rows:
    d = json.loads(p)
    src = d.get('src', '') or ''
    if os.path.splitext(src)[1].lower() not in PHOTO:
        continue
    fc, fconf, fs = final_of(d)
    if fconf < 0.35:
        none.append((rid, src, d.get('kind'), fc, round(fconf, 2),
                     d.get('vlm_category'), d.get('vlm_confidence'), d.get('vlm_model'),
                     d.get('category'), d.get('confidence')))
    if 0.72 <= fconf < 0.85 and fc not in REAL:
        nonreal72.append((rid, src, fc, fconf, fs, d.get('vlm_category'),
                          d.get('vlm_confidence'), d.get('category'), d.get('confidence')))
print('none-band rows:', len(none))
print('none final_cat dist:', json.dumps(Counter([str(n[3]) for n in none]), ensure_ascii=False))
print('none vlm_cat present:', sum(1 for n in none if n[5]),
      ' sig_cat present:', sum(1 for n in none if n[8]))
print('none sig_conf==0:', sum(1 for n in none if not n[9]),
      ' vlm_conf==0:', sum(1 for n in none if not n[6]))
print('--- nonreal 72-85 rows (%d):' % len(nonreal72))
for r in nonreal72:
    print(r)
print('--- none-band sample (first 20):')
for n in none[:20]:
    print(n)
print('--- none sig_conf buckets:', json.dumps(
    Counter([round(float(n[9] or 0), 1) for n in none]), ensure_ascii=False))
print('--- none vlm_conf buckets:', json.dumps(
    Counter([round(float(n[6] or 0), 1) if n[6] is not None else 'None' for n in none]),
    ensure_ascii=False))
# 严格无法定类目：siglip cat 为 无法判断/空 且 vlm cat 为空或 vlm conf<0.35
strict = [n for n in none
          if (n[8] in (None, '', '无法判断'))
          and (n[5] is None or float(n[6] or 0) < 0.35)]
print('strict-undet (sig 无法判断/空 + vlm 空或<0.35):', len(strict))
for n in strict[:30]:
    print(n)
# 文件名线索检查：文件名是否含中文/有意义词（非纯数字/随机串）
import re
def has_filename_clue(src):
    base = os.path.basename(src)
    name = os.path.splitext(base)[0]
    # 去掉常见无意义后缀
    if re.search(r'[\u4e00-\u9fff]{2,}', name):
        return True
    if re.search(r'[A-Za-z]{4,}', name):
        return True
    return False
print('strict with filename clue:', sum(1 for n in strict if has_filename_clue(n[1])))
print('strict without filename clue:', sum(1 for n in strict if not has_filename_clue(n[1])))
# 全部 none 带的文件名线索分布
print('none with filename clue:', sum(1 for n in none if has_filename_clue(n[1])),
      ' without:', sum(1 for n in none if not has_filename_clue(n[1])))
