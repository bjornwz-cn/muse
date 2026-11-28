#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch5 reclassification summary (read-only).

汇总收件箱全域无 review 行文件的第五批分类结果，写
  /data/year_reclassify_summary_batch5.json
并打印人类可读的分布表。不写 DB、不移动文件。

最终类目口径（每文件）：
- 有 vlm_category 且 vlm_confidence>=0.72 且非经济版模型 → 用 vlm_category。
- 否则 kind=文档：doc_party 为真实所属方（非经济版结论）→ 用 doc_party。
- 否则用 SigLIP 的 category（须为真实类目）。
- 否则为“待确认”。
最终置信度取提供最终类目那一方的置信度。
"""
import sys, os, json, sqlite3
from collections import Counter

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

DB_PATH = "/data/nas-curator.db"
OUT = "/data/year_reclassify_summary_batch5.json"
SNAPSHOT = "/data/year_batch5_snapshot.tsv"
PROGRESS_DIR = "/data/progress"

INBOX_ROOT = "/vol1/1000/收件箱"
try:
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None
REAL_CATS = {'人物合影', '儿童成长', '自拍', '旅行风景', '城市与建筑', '日常生活',
             '美食与咖啡', '宠物与动物', '工作与活动', '屏幕截图', '文档与票据',
             '梗图与表情', '聚会与夜生活', '运动与活动', '屏幕录制'}
PARTIES = {'1-王震', '2-上海润心', '3-北京供应链', '4-辽宁器械'}


def band(conf):
    if conf is None:
        return 'none'
    if conf >= 0.85:
        return 'ge85'
    if conf >= 0.72:
        return '72-85'
    if conf >= 0.35:
        return '35-72'
    return 'lt35'


def main():
    snap_kind = {}
    snap_ext = Counter()
    with open(SNAPSHOT, encoding='utf-8') as _fh:
        for _line in _fh:
            _p = _line.rstrip('\n').split('\t')
            if _p:
                snap_kind[_p[0]] = _p[3] if len(_p) >= 4 else ''
    disk_total = 0
    disk_by_ext = Counter()
    for root, _dirs, fns in os.walk(INBOX_ROOT):
        for fn in fns:
            disk_total += 1
            disk_by_ext[os.path.splitext(fn)[1].lower() or '(no ext)'] += 1
    conn = sqlite3.connect(DB_PATH, timeout=30)
    rows = conn.execute(
        "SELECT id, status, kind, payload FROM review_queue "
        "WHERE json_extract(payload,'$.src') LIKE '/vol1/1000/收件箱/%'"
    ).fetchall()
    conn.close()
    by_cat_photo = Counter()
    by_cat_video = Counter()
    by_cat_doc = Counter()
    by_kind_total = Counter()
    by_kind_classified = Counter()
    bands = Counter()
    bands_classified = Counter()
    siglip_ge85 = 0
    vlm_done = 0
    vlm_ge85 = 0
    vlm_economy = 0
    doc_done = 0
    doc_economy = 0
    unclassified = 0
    unclassified_by_kind = Counter()
    unclassified_by_why = Counter()
    unclassified_list = []
    n_rows = 0
    n_still_on_disk = 0
    n_moved_elsewhere = 0
    for rid, status, kind, payload in rows:
        try:
            d = json.loads(payload) if payload else {}
        except Exception:
            continue
        src = d.get('src') or ''
        # 注意：汇总时不以 os.path.exists 过滤——本批执行期间收件箱/媒体库
        # 整树被他方移走（见完成记录），快照文件仍按快照口径全数统计；
        # 是否仍在原路径另计 still_on_disk / moved_elsewhere。
        if not src or src not in snap_kind:
            continue
        n_rows += 1
        if os.path.exists(src):
            n_still_on_disk += 1
        else:
            n_moved_elsewhere += 1
        skind = snap_kind[src]
        by_kind_total[skind] += 1
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
        vmodel = d.get('vlm_model') or ''
        if vcat:
            vlm_done += 1
            if vconf >= 0.85:
                vlm_ge85 += 1
            if 'economy' in str(vmodel):
                vlm_economy += 1
                vcat = None
                vconf = 0.0
        doc_party = d.get('doc_party') or ''
        doc_model = d.get('doc_model') or ''
        if doc_party:
            doc_done += 1
            if 'economy' in str(doc_model):
                doc_economy += 1
                doc_party = ''
        # 最终类目 + 最终置信度
        final = None
        final_conf = None
        if vcat and vconf >= 0.72:
            final, final_conf = vcat, vconf
        elif skind == '文档' and doc_party in PARTIES:
            final, final_conf = doc_party, conf
        elif d.get('category') in REAL_CATS:
            final, final_conf = d['category'], conf
        bands[band(final_conf)] += 1
        if final:
            by_kind_classified[skind] += 1
            bands_classified[band(final_conf)] += 1
            if skind == '照片':
                by_cat_photo[final] += 1
            elif skind == '视频':
                by_cat_video[final] += 1
            elif skind == '文档':
                by_cat_doc[final] += 1
            else:
                by_cat_doc[final] += 1
        else:
            unclassified += 1
            unclassified_by_kind[skind] += 1
            why = (d.get('reason') or '')[:28]
            unclassified_by_why[why] += 1
            if len(unclassified_list) < 50:
                unclassified_list.append(src)
    summary = {
        'batch': 'batch5-inbox-no-review',
        'snapshot_total': len(snap_kind),
        'snapshot_disk_total': 26124,
        'generated_at': __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'disk_total_files': disk_total,
        'disk_by_ext_top': dict(disk_by_ext.most_common(40)),
        'review_rows_under_inbox': len(rows),
        'review_rows_on_disk': n_rows,
        'review_rows_still_on_disk': n_still_on_disk,
        'review_rows_moved_elsewhere': n_moved_elsewhere,
        'by_kind_total': dict(by_kind_total),
        'by_kind_classified': dict(by_kind_classified),
        'classified_total': sum(by_kind_classified.values()),
        'unclassified': unclassified,
        'unclassified_by_kind': dict(unclassified_by_kind),
        'unclassified_by_reason_top': dict(unclassified_by_why.most_common(20)),
        'final_conf_bands_all': dict(bands),
        'final_conf_bands_classified': dict(bands_classified),
        'siglip_ge85': siglip_ge85,
        'vlm_done': vlm_done,
        'vlm_ge85': vlm_ge85,
        'vlm_economy_excluded': vlm_economy,
        'doc_done': doc_done,
        'doc_economy_excluded': doc_economy,
        'by_category_photo': dict(sorted(by_cat_photo.items())),
        'by_category_video': dict(sorted(by_cat_video.items())),
        'by_category_doc': dict(sorted(by_cat_doc.items())),
        'unclassified_sample': unclassified_list,
    }
    with open(OUT, 'w', encoding='utf-8') as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if _write_hb is not None:
        try:
            _write_hb("year-reclassify-batch5", progress_dir=PROGRESS_DIR,
                      label="第五批分类（收件箱无 review 行）", kind="year-reclassify",
                      phase="summary", status="done",
                      total=len(snap_kind), done=n_rows,
                      extra={"unclassified": unclassified,
                             "vlm_done": vlm_done, "vlm_ge85": vlm_ge85,
                             "siglip_ge85": siglip_ge85})
        except Exception as e:  # noqa: BLE001
            print("heartbeat failed: %s" % e)


if __name__ == '__main__':
    main()
