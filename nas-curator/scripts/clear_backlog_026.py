#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0.2.6 存量清仓执行脚本（容器内运行，/app 在 path 上）。

按 P0→P5 顺序清复核队列。只移动文件进目标库/holding 区，绝不删除用户文件；
dup 只进 holding 区，不走 dup_delete。
用法（容器内）：
  python3 /app/scripts/clear_backlog_026.py p5 --dry-run
  python3 /app/scripts/clear_backlog_026.py p5 --apply
  python3 /app/scripts/clear_backlog_026.py p2 --apply --limit 200
  python3 /app/scripts/clear_backlog_026.py p3 --apply
  python3 /app/scripts/clear_backlog_026.py p1 --apply
每步先 dry-run 报数，再小批量 apply，抽查核对后再全量。
"""
import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, "/app")

DB_PATH = os.environ.get("CURATOR_DB", "/data/nas-curator.db")
CONFIG_PATH = "/data/config/libraries.json"
RULES_PATH = "/app/curator_core/rules.baijing-supply-v8.json"
MEDIA_CONFIG_PATH = "/app/media_core/config.json"


def _load_roots():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    roots = {}
    for lib in cfg.get("libraries", []):
        roots[lib["id"]] = lib["root"]
    roots["inbox"] = cfg.get("inbox", {}).get("path", "")
    return roots


def _get_rows(conn, kind, reason_like=None, limit=None):
    sql = ("SELECT id, json_extract(payload,'$.src') AS src, "
           "json_extract(payload,'$.suggested_dst') AS dst, "
           "json_extract(payload,'$.reason') AS reason, "
           "json_extract(payload,'$.category') AS category, payload "
           "FROM review_queue WHERE status='pending' AND kind=?")
    args = [kind]
    if reason_like:
        sql += " AND json_extract(payload,'$.reason') LIKE ?"
        args.append(reason_like)
    sql += " ORDER BY id"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return conn.execute(sql, args).fetchall()


def _decide(conn, rid, dst, decided_by, batch_id=None):
    """调 api.review.decide_one 批准移动（dst 为建议目标）。"""
    from api.review import DecideBody, decide_one
    body = DecideBody(decision="approved", suggested_dst=dst)
    return decide_one(conn, rid, "approved", body,
                      decided_by=decided_by, batch_id=batch_id)


def _update_payload_dst(conn, rid, dst, confidence=None, reason=None):
    """只改复核行的 suggested_dst（与置信度/原因），不改状态。"""
    row = conn.execute("SELECT payload FROM review_queue WHERE id=?",
                       (rid,)).fetchone()
    payload = json.loads(row[0]) if row and row[0] else {}
    payload["suggested_dst"] = dst
    if confidence is not None:
        payload["confidence"] = confidence
    if reason is not None:
        payload["reason"] = reason
    conn.execute("UPDATE review_queue SET payload=? WHERE id=?",
                 (json.dumps(payload, ensure_ascii=False), rid))
    conn.commit()


def step_p5(conn, apply, limit):
    """P5 shell：107 条空壳，suggested_dst 已齐全，整批批准移入 99-待确认/空壳。"""
    rows = _get_rows(conn, "shell", limit=limit)
    print(f"P5 shell: {len(rows)} 条")
    moved = skipped = 0
    for r in rows:
        rid, src, dst = r[0], r[1], r[2]
        if not dst:
            print(f"  #{rid} 无 suggested_dst，跳过")
            skipped += 1
            continue
        if not apply:
            print(f"  dry-run #{rid} -> {dst}")
            continue
        res = _decide(conn, rid, dst, "system:clear-026-p5")
        if res.get("moved"):
            moved += 1
        else:
            print(f"  #{rid} 未移动：{res.get('note')}")
            skipped += 1
    print(f"P5 结果：moved={moved} skipped={skipped}")
    return moved


def step_p2(conn, apply, limit):
    """P2 dup：源已不存在的直接系统结案；源存在的分批批准进 holding 区（不删除）。"""
    rows = _get_rows(conn, "dup", limit=limit)
    print(f"P2 dup: {len(rows)} 条")
    gone = [r for r in rows if not r[1] or not os.path.exists(r[1])]
    exists = [r for r in rows if r[1] and os.path.exists(r[1])]
    print(f"  源已不存在：{len(gone)} 条；源存在：{len(exists)} 条")
    moved = gone_n = skipped = 0
    for r in gone:
        if not apply:
            continue
        res = _decide(conn, r[0], "", "system:dup-src-gone")
        if res.get("ok"):
            gone_n += 1
        else:
            skipped += 1
    for i, r in enumerate(exists, 1):
        if not apply:
            if i <= 5:
                print(f"  dry-run #{r[0]} {os.path.basename(r[1] or '')} -> holding")
            continue
        res = _decide(conn, r[0], "", "system:clear-026-p2")
        if res.get("moved"):
            moved += 1
        else:
            print(f"  #{r[0]} 未移动：{res.get('note')}")
            skipped += 1
        if i % 100 == 0:
            print(f"  ...已处理 {i}/{len(exists)}，moved={moved}")
    print(f"P2 结果：moved(进 holding)={moved} src-gone 结案={gone_n} "
          f"skipped={skipped}")
    return moved, gone_n


def step_p3(conn, apply, limit):
    """P3 扫描件文件名优先：565 条 scanned-no-text，先跑 scan_name_category。
    分 >=8（conf 0.85，可自动）直接批准移动；4-7 分进复核带建议；无命中不动。"""
    from curator_core.engine import scan_name_category
    roots = _load_roots()
    work_root = roots.get("work", "")
    with open(RULES_PATH, encoding="utf-8") as f:
        rules = json.load(f)
    cats = rules.get("categories", [])
    rows = _get_rows(conn, "ambiguous", reason_like="scanned-no-text%",
                     limit=limit)
    print(f"P3 scanned-no-text: {len(rows)} 条")
    auto = suggest = none = moved = skipped = 0
    for r in rows:
        rid, src, dst, reason = r[0], r[1], r[2], r[3]
        name = os.path.basename(src or "")
        ext = os.path.splitext(name)[1].lower()
        fcat, fsc, freason, fev = scan_name_category(name, ext, cats)
        if not fcat:
            none += 1
            continue
        target = os.path.join(work_root, fcat, name)
        if fsc >= 8:
            auto += 1
            if not apply:
                if auto <= 5:
                    print(f"  dry-run auto #{rid} {name} -> {fcat} (score {fsc})")
                continue
            res = _decide(conn, rid, target, "system:clear-026-p3")
            if res.get("moved"):
                moved += 1
            else:
                print(f"  #{rid} 未移动：{res.get('note')}")
                skipped += 1
        else:
            suggest += 1
            if apply:
                _update_payload_dst(conn, rid, target, confidence=0.7,
                                    reason=f"scan-name:{freason}")
    print(f"P3 结果：可自动={auto}（moved={moved}） 带建议={suggest} "
          f"无命中={none} skipped={skipped}")
    return moved, suggest, none


def step_p1(conn, apply, limit):
    """P1 blur：用媒体分类器重跑 blur 行。截图类/小图跳过模糊判定后走内容分类，
    置信度达标则批准移动；仍判模糊的留队列；转低置信的改 kind 为 low_conf。"""
    from media_core.engine import build_classifier, plan_media_files
    with open(MEDIA_CONFIG_PATH, encoding="utf-8") as f:
        media_cfg = json.load(f)
    roots = _load_roots()
    media_cfg["library_root"] = roots.get("media", "")
    print("P1：加载媒体分类器（torch/SigLIP，约需 1-2 分钟）…")
    cls = build_classifier(media_cfg)
    print("P1：分类器已加载")
    rows = _get_rows(conn, "blur", limit=limit)
    print(f"P1 blur: {len(rows)} 条")
    moved = still_blur = lowconf = skipped = gone = 0
    for i, r in enumerate(rows, 1):
        rid, src = r[0], r[1]
        if not src or not os.path.exists(src):
            gone += 1
            continue
        try:
            items, _stats = plan_media_files([src], media_cfg, classifier=cls)
        except Exception as e:  # noqa: BLE001
            print(f"  #{rid} 分类异常：{e}")
            skipped += 1
            continue
        if not items:
            skipped += 1
            continue
        it = items[0]
        if it.get("action") == "move" and it.get("dst"):
            if not apply:
                if moved < 5:
                    print(f"  dry-run move #{rid} {os.path.basename(src)} "
                          f"-> {it['dst']}")
                moved += 1
                continue
            res = _decide(conn, rid, it["dst"], "system:clear-026-p1")
            if res.get("moved"):
                moved += 1
            else:
                print(f"  #{rid} 未移动：{res.get('note')}")
                skipped += 1
        elif it.get("review_kind") == "blur" or it.get("reason") == "blurry":
            still_blur += 1
        elif it.get("action") == "review":
            lowconf += 1
            if apply:
                conn.execute("UPDATE review_queue SET kind='low_conf' "
                             "WHERE id=?", (rid,))
                conn.commit()
        else:
            skipped += 1
        if i % 200 == 0:
            print(f"  ...已处理 {i}/{len(rows)}，moved={moved} "
                  f"仍模糊={still_blur} 转低置信={lowconf}")
    print(f"P1 结果：moved={moved} 仍模糊={still_blur} 转低置信={lowconf} "
          f"源不存在={gone} skipped={skipped}")
    return moved


def main():
    ap = argparse.ArgumentParser(description="0.2.6 存量清仓执行脚本")
    ap.add_argument("step", choices=["p1", "p2", "p3", "p5"])
    ap.add_argument("--apply", action="store_true", help="真的执行（默认 dry-run）")
    ap.add_argument("--limit", type=int, default=None, help="最多处理条数")
    args = ap.parse_args()
    from common import db as DB
    conn = DB.connect(DB_PATH)
    try:
        if args.step == "p5":
            step_p5(conn, args.apply, args.limit)
        elif args.step == "p2":
            step_p2(conn, args.apply, args.limit)
        elif args.step == "p3":
            step_p3(conn, args.apply, args.limit)
        elif args.step == "p1":
            step_p1(conn, args.apply, args.limit)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
