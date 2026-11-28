#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796 删除清单构建（容器内，只读＋算哈希，不删文件）。
对 --ids 文件（逗号分隔 rq_id）逐个：
- 校验 review 行仍 pending、src 在盘、大小/mtime 与 payload 一致；
- 算 sha256；--mode encrypted 时再验文件头 __CLOUDSYNC_ENC__（17 字节）。
输出 --manifest TSV（rq_id, src, size_disk, mtime_disk, sha256, reason, evidence, delete）。
任一校验失败的行记 delete=0 并写明原因，不进删除集。
用法：docker exec nas-curator python3 /data/r4796_build_delete_manifest.py \
  --ids <csv> --mode encrypted --manifest /data/r4796_delete_enc.tsv \
  --evidence <json-or-empty>
"""
import argparse, hashlib, json, os, sqlite3, sys

DB = '/data/nas-curator.db'
ENC = b'__CLOUDSYNC_ENC__'


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def clean(s):
    return (s or '').replace('\t', ' ').replace('\n', ' ').replace('\r', ' ')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ids', required=True)
    ap.add_argument('--mode', required=True, choices=['encrypted', 'undeterminable'])
    ap.add_argument('--manifest', required=True)
    ap.add_argument('--evidence', default='')
    args = ap.parse_args()
    ids = [int(x) for x in args.ids.split(',') if x.strip()]
    ev = {}
    if args.evidence:
        ev = json.load(open(args.evidence, encoding='utf-8'))
    db = sqlite3.connect(DB)
    rows = []
    for rid in ids:
        r = {'rq_id': rid, 'src': '', 'size_disk': '', 'mtime_disk': '',
             'sha256': '', 'reason': '', 'evidence': '', 'delete': '0'}
        row = db.execute("SELECT status, payload FROM review_queue WHERE id=?", (rid,)).fetchone()
        if not row:
            r['reason'] = 'row_gone'
        elif row[0] != 'pending':
            r['reason'] = 'not_pending:' + row[0]
        else:
            d = json.loads(row[1])
            src = d.get('src', '') or ''
            r['src'] = src
            try:
                st = os.stat(src)
            except FileNotFoundError:
                r['reason'] = 'src_missing'
            except Exception as e:
                r['reason'] = 'stat_error:' + type(e).__name__
            else:
                r['size_disk'] = st.st_size
                r['mtime_disk'] = st.st_mtime
                if d.get('size') is not None and int(d['size']) != st.st_size:
                    r['reason'] = 'size_differs_from_payload'
                elif args.mode == 'encrypted':
                    with open(src, 'rb') as fh:
                        head = fh.read(17)
                    if head != ENC:
                        r['reason'] = 'magic_not_enc'
                    else:
                        r['sha256'] = sha256_of(src)
                        r['reason'] = 'header __CLOUDSYNC_ENC__ confirmed, unreadable'
                        r['evidence'] = 'head17=%s size=%d' % (head.hex(), st.st_size)
                        r['delete'] = '1'
                else:
                    e = ev.get(str(rid)) or ev.get(rid) or {}
                    r['sha256'] = sha256_of(src)
                    r['reason'] = e.get('reason', 'undeterminable')
                    r['evidence'] = e.get('evidence', '')
                    r['delete'] = '1'
        rows.append(r)
    with open(args.manifest, 'w', encoding='utf-8') as f:
        f.write('rq_id\tsrc\tsize_disk\tmtime_disk\tsha256\treason\tevidence\tdelete\n')
        for r in rows:
            f.write('%d\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' % (
                r['rq_id'], clean(r['src']), r['size_disk'], r['mtime_disk'],
                r['sha256'], clean(r['reason']), clean(r['evidence']), r['delete']))
    n_del = sum(1 for r in rows if r['delete'] == '1')
    n_skip = [r for r in rows if r['delete'] != '1']
    print('manifest %s: n=%d delete=%d skip=%d' % (args.manifest, len(rows), n_del, len(n_skip)))
    for r in n_skip:
        print(' SKIP rq=%d %s' % (r['rq_id'], r['reason']))
    print('delete bytes: %d' % sum(int(r['size_disk']) for r in rows if r['delete'] == '1' and r['size_disk'] != ''))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
