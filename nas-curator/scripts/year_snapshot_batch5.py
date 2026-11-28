#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch5 snapshot: 收件箱全域当前在盘、且 review_queue 无对应行的文件。
只读 DB，只写快照 TSV。
输出 TSV 列：path<TAB>size<TAB>mtime<TAB>kind（与 batch3 快照同格式，无表头）。
kind 按扩展名：照片 / 视频 / 文档 / 其他。
用法（NAS 容器内）：docker exec nas-curator python3 /data/year_snapshot_batch5.py
"""
import sys, os, json, sqlite3
from collections import Counter

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

DB_PATH = "/data/nas-curator.db"
OUT = "/data/year_batch5_snapshot.tsv"
INBOX_ROOT = "/vol1/1000/收件箱"
IMG_EXTS = {'.jpg', '.jpeg', '.heic', '.png', '.heif', '.webp', '.gif', '.nef',
            '.tiff', '.jfif'}
VID_EXTS = {'.mov', '.mp4', '.mts', '.vob', '.avi', '.wmv'}
DOC_EXTS = {'.docx', '.doc', '.xlsx', '.xls', '.pdf', '.pptx', '.ppt',
            '.txt', '.md', '.csv', '.eml', '.html', '.webloc', '.zip',
            '.rar', '.eddx', '.emmx', '.osheet', '.odoc'}


def kind_of(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in IMG_EXTS:
        return '照片'
    if ext in VID_EXTS:
        return '视频'
    if ext in DOC_EXTS:
        return '文档'
    return '其他'


def main():
    disk = []
    for root, dirs, fns in os.walk(INBOX_ROOT):
        for fn in fns:
            disk.append(os.path.join(root, fn))
    disk.sort()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    rows = conn.execute(
        "SELECT payload FROM review_queue "
        "WHERE json_extract(payload,'$.src') LIKE '/vol1/1000/收件箱/%'"
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
    for path in disk:
        if path in existing:
            continue
        try:
            st = os.stat(path)
        except OSError:
            gone_meta += 1
            continue
        new.append((path, st.st_size, int(st.st_mtime), kind_of(path)))
    with open(OUT, 'w', encoding='utf-8') as fh:
        for path, size, mtime, kind in new:
            fh.write('%s\t%d\t%d\t%s\n' % (path, size, mtime, kind))
    by_kind = Counter(x[3] for x in new)
    by_top = Counter()
    by_ext = Counter()
    for path, _s, _m, _k in new:
        rel = os.path.relpath(path, INBOX_ROOT)
        by_top[rel.split(os.sep)[0] if os.sep in rel else '(root)'] += 1
        by_ext[os.path.splitext(path)[1].lower() or '(no ext)'] += 1
    # 媒体库下级分布
    by_media_sub = Counter()
    for path, _s, _m, _k in new:
        if path.startswith(INBOX_ROOT + '/媒体库/'):
            parts = os.path.relpath(path, INBOX_ROOT + '/媒体库').split(os.sep)
            by_media_sub[parts[0] if parts else '(root)'] += 1
    print('disk inbox files: %d' % len(disk))
    print('existing review rows under inbox: %d' % len(existing))
    print('snapshot new files: %d -> %s' % (len(new), OUT))
    print('by kind: %s' % dict(by_kind))
    print('by top dir: %s' % dict(by_top))
    print('by ext: %s' % dict(by_ext.most_common(40)))
    print('media subdirs: %s' % dict(by_media_sub))
    print('stat-failed during snapshot: %d' % gone_meta)


if __name__ == '__main__':
    main()
