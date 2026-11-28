#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤3 步骤1：损坏文件复核验证（只读验证，不删、不写库）。
范围 A：待确认 3,487 行中的媒体（照片/视频），排除加密桶；
范围 B：171 个抽帧失败视频（midconf_video_frame_failed.tsv）。
逐个复核：在盘＋大小＋mtime＋sha256＋解码（图片 PIL 严格 load，失败再试 cv2；
视频 cv2 读一帧）。结论：pil-ok / cv2-ok（能读，跳过不删）/ dead（完全读不出，删除候选）。
排除：20200614_0001.mov、凭据文件（rq 138941/138943/139432）、加密文件。
输出 /data/pending_triage_verify.tsv 与 /data/pending_triage_verify_summary.json。
用法：docker exec -d nas-curator python3 /data/pending_triage_verify.py
"""
import csv, hashlib, json, os, sys, time

try:
    sys.path.insert(0, '/app')
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None

try:
    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()
    import cv2
    _HAVE_IMG = True
except Exception as e:
    print('IMG LIBS MISSING: %s' % e, flush=True)
    _HAVE_IMG = False

NONE_TSV = '/data/pending_triage_none.tsv'
FF_TSV = '/data/midconf_video_frame_failed.tsv'
MANIFEST = '/data/pending_triage_verify.tsv'
SUMMARY = '/data/pending_triage_verify_summary.json'
PROGRESS_DIR = '/data/progress'
HB_ID = 'pending-triage-verify'

IMG_EXTS = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp', '.gif',
            '.bmp', '.tif', '.tiff', '.jfif'}
VID_EXTS = {'.mov', '.mp4', '.m4v', '.3gp', '.avi', '.mkv', '.vob',
            '.wmv', '.flv', '.mpg', '.mts'}
EXCLUDE_RQ = {138941, 138943, 139432}


def _hb(**kw):
    if _write_hb is None:
        return
    try:
        _write_hb(HB_ID, progress_dir=PROGRESS_DIR, **kw)
    except Exception as e:
        print('heartbeat failed: %s' % e, flush=True)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def decode_image(path):
    try:
        with Image.open(path) as im:
            im.load()
        return 'pil-ok'
    except Exception as e:
        pil_err = str(e)[:120]
    try:
        arr = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if arr is not None and arr.size > 0:
            return 'cv2-ok'
    except Exception as e:
        return 'dead'
    return 'dead'


def decode_video(path):
    try:
        cap = cv2.VideoCapture(path)
        ok, frame = cap.read()
        cap.release()
        if ok and frame is not None and frame.size > 0:
            return 'cv2-ok'
    except Exception:
        pass
    return 'dead'


def main():
    t0 = time.time()
    items = []  # (scope, rq_id, src, size_list, mtime_list)
    with open(NONE_TSV, encoding='utf-8') as f:
        rd = csv.DictReader(f, delimiter='\t')
        for r in rd:
            if r['skind'] not in ('照片', '视频'):
                continue
            if r['bucket'] == '加密':
                continue
            if int(r['rq_id']) in EXCLUDE_RQ:
                continue
            if '20200614_0001.mov' in r['src']:
                continue
            items.append(('none', int(r['rq_id']), r['src'], r['size'], r['mtime']))
    with open(FF_TSV, encoding='utf-8') as f:
        rd = csv.DictReader(f, delimiter='\t')
        for r in rd:
            if '20200614_0001.mov' in r['src']:
                continue
            items.append(('frame_failed', int(r['rq_id']), r['src'], '', ''))
    print('verify scope total: %d (none-media=%d frame_failed=%d)' % (
        len(items), sum(1 for i in items if i[0] == 'none'),
        sum(1 for i in items if i[0] == 'frame_failed')), flush=True)
    _hb(status='running', total=len(items), done=0, phase='verify')

    cols = ['scope', 'rq_id', 'src', 'size_list', 'mtime_list', 'size_disk',
            'mtime_disk', 'sha256_disk', 'verdict', 'detail']
    n_dead = n_pil = n_cv2 = n_missing = n_size_chg = n_err = 0
    with open(MANIFEST, 'w', encoding='utf-8') as out:
        w = csv.DictWriter(out, fieldnames=cols, delimiter='\t')
        w.writeheader()
        for idx, (scope, rq_id, src, size_list, mtime_list) in enumerate(items, 1):
            verdict, detail = '', ''
            size_disk, mtime_disk, sha = '', '', ''
            try:
                st = os.stat(src)
                size_disk, mtime_disk = st.st_size, int(st.st_mtime)
            except FileNotFoundError:
                verdict, detail = 'missing', 'src-not-on-disk'
                n_missing += 1
            except Exception as e:
                verdict, detail = 'error', 'stat:%s' % str(e)[:100]
                n_err += 1
            if not verdict:
                if size_list and str(size_disk) != str(size_list):
                    detail = 'size-changed list=%s disk=%s' % (size_list, size_disk)
                    n_size_chg += 1
                try:
                    sha = sha256(src)
                except Exception as e:
                    verdict, detail = 'error', 'hash:%s' % str(e)[:100]
                    n_err += 1
            if not verdict:
                ext = os.path.splitext(src)[1].lower()
                try:
                    if ext in IMG_EXTS:
                        verdict = decode_image(src)
                    elif ext in VID_EXTS:
                        verdict = decode_video(src)
                    else:
                        verdict, detail = 'error', 'unsupported-ext:%s' % ext
                        n_err += 1
                except Exception as e:
                    verdict, detail = 'error', 'decode:%s' % str(e)[:100]
                    n_err += 1
            if verdict == 'pil-ok':
                n_pil += 1
            elif verdict == 'cv2-ok':
                n_cv2 += 1
            elif verdict == 'dead':
                n_dead += 1
            w.writerow({'scope': scope, 'rq_id': rq_id, 'src': src,
                        'size_list': size_list, 'mtime_list': mtime_list,
                        'size_disk': size_disk, 'mtime_disk': mtime_disk,
                        'sha256_disk': sha, 'verdict': verdict, 'detail': detail})
            if idx % 100 == 0:
                print('verify %d/%d dead=%d pil=%d cv2=%d missing=%d err=%d elapsed=%.0fs' % (
                    idx, len(items), n_dead, n_pil, n_cv2, n_missing, n_err,
                    time.time() - t0), flush=True)
                _hb(status='running', total=len(items), done=idx, phase='verify')
    summ = {'scope_total': len(items),
            'none_media': sum(1 for i in items if i[0] == 'none'),
            'frame_failed': sum(1 for i in items if i[0] == 'frame_failed'),
            'pil_ok': n_pil, 'cv2_ok': n_cv2, 'dead': n_dead,
            'missing': n_missing, 'size_changed': n_size_chg, 'error': n_err,
            'elapsed_sec': round(time.time() - t0, 1)}
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print('VERIFY DONE %s' % json.dumps(summ, ensure_ascii=False), flush=True)
    _hb(status='done', total=len(items), done=len(items), phase='verify')


if __name__ == '__main__':
    main()
