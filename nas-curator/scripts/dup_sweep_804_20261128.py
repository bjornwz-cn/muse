#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性清扫：VLM 0.45 批复核（计划 #293）中确认的 804 个精确重复副本（2026-11-28）。

背景：计划 #293 移动 1,127 个文件时 816 个因“目标已存在”跳过；逐文件
SHA256 比对后 804 个确认真重复（收件箱源文件与库内目标文件 SHA256 完全
相同），12 个同名不同内容（另有清单，坚决不动），0 未知。
清单：容器 /data/vlm045_dstexists_same_sha_rqids.txt（804 行 review_queue id）。

只动清单上的 804 个 id；保留份取计划 #293 的 plan_items dst_path（即“目标
已存在”的那个库内文件）；指纹（sha256/size/ext）取复核行原始 plan 的
plan_items。验证与删除纪律复用 common.dup_delete（verify_kept_copy /
DELETED_DST / DECIDED_BY），与 0.2.3 自动删除同一标准：
  (a) 保留份存在且在库内（工作/家庭/媒体库），不在收件箱；
  (b) 保留份大小＋SHA 与副本一致；
  (c) 保留份能打开/解码。
并吸取上一批清扫的教训：验证阶段就检查 src 仍存在（verify_kept_copy 不
检查 src），dry-run 预测才准。

src-gone 处理（主代理已定 stale 模式）：src 已不在收件箱时，核对文件是否
安全——有未回滚的移动记录（移到库内且目标仍在），或同名文件在库内且大小
与预期一致——确认安全才判 approved（decided_by="stale-src-gone"，payload
记 stale_note）；确认不了安全的留在队列、不擅自批。

删除纪律：先写逐名清单 JSONL → 再用 sqlite3 backup API 备份 DB → 再逐条
删；每条删前重算副本 SHA，删后记 operations 行（dst="<deleted>"、
decided_by="auto:dup-sha256"、plan_id 取该行原始 plan_id），复核行按
review_id 精确判 approved，对应 plan_items 行置 done。
stale 批准单独记一份 JSONL 审计清单，不删文件、不写 operations 行。

用法（容器内）：python3 /app/scripts/dup_sweep_804_20261128.py --db /data/nas-curator.db --config /data/config/libraries.json [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, "/app")

from common import db as DB  # noqa: E402
from common import dup_delete as DD  # noqa: E402
from common import routing as RT  # noqa: E402
from common.hashing import sha256_file  # noqa: E402

LIST_PATH = "/data/vlm045_dstexists_same_sha_rqids.txt"
VLM_PLAN_ID = 293
STALE_DECIDED_BY = "stale-src-gone"
LIBRARY_TOPS = ("work", "family", "media")


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


def _stale_check(conn, roots, src, expected_size):
    """src 已不在收件箱：核对文件是否安全。返回 (safe, reason)。"""
    op = DB.get_operation_for_src(conn, src)
    if op:
        dst = op.get("dst_path") or ""
        if dst and dst != DD.DELETED_DST:
            top = RT.library_top(dst, roots)
            if top in LIBRARY_TOPS and os.path.isfile(dst):
                return True, "moved: plan %s -> %s" % (op.get("plan_id"), dst)
    base = os.path.basename(src)
    lib_roots = [roots[t] for t in LIBRARY_TOPS if roots.get(t)]
    for root in lib_roots:
        try:
            out = subprocess.run(
                ["find", root, "-type", "f", "-name", base],
                capture_output=True, text=True, timeout=120)
        except Exception:  # noqa: BLE001 - 搜索失败按不安全处理
            continue
        for hit in out.stdout.splitlines():
            hit = hit.strip()
            if not hit:
                continue
            try:
                if expected_size is not None and \
                        os.path.getsize(hit) != expected_size:
                    continue
            except OSError:
                continue
            return True, "same-name in library (size match): %s" % hit
    return False, "no move record, no same-name file in library"


def _approve_stale(conn, review_id, note):
    def _do(c):
        row = c.execute("SELECT payload FROM review_queue WHERE id=?",
                        (review_id,)).fetchone()
        payload = {}
        if row and row[0]:
            try:
                payload = json.loads(row[0])
            except ValueError:
                payload = {}
        payload["stale_note"] = note
        c.execute("UPDATE review_queue SET payload=? WHERE id=?",
                  (json.dumps(payload, ensure_ascii=False), review_id))
        c.commit()
    _retry_locked(_do, conn)
    _retry_locked(DB.decide_review, conn, review_id, "approved",
                  decided_by=STALE_DECIDED_BY)


def _load_items(conn):
    with open(LIST_PATH, encoding="utf-8") as f:
        ids = [int(line.strip()) for line in f if line.strip()]
    items = []
    for rid in ids:
        row = conn.execute(
            "SELECT plan_id, status, json_extract(payload,'$.src')"
            " FROM review_queue WHERE id=?", (rid,)).fetchone()
        if row is None:
            items.append({"review_id": rid, "missing_row": True})
            continue
        items.append({"review_id": rid, "plan_id": row[0],
                      "status": row[1], "src": row[2]})
    return items


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
             "stale_approved": 0, "stale_left": 0,
             "skipped": {}, "manifest": "", "backup": "",
             "stale_manifest": ""}
    verified = []
    stale_safe = []
    for it in items:
        if it.get("missing_row"):
            stats["skipped"]["review-row-missing"] = \
                stats["skipped"].get("review-row-missing", 0) + 1
            continue
        if it["status"] != "pending":
            stats["skipped"]["review-not-pending"] = \
                stats["skipped"].get("review-not-pending", 0) + 1
            continue
        src = it["src"]
        if not src:
            stats["skipped"]["no-src"] = stats["skipped"].get("no-src", 0) + 1
            continue
        # 原始 plan 的指纹（sha256/size/ext）
        orig = conn.execute(
            "SELECT sha256, size, ext FROM plan_items"
            " WHERE plan_id=? AND src_path=?",
            (it["plan_id"], src)).fetchone()
        if not orig or not orig[0]:
            stats["skipped"]["no-sha"] = stats["skipped"].get("no-sha", 0) + 1
            continue
        it["sha256"], it["size"], it["ext"] = orig[0], orig[1], orig[2]
        # src 已不在收件箱：走 stale 核对，不进删除通道
        if not os.path.isfile(src):
            safe, why = _stale_check(conn, roots, src, it["size"])
            if safe:
                stale_safe.append({**it, "stale_reason": why})
            else:
                stats["stale_left"] = stats["stale_left"] + 1
                stats["skipped"]["src-gone-unsafe"] = \
                    stats["skipped"].get("src-gone-unsafe", 0) + 1
            continue
        # 保留份：计划 #293 的 dst_path（“目标已存在”的库内文件）
        row293 = conn.execute(
            "SELECT dst_path, status FROM plan_items"
            " WHERE plan_id=? AND src_path=?", (VLM_PLAN_ID, src)).fetchone()
        if not row293 or not row293[0]:
            stats["skipped"]["kept-missing"] = \
                stats["skipped"].get("kept-missing", 0) + 1
            continue
        if row293[1] != "skipped":
            stats["skipped"]["vlm-item-not-skipped"] = \
                stats["skipped"].get("vlm-item-not-skipped", 0) + 1
            continue
        kept = row293[0]
        ok, why = DD.verify_kept_copy(src, kept, it["sha256"], it["size"],
                                      roots, ext=it["ext"])
        if not ok:
            stats["skipped"][why] = stats["skipped"].get(why, 0) + 1
            continue
        verified.append({**it, "kept": kept})
    stats["would_delete"] = len(verified)
    stats["stale_would_approve"] = len(stale_safe)
    if args.dry_run:
        stats["elapsed_s"] = round(time.time() - t0, 1)
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    # --- 正式执行：先写清单，再备份 DB，再逐条删 ---
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    db_dir = os.path.dirname(os.path.abspath(args.db))
    manifest_dir = os.path.join(db_dir, "dup_deletions")
    backup_dir = os.path.join(db_dir, "backups")
    os.makedirs(manifest_dir, exist_ok=True)
    os.makedirs(backup_dir, exist_ok=True)
    if verified:
        manifest_path = os.path.join(
            manifest_dir, "dup-delete-%s-sweep804-20261128.jsonl" % ts)
        with open(manifest_path, "w", encoding="utf-8") as f:
            for v in verified:
                f.write(json.dumps({**v, "verified_at": ts},
                                   ensure_ascii=False) + "\n")
        stats["manifest"] = manifest_path
    if stale_safe:
        stale_path = os.path.join(
            manifest_dir, "dup-stale-approve-%s-sweep804-20261128.jsonl" % ts)
        with open(stale_path, "w", encoding="utf-8") as f:
            for s in stale_safe:
                f.write(json.dumps({**s, "verified_at": ts},
                                   ensure_ascii=False) + "\n")
        stats["stale_manifest"] = stale_path
    backup_path = os.path.join(
        backup_dir, "nas-curator.db.bak-%s-predup-sweep804" % ts)
    bkp = sqlite3.connect(backup_path)
    try:
        conn.backup(bkp)
    finally:
        bkp.close()
    stats["backup"] = backup_path
    # stale 批准（不删文件、不写 operations 行）
    for s in stale_safe:
        row = conn.execute(
            "SELECT status FROM review_queue WHERE id=?",
            (s["review_id"],)).fetchone()
        if row is None or row[0] != "pending":
            stats["skipped"]["stale-review-not-pending"] = \
                stats["skipped"].get("stale-review-not-pending", 0) + 1
            continue
        if os.path.isfile(s["src"]):
            # 执行窗口内文件又出现了：不再按 stale 处理，留待下次清扫
            stats["skipped"]["stale-src-reappeared"] = \
                stats["skipped"].get("stale-src-reappeared", 0) + 1
            continue
        _approve_stale(conn, s["review_id"], s["stale_reason"])
        stats["stale_approved"] += 1
    # 逐条删除
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
