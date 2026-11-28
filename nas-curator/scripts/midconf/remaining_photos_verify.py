#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剩余照片损坏验证（只读验证，不删、不写库）。
范围：/data/remaining_photos_list.tsv 全部 3,639 张 pending 照片。
跳过：在 /data/pending_triage_verify.tsv 里记为 pil-ok 的行（以该表为准）。
判定（新规则：任何损坏就删，但须多重确认，不凭单次读取失败删）：
- PIL 严格 load 成功（LOAD_TRUNCATED_IMAGES=False）-> pil-ok，完好。
- PIL 失败 -> 试 cv2：cv2 解码成功 -> cv2-ok，完好（保留）。
- PIL 失败且 cv2 失败 -> 再试一次 PIL 严格 load（排除瞬时 I/O 故障）：
  仍失败 -> dead（确认损坏，删除候选）；成功 -> pil-ok-retry。
- 注意：HEIC/HEIF/CR2 等 cv2 不支持的格式，cv2 失败不作为损坏证据，
  判定以两次 PIL 为准；死结论要求 PIL 两次失败且 cv2 失败。
输出 /data/remaining_photos_verify.tsv 与 /data/remaining_photos_verify_summary.json。
用法：docker exec -d nas-curator python3 /data/remaining_photos_verify.py
"""
import csv, hashlib, json, os, sys, time

try:
    sys.path.insert(0, '/app')
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None

try:
    from PIL import Image, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    import pillow_heif
    pillow_heif.register_heif_opener()
    import cv2
    _HAVE_IMG = True
except Exception as e:
    print('IMG LIBS MISSING: %s' % e, flush=True)
    _HAVE_IMG = False

LIST_TSV = '/data/remaining_photos_list.tsv'
SKIP_TSV = '/data/pending_triage_verify.tsv'
MANIFEST = '/data/remaining_photos_verify.tsv'
SUMMARY = '/data/remaining_photos_verify_summary.json'
PROGRESS_DIR = '/data/progress'
HB_ID = 'remaining-photos-verify'


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


def pil_strict(path):
    """严格 load；返回 (ok, err_str)。"""
    try:
        with Image.open(path) as im:
            im.load()
        return True, ''
    except Exception as e:
        return False, '%s: %s' % (type(e).__name__, str(e)[:150])


def cv2_decode(path):
    try:
        arr = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if arr is not None and arr.size > 0:
            return True, ''
        return False, 'imread-none-or-empty'
    except Exception as e:
        return False, '%s: %s' % (type(e).__name__, str(e)[:120])


def decode_image(path):
    ok, pil_err = pil_strict(path)
    if ok:
        return 'pil-ok', ''
    ok2, cv2_err = cv2_decode(path)
    if ok2:
        return 'cv2-ok', 'pil-fail[%s] cv2-ok' % pil_err
    ok3, pil_err2 = pil_strict(path)
    if ok3:
        return 'pil-ok-retry', 'first-pil-fail[%s] retry-ok' % pil_err
    return 'dead', 'pil-fail[%s] cv2-fail[%s] pil-retry-fail[%s]' % (pil_err, cv2_err, pil_err2)


def main():
    t0 = time.time()
    skip = set()
    with open(SKIP_TSV, encoding='utf-8') as f:
        rd = csv.DictReader(f, delimiter='\t')
        for r in rd:
            if r['verdict'] == 'pil-ok':
                skip.add(int(r['rq_id']))
    items = []
    with open(LIST_TSV, encoding='utf-8') as f:
        rd = csv.DictReader(f, delimiter='\t')
        for r in rd:
            items.append((int(r['rq_id']), r['src']))
    print('verify scope: list=%d skip-pil-ok-in-scope=%d to-verify=%d' % (
        len(items), len([i for i in items if i[0] in skip]),
        len([i for i in items if i[0] not in skip])), flush=True)
    _hb(status='running', total=len(items), done=0, phase='verify')

    cols = ['rq_id', 'src', 'size_disk', 'mtime_disk', 'sha256_disk',
            'verdict', 'detail']
    n = {'pil-ok': 0, 'pil-ok-retry': 0, 'cv2-ok': 0, 'dead': 0,
         'missing': 0, 'error': 0, 'skipped': 0}
    with open(MANIFEST, 'w', encoding='utf-8') as out:
        w = csv.DictWriter(out, fieldnames=cols, delimiter='\t')
        w.writeheader()
        for idx, (rq_id, src) in enumerate(items, 1):
            if rq_id in skip:
                n['skipped'] += 1
                w.writerow({'rq_id': rq_id, 'src': src, 'size_disk': '',
                            'mtime_disk': '', 'sha256_disk': '',
                            'verdict': 'skipped-pil-ok', 'detail': 'triage-verify-pil-ok'})
                continue
            verdict, detail = '', ''
            size_disk, mtime_disk, sha = '', '', ''
            try:
                st = os.stat(src)
                size_disk, mtime_disk = st.st_size, int(st.st_mtime)
            except FileNotFoundError:
                verdict, detail = 'missing', 'src-not-on-disk'
                n['missing'] += 1
            except Exception as e:
                verdict, detail = 'error', 'stat:%s' % str(e)[:100]
                n['error'] += 1
            if not verdict:
                try:
                    sha = sha256(src)
                except Exception as e:
                    verdict, detail = 'error', 'hash:%s' % str(e)[:100]
                    n['error'] += 1
            if not verdict:
                try:
                    verdict, detail = decode_image(src)
                    n[verdict] = n.get(verdict, 0) + 1
                except Exception as e:
                    verdict, detail = 'error', 'decode:%s' % str(e)[:100]
                    n['error'] += 1
            w.writerow({'rq_id': rq_id, 'src': src, 'size_disk': size_disk,
                        'mtime_disk': mtime_disk, 'sha256_disk': sha,
                        'verdict': verdict, 'detail': detail})
            if idx % 200 == 0:
                print('verify %d/%d %s elapsed=%.0fs' % (
                    idx, len(items), json.dumps(n, ensure_ascii=False),
                    time.time() - t0), flush=True)
                _hb(status='running', total=len(items), done=idx, phase='verify')
    summ = dict(n)
    summ.update({'scope_total': len(items), 'elapsed_sec': round(time.time() - t0, 1)})
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print('VERIFY DONE %s' % json.dumps(summ, ensure_ascii=False), flush=True)
    _hb(status='done', total=len(items), done=len(items), phase='verify')


if __name__ == '__main__':
    main()
