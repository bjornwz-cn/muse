#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""remaining-4796 任务 1 keep-best（容器内，写 DB payload 仅限 72-85 带行）。
对有旧 VLM 结论的行，比较新结论（agent-plan 或阿里 qwen4-max）与旧结论：
- 新 vlm_confidence >= 旧 vlm_confidence：保留新的。
- 新的 < 旧的、或新的缺失：把旧的 5 个 vlm 键恢复回去，并把 decided_by
  恢复为旧 vlm_source 值。无旧结论的行按新结论为准（不动）。
新结论绝不能是经济版（economy 模型出现即报错退出）。
核对：每行都有 vlm_category（除非新旧结论都没有），且没有任何
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
OLD = "/data/remaining4796_72_85_vlm_old.json"
PHOTO_IDS = "/data/remaining4796_72_85_ids.txt"
KEYS = ["vlm_category", "vlm_confidence", "vlm_reason", "vlm_model", "vlm_source"]


def hb(**kw):
    if _write_hb is None:
        return
    try:
        _write_hb("remaining4796-photos-reclassify", progress_dir="/data/progress", **kw)
    except Exception as e:
        print("heartbeat failed: %s" % e, flush=True)


def main():
    old = json.load(open(OLD, encoding="utf-8"))
    hb(label="剩余 4796 照片 72-85 重分类", kind="remaining4796-photos-reclassify", phase="keep-best",
       status="running", total=len(old), done=0)
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
                     (json.dumps(d, ensure_ascii=False), o.get("vlm_source") or "vlm-remaining", rid))
    conn.commit()
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
    hb(label="剩余 4796 照片 72-85 重分类", kind="remaining4796-photos-reclassify", phase="keep-best",
       status="done", total=len(photo_ids), done=len(photo_ids))
    print("KEEPBEST-4796 DONE kept_new=%d restored=%d no_vlm=%d" % (kept_new, restored, len(no_vlm)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
