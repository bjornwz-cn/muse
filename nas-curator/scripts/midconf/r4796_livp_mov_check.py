#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796 深度核查：3 个 CRC 失败 .livp 的 .mov 条目是否内容完好（容器内，只读）。
按中央目录记录的偏移直接读原始字节（忽略 CRC），检查 mov 头
（ftyp/moov）是否存在；51248 的 mov 本地头已坏，改为在 heic 数据
结束后扫描 b'ftyp' 定位 mov 数据起始。
用法：docker exec nas-curator python3 /data/r4796_livp_mov_check.py
"""
import json, sqlite3, struct

DB = '/data/nas-curator.db'
IDS = [51248, 51249, 139388]


def parse_central(data):
    """返回 {filename: (local_header_offset, method, csize, usize, crc)}。"""
    out = {}
    pos = 0
    while True:
        i = data.find(b'PK\x01\x02', pos)
        if i < 0:
            break
        (ver_made, ver_need, flag, method, mtime, mdate, crc, csize, usize,
         nlen, elen, clen, disk, iattr, eattr, lho) = struct.unpack(
            '<HHHHHHIIIHHHHHII', data[i + 4:i + 46])
        fname = data[i + 46:i + 46 + nlen].decode('utf-8', errors='replace')
        out[fname] = (lho, method, csize, usize, crc)
        pos = i + 46 + nlen + elen + clen
    return out


def main():
    db = sqlite3.connect(DB)
    for rid in IDS:
        src = json.loads(db.execute(
            "SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()[0])['src']
        print('=== rq %d %s' % (rid, src))
        with open(src, 'rb') as fh:
            data = fh.read()
        print(' file size:', len(data))
        central = parse_central(data)
        print(' central:', {k: (v[0], v[1], v[2]) for k, v in central.items()})
        for fname, (lho, method, csize, usize, crc) in central.items():
            if not fname.lower().endswith('.mov'):
                continue
            print(' mov %s local_header_offset=%d method=%d csize=%d' % (fname, lho, method, csize))
            hdr = data[lho:lho + 4]
            print('  bytes at local header offset:', hdr)
            if hdr == b'PK\x03\x04':
                (ver, flag, m2, mt, md, c2, cs2, us2, nlen, elen) = struct.unpack(
                    '<HHHHHIIIHH', data[lho + 4:lho + 30])
                dstart = lho + 30 + nlen + elen
                print('  mov data start=%d head16=%s' % (dstart, data[dstart:dstart + 16].hex()))
                print('  mov data ascii:', ''.join(chr(c) if 32 <= c < 127 else '.' for c in data[dstart:dstart + 16]))
            else:
                # 扫描 ftyp
                j = data.find(b'ftyp', lho)
                print('  header damaged; first ftyp at offset %d (delta %d)' % (j, j - lho if j >= 0 else -1))
                if j >= 0:
                    print('  16 bytes before ftyp:', data[j - 4:j + 12].hex())
                    print('  mov data head:', data[j - 4:j + 12])


if __name__ == '__main__':
    main()
