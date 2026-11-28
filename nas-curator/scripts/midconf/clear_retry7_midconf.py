#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Clear the 5 vlm keys on the 7 rate-limited rows only, so the confirm
script can retry them. Old conclusions are safe in /data/midconf_vlm_old.json;
keep-best after the retry restores them if the retry is worse/missing.
"""
import json, sqlite3

DB = "/data/nas-curator.db"
IDS = [145877, 146050, 146239, 146414, 147244, 147322, 148005]
KEYS = ["vlm_category", "vlm_confidence", "vlm_reason", "vlm_model", "vlm_source"]

def main():
    old = json.load(open("/data/midconf_vlm_old.json", encoding="utf-8"))
    conn = sqlite3.connect(DB, timeout=30)
    for rid in IDS:
        row = conn.execute("SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()
        d = json.loads(row[0])
        assert str(rid) in old, "rq=%d has no old backup!" % rid
        assert d.get("vlm_category") == old[str(rid)].get("vlm_category"), "rq=%d current vlm != backup" % rid
        for k in KEYS:
            d.pop(k, None)
        conn.execute("UPDATE review_queue SET payload=? WHERE id=?",
                     (json.dumps(d, ensure_ascii=False), rid))
        print("cleared rq=%d (old was %s %.2f)" % (rid, old[str(rid)].get("vlm_category"), old[str(rid)].get("vlm_confidence") or 0))
    conn.commit()
    conn.close()
    print("CLEARED 7")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
