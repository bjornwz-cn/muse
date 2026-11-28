#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剩余视频任务 步骤 1B（容器内，有 cv2；无 ffmpeg）：
读 /data/remaining_verify_ffmpeg.tsv，cv2 交叉验证：
- ff damaged：cv2 全量读帧计数；cv2 计数 < 总帧数（或打不开、读出 0 帧）
  → 确认损坏；cv2 读出全部帧 → conflict（不删，留复核）。
- ff good：cv2 抽读首/中/末 3 帧，至少 1 帧读出 → 确认完好；
  3 帧全读不出 → conflict（留复核，待宿主机 ffmpeg 二次复核）。
- ff missing / novideo / probe_failed：cv2 试开，记 cv2 状态，不删。
输出 /data/remaining_verify_cv2.tsv、
/data/remaining_verify_final.tsv（加 final_verdict 列）与
/data/remaining_verify_final_summary.json。
不写库、不删文件。
用法：docker exec nas-curator python3 /data/remaining_verify_cv2.py [--workers 4]
"""
import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, '/app')
os.environ.setdefault('CURATOR_CONFIG_DIR', '/data/config')
import cv2

FF_TSV = '/data/remaining_verify_ffmpeg.tsv'
OUT_TSV = '/data/remaining_verify_cv2.tsv'
FINAL_TSV = '/data/remaining_verify_final.tsv'
OUT_JSON = '/data/remaining_verify_final_summary.json'
PROGRESS_DIR = '/data/progress'
HB_ID = 'remaining-videos-verify-cv2'

try:
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None


def _hb(**kw):
    if _write_hb is None:
        return
    try:
        _write_hb(HB_ID, progress_dir=PROGRESS_DIR, **kw)
    except Exception as e:
        print('heartbeat failed: %s' % e, flush=True)


def cv2_count_all(src, timeout_s=600):
    """全量读帧计数；返回 (count, opened, err)。超时则返回已计帧数+timeout 标记。"""
    t0 = time.time()
    try:
        cap = cv2.VideoCapture(src)
    except Exception as e:
        return 0, False, 'open:%s' % str(e)[:100]
    if not cap.isOpened():
        return 0, False, 'not-opened'
    n = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            n += 1
            if n % 2000 == 0 and time.time() - t0 > timeout_s:
                cap.release()
                return n, True, 'timeout-partial-count-%d' % n
    except Exception as e:
        cap.release()
        return n, True, 'read:%s' % str(e)[:100]
    cap.release()
    return n, True, ''


def cv2_sample(src, total):
    """抽读首/中/末 3 帧；返回 (n_ok, positions_ok, err)。"""
    try:
        cap = cv2.VideoCapture(src)
    except Exception as e:
        return 0, [], 'open:%s' % str(e)[:100]
    if not cap.isOpened():
        return 0, [], 'not-opened'
    pos = sorted(set([0, max(0, (total or 1) // 2), max(0, (total or 1) - 1)]))
    ok_pos = []
    try:
        for p in pos:
            try:
                cap.set(cv2.CAP_PROP_POS_FRAMES, p)
            except Exception:
                pass
            ok, frame = cap.read()
            if ok and frame is not None and frame.size > 0:
                ok_pos.append(p)
    except Exception as e:
        cap.release()
        return len(ok_pos), ok_pos, 'read:%s' % str(e)[:100]
    cap.release()
    return len(ok_pos), ok_pos, ''


def _int_or_none(v):
    if v in ('', None, 'None', 'N/A'):
        return None
    try:
        return int(v)
    except Exception:
        return None


def one(r):
    rid = int(r['rq_id'])
    src = r['src']
    ffv = r['ff_verdict']
    total = _int_or_none(r.get('total_frames'))
    t0 = time.time()
    rec = {'rq_id': rid, 'src': src, 'ff_verdict': ffv,
           'total_frames': total if total is not None else '',
           'decoded_frames': r.get('decoded_frames', '')}
    if ffv == 'missing':
        rec.update({'cv2_verdict': 'missing', 'cv2_frames': '',
                    'final_verdict': 'missing', 'detail': 'src not on disk'})
    elif ffv == 'damaged':
        n, opened, err = cv2_count_all(src)
        rec.update({'cv2_frames': n, 'cv2_opened': int(opened),
                    'cv2_err': err})
        if not opened or n == 0:
            rec.update({'cv2_verdict': 'dead',
                        'final_verdict': 'damaged',
                        'detail': 'cv2 cannot open/read any frame; ffmpeg decoded %s/%s' % (
                            r.get('decoded_frames'), total)})
        elif total is not None and n < total:
            rec.update({'cv2_verdict': 'partial',
                        'final_verdict': 'damaged',
                        'detail': 'cv2 read %d/%d frames; ffmpeg decoded %s/%s' % (
                            n, total, r.get('decoded_frames'), total)})
        else:
            rec.update({'cv2_verdict': 'full',
                        'final_verdict': 'conflict',
                        'detail': 'CONFLICT ffmpeg partial (%s/%s) but cv2 read all %d frames; NOT deleted' % (
                            r.get('decoded_frames'), total, n)})
    elif ffv == 'good':
        n_ok, ok_pos, err = cv2_sample(src, total)
        rec.update({'cv2_frames': n_ok, 'cv2_err': err,
                    'cv2_sample_ok_pos': ','.join(str(p) for p in ok_pos)})
        if n_ok >= 1:
            rec.update({'cv2_verdict': 'ok',
                        'final_verdict': 'good',
                        'detail': 'cv2 sample %d/3 frames ok' % n_ok})
        else:
            rec.update({'cv2_verdict': 'unreadable',
                        'final_verdict': 'conflict',
                        'detail': 'CONFLICT ffmpeg full decode ok but cv2 reads 0 sample frames; needs ffmpeg recheck, NOT deleted'})
    else:  # novideo / probe_failed
        try:
            cap = cv2.VideoCapture(src)
            opened = cap.isOpened()
            cap.release()
        except Exception as e:
            opened = False
        rec.update({'cv2_frames': '', 'cv2_opened': int(opened),
                    'cv2_verdict': 'opened' if opened else 'dead',
                    'final_verdict': 'conflict',
                    'detail': 'ff %s; cv2 opened=%s; manual review, NOT deleted' % (ffv, opened)})
    rec['latency_s'] = round(time.time() - t0, 2)
    return rec


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--workers', type=int, default=4)
    args = ap.parse_args()
    rows = []
    with open(FF_TSV, encoding='utf-8') as f:
        hdr = f.readline().rstrip('\n').split('\t')
        idx = {c: i for i, c in enumerate(hdr)}
        for line in f:
            p = line.rstrip('\n').split('\t')
            rows.append({c: (p[idx[c]] if idx[c] < len(p) else '') for c in hdr})
    print('cv2 scope: %d rows, workers=%d' % (len(rows), args.workers), flush=True)
    _hb(status='running', total=len(rows), done=0, phase='cv2-crosscheck')
    t0 = time.time()
    results, done = [], 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(one, r): r['rq_id'] for r in rows}
        for fut in as_completed(futs):
            rec = fut.result()
            results.append(rec)
            done += 1
            if done % 50 == 0:
                print('... %d/%d %.0fs' % (done, len(rows), time.time() - t0), flush=True)
                _hb(status='running', total=len(rows), done=done, phase='cv2-crosscheck')
    results.sort(key=lambda r: r['rq_id'])
    cols = ['rq_id', 'src', 'ff_verdict', 'total_frames', 'decoded_frames',
            'cv2_verdict', 'cv2_frames', 'cv2_opened', 'cv2_sample_ok_pos',
            'final_verdict', 'detail', 'cv2_err', 'latency_s']
    with open(OUT_TSV, 'w', encoding='utf-8') as f:
        f.write('\t'.join(cols) + '\n')
        for r in results:
            f.write('\t'.join(str(r.get(c, '')) for c in cols) + '\n')
    # final 合并表：沿用 ffmpeg 清单列 + final_verdict/cv2 列
    with open(FINAL_TSV, 'w', encoding='utf-8') as f:
        f.write('\t'.join(cols) + '\n')
        for r in results:
            f.write('\t'.join(str(r.get(c, '')) for c in cols) + '\n')
    counts = {}
    for r in results:
        counts[r['final_verdict']] = counts.get(r['final_verdict'], 0) + 1
    ff_counts = {}
    for r in results:
        ff_counts[r['ff_verdict']] = ff_counts.get(r['ff_verdict'], 0) + 1
    summ = {'n': len(results), 'ff_counts': ff_counts, 'final_counts': counts,
            'conflicts': [r['rq_id'] for r in results if r['final_verdict'] == 'conflict'],
            'missing': [r['rq_id'] for r in results if r['final_verdict'] == 'missing'],
            'total_s': round(time.time() - t0, 1)}
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print('VERIFY-CV2 DONE ' + json.dumps(summ, ensure_ascii=False), flush=True)
    _hb(status='done', total=len(rows), done=len(rows), phase='cv2-crosscheck')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
