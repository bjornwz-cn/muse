#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤 C 附带核查：1,614 条 pending 行（源文件已不在盘）。
对每条 gone 行，在媒体库按同名＋sha256 找对应文件，只报告、不改库、不标 approved。
方法（按优先级）：
  1. operations 表按 src_path 查 dst_path＋sha256，核对 dst 在盘且 sha256 一致
     -> found_ops（已归档，位置与哈希均对上）。
  2. 若无 operations 记录或 dst 不在/哈希不对：按 basename 在 /vol1/1000/媒体库/
     全树搜同名文件，逐个算 sha256，比对 plan_items/operations 里的期望 sha256
     -> found_name（同名＋同哈希）/ name_only（同名但哈希不同）/ not_found。
  3. 既无 operations 也无 plan_items 记录 -> no_record。
只读 DB、只读磁盘。结果写 /data/stepc_gone_check.tsv，汇总打印。
"""
import hashlib
import json
import os
import sqlite3
from collections import Counter

DB = "/data/nas-curator.db"
MEDIA_ROOT = "/vol1/1000/媒体库"
OUT = "/data/stepc_gone_check.tsv"


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    conn = sqlite3.connect(DB, timeout=30)
    rows = conn.execute(
        "SELECT id, payload FROM review_queue WHERE status='pending'"
    ).fetchall()
    gone = []
    for rid, payload in rows:
        d = json.loads(payload)
        src = d.get("src", "")
        if not src.startswith("/vol1/1000/收件箱/"):
            continue
        if os.path.exists(src):
            continue
        gone.append((rid, src, d.get("batch") == "batch4",
                     float(d.get("confidence") or 0)))
    print("gone rows (inbox src, not on disk, pending): %d" % len(gone))
    print("  其中 batch4: %d，非 batch4: %d" % (
        sum(1 for g in gone if g[2]), sum(1 for g in gone if not g[2])))

    # 建媒体库 basename 索引（只走一遍，不算哈希）
    name_index = {}
    n_files = 0
    for root, _dirs, fns in os.walk(MEDIA_ROOT):
        for fn in fns:
            p = os.path.join(root, fn)
            name_index.setdefault(fn, []).append(p)
            n_files += 1
    print("media library files indexed: %d (unique names %d)"
          % (n_files, len(name_index)))

    results = []
    for rid, src, is_b4, conf in gone:
        base = os.path.basename(src)
        # 期望 sha256：先 operations，后 plan_items
        exp_sha = None
        dst = None
        op = conn.execute(
            "SELECT dst_path, sha256 FROM operations WHERE src_path=? "
            "ORDER BY id DESC LIMIT 1", (src,)).fetchone()
        if op:
            dst, exp_sha = op[0], op[1]
        else:
            pi = conn.execute(
                "SELECT dst_path, sha256 FROM plan_items WHERE src_path=? "
                "AND sha256<>'' ORDER BY id DESC LIMIT 1", (src,)).fetchone()
            if pi:
                dst, exp_sha = pi[0], pi[1]
        status, found = "not_found", ""
        if dst and os.path.exists(dst):
            try:
                if exp_sha and sha256_of(dst) == exp_sha:
                    status, found = "found_ops", dst
                elif exp_sha:
                    status, found = "found_ops_sha_mismatch", dst
                else:
                    status, found = "found_ops_no_sha", dst
            except OSError:
                status, found = "found_ops_unreadable", dst
        if status in ("not_found", "found_ops_sha_mismatch",
                      "found_ops_unreadable"):
            # basename 回退搜
            cands = name_index.get(base, [])
            for cand in cands:
                if cand == dst:
                    continue
                try:
                    if exp_sha and sha256_of(cand) == exp_sha:
                        status, found = "found_name", cand
                        break
                except OSError:
                    continue
            else:
                if cands and status == "not_found":
                    status = "name_only"
                    found = cands[0]
        if not exp_sha and not dst:
            status = "no_record"
        results.append((rid, src, "batch4" if is_b4 else "pre-b4",
                        round(conf, 3), exp_sha or "", status, found))
    conn.close()

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("rq_id\tsrc\tbatch\tconfidence\texpected_sha256\tstatus\tfound_path\n")
        for r in results:
            fh.write("\t".join(str(x) for x in r) + "\n")
    cnt = Counter(r[5] for r in results)
    print("== 核查结果 ==")
    for k, v in sorted(cnt.items()):
        print("  %s: %d" % (k, v))
    print("  其中最终置信度>=0.85 且已找到（found_ops/found_name）: %d" % sum(
        1 for r in results if r[3] >= 0.85 and r[5] in ("found_ops", "found_name")))
    print("report: %s (%d rows)" % (OUT, len(results)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
