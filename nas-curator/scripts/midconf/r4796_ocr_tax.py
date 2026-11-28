#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796：税务 PDF OCR 找纳税人名称（容器内，只读＋写 1 个样本文件）。
解开 137964 的 CBJ.pdf 到 /data/r4796_tax_sample.pdf，
POST ocr-service（http://172.17.0.1:8866/ocr，max_pages=3），
打印 OCR 文本前 2000 字并搜索纳税人/公司名称。
用法：docker exec nas-curator python3 /data/r4796_ocr_tax.py
"""
import io, json, re, sqlite3, zipfile

import requests

DB = '/data/nas-curator.db'
OCR_URL = 'http://172.17.0.1:8866/ocr'


def main():
    db = sqlite3.connect(DB)
    src = json.loads(db.execute(
        "SELECT payload FROM review_queue WHERE id=137964").fetchone()[0])['src']
    z = zipfile.ZipFile(src)
    name = [n for n in z.namelist() if n.endswith('CBJ.pdf')][0]
    data = z.read(name)
    with open('/data/r4796_tax_sample.pdf', 'wb') as f:
        f.write(data)
    print('sample pdf:', name, len(data), 'bytes')
    with open('/data/r4796_tax_sample.pdf', 'rb') as f:
        r = requests.post(OCR_URL,
                          files={'file': ('CBJ.pdf', f, 'application/pdf')},
                          data={'max_pages': '3'}, timeout=600)
    print('ocr status:', r.status_code)
    if r.status_code != 200:
        print('ocr fail:', r.text[:300])
        return
    text = (r.json().get('text') or '')
    print('ocr chars:', len(text))
    print('--- text head 2000 ---')
    print(text[:2000])
    for pat in (r'纳税人名称[:：\s]*([^\s\n]{2,30})',
                r'纳税人[:：\s]*([^\s\n]{2,30})',
                r'(陕西[\u4e00-\u9fa5]{2,20}(?:有限公司|公司))',
                r'([\u4e00-\u9fa5]{2,20}医疗器械有限公司)'):
        m = re.findall(pat, text)
        if m:
            print('MATCH', pat, '->', list(dict.fromkeys(m))[:5])


if __name__ == '__main__':
    main()
