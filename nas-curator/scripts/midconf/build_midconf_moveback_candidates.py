#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Midconf moveback step 0: build candidate list (container-side, READ-ONLY).
Scope = the 2,944 midconf rows in /data/midconf_list.tsv only.
Final category/confidence per analyze_pending.py caliber:
VLM>=0.72 use VLM (economy excluded); else SigLIP real category; else undecided.
Keep only: src in inbox AND on disk AND final_conf >= 0.85.
Writes /data/midconf_moveback_candidates.tsv
(rq_id, src, final_category, final_confidence, batch, kind).
Also writes /data/midconf_moveback_snapshot.tsv
(src, size, mtime, kind) for the same 607 rows, stat-verified.
No DB writes, no file moves/deletes.
"""
import json, os, sqlite3
from collections import Counter

DB = "/data/nas-curator.db"
LIST = "/data/midconf_list.tsv"
OUT = "/data/midconf_moveback_candidates.tsv"
SNAP = "/data/midconf_moveback_snapshot.tsv"

REAL_CATS = {'人物合影', '儿童成长', '自拍', '旅行风景', '城市与建筑', '日常生活',
             '美食与咖啡', '宠物与动物', '工作与活动', '屏幕截图', '文档与票据',
             '梗图与表情', '聚会与夜生活', '运动与活动', '屏幕录制'}
IMG_EXTS = {'.jpg', '.jpeg', '.heic', '.png', '.heif', '.webp', '.gif', '.nef',
            '.tiff', '.jfif'}
VID_EXTS = {'.mov', '.mp4', '.mts', '.vob', '.avi', '.wmv'}


def kind_of(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in IMG_EXTS:
        return '照片'
    if ext in VID_EXTS:
        return '视频'
    return '其他'


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
    rows = []
    with open(LIST, encoding='utf-8') as fh:
        header = fh.readline().rstrip('\n').split('\t')
        idx = {c: i for i, c in enumerate(header)}
        for line in fh:
            p = line.rstrip('\n').split('\t')
            if p and p[0]:
                rows.append((int(p[idx['rq_id']]), p[idx['file_kind']]))
    assert len(rows) == 2944, len(rows)
    conn = sqlite3.connect('file:%s?mode=ro' % DB, uri=True, timeout=30)
    integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
    cands = []
    missing = []
    forbidden = []
    by_kind = Counter()
    by_source = Counter()
    by_cat = Counter()
    for rid, fkind in rows:
        row = conn.execute('SELECT status, payload FROM review_queue WHERE id=?', (rid,)).fetchone()
        if not row:
            missing.append(rid)
            continue
        st, payload = row
        if st != 'pending':
            print('NOT-PENDING rq=%d status=%s' % (rid, st))
            continue
        p = json.loads(payload) if payload else {}
        src = p.get('src', '')
        if '20200614' in src or '凭据' in src or 'credential' in src.lower():
            forbidden.append((rid, src, 'name'))
        if p.get('encrypted') or p.get('is_encrypted'):
            forbidden.append((rid, src, 'encrypted-flag'))
        fcat, fconf, fsrc = final_of(p)
        if fcat is None or fconf is None or fconf < 0.85:
            continue
        if not src.startswith('/vol1/1000/收件箱/'):
            print('NOT-INBOX rq=%d src=%s' % (rid, src))
            continue
        if not os.path.exists(src):
            print('MISSING-ON-DISK rq=%d src=%s' % (rid, src))
            missing.append(rid)
            continue
        kind = kind_of(src)
        if kind != fkind:
            print('KIND-MISMATCH rq=%d list=%s ext=%s src=%s' % (rid, fkind, kind, src))
        cands.append((rid, src, fcat, fconf, p.get('batch', ''), kind, fsrc))
        by_kind[kind] += 1
        by_source[fsrc] += 1
        by_cat[fcat] += 1
    conn.close()
    cands.sort(key=lambda t: t[0])
    print('integrity: %s' % integrity)
    print('midconf rows: %d, db-missing: %d, forbidden(20200614/credential/encrypted): %d %s'
          % (len(rows), len(missing), len(forbidden), forbidden[:5]))
    print('candidates >=0.85: %d (expected 607)' % len(cands))
    print('by kind: %s (expected 照片 343, 视频 264)' % dict(by_kind))
    print('by source: %s' % dict(by_source))
    print('by category: %s' % json.dumps(dict(by_cat), ensure_ascii=False, sort_keys=True))
    with open(OUT, 'w', encoding='utf-8') as fh:
        fh.write('rq_id\tsrc\tfinal_category\tfinal_confidence\tbatch\tkind\n')
        for rid, src, fcat, fconf, batch, kind, _ in cands:
            fh.write('%d\t%s\t%s\t%.3f\t%s\t%s\n' % (rid, src, fcat, fconf, batch, kind))
    n_snap = 0
    with open(SNAP, 'w', encoding='utf-8') as fh:
        for rid, src, fcat, fconf, batch, kind, _ in cands:
            st = os.stat(src)
            fh.write('%s\t%d\t%d\t%s\n' % (src, st.st_size, int(st.st_mtime), kind))
            n_snap += 1
    print('wrote %s (%d rows)' % (OUT, len(cands)))
    print('wrote %s (%d rows, stat-verified on disk)' % (SNAP, n_snap))
    ok = (len(cands) == 607 and by_kind.get('照片', 0) == 343
          and by_kind.get('视频', 0) == 264 and not forbidden and not missing)
    print('CANDIDATE CHECK: %s' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 3


if __name__ == '__main__':
    raise SystemExit(main())
