#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘点 pending 行：为“文件整理继续”找下一步。
只读 DB、只读磁盘，不写库、不移动文件。
对每条 pending 行按汇总口径算最终类目/置信度
（与 year_summary_batch5.py 完全同口径：
 VLM>=0.72 用 VLM（经济版除外）；否则 kind=文档 且 doc_party 为真实所属方
 用 doc_party；否则用 SigLIP 真实类目；否则待确认），
并核对 src 是否在盘、是否在收件箱。
输出分桶计数与可移回候选清单（仅供下一步参考，不执行）。
"""
import json, os, sqlite3
from collections import Counter

DB_PATH = "/data/nas-curator.db"
REAL_CATS = {'人物合影', '儿童成长', '自拍', '旅行风景', '城市与建筑', '日常生活',
             '美食与咖啡', '宠物与动物', '工作与活动', '屏幕截图', '文档与票据',
             '梗图与表情', '聚会与夜生活', '运动与活动', '屏幕录制'}
PARTIES = {'1-王震', '2-上海润心', '3-北京供应链', '4-辽宁器械'}
IMG_EXTS = {'.jpg', '.jpeg', '.heic', '.png', '.heif', '.webp', '.gif', '.nef',
            '.tiff', '.jfif'}
VID_EXTS = {'.mov', '.mp4', '.mts', '.vob', '.avi', '.wmv'}
DOC_EXTS = {'.docx', '.doc', '.xlsx', '.xls', '.pdf', '.pptx', '.ppt',
            '.txt', '.md', '.csv', '.eml', '.html', '.webloc', '.zip',
            '.rar', '.eddx', '.emmx', '.osheet', '.odoc'}


def kind_of(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in IMG_EXTS:
        return '照片'
    if ext in VID_EXTS:
        return '视频'
    if ext in DOC_EXTS:
        return '文档'
    return '其他'


def final_of(p, skind):
    """与 year_summary_batch5.py 同口径。返回 (final_category_or_None, final_conf_or_None)。"""
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
    doc_party = p.get('doc_party') or ''
    doc_model = p.get('doc_model') or ''
    if doc_party and 'economy' in str(doc_model):
        doc_party = ''
    if vcat and vconf >= 0.72:
        return vcat, vconf
    if skind == '文档' and doc_party in PARTIES:
        return doc_party, conf
    if p.get('category') in REAL_CATS:
        return p['category'], conf
    return None, None


def band(conf):
    if conf is None:
        return 'none'
    if conf >= 0.85:
        return 'ge85'
    if conf >= 0.72:
        return '72-85'
    if conf >= 0.35:
        return '35-72'
    return 'lt35'


def main():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    rows = conn.execute(
        "SELECT id, kind, payload FROM review_queue WHERE status='pending'"
    ).fetchall()
    conn.close()
    print('pending rows total: %d' % len(rows))
    buckets = Counter()
    ge85_inbox = []
    ge85_by_batch = Counter()
    ge85_by_kind = Counter()
    inbox_on_disk = 0
    for rid, kind, payload in rows:
        try:
            p = json.loads(payload) if payload else {}
        except Exception:
            p = {}
        src = p.get('src', '')
        in_inbox = src.startswith('/vol1/1000/收件箱/')
        on_disk = bool(src) and os.path.exists(src)
        skind = kind_of(src) if src else ''
        fcat, fconf = final_of(p, skind)
        b = band(fconf)
        buckets[(in_inbox, on_disk, b)] += 1
        if in_inbox and on_disk:
            inbox_on_disk += 1
            if b == 'ge85':
                ge85_inbox.append((rid, src, fcat, fconf, p.get('batch', ''), skind))
                ge85_by_batch[p.get('batch', '')] += 1
                ge85_by_kind[skind] += 1
    print('pending rows with src in inbox AND on disk: %d' % inbox_on_disk)
    print('== 分桶 (in_inbox, on_disk, band) ==')
    for k in sorted(buckets):
        print('  %s: %d' % (k, buckets[k]))
    print('== 收件箱在盘且最终 >=0.85（可移回候选）: %d ==' % len(ge85_inbox))
    print('  by batch:', dict(ge85_by_batch))
    print('  by kind:', dict(ge85_by_kind))
    with open('/data/batch5_moveback_candidates.tsv', 'w', encoding='utf-8') as fh:
        fh.write('rq_id\tsrc\tfinal_category\tfinal_confidence\tbatch\tkind\n')
        for rid, src, fcat, fconf, batch, skind in sorted(ge85_inbox):
            fh.write('%d\t%s\t%s\t%.3f\t%s\t%s\n' % (rid, src, fcat, fconf, batch, skind))
    print('candidate list: /data/batch5_moveback_candidates.tsv (%d rows)' % len(ge85_inbox))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
