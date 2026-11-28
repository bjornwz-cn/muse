#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796：新出现视频清单（容器内，只读）。
视频 scope 中不在 /data/remaining_videos.tsv（上次 444 行）内、
且在盘的行 -> /data/r4796_new_videos.tsv。
用法：docker exec nas-curator python3 /data/r4796_new_videos.py
"""
import csv

OLD = '/data/remaining_videos.tsv'
VID = '/data/r4796_videos.tsv'
OUT = '/data/r4796_new_videos.tsv'


def main():
    old = set()
    with open(OLD, encoding='utf-8') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            old.add(int(r['rq_id']))
    new = []
    with open(VID, encoding='utf-8') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            if int(r['rq_id']) not in old and r['on_disk'] == '1':
                new.append(r)
    print('new videos: %d %s' % (len(new), [r['rq_id'] for r in new]))
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write('rq_id\tsrc\tkind\text\tsize\tmtime\n')
        for r in new:
            f.write('%s\t%s\t%s\t%s\t%s\t%s\n' % (
                r['rq_id'], r['src'], r['kind'], r['ext'], r['size'], r['mtime']))
    print('wrote', OUT)


if __name__ == '__main__':
    main()
