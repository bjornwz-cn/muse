#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复核队列重评（0.1.2 路径先验规则）。

对 pending 的 low_conf 项逐条重评建议：
- clear：文件仍在库内正确位置（路径先验成立），且原建议是无信号的默认
  （reason=default）或无正文（no-text:待确认）-> 标记为 approved，
  decided_by=system:reeval-0.1.2，payload 记 reeval 说明。只改状态，不动文件。
- downgrade：文件已在库内正确位置，但原建议是真实分类器的低置信结论
  （low:margin / low-text / unreadable 等）-> 保留在队列，category 改为
  路径先验标签（如 家庭/王震/通话录音），reason 追加路径先验说明。
- keep：文件尚未归位（收件箱/笔记本备份/库外）、源文件已不在、或 kind
  不是 low_conf -> 不动。

只改 review_queue 行；绝不动 plans / plan_items / operations；
绝不动 /vol1 下任何文件；绝不动 repair-media 容器（本脚本只读写 SQLite）。

用法（容器内跑，DB 在 /data/nas-curator.db）：
  dry-run（默认，只算不写）：
    python3 reevaluate_review.py --db /data/nas-curator.db --config /data/config/libraries.json
  应用（先备份 DB 文件）：
    python3 reevaluate_review.py --db /data/nas-curator.db --config /data/config/libraries.json \\
        --apply --backup /data/nas-curator.db.bak-20261120-reeval
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import db as DB  # noqa: E402
from common import routing as RT  # noqa: E402

DECIDED_BY = "system:reeval-0.1.2"

# 可清除的 reason：分类器无任何信号（默认类目）或无正文可判
CLEAR_REASONS = ("default",)
CLEAR_PREFIXES = ("no-text:",)
# 可降级的 reason：真实分类器结论，但低置信且文件已在正确位置
DOWNGRADE_PREFIXES = ("low:", "low-text:", "unreadable:", "whitelist-miss")


def resolve_config_path(arg):
    """--config 既可给文件路径（…/libraries.json），也可给配置目录。"""
    if arg:
        if arg.endswith(".json"):
            return arg
        return os.path.join(arg, "libraries.json")
    cfg_dir = os.environ.get("CURATOR_CONFIG_DIR", "")
    if cfg_dir:
        return os.path.join(cfg_dir, "libraries.json")
    return os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "config", "libraries.json")


def load_roots(config_path):
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        print(f"! 读 libraries.json 失败（{config_path}）：{e}", file=sys.stderr)
        print("! 库根为空时重评会全部 keep，已中止（请用 --config 指到正确的 libraries.json）。",
              file=sys.stderr)
        raise SystemExit(2)
    return RT.roots_from_config(cfg)


def decide_row(payload, roots):
    """返回 (action, label, note)：action 为 clear/downgrade/keep。"""
    src = (payload.get("src") or "")
    label = RT.path_prior_label(src, roots)
    if label is None:
        return "keep", None, "未归位（收件箱/笔记本备份/库外），保留"
    if not os.path.exists(src):
        return "keep", label, "源文件已不在，保留待人工处理"
    reason = payload.get("reason") or ""
    if reason in CLEAR_REASONS or reason.startswith(CLEAR_PREFIXES):
        return ("clear", label,
                f"已在 {label}，原建议 {payload.get('category') or ''}"
                f"（{reason}）无有效信号，确认现状")
    if reason.startswith(DOWNGRADE_PREFIXES):
        return ("downgrade", label,
                f"路径先验：已在 {label}，低置信不跨顶层")
    return "keep", label, f"reason={reason}，保留"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--config", default="")
    ap.add_argument("--apply", action="store_true",
                    help="不加则 dry-run（只算不写）")
    ap.add_argument("--backup", default="",
                    help="--apply 时先把 DB 文件复制到此处")
    args = ap.parse_args()

    cfg_path = resolve_config_path(args.config)
    roots = load_roots(cfg_path)
    print(f"配置：{cfg_path}")
    print(f"库根：{roots}")

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    if args.apply and args.backup:
        # VACUUM INTO：在库在线时做一致性备份（比直接拷 .db 文件可靠，WAL 模式下
        # 单拷 .db 可能缺 -wal 里的数据）。
        conn.execute("VACUUM INTO ?", (args.backup,))
        print(f"已备份 DB 到 {args.backup}")
    before = conn.execute(
        "SELECT COUNT(*) c FROM review_queue WHERE status='pending'"
    ).fetchone()["c"]
    rows = conn.execute(
        "SELECT * FROM review_queue WHERE status='pending' ORDER BY id"
    ).fetchall()
    print(f"pending 总数（重评前）：{before}（其中 low_conf "
          f"{sum(1 for r in rows if r['kind'] == 'low_conf')}）")

    counts = collections.Counter()
    by_reason = collections.Counter()
    cleared_ids, downgraded_ids = [], []
    spotlight = []
    for r in rows:
        if r["kind"] != "low_conf":
            counts["keep:other-kind"] += 1
            continue
        try:
            payload = json.loads(r["payload"] or "{}")
        except ValueError:
            payload = {}
        action, label, note = decide_row(payload, roots)
        by_reason[(action, payload.get("reason") or "")] += 1
        counts[action] += 1
        if "2015-11-28 094806" in (payload.get("src") or ""):
            spotlight.append((r["id"], r["kind"], action, label))
        if not args.apply:
            if action == "clear":
                cleared_ids.append(r["id"])
            elif action == "downgrade":
                downgraded_ids.append(r["id"])
            continue
        if action == "clear":
            DB.decide_review(conn, r["id"], "approved", decided_by=DECIDED_BY)
            payload["reeval"] = "cleared:already-in-place"
            payload["reeval_note"] = note
            conn.execute("UPDATE review_queue SET payload=? WHERE id=?",
                         (json.dumps(payload, ensure_ascii=False), r["id"]))
            conn.commit()
            cleared_ids.append(r["id"])
        elif action == "downgrade":
            payload["category"] = label
            payload["reason"] = (payload.get("reason") or "") + " | " + note
            payload["reeval"] = "downgraded:path-prior"
            conn.execute("UPDATE review_queue SET payload=? WHERE id=?",
                         (json.dumps(payload, ensure_ascii=False), r["id"]))
            conn.commit()
            downgraded_ids.append(r["id"])

    after = conn.execute(
        "SELECT COUNT(*) c FROM review_queue WHERE status='pending'"
    ).fetchone()["c"]
    print(f"== {'APPLY' if args.apply else 'DRY-RUN'} 结果 ==")
    print(f"pending 总数：{before} -> {after}")
    print(f"clear（已在正确位置，确认现状）：{counts['clear']}")
    print(f"downgrade（保留在队列，建议改为路径先验标签）：{counts['downgrade']}")
    print(f"keep：{sum(v for k, v in counts.items() if k.startswith('keep'))}")
    print("== 按 (action, 原 reason) 分布 ==")
    for (a, reason), n in sorted(by_reason.items()):
        print(f"  {a:10s} {reason or '(空)':30s} {n}")
    print(f"clear id 样例（前 10）：{cleared_ids[:10]}")
    print(f"downgrade id 样例（前 10）：{downgraded_ids[:10]}")
    print(f"截图相关条目：{spotlight}")
    if args.apply and args.backup:
        print(f"DB 备份：{args.backup}")
    conn.close()
    return 0


if __name__ == "__main__":
    if "--apply" in sys.argv and "--backup" not in sys.argv:
        print("! --apply 必须同时给 --backup（DB 文件备份路径）", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main())
