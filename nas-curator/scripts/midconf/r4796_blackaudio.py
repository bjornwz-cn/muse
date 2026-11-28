#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796：黑视频音频检查（宿主机，只读）。
对 rq 124209/124214：列出流信息，并用 astats 测整轨音量，
判断黑视频是否带有非静音音频。
用法（宿主机）：python3 /data/r4796_blackaudio.py
"""
import json, os, sqlite3, subprocess

DATA = "/vol2/@appshare/nas-curator/data"
IDS = [124209, 124214]


def main():
    db = sqlite3.connect(os.path.join(DATA, "nas-curator.db"))
    for rid in IDS:
        src = json.loads(db.execute(
            "SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()[0])['src']
        db.close()
        print('=== rq %d %s' % (rid, src))
        r = subprocess.run(["ffprobe", "-v", "error", "-show_streams",
                            "-of", "json", src],
                           capture_output=True, text=True, timeout=120)
        try:
            streams = json.loads(r.stdout).get('streams', [])
        except Exception:
            streams = []
        for s in streams:
            print(' stream %s codec=%s dur=%s' % (
                s.get('index'), s.get('codec_name'), s.get('duration')))
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "info", "-i", src,
                            "-af", "astats=metadata=1:reset=0",
                            "-f", "null", "-"],
                           capture_output=True, text=True, timeout=300)
        vol = [ln for ln in (r.stderr or '').splitlines()
               if 'RMS level' in ln or 'Peak level' in ln or 'Mean' in ln]
        print(' audio stats:', vol[:6] if vol else '(no audio stats — silent or no audio track)')
        db = sqlite3.connect(os.path.join(DATA, "nas-curator.db"))


if __name__ == '__main__':
    main()
