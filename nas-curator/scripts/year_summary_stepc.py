#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤 C 复检汇总：按第四批同口径算最终类目分布。
对 recheck_batch='stepc' 的行：
  照片：vlm_confidence>=0.72 且有 vlm_category -> 用 VLM 类目；
        否则用 SigLIP 真实类目（category 非待确认/模糊/疑似重复）。
  视频：用 SigLIP 类目（视频不进 VLM）。
  文档：用 doc_party（所属方）。
  其余/无类目 -> 待确认。
输出 /data/year_reclassify_summary_stepc.json，并打印分布与计数。
只读 DB。
"""
import json
import sqlite3
from collections import Counter

DB = "/data/nas-curator.db"
OUT = "/data/year_reclassify_summary_stepc.json"

MEDIA_CATS = {"人物合影", "自拍", "儿童成长", "旅行风景", "城市与建筑",
              "美食与咖啡", "日常生活", "宠物与动物", "工作与活动",
              "屏幕截图", "文档与票据", "梗图与表情"}
PARTIES = {"1-王震", "2-上海润心", "3-北京供应链", "4-辽宁器械"}


def main():
    conn = sqlite3.connect(DB, timeout=30)
    rows = conn.execute(
        "SELECT id, kind, payload FROM review_queue "
        "WHERE json_extract(payload,'$.recheck_batch')='stepc'"
    ).fetchall()
    conn.close()
    final = Counter()
    conf_band = Counter()
    damaged = gone = 0
    by_kind = Counter()
    for rid, kind, payload in rows:
        d = json.loads(payload)
        by_kind[kind] += 1
        st = d.get("recheck_status", "")
        if st == "damaged":
            damaged += 1
        elif st == "gone":
            gone += 1
        cat = d.get("category", "")
        conf = float(d.get("confidence") or 0)
        vlm_cat = d.get("vlm_category")
        try:
            vlm_conf = float(d.get("vlm_confidence") or 0)
        except Exception:
            vlm_conf = 0.0
        party = d.get("doc_party", "")
        # 最终类目（第四批口径）
        if vlm_cat and vlm_conf >= 0.72:
            f = vlm_cat
        elif cat in MEDIA_CATS:
            f = cat
        elif party in PARTIES:
            f = party
        else:
            f = "待确认"
        final[f] += 1
        # 最终置信度段（VLM 优先，否则 SigLIP/文档置信度）
        fc = vlm_conf if (vlm_cat and vlm_conf >= 0.72) else conf
        if f == "待确认":
            conf_band["待确认"] += 1
        elif fc >= 0.85:
            conf_band[">=0.85"] += 1
        elif fc >= 0.72:
            conf_band["0.72-0.85"] += 1
        else:
            conf_band["<0.72"] += 1
    determined = sum(v for k, v in final.items() if k != "待确认")
    summary = {
        "total_rechecked": len(rows),
        "determined": determined,
        "pending": final.get("待确认", 0),
        "damaged_marked": damaged,
        "gone_marked": gone,
        "by_kind": dict(by_kind),
        "final_category": dict(final),
        "confidence_band": dict(conf_band),
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print("saved " + OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
