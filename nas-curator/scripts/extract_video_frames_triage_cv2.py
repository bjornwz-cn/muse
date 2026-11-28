#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤3 视频抽帧 cv2 兜底（容器内）：对 /data/triage_video_frame_failed.tsv
里仍失败的视频，用 cv2 读一帧存 /data/triage_frames/rq_<id>.jpg。
只写帧文件与汇总，不写库、不动用户文件。"""
import csv, json, os
import cv2

FAIL_TSV = '/data/triage_video_frame_failed.tsv'
FRAMES = '/data/triage_frames'
OUT = '/data/triage_video_frames_cv2.json'

os.makedirs(FRAMES, exist_ok=True)
rows = []
with open(FAIL_TSV, encoding='utf-8') as f:
    for r in csv.DictReader(f, delimiter='\t'):
        rows.append(r)
print('cv2 fallback candidates: %d' % len(rows), flush=True)
ok, fail = [], []
for r in rows:
    rid, src = int(r['rq_id']), r['src']
    out = os.path.join(FRAMES, 'rq_%d.jpg' % rid)
    try:
        cap = cv2.VideoCapture(src)
        good, frame = cap.read()
        cap.release()
        if good and frame is not None and frame.size > 0:
            cv2.imwrite(out, frame)
            if os.path.exists(out) and os.path.getsize(out) >= 500:
                ok.append(rid)
                continue
        fail.append(rid)
    except Exception as e:
        print('rq=%d error %s' % (rid, str(e)[:100]), flush=True)
        fail.append(rid)
print('CV2 FALLBACK DONE ok=%d fail=%d' % (len(ok), len(fail)), flush=True)
print('fail rq_ids:', fail, flush=True)
with open(OUT, 'w', encoding='utf-8') as f:
    json.dump({'n': len(rows), 'ok': len(ok), 'fail': len(fail),
               'ok_rqids': ok, 'fail_rqids': fail}, f, ensure_ascii=False, indent=2)
