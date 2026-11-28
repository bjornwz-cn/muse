#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796 livp 汇总（容器内，只读）：47 行最终 band 与 >=0.85 清单。"""
import csv, json, sqlite3
from collections import Counter

DB = '/data/nas-curator.db'
REAL = {'人物合影', '儿童成长', '自拍', '旅行风景', '城市与建筑', '日常生活',
        '美食与咖啡', '宠物与动物', '工作与活动', '屏幕截图', '文档与票据',
        '梗图与表情', '聚会与夜生活', '运动与活动', '屏幕录制'}


def main():
    db = sqlite3.connect(DB)
    ids = []
    with open('/data/r4796_livp_vlm.tsv', encoding='utf-8') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            ids.append(int(r['rq_id']))
    bands = Counter()
    cats = Counter()
    ge85 = []
    for rid in ids:
        d = json.loads(db.execute(
            "SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()[0])
        cat, conf = d.get('vlm_category'), float(d.get('vlm_confidence') or 0)
        if cat and 'economy' not in str(d.get('vlm_model') or '') and conf >= 0.72:
            final_cat, final_conf = cat, conf
        elif d.get('category') in REAL:
            final_cat, final_conf = d['category'], float(d.get('confidence') or 0)
        else:
            final_cat, final_conf = None, None
        b = ('ge85' if final_conf and final_conf >= 0.85 else
             '72-85' if final_conf and final_conf >= 0.72 else
             '35-72' if final_conf and final_conf >= 0.35 else 'none')
        bands[b] += 1
        if final_cat:
            cats[final_cat] += 1
        if b == 'ge85':
            ge85.append((rid, final_cat, final_conf, d.get('vlm_model')))
    print('livp47 final bands:', dict(bands))
    print('livp47 final categories:', dict(cats))
    print('livp ge85 rows:', len(ge85))
    for r in sorted(ge85):
        print(' GE85', r)
    print('integrity:', db.execute('PRAGMA integrity_check').fetchone()[0])
    print('review:', dict(db.execute(
        'SELECT status, COUNT(*) FROM review_queue GROUP BY status').fetchall()))


if __name__ == '__main__':
    main()
