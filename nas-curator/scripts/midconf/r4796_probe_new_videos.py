#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796 深度核查：新视频 probe_failed 原因（宿主机，只读）。
对 16 个新视频逐个打印：大小、前 32 字节 hex、ffprobe 两种取帧数的
原始 stdout/stderr/rc、ffmpeg 抽 1 帧是否成功。
用法（宿主机）：python3 /data/r4796_probe_new_videos.py
"""
import json, os, subprocess

DATA = "/vol2/@appshare/nas-curator/data"
LIST = os.path.join(DATA, "r4796_new_videos.tsv")


def main():
    rows = []
    with open(LIST, encoding='utf-8') as fh:
        hdr = fh.readline().rstrip('\n').split('\t')
        idx = {c: i for i, c in enumerate(hdr)}
        for line in fh:
            p = line.rstrip('\n').split('\t')
            if len(p) >= 2 and p[idx['rq_id']].isdigit():
                rows.append((int(p[idx['rq_id']]), p[idx['src']]))
    for rid, src in rows:
        print('=== rq %d %s' % (rid, src))
        try:
            st = os.stat(src)
            print(' size:', st.st_size)
            with open(src, 'rb') as fh:
                head = fh.read(32)
            print(' head32 hex:', head.hex())
            print(' head32 ascii:', ''.join(chr(c) if 32 <= c < 127 else '.' for c in head))
        except Exception as e:
            print(' stat/read fail:', e)
            continue
        for label, cmd in (
            ('nb_frames', ["ffprobe", "-v", "error", "-select_streams", "v:0",
                           "-show_entries", "stream=nb_frames", "-of", "csv=p=0", src]),
            ('count_frames', ["ffprobe", "-v", "error", "-count_frames",
                              "-select_streams", "v:0",
                              "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", src]),
        ):
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            print(' %s rc=%d stdout=%r stderr=%r' % (label, r.returncode,
                                                      (r.stdout or '')[:120],
                                                      (r.stderr or '')[:200]))
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", src,
                            "-frames:v", "1", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=120)
        print(' ffmpeg-1frame rc=%d stderr=%r' % (r.returncode, (r.stderr or '')[:200]))


if __name__ == '__main__':
    main()
