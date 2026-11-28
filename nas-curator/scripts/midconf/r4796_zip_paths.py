#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796：zip 路径核查＋1 个税务 PDF 读公司名（容器内，只读）。
打印 40 个 .zip 行的完整 src 路径；解开 137964 的 CBJ.pdf，
用 pypdf/pdftotext 读文本找纳税人名称。
用法：docker exec nas-curator python3 /data/r4796_zip_paths.py
"""
import csv, io, json, os, re, sqlite3, zipfile

DB = '/data/nas-curator.db'


def pdf_text(data):
    try:
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(data))
        return '\n'.join((p.extract_text() or '') for p in r.pages[:2])
    except Exception as e:
        return 'pypdf fail: %s' % e


def main():
    db = sqlite3.connect(DB)
    rows = []
    with open('/data/r4796_others.tsv', encoding='utf-8') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            if r['ext'] == '.zip':
                rows.append(r)
    print('zip rows: %d' % len(rows))
    for r in rows:
        prow = db.execute("SELECT status FROM review_queue WHERE id=?", (int(r['rq_id']),)).fetchone()
        print('%s %s %s' % (r['rq_id'], prow[0] if prow else 'gone', r['src']))
    # 读 137964 的 CBJ.pdf
    src = json.loads(db.execute(
        "SELECT payload FROM review_queue WHERE id=137964").fetchone()[0])['src']
    z = zipfile.ZipFile(src)
    names = z.namelist()
    print('137964 entries:', names)
    for n in names:
        if n.endswith('CBJ.pdf'):
            txt = pdf_text(z.read(n))
            print('--- CBJ.pdf text (first 1500 chars) ---')
            print(txt[:1500])
            m = re.findall(r'[\u4e00-\u9fa5]{2,20}(?:有限公司|公司|局|中心)', txt)
            print('company-like strings:', list(dict.fromkeys(m))[:10])
            break


if __name__ == '__main__':
    main()
