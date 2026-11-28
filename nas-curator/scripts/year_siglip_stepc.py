#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Year-folder SigLIP reclassification (classify only, NO move).

对收件箱/媒体库/照片/<年份> 与 视频/<年份> 下的全部媒体文件跑内容分类
（SigLIP + ChineseCLIP，media_core/curate.py 的 Classifier + decide，
视频用 media_core/engine.py 的多帧投票 _classify_video_frames）。
只分类、不移动、不建 plan、不执行。

- 已有有效分类的行（真实类目且 confidence>=0.35）直接复用，不重跑。
- 其余文件跑 SigLIP；结果写进 review_queue：
  无行的用 DB.add_review 建行（kind='low_conf'），有行的更新 payload
  的 category/confidence/reason/suggested_dst（保留 vlm_* 与 kind）。
- 低置信图片（<0.85）不由本脚本跑 VLM，另用 vlm_reclassify.py --rq-ids 处理。
- 断点续跑：结果逐行写 /data/year_siglip_results.jsonl；重跑时跳过已有结果。

运行（NAS 容器内，需要 /app 与 /data 挂载）：
  docker exec nas-curator python3 /data/year_siglip.py --workers 4
"""
import sys, os, json, time, argparse, sqlite3
from multiprocessing import Pool

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

DB_PATH = "/data/nas-curator.db"
RESULTS = "/data/year_siglip_results_stepc.jsonl"
PROGRESS_DIR = "/data/progress"

# 0.2.0 心跳：把进度写进 /data/progress/year-reclassify.json 供任务页显示；
# 写失败绝不影响主流程（best-effort）。注意：已在跑的实例用的是
# /data 里旧版脚本，不会读到这段心跳——它由 API 的 derived 兜底显示。
try:
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None


def _hb(**kw):
    if _write_hb is None:
        return
    try:
        _write_hb("stepc-recheck", progress_dir=PROGRESS_DIR, **kw)
    except Exception as e:  # noqa: BLE001 - 心跳失败只打印，不中断分类
        print("heartbeat failed: %s" % e, flush=True)
PHOTO_ROOT = "/vol1/1000/收件箱/媒体库/照片"
VIDEO_ROOT = "/vol1/1000/收件箱/媒体库/视频"
IMG_EXTS = {'.jpg', '.jpeg', '.heic', '.png', '.heif', '.webp', '.gif', '.nef'}
VID_EXTS = {'.mov', '.mp4', '.mts', '.vob', '.avi', '.wmv'}
CONF_HIGH = 0.85
CONF_MIN = 0.35


MEDIA_KINDS = ('照片', '视频')


def collect_files():
    files = []
    for base, kind in ((PHOTO_ROOT, '照片'), (VIDEO_ROOT, '视频')):
        for root, dirs, fns in os.walk(base):
            for fn in fns:
                ext = os.path.splitext(fn)[1].lower()
                if (kind == '照片' and ext in IMG_EXTS) or \
                   (kind == '视频' and ext in VID_EXTS):
                    files.append((os.path.join(root, fn), kind))
    files.sort()
    return files


def load_existing(db_path):
    """src -> (row_id, status, kind, payload_dict)"""
    conn = sqlite3.connect(db_path, timeout=30)
    rows = conn.execute(
        "SELECT id, status, kind, payload FROM review_queue "
        "WHERE json_extract(payload,'$.src') LIKE '/vol1/1000/收件箱/%'"
    ).fetchall()
    conn.close()
    out = {}
    for rid, status, kind, payload in rows:
        try:
            d = json.loads(payload) if payload else {}
        except Exception:
            d = {}
        src = d.get('src')
        if src:
            # 同一 src 多行时保留第一条（本批实测无重复行）
            out.setdefault(src, (rid, status, kind, d))
    return out


def load_results(path):
    done = {}
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    done[d['src']] = d
                except Exception:
                    continue
    return done


def valid_existing(payload):
    """已有 payload 的分类是否可复用：真实类目且 confidence>=0.35。"""
    cat = payload.get('category')
    try:
        conf = float(payload.get('confidence') or 0)
    except Exception:
        conf = 0.0
    if not cat or cat in ('待确认', '模糊', '疑似重复'):
        return False
    return conf >= CONF_MIN


_worker_cls = None
_worker_cfg = None


def worker_init(nworkers=4):
    global _worker_cls, _worker_cfg
    # 限制每 worker 的 torch 线程数，避免 4 worker x 16 线程在 24 核上过度抢占。
    # 先设环境变量再 import torch，让 OpenMP/MKL 也受限。
    import os as _os
    _os.environ['OMP_NUM_THREADS'] = str(max(2, 8 // max(1, nworkers)))
    _os.environ['MKL_NUM_THREADS'] = str(max(2, 8 // max(1, nworkers)))
    from media_core.curate import Classifier
    _worker_cfg = json.load(open('/app/media_core/config.json'))
    _worker_cls = Classifier(_worker_cfg)
    try:
        import torch as _torch
        _torch.set_num_threads(max(2, 8 // max(1, nworkers)))
    except Exception:
        pass


def classify_one(item):
    path, kind = item
    from media_core.curate import decode_item_frames, decide, target_dir
    from media_core.engine import _classify_video_frames
    try:
        st = os.stat(path)
    except OSError:
        return {'src': path, 'kind': kind, 'category': '待确认', 'confidence': 0.0,
                'reason': 'gone', 'evidence': '', 'why': 'gone', 'suggested_dst': ''}
    row = {'path': path, 'name': os.path.basename(path), 'size': st.st_size,
           'mtime': st.st_mtime, 'kind': kind, 'ext': os.path.splitext(path)[1].lower()}
    try:
        frames, blur, white, colors, ar, note = decode_item_frames(row)
    except Exception as e:
        return {'src': path, 'kind': kind, 'category': '待确认', 'confidence': 0.0,
                'reason': 'decode_failed:%s' % type(e).__name__, 'evidence': '',
                'why': 'decode_failed', 'suggested_dst': ''}
    if note or not frames:
        return {'src': path, 'kind': kind, 'category': '待确认', 'confidence': 0.0,
                'reason': 'decode_failed:%s' % (note or 'noframe'), 'evidence': '',
                'why': 'decode_failed', 'suggested_dst': ''}
    if kind == '照片' and blur < _worker_cfg['thresholds']['blur_min']:
        return {'src': path, 'kind': kind, 'category': '模糊', 'confidence': 0.3,
                'reason': 'blurry:%.1f' % blur, 'evidence': '', 'why': 'blurry',
                'suggested_dst': ''}
    try:
        if kind == '视频':
            cat, cat2, s1, s2, why = _classify_video_frames(_worker_cls, frames, row, _worker_cfg)
        else:
            sc = _worker_cls.scores(frames[0], '照片')
            cat, cat2, s1, s2, why = decide(row, sc, blur, white, colors, ar, '', _worker_cfg)
    except Exception as e:
        return {'src': path, 'kind': kind, 'category': '待确认', 'confidence': 0.0,
                'reason': 'classify_failed:%s' % type(e).__name__, 'evidence': '',
                'why': 'classify_failed', 'suggested_dst': ''}
    if why == 'accepted':
        evidence = '%s %.2f / %s %.2f' % (cat, s1, cat2, s2)
        dst = target_dir(_worker_cfg, kind, cat)
        return {'src': path, 'kind': kind, 'category': cat, 'confidence': round(float(s1), 3),
                'reason': 'siglip:%.2f' % s1, 'evidence': evidence, 'why': 'accepted',
                'suggested_dst': dst}
    # 低置信：decide 把 top1 类名放在 cat2
    evidence = '%s %.2f' % (cat2, s1) if cat2 != '待确认' else ''
    margin = s1 - s2
    th = _worker_cfg['thresholds']
    if margin < th['margin_min']:
        reason = 'low:margin:%.2f' % margin
    else:
        reason = 'low:score:%.2f' % s1
    return {'src': path, 'kind': kind, 'category': '待确认',
            'confidence': round(min(float(s1), 0.45), 3),
            'reason': reason, 'evidence': evidence, 'why': 'low_confidence',
            'suggested_dst': '', 'top1': cat2, 'top1_score': round(float(s1), 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--files-from', default='',
                        help='batch snapshot TSV (path,size,mtime,kind); classify only these')
    ap.add_argument('--chunksize', type=int, default=8)
    args = ap.parse_args()
    t0 = time.time()

    if args.files_from:
        files = []
        with open(args.files_from) as _fh:
            for _line in _fh:
                _p = _line.rstrip('\n').split('\t')
                if len(_p) >= 4:
                    files.append((_p[0], _p[3]))
        files.sort()
        print('snapshot files: %d' % len(files), flush=True)
    else:
        files = collect_files()
    print('disk media files: %d (photo %d, video %d)' % (
        len(files), sum(1 for _, k in files if k == '照片'),
        sum(1 for _, k in files if k == '视频')), flush=True)
    existing = load_existing(DB_PATH)
    print('existing review rows under inbox/media: %d' % len(existing), flush=True)
    done = load_results(RESULTS)
    print('already classified in %s: %d' % (RESULTS, len(done)), flush=True)

    todo = []
    reused = 0
    non_media = 0
    for path, kind in files:
        if kind not in MEDIA_KINDS:
            non_media += 1
            continue
        if path in done:
            continue
        ent = existing.get(path)
        if ent is not None and valid_existing(ent[3]):
            reused += 1
            continue
        todo.append((path, kind))
    if args.limit:
        todo = todo[:args.limit]
    print('reuse existing classification: %d; to classify (SigLIP): %d; non-media skipped: %d' % (reused, len(todo), non_media), flush=True)
    if not todo:
        print('nothing to do', flush=True)
        return
    _hb(label='步骤 C 复检（收件箱已有 review 行）', kind='year-reclassify', phase='siglip',
        status='running', total=len(todo), done=0, skipped=reused)

    # 并行分类，结果逐批写 JSONL（断点续跑）
    n_done = 0
    with open(RESULTS, 'a') as fh:
        with Pool(args.workers, initializer=worker_init,
                  initargs=(args.workers,)) as pool:
            for res in pool.imap_unordered(classify_one, todo, chunksize=args.chunksize):
                fh.write(json.dumps(res, ensure_ascii=False) + '\n')
                fh.flush()
                n_done += 1
                if n_done % 200 == 0:
                    print('PROG %d/%d %.0fs' % (n_done, len(todo), time.time() - t0), flush=True)
                    _hb(label='步骤 C 复检（收件箱已有 review 行）', kind='year-reclassify',
                        phase='siglip', status='running', total=len(todo),
                        done=n_done, skipped=reused)
    print('DONE classified %d in %.1fs' % (n_done, time.time() - t0), flush=True)
    _hb(label='步骤 C 复检（收件箱已有 review 行）', kind='year-reclassify', phase='siglip',
        status='done', total=len(todo), done=n_done, skipped=reused)


if __name__ == '__main__':
    main()
