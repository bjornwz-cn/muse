#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Midconf step 1e: keep-best（容器内，写 DB payload 仅限 2,254 照片行）。
对 2,223 个有旧 VLM 结论的行，比较 stage-2 agent-plan 新结论与旧结论：
- 新 vlm_confidence >= 旧 vlm_confidence：保留新的。
- 新的 < 旧的、或新的缺失（未进确认集 / 确认失败 / 解码失败）：把旧的
  5 个 vlm 键恢复回去，并把 decided_by 恢复为旧 vlm_source 值。
31 个无旧结论的行按新结论为准（不动）。
核对：2,254 行每行都有 vlm_category（除非新旧结论都没有），且没有任何
一行的 vlm_confidence 比旧的低。
"""
import sys, os, json, sqlite3

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")
try:
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None

DB = "/data/nas-curator.db"
OLD = "/data/midconf_vlm_old.json"
CONFIRM_SUMMARY = "/data/year_vlm_summary_midconf_confirm.json"
PHOTO_IDS = "/data/midconf_all_photo_rqids.txt"
KEYS = ["vlm_category", "vlm_confidence", "vlm_reason", "vlm_model", "vlm_source"]

def hb(**kw):
    if _write_hb is None:
        return
    try:
        _write_hb("midconf-reclassify", progress_dir="/data/progress", **kw)
    except Exception as e:
        print("heartbeat failed: %s" % e, flush=True)

def main():
    hb(label="中置信重分类", kind="midconf-reclassify", phase="keep-best",
       status="running", total=2223, done=0)
    old = json.load(open(OLD, encoding="utf-8"))
    assert len(old) == 2223
    # NOTE: confirm ran in two runs (stopped 06:45 for the Alibaba wiring,
    # relaunched 06:49); the summary file covers only the 2nd run (650 ok).
    # Decisions below are DB-driven, so both runs count. DB is authoritative.
    new_by_id = {}
    photo_ids = [int(x) for x in open(PHOTO_IDS, encoding="utf-8").read().replace(",", " ").split() if x.strip()]
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    kept_new = restored = restored_missing = restored_lower = 0
    new_lower_rows = []
    no_old_new_ok = 0
    for rid in photo_ids:
        row = conn.execute("SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()
        d = json.loads(row[0])
        key = str(rid)
        if key not in old:
            if "vlm_category" in d:
                no_old_new_ok += 1
            continue
        o = old[key]
        try:
            oconf = float(o.get("vlm_confidence") or 0)
        except Exception:
            oconf = 0.0
        if "vlm_category" in d:
            try:
                nconf = float(d.get("vlm_confidence") or 0)
            except Exception:
                nconf = 0.0
            # 新结论必须是 agent-plan（标准版）；经济版模型绝不能留作最终结论
            if "economy" in str(d.get("vlm_model") or ""):
                raise AssertionError("economy model left as final vlm on rq=%d" % rid)
            if nconf >= oconf:
                kept_new += 1
                continue
            restored += 1
            restored_lower += 1
            new_lower_rows.append((rid, nconf, oconf))
        else:
            restored += 1
            restored_missing += 1
        for k in KEYS:
            if k in o:
                d[k] = o[k]
            else:
                d.pop(k, None)
        conn.execute("UPDATE review_queue SET payload=?, decided_by=? WHERE id=?",
                     (json.dumps(d, ensure_ascii=False), o.get("vlm_source") or "vlm-midconf", rid))
    conn.commit()
    # verify
    no_vlm = []
    lower_than_old = []
    for rid in photo_ids:
        row = conn.execute("SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()
        d = json.loads(row[0])
        key = str(rid)
        if "vlm_category" not in d:
            no_vlm.append(rid)
        elif key in old:
            try:
                if float(d.get("vlm_confidence") or 0) < float(old[key].get("vlm_confidence") or 0):
                    lower_than_old.append(rid)
            except Exception:
                pass
    print("kept_new=%d restored=%d (missing_new=%d lower_new=%d) no_old_with_new=%d" % (
        kept_new, restored, restored_missing, restored_lower, no_old_new_ok))
    print("rows without any vlm_category after keep-best: %d %s" % (len(no_vlm), no_vlm[:20]))
    print("rows with vlm_confidence lower than old: %d %s" % (len(lower_than_old), lower_than_old[:20]))
    assert not lower_than_old, "some rows ended lower than old"
    conn.close()
    hb(label="中置信重分类", kind="midconf-reclassify", phase="keep-best",
       status="running", total=2223, done=2223)
    print("KEEPBEST DONE kept_new=%d restored=%d no_vlm=%d" % (kept_new, restored, len(no_vlm)))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
