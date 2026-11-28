#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796：新视频抽中间帧（宿主机，只读源文件）。
对 /data/r4796_new_videos_verify.tsv 中 verdict==good 的行抽中间帧
存 /data/remaining_video_frames/rq_<id>.jpg（与既有帧同目录）。
用法（宿主机）：python3 /data/r4796_extract_new_frames.py
"""
import csv, json, os, subprocess

DATA = "/vol2/@appshare/nas-curator/data"
LIST = os.path.join(DATA, "r4796_new_videos_verify.tsv")
FRAMES = os.path.join(DATA, "remaining_video_frames")


def main():
    rows = []
    with open(LIST, encoding='utf-8') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            if r['verdict'] == 'good':
                rows.append(r)
    print('good new videos: %d' % len(rows))
    os.makedirs(FRAMES, exist_ok=True)
    ok = 0
    for r in rows:
        rid, src = r['rq_id'], r['src']
        out = os.path.join(FRAMES, 'rq_%s.jpg' % rid)
        rp = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                             "format=duration", "-of", "csv=p=0", src],
                            capture_output=True, text=True, timeout=120)
        try:
            dur = float((rp.stdout or '').strip().strip(','))
        except Exception:
            print('skip rq=%s no duration' % rid)
            continue
        rr = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", "%.3f" % (dur / 2),
                             "-i", src, "-frames:v", "1", "-q:v", "2", "-y", out],
                            capture_output=True, text=True, timeout=300)
        if rr.returncode == 0 and os.path.exists(out) and os.path.getsize(out) >= 500:
            ok += 1
            print('ok rq=%s %d bytes' % (rid, os.path.getsize(out)))
        else:
            print('FAIL rq=%s rc=%d err=%s' % (rid, rr.returncode, (rr.stderr or '')[:120]))
    print('frames ok: %d/%d' % (ok, len(rows)))


if __name__ == '__main__':
    main()
