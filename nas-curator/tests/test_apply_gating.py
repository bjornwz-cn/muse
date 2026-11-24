#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3：inbox/process --apply 的自动执行门控测试。

门控口径（与 Web API auto_apply 子集一致；2026-11-24 计划 #160 修复）：
只放行 common.routing.is_auto_apply_eligible 判定的项——confidence >= auto
阈值（默认 0.85）且 kind 不是整目录 unit；整目录 unit 项永不自动执行，
低于阈值的项永不自动执行；auto_exempt 例外已废除。被拦下的项不移动，
改为写入 review_queue（kind=low_conf），出现在 Web 复核队列里。
背景：媒体引擎 score>=0.35 即出 move 项，不门控会把 0.40 置信的文件自动归位。
只用 tmp 目录与临时 DB，不碰 NAS。
"""
import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import curator as CUR  # noqa: E402
from common import db as DB  # noqa: E402

# 注意：api.jobs 不在模块顶层 import——api/deps.py 在 import 时就把
# CURATOR_CONFIG_DIR 冻结为 CONFIG_DIR；test_wizard.py 依赖在模块 import 前
# 先设该环境变量。顶层 import api.jobs 会让 wizard 测试写错配置目录
# （P4 回归曾因此失败）。与 test_api_smoke / test_web_flow 一致，api 模块
# 只在测试函数内懒加载。


def _args(tmp, db):
    return argparse.Namespace(
        db=db, root=tmp, library="", kind="document", out="", rules="",
        media_config="", ocr_url="http://127.0.0.1:8866", no_ocr=True,
        workers=1, max_chars=2000, ocr_timeout=90, min_score=4,
        content_first=False, cache="", unit_mode="auto", protect="",
        rename=False, rename_llm=False, llm=False,
        llm_host="http://172.17.0.1:11434", llm_model="qwen3:4b",
        llm_min_conf=0.55, llm_max=2000, sem=False, sem_model="bge-m3:latest",
        sem_min_sim=0.42, sem_margin=0.02, dry_run=False, uid=1000, gid=1000,
        map="", report="")


def test_apply_items_gates_low_confidence(tmp_path):
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    hi = src_dir / "high.txt"
    lo = src_dir / "low.txt"
    hi.write_text("高置信文件", encoding="utf-8")
    lo.write_text("低置信文件", encoding="utf-8")
    db = str(tmp_path / "t.db")
    conn = DB.connect(db)
    DB.init_db(db)
    plan_id = DB.create_plan(conn, "inbox", None, {"n": 2, "move": 2, "review": 0})
    items = [
        {"src": str(hi), "dst": str(dst_dir / "high.txt"), "new_name": "high.txt",
         "category": "01-合同与协议", "confidence": 0.95, "reason": "rule",
         "evidence": "", "action": "move", "kind": "document", "size": 10,
         "ext": ".txt", "sha256": "", "tags": [], "review_kind": None,
         "score": 12, "auto_exempt": False},
        {"src": str(lo), "dst": str(dst_dir / "low.txt"), "new_name": "low.txt",
         "category": "照片/日常生活", "confidence": 0.42, "reason": "siglip:0.42",
         "evidence": "", "action": "move", "kind": "media", "size": 10,
         "ext": ".txt", "sha256": "", "tags": [], "review_kind": None,
         "score": 0.42, "auto_exempt": False},
    ]
    CUR._apply_items(items, str(src_dir), _args(str(tmp_path), db), conn, plan_id)
    # 高置信：已移动
    assert not hi.exists()
    assert (dst_dir / "high.txt").exists()
    # 低置信：原地不动，且进了复核队列
    assert lo.exists()
    assert not (dst_dir / "low.txt").exists()
    reviews = DB.list_review(conn, status="pending")
    assert len(reviews) == 1
    assert reviews[0]["kind"] == "low_conf"
    assert reviews[0]["payload"]["src"] == str(lo)
    assert reviews[0]["payload"]["confidence"] == 0.42
    conn.close()


def test_run_auto_apply_writes_review_queue(tmp_path):
    """P4：Web API _run_auto_apply 被拦的低置信项也要进复核队列（与 CLI 同口径）。

    之前只置 plan_items.status='skipped'、不写 review_queue，文件会静默留在
    原地、复核页看不到。只用 tmp 目录与临时 DB，不碰 NAS。
    """
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    hi = src_dir / "high.txt"
    lo = src_dir / "low.txt"
    hi.write_text("高置信文件", encoding="utf-8")
    lo.write_text("低置信文件", encoding="utf-8")
    db = str(tmp_path / "t.db")
    conn = DB.connect(db)
    DB.init_db(db)
    plan_id = DB.create_plan(conn, "inbox", None, {"n": 2, "move": 2, "review": 0})
    for src, dst, conf, cat in (
            (hi, dst_dir / "high.txt", 0.95, "01-合同与协议"),
            (lo, dst_dir / "low.txt", 0.42, "照片/日常生活")):
        DB.add_plan_item(conn, plan_id, {
            "src": str(src), "dst": str(dst), "new_name": src.name,
            "category": cat, "confidence": conf, "reason": "siglip:0.42",
            "evidence": "", "action": "move", "kind": "document", "size": 10,
            "ext": ".txt", "sha256": "", "tags": [], "review_kind": None,
            "score": conf, "auto_exempt": False})
    job_id = DB.create_job(conn, "apply")
    conn.close()

    from api import jobs as JOBS  # noqa: E402 - 懒加载，见文件头说明
    JOBS._run_auto_apply(job_id, db, plan_id, 0.85)

    # 高置信：已移动；低置信：原地不动
    assert not hi.exists()
    assert (dst_dir / "high.txt").exists()
    assert lo.exists()
    assert not (dst_dir / "low.txt").exists()
    # 低置信：plan_item 置 skipped，同时进了复核队列
    conn = DB.connect(db)
    items = DB.list_plan_items(conn, plan_id)
    by_src = {i["src_path"]: i for i in items}
    assert by_src[str(hi)]["status"] == "done"
    assert by_src[str(lo)]["status"] == "skipped"
    reviews = DB.list_review(conn, status="pending")
    assert len(reviews) == 1
    assert reviews[0]["kind"] == "low_conf"
    assert reviews[0]["plan_id"] == plan_id
    assert reviews[0]["payload"]["src"] == str(lo)
    assert reviews[0]["payload"]["suggested_dst"] == str(dst_dir / "low.txt")
    assert reviews[0]["payload"]["confidence"] == 0.42
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert job["status"] == "done"
    conn.close()


def _unit_item(src_dir, dst_dir, conf, name="媒体库"):
    """2026-11-24 计划 #160 原形：整目录 unit 项。"""
    src = src_dir / name
    src.mkdir(exist_ok=True)
    (src / "a.jpg").write_text("x", encoding="utf-8")
    (src / "b.jpg").write_text("y", encoding="utf-8")
    return {"src": str(src), "dst": str(dst_dir / name), "new_name": name,
            "category": "12-项目资料/媒体库", "confidence": conf,
            "reason": "unit/project", "evidence": "文件 2 + 子目录 0",
            "action": "move", "kind": "unit", "size": 2, "ext": "(目录)",
            "sha256": "", "tags": [], "review_kind": None,
            "score": 0, "auto_exempt": True}


def test_apply_items_blocks_low_conf_unit_160_regression(tmp_path):
    """2026-11-24 计划 #160 回归：0.4 置信度 unit 整目录不得自动执行，
    即使标了 auto_exempt（旧豁免已废除）。"""
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    unit = _unit_item(src_dir, dst_dir, 0.4)
    db = str(tmp_path / "t.db")
    conn = DB.connect(db)
    DB.init_db(db)
    plan_id = DB.create_plan(conn, "inbox", None, {"n": 1, "move": 1, "review": 0})
    CUR._apply_items([unit], str(src_dir), _args(str(tmp_path), db), conn, plan_id)
    # 整目录原地不动，目标位置没有出现
    assert os.path.isdir(unit["src"])
    assert not os.path.exists(unit["dst"])
    # 进了复核队列（kind=low_conf），人工可点
    reviews = DB.list_review(conn, status="pending")
    assert len(reviews) == 1
    assert reviews[0]["kind"] == "low_conf"
    assert reviews[0]["payload"]["src"] == unit["src"]
    assert reviews[0]["payload"]["confidence"] == 0.4
    conn.close()


def test_apply_items_blocks_unit_even_when_high_confidence(tmp_path):
    """整目录 unit 项永不自动执行——即使置信度 0.9 也不行，一律进复核。"""
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    unit = _unit_item(src_dir, dst_dir, 0.9)
    db = str(tmp_path / "t.db")
    conn = DB.connect(db)
    DB.init_db(db)
    plan_id = DB.create_plan(conn, "inbox", None, {"n": 1, "move": 1, "review": 0})
    CUR._apply_items([unit], str(src_dir), _args(str(tmp_path), db), conn, plan_id)
    assert os.path.isdir(unit["src"])
    assert not os.path.exists(unit["dst"])
    reviews = DB.list_review(conn, status="pending")
    assert len(reviews) == 1
    assert reviews[0]["kind"] == "low_conf"
    conn.close()


def test_run_auto_apply_blocks_unit(tmp_path):
    """Web API _run_auto_apply 同口径：unit 整目录不得自动执行。"""
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    unit = _unit_item(src_dir, dst_dir, 0.9)
    hi = src_dir / "high.txt"
    hi.write_text("高置信文件", encoding="utf-8")
    db = str(tmp_path / "t.db")
    conn = DB.connect(db)
    DB.init_db(db)
    plan_id = DB.create_plan(conn, "inbox", None, {"n": 2, "move": 2, "review": 0})
    for item in ({"src": str(hi), "dst": str(dst_dir / "high.txt"),
                  "new_name": "high.txt", "category": "01-合同与协议",
                  "confidence": 0.95, "reason": "rule", "evidence": "",
                  "action": "move", "kind": "document", "size": 10,
                  "ext": ".txt", "sha256": "", "tags": [], "review_kind": None,
                  "score": 12, "auto_exempt": False}, unit):
        DB.add_plan_item(conn, plan_id, item)
    job_id = DB.create_job(conn, "apply")
    conn.close()

    from api import jobs as JOBS  # noqa: E402 - 懒加载，见文件头说明
    JOBS._run_auto_apply(job_id, db, plan_id, 0.85)

    # 高置信普通文件：已移动；高置信 unit：原地不动
    assert not hi.exists()
    assert (dst_dir / "high.txt").exists()
    assert os.path.isdir(unit["src"])
    assert not os.path.exists(unit["dst"])
    conn = DB.connect(db)
    items = DB.list_plan_items(conn, plan_id)
    by_src = {i["src_path"]: i for i in items}
    assert by_src[str(hi)]["status"] == "done"
    assert by_src[unit["src"]]["status"] == "skipped"
    reviews = DB.list_review(conn, status="pending")
    assert len(reviews) == 1
    assert reviews[0]["kind"] == "low_conf"
    assert reviews[0]["payload"]["src"] == unit["src"]
    conn.close()
