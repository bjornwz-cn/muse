#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0.1.7：CLI 发起的任务也要在 jobs 表留记录。

背景：此前只有 POST /api/jobs（Web 发起）才写 jobs 行；CLI 的 plan /
apply / rollback 与收件箱监听从不写，导致任务页与总览「最近任务」里
看不到这些任务（王震 2026-11-20 截图报“看不到任务、看不到进度”）。

口径：
- CLI plan/apply/rollback：开始建 running 行，结束 finish 为 done/failed，
  summary 带 source=cli 与 plan_id、move/review（apply 带 moved）。
- 收件箱监听：30 分钟一次的自动空扫（0 项）不留行，有动作或失败才留行；
  手动单次扫描无论 0 项与否都留行。
- API 后台线程已自建 job 行（_record_job=False），不由本文件测；
  test_api_smoke / test_web_flow 覆盖 API 路径不断言行数翻倍。

只用 tmp 目录与临时 DB，不碰 NAS。注意：api.jobs 不在模块顶层 import
（api/deps.py 在 import 时冻结 CURATOR_CONFIG_DIR；见 test_apply_gating.py
文件头警告）。
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import curator as CUR  # noqa: E402
from common import db as DB  # noqa: E402

RULES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "curator_core", "rules.baijing-supply-v8.json")

EXCLUDED_ROOT = "/vol1/1000/下载/Wangzhen-Mac/muse"  # guard 排除列表内


def _mk_tree(root):
    os.makedirs(os.path.join(root, "待处理"), exist_ok=True)
    files = {
        "顶级合同.txt": "产品销售合同 甲方北京红橙天天健康科技有限公司",
        os.path.join("待处理", "报价单.txt"): "产品销售合同报价清单",
    }
    for rel, text in files.items():
        with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
            f.write(text)
    return files


def _jobs(conn):
    out = []
    for r in conn.execute("SELECT * FROM jobs ORDER BY id"):
        d = dict(r)
        if d.get("summary"):
            d["summary"] = json.loads(d["summary"])
        out.append(d)
    return out


def _run_plan(root, db, out):
    ns = CUR.build_parser().parse_args(
        ["--db", db, "plan", "--root", root, "--rules", RULES,
         "--no-ocr", "--deep", "--out", out])
    assert CUR.cmd_plan(ns) == 0
    conn = DB.connect(db)
    plan_id = conn.execute("SELECT MAX(id) FROM plans").fetchone()[0]
    return conn, plan_id


def test_cli_plan_records_done_job(tmp_path):
    root = str(tmp_path / "lib")
    os.makedirs(root)
    _mk_tree(root)
    db = str(tmp_path / "t.db")
    conn, plan_id = _run_plan(root, db, str(tmp_path / "plan.csv"))

    rows = _jobs(conn)
    assert len(rows) == 1, rows
    job = rows[0]
    assert job["kind"] == "plan" and job["status"] == "done", job
    assert job["started_at"] and job["finished_at"], job
    s = job["summary"]
    assert s["source"] == "cli" and s["plan_id"] == plan_id, s
    assert s["move"] == 2 and s["review"] == 0, s
    conn.close()
    print(f"CLI plan 记 done 行：plan #{plan_id}，move 2")


def test_cli_plan_failure_records_failed_job(tmp_path):
    db = str(tmp_path / "t.db")
    ns = CUR.build_parser().parse_args(
        ["--db", db, "plan", "--root", EXCLUDED_ROOT, "--no-ocr"])
    with pytest.raises(ValueError):
        CUR.cmd_plan(ns)
    conn = DB.connect(db)
    rows = _jobs(conn)
    assert len(rows) == 1, rows
    job = rows[0]
    assert job["kind"] == "plan" and job["status"] == "failed", job
    assert "error" in (job["summary"] or {}), job
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    conn.close()
    print("CLI plan 失败记 failed 行：排除路径被 guard 拒绝")


def test_cli_apply_and_rollback_record_jobs(tmp_path):
    root = str(tmp_path / "lib")
    os.makedirs(root)
    files = _mk_tree(root)
    db = str(tmp_path / "t.db")
    conn, plan_id = _run_plan(root, db, str(tmp_path / "plan.csv"))

    ns = CUR.build_parser().parse_args(
        ["--db", db, "apply", "--plan-id", str(plan_id)])
    assert CUR.cmd_apply(ns) == 0
    rows = _jobs(conn)
    assert len(rows) == 2, rows
    apply_job = rows[1]
    assert apply_job["kind"] == "apply" and apply_job["status"] == "done", apply_job
    assert apply_job["summary"]["plan_id"] == plan_id, apply_job["summary"]
    assert apply_job["summary"]["moved"] == 2, apply_job["summary"]
    # 文件确实被移动了（dst 在 tmp 树内）
    assert not os.path.exists(os.path.join(root, "顶级合同.txt"))
    assert os.path.exists(os.path.join(root, "03-合同与协议", "顶级合同.txt"))

    ns = CUR.build_parser().parse_args(
        ["--db", db, "rollback", "--plan-id", str(plan_id)])
    assert CUR.cmd_rollback(ns) == 0
    rows = _jobs(conn)
    assert len(rows) == 3, rows
    rb_job = rows[2]
    assert rb_job["kind"] == "rollback" and rb_job["status"] == "done", rb_job
    assert rb_job["summary"]["plan_id"] == plan_id, rb_job["summary"]
    assert rb_job["summary"]["moved"] == 2, rb_job["summary"]
    assert os.path.exists(os.path.join(root, "顶级合同.txt"))
    conn.close()
    print("CLI apply/rollback 各记 done 行，文件移动与回滚均真实发生")


def test_inbox_watch_empty_scan_leaves_no_job(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    db = str(tmp_path / "t.db")
    ns = CUR.build_parser().parse_args(
        ["--db", db, "inbox", "--inbox", str(inbox), "--watch",
         "--max-runs", "1", "--no-ocr", "--out", str(tmp_path / "plan.csv")])
    assert CUR.cmd_inbox(ns) == 0
    conn = DB.connect(db)
    # 扫描确实跑了（留下一个 0 项 plan 行），但 jobs 表不留行
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1
    assert _jobs(conn) == []
    conn.close()
    print("收件箱自动空扫：plan 留行、jobs 不留行")


def test_inbox_manual_empty_scan_records_job(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    db = str(tmp_path / "t.db")
    ns = CUR.build_parser().parse_args(
        ["--db", db, "inbox", "--inbox", str(inbox),
         "--no-ocr", "--out", str(tmp_path / "plan.csv")])
    assert CUR.cmd_inbox(ns) == 0
    conn = DB.connect(db)
    rows = _jobs(conn)
    assert len(rows) == 1, rows
    job = rows[0]
    assert job["kind"] == "plan" and job["status"] == "done", job
    assert job["summary"]["source"] == "cli", job["summary"]
    assert job["summary"]["move"] == 0 and job["summary"]["review"] == 0
    conn.close()
    print("收件箱手动单次空扫：留 done 行（0 项）")


def test_inbox_watch_scan_with_files_records_job(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    f = inbox / "合同.txt"
    f.write_text("产品销售合同 甲方北京红橙天天健康科技有限公司", encoding="utf-8")
    # settle 默认 3 分钟：把 mtime 拨回 10 分钟前，否则文件会被当“未稳定”跳过
    old = time.time() - 600
    os.utime(f, (old, old))
    db = str(tmp_path / "t.db")
    ns = CUR.build_parser().parse_args(
        ["--db", db, "inbox", "--inbox", str(inbox), "--watch",
         "--max-runs", "1",
         "--no-ocr", "--out", str(tmp_path / "plan.csv")])
    assert CUR.cmd_inbox(ns) == 0
    conn = DB.connect(db)
    rows = _jobs(conn)
    assert len(rows) == 1, rows
    job = rows[0]
    assert job["kind"] == "plan" and job["status"] == "done", job
    assert job["summary"]["source"] == "inbox-watch", job["summary"]
    assert job["summary"]["move"] >= 1, job["summary"]
    conn.close()
    print("收件箱自动扫描有动作：留 done 行")
