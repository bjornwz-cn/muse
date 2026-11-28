#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnostic: are the cv2-ok (PIL-fail) files truncated?
For a sample of skip-readable cv2-ok files, check:
- PIL error message
- whether the file ends with JPEG EOI marker (0xFFD9) -> truncated if missing
- cv2 image shape (does it return pixels?)
Read-only."""
import os
from PIL import Image
import cv2

rows = []
with open('/data/batch4_delete_verify.tsv', encoding='utf-8') as f:
    hdr = f.readline().rstrip('\n').split('\t')
    for line in f:
        p = line.rstrip('\n').split('\t')
        if len(p) < len(hdr):
            p += [''] * (len(hdr) - len(p))
        r = dict(zip(hdr, p))
        if r.get('action') == 'skip-readable' and 'cv2-ok' in r.get('detail', ''):
            rows.append(r['src'])
print('cv2-ok total:', len(rows))
sample = rows[::max(1, len(rows) // 20)][:20]
n_eoi_missing = 0
n_pil_err = 0
for src in sample:
    ext = os.path.splitext(src)[1].lower()
    pil_err = ''
    try:
        with Image.open(src) as im:
            im.load()
        pil_err = 'PIL-OK?!'
    except Exception as e:
        pil_err = '%s:%s' % (type(e).__name__, str(e)[:70])
        n_pil_err += 1
    eoi = None
    if ext in ('.jpg', '.jpeg'):
        with open(src, 'rb') as f:
            f.seek(-2, os.SEEK_END)
            tail = f.read(2)
        eoi = (tail == b'\xff\xd9')
        if not eoi:
            n_eoi_missing += 1
    arr = cv2.imread(src, cv2.IMREAD_UNCHANGED)
    shape = arr.shape if arr is not None else None
    print('%s | PIL:%s | EOI:%s | cv2 shape:%s | size=%d'
          % (os.path.basename(src), pil_err, eoi, shape,
             os.path.getsize(src)))
print('sample: PIL errors %d/20, missing EOI %d/20' % (n_pil_err, n_eoi_missing))
