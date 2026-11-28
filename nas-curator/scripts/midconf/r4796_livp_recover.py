#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796：恢复 CRC 失败的 2 个 .livp 静帧（容器内，只读源文件）。
51249/139388 的静帧内容完好、仅 CRC 字段错：按中央目录偏移
直接读 stored 数据，PIL 校验后存 /data/remaining_video_frames/rq_<id>.jpg。
同时输出 livp VLM 清单 /data/r4796_livp_vlm.tsv（47 行：45 正常＋2 恢复；
51248 损坏不进 VLM）。
用法：docker exec nas-curator python3 /data/r4796_livp_recover.py
"""
import csv, io, json, os, sqlite3, struct, zipfile

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass

from PIL import Image

DB = '/data/nas-curator.db'
STILLS = '/data/r4796_livp_stills.tsv'
OUT = '/data/r4796_livp_vlm.tsv'
FRAMES = '/data/remaining_video_frames'
RECOVER = [51249, 139388]


def main():
    db = sqlite3.connect(DB)
    ok_ids = []
    with open(STILLS, encoding='utf-8') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            if r['status'] == 'ok':
                ok_ids.append(int(r['rq_id']))
    print('stills ok: %d' % len(ok_ids))
    for rid in RECOVER:
        src = json.loads(db.execute(
            "SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()[0])['src']
        z = zipfile.ZipFile(src)
        stills = [i for i in z.infolist() if not i.filename.lower().endswith('.mov')]
        info = stills[0]
        with open(src, 'rb') as fh:
            data = fh.read()
        lho = info.header_offset
        assert data[lho:lho + 4] == b'PK\x03\x04', 'header not PK'
        (ver, flag, m2, mt, md, c2, cs2, us2, nlen, elen) = struct.unpack(
            '<HHHHHIIIHH', data[lho + 4:lho + 30])
        dstart = lho + 30 + nlen + elen
        raw = data[dstart:dstart + info.compress_size]
        img = Image.open(io.BytesIO(raw))
        img.verify()
        img = Image.open(io.BytesIO(raw)).convert('RGB')
        dst = os.path.join(FRAMES, 'rq_%d.jpg' % rid)
        img.save(dst, 'JPEG', quality=85)
        print('recovered rq=%d still=%s %d bytes -> %s (%d bytes)' % (
            rid, info.filename, len(raw), dst, os.path.getsize(dst)))
        ok_ids.append(rid)
    ok_ids = sorted(set(ok_ids))
    print('livp vlm total: %d' % len(ok_ids))
    rows = []
    for rid in ok_ids:
        prow = db.execute("SELECT status, payload FROM review_queue WHERE id=?", (rid,)).fetchone()
        assert prow and prow[0] == 'pending', (rid, prow[0] if prow else None)
        d = json.loads(prow[1])
        rows.append({'rq_id': rid, 'src': d.get('src', ''), 'size': d.get('size'),
                     'mtime': d.get('mtime'), 'has_vlm': 'vlm_category' in d,
                     'old_vlm_conf': d.get('vlm_confidence'),
                     'old_vlm_model': d.get('vlm_model')})
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write('rq_id\tsrc\tsize\tmtime\thas_vlm\told_vlm_conf\told_vlm_model\n')
        for r in rows:
            f.write('%d\t%s\t%s\t%s\t%s\t%s\t%s\n' % (
                r['rq_id'], r['src'].replace('\t', ' '), r['size'], r['mtime'],
                int(r['has_vlm']), r['old_vlm_conf'], r['old_vlm_model']))
    print('wrote', OUT)


if __name__ == '__main__':
    main()
