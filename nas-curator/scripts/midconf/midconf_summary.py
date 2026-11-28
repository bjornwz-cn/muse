#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Midconf step 3: summary (container-side, READ-ONLY vs DB).
Recompute final category/confidence for the 2,944 midconf rows with the
analyze_pending.py口径 (VLM>=0.72 use VLM, economy excluded; else SigLIP
real category; docs n/a here), write /data/midconf_reclassify_summary.json.
No DB writes, no approvals, no file moves/deletes.
"""
import json, os, sqlite3
from collections import Counter

DB = "/data/nas-curator.db"
LIST = "/data/midconf_list.tsv"
OUT = "/data/midconf_reclassify_summary.json"

REAL_CATS = {'人物合影', '儿童成长', '自拍', '旅行风景', '城市与建筑', '日常生活',
             '美食与咖啡', '宠物与动物', '工作与活动', '屏幕截图', '文档与票据',
             '梗图与表情', '聚会与夜生活', '运动与活动', '屏幕录制'}

def final_of(p):
    try:
        conf = float(p.get('confidence')) if p.get('confidence') is not None else 0.0
    except Exception:
        conf = 0.0
    vcat = p.get('vlm_category')
    try:
        vconf = float(p.get('vlm_confidence')) if p.get('vlm_confidence') is not None else 0.0
    except Exception:
        vconf = 0.0
    vmodel = p.get('vlm_model') or ''
    if vcat and 'economy' in str(vmodel):
        vcat, vconf = None, 0.0
    if vcat and vconf >= 0.72:
        return vcat, vconf, 'vlm'
    if p.get('category') in REAL_CATS:
        return p['category'], conf, 'siglip'
    return None, None, 'undecided'

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

def load_summary(path):
    if not os.path.exists(path):
        return None
    return json.load(open(path, encoding="utf-8"))

def main():
    rows = []
    with open(LIST, encoding="utf-8") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        idx = {c: i for i, c in enumerate(header)}
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if p and p[0]:
                rows.append((int(p[idx["rq_id"]]), p[idx["file_kind"]]))
    assert len(rows) == 2944
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=30)
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    status_counts = dict(conn.execute("SELECT status, count(*) FROM review_queue GROUP BY status").fetchall())
    per_kind = {}
    final_cats = Counter()
    final_source = Counter()
    bands = Counter()
    vlm_present = Counter()
    econ_present = 0
    undecided = []
    for rid, fkind in rows:
        row = conn.execute("SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()
        p = json.loads(row[0]) if row else {}
        fcat, fconf, src = final_of(p)
        bands[(fkind, band(fconf))] += 1
        final_source[(fkind, src)] += 1
        if fcat:
            final_cats[fcat] += 1
        else:
            undecided.append(rid)
        if p.get("vlm_category"):
            vlm_present[fkind] += 1
        if p.get("vlm_economy_category"):
            econ_present += 1
    conn.close()
    photo_ge85 = bands[("照片", "ge85")]
    video_ge85 = bands[("视频", "ge85")]
    total_ge85 = photo_ge85 + video_ge85
    # channel call accounting (from stage summaries; confirm ran twice, see notes)
    scr = load_summary("/data/year_vlm_summary_midconf_screen.json") or {}
    cnf2 = load_summary("/data/year_vlm_summary_midconf_confirm.json") or {}
    vscr = load_summary("/data/year_vlm_summary_midconf_video_screen.json") or {}
    vcnf = load_summary("/data/year_vlm_summary_midconf_video_confirm.json") or {}
    frames = load_summary("/data/midconf_video_frames_summary.json") or {}
    summary = {
        "n_total": 2944,
        "n_photo": 2254, "n_video": 690,
        "db_integrity": integrity,
        "review_status_counts": status_counts,
        "final_ge85_total": total_ge85,
        "final_ge85_photo": photo_ge85,
        "final_ge85_video": video_ge85,
        "final_bands": {"%s/%s" % k: v for k, v in sorted(bands.items())},
        "final_by_source": {"%s/%s" % k: v for k, v in sorted(final_source.items())},
        "final_categories": dict(final_cats),
        "undecided_n": len(undecided),
        "undecided_rq_ids": undecided,
        "vlm_category_present": dict(vlm_present),
        "vlm_economy_present": econ_present,
        "photo_screen": {k: scr.get(k) for k in ("n", "ok", "decode_failed", "api_error", "write_failed", "ge85", "ge72", "total_s")},
        "photo_confirm_run2": {k: cnf2.get(k) for k in ("n", "ok", "decode_failed", "api_error", "write_failed", "ge85", "total_s")},
        "photo_confirm_run1_note": "run1 stopped 06:45 for Alibaba wiring: 696 ok + 1 api_error (log overwritten by relaunch; DB is authoritative)",
        "photo_confirm_total_ok_db": 1346,
        "video_frames": {k: frames.get(k) for k in ("n", "ok", "src_missing", "probe_failed", "frame_failed", "total_s")},
        "video_screen": {k: vscr.get(k) for k in ("n", "ok", "decode_failed", "api_error", "write_failed", "ge85", "ge72", "total_s")},
        "video_confirm": {k: vcnf.get(k) for k in ("n", "ok", "decode_failed", "api_error", "write_failed", "ge85", "ge72", "total_s")},
        "channels": {
            "ark_agent_plan_calls": "see receipt (probe + partial single-stage 238 + confirm run1 697 + confirm run2 657 + video confirm)",
            "ark_model3_calls": "see receipt (probe + photo screen 2253 + video screen)",
            "alibaba_calls": 0,
            "alibaba_note": "channel blocked: provided key returned 401 InvalidApiKey on both the token-plan endpoint and DashScope; see receipt",
        },
        "fees": "agent-plan billing=free (subscription quota, out-of-pocket ¥0); model-3 billing=payg, amount per Ark console billing (call counts in receipt)",
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in summary.items() if k not in ("final_categories", "undecided_rq_ids")}, ensure_ascii=False, indent=1))
    print("final_categories: %s" % json.dumps(dict(final_cats), ensure_ascii=False))
    print("saved " + OUT)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
