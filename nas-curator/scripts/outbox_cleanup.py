#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理收件箱外的残留 review 行（只改 DB，不动文件）。
第 2 类（收件箱外在盘，约 12,636 条）：文件已在媒体库等位置，review 行仍 pending。
  逐个核对：src 在盘＋sha256 与 plan_items 记录一致 → 标 approved
  （decided_by='redirect-approved'，payload 记 in_place_verified；src 已是现位置，无需更新）。
  若 src 的哈希不符 → 按 sha 在 files/operations 里找现位置，核对后更新 src 并标 approved；
  找不到 → 跳过记原因。
第 3 类（收件箱外不在盘，约 3,311 条）＋未归类（收件箱在盘外、src 不在盘，3 条）：
  按 sha 在 files/operations 里找现位置；找到并核对（在盘＋sha 一致）→ 更新 src 并标 approved；
  找不到 → 确认 src 不在盘、无 operations 记录 → 标 approved（decided_by='gone-cleanup-out'）。
只改 review_queue 行（status＋decided_by＋decided_at＋payload 备注），不动文件、
不动 plans / plan_items / operations；不动收件箱在盘的 6,431 条。
每行更新前重读状态与 payload（防监听器并发改动），更新用单条原子 UPDATE
（status＋decided_by＋decided_at＋payload 一起写）并按行提交。
用法（NAS 容器内 detached）：docker exec -d nas-curator python3 /data/outbox_cleanup.py
心跳：/data/progress/outbox-cleanup.json；核对报告：/data/outbox_cleanup_verify.tsv
"""
from __future__ import annotations

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
OUT = "/data/outbox_cleanup_verify.tsv"
HEARTBEAT = "/data/progress/outbox-cleanup.json"
INBOX_PREFIX = "/vol1/1000/收件箱/"
DECIDED_REDIRECT = "redirect-approved"
DECIDED_GONE = "gone-cleanup-out"


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_heartbeat(phase, status, counts, extra=None):
    try:
        os.makedirs(os.path.dirname(HEARTBEAT), exist_ok=True)
        data = {
            "phase": phase, "status": status,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.gmtime(time.time() + 8 * 3600)),
            # counts 的键是 (category, action) 元组，JSON 要求字符串键，先转字符串
            "counts": {("%s:%s" % k): v for k, v in counts.items()},
        }
        if extra:
            data.update(extra)
        text = json.dumps(data, ensure_ascii=False)
        tmp = HEARTBEAT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, HEARTBEAT)
    except OSError:
        pass


def main():
    import common.db as DB

    # 1. DB 备份（sqlite3 backup API，VACUUM INTO）
    conn = DB.connect(DB_PATH)
    cst = time.strftime("%Y%m%d-%H%M", time.gmtime(time.time() + 8 * 3600))
    backup = "/data/nas-curator.db.bak-%s-pre-outbox-cleanup" % cst
    conn.execute("VACUUM INTO ?", (backup,))
    print("DB backup: %s" % backup, flush=True)
    write_heartbeat("backup", "running", {})

    # 2. 载入全部 pending 行，处理时按当时状态重分类
    rows = conn.execute(
        "SELECT id, kind, payload FROM review_queue WHERE status='pending'"
    ).fetchall()
    print("pending rows loaded: %d" % len(rows), flush=True)

    # 3. 建索引（只读快照）
    pi_by_src = {}
    for src_path, sha, size, dst, action, status in conn.execute(
            "SELECT src_path, sha256, size, dst_path, action, status "
            "FROM plan_items"):
        pi_by_src.setdefault(src_path, []).append(
            (sha, size, dst, action, status))
    files_by_sha = {}
    files_by_path = {}
    for path, sha, size, status in conn.execute(
            "SELECT path, sha256, size, status FROM files"):
        files_by_path[path] = (sha, size, status)
        if sha:
            files_by_sha.setdefault(sha, []).append(path)
    ops_by_sha = {}
    ops_by_src = {}
    for sha, src_path, dst_path in conn.execute(
            "SELECT sha256, src_path, dst_path FROM operations"):
        if sha:
            ops_by_sha.setdefault(sha, []).append(dst_path)
        ops_by_src.setdefault(src_path, []).append((sha, dst_path))
    print("indexes: plan_items src %d, files %d, files sha %d, ops sha %d" % (
        len(pi_by_src), len(files_by_path), len(files_by_sha),
        len(ops_by_sha)), flush=True)

    hash_cache = {}

    def cached_sha(path):
        if path not in hash_cache:
            try:
                hash_cache[path] = sha256_of(path)
            except OSError as e:
                hash_cache[path] = ("__ERR__:" + type(e).__name__)
        return hash_cache[path]

    def expected_sha(src):
        """取该 src 的期望 sha：plan_items（去重）→ files 表 → operations（按 src）。"""
        shas = set()
        for sha, _size, _dst, _action, _status in pi_by_src.get(src, []):
            if sha:
                shas.add(sha)
        if len(shas) == 1:
            return shas.pop()
        if len(shas) > 1:
            return ("__MULTI__:" + ",".join(sorted(shas)))
        if src in files_by_path and files_by_path[src][0]:
            return files_by_path[src][0]
        for sha, _dst in ops_by_src.get(src, []):
            if sha:
                return sha
        return None

    def find_verified_locations(sha, exclude):
        """按 sha 在 files/operations 里找现位置，逐个核对在盘＋sha 一致。"""
        cands = []
        for p in files_by_sha.get(sha, []):
            if p not in cands:
                cands.append(p)
        for p in ops_by_sha.get(sha, []):
            if p not in cands:
                cands.append(p)
        verified = []
        for p in cands:
            if p in exclude:
                continue
            if not p or not os.path.exists(p):
                continue
            if cached_sha(p) == sha:
                verified.append(p)
        return verified

    def approve_row(rq_id, decided_by, payload_patch):
        """重读状态＋payload，单条原子 UPDATE。返回 (ok, reason)。"""
        row = conn.execute(
            "SELECT status, payload FROM review_queue WHERE id=?",
            (rq_id,)).fetchone()
        if row is None:
            return False, "no_review_row"
        if row[0] != "pending":
            return False, "already_decided:%s" % row[0]
        try:
            payload = json.loads(row[1]) if row[1] else {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {"_old_payload": payload}
        old_src = payload.get("src", "")
        payload.update(payload_patch)
        cur = conn.execute(
            "UPDATE review_queue SET status='approved', decided_by=?,"
            " decided_at=strftime('%Y-%m-%dT%H:%M:%S','now'), payload=?"
            " WHERE id=? AND status='pending'",
            (decided_by, json.dumps(payload, ensure_ascii=False), rq_id))
        if cur.rowcount != 1:
            conn.rollback()
            return False, "update_rowcount_0"
        conn.commit()
        return True, old_src

    counts = Counter()
    results = []
    t0 = time.time()
    n = len(rows)
    for i, (rq_id, kind, payload) in enumerate(rows):
        try:
            p = json.loads(payload) if payload else {}
        except ValueError:
            p = {}
        src = (p.get("src") or "") if isinstance(p, dict) else ""
        in_inbox = src.startswith(INBOX_PREFIX)
        on_disk = bool(src) and os.path.exists(src)
        if in_inbox and on_disk:
            cat = "cat1-skip"
        elif not in_inbox and on_disk:
            cat = "cat2"
        elif not in_inbox and not on_disk:
            cat = "cat3"
        else:
            cat = "unc"
        action, reason, new_src = "skip", "", ""
        if cat == "cat1-skip":
            action, reason = "skip", "inbox_on_disk_untouched"
        else:
            exp = expected_sha(src)
            if isinstance(exp, str) and exp.startswith("__MULTI__:"):
                action, reason = "skip", "multi_sha_conflict"
            elif cat == "cat2":
                if exp is None:
                    action, reason = "skip", "no_expected_sha"
                else:
                    actual = cached_sha(src) if on_disk else None
                    if isinstance(actual, str) and actual.startswith("__ERR__"):
                        action, reason = "skip", "unreadable:" + actual[8:]
                    elif actual == exp:
                        ok, info = approve_row(rq_id, DECIDED_REDIRECT, {
                            "outbox_cleanup": "in_place_verified",
                            "outbox_verified_sha": exp,
                        })
                        if ok:
                            action, reason = "approved", "in_place_verified"
                        else:
                            action, reason = "skip", info
                    else:
                        # src 处文件内容不符：按 sha 另找现位置
                        verified = find_verified_locations(exp, {src})
                        if verified:
                            new_src = verified[0]
                            ok, _info = approve_row(rq_id, DECIDED_REDIRECT, {
                                "outbox_cleanup": "redirected",
                                "outbox_old_src": src,
                                "src": new_src,
                                "outbox_verified_sha": exp,
                                "outbox_verified_candidates": verified,
                            })
                            if ok:
                                action, reason = "approved", "redirected"
                            else:
                                action, reason = "skip", _info
                        else:
                            action, reason = "skip", "hash_mismatch_no_alt"
            else:  # cat3 / unc：src 不在盘
                if exp is None:
                    # 无任何 hash 可核：确认无 operations/files 记录再标 gone
                    has_ops = bool(ops_by_src.get(src))
                    has_files = src in files_by_path
                    if not has_ops and not has_files:
                        ok, _info = approve_row(rq_id, DECIDED_GONE, {
                            "outbox_cleanup": "gone_verified",
                            "outbox_verified_sha": None,
                            "outbox_note": "no_hash_available",
                        })
                        if ok:
                            action, reason = "approved", "gone_no_hash"
                        else:
                            action, reason = "skip", _info
                    else:
                        action, reason = "skip", "no_hash_but_has_trace"
                else:
                    verified = find_verified_locations(exp, {src})
                    if verified:
                        new_src = verified[0]
                        ok, _info = approve_row(rq_id, DECIDED_REDIRECT, {
                            "outbox_cleanup": "redirected",
                            "outbox_old_src": src,
                            "src": new_src,
                            "outbox_verified_sha": exp,
                            "outbox_verified_candidates": verified,
                        })
                        if ok:
                            action, reason = "approved", "redirected"
                        else:
                            action, reason = "skip", _info
                    else:
                        ok, _info = approve_row(rq_id, DECIDED_GONE, {
                            "outbox_cleanup": "gone_verified",
                            "outbox_verified_sha": exp,
                            "outbox_note": "src_missing_no_verified_location",
                        })
                        if ok:
                            action, reason = "approved", "gone_verified"
                        else:
                            action, reason = "skip", _info
        counts[(cat, action)] += 1
        results.append((rq_id, cat, src, action, reason, new_src))
        if (i + 1) % 500 == 0:
            print("PROG %d/%d %.0fs %s" % (
                i + 1, n, time.time() - t0, dict(counts)), flush=True)
            write_heartbeat("processing", "running", counts,
                            {"processed": i + 1, "total": n})

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("rq_id\tcategory\tsrc\taction\treason\tnew_src\n")
        for x in results:
            fh.write("\t".join(str(v) for v in x) + "\n")

    print("== DONE ==", flush=True)
    print("total: %d" % n, flush=True)
    for k in sorted(counts):
        print("  %s: %d" % (k, counts[k]), flush=True)
    print("skip reasons: %s" % dict(Counter(
        r[4] for r in results if r[3] == "skip")), flush=True)
    print("elapsed: %.1fs" % (time.time() - t0), flush=True)
    print("verify report: %s" % OUT, flush=True)
    print("review:", conn.execute(
        "SELECT status, COUNT(*) FROM review_queue GROUP BY status"
    ).fetchall(), flush=True)
    print("integrity:", conn.execute(
        "PRAGMA integrity_check").fetchone()[0], flush=True)
    print("redirect-approved:", conn.execute(
        "SELECT COUNT(*) FROM review_queue WHERE decided_by=?",
        (DECIDED_REDIRECT,)).fetchone()[0], flush=True)
    print("gone-cleanup-out:", conn.execute(
        "SELECT COUNT(*) FROM review_queue WHERE decided_by=?",
        (DECIDED_GONE,)).fetchone()[0], flush=True)
    write_heartbeat("done", "done", counts, {"elapsed_s": round(time.time() - t0, 1)})
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
