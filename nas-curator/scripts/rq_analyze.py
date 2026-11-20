#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复核队列现状分析（只读）。用法：python3 rq_analyze.py --db /data/nas-curator.db"""
import argparse
import collections
import json
import os
import sqlite3
import sys


def top_of(src):
    if not src:
        return "unknown"
    if src.startswith("/vol1/1000/工作文件/"):
        return "work"
    if src.startswith("/vol1/1000/家庭文件/"):
        return "family"
    if src.startswith("/vol1/1000/媒体库/"):
        return "media"
    if src.startswith("/vol1/1000/收件箱/"):
        return "inbox"
    if src.startswith("/vol1/1000/下载/Wangzhen-Mac/"):
        return "mac-backup"
    if src.startswith("/vol1/1000/"):
        return "vol1-other"
    return "other"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--dump", default="",
                    help="把 pending 队列全量写 JSON 到该路径（容器内路径）")
    args = ap.parse_args()
    conn = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    conn.row_factory = sqlite3.Row

    print("== review_queue by kind/status ==")
    for r in conn.execute(
            "SELECT kind, status, COUNT(*) c FROM review_queue GROUP BY kind, status ORDER BY kind, status"):
        print(f"  {r['kind']:12s} {r['status']:10s} {r['c']}")

    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM review_queue WHERE status='pending' ORDER BY id")]
    print(f"== pending total: {len(rows)} ==")
    by_top_kind = collections.Counter()
    cat_counter = collections.Counter()
    conf_buckets = collections.Counter()
    dst_nonempty = collections.Counter()
    for r in rows:
        try:
            p = json.loads(r.get("payload") or "{}")
        except ValueError:
            p = {}
        r["_p"] = p
        by_top_kind[(top_of(p.get("src") or ""), r["kind"])] += 1
        cat_counter[(r["kind"], p.get("category") or "")] += 1
        conf = p.get("confidence")
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = None
        if conf is None:
            conf_buckets[(r["kind"], "none")] += 1
        elif conf < 0.6:
            conf_buckets[(r["kind"], "<0.6")] += 1
        elif conf < 0.85:
            conf_buckets[(r["kind"], "0.6-0.85")] += 1
        else:
            conf_buckets[(r["kind"], ">=0.85")] += 1
        if p.get("suggested_dst"):
            dst_nonempty[r["kind"]] += 1
    print("== pending by src-top / kind ==")
    for (t, k), c in sorted(by_top_kind.items()):
        print(f"  {t:12s} {k:12s} {c}")
    print("== pending category top 15 ==")
    for (k, c), n in cat_counter.most_common(15):
        print(f"  {k:12s} {c or '(空)':40s} {n}")
    print("== confidence buckets ==")
    for (k, b), n in sorted(conf_buckets.items()):
        print(f"  {k:12s} {b:8s} {n}")
    print("== suggested_dst 非空 ==")
    for k, n in sorted(dst_nonempty.items()):
        print(f"  {k:12s} {n}")
    print("== 截图那条（刘音 2015-11-28 094806） ==")
    for r in rows:
        p = r["_p"]
        if "2015-11-28 094806" in (p.get("src") or ""):
            print(f"  id={r['id']} kind={r['kind']} plan={r['plan_id']} payload={json.dumps(p, ensure_ascii=False)}")
    if args.dump:
        out = [{k: v for k, v in r.items() if k != "_p"} for r in rows]
        with open(args.dump, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        print(f"dumped {len(out)} rows to {args.dump}")
    print("== categories ==")
    for r in conn.execute("SELECT id, path, kind FROM categories ORDER BY id"):
        print(f"  {r['id']:3d} {r['path']}  [{r['kind']}]")
    print("== plans with review items (top) ==")
    for r in conn.execute(
            "SELECT plan_id, COUNT(*) c FROM plan_items WHERE action='review' GROUP BY plan_id ORDER BY plan_id"):
        print(f"  plan {r['plan_id']}: {r['c']} review items")


if __name__ == "__main__":
    sys.exit(main())
