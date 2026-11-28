#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Midconf step 1a/1b (container-side):
1. Backup the 5 vlm keys of the 2,223 photo rows that already have a VLM
   conclusion into /data/midconf_vlm_old.json; verify count == 2223.
2. Delete ONLY those 5 keys (vlm_category/vlm_confidence/vlm_reason/
   vlm_model/vlm_source) from payload; do not touch status/decided_by
   or any other payload field.
3. Verify afterwards that none of the 2,223 rows still has vlm_category.
Writes DB (payload only). No file moves/deletes.
"""
import sys, os, json, sqlite3

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")
try:
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None

DB = "/data/nas-curator.db"
IDS_FILE = "/data/midconf_photos_with_vlm_rqids.txt"
OUT = "/data/midconf_vlm_old.json"
KEYS = ["vlm_category", "vlm_confidence", "vlm_reason", "vlm_model", "vlm_source"]

def hb(**kw):
    if _write_hb is None:
        return
    try:
        _write_hb("midconf-reclassify", progress_dir="/data/progress", **kw)
    except Exception as e:
        print("heartbeat failed: %s" % e, flush=True)

def main():
    hb(label="中置信重分类", kind="midconf-reclassify", phase="backup-old-vlm",
       status="running", total=2223, done=0)
    ids = [int(x) for x in open(IDS_FILE, encoding="utf-8").read().replace(",", " ").split() if x.strip()]
    print("ids from file: %d (unique %d)" % (len(ids), len(set(ids))))
    assert len(ids) == 2223 and len(set(ids)) == 2223, "expected 2223 unique ids"
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    backup = {}
    missing_vlm = []
    for rid in ids:
        row = conn.execute("SELECT payload FROM review_queue WHERE id=? AND status='pending'", (rid,)).fetchone()
        assert row, "row gone rid=%d" % rid
        d = json.loads(row[0])
        if "vlm_category" not in d:
            missing_vlm.append(rid)
            continue
        backup[str(rid)] = {k: d.get(k) for k in KEYS if k in d}
    print("backup rows: %d ; rows without vlm_category (unexpected): %d %s" % (len(backup), len(missing_vlm), missing_vlm[:10]))
    assert len(backup) == 2223, "backup count != 2223"
    assert not missing_vlm, "some listed rows lack vlm_category"
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(backup, f, ensure_ascii=False, indent=1)
    print("wrote %s (%d bytes, %d rows)" % (OUT, os.path.getsize(OUT), len(backup)))

    # 1b: delete only the 5 vlm keys
    changed = 0
    for rid in ids:
        row = conn.execute("SELECT payload FROM review_queue WHERE id=? AND status='pending'", (rid,)).fetchone()
        d = json.loads(row[0])
        if any(k in d for k in KEYS):
            for k in KEYS:
                d.pop(k, None)
            conn.execute("UPDATE review_queue SET payload=? WHERE id=? AND status='pending'",
                         (json.dumps(d, ensure_ascii=False), rid))
            changed += 1
    conn.commit()
    # verify
    remaining = 0
    for rid in ids:
        row = conn.execute("SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()
        d = json.loads(row[0])
        if "vlm_category" in d:
            remaining += 1
    print("rows updated (keys removed): %d ; rows still with vlm_category: %d" % (changed, remaining))
    assert remaining == 0, "vlm_category remains on %d rows" % remaining
    conn.close()
    # verify backup file re-read
    back = json.load(open(OUT, encoding="utf-8"))
    print("backup re-read rows: %d" % len(back))
    assert len(back) == 2223
    hb(label="中置信重分类", kind="midconf-reclassify", phase="backup-old-vlm",
       status="running", total=2223, done=2223)
    print("STEP1AB DONE backup=2223 cleared=2223")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
