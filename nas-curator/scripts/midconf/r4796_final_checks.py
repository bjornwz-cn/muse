#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796 收尾汇总（容器内，只读）：视频 177 行最终 band、其他 188 行状态、
review 计数、integrity。"""
import csv, json, sqlite3
from collections import Counter

DB = '/data/nas-curator.db'
REAL = {'人物合影', '儿童成长', '自拍', '旅行风景', '城市与建筑', '日常生活',
        '美食与咖啡', '宠物与动物', '工作与活动', '屏幕截图', '文档与票据',
        '梗图与表情', '聚会与夜生活', '运动与活动', '屏幕录制'}


def final_of(d):
    try:
        conf = float(d.get('confidence') or 0)
    except Exception:
        conf = 0.0
    vcat = d.get('vlm_category')
    try:
        vconf = float(d.get('vlm_confidence') or 0)
    except Exception:
        vconf = 0.0
    if vcat and 'economy' in str(d.get('vlm_model') or ''):
        vcat, vconf = None, 0.0
    if vcat and vconf >= 0.72:
        return vcat, vconf, 'vlm'
    if d.get('category') in REAL:
        return d['category'], conf, 'siglip'
    return None, None, 'none'


def band(c):
    if c is None:
        return 'none'
    return 'ge85' if c >= 0.85 else '72-85' if c >= 0.72 else '35-72' if c >= 0.35 else 'lt35'


def main():
    db = sqlite3.connect(DB)
    print('integrity:', db.execute('PRAGMA integrity_check').fetchone()[0])
    print('review:', dict(db.execute(
        'SELECT status, COUNT(*) FROM review_queue GROUP BY status').fetchall()))
    print('max plan:', db.execute('SELECT MAX(id) FROM plans').fetchone()[0])
    for path, name in (('/data/r4796_videos.tsv', 'videos177'),
                       ('/data/r4796_others.tsv', 'others188')):
        ids = []
        with open(path, encoding='utf-8') as f:
            for r in csv.DictReader(f, delimiter='\t'):
                ids.append(int(r['rq_id']))
        st = Counter()
        bands = Counter()
        for rid in ids:
            row = db.execute("SELECT status, payload FROM review_queue WHERE id=?", (rid,)).fetchone()
            if not row:
                st['gone'] += 1
                continue
            st[row[0]] += 1
            if row[0] == 'pending':
                _c, cf, _s = final_of(json.loads(row[1]))
                bands[band(cf)] += 1
        print('%s status=%s pending-bands=%s' % (name, dict(st), dict(bands)))
    # rerun108 ge85 最终清单
    rerun = [int(l.split('\t')[0]) for l in open('/data/r4796_rerun.tsv').read().splitlines()[1:] if l.strip()]
    ge85 = []
    for rid in rerun:
        row = db.execute("SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()
        d = json.loads(row[0])
        _c, cf, _s = final_of(d)
        if cf and cf >= 0.85:
            ge85.append((rid, d.get('vlm_category'), cf, d.get('vlm_model')))
    print('rerun108 final ge85: %d %s' % (len(ge85), sorted(ge85)))


if __name__ == '__main__':
    main()
