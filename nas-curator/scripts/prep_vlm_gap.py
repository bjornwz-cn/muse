#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VLM 缺口补跑前置准备（容器内执行）：
1. 用 sqlite3 backup API 备份 DB（备份名按 CST 时间）。
2. 读 /data/year_vlm_gap_rqids_all.txt（3,081 个 FileNotFoundError 的 rq id），
   逐个从 DB 取 src，核对文件是否在盘。
3. 在盘的 id 写成逗号分隔单行 /data/year_vlm_gap_rqids.txt（供 VLM 脚本 --rq-ids 用）；
   不在盘的 id 写 /data/year_vlm_gap_missing.txt（一行一个，附 src）。
只读 DB（除备份外无写操作），不移动、不删除任何文件。
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

DB = "/data/nas-curator.db"
IDS_ALL = "/data/year_vlm_gap_rqids_all.txt"
IDS_OK = "/data/year_vlm_gap_rqids.txt"
IDS_MISSING = "/data/year_vlm_gap_missing.txt"

cst = timezone(timedelta(hours=8))
ts = datetime.now(cst).strftime("%Y%m%d-%H%M")

# 1. 备份 DB
bak = "/data/nas-curator.db.bak-%s-pre-vlm-gap" % ts
src_conn = sqlite3.connect(DB)
dst_conn = sqlite3.connect(bak)
src_conn.backup(dst_conn)
dst_conn.close()
src_conn.close()
print("backup: %s (%d bytes)" % (bak, os.path.getsize(bak)))

# 2. 逐个核对在盘
ids = [int(x) for x in open(IDS_ALL).read().split() if x.strip()]
print("gap ids total: %d" % len(ids))
conn = sqlite3.connect(DB)
present, missing = [], []
for rid in ids:
    row = conn.execute(
        "SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()
    if not row:
        missing.append((rid, "<row_gone>"))
        continue
    src = json.loads(row[0]).get("src", "")
    if src and os.path.exists(src):
        present.append(rid)
    else:
        missing.append((rid, src))
conn.close()

with open(IDS_OK, "w", encoding="utf-8") as f:
    f.write(",".join(str(x) for x in present))
with open(IDS_MISSING, "w", encoding="utf-8") as f:
    for rid, src in missing:
        f.write("%d\t%s\n" % (rid, src))

print("present (on disk): %d" % len(present))
print("missing (skip): %d" % len(missing))
for rid, src in missing[:20]:
    print("  missing rq=%d src=%s" % (rid, src))
if len(missing) > 20:
    print("  ... and %d more (see %s)" % (len(missing) - 20, IDS_MISSING))
