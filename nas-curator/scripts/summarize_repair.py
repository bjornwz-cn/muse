#!/usr/bin/env python3
"""P4 /vol1 全量修库 dry-run 汇总：按批统计计划项、自动/复核分流、类目分布、覆盖文件数。

用法（在 NAS Curator 容器内）：
    python3 /app/summarize_repair.py --db /data/nas-curator.db --since 2026-11-19T03:52:00

只读：只查 plans/plan_items，不移动、不改任何文件。
分流口径与 apply 门控一致（curator.py _apply_items / api/jobs.py _run_auto_apply）：
    auto   = action in (move, rename) 且 (confidence >= 0.85 或 auto_exempt=1)
    review = 其余 move/rename 项（低置信、非豁免）或 action=review
    skip   = action=skip
覆盖文件数：文件项计 1；单元项（auto_exempt=1）计其 src 目录下实际文件数（find 计数）。
"""
import argparse
import json
import os
import sqlite3
import subprocess
from collections import Counter

AUTO_CONF = 0.85


def _files_under(path):
    """目录下实际文件数（跟随符号链接不展开；不存在返回 0）。"""
    if not path or not os.path.isdir(path):
        return 1 if path and os.path.isfile(path) else 0
    try:
        out = subprocess.run(
            ["find", path, "-type", "f"],
            capture_output=True, text=True, timeout=300)
        return len([l for l in out.stdout.splitlines() if l])
    except Exception:
        return 0


def summarize(db, since):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    plans = conn.execute(
        "SELECT id, library, rules_version, status, created_at, summary "
        "FROM plans WHERE created_at >= ? AND library != 'inbox' "
        "ORDER BY id", (since,)).fetchall()
    out = []
    totals = Counter()
    for p in plans:
        pid = p["id"]
        items = conn.execute(
            "SELECT src_path, category, action, confidence, auto_exempt, size "
            "FROM plan_items WHERE plan_id=?", (pid,)).fetchall()
        n = len(items)
        auto = review = skip = 0
        cats = Counter()
        size = 0
        files_covered = 0
        for it in items:
            cats[it["category"]] += 1
            size += it["size"] or 0
            a = it["action"]
            if a == "skip":
                skip += 1
                continue
            if a in ("move", "rename") and (
                    (it["confidence"] or 0) >= AUTO_CONF or it["auto_exempt"]):
                auto += 1
            else:
                review += 1
            files_covered += _files_under(it["src_path"]) if it["auto_exempt"] else 1
        s = json.loads(p["summary"]) if p["summary"] else {}
        out.append({
            "plan_id": pid, "library": p["library"],
            "rules_version": p["rules_version"], "status": p["status"],
            "created_at": p["created_at"], "n": n, "auto": auto,
            "review": review, "skip": skip, "size": size,
            "files_covered": files_covered, "categories": dict(cats),
            "raw_summary": s,
        })
        totals["items"] += n
        totals["auto"] += auto
        totals["review"] += review
        totals["skip"] += skip
        totals["size"] += size
        totals["files_covered"] += files_covered
    conn.close()
    return out, dict(totals)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/data/nas-curator.db")
    ap.add_argument("--since", required=True,
                    help="只汇总此时间之后创建的计划（ISO，如 2026-11-19T03:52:00）")
    args = ap.parse_args()
    plans, totals = summarize(args.db, args.since)
    print(json.dumps({"plans": plans, "totals": totals},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
