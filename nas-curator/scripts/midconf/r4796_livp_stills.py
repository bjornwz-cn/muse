#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796 任务 步骤 4B：.livp 静帧提取（容器内，只读源文件）。
对 /data/r4796_others.tsv 中 .livp 行（仅仍 pending 的行）：
解开 zip，取静帧条目（非 .mov 的那个；.heic 用 pillow_heif 转 JPEG，
.jpeg/.jpg 直接取），存 /data/remaining_video_frames/rq_<id>.jpg，
供 VLM 看帧脚本复用。只写帧文件，不写库。
输出 /data/r4796_livp_stills.tsv、/data/r4796_livp_stills_summary.json。
用法：docker exec nas-curator python3 /data/r4796_livp_stills.py
"""
import csv, io, json, os, sqlite3, zipfile

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HAVE_HEIF = True
except Exception:
    HAVE_HEIF = False

from PIL import Image

LIST = '/data/r4796_others.tsv'
FRAMES = '/data/remaining_video_frames'
OUT_TSV = '/data/r4796_livp_stills.tsv'
OUT_JSON = '/data/r4796_livp_stills_summary.json'
DB = '/data/nas-curator.db'


def clean(s):
    return (s or '').replace('\t', ' ').replace('\n', ' ').replace('\r', ' ')


def main():
    print('pillow_heif:', HAVE_HEIF, flush=True)
    db = sqlite3.connect(DB)
    rows = []
    with open(LIST, encoding='utf-8') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            if r['ext'] == '.livp':
                rows.append(r)
    print('livp rows: %d' % len(rows), flush=True)
    os.makedirs(FRAMES, exist_ok=True)
    out = []
    for r in rows:
        rid = int(r['rq_id'])
        src = r['src']
        rec = {'rq_id': rid, 'src': src, 'status': '', 'still_name': '',
               'still_size': '', 'frame': '', 'frame_bytes': '', 'error': ''}
        prow = db.execute("SELECT status FROM review_queue WHERE id=?", (rid,)).fetchone()
        if not prow or prow[0] != 'pending':
            rec.update(status='skip-not-pending', error='status=%s' % (prow[0] if prow else 'gone'))
            out.append(rec)
            continue
        try:
            z = zipfile.ZipFile(src)
        except Exception as e:
            rec.update(status='zip_failed', error='%s: %s' % (type(e).__name__, str(e)[:100]))
            out.append(rec)
            continue
        names = z.namelist()
        stills = [n for n in names if not n.lower().endswith('.mov')]
        if not stills:
            rec.update(status='no_still', error='entries=%s' % names)
            out.append(rec)
            continue
        name = stills[0]
        rec['still_name'] = name
        try:
            raw = z.read(name)
        except Exception as e:
            rec.update(status='read_failed', error='%s: %s' % (type(e).__name__, str(e)[:100]))
            out.append(rec)
            continue
        rec['still_size'] = len(raw)
        dst = os.path.join(FRAMES, 'rq_%d.jpg' % rid)
        try:
            low = name.lower()
            if low.endswith(('.heic', '.heif')):
                if not HAVE_HEIF:
                    rec.update(status='heic_no_decoder', error='pillow_heif missing')
                    out.append(rec)
                    continue
                img = Image.open(io.BytesIO(raw))
                img = img.convert('RGB')
                img.save(dst, 'JPEG', quality=85)
            elif low.endswith(('.jpeg', '.jpg')):
                if raw[:3] != b'\xff\xd8\xff':
                    rec.update(status='bad_jpeg', error='not jpeg magic')
                    out.append(rec)
                    continue
                with open(dst, 'wb') as fh:
                    fh.write(raw)
            else:
                img = Image.open(io.BytesIO(raw))
                img = img.convert('RGB')
                img.save(dst, 'JPEG', quality=85)
            chk = Image.open(dst)
            chk.verify()
            sz = os.path.getsize(dst)
            if sz < 500:
                rec.update(status='frame_too_small', error='%d bytes' % sz)
            else:
                rec.update(status='ok', frame=dst, frame_bytes=sz)
        except Exception as e:
            rec.update(status='convert_failed', error='%s: %s' % (type(e).__name__, str(e)[:120]))
        out.append(rec)
    with open(OUT_TSV, 'w', encoding='utf-8') as f:
        f.write('rq_id\tsrc\tstatus\tstill_name\tstill_size\tframe\tframe_bytes\terror\n')
        for r in out:
            f.write('%d\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' % (
                r['rq_id'], clean(r['src']), r['status'], clean(r['still_name']),
                r['still_size'], clean(r['frame']), r['frame_bytes'], clean(r['error'])))
    ok = [r for r in out if r['status'] == 'ok']
    summ = {'n': len(out), 'ok': len(ok),
            'by_status': {s: sum(1 for r in out if r['status'] == s)
                          for s in sorted(set(r['status'] for r in out))},
            'ok_ids': [r['rq_id'] for r in ok],
            'failed': [{'rq_id': r['rq_id'], 'status': r['status'], 'error': r['error']}
                       for r in out if r['status'] != 'ok']}
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print('SUMMARY ' + json.dumps({k: v for k, v in summ.items() if k != 'ok_ids'}, ensure_ascii=False))
    print('ok ids: %d' % len(summ['ok_ids']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
