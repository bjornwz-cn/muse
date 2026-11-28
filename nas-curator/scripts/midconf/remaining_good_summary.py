#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剩余视频任务 收尾：仅对 184 个完好视频（仍 pending）按 analyze_pending 同口径汇总最终 band。"""
import sqlite3, json, csv

db = sqlite3.connect("/data/nas-curator.db")
good = []
with open("/data/remaining_good_videos.tsv", encoding="utf-8") as f:
    rd = csv.DictReader(f, delimiter="\t")
    for r in rd:
        good.append(int(r["rq_id"]))
band = {"ge85": 0, "b72_85": 0, "b35_72": 0, "none": 0}
cats = {}
sources = {}
for rid in good:
    row = db.execute("SELECT status, payload FROM review_queue WHERE id=?", (rid,)).fetchone()
    assert row and row[0] == "pending", (rid, row[0] if row else None)
    d = json.loads(row[1])
    cat, conf, src = None, 0.0, "siglip"
    vmodel = str(d.get("vlm_model") or "")
    if "vlm_category" in d and "economy" not in vmodel.lower():
        try:
            vconf = float(d.get("vlm_confidence") or 0)
        except Exception:
            vconf = 0.0
        if vconf >= 0.72:
            cat, conf, src = d["vlm_category"], vconf, "vlm"
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
    cats[cat] = cats.get(cat, 0) + 1
    sources[src] = sources.get(src, 0) + 1
print("good-only final band:", json.dumps(band, ensure_ascii=False))
print("good-only categories:", json.dumps(cats, ensure_ascii=False))
print("good-only sources:", json.dumps(sources, ensure_ascii=False))
