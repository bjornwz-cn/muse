#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性清扫：复核队列里 pending 的 dup:sha256 行（2026-11-28）。

只动 kind='dup' 且 reason 以 dup:sha256 开头的 pending 行（465 条）；
dup:dhash（1,182 条，感知哈希近似重复、非字节相同）与 blurry 行一律不动。
验证与删除纪律复用 common.dup_delete（resolve_kept_path / verify_kept_copy /
DELETED_DST / DECIDED_BY），与 0.2.3 的自动删除同一标准：
  (a) 保留份存在且在库内（工作/家庭/媒体库），不在收件箱；
  (b) 大小＋SHA 一致；
  (c) 保留份能打开/解码。
删除纪律：先写逐名清单 JSONL → 再用 sqlite3 backup API 备份 DB → 再逐条删；
每条删前重算副本 SHA，删后记 operations 行（dst="<deleted>"、decided_by=
"auto:dup-sha256"、plan_id 取该行原始 plan_id），复核行按 review_id 精确判
approved，并把对应 plan_items 行置 done。

与模块批处理函数的区别：模块函数是单 plan 形态（一个 plan_id 管整批），
本次清扫跨 25 个历史 plan，故用薄封装、按行取原始 plan_id；验证函数本身
未重写。写操作遇 database is locked 重试。
用法（容器内）：python3 /app/scripts/dup_sweep_20261128.py --db /data/nas-curator.db --config /data/config/libraries.json [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, "/app")

from common import db as DB  # noqa: E402
from common import dup_delete as DD  # noqa: E402
from common import routing as RT  # noqa: E402
from common.hashing import sha256_file  # noqa: E402


def _load_items(conn):
    rows = conn.execute(
        "SELECT rq.id, rq.plan_id,"
        " json_extract(rq.payload,'$.src'),"
        " json_extract(rq.payload,'$.evidence'),"
        " pi.sha256, pi.size, pi.ext"
        " FROM review_queue rq JOIN plan_items pi"
        " ON pi.plan_id=rq.plan_id"
        " AND pi.src_path=json_extract(rq.payload,'$.src')"
        " WHERE rq.status='pending' AND rq.kind='dup'"
        " AND json_extract(rq.payload,'$.reason') LIKE 'dup:sha256%'"
        " ORDER BY rq.id").fetchall()
    return [{"review_id": r[0], "plan_id": r[1], "src": r[2], "evidence": r[3],
             "sha256": r[4], "size": r[5], "ext": r[6]} for r in rows]


def _retry_locked(fn, *args, **kwargs):
    last = None
    for attempt in range(12):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower():
                raise
            last = e
            time.sleep(5)
    raise last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/data/nas-curator.db")
    ap.add_argument("--config", default="/data/config/libraries.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    with open(args.config, encoding="utf-8") as f:
        roots = RT.roots_from_config(json.load(f))
    conn = sqlite3.connect(args.db, timeout=120)
    conn.row_factory = sqlite3.Row
    items = _load_items(conn)
    stats = {"candidates": len(items), "deleted": 0, "would_delete": 0,
             "skipped": {}, "manifest": "", "backup": ""}
    verified = []
    for it in items:
        kept = DD.resolve_kept_path(conn, it["evidence"])
        if kept is None:
            stats["skipped"]["kept-missing"] = \
                stats["skipped"].get("kept-missing", 0) + 1
            continue
        ok, why = DD.verify_kept_copy(it["src"], kept, it["sha256"],
                                      it["size"], roots, ext=it["ext"])
        if not ok:
            stats["skipped"][why] = stats["skipped"].get(why, 0) + 1
            continue
        verified.append({**it, "kept": kept})
    if args.dry_run:
        stats["would_delete"] = len(verified)
        stats["elapsed_s"] = round(time.time() - t0, 1)
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    if not verified:
        stats["elapsed_s"] = round(time.time() - t0, 1)
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    db_dir = os.path.dirname(os.path.abspath(args.db))
    manifest_dir = os.path.join(db_dir, "dup_deletions")
    backup_dir = os.path.join(db_dir, "backups")
    os.makedirs(manifest_dir, exist_ok=True)
    os.makedirs(backup_dir, exist_ok=True)
    manifest_path = os.path.join(
        manifest_dir, "dup-delete-%s-sweep20261128.jsonl" % ts)
    with open(manifest_path, "w", encoding="utf-8") as f:
        for v in verified:
            f.write(json.dumps({**v, "verified_at": ts},
                               ensure_ascii=False) + "\n")
    stats["manifest"] = manifest_path
    backup_path = os.path.join(
        backup_dir, "nas-curator.db.bak-%s-predup-sweep" % ts)
    bkp = sqlite3.connect(backup_path)
    try:
        conn.backup(bkp)
    finally:
        bkp.close()
    stats["backup"] = backup_path
    for v in verified:
        src, sha = v["src"], v["sha256"]
        # 删前复核：复核行仍 pending、副本仍在、SHA 未变，任一不符跳过。
        row = conn.execute(
            "SELECT status FROM review_queue WHERE id=?",
            (v["review_id"],)).fetchone()
        if row is None or row[0] != "pending":
            stats["skipped"]["review-not-pending"] = \
                stats["skipped"].get("review-not-pending", 0) + 1
            continue
        if not os.path.isfile(src):
            stats["skipped"]["src-gone"] = \
                stats["skipped"].get("src-gone", 0) + 1
            continue
        try:
            cur_sha = sha256_file(src)
        except OSError:
            stats["skipped"]["src-unreadable"] = \
                stats["skipped"].get("src-unreadable", 0) + 1
            continue
        if not sha or cur_sha != sha:
            stats["skipped"]["src-changed"] = \
                stats["skipped"].get("src-changed", 0) + 1
            continue
        try:
            os.unlink(src)
        except OSError:
            stats["skipped"]["unlink-failed"] = \
                stats["skipped"].get("unlink-failed", 0) + 1
            continue
        _retry_locked(DB.add_operation, conn, v["plan_id"], src,
                      DD.DELETED_DST, sha, decided_by=DD.DECIDED_BY)
        _retry_locked(DB.decide_review, conn, v["review_id"], "approved",
                      decided_by=DD.DECIDED_BY)
        try:
            _retry_locked(conn.execute,
                          "UPDATE plan_items SET status='done'"
                          " WHERE plan_id=? AND src_path=?",
                          (v["plan_id"], src))
            conn.commit()
        except Exception:  # noqa: BLE001 - 审计行更新失败不影响已删除的事实
            pass
        stats["deleted"] += 1
    stats["elapsed_s"] = round(time.time() - t0, 1)
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
