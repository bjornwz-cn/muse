#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796 任务 步骤 1：文件头扫描（容器内，只读）。
对 /data/r4796_videos.tsv 与 /data/r4796_others.tsv 的全部行：
读前 32 字节，判定是否 __CLOUDSYNC_ENC__（前 16 字节精确相等），
记录 magic 猜测与视频帧是否已存在（/data/remaining_video_frames/rq_<id>.jpg）。
不写库、不删文件。
输出 /data/r4796_header_scan.tsv、/data/r4796_header_scan_summary.json。
用法：docker exec nas-curator python3 /data/r4796_header_scan.py
"""
import csv, json, os

ENC = b'__CLOUDSYNC_ENC__'
FRAMES = '/data/remaining_video_frames'
OUT_TSV = '/data/r4796_header_scan.tsv'
OUT_JSON = '/data/r4796_header_scan_summary.json'


def magic_guess(b):
    if b.startswith(ENC):
        return 'CLOUDSYNC_ENC'
    if b.startswith(b'PK\x03\x04'):
        return 'zip'
    if b.startswith(b'%PDF'):
        return 'pdf'
    if b.startswith(b'\xff\xd8\xff'):
        return 'jpeg'
    if b.startswith(b'\x89PNG'):
        return 'png'
    if b.startswith(b'ftyp'):
        return 'mp4-ftyp?'
    if len(b) >= 8 and b[4:8] == b'ftyp':
        return 'mp4'
    if b.startswith(b'Rar!'):
        return 'rar'
    if b.startswith(b'MZ'):
        return 'exe-mz'
    if b.startswith(b'II*\x00') or b.startswith(b'MM\x00*'):
        return 'tiff/cr2'
    if b.startswith(b'\x00\x00\x00'):
        return 'zero-head'
    try:
        b.decode('utf-8')
        return 'text?'
    except Exception:
        return 'binary?'


def clean(s):
    return (s or '').replace('\t', ' ').replace('\n', ' ').replace('\r', ' ')


def main():
    rows = []
    for path in ('/data/r4796_videos.tsv', '/data/r4796_others.tsv'):
        with open(path, encoding='utf-8') as f:
            rd = csv.DictReader(f, delimiter='\t')
            for r in rd:
                r['_scope'] = 'video' if 'videos' in path else 'other'
                rows.append(r)
    print('scope rows: %d' % len(rows), flush=True)
    out = []
    for r in rows:
        rid = int(r['rq_id'])
        src = r['src']
        rec = {'rq_id': rid, 'scope': r['_scope'], 'src': src, 'ext': r['ext'],
               'exists': False, 'size_disk': '', 'head16_hex': '',
               'head16_ascii': '', 'is_enc': 0, 'magic': '',
               'frame_exists': int(os.path.exists(os.path.join(FRAMES, 'rq_%d.jpg' % rid)))}
        try:
            st = os.stat(src)
            rec['exists'] = True
            rec['size_disk'] = st.st_size
            with open(src, 'rb') as fh:
                head = fh.read(32)
            rec['head16_hex'] = head[:17].hex()
            rec['head16_ascii'] = ''.join(chr(c) if 32 <= c < 127 else '.' for c in head[:17])
            rec['is_enc'] = int(head.startswith(ENC))
            rec['magic'] = magic_guess(head)
        except FileNotFoundError:
            rec['magic'] = 'missing'
        except Exception as e:
            rec['magic'] = 'read_error:%s' % type(e).__name__
        out.append(rec)
    with open(OUT_TSV, 'w', encoding='utf-8') as f:
        f.write('rq_id\tscope\tsrc\text\texists\tsize_disk\thead16_hex\thead16_ascii\tis_enc\tmagic\tframe_exists\n')
        for r in out:
            f.write('%d\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%d\t%s\t%d\n' % (
                r['rq_id'], r['scope'], clean(r['src']), r['ext'],
                int(r['exists']), r['size_disk'], r['head16_hex'],
                clean(r['head16_ascii']), r['is_enc'], clean(r['magic']),
                r['frame_exists']))
    enc = [r for r in out if r['is_enc']]
    summ = {'n': len(out),
            'exists': sum(1 for r in out if r['exists']),
            'missing': [r['rq_id'] for r in out if not r['exists']],
            'enc_n': len(enc),
            'enc_by_scope': {s: sum(1 for r in enc if r['scope'] == s) for s in ('video', 'other')},
            'enc_by_ext': {e: sum(1 for r in enc if r['ext'] == e)
                           for e in sorted(set(r['ext'] for r in enc))},
            'enc_ids': [r['rq_id'] for r in enc],
            'enc_bytes': sum(int(r['size_disk']) for r in enc if r['size_disk'] != ''),
            'video_frame_exists': sum(1 for r in out if r['scope'] == 'video' and r['frame_exists']),
            'video_frame_missing': [r['rq_id'] for r in out if r['scope'] == 'video' and not r['frame_exists']],
            'magic_counts': {m: sum(1 for r in out if r['magic'] == m)
                             for m in sorted(set(r['magic'] for r in out))}}
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print('SUMMARY ' + json.dumps(summ, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
