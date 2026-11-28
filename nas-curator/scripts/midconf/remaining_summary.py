#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剩余视频任务 汇总（容器内，只读 DB）：
对 /data/remaining_videos.tsv 的 444 行，按 analyze_pending 同口径汇总最终类目：
VLM>=0.72 用 VLM（经济版除外——vlm_model 含 economy 的行按无 VLM 处理），
否则用 SigLIP 真实类目（payload category/confidence）。
输出 /data/remaining_videos_final_summary.json，并打印 review 计数、integrity、
收件箱不在此脚本内（宿主机侧另查）。
用法：docker exec nas-curator python3 /data/remaining_summary.py
"""
import json, os, sqlite3

DB = "/data/nas-curator.db"
LIST = "/data/remaining_videos.tsv"
OUT = "/data/remaining_videos_final_summary.json"


def main():
    ids = []
    with open(LIST, encoding="utf-8") as f:
        hdr = f.readline().rstrip("\n").split("\t")
        idx = {c: i for i, c in enumerate(hdr)}
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 2 and p[idx["rq_id"]].isdigit():
                ids.append(int(p[idx["rq_id"]]))
    db = sqlite3.connect(DB)
    print("integrity:", db.execute("PRAGMA integrity_check").fetchone()[0], flush=True)
    counts = dict(db.execute(
        "SELECT status, COUNT(*) FROM review_queue GROUP BY status").fetchall())
    print("review counts:", json.dumps(counts, ensure_ascii=False), flush=True)
    rows = {}
    for rid in ids:
        r = db.execute(
            "SELECT status, decided_by, payload FROM review_queue WHERE id=?",
            (rid,)).fetchone()
        if r:
            rows[rid] = r
    band = {"ge85": 0, "b72_85": 0, "b35_72": 0, "none": 0}
    cats = {}
    sources = {}
    vlm_rows = 0
    vlm_ge85 = 0
    vlm_models = {}
    status_counts = {}
    for rid, (status, decided_by, payload) in rows.items():
        status_counts[status] = status_counts.get(status, 0) + 1
        d = json.loads(payload)
        cat = None
        conf = 0.0
        src = "siglip"
        vmodel = str(d.get("vlm_model") or "")
        if "vlm_category" in d and "economy" not in vmodel.lower():
            try:
                vconf = float(d.get("vlm_confidence") or 0)
            except Exception:
                vconf = 0.0
            if vconf >= 0.72:
                cat, conf, src = d["vlm_category"], vconf, "vlm"
                vlm_rows += 1
                vlm_models[vmodel] = vlm_models.get(vmodel, 0) + 1
                if vconf >= 0.85:
                    vlm_ge85 += 1
        if cat is None:
            cat = d.get("category")
            try:
                conf = float(d.get("confidence") or 0)
            except Exception:
                conf = 0.0
        if conf >= 0.85:
            band["ge85"] += 1
        elif conf >= 0.72:
            band["b72_85"] += 1
        elif conf >= 0.35:
            band["b35_72"] += 1
        else:
            band["none"] += 1
        if cat:
            cats[cat] = cats.get(cat, 0) + 1
        sources[src] = sources.get(src, 0) + 1
    summ = {"scope_n": len(ids), "rows_found": len(rows),
            "review_counts": counts, "status_in_scope": status_counts,
            "final_band": band, "final_categories": cats,
            "final_sources": sources,
            "vlm_final_rows": vlm_rows, "vlm_ge85": vlm_ge85,
            "vlm_models_final": vlm_models}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print("SUMMARY " + json.dumps(summ, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
