#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态资源版本缓存破坏（0.2.0 教训）：
用户真机报错「C.progressBar is not a function」——模板调用 C.progressBar，
但 script 标签是 <script src="/static/app.js">，无缓存破坏参数；
浏览器缓存的是 0.1.9 的旧 app.js（6,491 字节，无 progressBar），
新版是 8,284 字节。修法：base.html 的 app.js/style.css 带 ?v=<VERSION>，
版本号启动时从 VERSION 文件读（与 VERSION 联动，升版自动换 URL，
浏览器必取新文件）。本文件回归：页面 HTML 必须含版本化 URL，
且版本化 URL 确实返回含 progressBar 的新 JS。
只用临时 DB，不碰项目 data/nas-curator.db 与 NAS。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

os.environ.setdefault("CURATOR_DB", os.path.join(tempfile.gettempdir(), "p2-static-ver.db"))

from fastapi.testclient import TestClient  # noqa: E402

from common.version import read_local_version  # noqa: E402

VERSION = read_local_version()["version"]
APP_JS_URL = "/static/app.js?v=" + VERSION
STYLE_URL = "/static/style.css?v=" + VERSION


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("db") / "static-ver.db")
    os.environ["CURATOR_DB"] = db
    from api.main import app
    from common import db as DB
    DB.init_db(db)
    with TestClient(app) as c:
        yield c


def test_index_has_versioned_assets(client):
    r = client.get("/")
    assert r.status_code == 200, r.text
    assert APP_JS_URL in r.text
    assert STYLE_URL in r.text


def test_jobs_page_has_versioned_assets(client):
    r = client.get("/jobs")
    assert r.status_code == 200, r.text
    assert APP_JS_URL in r.text


def test_setup_page_has_versioned_assets(client):
    r = client.get("/setup")
    assert r.status_code == 200, r.text
    assert APP_JS_URL in r.text


def test_versioned_static_serves_new_js(client):
    # 版本化 URL 必须返回新版 app.js（8,284 字节，含 progressBar），
    # 而不是浏览器缓存的 0.1.9 旧版（6,491 字节，无 progressBar）。
    r = client.get(APP_JS_URL)
    assert r.status_code == 200, r.text[:200]
    assert "progressBar" in r.text
    local = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "web", "static", "app.js")
    assert len(r.content) == os.path.getsize(local)


def test_base_template_uses_version_variable():
    # 模板必须用 {{ app_version }} 变量，不能写死版本号，
    # 否则下次升版又会忘改、重蹈 0.2.0 的覆辙。
    tpl = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "web", "templates", "base.html")
    with open(tpl, encoding="utf-8") as f:
        html = f.read()
    assert "?v={{ app_version }}" in html
    assert "app.js?v=0." not in html and "style.css?v=0." not in html
