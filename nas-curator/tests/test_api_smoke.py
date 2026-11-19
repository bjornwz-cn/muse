#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 联调：FastAPI TestClient 跑通六页 API（总览/复核/任务/浏览/规则/设置）。

只用临时 DB（CURATOR_DB 指向 tmp），不碰项目 data/nas-curator.db 与 NAS。
规则层 plan（use_llm/use_sem 默认关），不调模型。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

os.environ.setdefault("CURATOR_DB", os.path.join(tempfile.gettempdir(), "p2-smoke.db"))

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("db") / "smoke.db")
    os.environ["CURATOR_DB"] = db
    from api.main import app
    from common import db as DB
    DB.init_db(db)
    conn = DB.connect(db)
    # 造 2 条复核 + 1 个计划（不依赖引擎）
    pid = DB.create_plan(conn, "work", None, {"n": 2, "move": 1, "review": 1})
    DB.add_plan_item(conn, pid, {
        "src": "/tmp/x/a.txt", "dst": "/tmp/x/01-合同与协议/a.txt",
        "new_name": "a.txt", "category": "01-合同与协议", "confidence": 0.9,
        "reason": "rule", "evidence": "", "action": "move", "kind": "document",
        "size": 10, "ext": ".txt", "sha256": "", "tags": [], "review_kind": None,
        "score": 12, "auto_exempt": False})
    DB.add_review(conn, plan_id=pid, kind="low_conf", payload={
        "src": "/tmp/x/b.txt", "category": "01-合同与协议", "confidence": 0.4,
        "reason": "low:3", "evidence": "合同", "suggested_dst": "", "tags": []})
    conn.close()
    with TestClient(app) as c:
        yield c


def test_dashboard(client):
    r = client.get("/api/dashboard")
    assert r.status_code == 200, r.text
    d = r.json()
    assert "libraries" in d and "review_pending" in d
    assert d["review_pending"] >= 1
    print("dashboard: PASS")


def test_review_list_and_decide(client):
    r = client.get("/api/review?status=pending")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) >= 1
    rid = rows[0]["id"]
    # 拒绝：源文件不存在，只改状态
    r = client.post(f"/api/review/{rid}/decide", json={"decision": "rejected"})
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True
    r = client.get("/api/review?status=pending")
    assert all(x["id"] != rid for x in r.json())
    print("review list+decide: PASS")


def test_jobs_dry_run_tmp_library(client, tmp_path):
    lib = tmp_path / "lib"
    (lib / "sub").mkdir(parents=True)
    (lib / "供货合同-强生.txt").write_text("供货合同 强生 2024", encoding="utf-8")
    (lib / "sub" / "周报.txt").write_text("周报 工作", encoding="utf-8")
    r = client.post("/api/jobs", json={"root": str(lib), "dry_run": True})
    assert r.status_code in (200, 202), r.text
    job_id = r.json()["job_id"]
    import time
    for _ in range(60):
        time.sleep(0.5)
        d = client.get(f"/api/jobs/{job_id}").json()
        if d.get("job", {}).get("status") in ("done", "failed"):
            break
    assert d["job"]["status"] == "done", d
    assert d.get("plan") is not None
    assert len(d.get("items", [])) >= 1
    print("jobs dry-run: PASS (plan #%s, %d items)" % (d["plan"]["id"], len(d["items"])))


def test_browse_and_file(client, tmp_path):
    lib = tmp_path / "lib2"
    lib.mkdir()
    (lib / "a.txt").write_text("hello", encoding="utf-8")
    r = client.get("/api/browse", params={"dir": str(lib)})
    assert r.status_code == 200, r.text
    assert any(e["name"] == "a.txt" for e in r.json()["entries"])
    r = client.get("/api/browse/file", params={"path": str(lib / "a.txt")})
    assert r.status_code == 200, r.text
    assert r.json()["path"] == str(lib / "a.txt")
    print("browse: PASS")


def test_rules_and_settings(client):
    r = client.get("/api/rules")
    assert r.status_code == 200, r.text
    d = r.json()
    assert "work" in d and "thresholds" in d
    assert d["thresholds"]["auto"] == 0.85
    r = client.get("/api/settings")
    assert r.status_code == 200, r.text
    d = r.json()
    assert "libraries" in d and "inbox" in d
    # 阈值可写回
    r = client.put("/api/settings", json={"thresholds": {"auto": 0.86}})
    assert r.status_code == 200, r.text
    r = client.get("/api/settings")
    assert r.json()["thresholds"]["auto"] == 0.86
    # 还原
    client.put("/api/settings", json={"thresholds": {"auto": 0.85}})
    print("rules+settings: PASS")
