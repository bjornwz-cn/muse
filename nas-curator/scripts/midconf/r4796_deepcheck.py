#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796 深度核查（容器内，只读）：137724 eml 二进制体、139385 root.bolt、
139399 rtf 全文、rar 工具可用性。输出打印到 stdout。"""
import sqlite3, json, re, zlib, gzip

DB = '/data/nas-curator.db'


def main():
    db = sqlite3.connect(DB)
    for rid in (139385,):
        src = json.loads(db.execute(
            "SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()[0])['src']
        print('=== rq %d %s' % (rid, src))
        try:
            with open(src, 'rb') as f:
                data = f.read(65536)
        except FileNotFoundError:
            print(' src missing (already deleted?)')
            continue
        print(' head64 hex:', data[:64].hex())
        print(' head64 ascii:', ''.join(chr(c) if 32 <= c < 127 else '.' for c in data[:64]))
        strs = re.findall(rb'[\x20-\x7e]{8,}', data)
        print(' strings>=8 in first 64KB:', len(strs), [s.decode()[:60] for s in strs[:10]])
        for name, fn in (('gzip', gzip.decompress), ('zlib', zlib.decompress)):
            try:
                out = fn(data[:10000])
                print('  %s OK len=%d head=%r' % (name, len(out), out[:80]))
            except Exception as e:
                print('  %s no: %s' % (name, str(e)[:60]))
    src = json.loads(db.execute(
        "SELECT payload FROM review_queue WHERE id=139399").fetchone()[0])['src']
    print('=== rtf 139399:', src)
    txt = open(src, encoding='utf-8', errors='replace').read()
    print(txt[:2000])
    print('--- rtf stripped text sample ---')
    stripped = re.sub(r'\\[a-z]+\d*', ' ', txt)
    stripped = re.sub(r'[{}]', ' ', stripped)
    stripped = re.sub(r'\s+', ' ', stripped).strip()
    print(stripped[:1500])


if __name__ == '__main__':
    main()
