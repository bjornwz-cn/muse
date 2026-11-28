#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796：疑似黑帧视频多点采样（宿主机，只读）。
对 rq 124209/124214/150079 在 10/25/50/75/90% 处各抽 1 帧
存 /data/r4796_blackcheck/rq_<id>_p< pct>.jpg，并用 PIL 算灰度统计打印。
用法（宿主机）：python3 /data/r4796_blackcheck.py
"""
import json, os, sqlite3, subprocess
from PIL import Image, ImageStat

DATA = "/vol2/@appshare/nas-curator/data"
OUTDIR = os.path.join(DATA, "r4796_blackcheck")
DB = "/data/nas-curator.db"
IDS = [124209, 124214, 150079]
PCTS = [10, 25, 50, 75, 90]


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    for rid in IDS:
        # src 从容器 DB 读不到（宿主机无 sqlite 路径？/data 在宿主机就是 DATA）
        import sqlite3 as s3
        db = s3.connect(os.path.join(DATA, "nas-curator.db"))
        src = json.loads(db.execute(
            "SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()[0])['src']
        db.close()
        rp = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                             "format=duration", "-of", "csv=p=0", src],
                            capture_output=True, text=True, timeout=120)
        dur = float((rp.stdout or '').strip().strip(','))
        print('=== rq %d dur=%.1f %s' % (rid, dur, src))
        for pct in PCTS:
            out = os.path.join(OUTDIR, 'rq_%d_p%d.jpg' % (rid, pct))
            rr = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss",
                                 "%.3f" % (dur * pct / 100), "-i", src,
                                 "-frames:v", "1", "-q:v", "2", "-y", out],
                                capture_output=True, text=True, timeout=300)
            if rr.returncode != 0 or not os.path.exists(out):
                print(' p%d FAIL rc=%d' % (pct, rr.returncode))
                continue
            img = Image.open(out).convert('L')
            st = ImageStat.Stat(img)
            print(' p%d mean=%.1f stddev=%.1f min=%d max=%d bytes=%d' % (
                pct, st.mean[0], st.stddev[0], img.getextrema()[0],
                img.getextrema()[1], os.path.getsize(out)))


if __name__ == '__main__':
    main()
