#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796：none 波段视频清单（容器内，只读）。
打印视频 scope 中最终 band 为 none（<0.35 或无类目）的行：
文件名、SigLIP 类目/置信度、VLM 类目/置信度/模型、帧是否存在。
用法：docker exec nas-curator python3 /data/r4796_none_videos.py
"""
import csv, json, os, sqlite3

DB = '/data/nas-curator.db'
VID = '/data/r4796_videos.tsv'
FRAMES = '/data/remaining_video_frames'


def main():
    db = sqlite3.connect(DB)
    rows = []
    with open(VID, encoding='utf-8') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            if r['band'] == 'none':
                rows.append(r)
    print('none-band videos: %d' % len(rows))
    for r in rows:
        rid = int(r['rq_id'])
        d = json.loads(db.execute(
            "SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()[0])
        print('%d | %s | siglip=%s(%.2f) vlm=%s(%.2f,%s) frame=%s' % (
            rid, os.path.basename(r['src']),
            d.get('category'), float(d.get('confidence') or 0),
            d.get('vlm_category'), float(d.get('vlm_confidence') or 0),
            (d.get('vlm_model') or '')[:30],
            os.path.exists(os.path.join(FRAMES, 'rq_%d.jpg' % rid))))


if __name__ == '__main__':
    main()
