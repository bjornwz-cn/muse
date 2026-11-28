#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤3 步骤1：删除确认损坏的视频（计划＋apply，参照 #161/#164）。
删除清单：/data/pending_triage_verify.tsv 中 verdict=='dead' 且
/data/pending_triage_ffmpeg_check.tsv 中 ffmpeg=='fail' 的行（386 个，
cv2 与 ffmpeg 双重确认完全读不出）。
其余复核行记 plan_items skipped，不动文件。
删除前 sqlite3 backup API 备份 DB；删前逐个再验大小＋mtime；
删除后 review 行标 approved，decided_by='pending-triage-delete-damaged'。
排除（清单里本来就没有，但再验一遍）：20200614_0001.mov、凭据文件、
加密文件（__CLOUDSYNC_ENC__）。
用法：docker exec -d nas-curator python3 /data/pending_triage_delete.py --apply
"""
import argparse, json, os, sqlite3, sys, time

try:
    sys.path.insert(0, '/app')
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None

DB = '/data/nas-curator.db'
MANIFEST = '/data/pending_triage_verify.tsv'
FFCHECK = '/data/pending_triage_ffmpeg_check.tsv'
SUMMARY = '/data/pending_triage_delete_summary.json'
PROGRESS_DIR = '/data/progress'
HB_ID = 'pending-triage-delete'
DECIDED_BY = 'pending-triage-delete-damaged'
EXCLUDE_RQ = {138941, 138943, 139432}


def _hb(**kw):
    if _write_hb is None:
        return
    try:
        _write_hb(HB_ID, progress_dir=PROGRESS_DIR, **kw)
    except Exception as e:
        print('heartbeat failed: %s' % e, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if not args.apply:
        print('need --apply')
        return 2
    # ffmpeg cross-check map
    ff = {}
    with open(FFCHECK, encoding='utf-8') as f:
        hdr = f.readline()
        for line in f:
            parts = line.rstrip('\n').split('\t')
            if len(parts) >= 3:
                ff[parts[0]] = parts[2]
    rows = []
    import csv
    with open(MANIFEST, encoding='utf-8') as f:
        rd = csv.DictReader(f, delimiter='\t')
        for r in rd:
            rows.append(r)
    n_delete_cand = 0
    for r in rows:
        r['delete'] = (r['verdict'] == 'dead'
                       and ff.get(r['rq_id']) == 'fail'
                       and int(r['rq_id']) not in EXCLUDE_RQ
                       and '20200614_0001.mov' not in r['src']
                       and '__CLOUDSYNC_ENC__' not in r['src'])
        if r['delete']:
            n_delete_cand += 1
    print('manifest rows: %d delete candidates: %d (ffmpeg fail-gated)' % (
        len(rows), n_delete_cand), flush=True)
    if n_delete_cand == 0:
        print('nothing to delete')
        return 0

    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    stamp = time.strftime('%Y%m%d-%H%M%S')
    bak = '/data/nas-curator.db.bak-%s-pre-pending-triage-delete' % stamp
    with sqlite3.connect(bak) as dst:
        db.backup(dst)
    print('db backup:', bak, flush=True)
    cur = db.execute(
        "INSERT INTO plans (library, summary) VALUES ('media', ?)",
        (json.dumps({'kind': 'pending-triage-delete-damaged', 'manifest': MANIFEST,
                     'ffcheck': FFCHECK, 'backup': bak}, ensure_ascii=False),))
    plan_id = cur.lastrowid
    db.execute("UPDATE plans SET status='applying' WHERE id=?", (plan_id,))
    db.commit()
    print('plan #%d created' % plan_id, flush=True)

    _hb(phase='apply', status='running', total=len(rows), done=0)
    counts = {}
    freed = 0
    t0 = time.time()
    for n, r in enumerate(rows, 1):
        src = r['src']
        rq_id = int(r['rq_id']) if r['rq_id'] else None
        action = 'delete' if r['delete'] else 'skip'
        cur = db.execute(
            """INSERT INTO plan_items
               (plan_id, src_path, category, confidence, reason, evidence,
                action, kind, size, ext, sha256, review_kind, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (plan_id, src, 'damaged', 0.0,
             'pending-triage-delete:%s ffmpeg=%s' % (r['verdict'], ff.get(r['rq_id'], '')),
             r['detail'], 'review', 'media',
             int(r['size_disk']) if r['size_disk'] else None,
             os.path.splitext(src)[1].lower(),
             r['sha256_disk'] or None, 'low_conf',
             'done' if action == 'delete' else 'skipped'))
        item_id = cur.lastrowid
        if action != 'delete':
            counts['skip-' + r['verdict']] = counts.get('skip-' + r['verdict'], 0) + 1
            db.commit()
            continue
        try:
            st = os.stat(src)
            if r['size_disk'] and int(r['size_disk']) != st.st_size:
                db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (item_id,))
                counts['skip-size-changed'] = counts.get('skip-size-changed', 0) + 1
                db.commit()
                continue
            if r['mtime_disk'] and abs(float(r['mtime_disk']) - st.st_mtime) > 1:
                db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (item_id,))
                counts['skip-mtime-changed'] = counts.get('skip-mtime-changed', 0) + 1
                db.commit()
                continue
        except FileNotFoundError:
            db.execute("UPDATE plan_items SET status='skipped' WHERE id=?", (item_id,))
            counts['already-gone'] = counts.get('already-gone', 0) + 1
            db.commit()
            continue
        try:
            os.remove(src)
            freed += st.st_size
        except Exception as e:
            db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (item_id,))
            counts['failed-delete'] = counts.get('failed-delete', 0) + 1
            print('FAILED delete %s : %s: %s' % (src, type(e).__name__, str(e)[:120]), flush=True)
            db.commit()
            continue
        if rq_id:
            cur2 = db.execute(
                "UPDATE review_queue SET status='approved', decided_by=?, "
                "decided_at=strftime('%Y-%m-%dT%H:%M:%S','now') "
                "WHERE id=? AND status='pending'", (DECIDED_BY, rq_id))
            if cur2.rowcount:
                counts['review-rows-approved'] = counts.get('review-rows-approved', 0) + cur2.rowcount
        counts['file-deleted'] = counts.get('file-deleted', 0) + 1
        db.commit()
        if n % 100 == 0:
            print('%d/%d applied (%.1fs) counts=%s' % (n, len(rows), time.time() - t0, counts), flush=True)
            _hb(phase='apply', status='running', total=len(rows), done=n)
    db.execute(
        "UPDATE plans SET status='done', summary=? WHERE id=?",
        (json.dumps({'kind': 'pending-triage-delete-damaged', 'manifest': MANIFEST,
                     'ffcheck': FFCHECK, 'backup': bak, 'counts': counts,
                     'freed_bytes': freed}, ensure_ascii=False), plan_id))
    db.commit()
    print('integrity:', db.execute('PRAGMA integrity_check').fetchone()[0], flush=True)
    print('pending now:', db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE status='pending'").fetchone()[0], flush=True)
    print('plan #%d items:' % plan_id, db.execute(
        "SELECT status, COUNT(*) FROM plan_items WHERE plan_id=? GROUP BY status",
        (plan_id,)).fetchall(), flush=True)
    print('APPLY DONE n=%d elapsed=%.1fs counts=%s freed=%d bytes (%.2f GB)'
          % (len(rows), time.time() - t0, counts, freed, freed / (1 << 30)), flush=True)
    _hb(phase='apply', status='done', total=len(rows), done=len(rows))
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump({'phase': 'apply', 'plan_id': plan_id, 'backup': bak,
                   'n': len(rows), 'elapsed': round(time.time() - t0, 1),
                   'counts': counts, 'freed_bytes': freed},
                  f, ensure_ascii=False, indent=2)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
