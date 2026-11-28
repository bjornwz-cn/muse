#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Year-folder reclassification summary (read-only).

汇总收件箱/媒体库 年份文件夹的重分类结果，写
  /data/year_reclassify_summary.json
并打印人类可读的分布表。不写 DB、不移动文件。

最终类目口径（每文件）：
- 有 vlm_category 且 vlm_confidence>=0.72 → 用 vlm_category（VLM 为准）。
- 否则用 SigLIP 的 category（须为真实类目）。
- 否则为“待确认”（仍无法定类目）。
"""
import sys, os, json, sqlite3
from collections import Counter

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

DB_PATH = "/data/nas-curator.db"
OUT = "/data/year_reclassify_summary_batch3.json"
SNAPSHOT = "/data/year_batch3_snapshot.tsv"
PROGRESS_DIR = "/data/progress"

# 0.2.0 心跳：汇总跑完即把同一任务标识 year-reclassify 标 done。
try:
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None
PHOTO_ROOT = "/vol1/1000/收件箱/媒体库/照片"
VIDEO_ROOT = "/vol1/1000/收件箱/媒体库/视频"
IMG_EXTS = {'.jpg', '.jpeg', '.heic', '.png', '.heif', '.webp', '.gif', '.nef'}
VID_EXTS = {'.mov', '.mp4', '.mts', '.vob', '.avi', '.wmv'}
ALL_MEDIA = IMG_EXTS | VID_EXTS
REAL_CATS = {'人物合影', '儿童成长', '自拍', '旅行风景', '城市与建筑', '日常生活',
             '美食与咖啡', '宠物与动物', '工作与活动', '屏幕截图', '文档与票据',
             '梗图与表情', '聚会与夜生活', '运动与活动', '屏幕录制'}


def main():
    # 磁盘扫描
    disk = {'照片': Counter(), '视频': Counter()}
    excluded = Counter()
    total_disk = 0
    for base, kind in ((PHOTO_ROOT, '照片'), (VIDEO_ROOT, '视频')):
        for root, dirs, fns in os.walk(base):
            for fn in fns:
                ext = os.path.splitext(fn)[1].lower()
                p = os.path.join(root, fn)
                total_disk += 1
                if ext in ALL_MEDIA:
                    disk[kind]['total'] += 1
                else:
                    excluded[ext or '(no ext)'] += 1
    # DB：review 行
    conn = sqlite3.connect(DB_PATH, timeout=30)
    rows = conn.execute(
        "SELECT id, status, kind, payload FROM review_queue "
        "WHERE json_extract(payload,'$.src') LIKE '/vol1/1000/收件箱/媒体库/%'"
    ).fetchall()
    conn.close()
    # 本批快照集合：只统计快照内的 src
    snap_srcs = set()
    with open(SNAPSHOT) as _fh:
        for _line in _fh:
            _p = _line.rstrip('\n').split('\t')
            if _p:
                snap_srcs.add(_p[0])
    # 每文件最终类目
    by_cat_photo = Counter()
    by_cat_video = Counter()
    siglip_ge85 = 0
    vlm_done = 0
    vlm_ge85 = 0
    unclassified = 0
    unclassified_list = []
    n_rows = 0
    for rid, status, kind, payload in rows:
        try:
            d = json.loads(payload) if payload else {}
        except Exception:
            continue
        src = d.get('src') or ''
        if not src or src not in snap_srcs or not os.path.exists(src):
            continue
        n_rows += 1
        is_photo = '/照片/' in src
        conf = d.get('confidence')
        try:
            conf = float(conf) if conf is not None else 0.0
        except Exception:
            conf = 0.0
        if conf >= 0.85:
            siglip_ge85 += 1
        vcat = d.get('vlm_category')
        vconf = d.get('vlm_confidence')
        try:
            vconf = float(vconf) if vconf is not None else 0.0
        except Exception:
            vconf = 0.0
        if vcat:
            vlm_done += 1
            if vconf >= 0.85:
                vlm_ge85 += 1
        # 经济版（economy）结论只进复核、不参与最终类目判定：最终类目
        # 只采纳 agent-plan 的 VLM 结论，economy 行回落到 SigLIP 真实类目。
        vmodel = d.get('vlm_model') or ''
        if vcat and 'economy' in str(vmodel):
            vcat = None
            vconf = 0.0
        # 最终类目
        final = None
        if vcat and vconf >= 0.72:
            final = vcat
        elif d.get('category') in REAL_CATS:
            final = d['category']
        if final:
            (by_cat_photo if is_photo else by_cat_video)[final] += 1
        else:
            unclassified += 1
            if len(unclassified_list) < 50:
                unclassified_list.append(src)
    summary = {
        'batch': 'batch3-baidu-new',
        'snapshot_total': len(snap_srcs),
        'generated_at': __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'disk_total_files': total_disk,
        'disk_media_photo': disk['照片']['total'],
        'disk_media_video': disk['视频']['total'],
        'disk_excluded_by_ext': dict(excluded),
        'review_rows_under_inbox_media': len(rows),
        'review_rows_on_disk': n_rows,
        'siglip_ge85': siglip_ge85,
        'vlm_done': vlm_done,
        'vlm_ge85': vlm_ge85,
        'unclassified': unclassified,
        'by_category_photo': dict(sorted(by_cat_photo.items())),
        'by_category_video': dict(sorted(by_cat_video.items())),
        'unclassified_sample': unclassified_list,
    }
    with open(OUT, 'w') as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if _write_hb is not None:
        try:
            _write_hb("year-reclassify-batch3", progress_dir=PROGRESS_DIR,
                      label="第三批重分类（百度新到）", kind="year-reclassify",
                      phase="summary", status="done",
                      total=disk['照片']['total'] + disk['视频']['total'],
                      done=n_rows,
                      extra={"unclassified": unclassified,
                             "vlm_done": vlm_done, "vlm_ge85": vlm_ge85,
                             "siglip_ge85": siglip_ge85})
        except Exception as e:  # noqa: BLE001 - 心跳失败只打印，不影响汇总
            print("heartbeat failed: %s" % e)


if __name__ == '__main__':
    main()
