#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch3 snapshot: 收件箱/媒体库/ 照片+视频年份文件夹下当前在盘、
且 review_queue 无对应行的新到文件。只读 DB，只写快照 TSV。
输出 TSV 列：path<TAB>size<TAB>mtime<TAB>kind（与 batch2 快照同格式，无表头）。
用法（NAS 容器内）：docker exec nas-curator python3 /data/year_snapshot_batch3.py
"""
import sys, os, json, sqlite3

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

DB_PATH = "/data/nas-curator.db"
OUT = "/data/year_batch3_snapshot.tsv"
PHOTO_ROOT = "/vol1/1000/收件箱/媒体库/照片"
VIDEO_ROOT = "/vol1/1000/收件箱/媒体库/视频"
IMG_EXTS = {'.jpg', '.jpeg', '.heic', '.png', '.heif', '.webp', '.gif', '.nef'}
VID_EXTS = {'.mov', '.mp4', '.mts', '.vob', '.avi', '.wmv'}


def main():
    disk = []
    for base, kind in ((PHOTO_ROOT, '照片'), (VIDEO_ROOT, '视频')):
        exts = IMG_EXTS if kind == '照片' else VID_EXTS
        for root, dirs, fns in os.walk(base):
            for fn in fns:
                if os.path.splitext(fn)[1].lower() in exts:
                    disk.append((os.path.join(root, fn), kind))
    disk.sort()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    rows = conn.execute(
        "SELECT payload FROM review_queue "
        "WHERE json_extract(payload,'$.src') LIKE '/vol1/1000/收件箱/媒体库/%'"
    ).fetchall()
    conn.close()
    existing = set()
    for (p,) in rows:
        try:
            d = json.loads(p) if p else {}
        except Exception:
            continue
        if d.get('src'):
            existing.add(d['src'])
    new = []
    gone_meta = 0
    for path, kind in disk:
        if path in existing:
            continue
        try:
            st = os.stat(path)
        except OSError:
            gone_meta += 1
            continue
        new.append((path, st.st_size, int(st.st_mtime), kind))
    with open(OUT, 'w', encoding='utf-8') as fh:
        for path, size, mtime, kind in new:
            fh.write('%s\t%d\t%d\t%s\n' % (path, size, mtime, kind))
    n_photo = sum(1 for x in new if x[3] == '照片')
    n_video = sum(1 for x in new if x[3] == '视频')
    print('disk media files: %d (photo %d, video %d)' % (len(disk),
          sum(1 for _, k in disk if k == '照片'),
          sum(1 for _, k in disk if k == '视频')))
    print('existing review rows under inbox/media: %d' % len(existing))
    print('snapshot new files: %d (photo %d, video %d) -> %s' % (
        len(new), n_photo, n_video, OUT))
    print('stat-failed during snapshot: %d' % gone_meta)


if __name__ == '__main__':
    main()
