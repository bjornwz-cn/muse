#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests.test_stale_job_reap —— 启动清理僵尸任务（0.2.4）。

reap_stale_running_jobs：只清理 started_at 早于截止时间的 running 行；
done/failed 不动；截止时间之后的 running 行不动；被清理的行带原因。
"""
import sqlite3

import pytest

from common import db as DB


@pytest.fixture
def conn(tmp_path):
    c = DB.connect(str(tmp_path / "t.db"))
    DB.init_db(str(tmp_path / "t.db"))
    yield c
    c.close()


def _mk(conn, started_at, status="running"):
    cur = conn.execute(
        "INSERT INTO jobs (kind, status, started_at) VALUES ('plan', ?, ?)",
        (status, started_at))
    conn.commit()
    return cur.lastrowid


def test_reaps_old_running_jobs(conn):
    old = _mk(conn, "2026-11-26T01:19:50")
    new = _mk(conn, "2026-11-27T07:00:00")
    reaped = DB.reap_stale_running_jobs(conn, before="2026-11-27T06:00:00")
    assert reaped == [old]
    row = conn.execute(
        "SELECT status, summary FROM jobs WHERE id=?", (old,)).fetchone()
    assert row["status"] == "failed"
    assert "启动清理" in row["summary"]
    assert "2026-11-26T01:19:50" in row["summary"]
    assert conn.execute(
        "SELECT status FROM jobs WHERE id=?", (new,)).fetchone()["status"] == "running"


def test_leaves_finished_jobs_alone(conn):
    done = _mk(conn, "2026-11-26T01:19:50", status="done")
    failed = _mk(conn, "2026-11-26T01:19:50", status="failed")
    assert DB.reap_stale_running_jobs(conn, before="2026-11-27T06:00:00") == []
    assert conn.execute(
        "SELECT status FROM jobs WHERE id=?", (done,)).fetchone()["status"] == "done"
    assert conn.execute(
        "SELECT status FROM jobs WHERE id=?", (failed,)).fetchone()["status"] == "failed"


def test_default_before_uses_now(conn):
    old = _mk(conn, "2026-11-26T01:19:50")
    reaped = DB.reap_stale_running_jobs(conn)
    assert reaped == [old]


def test_nothing_running_returns_empty(conn):
    assert DB.reap_stale_running_jobs(conn, before="2026-11-27T06:00:00") == []
