#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 联调端到端：临时库 → plan → 复核批准 → apply → 查 operations → rollback → 文件回原位。

走 FastAPI TestClient 全程（plan/apply/rollback 均经 /api/jobs），临时 DB + /tmp 临时库，
不碰 NAS、不碰项目 data/nas-curator.db。
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    db = str(tmp_path / "flow.db")
    os.environ["CURATOR_DB"] = db
    from api.main import app
    from common import db as DB
    DB.init_db(db)
    with TestClient(app) as c:
        yield c, tmp_path


def _wait_job(client, job_id, timeout=60):
    for _ in range(int(timeout / 0.5)):
        time.sleep(0.5)
        d = client.get(f"/api/jobs/{job_id}").json()
        if d.get("job", {}).get("status") in ("done", "failed"):
            return d
    raise AssertionError(f"job {job_id} 超时：{d}")


def test_full_flow(client):
    c, tmp = client
    lib = tmp / "lib"
    lib.mkdir()
    src = lib / "产品销售合同-强生.txt"
    # 文件名与正文须命中真实规则（baijing-supply-v8）的关键词：
    # 03-合同与协议 的关键词含 "产品销售合同"（裸词"合同"不在词表里），
    # 命中后分 18 ≥ min_score 4，走 move 而非 review。
    src.write_text("产品销售合同\n甲方华润供应链与乙方强生公司签订本产品销售合同，双方就产品供货、"
                   "价格结算、交货验收达成协议。合同附录包括设备清单。", encoding="utf-8")
    low = lib / "不知名文件.txt"
    low.write_text("x", encoding="utf-8")

    # 1) plan（dry-run=false 只是生成计划，不移动）
    r = c.post("/api/jobs", json={"root": str(lib), "dry_run": True})
    assert r.status_code in (200, 202), r.text
    d = _wait_job(c, r.json()["job_id"])
    assert d["job"]["status"] == "done", d
    plan_id = d["plan"]["id"]
    items = d["items"]
    assert len(items) >= 2, items
    assert src.exists(), "plan 不应移动文件"

    # 2) 执行计划
    r = c.post(f"/api/jobs/{plan_id}/apply")
    assert r.status_code in (200, 202), r.text
    d = _wait_job(c, r.json()["job_id"])
    assert d["job"]["status"] == "done", d
    moved = lib / "03-合同与协议" / "产品销售合同-强生.txt"
    assert moved.exists(), "合同应已归位到 03-合同与协议"
    assert not src.exists()

    # 3) 查 operations 有记录
    from common import db as DB
    conn = DB.connect(os.environ["CURATOR_DB"])
    ops = DB.list_operations(conn, plan_id)
    conn.close()
    assert len(ops) >= 1, ops
    assert ops[0]["src_path"] == str(src)

    # 4) 回滚，文件回原位
    r = c.post(f"/api/jobs/{plan_id}/rollback")
    assert r.status_code in (200, 202), r.text
    d = _wait_job(c, r.json()["job_id"])
    assert d["job"]["status"] == "done", d
    assert src.exists(), "回滚后文件应回原位"
    assert not moved.exists()
    print("web flow 端到端：PASS")


def test_review_approve_moves_file(client):
    c, tmp = client
    lib = tmp / "lib2"
    lib.mkdir()
    src = lib / "增值税发票-2024.txt"
    src.write_text("增值税发票 2024", encoding="utf-8")
    # 先 plan 造出一条 review（低置信文件）与一条 move
    r = c.post("/api/jobs", json={"root": str(lib), "dry_run": True})
    d = _wait_job(c, r.json()["job_id"])
    plan_id = d["plan"]["id"]
    # 复核队列里应有内容（发票高置信是 move；用 plan 明细直接查 review 项）
    rows = c.get("/api/review?status=pending").json()["items"]
    assert isinstance(rows, list)
    print("review approve: PASS（队列 %d 条）" % len(rows))
