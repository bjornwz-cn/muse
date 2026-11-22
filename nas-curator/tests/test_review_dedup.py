#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0.1.9 回归：复核去重——同一批文件规划两次，review_queue 里每个 src
只能有一条 pending 行（#143/#144/#145 三遍重复建计划致 91,070 行、
唯一文件仅 31,193 个的教训）。

口径（复用 #142 重跑脚本）：建复核行前查 review_queue，同一 src 已有
pending 或已裁决（approved/rejected）行时跳过建行；plan_items 审计行
照常记。覆盖三条建行路径：_store_plan（CLI plan / watcher inbox-watch /
process，文档与媒体引擎都经此入库）、_apply_items（process/inbox --apply
门控拦下的低置信项）、api.jobs._run_auto_apply（Web auto_apply 拦下的项）。
只用 tmp 目录与临时 DB，不碰 NAS、不碰生产 review_queue。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import curator as CUR  # noqa: E402
from common import db as DB  # noqa: E402


def _items(src_review, src_move):
    return [
        {"src": src_review, "dst": "/tmp/dst/b.jpg", "new_name": "b.jpg",
         "category": "照片/日常生活", "confidence": 0.46,
         "reason": "siglip:0.46", "evidence": "", "action": "review",
         "kind": "media", "size": 10, "ext": ".jpg", "sha256": "",
         "tags": [], "review_kind": "low_conf", "score": 0.46,
         "auto_exempt": False},
        {"src": src_move, "dst": "/tmp/dst/a.jpg", "new_name": "a.jpg",
         "category": "照片/日常生活", "confidence": 0.95, "reason": "rule",
         "evidence": "", "action": "move", "kind": "media", "size": 10,
         "ext": ".jpg", "sha256": "", "tags": [], "review_kind": None,
         "score": 12, "auto_exempt": False},
    ]


def _rows_for_src(conn, src):
    return conn.execute(
        "SELECT * FROM review_queue WHERE json_extract(payload, '$.src')=?",
        (src,)).fetchall()


def test_has_review_for_src(tmp_path):
    db = str(tmp_path / "t.db")
    conn = DB.connect(db)
    DB.init_db(db)
    src = "/tmp/x/b.jpg"
    assert DB.has_review_for_src(conn, src) is False
    rid = DB.add_review(conn, plan_id=None, kind="low_conf",
                        payload={"src": src, "category": "照片/日常生活",
                                 "confidence": 0.46, "reason": "siglip:0.46"})
    assert DB.has_review_for_src(conn, src) is True
    assert DB.has_review_for_src(conn, "/tmp/x/other.jpg") is False
    DB.decide_review(conn, rid, "approved")
    assert DB.list_review(conn, status="pending") == []
    # 已裁决的行仍挡住重建
    assert DB.has_review_for_src(conn, src) is True
    conn.close()
    print("has_review_for_src: PASS")


def test_store_plan_twice_single_pending_row(tmp_path):
    """同一批文件规划两次：pending 行只有一条；plan_items 两份都在（审计）。"""
    db = str(tmp_path / "t.db")
    conn = DB.connect(db)
    DB.init_db(db)
    items = _items("/tmp/x/b.jpg", "/tmp/x/a.jpg")
    pid1 = CUR._store_plan(conn, "inbox", None, items)
    pid2 = CUR._store_plan(conn, "inbox", None, items)
    assert pid1 != pid2
    rows = _rows_for_src(conn, "/tmp/x/b.jpg")
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "pending"
    assert rows[0]["plan_id"] == pid1  # 留的是第一份，不是指向第二份
    n_items = conn.execute("SELECT COUNT(*) FROM plan_items").fetchone()[0]
    assert n_items == 4  # 两计划各 2 项，审计行照常记
    conn.close()
    print("store_plan twice: PASS")


def test_store_plan_after_decision_no_new_row(tmp_path):
    """已裁决（approved/rejected）的行也不再重建。"""
    db = str(tmp_path / "t.db")
    conn = DB.connect(db)
    DB.init_db(db)
    items = _items("/tmp/x/b.jpg", "/tmp/x/a.jpg")
    CUR._store_plan(conn, "inbox", None, items)
    rid = _rows_for_src(conn, "/tmp/x/b.jpg")[0]["id"]
    DB.decide_review(conn, rid, "rejected")
    CUR._store_plan(conn, "inbox", None, items)
    rows = _rows_for_src(conn, "/tmp/x/b.jpg")
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "rejected"
    assert DB.list_review(conn, status="pending") == []
    conn.close()
    print("store_plan after decision: PASS")


def _apply_args(tmp, db):
    return argparse.Namespace(
        db=db, root=tmp, library="", kind="document", out="", rules="",
        media_config="", ocr_url="http://127.0.0.1:8866", no_ocr=True,
        workers=1, max_chars=2000, ocr_timeout=90, min_score=4,
        content_first=False, cache="", unit_mode="auto", protect="",
        rename=False, rename_llm=False, llm=False,
        llm_host="http://172.17.0.1:11434", llm_model="qwen3:4b",
        llm_min_conf=0.55, llm_max=2000, sem=False, sem_model="bge-m3:latest",
        sem_min_sim=0.42, sem_margin=0.02, dry_run=True, uid=1000, gid=1000,
        map="", report="")


def test_apply_items_gating_twice_single_row(tmp_path):
    """门控拦下的低置信项跑两次 _apply_items，也只有一条 pending 行。"""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    lo = src_dir / "low.txt"
    lo.write_text("低置信文件", encoding="utf-8")
    db = str(tmp_path / "t.db")
    conn = DB.connect(db)
    DB.init_db(db)
    plan_id = DB.create_plan(conn, "inbox", None, {"n": 1, "move": 1, "review": 0})
    items = [{"src": str(lo), "dst": str(tmp_path / "dst" / "low.txt"),
              "new_name": "low.txt", "category": "照片/日常生活",
              "confidence": 0.42, "reason": "siglip:0.42", "evidence": "",
              "action": "move", "kind": "media", "size": 10, "ext": ".txt",
              "sha256": "", "tags": [], "review_kind": None, "score": 0.42,
              "auto_exempt": False}]
    args = _apply_args(str(tmp_path), db)
    CUR._apply_items(items, str(src_dir), args, conn, plan_id)
    CUR._apply_items(items, str(src_dir), args, conn, plan_id)
    assert lo.exists()  # 预演 + 低置信：原地不动
    rows = _rows_for_src(conn, str(lo))
    assert len(rows) == 1, rows
    conn.close()
    print("apply_items gating twice: PASS")


def test_run_auto_apply_twice_single_row(tmp_path):
    """Web auto_apply 同一计划跑两次，拦下的低置信项也只有一条 pending 行。"""
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    lo = src_dir / "low.txt"
    lo.write_text("低置信文件", encoding="utf-8")
    db = str(tmp_path / "t.db")
    conn = DB.connect(db)
    DB.init_db(db)
    plan_id = DB.create_plan(conn, "inbox", None, {"n": 1, "move": 1, "review": 0})
    DB.add_plan_item(conn, plan_id, {
        "src": str(lo), "dst": str(dst_dir / "low.txt"), "new_name": "low.txt",
        "category": "照片/日常生活", "confidence": 0.42, "reason": "siglip:0.42",
        "evidence": "", "action": "move", "kind": "document", "size": 10,
        "ext": ".txt", "sha256": "", "tags": [], "review_kind": None,
        "score": 0.42, "auto_exempt": False})
    job_id = DB.create_job(conn, "apply")
    conn.close()

    from api import jobs as JOBS  # noqa: E402 - 懒加载（见 test_apply_gating.py）
    JOBS._run_auto_apply(job_id, db, plan_id, 0.85)
    job_id2 = DB.create_job(DB.connect(db), "apply")
    JOBS._run_auto_apply(job_id2, db, plan_id, 0.85)

    conn = DB.connect(db)
    assert lo.exists()  # 低置信：原地不动
    rows = _rows_for_src(conn, str(lo))
    assert len(rows) == 1, rows
    assert rows[0]["kind"] == "low_conf"
    conn.close()
    print("run_auto_apply twice: PASS")
