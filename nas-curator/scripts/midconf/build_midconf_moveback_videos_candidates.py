#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 2b step 0: build the 119-video candidate list + snapshot (READ-ONLY on DB).
Scope = /data/midconf_moveback_candidates.tsv rows with kind=视频 and
final_category in the 9 photo categories that have no 视频/ directory.
Per-row DB verification: still pending, vlm_source='vlm-midconf-frame',
vlm_confidence>=0.85, final category per analyze_pending rule (>=0.85),
src in inbox AND on disk, no 20200614 / credential / encrypted rows.
Writes:
  /data/midconf_moveback_videos_candidates.tsv (rq_id,src,final_category,final_confidence,batch,kind)
  /data/midconf_moveback_videos_snapshot.tsv (src,size,mtime,kind)
No DB writes, no file moves/deletes.
"""
import json, os, sqlite3
from collections import Counter

DB = "/data/nas-curator.db"
CANDS = "/data/midconf_moveback_candidates.tsv"
OUT = "/data/midconf_moveback_videos_candidates.tsv"
SNAP = "/data/midconf_moveback_videos_snapshot.tsv"

NINE = {'文档与票据', '自拍', '城市与建筑', '人物合影', '工作与活动',
        '宠物与动物', '美食与咖啡', '梗图与表情', '屏幕截图'}
REAL_CATS = {'人物合影', '儿童成长', '自拍', '旅行风景', '城市与建筑', '日常生活',
             '美食与咖啡', '宠物与动物', '工作与活动', '屏幕截图', '文档与票据',
             '梗图与表情', '聚会与夜生活', '运动与活动', '屏幕录制'}


def final_of(p):
    try:
        conf = float(p.get('confidence')) if p.get('confidence') is not None else 0.0
    except Exception:
        conf = 0.0
    vcat = p.get('vlm_category')
    try:
        vconf = float(p.get('vlm_confidence')) if p.get('vlm_confidence') is not None else 0.0
    except Exception:
        vconf = 0.0
    vmodel = p.get('vlm_model') or ''
    if vcat and 'economy' in str(vmodel):
        vcat, vconf = None, 0.0
    if vcat and vconf >= 0.72:
        return vcat, vconf, 'vlm'
    if p.get('category') in REAL_CATS:
        return p['category'], conf, 'siglip'
    return None, None, 'undecided'


def main():
    import csv
    base = list(csv.DictReader(open(CANDS, encoding='utf-8'), delimiter='\t'))
    sel = [r for r in base if r['kind'] == '视频' and r['final_category'] in NINE]
    print('base candidates: %d, selected videos-in-9-cats: %d (expected 119)' % (len(base), len(sel)))
    conn = sqlite3.connect('file:%s?mode=ro' % DB, uri=True, timeout=30)
    integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
    kept, problems, forbidden = [], [], []
    by_cat, by_source = Counter(), Counter()
    for r in sel:
        rid = int(r['rq_id'])
        row = conn.execute('SELECT status, payload FROM review_queue WHERE id=?', (rid,)).fetchone()
        if not row:
            problems.append((rid, 'db-missing'))
            continue
        st, payload = row
        if st != 'pending':
            problems.append((rid, 'not-pending:%s' % st))
            continue
        p = json.loads(payload) if payload else {}
        src = p.get('src', '')
        if '20200614' in src or '凭据' in src or 'credential' in src.lower():
            forbidden.append((rid, src, 'name'))
        if p.get('encrypted') or p.get('is_encrypted'):
            forbidden.append((rid, src, 'encrypted-flag'))
        if rid == 133420:
            problems.append((rid, 'rq-133420-must-not-move'))
            continue
        if p.get('vlm_source') != 'vlm-midconf-frame':
            problems.append((rid, 'vlm_source=%s' % p.get('vlm_source')))
            continue
        fcat, fconf, fsrc = final_of(p)
        if fcat != r['final_category'] or fconf is None or fconf < 0.85:
            problems.append((rid, 'final-mismatch db=%s/%.2f vs list=%s/%s' % (fcat, fconf or 0, r['final_category'], r['final_confidence'])))
            continue
        if not src.startswith('/vol1/1000/收件箱/'):
            problems.append((rid, 'not-inbox:%s' % src))
            continue
        if not os.path.exists(src):
            problems.append((rid, 'missing-on-disk:%s' % src))
            continue
        if src != r['src']:
            problems.append((rid, 'src-mismatch db vs list'))
            continue
        kept.append((rid, src, fcat, fconf, r.get('batch', ''), '视频'))
        by_cat[fcat] += 1
        by_source[fsrc] += 1
    conn.close()
    kept.sort(key=lambda t: t[0])
    print('integrity: %s' % integrity)
    print('kept: %d (expected 119)' % len(kept))
    print('problems: %d %s' % (len(problems), problems[:10]))
    print('forbidden(20200614/credential/encrypted): %d %s' % (len(forbidden), forbidden[:5]))
    print('by category: %s' % json.dumps(dict(by_cat), ensure_ascii=False, sort_keys=True))
    print('by source: %s (expected vlm 119)' % dict(by_source))
    print('min final_conf: %s' % (min(k[3] for k in kept) if kept else None))
    assert len(kept) == 119, 'kept != 119'
    assert not problems, problems[:5]
    assert not forbidden, forbidden[:5]
    assert by_source == {'vlm': 119}, by_source
    with open(OUT, 'w', encoding='utf-8') as fh:
        fh.write('rq_id\tsrc\tfinal_category\tfinal_confidence\tbatch\tkind\n')
        for rid, src, fcat, fconf, batch, kind in kept:
            fh.write('%d\t%s\t%s\t%s\t%s\t%s\n' % (rid, src, fcat, fconf, batch, kind))
    with open(SNAP, 'w', encoding='utf-8') as fh:
        for rid, src, fcat, fconf, batch, kind in kept:
            st = os.stat(src)
            fh.write('%s\t%d\t%r\t%s\n' % (src, st.st_size, st.st_mtime, kind))
    print('wrote %s (%d rows) and %s (%d rows)' % (OUT, len(kept), SNAP, len(kept)))
    print('STEP0 PASS')


if __name__ == '__main__':
    main()
