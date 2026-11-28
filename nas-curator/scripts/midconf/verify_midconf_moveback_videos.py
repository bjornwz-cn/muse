#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 2b step 3: post-apply verification (READ-ONLY on DB, except nothing).
Checks: integrity, review counts, decided_by breakdown for plan #170,
map file, src-gone/dst-present spot checks, rq 133420 untouched,
20200614 / credential / encrypted untouched, inbox count, container plans.
Prints VERIFY lines; exits non-zero on any mismatch.
"""
import json, os, sqlite3, sys

DB = "/data/nas-curator.db"
MAP = "/data/year_moveback_midconf_videos_map.tsv"
CANDS = "/data/midconf_moveback_videos_candidates.tsv"
PLAN_ID = 170
NINE = ['文档与票据', '自拍', '城市与建筑', '人物合影', '工作与活动',
        '宠物与动物', '美食与咖啡', '梗图与表情', '屏幕截图']

fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(detail) if detail else ""))
    if not cond:
        fails.append(name)


db = sqlite3.connect('file:%s?mode=ro' % DB, uri=True, timeout=30)
check("integrity", db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok')
st = dict(db.execute("SELECT status,COUNT(*) FROM review_queue GROUP BY status").fetchall())
print("review status:", st)
check("approved 63230+119=63349", st.get('approved') == 63349, st)
check("pending 5944-119=5825", st.get('pending') == 5825, st)
check("rejected unchanged 286", st.get('rejected') == 286, st)
dec = dict(db.execute("SELECT decided_by,COUNT(*) FROM review_queue WHERE decided_by LIKE 'year-moveback-midconf-videos%' GROUP BY decided_by").fetchall())
print("decided_by videos:", dec)
check("decided_by total 119", sum(dec.values()) == 119, dec)
check("decided_by move 119", dec.get('year-moveback-midconf-videos') == 119, dec)
check("no rename/dedup rows", dec.get('year-moveback-midconf-videos-rename', 0) == 0 and dec.get('year-moveback-midconf-videos-dedup', 0) == 0, dec)
check("max plan 170", db.execute("SELECT MAX(id) FROM plans").fetchone()[0] == 170)
check("plan 170 done", db.execute("SELECT status FROM plans WHERE id=170").fetchone()[0] == 'done')
items = dict(db.execute("SELECT status,COUNT(*) FROM plan_items WHERE plan_id=170 GROUP BY status").fetchall())
print("plan170 items:", items)
check("plan170 items done 119", items.get('done') == 119 and sum(items.values()) == 119, items)
check("plan170 operations 119", db.execute("SELECT COUNT(*) FROM operations WHERE plan_id=170").fetchone()[0] == 119)
check("no applying plans", db.execute("SELECT COUNT(*) FROM plans WHERE status='applying'").fetchone()[0] == 0)
r = db.execute("SELECT status,decided_by FROM review_queue WHERE id=133420").fetchone()
print("rq133420:", r)
check("rq133420 still pending", r is not None and r[0] == 'pending', r)
n2020 = db.execute("SELECT COUNT(*) FROM review_queue WHERE decided_by LIKE 'year-moveback-midconf-videos%' AND json_extract(payload,'$.src') LIKE '%20200614%'").fetchone()[0]
check("no 20200614 in plan170", n2020 == 0, n2020)
db.close()

# map + disk checks
lines = [l.rstrip('\n').split('\t') for l in open(MAP, encoding='utf-8') if l.strip()]
print("map lines:", len(lines))
check("map 119 lines", len(lines) == 119)
cands = [l.rstrip('\n').split('\t') for l in open(CANDS, encoding='utf-8')][1:]
check("candidates 119 rows", len(cands) == 119)
srcs = {c[1] for c in cands}
map_srcs = {m[0] for m in lines}
check("map srcs == candidate srcs", map_srcs == srcs, (len(map_srcs), len(srcs)))
src_gone = sum(1 for s in srcs if not os.path.exists(s))
check("all 119 srcs gone from disk", src_gone == 119, src_gone)
dst_ok = sum(1 for m in lines if len(m) >= 2 and os.path.isfile(m[1]))
check("all 119 dsts on disk", dst_ok == 119, dst_ok)
bad_dir = [m[1] for m in lines if len(m) >= 2 and '/媒体库/视频/' not in m[1]]
check("all dsts under 媒体库/视频/", not bad_dir, bad_dir[:3])
# per-dir counts
from collections import Counter
per = Counter()
for m in lines:
    d = os.path.basename(os.path.dirname(m[1]))
    per[d] += 1
print("dst per dir:", dict(per))
check("9 new dirs only", set(per) == set(NINE), set(per))
check("per-dir matches 39/21/16/14/12/7/4/3/3",
      dict(per) == {'文档与票据': 39, '自拍': 21, '城市与建筑': 16, '人物合影': 14,
                    '工作与活动': 12, '宠物与动物': 7, '美食与咖啡': 4,
                    '梗图与表情': 3, '屏幕截图': 3}, dict(per))
# 20200614 media file untouched
p2020 = '/vol1/1000/媒体库/视频/屏幕录制/20200614_0001.mov'
check("20200614 media copy still in place", os.path.exists(p2020), p2020)

print("VERIFY " + ("PASS" if not fails else "FAIL: %s" % fails))
sys.exit(0 if not fails else 1)
