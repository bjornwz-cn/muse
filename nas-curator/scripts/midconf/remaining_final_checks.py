#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剩余视频任务 收尾核查（只读）：计划 #176 与新增 pending 行的来源、auto_apply 现状。"""
import sqlite3, json

db = sqlite3.connect("/data/nas-curator.db")
print("plan176:", db.execute(
    "SELECT id, library, status, summary FROM plans WHERE id=176").fetchone())
print("max review id:", db.execute("SELECT MAX(id) FROM review_queue").fetchone()[0])
print("pending id>150200:", db.execute(
    "SELECT COUNT(*) FROM review_queue WHERE status='pending' AND id>150200").fetchone()[0])
print("pending total:", db.execute(
    "SELECT COUNT(*) FROM review_queue WHERE status='pending'").fetchone()[0])
print("approved total:", db.execute(
    "SELECT COUNT(*) FROM review_queue WHERE status='approved'").fetchone()[0])
tables = [r[0] for r in db.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("tables:", tables)
for t in tables:
    cols = [r[1] for r in db.execute("PRAGMA table_info(%s)" % t).fetchall()]
    if any("auto" in c.lower() for c in cols):
        print("table with auto col:", t, cols)
        try:
            print(db.execute("SELECT * FROM %s" % t).fetchall()[:5])
        except Exception as e:
            print("read fail:", e)
# plans 174/175/176 列表
for r in db.execute("SELECT id, library, status FROM plans WHERE id>=174 ORDER BY id"):
    print("plan:", r)
