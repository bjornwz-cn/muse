#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0.1.4 回归：半自动升级——版本比较、VERSION 文件、新版本检查、升级 API。

只用临时 DB 与临时目录，不碰项目 data/nas-curator.db 与 NAS。
版本检查的网络调用全部 mock，不发真实请求。
"""
import base64
import io
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

os.environ.setdefault("CURATOR_DB", os.path.join(tempfile.gettempdir(), "p2-smoke.db"))

from common import upgrade_check as UC  # noqa: E402
from common.version import (  # noqa: E402
    compare_versions, parse_version, parse_version_payload, read_local_version)


# ---------- 版本比较 ----------

def test_parse_version():
    assert parse_version("0.1.4")[0] == (0, 1, 4)
    assert parse_version("v0.1.4")[0] == (0, 1, 4)
    assert parse_version("0.1.4-beta")[1] == "beta"
    with pytest.raises(ValueError):
        parse_version("abc")
    with pytest.raises(ValueError):
        parse_version("1.2.3.4.5")
    print("parse_version: PASS")


def test_compare_versions():
    assert compare_versions("0.1.4", "0.1.3") > 0
    assert compare_versions("0.1.3", "0.1.4") < 0
    assert compare_versions("0.1.4", "0.1.4") == 0
    assert compare_versions("0.1.10", "0.1.9") > 0  # 数字比较，不是字符串比较
    assert compare_versions("0.1", "0.1.0") == 0
    assert compare_versions("0.1.4-beta", "0.1.4") < 0  # 预发布 < 正式版
    assert compare_versions("v0.1.5", "0.1.4") > 0
    print("compare_versions: PASS")


def test_parse_version_payload():
    d = parse_version_payload({"version": "0.1.4", "date": "2026-11-20", "notes": "x"})
    assert d == {"version": "0.1.4", "date": "2026-11-20", "notes": "x"}
    d = parse_version_payload({"version": "0.1.5"})
    assert d["date"] == "" and d["notes"] == ""
    with pytest.raises(ValueError):
        parse_version_payload({"date": "2026-11-20"})  # 缺 version
    with pytest.raises(ValueError):
        parse_version_payload({"version": "not-a-version"})
    with pytest.raises(ValueError):
        parse_version_payload(["0.1.4"])
    print("parse_version_payload: PASS")


def test_read_local_version():
    v = read_local_version()
    assert v["version"] == "0.1.9", v  # 与项目 VERSION 文件一致
    missing = read_local_version("/tmp/definitely-not-there-VERSION")
    assert missing["version"] == "0.0.0-dev"  # 缺失不抛错
    print("read_local_version: PASS (local=%s)" % v["version"])


# ---------- 新版本检查（mock 网络） ----------

class _FakeResp:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen_ok(payload):
    def _open(req, timeout=None):
        content = base64.b64encode(json.dumps(payload).encode("utf-8")).decode()
        return _FakeResp({"content": content, "encoding": "base64"})
    return _open


def test_check_latest_newer_available(tmp_path, monkeypatch):
    cache = str(tmp_path)
    opener = _fake_urlopen_ok({"version": "0.1.5", "date": "2026-11-21", "notes": "新功能"})
    r = UC.check_latest(cache, "0.1.4", urlopen=opener)
    assert r["latest"] == "0.1.5" and r["update_available"] is True
    assert r["cached"] is False and r["error"] is None
    # 缓存已写：第二次不发请求也返回同一结果
    calls = []
    r2 = UC.check_latest(cache, "0.1.4", urlopen=lambda *a, **k: calls.append(1))
    assert r2["latest"] == "0.1.5" and r2["cached"] is True and calls == []
    print("check_latest newer+cached: PASS")


def test_check_latest_same_version(tmp_path):
    opener = _fake_urlopen_ok({"version": "0.1.4"})
    r = UC.check_latest(str(tmp_path), "0.1.4", urlopen=opener)
    assert r["latest"] == "0.1.4" and r["update_available"] is False
    print("check_latest same: PASS")


def test_check_latest_fetch_failure_keeps_cache(tmp_path, monkeypatch):
    cache = str(tmp_path)
    # 先写一份旧缓存（0.1.5 可用）
    UC.check_latest(cache, "0.1.4",
                    urlopen=_fake_urlopen_ok({"version": "0.1.5"}))
    # 再把缓存改旧（超过 6 小时），网络此时失败
    p = os.path.join(cache, "check.json")
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    data["checked_at"] -= 7 * 3600
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)

    def _boom(req, timeout=None):
        raise OSError("network down")

    r = UC.check_latest(cache, "0.1.4", urlopen=_boom)
    assert r["error"] is not None
    assert r["latest"] == "0.1.5" and r["update_available"] is True  # 旧缓存仍可用
    assert r["cached"] is True
    print("check_latest failure keeps cache: PASS")


def test_check_latest_no_cache_and_failure(tmp_path):
    def _boom(req, timeout=None):
        raise OSError("network down")

    r = UC.check_latest(str(tmp_path), "0.1.4", urlopen=_boom)
    assert r["error"] is not None and r["latest"] == ""
    assert r["update_available"] is False
    print("check_latest no cache + failure: PASS")


# ---------- 升级 API ----------

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("db") / "upgrade.db")
    os.environ["CURATOR_DB"] = db
    from api.main import app
    from common import db as DB
    DB.init_db(db)
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


def _upgrade_dir():
    from api.deps import get_upgrade_dir
    return get_upgrade_dir()


def test_upgrade_status_idle(client):
    r = client.get("/api/upgrade/status")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["state"] == "idle" and d["current_version"] == "0.1.9"
    print("upgrade status idle: PASS")


def test_upgrade_check_uses_cache_no_network(client, tmp_path, monkeypatch):
    # 预置新鲜缓存，API 不应发真实请求
    from common import upgrade_check as UCmod
    d = _upgrade_dir()
    os.makedirs(d, exist_ok=True)
    import time
    with open(os.path.join(d, "check.json"), "w", encoding="utf-8") as f:
        json.dump({"current": "0.1.9", "latest": "0.1.10", "update_available": True,
                   "date": "2026-11-21", "notes": "新功能",
                   "checked_at": int(time.time()), "cached": False, "error": None}, f)
    calls = []
    monkeypatch.setattr(UCmod, "_fetch_latest",
                        lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(AssertionError("不应发请求")))
    r = client.get("/api/upgrade/check")
    assert r.status_code == 200, r.text
    assert r.json()["latest"] == "0.1.10" and r.json()["update_available"] is True
    assert calls == []
    print("upgrade check cached: PASS")


def test_upgrade_request_validation(client):
    # 目标 <= 当前 → 400
    r = client.post("/api/upgrade/request", json={"target_version": "0.1.4"})
    assert r.status_code == 400, r.text
    r = client.post("/api/upgrade/request", json={"target_version": "0.1.3"})
    assert r.status_code == 400, r.text
    # 非法版本 → 400
    r = client.post("/api/upgrade/request", json={"target_version": "abc"})
    assert r.status_code == 400, r.text
    # 缺字段 → 400
    r = client.post("/api/upgrade/request", json={})
    assert r.status_code == 400, r.text
    print("upgrade request validation: PASS")


def test_upgrade_request_and_status_flow(client):
    d = _upgrade_dir()
    for name in ("request.json", "status.json"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            os.remove(p)
    r = client.post("/api/upgrade/request", json={"target_version": "0.1.10"})
    assert r.status_code == 202, r.text
    assert r.json()["target_version"] == "0.1.10"
    # 请求标志文件已落盘
    with open(os.path.join(d, "request.json"), encoding="utf-8") as f:
        req = json.load(f)
    assert req["target_version"] == "0.1.10" and req["status"] == "requested"
    # 进行中时再提交 → 409
    r = client.post("/api/upgrade/request", json={"target_version": "0.1.11"})
    assert r.status_code == 409, r.text
    # 状态接口反映 requested
    r = client.get("/api/upgrade/status")
    assert r.json()["state"] == "requested"
    assert r.json()["target_version"] == "0.1.10"
    # 模拟 watcher 写的状态（running → done）
    with open(os.path.join(d, "status.json"), "w", encoding="utf-8") as f:
        json.dump({"state": "running", "stage": "build", "message": "构建中",
                   "target_version": "0.1.10", "current_version": "0.1.9",
                   "started_at": "2026-11-20T00:00:00", "finished_at": "",
                   "error": "", "rolled_back": False}, f)
    r = client.get("/api/upgrade/status")
    assert r.json()["state"] == "running" and r.json()["stage"] == "build"
    with open(os.path.join(d, "status.json"), "w", encoding="utf-8") as f:
        json.dump({"state": "done", "stage": "done", "message": "升级完成",
                   "target_version": "0.1.10", "current_version": "0.1.9",
                   "started_at": "2026-11-20T00:00:00",
                   "finished_at": "2026-11-20T00:05:00",
                   "error": "", "rolled_back": False}, f)
    r = client.get("/api/upgrade/status")
    assert r.json()["state"] == "done"
    # 完成后可再次提交新请求
    r = client.post("/api/upgrade/request", json={"target_version": "0.1.11"})
    assert r.status_code == 202, r.text
    # 清理：恢复 idle
    for name in ("request.json", "status.json"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            os.remove(p)
    print("upgrade request/status flow: PASS")


def test_dashboard_includes_version(client):
    r = client.get("/api/dashboard")
    assert r.status_code == 200, r.text
    assert r.json().get("version") == "0.1.9"
    print("dashboard version: PASS")


def test_upgrade_shell_scripts_syntax():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("scripts/upgrade.sh", "scripts/upgrade-watcher.sh",
                 "scripts/sync-code-to-nas.sh"):
        p = os.path.join(root, name)
        r = subprocess.run(["bash", "-n", p], capture_output=True, text=True)
        assert r.returncode == 0, f"{name} 语法错误：{r.stderr}"
        assert os.access(p, os.X_OK), f"{name} 缺少可执行权限"
    print("upgrade shell scripts syntax: PASS")
