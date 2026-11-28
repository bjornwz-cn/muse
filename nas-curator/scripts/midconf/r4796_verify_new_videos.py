#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796 任务：新出现视频全量解码验证（NAS 宿主机侧，容器内无 ffmpeg）。
对清单（/data/r4796_new_videos.tsv，16 个上次 444 行之外的新 pending 视频），
ffprobe 取总帧数，ffmpeg 全量解码到 null 取解出帧数；
解出 < 总数或 ffmpeg 报错 -> damaged；全部解出 -> good。
只读源文件、不写库。输出 /data/r4796_new_videos_verify.tsv、
/data/r4796_new_videos_verify_summary.json。
用法（宿主机）：python3 /data/r4796_verify_new_videos.py [--workers 4]
"""
import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

DATA = "/vol2/@appshare/nas-curator/data"
LIST = os.path.join(DATA, "r4796_new_videos.tsv")
OUT_TSV = os.path.join(DATA, "r4796_new_videos_verify.tsv")
OUT_JSON = os.path.join(DATA, "r4796_new_videos_verify_summary.json")


def clean(s, n=200):
    s = (s or '').replace('\t', ' ').replace('\n', ' ').replace('\r', ' ')
    return s[:n]


def total_frames(src):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=nb_frames",
                        "-of", "csv=p=0", src],
                       capture_output=True, text=True, timeout=120)
    if r.returncode == 0:
        s = (r.stdout or '').strip().strip(',').strip()
        if s.isdigit():
            return int(s), None
    r2 = subprocess.run(["ffprobe", "-v", "error", "-count_frames",
                         "-select_streams", "v:0",
                         "-show_entries", "stream=nb_read_frames",
                         "-of", "csv=p=0", src],
                        capture_output=True, text=True, timeout=600)
    if r2.returncode == 0:
        s = (r2.stdout or '').strip().strip(',').strip()
        if s.isdigit():
            return int(s), None
    return None, ((r.stderr or '') + (r2.stderr or '')).strip()[:200]


def decode_count(src):
    r = subprocess.run(["ffmpeg", "-nostdin", "-threads", "2", "-v", "info",
                        "-i", src, "-f", "null", "-"],
                       capture_output=True, text=True, timeout=900)
    frames = None
    for line in (r.stderr or '').splitlines():
        if 'frame=' in line:
            try:
                frames = int(line.split('frame=')[1].split()[0])
            except Exception:
                pass
    return frames, r.returncode, (r.stderr or '')[-300:]


def one(row):
    rid, src = row
    t0 = time.time()
    rec = {'rq_id': rid, 'src': src, 'verdict': '', 'total_frames': '',
           'decoded_frames': '', 'ff_rc': '', 'detail': ''}
    if not os.path.exists(src):
        rec.update(verdict='missing', detail='src not on disk')
        return rec
    total, err = total_frames(src)
    if total is None:
        rec.update(verdict='probe_failed', detail=clean(err, 200))
        return rec
    dec, rc, errtail = decode_count(src)
    rec['total_frames'] = total
    rec['decoded_frames'] = dec if dec is not None else ''
    rec['ff_rc'] = rc
    if dec is None:
        rec.update(verdict='damaged', detail='ffmpeg decoded none; ' + clean(errtail, 150))
    elif dec < total:
        rec.update(verdict='damaged', detail='decoded %d/%d rc=%d' % (dec, total, rc))
    else:
        rec.update(verdict='good', detail='decoded %d/%d rc=%d' % (dec, total, rc))
    rec['latency_s'] = round(time.time() - t0, 1)
    return rec


def main():
    workers = int(sys.argv[sys.argv.index('--workers') + 1]) if '--workers' in sys.argv else 4
    rows = []
    with open(LIST, encoding='utf-8') as fh:
        hdr = fh.readline().rstrip('\n').split('\t')
        idx = {c: i for i, c in enumerate(hdr)}
        for line in fh:
            p = line.rstrip('\n').split('\t')
            if len(p) >= 2 and p[idx['rq_id']].isdigit():
                rows.append((int(p[idx['rq_id']]), p[idx['src']]))
    print('videos to verify: %d' % len(rows), flush=True)
    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, row): row[0] for row in rows}
        for fut in as_completed(futs):
            rec = fut.result()
            results.append(rec)
            print('%s rq=%d %s %s' % (rec['verdict'], rec['rq_id'],
                                      rec.get('detail', '')[:100], rec.get('src', '')[-40:]), flush=True)
    results.sort(key=lambda r: r['rq_id'])
    with open(OUT_TSV, 'w', encoding='utf-8') as f:
        f.write('rq_id\tsrc\tverdict\ttotal_frames\tdecoded_frames\tff_rc\tdetail\n')
        for r in results:
            f.write('%d\t%s\t%s\t%s\t%s\t%s\t%s\n' % (
                r['rq_id'], clean(r['src']), r['verdict'], r['total_frames'],
                r['decoded_frames'], r['ff_rc'], clean(r.get('detail', ''), 200)))
    from collections import Counter
    summ = {'n': len(results), 'verdicts': dict(Counter(r['verdict'] for r in results)),
            'total_s': round(time.time() - t0, 1),
            'damaged_ids': [r['rq_id'] for r in results if r['verdict'] == 'damaged'],
            'good_ids': [r['rq_id'] for r in results if r['verdict'] == 'good']}
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print('SUMMARY ' + json.dumps(summ, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    main()
