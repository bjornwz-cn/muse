#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796 任务 登记其他文件结论（容器内，写 DB payload 仅限结论清单行）。
读 /data/r4796_other_conclusions.json，对仍 pending 的行写入：
other_category / other_confidence / other_party / other_reason /
other_source，decided_by='r4796-other'；不动 status 与其他 payload 字段。
登记前 sqlite3 backup API 备份 DB。
输出 /data/r4796_register_other_summary.json。
用法：docker exec nas-curator python3 /data/r4796_register_other.py
"""
import json, os, sqlite3, time

try:
    import sys
    sys.path.insert(0, '/app')
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None

DB = '/data/nas-curator.db'
CONC = '/data/r4796_other_conclusions.json'
OUT = '/data/r4796_register_other_summary.json'


def main():
    conc = json.load(open(CONC, encoding='utf-8'))
    print('conclusions: %d' % len(conc), flush=True)
    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    stamp = time.strftime('%Y%m%d-%H%M%S')
    bak = '/data/nas-curator.db.bak-%s-pre-r4796-register-other' % stamp
    with sqlite3.connect(bak) as dst:
        db.backup(dst)
    print('db backup:', bak, os.path.getsize(bak), flush=True)
    updated, skipped, already = 0, [], 0
    for key in sorted(conc, key=int):
        c = conc[key]
        rid = int(key)
        row = db.execute(
            "SELECT payload FROM review_queue WHERE id=? AND status='pending'",
            (rid,)).fetchone()
        if not row:
            skipped.append(rid)
            continue
        d = json.loads(row[0])
        if d.get('other_source') == c.get('source') and d.get('other_reason') == c.get('reason'):
            already += 1
            continue
        d['other_category'] = c.get('cat', '')
        d['other_confidence'] = c.get('conf', 0)
        d['other_party'] = c.get('party', '')
        d['other_reason'] = c.get('reason', '')
        d['other_source'] = c.get('source', '')
        cur = db.execute(
            "UPDATE review_queue SET payload=?, decided_by='r4796-other' "
            "WHERE id=? AND status='pending'",
            (json.dumps(d, ensure_ascii=False), rid))
        if cur.rowcount == 1:
            updated += 1
        else:
            skipped.append(rid)
        db.commit()
    print('integrity:', db.execute('PRAGMA integrity_check').fetchone()[0], flush=True)
    summ = {'n': len(conc), 'updated': updated, 'already': already,
            'skipped_not_pending': skipped, 'backup': bak}
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print('SUMMARY ' + json.dumps(summ, ensure_ascii=False))
    if _write_hb is not None:
        try:
            _write_hb('r4796-register-other', progress_dir='/data/progress',
                      label='r4796 其他文件结论登记', kind='r4796-other',
                      phase='register', status='done', total=len(conc), done=updated)
        except Exception as e:
            print('heartbeat failed: %s' % e, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
