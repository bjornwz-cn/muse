#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剩余视频任务 步骤 1C：删除确认损坏的视频（计划＋apply，参照 #161/#171）。
删除清单：/data/remaining_verify_final.tsv 中 final_verdict=='damaged' 的行
（ffmpeg 全量解码 < 总帧数或报错，且 cv2 全量读帧亦 < 总帧数或完全读不出，
双重确认）。其余行记 plan_items skipped，不动文件。
特别处理 rq 133420：源（收件箱）按损坏删除；目标
/vol1/1000/媒体库/视频/儿童成长/20190507_0001.mov 与源字节相同（md5
a932e696cbf6b28b5f2dc9ff570394a5）、同为部分损坏（23/89 帧），一并删除；
删前再验目标 md5/大小/帧数，记一条额外 plan_item（无 review 行）。
删除前 sqlite3 backup API 备份 DB；删前逐个再验大小＋mtime＋sha256；
删除后 review 行标 approved，decided_by='remaining-delete-damaged'。
绝不动：rq 149995（missing）、20200614_0001.mov、加密文件、凭据文件。
用法：docker exec -d nas-curator python3 /data/remaining_delete_damaged.py --apply
"""
import argparse, csv, hashlib, json, os, sqlite3, sys, time

try:
    sys.path.insert(0, '/app')
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None

DB = '/data/nas-curator.db'
MANIFEST = '/data/remaining_verify_final.tsv'
FF_TSV = '/data/remaining_verify_ffmpeg.tsv'
SUMMARY = '/data/remaining_delete_summary.json'
PROGRESS_DIR = '/data/progress'
HB_ID = 'remaining-delete-damaged'
DECIDED_BY = 'remaining-delete-damaged'
EXCLUDE_RQ = {149995}
RQ133420 = 133420
TARGET_133420 = '/vol1/1000/媒体库/视频/儿童成长/20190507_0001.mov'
TARGET_133420_MD5 = 'a932e696cbf6b28b5f2dc9ff570394a5'
TARGET_133420_SIZE = 2572924


def _hb(**kw):
    if _write_hb is None:
        return
    try:
        _write_hb(HB_ID, progress_dir=PROGRESS_DIR, **kw)
    except Exception as e:
        print('heartbeat failed: %s' % e, flush=True)


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if not args.apply:
        print('need --apply')
        return 2
    ff = {}
    with open(FF_TSV, encoding='utf-8') as f:
        rd = csv.DictReader(f, delimiter='\t')
        for r in rd:
            ff[r['rq_id']] = r
    rows = []
    with open(MANIFEST, encoding='utf-8') as f:
        rd = csv.DictReader(f, delimiter='\t')
        for r in rd:
            rows.append(r)
    n_delete_cand = 0
    for r in rows:
        r['delete'] = (r['final_verdict'] == 'damaged'
                       and int(r['rq_id']) not in EXCLUDE_RQ
                       and '20200614_0001.mov' not in r['src']
                       and '__CLOUDSYNC_ENC__' not in r['src'])
        if r['delete']:
            n_delete_cand += 1
    print('manifest rows: %d delete candidates: %d' % (len(rows), n_delete_cand), flush=True)
    print('rq 133420 in delete set: %s' % any(
        r['delete'] and int(r['rq_id']) == RQ133420 for r in rows), flush=True)

    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    stamp = time.strftime('%Y%m%d-%H%M%S')
    bak = '/data/nas-curator.db.bak-%s-pre-remaining-delete-damaged' % stamp
    with sqlite3.connect(bak) as dst:
        db.backup(dst)
    print('db backup:', bak, flush=True)
    cur = db.execute(
        "INSERT INTO plans (library, summary) VALUES ('media', ?)",
        (json.dumps({'kind': 'remaining-delete-damaged', 'manifest': MANIFEST,
                     'ffcheck': FF_TSV, 'backup': bak}, ensure_ascii=False),))
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
        ffrow = ff.get(r['rq_id'], {})
        cur = db.execute(
            """INSERT INTO plan_items
               (plan_id, src_path, category, confidence, reason, evidence,
                action, kind, size, ext, sha256, review_kind, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (plan_id, src, 'damaged', 0.0,
             'remaining-verify: ff=%s decoded=%s/%s cv2=%s' % (
                 r['ff_verdict'], r.get('decoded_frames'), r.get('total_frames'),
                 r.get('cv2_verdict')),
             r.get('detail', ''), 'review', 'media',
             int(ffrow['size_disk']) if ffrow.get('size_disk') else None,
             os.path.splitext(src)[1].lower(),
             ffrow.get('sha256') or None, 'low_conf',
             'done' if action == 'delete' else 'skipped'))
        item_id = cur.lastrowid
        if action != 'delete':
            counts['skip-' + r['final_verdict']] = counts.get('skip-' + r['final_verdict'], 0) + 1
            db.commit()
            continue
        try:
            st = os.stat(src)
            if ffrow.get('size_disk') and int(ffrow['size_disk']) != st.st_size:
                db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (item_id,))
                counts['skip-size-changed'] = counts.get('skip-size-changed', 0) + 1
                db.commit()
                continue
            if ffrow.get('mtime_disk') and abs(float(ffrow['mtime_disk']) - st.st_mtime) > 1:
                db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (item_id,))
                counts['skip-mtime-changed'] = counts.get('skip-mtime-changed', 0) + 1
                db.commit()
                continue
        except FileNotFoundError:
            db.execute("UPDATE plan_items SET status='skipped' WHERE id=?", (item_id,))
            counts['already-gone'] = counts.get('already-gone', 0) + 1
            db.commit()
            continue
        # sha256 再验（删前最后一道）
        try:
            h = hashlib.sha256()
            with open(src, 'rb') as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b''):
                    h.update(chunk)
            if ffrow.get('sha256') and h.hexdigest() != ffrow['sha256']:
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
                "WHERE id=? AND status='pending'", (DECIDED_BY, rq_id))
            if cur2.rowcount:
                counts['review-rows-approved'] = counts.get('review-rows-approved', 0) + cur2.rowcount
        counts['file-deleted'] = counts.get('file-deleted', 0) + 1
        db.commit()
        if n % 100 == 0:
            print('%d/%d applied (%.1fs) counts=%s' % (n, len(rows), time.time() - t0, counts), flush=True)
            _hb(phase='apply', status='running', total=len(rows), done=n)

    # --- rq 133420 目标文件（媒体库保留份）单独处理 ---
    target_info = {'target': TARGET_133420, 'deleted': False, 'reason': ''}
    src_deleted = any(r['delete'] and int(r['rq_id']) == RQ133420 for r in rows) and \
        db.execute("SELECT COUNT(*) FROM review_queue WHERE id=? AND status='approved'",
                   (RQ133420,)).fetchone()[0] == 1
    if src_deleted:
        try:
            tst = os.stat(TARGET_133420)
            tmd5 = md5(TARGET_133420)
            if tst.st_size != TARGET_133420_SIZE or tmd5 != TARGET_133420_MD5:
                target_info['reason'] = ('target changed since check: size=%d md5=%s '
                                         '(expect %d/%s); NOT deleted' % (
                                             tst.st_size, tmd5, TARGET_133420_SIZE,
                                             TARGET_133420_MD5))
                print('133420 TARGET CHANGED, skip: %s' % target_info['reason'], flush=True)
            else:
                cur = db.execute(
                    """INSERT INTO plan_items
                       (plan_id, src_path, category, confidence, reason, evidence,
                        action, kind, size, ext, sha256, review_kind, status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (plan_id, TARGET_133420, 'damaged', 0.0,
                     'rq133420 library twin: same md5 as damaged src, partial 23/89 frames',
                     'md5=%s size=%d' % (tmd5, tst.st_size),
                     'review', 'media', tst.st_size, '.mov', None, 'low_conf', 'done'))
                os.remove(TARGET_133420)
                freed += tst.st_size
                counts['file-deleted'] = counts.get('file-deleted', 0) + 1
                counts['rq133420-target-deleted'] = 1
                target_info.update({'deleted': True, 'size': tst.st_size, 'md5': tmd5})
                print('133420 TARGET DELETED %s (%d bytes, md5 verified)' % (
                    TARGET_133420, tst.st_size), flush=True)
            db.commit()
        except FileNotFoundError:
            target_info['reason'] = 'target already gone'
            print('133420 TARGET already gone', flush=True)
        except Exception as e:
            target_info['reason'] = '%s: %s' % (type(e).__name__, str(e)[:150])
            print('133420 TARGET FAILED: %s' % target_info['reason'], flush=True)
    else:
        target_info['reason'] = 'src not deleted (not in delete set or already approved before); target kept'
        print('133420 TARGET kept: %s' % target_info['reason'], flush=True)

    db.execute(
        "UPDATE plans SET status='done', summary=? WHERE id=?",
        (json.dumps({'kind': 'remaining-delete-damaged', 'manifest': MANIFEST,
                     'ffcheck': FF_TSV, 'backup': bak, 'counts': counts,
                     'freed_bytes': freed, 'rq133420_target': target_info},
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
    _hb(phase='apply', status='done', total=len(rows), done=len(rows))
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump({'phase': 'apply', 'plan_id': plan_id, 'backup': bak,
                   'n': len(rows), 'elapsed': round(time.time() - t0, 1),
                   'counts': counts, 'freed_bytes': freed,
                   'rq133420_target': target_info},
                  f, ensure_ascii=False, indent=2)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
