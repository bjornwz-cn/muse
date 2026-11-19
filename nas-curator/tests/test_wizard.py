#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3：首次安装向导测试（TestClient，临时 DB + 临时配置目录）。

覆盖：GET /setup 200；未完成向导时页面路由跳 /setup；POST /api/setup
写回 libraries.json（只改 root）+ key 进 .env.local（600）+ 打完成标记；
完成后 / 恢复 200。测试只用 tmp，不碰项目 config/ 与 data/。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="p3-wizard-")
os.environ["CURATOR_DB"] = os.path.join(_TMP, "wizard.db")
os.environ["CURATOR_CONFIG_DIR"] = os.path.join(_TMP, "config")

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from common import db as DB  # noqa: E402

SHIPPED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "config", "libraries.json")


@pytest.fixture(scope="module")
def client():
    import shutil
    os.makedirs(os.environ["CURATOR_CONFIG_DIR"], exist_ok=True)
    shutil.copyfile(SHIPPED, os.path.join(os.environ["CURATOR_CONFIG_DIR"],
                                          "libraries.json"))
    DB.init_db(os.environ["CURATOR_DB"])
    with TestClient(app) as c:
        yield c


def test_setup_page_200(client):
    r = client.get("/setup")
    assert r.status_code == 200, r.text
    assert "首次安装向导" in r.text


def test_page_redirects_before_setup(client):
    # 未完成向导：页面路由跳 /setup；API 不受影响
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303, r.status_code
    assert r.headers["location"] == "/setup"
    r = client.get("/api/dashboard")
    assert r.status_code == 200


def test_setup_submit_and_done(client):
    body = {
        "libraries": [{"id": "work", "root": "/vol1/1000/工作文件"},
                      {"id": "family", "root": "/vol1/1000/家庭文件"},
                      {"id": "media", "root": "/vol1/1000/媒体库"}],
        "inbox": {"path": "/vol1/1000/收件箱",
                  "scan_interval_minutes": 30, "settle_minutes": 3},
        "input_sources": [{"id": "mac-backup",
                           "root": "/vol1/1000/下载/Wangzhen-Mac"}],
        "model_endpoints": {"llm_host": "http://172.17.0.1:11434",
                            "sem_host": "http://172.17.0.1:11500",
                            "ocr_url": "http://172.17.0.1:8866"},
        "thresholds": {"auto": 0.85, "review_suggest": 0.60},
        "auto_apply": True,
        "keys": {"ark_plan": "sk-test-plan-key-12345"},
    }
    r = client.post("/api/setup", json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["setup_done"] is True
    # key 只返回掩码，不回显明文
    assert d["keys"]["ark_plan"] == "sk****45"
    assert "sk-test-plan-key-12345" not in json.dumps(d)
    # libraries.json 写回：root 改了，其他字段保留
    cfg = json.load(open(os.path.join(os.environ["CURATOR_CONFIG_DIR"],
                                      "libraries.json"), encoding="utf-8"))
    work = [x for x in cfg["libraries"] if x["id"] == "work"][0]
    assert work["root"] == "/vol1/1000/工作文件"
    assert work.get("rules") == "baijing-supply-v8"  # 其他字段保留
    assert cfg["model_endpoints"]["llm_host"] == "http://172.17.0.1:11434"
    assert cfg["auto_apply"] is True
    # .env.local 600 且含该 key 行
    env_path = os.path.join(os.environ["CURATOR_CONFIG_DIR"], ".env.local")
    assert oct(os.stat(env_path).st_mode & 0o777) == "0o600"
    assert "ARK_PLAN_KEY=sk-test-plan-key-12345" in open(env_path).read()
    # 完成后 / 恢复 200，不再跳转
    r = client.get("/")
    assert r.status_code == 200, r.status_code
    r = client.get("/api/setup")
    assert r.json()["setup_done"] is True
