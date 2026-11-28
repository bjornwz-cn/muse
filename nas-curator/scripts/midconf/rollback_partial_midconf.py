#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Midconf rollback of the stopped single-stage run (container-side):
The 06:16 single-stage agent-plan run was stopped per the 06:16 two-stage
instruction. It had already written vlm_* conclusions to some rows
(decided_by='vlm-midconf', vlm_source='vlm-midconf').
Delete ONLY the 5 vlm keys from exactly those rows, restoring them to the
cleared state (as after step 1b), so the two-stage pipeline starts clean.
Do not touch status/decided_by? -- decided_by stays 'vlm-midconf' on those
rows (harmless marker of the rolled-back attempt; stage-2 write overwrites
it). Actually reset decided_by too? NO: keep decided_by unchanged to avoid
rewriting history; stage-2 UPDATE sets decided_by='vlm-midconf' anyway on
rows it confirms, and keep-best restore leaves decided_by as-is. The
rollback is verified by vlm_category absence, not decided_by.
Verify afterwards: 0 of the 2,254 photo rows has vlm_category; count rows
rolled back; count decided_by='vlm-midconf' rows in whole DB.
"""
import sys, os, json, sqlite3

DB = "/data/nas-curator.db"
IDS_FILE = "/data/midconf_all_photo_rqids.txt"
KEYS = ["vlm_category", "vlm_confidence", "vlm_reason", "vlm_model", "vlm_source"]

def main():
    ids = [int(x) for x in open(IDS_FILE, encoding="utf-8").read().replace(",", " ").split() if x.strip()]
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    total_midconf_decided = conn.execute(
        "SELECT count(*) FROM review_queue WHERE decided_by='vlm-midconf'").fetchone()[0]
    print("db rows decided_by='vlm-midconf' (whole db): %d" % total_midconf_decided)
    rolled = 0
    had_old = 0
    old_backup = json.load(open("/data/midconf_vlm_old.json", encoding="utf-8"))
    for rid in ids:
        row = conn.execute("SELECT payload, decided_by FROM review_queue WHERE id=?", (rid,)).fetchone()
        if not row:
            continue
        d = json.loads(row[0])
        if row[1] == "vlm-midconf" and "vlm_category" in d:
            if str(rid) in old_backup:
                had_old += 1
            for k in KEYS:
                d.pop(k, None)
            conn.execute("UPDATE review_queue SET payload=? WHERE id=?",
                         (json.dumps(d, ensure_ascii=False), rid))
            rolled += 1
    conn.commit()
    remaining = 0
    for rid in ids:
        row = conn.execute("SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()
        if "vlm_category" in json.loads(row[0]):
            remaining += 1
    print("rolled back rows: %d (of which had old backup: %d)" % (rolled, had_old))
    print("photo rows still with vlm_category: %d" % remaining)
    assert remaining == 0
    # any vlm_category left on decided_by='vlm-midconf' rows anywhere?
    n = conn.execute("SELECT count(*) FROM review_queue WHERE decided_by='vlm-midconf' "
                     "AND json_extract(payload,'$.vlm_category') IS NOT NULL").fetchone()[0]
    print("db-wide decided_by='vlm-midconf' rows still with vlm_category: %d" % n)
    assert n == 0
    conn.close()
    print("ROLLBACK DONE rolled=%d" % rolled)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
