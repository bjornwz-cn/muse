#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796：帧亮度统计（容器内，只读）。
对指定 rq_id 的帧（/data/remaining_video_frames/rq_<id>.jpg）算
灰度均值/标准差/极值，用于判断是否黑帧/空白帧。
用法：docker exec nas-curator python3 /data/r4796_frame_stats.py <rq_id...>
"""
import os, sys
from PIL import Image, ImageStat

FRAMES = '/data/remaining_video_frames'


def main():
    for rid in sys.argv[1:]:
        p = os.path.join(FRAMES, 'rq_%s.jpg' % rid)
        if not os.path.exists(p):
            print('%s NOFRAME' % rid)
            continue
        img = Image.open(p).convert('L')
        st = ImageStat.Stat(img)
        print('%s size=%s mean=%.1f stddev=%.1f min=%d max=%d bytes=%d' % (
            rid, img.size, st.mean[0], st.stddev[0],
            img.getextrema()[0], img.getextrema()[1], os.path.getsize(p)))


if __name__ == '__main__':
    main()
