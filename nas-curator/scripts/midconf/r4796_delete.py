#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796 任务 删除执行（容器内，计划＋apply，参照 remaining_delete_damaged.py）。
清单：--manifest TSV（rq_id, src, size_disk, mtime_disk, sha256, reason, evidence），
  仅删除清单内行；删前逐个再验大小＋mtime＋sha256；
  --mode encrypted 时删前再验文件头 __CLOUDSYNC_ENC__（17 字节）。
删除后 review 行标 approved，decided_by 由 --decided-by 指定。
删除前 sqlite3 backup API 备份 DB。
绝不动：rq 149995、150025、150143（src 是目录）、20200614_0001.mov、
  凭据行（138941/138943/139426/139428/139430/139432）。
用法：docker exec -d nas-curator python3 /data/r4796_delete.py --manifest <tsv> \
  --mode encrypted|undeterminable --decided-by <decided_by> --plan-kind <kind> \
  --summary <json> --heartbeat-id <id> --apply
"""
import argparse, csv, hashlib, json, os, sqlite3, sys, time

try:
    sys.path.insert(0, '/app')
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None

DB = '/data/nas-curator.db'
PROGRESS_DIR = '/data/progress'
ENC = b'__CLOUDSYNC_ENC__'
EXCLUDE_RQ = {149995, 150025, 150143, 138941, 138943, 139426, 139428, 139430, 139432}


def _hb(task_id, **kw):
    if _write_hb is None:
        return
    try:
        _write_hb(task_id, progress_dir=PROGRESS_DIR, **kw)
    except Exception as e:
        print('heartbeat failed: %s' % e, flush=True)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--manifest', required=True)
    ap.add_argument('--mode', required=True, choices=['encrypted', 'undeterminable', 'damaged'])
    ap.add_argument('--decided-by', required=True)
    ap.add_argument('--plan-kind', required=True)
    ap.add_argument('--summary', required=True)
    ap.add_argument('--heartbeat-id', required=True)
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if not args.apply:
        print('need --apply')
        return 2
    rows = []
    with open(args.manifest, encoding='utf-8') as f:
        rd = csv.DictReader(f, delimiter='\t')
        for r in rd:
            rows.append(r)
    n_cand = sum(1 for r in rows if r.get('delete') == '1')
    print('manifest rows: %d delete candidates: %d mode=%s' % (len(rows), n_cand, args.mode), flush=True)
    for r in rows:
        if int(r['rq_id']) in EXCLUDE_RQ and r.get('delete') == '1':
            print('FATAL: excluded rq in delete set: %s' % r['rq_id'], flush=True)
            return 3
        if '20200614_0001.mov' in r['src'] and r.get('delete') == '1':
            print('FATAL: 20200614 in delete set', flush=True)
            return 3

    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    stamp = time.strftime('%Y%m%d-%H%M%S')
    bak = '/data/nas-curator.db.bak-%s-pre-r4796-delete-%s' % (stamp, args.mode)
    with sqlite3.connect(bak) as dst:
        db.backup(dst)
    print('db backup:', bak, flush=True)
    cur = db.execute(
        "INSERT INTO plans (library, summary) VALUES ('media', ?)",
        (json.dumps({'kind': args.plan_kind, 'manifest': args.manifest,
                     'backup': bak, 'mode': args.mode}, ensure_ascii=False),))
    plan_id = cur.lastrowid
    db.execute("UPDATE plans SET status='applying' WHERE id=?", (plan_id,))
    db.commit()
    print('plan #%d created' % plan_id, flush=True)

    _hb(args.heartbeat_id, phase='apply', status='running', total=len(rows), done=0)
    counts = {}
    freed = 0
    t0 = time.time()
    for n, r in enumerate(rows, 1):
        src = r['src']
        rq_id = int(r['rq_id']) if r['rq_id'] else None
        action = 'delete' if r.get('delete') == '1' else 'skip'
        cur = db.execute(
            """INSERT INTO plan_items
               (plan_id, src_path, category, confidence, reason, evidence,
                action, kind, size, ext, sha256, review_kind, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (plan_id, src, args.mode, 0.0,
             r.get('reason', ''), r.get('evidence', ''),
             'review', 'media',
             int(r['size_disk']) if r.get('size_disk') else None,
             os.path.splitext(src)[1].lower(),
             r.get('sha256') or None, 'low_conf',
             'done' if action == 'delete' else 'skipped'))
        item_id = cur.lastrowid
        if action != 'delete':
            counts['skip'] = counts.get('skip', 0) + 1
            db.commit()
            continue
        try:
            st = os.stat(src)
            if r.get('size_disk') and int(r['size_disk']) != st.st_size:
                db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (item_id,))
                counts['skip-size-changed'] = counts.get('skip-size-changed', 0) + 1
                db.commit()
                continue
            if r.get('mtime_disk') and abs(float(r['mtime_disk']) - st.st_mtime) > 1:
                db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (item_id,))
                counts['skip-mtime-changed'] = counts.get('skip-mtime-changed', 0) + 1
                db.commit()
                continue
        except FileNotFoundError:
            db.execute("UPDATE plan_items SET status='skipped' WHERE id=?", (item_id,))
            counts['already-gone'] = counts.get('already-gone', 0) + 1
            db.commit()
            continue
        if args.mode == 'encrypted':
            try:
                with open(src, 'rb') as fh:
                    head = fh.read(17)
                if head != ENC:
                    db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (item_id,))
                    counts['skip-magic-changed'] = counts.get('skip-magic-changed', 0) + 1
                    print('SKIP magic changed rq=%d %s' % (rq_id, src), flush=True)
                    db.commit()
                    continue
            except FileNotFoundError:
                db.execute("UPDATE plan_items SET status='skipped' WHERE id=?", (item_id,))
                counts['already-gone'] = counts.get('already-gone', 0) + 1
                db.commit()
                continue
        try:
            h = sha256_of(src)
            if r.get('sha256') and h != r['sha256']:
                db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (item_id,))
                counts['skip-hash-changed'] = counts.get('skip-hash-changed', 0) + 1
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
                "WHERE id=? AND status='pending'", (args.decided_by, rq_id))
            if cur2.rowcount:
                counts['review-rows-approved'] = counts.get('review-rows-approved', 0) + cur2.rowcount
        counts['file-deleted'] = counts.get('file-deleted', 0) + 1
        db.commit()
        if n % 50 == 0:
            print('%d/%d applied (%.1fs) counts=%s' % (n, len(rows), time.time() - t0, counts), flush=True)
            _hb(args.heartbeat_id, phase='apply', status='running', total=len(rows), done=n)

    db.execute(
        "UPDATE plans SET status='done', summary=? WHERE id=?",
        (json.dumps({'kind': args.plan_kind, 'manifest': args.manifest,
                     'backup': bak, 'counts': counts,
                     'freed_bytes': freed, 'mode': args.mode},
                    ensure_ascii=False), plan_id))
    db.commit()
    print('integrity:', db.execute('PRAGMA integrity_check').fetchone()[0], flush=True)
    print('pending now:', db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE status='pending'").fetchone()[0], flush=True)
    print('plan #%d items:' % plan_id, db.execute(
        "SELECT status, COUNT(*) FROM plan_items WHERE plan_id=? GROUP BY status",
        (plan_id,)).fetchall(), flush=True)
    print('APPLY DONE n=%d elapsed=%.1fs counts=%s freed=%d bytes (%.2f GB)'
          % (len(rows), time.time() - t0, counts, freed, freed / (1 << 30)), flush=True)
    _hb(args.heartbeat_id, phase='apply', status='done', total=len(rows), done=len(rows))
    with open(args.summary, 'w', encoding='utf-8') as f:
        json.dump({'phase': 'apply', 'plan_id': plan_id, 'backup': bak,
                   'n': len(rows), 'elapsed': round(time.time() - t0, 1),
                   'counts': counts, 'freed_bytes': freed, 'mode': args.mode},
                  f, ensure_ascii=False, indent=2)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
