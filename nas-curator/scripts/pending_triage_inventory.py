#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤3 步骤0：盘点待确认文件（只读，不写库）。
收件箱在盘 pending 行中最终 band 为 none 的行，按 kind x 原因分桶；
另盘点 171 个抽帧失败视频的重叠与 rq 133420 状态。
输出 /data/pending_triage_inventory.json 与 /data/pending_triage_none.tsv。
"""
import json, os, sqlite3
from collections import Counter

DB = '/data/nas-curator.db'
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


def bucket(p, skind, db_kind):
    src = p.get('src', '') or ''
    reason = p.get('reason', '') or ''
    cat = p.get('category', '') or ''
    vlm_reason = p.get('vlm_reason', '') or ''
    if '__CLOUDSYNC_ENC__' in src or '加密' in reason or '加密' in cat:
        return '加密'
    if db_kind == 'dup':
        return '重复(dup)'
    if 'decode' in reason.lower() or '解码' in reason:
        return 'SigLIP解码失败'
    if 'decode' in vlm_reason.lower() or '解码失败' in vlm_reason or '无法解码' in vlm_reason:
        return 'VLM解码失败'
    if cat == '模糊' or reason.startswith('blurry'):
        return '模糊'
    if reason.startswith('low'):
        return '低置信'
    if skind == '文档' and ('empty' in reason.lower() or '无文本' in reason or '无内容' in reason):
        return '文档无文本'
    if skind == '其他':
        return '不支持的扩展名/其他'
    return '其他:' + reason.split(':')[0][:40]


def main():
    conn = sqlite3.connect(DB, timeout=30)
    rows = conn.execute(
        "SELECT id, kind, payload FROM review_queue WHERE status='pending'").fetchall()
    rq133420 = conn.execute(
        "SELECT id, kind, status, decided_by, payload FROM review_queue WHERE id=133420").fetchone()
    total = conn.execute("SELECT status, count(*) FROM review_queue GROUP BY status").fetchall()
    conn.close()
    print('review totals:', total)

    none_rows = []
    not_inbox = 0
    not_on_disk = 0
    band_counter = Counter()
    for rid, db_kind, payload in rows:
        try:
            p = json.loads(payload) if payload else {}
        except Exception:
            p = {}
        src = p.get('src', '') or ''
        in_inbox = src.startswith('/vol1/1000/收件箱/')
        on_disk = bool(src) and os.path.exists(src)
        skind = kind_of(src) if src else ''
        fcat, fconf = final_of(p, skind)
        b = band(fconf)
        band_counter[(b, in_inbox, on_disk)] += 1
        if b == 'none' and in_inbox and on_disk:
            none_rows.append((rid, db_kind, p, skind))
        elif b == 'none':
            if not in_inbox:
                not_inbox += 1
            if not on_disk:
                not_on_disk += 1

    print('band x inbox x ondisk:', dict(band_counter))
    print('none+inbox+ondisk:', len(none_rows), ' none-not-inbox:', not_inbox, ' none-not-ondisk:', not_on_disk)

    by_kind = Counter()
    by_bucket = Counter()
    by_kind_bucket = Counter()
    by_dbkind = Counter()
    by_ext = Counter()
    total_bytes = 0
    for rid, db_kind, p, skind in none_rows:
        by_kind[skind] += 1
        by_dbkind[db_kind] += 1
        bk = bucket(p, skind, db_kind)
        by_bucket[bk] += 1
        by_kind_bucket[(skind, bk)] += 1
        ext = os.path.splitext(p.get('src', ''))[1].lower()
        by_ext[ext] += 1
        try:
            total_bytes += os.path.getsize(p.get('src', ''))
        except Exception:
            pass
    print('by kind:', dict(by_kind))
    print('by db_kind:', dict(by_dbkind))
    print('by bucket:', dict(by_bucket))
    print('by kind x bucket:', {f'{k[0]}|{k[1]}': v for k, v in sorted(by_kind_bucket.items())})
    print('by ext:', dict(by_ext))
    print('total bytes:', total_bytes, 'GB %.2f' % (total_bytes / 1e9))

    # 171 抽帧失败视频重叠
    ff_path = '/data/midconf_video_frame_failed.tsv'
    ff_rows = []
    if os.path.exists(ff_path):
        with open(ff_path, encoding='utf-8', errors='replace') as f:
            header = f.readline()
            for line in f:
                parts = line.rstrip('\n').split('\t')
                ff_rows.append(parts)
    print('frame_failed file header:', header.strip() if os.path.exists(ff_path) else 'MISSING')
    print('frame_failed rows:', len(ff_rows))
    none_srcs = {p.get('src', '') for _, _, p, _ in none_rows}
    none_ids = {rid for rid, _, _, _ in none_rows}
    ff_srcs = set()
    ff_ids = set()
    for parts in ff_rows:
        for c in parts:
            if c.startswith('/vol1/'):
                ff_srcs.add(c)
            if c.isdigit() and len(c) >= 4:
                ff_ids.add(int(c))
    overlap_src = ff_srcs & none_srcs
    print('frame_failed distinct srcs:', len(ff_srcs), 'distinct ids:', len(ff_ids))
    print('overlap with none set (by src):', len(overlap_src))
    print('overlap with none set (by id):', len(ff_ids & none_ids))
    print('frame_failed not in none set (by src):', len(ff_srcs - none_srcs))

    # rq 133420
    print('rq133420:', rq133420[0], rq133420[1], rq133420[2], rq133420[3], (rq133420[4] or '')[:400] if rq133420 else None)

    # 写清单 TSV
    with open('/data/pending_triage_none.tsv', 'w', encoding='utf-8') as f:
        f.write('rq_id\tdb_kind\tskind\tbucket\tsrc\tsize\tmtime\tcategory\tconfidence\treason\tvlm_category\tvlm_confidence\tvlm_model\tvlm_reason\tdoc_party\n')
        for rid, db_kind, p, skind in sorted(none_rows):
            src = p.get('src', '') or ''
            try:
                st = os.stat(src)
                size, mtime = st.st_size, int(st.st_mtime)
            except Exception:
                size, mtime = '', ''
            bk = bucket(p, skind, db_kind)
            f.write('\t'.join(str(x) for x in [
                rid, db_kind, skind, bk, src, size, mtime,
                p.get('category', ''), p.get('confidence', ''),
                (p.get('reason', '') or '').replace('\t', ' ').replace('\n', ' '),
                p.get('vlm_category', ''), p.get('vlm_confidence', ''), p.get('vlm_model', ''),
                (p.get('vlm_reason', '') or '').replace('\t', ' ').replace('\n', ' ')[:200],
                p.get('doc_party', '')]) + '\n')

    inv = {
        'review_totals': {k: v for k, v in total},
        'none_inbox_ondisk': len(none_rows),
        'none_not_inbox': not_inbox,
        'none_not_ondisk': not_on_disk,
        'band_counter': {f'{k[0]}|inbox={k[1]}|ondisk={k[2]}': v for k, v in band_counter.items()},
        'by_kind': dict(by_kind),
        'by_db_kind': dict(by_dbkind),
        'by_bucket': dict(by_bucket),
        'by_kind_bucket': {f'{k[0]}|{k[1]}': v for k, v in sorted(by_kind_bucket.items())},
        'by_ext': dict(by_ext),
        'total_bytes': total_bytes,
        'frame_failed_total': len(ff_rows),
        'frame_failed_distinct_srcs': len(ff_srcs),
        'frame_failed_overlap_none_src': len(overlap_src),
        'frame_failed_overlap_none_id': len(ff_ids & none_ids),
        'frame_failed_not_in_none_src': len(ff_srcs - none_srcs),
        'rq133420': {'id': rq133420[0], 'kind': rq133420[1], 'status': rq133420[2],
                     'decided_by': rq133420[3]} if rq133420 else None,
    }
    with open('/data/pending_triage_inventory.json', 'w', encoding='utf-8') as f:
        json.dump(inv, f, ensure_ascii=False, indent=2)
    print('wrote /data/pending_triage_inventory.json and /data/pending_triage_none.tsv')


if __name__ == '__main__':
    main()
