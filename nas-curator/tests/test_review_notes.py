#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0.1.5：复核队列每条按 kind 给一句白话“需要你决定什么”（api.review._enrich）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from common import db as DB  # noqa: E402

# 注意：api.main 不在模块顶层 import——api/deps.py 在 import 时就把
# CURATOR_CONFIG_DIR 冻结为 CONFIG_DIR；test_wizard.py 依赖在模块 import 前
# 先设该环境变量。顶层 import api.main 会让 wizard 测试写错配置目录
# （P4 回归曾因此失败；0.1.5 曾因此重演）。与 test_api_smoke /
# test_web_flow / test_apply_gating 一致，api 模块只在函数内懒加载。

EXPECTED = {
    "dup": "这份与另一份内容完全一样，批准即移到重复待确认区，不删",
    "low_conf": "系统没把握、置信度低，请确认分到哪类",
    "blur": "图太糊判不准，请看一眼",
    "ambiguous": "像两类，请选一个",
    "sensitive": "可能含敏感内容，请确认",
    "shell": "空壳文件（内容极小或已损坏），请确认如何处理",
}


def _client(tmp_path, monkeypatch):
    from api.main import create_app  # noqa: E402 - 懒加载，见文件头说明
    db_path = tmp_path / "t.db"
    monkeypatch.setenv("CURATOR_DB", str(db_path))
    monkeypatch.setenv("CURATOR_CONFIG_DIR", str(tmp_path / "config"))
    DB.init_db(str(db_path))
    conn = DB.connect(str(db_path))
    for kind in EXPECTED:
        DB.add_review(conn, file_id=None, plan_id=None, kind=kind,
                      payload={"src": f"/vol1/1000/收件箱/x-{kind}.txt",
                               "category": "06-其他", "confidence": "低",
                               "reason": "测试"})
    conn.commit()
    conn.close()
    return TestClient(create_app())


def test_review_notes_per_kind(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.get("/api/review", params={"status": "pending"})
    assert r.status_code == 200
    notes = {row["kind"]: row["review_note"] for row in r.json()["items"]}
    for kind, note in EXPECTED.items():
        assert notes[kind] == note


def test_review_note_fallback_for_unknown_kind():
    # DB 有 CHECK 约束（kind 只能是 6 种），未知 kind 的兜底直接测字典口径
    from api.review import DEFAULT_REVIEW_NOTE, REVIEW_NOTES
    assert REVIEW_NOTES.get("weird_kind", DEFAULT_REVIEW_NOTE) == "请看一眼再决定"
