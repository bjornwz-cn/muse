#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Midconf final checks (container-side, read-only)."""
import json, sqlite3, os
from collections import Counter

DB = "/data/nas-curator.db"
LIST = "/data/midconf_list.tsv"

def main():
    ids = []
    with open(LIST, encoding="utf-8") as fh:
        fh.readline()
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if p and p[0]:
                ids.append(int(p[0]))
    print("midconf ids: %d" % len(ids))
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=30)
    print("integrity: %s" % conn.execute("PRAGMA integrity_check").fetchone()[0])
    print("status in 2944: %s" % dict(Counter(
        conn.execute("SELECT status FROM review_queue WHERE id=?", (i,)).fetchone()[0] for i in ids)))
    print("decided_by in 2944: %s" % dict(Counter(
        conn.execute("SELECT decided_by FROM review_queue WHERE id=?", (i,)).fetchone()[0] for i in ids)))
    econ_final = 0
    for i in ids:
        p = json.loads(conn.execute("SELECT payload FROM review_queue WHERE id=?", (i,)).fetchone()[0])
        if "economy" in str(p.get("vlm_model") or ""):
            econ_final += 1
    print("final vlm_model economy rows in 2944: %d" % econ_final)
    print("total status: %s" % dict(conn.execute("SELECT status, count(*) FROM review_queue GROUP BY status").fetchall()))
    has_plan = conn.execute("SELECT count(*) FROM sqlite_master WHERE name='plan_items'").fetchone()[0]
    if has_plan:
        print("max plan id: %s" % conn.execute("SELECT max(id) FROM plan_items").fetchone()[0])
    else:
        print("max plan id: n/a (no plan_items table)")
    conn.close()
    s = json.load(open("/data/year_vlm_summary_midconf_confirm.json", encoding="utf-8"))
    errs = [(r["rq_id"], (r.get("error") or "")[:150]) for r in s["results"] if r.get("status") == "api_error"]
    print("confirm-run2 api_error rq_ids: %s" % [e[0] for e in errs])
    for rid, err in errs:
        print("  rq=%d %s" % (rid, err))
    for name in ("midconf-screen", "midconf-reclassify", "midconf-video-screen", "midconf-video-frames"):
        path = "/data/progress/%s.json" % name
        if os.path.exists(path):
            d = json.load(open(path, encoding="utf-8"))
            print("heartbeat %s: status=%s done=%s/%s phase=%s" % (name, d.get("status"), d.get("done"), d.get("total"), d.get("phase")))
        else:
            print("heartbeat %s: MISSING" % name)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
