import sqlite3, json
c = sqlite3.connect("/data/nas-curator.db")
rows = c.execute(
    "SELECT json_extract(payload,'$.src') FROM review_queue "
    "WHERE json_extract(payload,'$.src') LIKE '%待整理%'").fetchall()
from collections import Counter
pref = Counter()
for (s,) in rows:
    if s is None:
        pref["NULL"] += 1
    elif s.startswith("/vol1/1000/收件箱/待整理-视频"):
        pref["inbox-video"] += 1
    elif s.startswith("/vol1/1000/收件箱/待整理"):
        pref["inbox-doc"] += 1
    elif s.startswith("/vol1/1000/工作文件/"):
        pref["work-old"] += 1
    else:
        pref["other:" + s[:60]] += 1
print("review src prefixes:", dict(pref))
print("distinct kinds:", c.execute(
    "SELECT kind, COUNT(*) FROM review_queue "
    "WHERE json_extract(payload,'$.src') LIKE '%待整理%' GROUP BY kind").fetchall())
# do all current disk files have a row? check a few known files
import os
for p in ["/vol1/1000/收件箱/待整理/按日期/2014/2014-03/20140309_0022.nef",
          "/vol1/1000/收件箱/待整理-视频/2013/A0013237-王震-刘音--2.exe"]:
    n = c.execute(
        "SELECT COUNT(*) FROM review_queue WHERE json_extract(payload,'$.src')=?",
        (p,)).fetchone()[0]
    print(p, "rows:", n)
# plan 155 status/items
print("plan155:", c.execute("SELECT id, library, status, summary FROM plans WHERE id=155").fetchall())
print("plan155 items:", c.execute("SELECT status, COUNT(*) FROM plan_items WHERE plan_id=155 GROUP BY status").fetchall())
print("integrity:", c.execute("PRAGMA integrity_check").fetchone())
