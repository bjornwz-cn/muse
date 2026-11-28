#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796 任务 步骤 0：建范围清单（容器内，只读 DB）。
- 视频 scope：pending 行中扩展名属视频（含 .mpg/.m4v 等宽松集）。
- 其他 scope：analyze_pending 口径 kind=='其他'（102 行级）＋ .zip/.rar/.eml
  （.zip/.rar 未进文档任务范围；.eml 26/27 已进文档任务，本次只核查加密/定不了，
  不重跑文档分类；定义见完成记录）。
- 重跑集：视频 scope 中最终 band 72-85 的行，排除 rq 149995/150025
  （源不在盘，不动）与 20200614_0001.mov、凭据行。
最终 band 口径与 analyze_pending 完全一致：
  VLM>=0.72 用 VLM（经济版除外）；否则 SigLIP 真实类目；否则 none。
输出 /data/r4796_videos.tsv、/data/r4796_others.tsv、/data/r4796_rerun.tsv、
/data/r4796_scope_summary.json；打印 review 计数与 integrity。
用法：docker exec nas-curator python3 /data/r4796_build_lists.py
"""
import json, os, sqlite3
from collections import Counter

DB = '/data/nas-curator.db'
OUT_VID = '/data/r4796_videos.tsv'
OUT_OTH = '/data/r4796_others.tsv'
OUT_RERUN = '/data/r4796_rerun.tsv'
OUT_JSON = '/data/r4796_scope_summary.json'

REAL_CATS = {'人物合影', '儿童成长', '自拍', '旅行风景', '城市与建筑', '日常生活',
             '美食与咖啡', '宠物与动物', '工作与活动', '屏幕截图', '文档与票据',
             '梗图与表情', '聚会与夜生活', '运动与活动', '屏幕录制'}
PARTIES = {'1-王震', '2-上海润心', '3-北京供应链', '4-辽宁器械'}
IMG_EXTS = {'.jpg', '.jpeg', '.heic', '.png', '.heif', '.webp', '.gif', '.nef',
            '.tiff', '.jfif'}
VID_CANON = {'.mov', '.mp4', '.mts', '.vob', '.avi', '.wmv'}
VID_LOOSE = {'.mov', '.mp4', '.avi', '.mkv', '.m4v', '.wmv', '.flv',
             '.webm', '.vob', '.3gp', '.mts', '.mpg'}
DOC_EXTS = {'.docx', '.doc', '.xlsx', '.xls', '.pdf', '.pptx', '.ppt',
            '.txt', '.md', '.csv', '.eml', '.html', '.webloc', '.zip',
            '.rar', '.eddx', '.emmx', '.osheet', '.odoc'}
EXCLUDE_RQ = {149995, 150025}
CRED_RQ = {138941, 138943, 139426, 139428, 139430, 139432}


def kind_of(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in IMG_EXTS:
        return '照片'
    if ext in VID_CANON:
        return '视频'
    if ext in DOC_EXTS:
        return '文档'
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
    return None, None, 'none'


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


def clean(s):
    return (s or '').replace('\t', ' ').replace('\n', ' ').replace('\r', ' ')


def main():
    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    print('integrity:', db.execute('PRAGMA integrity_check').fetchone()[0], flush=True)
    counts = dict(db.execute(
        "SELECT status, COUNT(*) FROM review_queue GROUP BY status").fetchall())
    print('review counts:', json.dumps(counts, ensure_ascii=False), flush=True)
    rows = db.execute(
        "SELECT id, kind, payload FROM review_queue WHERE status='pending' ORDER BY id"
    ).fetchall()
    print('pending rows scanned: %d' % len(rows), flush=True)
    vids, oths = [], []
    for rid, kind, payload in rows:
        try:
            d = json.loads(payload)
        except Exception:
            continue
        src = d.get('src', '') or ''
        ext = os.path.splitext(src)[1].lower()
        fcat, fconf, fsrc = final_of(d)
        rec = {'rq_id': rid, 'src': src, 'kind': kind, 'ext': ext,
               'size': d.get('size'), 'mtime': d.get('mtime'),
               'final_cat': fcat, 'final_conf': fconf, 'final_src': fsrc,
               'band': band(fconf),
               'has_vlm': 'vlm_category' in d,
               'old_vlm_conf': d.get('vlm_confidence'),
               'old_vlm_model': d.get('vlm_model'),
               'doc_party': d.get('doc_party') or '',
               'on_disk': os.path.exists(src) if src else False}
        if ext in VID_LOOSE:
            vids.append(rec)
        elif kind_of(src) == '其他' or ext in ('.zip', '.rar', '.eml'):
            oths.append(rec)
    # 排除项核查
    excl = {}
    for rid in sorted(EXCLUDE_RQ | CRED_RQ | {119852}):
        r = db.execute("SELECT status, decided_by, payload FROM review_queue WHERE id=?",
                       (rid,)).fetchone()
        excl[rid] = {'status': r[0] if r else 'NOT FOUND',
                     'decided_by': r[1] if r else None,
                     'src': (json.loads(r[2]).get('src') if r else None)}
    in_vid_excl = [v for v in vids if v['rq_id'] in EXCLUDE_RQ]
    in_oth_excl = [o for o in oths if o['rq_id'] in EXCLUDE_RQ]
    rerun = [v for v in vids
             if v['band'] == '72-85'
             and v['rq_id'] not in EXCLUDE_RQ
             and '20200614_0001.mov' not in v['src']
             and v['rq_id'] not in CRED_RQ
             and v['on_disk']]
    with open(OUT_VID, 'w', encoding='utf-8') as f:
        f.write('rq_id\tsrc\tkind\text\tsize\tmtime\tfinal_cat\tfinal_conf\tfinal_src\tband\thas_vlm\told_vlm_conf\told_vlm_model\ton_disk\n')
        for r in vids:
            f.write('%d\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' % (
                r['rq_id'], clean(r['src']), r['kind'], r['ext'], r['size'], r['mtime'],
                clean(str(r['final_cat'])), r['final_conf'], r['final_src'], r['band'],
                int(r['has_vlm']), r['old_vlm_conf'], clean(str(r['old_vlm_model'])),
                int(r['on_disk'])))
    with open(OUT_OTH, 'w', encoding='utf-8') as f:
        f.write('rq_id\tsrc\tkind\text\tsize\tmtime\tfinal_cat\tfinal_conf\tfinal_src\tband\thas_vlm\told_vlm_conf\told_vlm_model\tdoc_party\ton_disk\n')
        for r in oths:
            f.write('%d\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' % (
                r['rq_id'], clean(r['src']), r['kind'], r['ext'], r['size'], r['mtime'],
                clean(str(r['final_cat'])), r['final_conf'], r['final_src'], r['band'],
                int(r['has_vlm']), r['old_vlm_conf'], clean(str(r['old_vlm_model'])),
                clean(r['doc_party']), int(r['on_disk'])))
    with open(OUT_RERUN, 'w', encoding='utf-8') as f:
        f.write('rq_id\tsrc\tkind\text\tsize\tmtime\tfinal_cat\tfinal_conf\tfinal_src\tband\thas_vlm\told_vlm_conf\told_vlm_model\ton_disk\n')
        for r in rerun:
            f.write('%d\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' % (
                r['rq_id'], clean(r['src']), r['kind'], r['ext'], r['size'], r['mtime'],
                clean(str(r['final_cat'])), r['final_conf'], r['final_src'], r['band'],
                int(r['has_vlm']), r['old_vlm_conf'], clean(str(r['old_vlm_model'])),
                int(r['on_disk'])))
    summ = {
        'pending_total': len(rows),
        'videos_scope': len(vids),
        'videos_by_ext': dict(Counter(v['ext'] for v in vids)),
        'videos_by_band': dict(Counter(v['band'] for v in vids)),
        'videos_on_disk': sum(1 for v in vids if v['on_disk']),
        'videos_missing': [v['rq_id'] for v in vids if not v['on_disk']],
        'others_scope': len(oths),
        'others_by_ext': dict(Counter(o['ext'] for o in oths)),
        'others_by_band': dict(Counter(o['band'] for o in oths)),
        'others_on_disk': sum(1 for o in oths if o['on_disk']),
        'others_missing': [o['rq_id'] for o in oths if not o['on_disk']],
        'rerun_n': len(rerun),
        'rerun_excluded_missing_band72_85': [v['rq_id'] for v in in_vid_excl],
        'rerun_excluded_other_scope': [o['rq_id'] for o in in_oth_excl],
        'exclusions': excl,
        'videos_20200614_in_scope': [v['rq_id'] for v in vids if '20200614_0001.mov' in v['src']],
        'cred_in_scope': [v['rq_id'] for v in vids if v['rq_id'] in CRED_RQ] +
                         [o['rq_id'] for o in oths if o['rq_id'] in CRED_RQ],
    }
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print('SUMMARY ' + json.dumps(summ, ensure_ascii=False), flush=True)
    db.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
