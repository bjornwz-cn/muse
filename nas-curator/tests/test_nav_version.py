#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0.1.5（补充）：顶部导航常驻版本号 + 有新版时的小提示。

覆盖：六页模板都渲染出 nav-version / nav-update 占位；版本号取数来源
（/api/dashboard 的 version）与新版提示取数来源（/api/upgrade/check 的
latest/update_available）的数据口径；app.js 里接线存在。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PAGES = ["/", "/review", "/jobs", "/browse", "/rules", "/settings"]

# 注意：api.main 不在模块顶层 import——api/deps.py 在 import 时就把
# CURATOR_CONFIG_DIR 冻结为 CONFIG_DIR；test_wizard.py 依赖在模块 import 前
# 先设该环境变量。顶层 import api.main 会让 wizard 测试写错配置目录
# （P4 回归曾因此失败；0.1.5 曾因此重演）。api 模块只在 fixture 内懒加载。


@pytest.fixture()
def client(tmp_path):
    db = str(tmp_path / "nav.db")
    os.environ["CURATOR_DB"] = db
    from api.deps import get_upgrade_dir, mark_setup_done  # noqa: E402
    from api.main import app  # noqa: E402
    from common import db as DB  # noqa: E402
    DB.init_db(db)
    mark_setup_done()
    with TestClient(app) as c:
        yield c, tmp_path, get_upgrade_dir()


def test_all_pages_show_nav_version(client):
    c, _, _ = client
    for path in PAGES:
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code)
        assert 'id="nav-version"' in r.text, path
        assert 'id="nav-update"' in r.text, path
        assert "当前版本" in r.text, path


def test_nav_version_data_source(client):
    """版本号取数来源：/api/dashboard 的 version 字段（与总览页同一来源）。"""
    c, _, _ = client
    r = c.get("/api/dashboard")
    assert r.status_code == 200, r.text
    assert r.json().get("version") == "0.1.6"


def _seed_check(upgrade_dir, current, latest):
    os.makedirs(upgrade_dir, exist_ok=True)
    with open(os.path.join(upgrade_dir, "check.json"), "w",
              encoding="utf-8") as f:
        json.dump({"current": current, "latest": latest,
                   "update_available": latest > current,
                   "date": "2026-11-21", "notes": "新功能",
                   "checked_at": int(time.time()), "cached": False,
                   "error": None}, f)


def test_nav_update_hint_when_newer_available(client):
    """有新版本时 /api/upgrade/check 给出 latest + update_available=True
   （前端据此在版本号旁显示“有新版 X”，点进总览页升级）。"""
    c, _, upgrade_dir = client
    _seed_check(upgrade_dir, "0.1.6", "0.1.7")
    r = c.get("/api/upgrade/check")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["latest"] == "0.1.7" and d["update_available"] is True


def test_nav_update_hint_hidden_when_latest(client):
    """无新版本时 update_available=False（前端不打扰，不显示提示）。"""
    c, _, upgrade_dir = client
    _seed_check(upgrade_dir, "0.1.6", "0.1.6")
    r = c.get("/api/upgrade/check")
    assert r.status_code == 200, r.text
    assert r.json()["update_available"] is False


def test_app_js_wires_nav_version(client):
    c, _, _ = client
    r = c.get("/static/app.js")
    assert r.status_code == 200, r.text
    assert "nav-version" in r.text
    assert "nav-update" in r.text
    assert "/api/upgrade/check" in r.text
    assert "有新版" in r.text
