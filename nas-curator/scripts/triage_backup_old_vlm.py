#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤3 步骤3a：备份旧 VLM 结论并清空（照片行）。
开始前先用 sqlite3 backup API 备份 DB。
对 /data/triage_photo_ids.txt 的行：把已有的 5 个 vlm 键备份到
/data/triage_vlm_old.json，再从 payload 删掉这 5 个键（只动 payload，
不动 status/decided_by 与其他字段）。核对清空后无一行残留 vlm_category。
"""
import json, os, sqlite3, time

DB = '/data/nas-curator.db'
IDS_FILE = '/data/triage_photo_ids.txt'
OUT = '/data/triage_vlm_old.json'
KEYS = ['vlm_category', 'vlm_confidence', 'vlm_reason', 'vlm_model', 'vlm_source']

conn = sqlite3.connect(DB, timeout=30)
conn.execute('PRAGMA busy_timeout=30000')
stamp = time.strftime('%Y%m%d-%H%M%S')
bak = '/data/nas-curator.db.bak-%s-pre-triage-reclassify' % stamp
with sqlite3.connect(bak) as dst:
    conn.backup(dst)
print('db backup:', bak, flush=True)

ids = [int(x) for x in open(IDS_FILE, encoding='utf-8').read().replace(',', ' ').split() if x.strip()]
print('ids: %d unique %d' % (len(ids), len(set(ids))), flush=True)
assert len(ids) == len(set(ids))
backup = {}
missing = []
for rid in ids:
    row = conn.execute("SELECT payload FROM review_queue WHERE id=? AND status='pending'", (rid,)).fetchone()
    assert row, 'row gone rid=%d' % rid
    d = json.loads(row[0])
    if 'vlm_category' not in d:
        missing.append(rid)
        continue
    backup[str(rid)] = {k: d.get(k) for k in KEYS if k in d}
print('backup rows with old vlm: %d ; without vlm: %d %s' % (len(backup), len(missing), missing[:10]), flush=True)
with open(OUT, 'w', encoding='utf-8') as f:
    json.dump(backup, f, ensure_ascii=False, indent=1)
print('wrote %s (%d bytes, %d rows)' % (OUT, os.path.getsize(OUT), len(backup)), flush=True)

changed = 0
for rid in ids:
    row = conn.execute("SELECT payload FROM review_queue WHERE id=? AND status='pending'", (rid,)).fetchone()
    d = json.loads(row[0])
    if any(k in d for k in KEYS):
        for k in KEYS:
            d.pop(k, None)
        conn.execute("UPDATE review_queue SET payload=? WHERE id=? AND status='pending'",
                     (json.dumps(d, ensure_ascii=False), rid))
        changed += 1
conn.commit()
remaining = 0
for rid in ids:
    d = json.loads(conn.execute("SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()[0])
    if 'vlm_category' in d:
        remaining += 1
print('rows updated (keys removed): %d ; rows still with vlm_category: %d' % (changed, remaining), flush=True)
assert remaining == 0
conn.close()
print('BACKUP-OLD-VLM DONE backup=%d cleared=%d db_backup=%s' % (len(backup), changed, bak), flush=True)
