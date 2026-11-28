#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Midconf step-0 verification (read-only except heartbeat): check
/data/midconf_list.tsv vs DB review_queue rows, DB integrity, on-disk status.
Run inside container: docker exec nas-curator python3 /data/verify_midconf.py
"""
import json, os, sqlite3
from collections import Counter

DB = "/data/nas-curator.db"
LIST = "/data/midconf_list.tsv"

def main():
    rows = []
    with open(LIST, encoding="utf-8") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        print("header: %s" % header)
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if not p or not p[0]:
                continue
            rows.append(dict(zip(header, p)))
    print("list rows: %d" % len(rows))
    print("file_kind: %s" % dict(Counter(r.get("file_kind") for r in rows)))
    print("db_kind: %s" % dict(Counter(r.get("db_kind") for r in rows)))
    print("band: %s" % dict(Counter(r.get("band") for r in rows)))
    def fconf(r, k):
        try:
            return float(r.get(k)) if r.get(k) not in (None, "") else None
        except Exception:
            return None
    vlm_present = sum(1 for r in rows if r.get("vlm_category"))
    print("list vlm_category present: %d" % vlm_present)
    print("list vlm>=0.72: %d" % sum(1 for r in rows if (fconf(r, "vlm_confidence") or 0) >= 0.72))
    print("list vlm<0.72 (present): %d" % sum(1 for r in rows if r.get("vlm_category") and (fconf(r, "vlm_confidence") or 0) < 0.72))
    print("list final>=0.85: %d" % sum(1 for r in rows if (fconf(r, "final_confidence") or 0) >= 0.85))
    print("list final 0.72-0.85: %d" % sum(1 for r in rows if 0.72 <= (fconf(r, "final_confidence") or 0) < 0.85))
    print("list final 0.35-0.72: %d" % sum(1 for r in rows if 0.35 <= (fconf(r, "final_confidence") or 0) < 0.72))
    print("list final <0.35: %d" % sum(1 for r in rows if (fconf(r, "final_confidence") or 0) < 0.35))
    # photo/video split by file_kind
    photos = [r for r in rows if r.get("file_kind") == "照片"]
    videos = [r for r in rows if r.get("file_kind") == "视频"]
    print("photos: %d videos: %d other: %d" % (len(photos), len(videos), len(rows)-len(photos)-len(videos)))
    print("photos with vlm: %d without: %d" % (sum(1 for r in photos if r.get("vlm_category")), sum(1 for r in photos if not r.get("vlm_category"))))
    print("videos with vlm: %d" % sum(1 for r in videos if r.get("vlm_category")))
    # encrypted / 20200614 / credential check in src
    bad = [r for r in rows if "20200614" in r.get("src","") or "凭据" in r.get("src","") or "credential" in r.get("src","").lower()]
    print("20200614/credential src rows: %d" % len(bad))
    enc = [r for r in rows if "加密" in r.get("src","")]
    print("encrypted-name src rows: %d" % len(enc))

    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=30)
    print("integrity: %s" % conn.execute("PRAGMA integrity_check").fetchone()[0])
    print("status counts: %s" % dict(conn.execute("SELECT status, count(*) FROM review_queue GROUP BY status").fetchall()))
    print("kind counts: %s" % dict(conn.execute("SELECT kind, count(*) FROM review_queue GROUP BY kind").fetchall()))
    ids = [int(r["rq_id"]) for r in rows]
    print("unique rq_ids: %d" % len(set(ids)))
    missing, wrong_status, wrong_kind = [], [], []
    db_vlm = 0
    on_disk = 0
    enc_payload = 0
    for rid in ids:
        row = conn.execute("SELECT status, kind, payload FROM review_queue WHERE id=?", (rid,)).fetchone()
        if not row:
            missing.append(rid); continue
        st, kind, payload = row
        if st != "pending":
            wrong_status.append((rid, st))
        p = json.loads(payload) if payload else {}
        if p.get("vlm_category"):
            db_vlm += 1
        if p.get("encrypted") or p.get("is_encrypted"):
            enc_payload += 1
        src = p.get("src", "")
        if src and os.path.exists(src):
            on_disk += 1
    print("db rows missing: %d %s" % (len(missing), missing[:10]))
    print("db rows not pending: %d %s" % (len(wrong_status), wrong_status[:10]))
    print("db vlm_category present: %d" % db_vlm)
    print("db encrypted-flag rows: %d" % enc_payload)
    print("src on disk: %d / %d" % (on_disk, len(ids)))
    # rq id files prepared by parent
    for name in ("midconf_all_photo_rqids.txt", "midconf_photos_with_vlm_rqids.txt", "midconf_photos_no_vlm_rqids.txt"):
        path = "/data/" + name
        if os.path.exists(path):
            txt = open(path, encoding="utf-8").read()
            n = len([x for x in txt.replace(",", " ").split() if x.strip()])
            print("%s: %d ids" % (name, n))
    conn.close()

if __name__ == "__main__":
    main()
