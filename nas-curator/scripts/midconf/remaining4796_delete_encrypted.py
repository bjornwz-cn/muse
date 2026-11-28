#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""remaining-4796 任务 2：删除确认加密的 25 张照片（rq 138768-138792）。
逐个核验：文件头前 17 字节 == b'__CLOUDSYNC_ENC__'（魔数实为 17 字节，
任务书“16 字节”为笔误；已实测 head17 与另一 worker 的 head17 清单一致），
且 PIL 严格 load 读不出（两次 PIL 均失败才算读不出；HEIC/CR2 等不以此为据——
本清单 25 个均为上次 verify 记 dead 的 jpg/png，PIL 两次失败已确认，本次再验一次）。
删前 sqlite3 backup API 备份 DB；删前逐个再验大小＋mtime＋sha256（与
/data/remaining_photos_verify.tsv 记录比对）；删除后 review 行标 approved，
decided_by='remaining-delete-encrypted'。建 plan（与 remaining-delete-damaged
同口径），plan_items 逐行记 done/skipped/failed。
绝不动：20200614、4 个凭据文件、rq 149995、rq 150025、CR2 rq 139386、
以及本清单之外的任何行。
用法：docker exec -d nas-curator python3 /data/remaining4796_delete_encrypted.py --apply
"""
import argparse, csv, hashlib, json, os, sqlite3, sys, time

try:
    sys.path.insert(0, '/app')
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None
try:
    from PIL import Image, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass

DB = '/data/nas-curator.db'
VERIFY_TSV = '/data/remaining_photos_verify.tsv'
SUMMARY = '/data/remaining4796_delete_encrypted_summary.json'
PROGRESS_DIR = '/data/progress'
HB_ID = 'remaining4796-delete-encrypted'
DECIDED_BY = 'remaining-delete-encrypted'
ENC_IDS = list(range(138768, 138793))
MAGIC = b'__CLOUDSYNC_ENC__'


def _hb(**kw):
    if _write_hb is None:
        return
    try:
        _write_hb(HB_ID, progress_dir=PROGRESS_DIR, **kw)
    except Exception as e:
        print('heartbeat failed: %s' % e, flush=True)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def pil_strict_ok(path):
    try:
        with Image.open(path) as im:
            im.load()
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if not args.apply:
        print('need --apply')
        return 2
    verify = {}
    with open(VERIFY_TSV, encoding='utf-8') as f:
        rd = csv.DictReader(f, delimiter='\t')
        for r in rd:
            verify[int(r['rq_id'])] = r
    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    stamp = time.strftime('%Y%m%d-%H%M%S')
    bak = '/data/nas-curator.db.bak-%s-pre-remaining4796-delete-encrypted' % stamp
    with sqlite3.connect(bak) as dst:
        db.backup(dst)
    print('db backup:', bak, flush=True)
    cur = db.execute(
        "INSERT INTO plans (library, summary) VALUES ('media', ?)",
        (json.dumps({'kind': 'remaining4796-delete-encrypted',
                     'verify': VERIFY_TSV, 'backup': bak,
                     'rq_ids': ENC_IDS}, ensure_ascii=False),))
    plan_id = cur.lastrowid
    db.execute("UPDATE plans SET status='applying' WHERE id=?", (plan_id,))
    db.commit()
    print('plan #%d created' % plan_id, flush=True)

    _hb(phase='apply', status='running', total=len(ENC_IDS), done=0)
    counts = {}
    freed = 0
    deleted = []
    t0 = time.time()
    for n, rq_id in enumerate(ENC_IDS, 1):
        row = db.execute(
            "SELECT status, payload FROM review_queue WHERE id=?", (rq_id,)).fetchone()
        vrow = verify.get(rq_id, {})
        src = None
        size_exp = mtime_exp = sha_exp = None
        if row:
            d = json.loads(row[1])
            src = d.get('src', '')
        if vrow:
            size_exp = int(vrow['size_disk']) if vrow.get('size_disk') else None
            mtime_exp = float(vrow['mtime_disk']) if vrow.get('mtime_disk') else None
            sha_exp = vrow.get('sha256_disk') or None
        item_reason = ''
        action = 'delete'
        if not row:
            action, item_reason = 'skip', 'review-row-missing'
        elif row[0] != 'pending':
            action, item_reason = 'skip', 'status-not-pending:%s' % row[0]
        elif not src or '20200614' in src:
            action, item_reason = 'skip', 'src-excluded'
        cur = db.execute(
            """INSERT INTO plan_items
               (plan_id, src_path, category, confidence, reason, evidence,
                action, kind, size, ext, sha256, review_kind, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (plan_id, src or '', 'encrypted', 0.0,
             'remaining4796-delete-encrypted: header __CLOUDSYNC_ENC__ + unreadable; %s' % item_reason,
             'verify-verdict=%s' % (vrow.get('verdict') or ''),
             'review', 'media', size_exp,
             os.path.splitext(src or '')[1].lower(), sha_exp, 'ambiguous',
             'done' if action == 'delete' else 'skipped'))
        item_id = cur.lastrowid
        db.commit()
        if action != 'delete':
            counts['skip-' + item_reason] = counts.get('skip-' + item_reason, 0) + 1
            print('rq %d SKIP %s' % (rq_id, item_reason), flush=True)
            continue
        # 逐个核验：头 17 字节 + PIL 两次读不出 + 大小/mtime/sha256 未变
        try:
            with open(src, 'rb') as fh:
                head = fh.read(17)
        except FileNotFoundError:
            db.execute("UPDATE plan_items SET status='skipped' WHERE id=?", (item_id,))
            counts['already-gone'] = counts.get('already-gone', 0) + 1
            db.commit()
            print('rq %d SKIP already-gone' % rq_id, flush=True)
            continue
        if head != MAGIC:
            db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (item_id,))
            counts['fail-header-not-encrypted'] = counts.get('fail-header-not-encrypted', 0) + 1
            db.commit()
            print('rq %d FAIL header mismatch: %r' % (rq_id, head), flush=True)
            continue
        if pil_strict_ok(src) or pil_strict_ok(src):
            db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (item_id,))
            counts['fail-readable'] = counts.get('fail-readable', 0) + 1
            db.commit()
            print('rq %d FAIL unexpectedly readable' % rq_id, flush=True)
            continue
        try:
            st = os.stat(src)
        except FileNotFoundError:
            db.execute("UPDATE plan_items SET status='skipped' WHERE id=?", (item_id,))
            counts['already-gone'] = counts.get('already-gone', 0) + 1
            db.commit()
            continue
        if size_exp is not None and st.st_size != size_exp:
            db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (item_id,))
            counts['fail-size-changed'] = counts.get('fail-size-changed', 0) + 1
            db.commit()
            print('rq %d FAIL size changed %d != %d' % (rq_id, st.st_size, size_exp), flush=True)
            continue
        if mtime_exp is not None and abs(st.st_mtime - mtime_exp) > 1:
            db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (item_id,))
            counts['fail-mtime-changed'] = counts.get('fail-mtime-changed', 0) + 1
            db.commit()
            print('rq %d FAIL mtime changed' % rq_id, flush=True)
            continue
        h = sha256(src)
        if sha_exp and h != sha_exp:
            db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (item_id,))
            counts['fail-hash-changed'] = counts.get('fail-hash-changed', 0) + 1
            db.commit()
            print('rq %d FAIL sha changed' % rq_id, flush=True)
            continue
        try:
            os.remove(src)
            freed += st.st_size
        except Exception as e:
            db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (item_id,))
            counts['fail-delete'] = counts.get('fail-delete', 0) + 1
            print('rq %d FAIL delete %s: %s' % (rq_id, type(e).__name__, str(e)[:120]), flush=True)
            db.commit()
            continue
        cur2 = db.execute(
            "UPDATE review_queue SET status='approved', decided_by=?, "
            "decided_at=strftime('%Y-%m-%dT%H:%M:%S','now') "
            "WHERE id=? AND status='pending'", (DECIDED_BY, rq_id))
        if cur2.rowcount:
            counts['review-rows-approved'] = counts.get('review-rows-approved', 0) + cur2.rowcount
        counts['file-deleted'] = counts.get('file-deleted', 0) + 1
        deleted.append({'rq_id': rq_id, 'src': src, 'size': st.st_size, 'sha256': h})
        db.commit()
        print('rq %d DELETED %s (%d bytes)' % (rq_id, src, st.st_size), flush=True)
        _hb(phase='apply', status='running', total=len(ENC_IDS), done=n)

    db.execute(
        "UPDATE plans SET status='done', summary=? WHERE id=?",
        (json.dumps({'kind': 'remaining4796-delete-encrypted', 'verify': VERIFY_TSV,
                     'backup': bak, 'counts': counts, 'freed_bytes': freed,
                     'deleted': deleted}, ensure_ascii=False), plan_id))
    db.commit()
    print('integrity:', db.execute('PRAGMA integrity_check').fetchone()[0], flush=True)
    print('pending now:', db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE status='pending'").fetchone()[0], flush=True)
    print('plan #%d items:' % plan_id, db.execute(
        "SELECT status, COUNT(*) FROM plan_items WHERE plan_id=? GROUP BY status",
        (plan_id,)).fetchall(), flush=True)
    print('APPLY DONE n=%d elapsed=%.1fs counts=%s freed=%d bytes (%.3f GB)'
          % (len(ENC_IDS), time.time() - t0, counts, freed, freed / (1 << 30)), flush=True)
    _hb(phase='apply', status='done', total=len(ENC_IDS), done=len(ENC_IDS))
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump({'phase': 'apply', 'plan_id': plan_id, 'backup': bak,
                   'n': len(ENC_IDS), 'elapsed': round(time.time() - t0, 1),
                   'counts': counts, 'freed_bytes': freed, 'deleted': deleted},
                  f, ensure_ascii=False, indent=2)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
