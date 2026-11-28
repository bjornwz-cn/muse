#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剩余视频任务 步骤 2B-0：旧 VLM 结论备份＋清空（容器内，写 DB payload 仅限清单行）。
对 /data/remaining_good_videos.tsv 清单内已有 vlm_category 的行：
1. 5 个 vlm 键备份到 /data/remaining_vlm_old.json；
2. 只删这 5 个键（vlm_category/vlm_confidence/vlm_reason/vlm_model/vlm_source），
   不动 status/decided_by 与其他 payload 字段；vlm_economy_* 保留不动。
用法：docker exec nas-curator python3 /data/remaining_backup_old_vlm.py
"""
import sys, os, json, sqlite3

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")
try:
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None

DB = "/data/nas-curator.db"
LIST = "/data/remaining_good_videos.tsv"
OUT = "/data/remaining_vlm_old.json"
KEYS = ["vlm_category", "vlm_confidence", "vlm_reason", "vlm_model", "vlm_source"]


def hb(**kw):
    if _write_hb is None:
        return
    try:
        _write_hb("remaining-videos-vlm", progress_dir="/data/progress", **kw)
    except Exception as e:
        print("heartbeat failed: %s" % e, flush=True)


def main():
    ids = []
    with open(LIST, encoding="utf-8") as f:
        hdr = f.readline().rstrip("\n").split("\t")
        idx = {c: i for i, c in enumerate(hdr)}
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 2 and p[idx["rq_id"]].isdigit():
                ids.append(int(p[idx["rq_id"]]))
    print("scope ids: %d" % len(ids), flush=True)
    hb(label="剩余视频 VLM 重分类", kind="remaining-videos-vlm",
       phase="backup-old-vlm", status="running", total=len(ids), done=0)
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    backup = {}
    for rid in ids:
        row = conn.execute(
            "SELECT payload FROM review_queue WHERE id=? AND status='pending'",
            (rid,)).fetchone()
        if not row:
            print("skip rq=%d row_gone" % rid, flush=True)
            continue
        d = json.loads(row[0])
        if "vlm_category" in d:
            backup[str(rid)] = {k: d.get(k) for k in KEYS if k in d}
    print("backup rows: %d" % len(backup), flush=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(backup, f, ensure_ascii=False, indent=1)
    print("wrote %s (%d bytes, %d rows)" % (OUT, os.path.getsize(OUT), len(backup)))
    changed = 0
    for rid in ids:
        row = conn.execute(
            "SELECT payload FROM review_queue WHERE id=? AND status='pending'",
            (rid,)).fetchone()
        if not row:
            continue
        d = json.loads(row[0])
        if any(k in d for k in KEYS):
            for k in KEYS:
                d.pop(k, None)
            conn.execute("UPDATE review_queue SET payload=? WHERE id=? AND status='pending'",
                         (json.dumps(d, ensure_ascii=False), rid))
            changed += 1
    conn.commit()
    remaining = 0
    for rid in ids:
        row = conn.execute("SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()
        if row and "vlm_category" in json.loads(row[0]):
            remaining += 1
    print("rows updated (keys removed): %d ; rows still with vlm_category: %d" % (changed, remaining))
    assert remaining == 0, "vlm_category remains on %d rows" % remaining
    conn.close()
    back = json.load(open(OUT, encoding="utf-8"))
    print("backup re-read rows: %d" % len(back))
    assert len(back) == len(backup)
    hb(label="剩余视频 VLM 重分类", kind="remaining-videos-vlm",
       phase="backup-old-vlm", status="done", total=len(ids), done=len(ids))
    print("BACKUP-OLD-VLM DONE backup=%d cleared=%d" % (len(backup), changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
