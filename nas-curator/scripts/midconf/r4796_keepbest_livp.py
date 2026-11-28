#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剩余视频任务 步骤 2C：keep-best（容器内，写 DB payload 仅限清单行）。
对有旧 VLM 结论的行，比较新结论与旧结论：
- 新 vlm_confidence >= 旧 vlm_confidence：保留新的。
- 新的 < 旧的、或新的缺失：恢复旧的 5 个 vlm 键，decided_by 恢复为旧 vlm_source。
  但旧结论若为经济版模型（vlm_model 含 economy），绝不恢复为最终结论——
  该行留空（无 vlm_category，走 SigLIP 兜底），计入 restored_economy_skipped。
- 新结论含 economy 直接报错（不应发生：两通道均已硬门控）。
无旧结论的行按新结论为准（不动）。
核对：没有任何一行的最终 vlm_confidence 比旧（非经济版）结论低。
用法：docker exec nas-curator python3 /data/remaining_keepbest.py
"""
import sys, os, json, sqlite3

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")
try:
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None

DB = "/data/nas-curator.db"
OLD = "/data/r4796_livp_vlm_old.json"
LIST = "/data/r4796_livp_vlm.tsv"
KEYS = ["vlm_category", "vlm_confidence", "vlm_reason", "vlm_model", "vlm_source"]


def hb(**kw):
    if _write_hb is None:
        return
    try:
        _write_hb("r4796-livp-vlm", progress_dir="/data/progress", **kw)
    except Exception as e:
        print("heartbeat failed: %s" % e, flush=True)


def main():
    old = json.load(open(OLD, encoding="utf-8"))
    ids = []
    with open(LIST, encoding="utf-8") as f:
        hdr = f.readline().rstrip("\n").split("\t")
        idx = {c: i for i, c in enumerate(hdr)}
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 2 and p[idx["rq_id"]].isdigit():
                ids.append(int(p[idx["rq_id"]]))
    print("scope ids: %d ; old backup rows: %d" % (len(ids), len(old)), flush=True)
    hb(label="剩余视频 VLM 重分类", kind="r4796-livp-vlm",
       phase="keep-best", status="running", total=len(ids), done=0)
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    kept_new = restored = restored_missing = restored_lower = 0
    restored_econ_skip = 0
    no_old_new_ok = 0
    new_lower_rows = []
    for rid in ids:
        row = conn.execute("SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()
        if not row:
            continue
        d = json.loads(row[0])
        key = str(rid)
        if "economy" in str(d.get("vlm_model") or ""):
            raise AssertionError("economy model left as final vlm on rq=%d" % rid)
        if key not in old:
            if "vlm_category" in d:
                no_old_new_ok += 1
            continue
        o = old[key]
        old_is_econ = "economy" in str(o.get("vlm_model") or "")
        try:
            oconf = float(o.get("vlm_confidence") or 0)
        except Exception:
            oconf = 0.0
        if "vlm_category" in d:
            try:
                nconf = float(d.get("vlm_confidence") or 0)
            except Exception:
                nconf = 0.0
            if nconf >= oconf:
                kept_new += 1
                continue
            restored += 1
            restored_lower += 1
            new_lower_rows.append((rid, nconf, oconf))
        else:
            restored += 1
            restored_missing += 1
        if old_is_econ:
            # 旧结论是经济版：不恢复为最终结论，留空走 SigLIP
            restored_econ_skip += 1
            for k in KEYS:
                d.pop(k, None)
            conn.execute("UPDATE review_queue SET payload=? WHERE id=?",
                         (json.dumps(d, ensure_ascii=False), rid))
            continue
        for k in KEYS:
            if k in o:
                d[k] = o[k]
            else:
                d.pop(k, None)
        conn.execute("UPDATE review_queue SET payload=?, decided_by=? WHERE id=?",
                     (json.dumps(d, ensure_ascii=False), o.get("vlm_source") or "vlm-remaining-frame", rid))
    conn.commit()
    no_vlm = []
    lower_than_old = []
    for rid in ids:
        row = conn.execute("SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()
        if not row:
            continue
        d = json.loads(row[0])
        key = str(rid)
        if "vlm_category" not in d:
            no_vlm.append(rid)
        elif key in old and "economy" not in str(old[key].get("vlm_model") or ""):
            try:
                if float(d.get("vlm_confidence") or 0) < float(old[key].get("vlm_confidence") or 0):
                    lower_than_old.append(rid)
            except Exception:
                pass
    print("kept_new=%d restored=%d (missing_new=%d lower_new=%d) restored_economy_skipped=%d no_old_with_new=%d" % (
        kept_new, restored, restored_missing, restored_lower, restored_econ_skip, no_old_new_ok))
    print("rows without any vlm_category after keep-best: %d %s" % (len(no_vlm), no_vlm[:20]))
    print("rows with vlm_confidence lower than old (non-economy): %d %s" % (len(lower_than_old), lower_than_old[:20]))
    assert not lower_than_old, "some rows ended lower than old"
    conn.close()
    hb(label="剩余视频 VLM 重分类", kind="r4796-livp-vlm",
       phase="keep-best", status="done", total=len(ids), done=len(ids))
    print("KEEPBEST DONE kept_new=%d restored=%d econ_skipped=%d no_vlm=%d" % (
        kept_new, restored, restored_econ_skip, len(no_vlm)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
