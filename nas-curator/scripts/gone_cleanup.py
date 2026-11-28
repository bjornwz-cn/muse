#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤 A：清理 1,671 条 gone 行（只改 DB，不动文件）。
读 /data/stepc_gone_check.tsv，对每行逐个再核对：
  found_path 仍在盘、sha256(found_path) == expected_sha256，
再把对应 review 行标 approved（decided_by='gone-cleanup'），
payload 记 gone_cleanup 说明与 found_path。
核对不上的不标、记原因。只改 review_queue 行，不动文件、
不动 plans / plan_items / operations。
用法（NAS 容器内）：docker exec nas-curator python3 /data/gone_cleanup.py
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import sys
import time
from collections import Counter

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

DB_PATH = "/data/nas-curator.db"
TSV = "/data/stepc_gone_check.tsv"
OUT = "/data/gone_cleanup_verify.tsv"
DECIDED_BY = "gone-cleanup"


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    import common.db as DB

    # 1. DB 备份（sqlite3 backup API，VACUUM INTO）
    conn = DB.connect(DB_PATH)
    cst = time.strftime("%Y%m%d-%H%M", time.gmtime(time.time() + 8 * 3600))
    backup = "/data/nas-curator.db.bak-%s-pre-gone-cleanup" % cst
    conn.execute("VACUUM INTO ?", (backup,))
    print("DB backup: %s" % backup, flush=True)

    rows = list(csv.DictReader(open(TSV, encoding="utf-8"), delimiter="\t"))
    print("gone rows to check: %d" % len(rows), flush=True)
    counts = Counter()
    results = []
    t0 = time.time()
    for i, r in enumerate(rows):
        rq_id = int(r["rq_id"])
        found = r["found_path"]
        exp = r["expected_sha256"]
        action, reason = "skip", ""
        row = conn.execute(
            "SELECT status, payload FROM review_queue WHERE id=?", (rq_id,)
        ).fetchone()
        if row is None:
            action, reason = "skip", "no_review_row"
        elif row[0] != "pending":
            action, reason = "skip", "not_pending:%s" % row[0]
        elif not found:
            action, reason = "skip", "no_found_path"
        elif not os.path.exists(found):
            action, reason = "skip", "found_path_missing"
        elif not exp:
            action, reason = "skip", "no_expected_sha"
        else:
            try:
                actual = sha256_of(found)
            except OSError as e:
                action, reason = "skip", "unreadable:%s" % type(e).__name__
            else:
                if actual != exp:
                    action, reason = "skip", "sha_mismatch"
                else:
                    try:
                        payload = json.loads(row[1]) if row[1] else {}
                    except ValueError:
                        payload = {}
                    payload["gone_cleanup"] = "found_ops_verified"
                    payload["gone_found_path"] = found
                    DB.decide_review(conn, rq_id, "approved",
                                     decided_by=DECIDED_BY)
                    conn.execute(
                        "UPDATE review_queue SET payload=? WHERE id=?",
                        (json.dumps(payload, ensure_ascii=False), rq_id))
                    conn.commit()
                    action, reason = "approved", "found_ops_verified"
        counts[action] += 1
        results.append((rq_id, r["src"], found, action, reason))
        if (i + 1) % 200 == 0:
            print("PROG %d/%d %.0fs %s" % (
                i + 1, len(rows), time.time() - t0, dict(counts)), flush=True)

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("rq_id\tsrc\tfound_path\taction\treason\n")
        for x in results:
            fh.write("\t".join(str(v) for v in x) + "\n")

    print("== DONE ==", flush=True)
    print("total: %d, approved: %d, skipped: %d" % (
        len(rows), counts["approved"], counts["skip"]), flush=True)
    print("skip reasons: %s" % dict(Counter(
        r[4] for r in results if r[3] == "skip")), flush=True)
    print("elapsed: %.1fs" % (time.time() - t0), flush=True)
    print("verify report: %s" % OUT, flush=True)
    print("review:", conn.execute(
        "SELECT status, COUNT(*) FROM review_queue GROUP BY status"
    ).fetchall(), flush=True)
    print("integrity:", conn.execute(
        "PRAGMA integrity_check").fetchone()[0], flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
