#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤3 步骤3前置：建重分类清单（只读，不写库）。
照片：待确认行中复核 pil-ok 的照片（1,726 个）；
视频：待确认行中复核 cv2-ok 的视频（202 个）＋抽帧失败但 cv2-ok 的视频（152 个）；
文档：doc_party=无法判断 的文档（263 个）＋ empty-text 文档（106 个）。
输出 /data/triage_photo_ids.txt、/data/triage_photo_list.tsv、
/data/triage_video_list.tsv、/data/triage_doc_list.tsv。
逐行核对：仍 pending、在盘、非加密、非凭据、非 20200614。"""
import csv, json, os, sqlite3
from collections import Counter

DB = '/data/nas-curator.db'
NONE_TSV = '/data/pending_triage_none.tsv'
VERIFY_TSV = '/data/pending_triage_verify.tsv'
FF_TSV = '/data/midconf_video_frame_failed.tsv'
EXCLUDE_RQ = {138941, 138943, 139432}

conn = sqlite3.connect(DB, timeout=30)
pending = {r[0] for r in conn.execute("select id from review_queue where status='pending'")}
conn.close()

none = {}
with open(NONE_TSV, encoding='utf-8') as f:
    for r in csv.DictReader(f, delimiter='\t'):
        none[int(r['rq_id'])] = r
verify = {}
with open(VERIFY_TSV, encoding='utf-8') as f:
    for r in csv.DictReader(f, delimiter='\t'):
        verify[int(r['rq_id'])] = r

photos, videos, docs = [], [], []
problems = []
for rid, r in sorted(none.items()):
    if rid in EXCLUDE_RQ or '20200614_0001.mov' in r['src']:
        continue
    if rid not in pending:
        problems.append((rid, 'not-pending-anymore'))
        continue
    if r['bucket'] == '加密':
        continue
    src = r['src']
    if not os.path.exists(src):
        problems.append((rid, 'not-on-disk'))
        continue
    if r['skind'] == '照片' and verify.get(rid, {}).get('verdict') == 'pil-ok':
        photos.append((rid, src))
    elif r['skind'] == '视频' and verify.get(rid, {}).get('verdict') == 'cv2-ok':
        videos.append((rid, src))
    elif r['skind'] == '文档':
        docs.append((rid, src))
print('photos pil-ok:', len(photos), 'videos cv2-ok(none):', len(videos), 'docs:', len(docs))
print('problems:', problems[:10], 'n_problems:', len(problems))
print('photo buckets:', dict(Counter(none[r]['bucket'] for r, _ in photos)))
print('doc buckets:', dict(Counter(none[r]['bucket'].split(':')[0] for r, _ in docs)))

# 抽帧失败 cv2-ok 的视频（不在 none 集内，另行核对 pending＋在盘）
ff_videos = []
with open(FF_TSV, encoding='utf-8') as f:
    for r in csv.DictReader(f, delimiter='\t'):
        rid = int(r['rq_id'])
        if verify.get(rid, {}).get('verdict') == 'cv2-ok' and rid in pending \
                and os.path.exists(r['src']) and '20200614_0001.mov' not in r['src']:
            ff_videos.append((rid, r['src']))
        elif verify.get(rid, {}).get('verdict') == 'cv2-ok':
            problems.append((rid, 'ff-not-pending-or-gone'))
print('frame_failed cv2-ok videos:', len(ff_videos))
print('problems total:', len(problems))

with open('/data/triage_photo_ids.txt', 'w', encoding='utf-8') as f:
    f.write(','.join(str(r) for r, _ in photos))
with open('/data/triage_photo_list.tsv', 'w', encoding='utf-8') as f:
    f.write('rq_id\tsrc\n')
    for rid, src in photos:
        f.write('%d\t%s\n' % (rid, src))
all_videos = sorted(videos + ff_videos)
with open('/data/triage_video_list.tsv', 'w', encoding='utf-8') as f:
    f.write('rq_id\tsrc\tscope\n')
    vset = {r for r, _ in videos}
    for rid, src in all_videos:
        f.write('%d\t%s\t%s\n' % (rid, src, 'none' if (rid, src) in vset else 'frame_failed'))
with open('/data/triage_doc_list.tsv', 'w', encoding='utf-8') as f:
    f.write('rq_id\tsrc\tbucket\n')
    for rid, src in docs:
        f.write('%d\t%s\t%s\n' % (rid, src, none[rid]['bucket']))
print('wrote triage lists: photos=%d videos=%d (none=%d ff=%d) docs=%d' % (
    len(photos), len(all_videos), len(videos), len(ff_videos), len(docs)))
