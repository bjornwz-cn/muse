#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""remaining-4796 任务 3 候选逐个核实（只读）：
列出全部 pending 照片中 VLM 结论 <0.35 或缺失的行（加密 25 除外），
逐行给：rq_id、src、kind、vlm_cat/conf/model、sig_cat/conf、文件名线索判定。
文件名线索：含 >=2 个连续汉字，或含 >=4 个连续英文字母，或含日期/证件/
身份证/发票/合同等提示词；纯数字/时间戳/UUID/随机串算无线索。
同时统计：候选总数、其中有 VLM 类目名者、有文件名线索者、两者皆无者。
"""
import sqlite3, json, os, re
from collections import Counter

DB = '/data/nas-curator.db'
PHOTO = {'.jpg', '.jpeg', '.heic', '.heif', '.png', '.gif', '.tiff',
         '.cr2', '.webp', '.nef', '.bmp'}
CLUE_WORDS = ['身份证', '发票', '合同', '票据', '证件', '截图', '微信', '合影',
              '毕业', '结婚', '生日', '旅游', '风景', '美食', 'logo', 'Logo', 'LOGO']

def filename_clue(src):
    base = os.path.basename(src)
    name = os.path.splitext(base)[0]
    if re.search(r'[\u4e00-\u9fff]{2,}', name):
        return True, 'chinese'
    if any(w in name for w in CLUE_WORDS):
        return True, 'keyword'
    if re.search(r'[A-Za-z]{4,}', name):
        return True, 'alpha4+'
    if re.search(r'(19|20)\d{2}[-_/.]?\d{1,2}[-_/.]?\d{1,2}', name):
        return True, 'date'
    return False, 'none'

db = sqlite3.connect(DB)
rows = db.execute("SELECT id, payload FROM review_queue WHERE status='pending'").fetchall()
cands = []
for rid, p in rows:
    d = json.loads(p)
    src = d.get('src', '') or ''
    if os.path.splitext(src)[1].lower() not in PHOTO:
        continue
    if 138768 <= rid <= 138792:
        continue  # 加密 25，任务 2 另行处理
    try:
        vc = float(d.get('vlm_confidence') or 0) if 'vlm_category' in d else None
    except Exception:
        vc = None
    if vc is not None and vc >= 0.35:
        continue
    clue, why = filename_clue(src)
    cands.append({'rq_id': rid, 'src': src, 'kind': d.get('kind'),
                  'vlm_cat': d.get('vlm_category'), 'vlm_conf': d.get('vlm_confidence'),
                  'vlm_model': d.get('vlm_model'), 'sig_cat': d.get('category'),
                  'sig_conf': d.get('confidence'), 'clue': clue, 'clue_why': why})
print('task3 candidates (vlm missing or <0.35, excl enc25): %d' % len(cands))
print('  with vlm category name:', sum(1 for c in cands if c['vlm_cat']))
print('  with filename clue:', sum(1 for c in cands if c['clue']))
print('  neither vlm-cat nor filename clue:', sum(
    1 for c in cands if not c['vlm_cat'] and not c['clue']))
print('  vlm_conf dist:', json.dumps(Counter([str(c['vlm_conf']) for c in cands]), ensure_ascii=False))
print('  sig_cat dist:', json.dumps(Counter([str(c['sig_cat']) for c in cands]), ensure_ascii=False))
print('  kind dist:', json.dumps(Counter([str(c['kind']) for c in cands]), ensure_ascii=False))
print('--- full candidate list:')
for c in cands:
    print(json.dumps(c, ensure_ascii=False))
with open('/data/remaining4796_undet_candidates.json', 'w', encoding='utf-8') as f:
    json.dump(cands, f, ensure_ascii=False, indent=1)
print('wrote /data/remaining4796_undet_candidates.json (%d rows)' % len(cands))
