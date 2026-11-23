#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0.2.0：任务进度——进度计算、心跳契约、兜底推导与进度接口。

只用临时 DB 与临时目录，不碰项目 data/nas-curator.db 与 NAS。
进度数字必须来自真实计数：本文件断言的计数全部由测试自己造的
plan_items / 心跳文件 / 日志夹具得出，不编造。
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from common import progress as PROG


# ---------------------------------------------------------------- compute_progress

def test_compute_progress_normal():
    # 100 项，10 分钟前开始，已做 60、跳过 10、问题 5 → 待处理 25，
    # 速率 6 项/分钟，ETA 25/6 分钟 = 250 秒。
    p = PROG.compute_progress(100, 60, 10, 5,
                              "2026-11-23T14:00:00", None,
                              "2026-11-23T14:10:00")
    assert p["total"] == 100 and p["done"] == 60
    assert p["pending"] == 25 and p["skipped"] == 10 and p["failed"] == 5
    assert p["pct"] == 60.0
    assert p["rate_per_min"] == 6.0
    assert p["eta_seconds"] == 250
    assert p["elapsed_s"] == 600


def test_compute_progress_no_rate_when_too_fast():
    # 已耗时不足 60 秒：速率与 ETA 一律 None（前端显示 —，不编造）。
    p = PROG.compute_progress(100, 5, 0, 0,
                              "2026-11-23T14:09:30", None,
                              "2026-11-23T14:10:00")
    assert p["rate_per_min"] is None and p["eta_seconds"] is None
    assert p["elapsed_s"] == 30


def test_compute_progress_unknown_total():
    # 总数未知（计划生成中）：pct/pending/ETA 全为 None。
    p = PROG.compute_progress(None, 0, 0, 0, "2026-11-23T14:00:00")
    assert p["total"] is None and p["pct"] is None
    assert p["pending"] is None and p["eta_seconds"] is None


def test_compute_progress_finished_empty_plan():
    # 已结束、总数 0：pct 按 100 计（不是除零错）。
    p = PROG.compute_progress(0, 0, 0, 0,
                              "2026-11-23T14:00:00", "2026-11-23T14:05:00")
    assert p["pct"] == 100.0 and p["pending"] == 0
    assert p["eta_seconds"] is None and p["finished"] is True


def test_compute_progress_pending_clamps_at_zero():
    # 计数异常（done+skipped+failed 超过 total）时 pending 下限 0。
    p = PROG.compute_progress(10, 9, 2, 1, "2026-11-23T14:00:00")
    assert p["pending"] == 0


# ---------------------------------------------------------------- 心跳

def test_heartbeat_roundtrip(tmp_path):
    d = str(tmp_path / "progress")
    rec = PROG.write_heartbeat(
        "vlm-apply", progress_dir=d, label="VLM 高置信自动归档",
        kind="vlm-apply", phase="apply", status="running",
        total=100, done=40, skipped=5, failed=2,
        errors=["rq=1: api_error"], now="2026-11-23T14:10:00")
    assert rec["task_id"] == "vlm-apply"
    assert rec["total"] == 100 and rec["done"] == 40
    assert rec["pending"] == 53 and rec["pct"] == 40.0
    assert rec["source"] == "heartbeat"
    assert os.path.exists(os.path.join(d, "vlm-apply.json"))
    rows = PROG.read_heartbeats(d, now="2026-11-23T14:10:00")
    assert len(rows) == 1 and rows[0]["task_id"] == "vlm-apply"
    assert rows[0]["stale"] is False


def test_heartbeat_keeps_started_at_across_writes(tmp_path):
    d = str(tmp_path / "progress")
    PROG.write_heartbeat("t1", progress_dir=d, total=10, done=1,
                         now="2026-11-23T14:00:00")
    rec = PROG.write_heartbeat("t1", progress_dir=d, total=10, done=2,
                               now="2026-11-23T14:05:00")
    # 同一轮运行不重置计时：started_at 仍是第一次写的时间。
    assert rec["started_at"] == "2026-11-23T14:00:00"
    assert rec["elapsed_s"] == 300
    assert rec["rate_per_min"] == pytest.approx(0.4)


def test_heartbeat_stale_after_10_minutes(tmp_path):
    d = str(tmp_path / "progress")
    PROG.write_heartbeat("t1", progress_dir=d, total=10, done=1,
                         now="2026-11-23T14:00:00")
    rows = PROG.read_heartbeats(d, now="2026-11-23T14:10:01")
    assert rows[0]["stale"] is True  # 10 分 01 秒未更新 → 已失联
    rows = PROG.read_heartbeats(d, now="2026-11-23T14:09:59")
    assert rows[0]["stale"] is False
    # 已结束的心跳永不标 stale。
    PROG.write_heartbeat("t1", progress_dir=d, total=10, done=10,
                         status="done", now="2026-11-23T14:00:00")
    rows = PROG.read_heartbeats(d, now="2026-11-23T23:00:00")
    assert rows[0]["stale"] is False


def test_heartbeat_bad_task_id_rejected(tmp_path):
    with pytest.raises(ValueError):
        PROG.write_heartbeat("../evil", progress_dir=str(tmp_path))
    with pytest.raises(ValueError):
        PROG.write_heartbeat("a/b", progress_dir=str(tmp_path))


def test_heartbeat_broken_file_skipped(tmp_path):
    d = tmp_path / "progress"
    d.mkdir()
    (d / "bad.json").write_text("{not json", encoding="utf-8")
    (d / "good.json").write_text(json.dumps(
        {"task_id": "good", "status": "running",
         "updated_at": "2026-11-23T14:00:00"}), encoding="utf-8")
    rows = PROG.read_heartbeats(str(d), now="2026-11-23T14:00:00")
    assert [r["task_id"] for r in rows] == ["good"]


# ---------------------------------------------------------------- 兜底推导：年份重分类

def _write_year_fixtures(d, n_results=8600, total=14315, elapsed=3829):
    log = (
        "disk media files: 16843 (photo 13896, video 2320)\n"
        "existing review rows under inbox/media: 3154\n"
        "already classified in /data/year_siglip_results.jsonl: 0\n"
        "reuse existing classification: 2528; to classify (SigLIP): %d\n"
        "PROG 8400/%d 3719s\n"
        "PROG 8600/%d 3829s\n" % (total, total, total))
    (d / "year_siglip.log").write_text(log, encoding="utf-8")
    with open(d / "year_siglip_results.jsonl", "w", encoding="utf-8") as f:
        for i in range(n_results):
            f.write(json.dumps({"src": "/x/%d.jpg" % i}) + "\n")


def test_derive_year_progress_from_log_and_jsonl(tmp_path):
    d = tmp_path
    _write_year_fixtures(d)
    rec = PROG.derive_year_progress(str(d), now="2026-11-23T15:21:00")
    assert rec is not None
    assert rec["source"] == "derived"
    assert rec["task_id"] == "year-reclassify"
    assert rec["total"] == 14315 and rec["done"] == 8600
    assert rec["pending"] == 5715
    assert rec["pct"] == pytest.approx(60.1, abs=0.1)
    # 速率来自脚本自报的 PROG：8600 项 / 3829 秒 ≈ 134.8 项/分钟。
    assert rec["rate_per_min"] == pytest.approx(134.8, abs=0.5)
    assert rec["eta_seconds"] == pytest.approx(2544, abs=60)
    assert rec["phase"] == "siglip" and rec["status"] == "running"


def test_derive_year_progress_none_when_no_artifacts(tmp_path):
    assert PROG.derive_year_progress(str(tmp_path)) is None


def test_derive_year_progress_done_when_summary_exists(tmp_path):
    d = tmp_path
    _write_year_fixtures(d)
    (d / "year_reclassify_summary.json").write_text(json.dumps({
        "disk_media_photo": 13896, "disk_media_video": 2320,
        "review_rows_on_disk": 16000, "unclassified": 120}),
        encoding="utf-8")
    rec = PROG.derive_year_progress(str(d))
    assert rec["status"] == "done" and rec["phase"] == "summary"
    assert rec["total"] == 16216 and rec["done"] == 16000
    assert rec["extra"]["unclassified"] == 120


def test_derive_year_progress_subtracts_preexisting_lines(tmp_path):
    # 真实情况（2026-11-23 实测）：JSONL 在本轮开始前已有 500 行（上一轮
    # 断点续跑），直接数行数会把 done 算多 500、百分比破百。
    d = tmp_path
    _write_year_fixtures(d, n_results=9100, total=14315, elapsed=3829)
    log = (d / "year_siglip.log").read_text(encoding="utf-8")
    log = log.replace("already classified in /data/year_siglip_results.jsonl: 0",
                      "already classified in /data/year_siglip_results.jsonl: 500")
    log = log.replace("reuse existing classification: 2528",
                      "reuse existing classification: 1401")
    (d / "year_siglip.log").write_text(log, encoding="utf-8")
    rec = PROG.derive_year_progress(str(d), now="2026-11-23T15:21:00")
    assert rec["total"] == 14315 and rec["done"] == 8600  # 9100 − 500
    assert rec["pct"] == pytest.approx(60.1, abs=0.1)
    assert rec["extra"]["already_classified"] == 500
    assert rec["extra"]["reused_existing"] == 1401


# ---------------------------------------------------------------- API

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("db") / "jobs-progress.db")
    os.environ["CURATOR_DB"] = db
    from api.main import app
    from common import db as DB
    DB.init_db(db)
    conn = DB.connect(db)
    # 计划 #1：4 项——done 2、pending 1、skipped 1，另 1 项 failed（带原因）。
    pid = DB.create_plan(conn, "work", None, {"n": 5})
    for i, st in enumerate(["done", "done", "pending", "skipped", "failed"]):
        DB.add_plan_item(conn, pid, {
            "src": "/tmp/x/%d.txt" % i, "dst": "/tmp/y/%d.txt" % i,
            "category": "c", "confidence": 0.9,
            "reason": "boom" if st == "failed" else "rule",
            "evidence": "", "action": "move", "kind": "document",
            "size": 10, "ext": ".txt", "sha256": "", "tags": [],
            "review_kind": None, "status": st})
    # job 1：apply 进行中（started 10 分钟前），挂计划 #1。
    jid = DB.create_job(conn, "apply")
    conn.execute(
        "UPDATE jobs SET started_at='2026-11-23T14:00:00',"
        " summary=? WHERE id=?",
        (json.dumps({"plan_id": pid}), jid))
    # job 2：plan 已完成（无 plan 项，summary 有计数）。
    jid2 = DB.create_job(conn, "plan")
    DB.finish_job(conn, jid2, "done", {"plan_id": None, "move": 3, "review": 2})
    conn.commit()
    conn.close()
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c, db


def test_api_jobs_list_has_progress(client):
    c, _db = client
    r = c.get("/api/jobs")
    assert r.status_code == 200, r.text
    jobs = {j["id"]: j for j in r.json()}
    p = jobs[1]["progress"]
    # 真实计数：done 2 / pending 1 / skipped 1 / failed 1，总数 5。
    assert p["total"] == 5 and p["done"] == 2 and p["pending"] == 1
    assert p["skipped"] == 1 and p["failed"] == 1
    assert p["pct"] == 40.0 and p["phase"] == "执行中"
    assert p["plan_id"] == 1
    # 已完成任务按 summary 计数算 100%。
    assert jobs[2]["progress"]["pct"] == 100.0
    assert jobs[2]["progress"]["phase"] == "已完成"


def test_api_job_progress_detail_failed_items(client):
    c, _db = client
    r = c.get("/api/jobs/1/progress")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["progress"]["total"] == 5
    assert d["failed_total"] == 1
    assert len(d["failed_items"]) == 1
    assert d["failed_items"][0]["src_path"] == "/tmp/x/4.txt"
    assert d["failed_items"][0]["reason"] == "boom"
    r = c.get("/api/jobs/999/progress")
    assert r.status_code == 404


def test_api_running_includes_job_and_heartbeat(client):
    c, db = client
    # 心跳文件写进 DB 同目录的 progress/（默认心跳目录）；用真实当前时间写，
    # 否则读时会因超过 10 分钟而被标 stale。
    PROG.write_heartbeat(
        "vlm-reclassify", label="VLM 看图重分类", kind="vlm-reclassify",
        phase="vlm", status="running", total=100, done=30)
    r = c.get("/api/jobs/running")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [j["id"] for j in d["jobs"]] == [1]  # 只有 running 的 job
    assert d["jobs"][0]["progress"]["total"] == 5
    scripts = {t["task_id"]: t for t in d["scripts"]}
    assert "vlm-reclassify" in scripts
    assert scripts["vlm-reclassify"]["done"] == 30
    assert scripts["vlm-reclassify"]["stale"] is False
    # 年份推导：本测试 DB 目录下无 year 产物，不应出现。
    assert "year-reclassify" not in scripts


def test_api_running_derived_year_task(client, tmp_path):
    c, db = client
    data_dir = os.path.dirname(db)
    _write_year_fixtures(Path(data_dir))
    r = c.get("/api/jobs/running")
    assert r.status_code == 200, r.text
    scripts = {t["task_id"]: t for t in r.json()["scripts"]}
    assert "year-reclassify" in scripts
    assert scripts["year-reclassify"]["source"] == "derived"
    assert scripts["year-reclassify"]["total"] == 14315
    assert scripts["year-reclassify"]["done"] == 8600
