#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤3 收尾（只读 DB，写 TSV）：仍留复核清单。
范围：当前仍 pending、收件箱在盘、最终 band 为 none 的行（2,843），
另加 rq133420（ge85 但保留份损坏）与 rq149995（不在盘）两个个案。
原因优先级：加密 > 文档无文本 > 去重 verdict > VLM 未达标 > 原分桶。"""
import csv, json, os, sqlite3
from collections import Counter

DB = '/data/nas-curator.db'
NONE = '/data/pending_triage_none.tsv'
DEDUP2 = '/data/pending_triage_dedup2.tsv'
DOCRES = '/data/year_siglip_results_triage.jsonl'
OUT = '/data/pending_triage_remaining.tsv'
PARTIES = {'1-王震', '2-上海润心', '3-北京供应链', '4-辽宁器械'}
REAL_CATS = {'人物合影', '儿童成长', '自拍', '旅行风景', '城市与建筑', '日常生活',
             '美食与咖啡', '宠物与动物', '工作与活动', '屏幕截图', '文档与票据',
             '梗图与表情', '聚会与夜生活', '运动与活动', '屏幕录制'}  # 与 analyze_pending.py 完全一致

def final_of(p, skind):
    """与 analyze_pending.py 完全同口径：返回 (final_or_None, conf_or_None)。
    skind 取 review_queue 的 kind 列（不是 payload 的 kind，后者常为 None）。"""
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


# 1) 原分桶（rq_id -> bucket）
bucket = {}
with open(NONE, encoding='utf-8') as f:
    for r in csv.DictReader(f, delimiter='\t'):
        bucket[int(r['rq_id'])] = r['bucket']
# 2) 去重 verdict（rq_id -> reason）
dedup_reason = {}
with open(DEDUP2, encoding='utf-8') as f:
    for r in csv.DictReader(f, delimiter='\t'):
        if r['action'] == 'skip':
            dedup_reason[int(r['rq_id'])] = r['reason']
# 3) 文档无文本（src -> True）
doc_empty = set()
with open(DOCRES, encoding='utf-8') as f:
    for line in f:
        r = json.loads(line)
        if r.get('why') == 'doc-empty':
            doc_empty.add(r['src'])

conn = sqlite3.connect(DB)
rows = conn.execute(
    "SELECT id, kind, payload FROM review_queue WHERE status='pending' "
    "AND json_extract(payload,'$.src') LIKE '/vol1/1000/收件箱/%'").fetchall()
out_rows = []
reasons = Counter()
for rid, skind, payload in rows:
    d = json.loads(payload)
    src = d.get('src') or ''
    if not os.path.exists(src):
        continue
    kind = d.get('kind') or ''
    final, fconf = final_of(d, skind)
    if final is not None:
        continue  # not none-band
    if rid in (133420, 149995):
        continue  # handled as specials below
    b = bucket.get(rid, '')
    if b == '加密':
        reason = '加密文件，等解密口径'
    elif src in doc_empty:
        reason = '文档无文本（抽不到文本：扫描件/旧格式）'
    elif rid in dedup_reason:
        reason = '重复嫌疑：' + dedup_reason[rid]
    elif d.get('vlm_category'):
        reason = 'VLM %s %.2f 未达 0.85' % (d.get('vlm_category'), float(d.get('vlm_confidence') or 0))
    elif b == '模糊':
        reason = '模糊照片，VLM 未达 0.85'
    elif b == '低置信':
        reason = '低置信，VLM 未达 0.85'
    elif b == 'SigLIP解码失败':
        reason = 'SigLIP 解码失败；复核可解码，VLM 未达 0.85'
    elif b == '文档无法定所属方':
        reason = '文档无法定所属方（LLM 两次均无法判断）'
    elif b == '照片无法判断':
        reason = '照片无法判断，VLM 未达 0.85'
    elif b == '不支持的扩展名':
        reason = '不支持的扩展名'
    else:
        reason = '待确认：%s' % b
    out_rows.append((rid, src, kind, reason))
    reasons[reason.split('：')[0].split('，')[0]] += 1

# specials
out_rows.append((133420, '/vol1/1000/收件箱/媒体库/视频/2019/20190507_0001.mov', '视频',
                 '目标与源字节相同、均部分损坏（ffmpeg 解出 23/89 帧，cv2 读不出），保留份不完整可用，按规则不删源，等王震定'))
out_rows.append((149995, '/vol1/1000/收件箱/媒体库/视频/2021/20210111_0001.mp4', '视频',
                 '源文件不在盘（已被移走）；媒体库有同名不同内容两份，哈希不同，不可误删'))
out_rows.sort(key=lambda r: r[0])
with open(OUT, 'w', encoding='utf-8') as f:
    f.write('rq_id\tsrc\tkind\treason\n')
    for r in out_rows:
        f.write('%d\t%s\t%s\t%s\n' % r)
print('remaining rows: %d' % len(out_rows))
print('reason heads:', dict(reasons))
print('REMAINING DONE')
