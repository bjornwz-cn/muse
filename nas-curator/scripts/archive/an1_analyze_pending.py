#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 1 分析：盘点全部 3877 条 pending，输出清单 TSV 到 /data/。
只读 DB、不动文件、不改库。容器内运行。
输出：
  /data/an1_media.tsv     媒体项：rq_id, kind, src, ext, mtype, eff_cat, cat_src, vlm_conf, damage_suspect, dst_rel, exists, size
  /data/an1_docs.tsv      文档项：rq_id, kind, src, ext, doc_party, doc_subdir, category, confidence, special, reason
  /data/an1_enc.tsv       加密头命中（__CLOUDSYNC_ENC__）：rq_id, kind, src, size
  /data/an1_summary.json  汇总计数
"""
import sqlite3, json, os
from collections import Counter

DB = '/data/nas-curator.db'
OUT = '/data'

PHOTO_DIRS = {
    '人物合影': '照片/家庭与人物/人物合影',
    '儿童成长': '照片/家庭与人物/儿童成长',
    '自拍': '照片/家庭与人物/自拍',
    '旅行风景': '照片/旅行与城市/旅行风景',
    '城市与建筑': '照片/旅行与城市/城市与建筑',
    '日常生活': '照片/生活记录/日常生活',
    '美食与咖啡': '照片/生活记录/美食与咖啡',
    '宠物与动物': '照片/生活记录/宠物与动物',
    '工作与活动': '照片/生活记录/工作与活动',
    '屏幕截图': '照片/资料与截图/屏幕截图',
    '文档与票据': '照片/资料与截图/文档与票据',
    '梗图与表情': '照片/资料与截图/梗图与表情',
}
VIDEO_CATS = {'人物合影', '儿童成长', '城市与建筑', '宠物与动物', '屏幕录制',
              '屏幕截图', '工作与活动', '文档与票据', '旅行风景', '日常生活',
              '梗图与表情', '美食与咖啡', '聚会与夜生活', '自拍', '运动与活动'}
PHOTO_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif', '.gif',
              '.tiff', '.tif', '.cr2', '.livp'}
VIDEO_EXTS = {'.mp4', '.mov', '.mts', '.mpg', '.vob', '.avi', '.mkv'}
UNUSABLE_CATS = {'待确认', '模糊', '疑似重复', '99-待确认/04-低置信',
                 '99-待确认/01-云同步加密', ''}
DAMAGE_KWS = ['全灰', '灰色空白', '全黑', '黑屏', '无法辨识', '无法辨认',
              '看不清', '空白', '加载异常', '损坏', '全为灰色', '几乎全黑',
              '画面全黑', '无可见内容', '无法识别实际场景', '仅顶部']
# 照片类目不在 PHOTO_DIRS 时的兜底映射（视频类目出现在照片上时）
PHOTO_FALLBACK = {'屏幕录制': '照片/资料与截图/屏幕截图',
                  '聚会与夜生活': '照片/生活记录/日常生活',
                  '运动与活动': '照片/生活记录/日常生活'}


def eff_cat(p):
    for k in ('vlm_category', 'vlm_economy_category'):
        c = (p.get(k) or '').strip()
        if c and c not in UNUSABLE_CATS:
            return c, k
    return '', ''


def damage_suspect(p):
    txt = (p.get('vlm_reason') or '') + '|' + (p.get('vlm_economy_reason') or '')
    return any(kw in txt for kw in DAMAGE_KWS)


def dst_for(mtype, cat):
    if not cat:
        return ''
    if mtype == 'photo':
        return PHOTO_DIRS.get(cat) or PHOTO_FALLBACK.get(cat, '')
    return ('视频/' + cat) if cat in VIDEO_CATS else ''


def main():
    db = sqlite3.connect(DB)
    cur = db.cursor()
    rows = cur.execute(
        "SELECT id, kind, payload FROM review_queue WHERE status='pending'"
    ).fetchall()
    media, docs, enc = [], [], []
    for rid, kind, payload in rows:
        try:
            p = json.loads(payload)
        except Exception:
            p = {}
        src = p.get('src') or p.get('src_path') or ''
        ext = '.' + src.rsplit('.', 1)[-1].lower() if '.' in src else ''
        exists, size = False, -1
        if src and os.path.exists(src):
            exists = True
            try:
                size = os.path.getsize(src)
            except Exception:
                size = -1
            try:
                with open(src, 'rb') as f:
                    if f.read(32).startswith(b'__CLOUDSYNC_ENC__'):
                        enc.append((rid, kind, src, size))
            except Exception:
                pass
        if ext in PHOTO_EXTS or ext in VIDEO_EXTS:
            mtype = 'photo' if ext in PHOTO_EXTS else 'video'
            cat, cat_src = eff_cat(p)
            vc = p.get('vlm_confidence')
            if vc is None:
                vc = p.get('vlm_economy_confidence')
            media.append((rid, kind, src, ext, mtype, cat, cat_src,
                          vc if vc is not None else '',
                          1 if damage_suspect(p) else 0,
                          dst_for(mtype, cat), 1 if exists else 0, size))
        else:
            special = ''
            if '纳威' in (payload or ''):
                special = 'nawei'
            if rid in (139399, 138941, 138943):
                special = 'credential'
            docs.append((rid, kind, src, ext,
                         p.get('doc_party') or '', p.get('doc_subdir') or '',
                         p.get('category') or '',
                         p.get('confidence') if p.get('confidence') is not None else '',
                         special, (p.get('reason') or '')[:200].replace('\t', ' ').replace('\n', ' ')))
    with open(os.path.join(OUT, 'an1_media.tsv'), 'w', encoding='utf-8') as f:
        f.write('rq_id\tkind\tsrc\text\tmtype\teff_cat\tcat_src\tvlm_conf\tdamage_suspect\tdst_rel\texists\tsize\n')
        for r in media:
            f.write('\t'.join(str(x) for x in r) + '\n')
    with open(os.path.join(OUT, 'an1_docs.tsv'), 'w', encoding='utf-8') as f:
        f.write('rq_id\tkind\tsrc\text\tdoc_party\tdoc_subdir\tcategory\tconfidence\tspecial\treason\n')
        for r in docs:
            f.write('\t'.join(str(x) for x in r) + '\n')
    with open(os.path.join(OUT, 'an1_enc.tsv'), 'w', encoding='utf-8') as f:
        f.write('rq_id\tkind\tsrc\tsize\n')
        for r in enc:
            f.write('\t'.join(str(x) for x in r) + '\n')
    summary = {
        'pending_total': len(rows),
        'media': len(media),
        'docs': len(docs),
        'enc_hits': len(enc),
        'media_by_mtype_cat': Counter((m[4], m[5]) for m in media).most_common(),
        'media_no_cat': sum(1 for m in media if not m[5]),
        'media_no_dst': sum(1 for m in media if not m[9]),
        'media_damage_suspect': sum(1 for m in media if m[8]),
        'media_missing': sum(1 for m in media if not m[10]),
        'docs_by_party': Counter(d[4] for d in docs).most_common(),
        'docs_nawei': sum(1 for d in docs if d[8] == 'nawei'),
        'docs_credential': sum(1 for d in docs if d[8] == 'credential'),
        'docs_missing': 0,
    }
    with open(os.path.join(OUT, 'an1_summary.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
