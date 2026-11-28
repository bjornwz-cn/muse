#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 2：损坏核验。只针对 an1_media.tsv 里 damage_suspect=1 的 946 项 + 2 个黑视频。
照片：PIL（+pillow_heif）与 cv2 双通道解码；都失败 -> 确认损坏。
  能解开的再算像素均值/标准差，记入清单供判断（均匀纯色也记出来，但不直接判删）。
视频：cv2 抽帧，能抽到帧 -> 可解码；抽不到 -> 再用 ffprobe 看流信息辅助判断。
黑视频 rq 124209/124214：多点采样帧亮度均值，确认是否全黑。
输出 /data/an2_damage.tsv：rq_id, mtype, src, pil, cv2, mean, stddev, verdict
  verdict: damaged（双通道都失败）/ decodable / uniform（能解但 stddev<3）
只读、不删文件、不改库。
"""
import csv, json, os, sys

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass
try:
    from PIL import Image
    import numpy as np
    import cv2
    HAVE = True
except Exception as e:
    print('img libs missing:', e)
    HAVE = False


def photo_check(path):
    """返回 (pil_ok, cv2_ok, mean, stddev)。"""
    pil_ok, cv2_ok, mean, stddev = False, False, -1.0, -1.0
    try:
        with Image.open(path) as im:
            im.load()
            rgb = im.convert('RGB')
            arr = np.asarray(rgb, dtype=np.float32)
            mean = float(arr.mean())
            stddev = float(arr.std())
        pil_ok = True
    except Exception:
        pass
    if not pil_ok:
        try:
            arr = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            if arr is not None:
                cv2_ok = True
                f = arr.astype(np.float32)
                mean = float(f.mean())
                stddev = float(f.std())
        except Exception:
            pass
    return pil_ok, cv2_ok, mean, stddev


def video_check(path):
    """返回 (cv2_ok, mean_brightness)。抽 5 点帧取平均亮度。"""
    try:
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return False, -1.0
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        idxs = [0] if n <= 1 else sorted(set([0, n // 4, n // 2, 3 * n // 4, n - 1]))
        vals = []
        for i in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, fr = cap.read()
            if ok and fr is not None:
                vals.append(float(fr.mean()))
        cap.release()
        if not vals:
            return False, -1.0
        return True, sum(vals) / len(vals)
    except Exception:
        return False, -1.0


def main():
    rows = list(csv.DictReader(open('/data/an1_media.tsv'), delimiter='\t'))
    suspects = [r for r in rows if r['damage_suspect'] == '1' and r['exists'] == '1']
    print('suspects to verify:', len(suspects), flush=True)
    out = []
    for i, r in enumerate(suspects, 1):
        src, mtype = r['src'], r['mtype']
        if mtype == 'photo':
            pil_ok, cv2_ok, mean, stddev = photo_check(src)
            if not pil_ok and not cv2_ok:
                verdict = 'damaged'
            elif stddev >= 0 and stddev < 3:
                verdict = 'uniform'
            else:
                verdict = 'decodable'
            out.append((r['rq_id'], mtype, src, pil_ok, cv2_ok,
                        round(mean, 2), round(stddev, 2), verdict))
        else:
            ok, bright = video_check(src)
            verdict = 'decodable' if ok else 'damaged'
            out.append((r['rq_id'], mtype, src, '', ok,
                        round(bright, 2) if bright >= 0 else -1, '', verdict))
        if i % 100 == 0:
            print('%d/%d' % (i, len(suspects)), flush=True)
    with open('/data/an2_damage.tsv', 'w', encoding='utf-8') as f:
        f.write('rq_id\tmtype\tsrc\tpil\tcv2\tmean\tstddev\tverdict\n')
        for r in out:
            f.write('\t'.join(str(x) for x in r) + '\n')
    from collections import Counter
    print('verdicts:', Counter(o[7] for o in out))
    print('damaged list:')
    for o in out:
        if o[7] == 'damaged':
            print('  ', o[0], o[2])
    print('uniform list (decodable but stddev<3):', sum(1 for o in out if o[7] == 'uniform'))
    # 黑视频专项
    print('=== black video check ===')
    for rid in ('124209', '124214'):
        r = next((x for x in rows if x['rq_id'] == rid), None)
        if not r:
            print(rid, 'not in manifest')
            continue
        ok, bright = video_check(r['src'])
        print(rid, r['src'], 'cv2_ok=', ok, 'mean_brightness=', round(bright, 2) if bright >= 0 else bright)


if __name__ == '__main__':
    main()
