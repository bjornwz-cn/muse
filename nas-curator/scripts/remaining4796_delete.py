#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剩余文档删除（remaining-4796，2026-11-25）：删确认加密 / 确实定不了的文档。
两批分开跑：--kind encrypted | undet。
  encrypted 清单 /data/remaining4796_del_enc_manifest.json（689 行）：
    662 个文件头 magic __CLOUDSYNC_ENC__，26 个 .pag（群晖 CloudSync 状态文件，
    25 个头 0a00ec03…、1 个全零 1024 字节），1 个 .eml 随机二进制头（138724，
    最长可打印 runs 仅 13 字符、无 MIME/zip 结构）。
    注意：138738 .osheet 与 138747 .odoc 原以为是加密，经纯 Python 深挖证实为
    可读群晖 Office 文档（含完整正文：防护用品报价表、防疫物资储备方案），
    已移出删除清单、另行分类，不删。
  undet 清单 /data/remaining4796_del_undet_manifest.json（31 行）：
    29 个 0 字节 PDF（rq 137918-137946），139130 02_2.xls（空表，仅内置数字格式串，
    无单元格文本、无文件名提示），139392 Doc1.docx（仅一朵花剪贴画、无文本、OCR 仅 "WW"）。
Phase --verify：逐个重验（仍 pending、非排除项、文件在、大小未变、加密头/0 字节/
无文本指纹仍成立），写核验 manifest，不删文件、不写库。
Phase --apply：sqlite backup 后建 plan + plan_items（action='review'），重验
大小+mtime 后删文件、review 行标 approved（decided_by 按 kind）。
永不删：5 个凭据文件、rq 137963、rq 149995、rq 150025（显式排除，命中即跳过）。
只删清单内、核验通过的行。容器内运行。"""
import argparse, hashlib, json, os, sqlite3, sys, time, subprocess

DB = '/data/nas-curator.db'
MANIFESTS = {'encrypted': '/data/remaining4796_del_enc_manifest.json',
             'undet': '/data/remaining4796_del_undet_manifest.json'}
VERIFY_OUT = {'encrypted': '/data/remaining4796_del_enc_verify.tsv',
              'undet': '/data/remaining4796_del_undet_verify.tsv'}
SUMMARY = {'encrypted': '/data/remaining4796_del_enc_summary.json',
           'undet': '/data/remaining4796_del_undet_summary.json'}
DECIDED_BY = {'encrypted': 'remaining-delete-encrypted',
              'undet': 'remaining-delete-undeterminable'}
PROGRESS_DIR = '/data/progress'
HB_ID = 'remaining-4796-delete'
NEVER = {138941, 138943, 139426, 139428, 139432, 137963, 149995, 150025}
BINARY3 = {138724}  # 仅 .eml 随机二进制头；138738/138747 经深挖为可读群晖 Office 文档，已移出清单

try:
    sys.path.insert(0, '/app')
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None


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


def py_scan(path):
    """纯 Python 可读内容扫描（不依赖 strings 二进制，容器内无此命令）。
    返回 (printable_runs_ge8, longest_run, cjk_chars)。"""
    import re as _re
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except Exception:
        return -1, -1, -1
    runs = [m.group(0) for m in _re.finditer(rb'[\x20-\x7e]{8,}', data)]
    txt = data.decode('utf-8', errors='ignore')
    cjk = sum(1 for ch in txt if '\u4e00' <= ch <= '\u9fff')
    return len(runs), max((len(r) for r in runs), default=0), cjk


def check_condition(kind, row, head):
    """重验删除条件。返回 (ok, detail)。"""
    rq_id = row['rq_id']
    if kind == 'encrypted':
        if head.startswith(b'__CLOUDSYNC_ENC__'):
            return True, 'magic-head'
        if row['ext'] == '.pag':
            # 群晖 CloudSync 状态文件：标准头 0a00ec03… 或全零文件（无内容）
            if head[:8].hex() == '0a00ec03cd03b703':
                n, longest, cjk = py_scan(row['src'])
                # .pag 内仅 WebDAV 同步元数据（属性名/RFC 日期，最长 29 字符），无正文、无中文
                if longest >= 40 or cjk > 0:
                    return False, 'pag-has-content-run-%d/cjk=%d' % (longest, cjk)
                return True, 'pag-state-head'
            try:
                with open(row['src'], 'rb') as fh:
                    data = fh.read()
                if len(data) == 1024 and all(b == 0 for b in data):
                    return True, 'pag-all-zero-1024'
            except Exception:
                pass
            return False, 'pag-head-unknown'
        if rq_id in BINARY3:
            n, longest, cjk = py_scan(row['src'])
            if longest <= 20 and not head[:2] == b'PK':
                return True, 'binary-head-longest-run-%d' % longest
            return False, 'binary-head-readable-longest-%d/cjk=%d' % (longest, cjk)
        return False, 'no-encrypted-head'
    # undet
    if row['kind'] == 'zero-byte':
        return (True, 'size-0') if row['size'] == 0 else (False, 'size-not-0')
    if rq_id == 139130:
        import re as _re
        with open(row['src'], 'rb') as fh:
            data = fh.read()
        # 有意义的可打印串：去首尾空格后仍 >=8 字符的串（排除纯空格填充）
        runs = [m.group(0).decode().strip() for m in _re.finditer(rb'[\x20-\x7e]{8,}', data)]
        meaningful = [r for r in runs if len(r) >= 8]
        longest = max((len(r) for r in meaningful), default=0)
        # 连续中文：>=2 个连续汉字才算（单个汉字多为随机字节误解码）
        txt = data.decode('utf-8', errors='ignore')
        cjk_runs = _re.findall(r'[\u4e00-\u9fff]{2,}', txt)
        # 排除纯数字格式串后是否还有别的文本：只允许数字格式/ThisWorkbook/日期/ID
        allowed = all(_re.fullmatch(r'[#0.,;_ *\\\\$()\"?@\-+Red\[\]a-zA-Z/ ]+', r) or
                      r in ('ThisWorkbook',) or _re.fullmatch(r'[0-9\-]+', r)
                      for r in meaningful)
        if longest <= 60 and not cjk_runs and allowed:
            return True, 'ole-format-strings-only-longest-%d' % longest
        return False, 'readable-longest-%d/cjk-runs=%d/allowed=%s' % (longest, len(cjk_runs), allowed)
    if rq_id == 139392:
        import zipfile
        try:
            z = zipfile.ZipFile(row['src'])
            media = [x for x in z.namelist() if x.startswith('word/media/')]
            docxml = z.read('word/document.xml').decode(errors='replace')
            import re
            texts = re.findall(r'<w:t[^>]*>([^<]*)</w:t>', docxml)
            joined = ''.join(texts).strip()
            if len(media) == 1 and joined == '':
                return True, 'only-flower-image-no-text'
            return False, 'media=%d text=%r' % (len(media), joined[:40])
        except Exception as e:
            return False, 'docx-check-error:%s' % type(e).__name__
    return False, 'unknown-undet-kind'


def verify(kind):
    with open(MANIFESTS[kind], encoding='utf-8') as f:
        rows = json.load(f)
    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    pending = {}
    for rid, src in db.execute(
            "SELECT id, json_extract(payload,'$.src') FROM review_queue "
            "WHERE status='pending'"):
        if src:
            pending[src] = rid
    db.close()
    counts = {}
    freed = 0
    t0 = time.time()
    _hb(label='剩余文档删除核验', kind='remaining-4796-delete', phase='verify-' + kind,
        status='running', total=len(rows), done=0)
    with open(VERIFY_OUT[kind], 'w', encoding='utf-8') as out:
        out.write('rq_id\tsrc\tsize_list\tsize_disk\tmtime_disk\tsha256_disk\taction\tdetail\n')
        for i, row in enumerate(rows):
            rq_id, src = row['rq_id'], row['src']
            action, detail = '', ''
            size_disk, mtime_disk, sha_disk = '', '', ''
            if rq_id in NEVER:
                action, detail = 'skip-excluded', 'in NEVER set'
            elif pending.get(src) != rq_id:
                action, detail = 'skip-not-pending', 'rq mismatch or not pending'
            else:
                try:
                    st = os.stat(src)
                    size_disk, mtime_disk = st.st_size, st.st_mtime
                except FileNotFoundError:
                    action, detail = 'skip-src-gone', 'not-on-disk'
                else:
                    if not os.path.isfile(src):
                        action, detail = 'skip-src-gone', 'not-a-file'
                    elif int(row['size']) != st.st_size:
                        action, detail = 'skip-size-changed', 'list=%s disk=%d' % (row['size'], st.st_size)
                    else:
                        with open(src, 'rb') as fh:
                            head = fh.read(64)
                        ok, det = check_condition(kind, row, head)
                        if ok:
                            sha_disk = sha256(src)
                            action, detail = 'delete', det
                            freed += st.st_size
                        else:
                            action, detail = 'skip-condition-failed', det
            out.write('%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' % (
                rq_id, src, row['size'], size_disk, mtime_disk, sha_disk, action, detail))
            counts[action] = counts.get(action, 0) + 1
            if (i + 1) % 100 == 0:
                print('%d/%d verified (%.1fs) counts=%s' % (i + 1, len(rows), time.time() - t0, counts), flush=True)
                _hb(label='剩余文档删除核验', kind='remaining-4796-delete', phase='verify-' + kind,
                    status='running', total=len(rows), done=i + 1)
    print('VERIFY-%s DONE n=%d elapsed=%.1fs counts=%s freed_if_deleted=%d bytes (%.2f MB)'
          % (kind, len(rows), time.time() - t0, counts, freed, freed / (1 << 20)), flush=True)
    _hb(label='剩余文档删除核验', kind='remaining-4796-delete', phase='verify-' + kind,
        status='done', total=len(rows), done=len(rows))
    with open(SUMMARY[kind], 'w', encoding='utf-8') as f:
        json.dump({'phase': 'verify', 'kind': kind, 'n': len(rows),
                   'elapsed': round(time.time() - t0, 1), 'counts': counts,
                   'freed_if_deleted_bytes': freed}, f, ensure_ascii=False, indent=2)
    return 0


def apply(kind):
    if not os.path.exists(VERIFY_OUT[kind]):
        print('no manifest %s: run --verify first' % VERIFY_OUT[kind])
        return 2
    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    stamp = time.strftime('%Y%m%d-%H%M%S')
    bak = '/data/nas-curator.db.bak-%s-pre-remaining4796-delete-%s' % (stamp, kind)
    with sqlite3.connect(bak) as dst:
        db.backup(dst)
    print('db backup:', bak, flush=True)
    cur = db.execute(
        "INSERT INTO plans (library, summary) VALUES ('docs', ?)",
        (json.dumps({'kind': 'remaining4796-delete-' + kind,
                     'manifest': VERIFY_OUT[kind], 'backup': bak},
                    ensure_ascii=False),))
    plan_id = cur.lastrowid
    db.execute("UPDATE plans SET status='applying' WHERE id=?", (plan_id,))
    db.commit()
    print('plan #%d created' % plan_id, flush=True)
    rows = []
    with open(VERIFY_OUT[kind], encoding='utf-8') as f:
        f.readline()
        for line in f:
            parts = line.rstrip('\n').split('\t')
            if len(parts) < 8:
                parts += [''] * (8 - len(parts))
            rows.append(dict(zip(['rq_id', 'src', 'size_list', 'size_disk',
                                  'mtime_disk', 'sha256_disk', 'action', 'detail'], parts)))
    _hb(label='剩余文档删除执行', kind='remaining-4796-delete', phase='apply-' + kind,
        status='running', total=len(rows), done=0)
    counts = {}
    freed = 0
    t0 = time.time()
    for i, r in enumerate(rows):
        src = r['src']
        action = r['action']
        rq_id = int(r['rq_id']) if r['rq_id'] else None
        if (i + 1) % 100 == 0:
            print('%d/%d applied (%.1fs) counts=%s' % (i + 1, len(rows), time.time() - t0, counts), flush=True)
            _hb(label='剩余文档删除执行', kind='remaining-4796-delete', phase='apply-' + kind,
                status='running', total=len(rows), done=i + 1)
        cur = db.execute(
            """INSERT INTO plan_items
               (plan_id, src_path, category, confidence, reason, evidence,
                action, kind, size, ext, sha256, review_kind, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (plan_id, src, 'deleted-' + kind, 0.0,
             '%s:%s' % (DECIDED_BY[kind], r['detail'] or action),
             r['detail'], 'review', 'docs',
             int(r['size_disk']) if r['size_disk'] else None,
             os.path.splitext(src)[1].lower(),
             r['sha256_disk'] or None, 'low_conf',
             'done' if action == 'delete' else 'skipped'))
        if action != 'delete':
            counts['skip-' + action] = counts.get('skip-' + action, 0) + 1
            db.commit()
            continue
        try:
            st = os.stat(src)
            if r['size_disk'] and int(r['size_disk']) != st.st_size:
                db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (cur.lastrowid,))
                counts['skip-size-changed'] = counts.get('skip-size-changed', 0) + 1
                db.commit()
                continue
            if r['mtime_disk'] and abs(float(r['mtime_disk']) - st.st_mtime) > 1:
                db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (cur.lastrowid,))
                counts['skip-mtime-changed'] = counts.get('skip-mtime-changed', 0) + 1
                db.commit()
                continue
        except FileNotFoundError:
            db.execute("UPDATE plan_items SET status='skipped' WHERE id=?", (cur.lastrowid,))
            counts['already-gone'] = counts.get('already-gone', 0) + 1
            db.commit()
            continue
        try:
            os.remove(src)
            freed += st.st_size
        except Exception as e:
            db.execute("UPDATE plan_items SET status='failed' WHERE id=?", (cur.lastrowid,))
            counts['failed-delete'] = counts.get('failed-delete', 0) + 1
            print('FAILED delete %s : %s: %s' % (src, type(e).__name__, str(e)[:120]), flush=True)
            db.commit()
            continue
        if rq_id:
            cur2 = db.execute(
                "UPDATE review_queue SET status='approved', "
                "decided_by=?, decided_at=strftime('%Y-%m-%dT%H:%M:%S','now') "
                "WHERE id=? AND status='pending'", (DECIDED_BY[kind], rq_id))
            if cur2.rowcount:
                counts['review-rows-approved'] = counts.get('review-rows-approved', 0) + cur2.rowcount
        counts['file-deleted'] = counts.get('file-deleted', 0) + 1
        db.commit()
    db.execute(
        "UPDATE plans SET status='done', summary=? WHERE id=?",
        (json.dumps({'kind': 'remaining4796-delete-' + kind,
                     'manifest': VERIFY_OUT[kind], 'backup': bak,
                     'counts': counts, 'freed_bytes': freed},
                    ensure_ascii=False), plan_id))
    db.commit()
    print('integrity:', db.execute('PRAGMA integrity_check').fetchone()[0], flush=True)
    print('pending now:', db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE status='pending'").fetchone()[0], flush=True)
    print('plan #%d items:' % plan_id, db.execute(
        "SELECT status, COUNT(*) FROM plan_items WHERE plan_id=? GROUP BY status",
        (plan_id,)).fetchall(), flush=True)
    print('APPLY-%s DONE n=%d elapsed=%.1fs counts=%s freed=%d bytes (%.2f MB)'
          % (kind, len(rows), time.time() - t0, counts, freed, freed / (1 << 20)), flush=True)
    _hb(label='剩余文档删除执行', kind='remaining-4796-delete', phase='apply-' + kind,
        status='done', total=len(rows), done=len(rows))
    with open(SUMMARY[kind], 'w', encoding='utf-8') as f:
        json.dump({'phase': 'apply', 'kind': kind, 'plan_id': plan_id, 'backup': bak,
                   'n': len(rows), 'elapsed': round(time.time() - t0, 1),
                   'counts': counts, 'freed_bytes': freed},
                  f, ensure_ascii=False, indent=2)
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--verify', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--kind', choices=['encrypted', 'undet'], required=True)
    args = ap.parse_args()
    if args.verify:
        raise SystemExit(verify(args.kind))
    elif args.apply:
        raise SystemExit(apply(args.kind))
    else:
        ap.print_help()
        raise SystemExit(2)
