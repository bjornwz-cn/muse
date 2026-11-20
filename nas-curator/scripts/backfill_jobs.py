#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回填历史任务行（0.1.7 一次性脚本）：给 plans 表里没有对应 job 行的计划补 done 行。

背景：0.1.7 之前只有 POST /api/jobs（Web 发起）才写 jobs 行，CLI 跑的
plan（含 P4 各批、今天的 plan 77–80）与收件箱扫描在任务页完全不可见。
本脚本只写 jobs 表，不动 plans / plan_items / review_queue。

口径：
- 只补有实际内容的计划（plan_items 总数 > 0）。0 项的收件箱自动空扫
不补——补了就是把“空扫刷屏”从未来搬到历史里，与 0.1.7 的不刷屏口径相悖。
- 每行：kind=plan，status=done，started_at/finished_at 取 plans.created_at，
summary 带 plan_id、move/review 数（按 plan_items 的 action 现算）、
library 与 source=backfill。
- 幂等：已补过的 plan_id（jobs.summary 里已有）跳过，可反复跑。
- 只用 sqlite3，不依赖项目 import，可直接经 docker exec 喂 stdin 跑。

用法：
python3 scripts/backfill_jobs.py [--db /data/nas-curator.db] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys


def _existing_plan_ids(conn) -> set:
out = set()
for (summary,) in conn.execute("SELECT summary FROM jobs"):
if not summary:
continue
try:
pid = json.loads(summary).get("plan_id")
except ValueError:
continue
if isinstance(pid, int):
out.add(pid)
return out


def main(argv=None) -> int:
ap = argparse.ArgumentParser(description="回填历史计划的 jobs 行（0.1.7）")
ap.add_argument("--db", default=os.environ.get("CURATOR_DB") or "/data/nas-curator.db")
ap.add_argument("--dry-run", action="store_true", help="只打印，不写入")
args = ap.parse_args(argv)

conn = sqlite3.connect(args.db)
conn.row_factory = sqlite3.Row
existing = _existing_plan_ids(conn)
plans = conn.execute(
"SELECT id, library, created_at FROM plans ORDER BY id").fetchall()
to_add = []
skipped_empty = 0
skipped_done = 0
for p in plans:
if p["id"] in existing:
skipped_done += 1
continue
counts = conn.execute(
"SELECT COUNT(*) AS n,"
" SUM(CASE WHEN action IN ('move','rename') THEN 1 ELSE 0 END) AS move,"
" SUM(CASE WHEN action='review' THEN 1 ELSE 0 END) AS review"
" FROM plan_items WHERE plan_id=?", (p["id"],)).fetchone()
n = counts["n"] or 0
if n == 0:
skipped_empty += 1
continue
to_add.append({
"plan_id": p["id"],
"library": p["library"],
"created_at": p["created_at"],
"move": counts["move"] or 0,
"review": counts["review"] or 0,
})

for row in to_add:
summary = {"plan_id": row["plan_id"], "move": row["move"],
"review": row["review"], "library": row["library"],
"source": "backfill"}
print(f" plan #{row['plan_id']}（{row['library']}）："
f"move {row['move']} / review {row['review']} / {row['created_at']}")
if not args.dry_run:
conn.execute(
"INSERT INTO jobs (kind, status, started_at, finished_at, summary)"
" VALUES ('plan', 'done',?,?,?)",
(row["created_at"], row["created_at"],
json.dumps(summary, ensure_ascii=False)))
if not args.dry_run:
conn.commit()
print(f"回填 {len(to_add)} 行（跳过已有 {skipped_done} 个、0 项空计划 {skipped_empty} 个）"
+ ("" if args.dry_run else ""))
return 0


if __name__ == "__main__":
sys.exit(main())
