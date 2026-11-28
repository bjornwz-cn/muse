#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796 深度核查：全部 48 个 .livp 的两个条目逐个 CRC 校验（容器内，只读）。
zipfile 读条目即校验 CRC；失败的再用原始字节读并 PIL/头校验，
区分“内容完好仅 CRC 字段错”与“内容真损坏”。
输出 /data/r4796_livp_crc_all.tsv。
用法：docker exec nas-curator python3 /data/r4796_livp_crc_all.py
"""
import csv, io, json, os, sqlite3, zipfile
from PIL import Image

LIST = '/data/r4796_others.tsv'
OUT = '/data/r4796_livp_crc_all.tsv'
DB = '/data/nas-curator.db'


def clean(s):
    return (s or '').replace('\t', ' ').replace('\n', ' ').replace('\r', ' ')


def main():
    db = sqlite3.connect(DB)
    rows = []
    with open(LIST, encoding='utf-8') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            if r['ext'] == '.livp':
                rows.append(r)
    out = []
    for r in rows:
        rid = int(r['rq_id'])
        src = r['src']
        prow = db.execute("SELECT status FROM review_queue WHERE id=?", (rid,)).fetchone()
        if not prow or prow[0] != 'pending':
            out.append({'rq_id': rid, 'src': src, 'entry': '', 'verdict': 'skip-not-pending',
                        'detail': 'status=%s' % (prow[0] if prow else 'gone')})
            continue
        try:
            z = zipfile.ZipFile(src)
            infos = z.infolist()
        except Exception as e:
            out.append({'rq_id': rid, 'src': src, 'entry': '', 'verdict': 'zip_open_failed',
                        'detail': '%s: %s' % (type(e).__name__, str(e)[:100])})
            continue
        for info in infos:
            rec = {'rq_id': rid, 'src': src, 'entry': info.filename, 'verdict': '', 'detail': ''}
            try:
                raw = z.read(info.filename)
                rec['verdict'] = 'crc_ok'
                rec['detail'] = 'len=%d' % len(raw)
            except Exception as e:
                # 原始字节读：按中央目录偏移取 stored 数据
                try:
                    with open(src, 'rb') as fh:
                        data = fh.read()
                    import struct
                    lho = info.header_offset
                    if data[lho:lho + 4] == b'PK\x03\x04':
                        (ver, flag, m2, mt, md, c2, cs2, us2, nlen, elen) = struct.unpack(
                            '<HHHHHIIIHH', data[lho + 4:lho + 30])
                        dstart = lho + 30 + nlen + elen
                        raw2 = data[dstart:dstart + info.compress_size]
                        if info.filename.lower().endswith(('.heic', '.heif')):
                            import pillow_heif
                            pillow_heif.register_heif_opener()
                            img = Image.open(io.BytesIO(raw2))
                            img.verify()
                            rec['verdict'] = 'content_ok_crc_bad'
                            rec['detail'] = 'still decodes, format=%s size=%s (CRC field wrong)' % (img.format, img.size)
                        elif info.filename.lower().endswith('.mov'):
                            head = raw2[:16].hex()
                            rec['verdict'] = ('content_ok_crc_bad' if raw2[:8][4:8] == b'ftyp'
                                              else 'content_bad')
                            rec['detail'] = 'mov head16=%s' % head
                        else:
                            rec['verdict'] = 'content_unknown'
                            rec['detail'] = 'raw len=%d' % len(raw2)
                    else:
                        rec['verdict'] = 'content_bad'
                        rec['detail'] = 'local header damaged (%s); %s' % (
                            data[lho:lho + 4].hex(), str(e)[:80])
                except Exception as e2:
                    rec['verdict'] = 'check_failed'
                    rec['detail'] = '%s / %s' % (str(e)[:80], str(e2)[:80])
            out.append(rec)
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write('rq_id\tsrc\tentry\tverdict\tdetail\n')
        for r in out:
            f.write('%d\t%s\t%s\t%s\t%s\n' % (
                r['rq_id'], clean(r['src']), clean(r['entry']), r['verdict'], clean(r['detail'])))
    from collections import Counter
    print('SUMMARY entries=%d verdicts=%s' % (len(out), dict(Counter(r['verdict'] for r in out))))
    bad = [r for r in out if r['verdict'] in ('content_bad', 'zip_open_failed', 'check_failed')]
    print('BAD entries: %d' % len(bad))
    for r in bad:
        print(' BAD rq=%d %s %s' % (r['rq_id'], r['entry'], r['detail'][:120]))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
