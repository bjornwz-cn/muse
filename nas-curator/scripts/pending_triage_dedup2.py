#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤3 步骤2b：去重再分析（只读，不删、不写库）。
修正 2a 的方法缺陷（files 表路径已过期，#107 实测结论）：
改用媒体库 basename 索引找保留份（同名 → 磁盘 sha256 比对 → 解码验证），
保留份只认 /vol1/1000/媒体库/ 下的文件；收件箱内同伴按哈希分组、每组只留一份。
对象：445 个 kind=dup 待确认行（2a 已判 delete 的 17 行也重算，以本次为准）。
输出 /data/pending_triage_dedup2.tsv 与 /data/pending_triage_dedup2.json。
"""
import csv, hashlib, json, os, sqlite3, subprocess, sys
from collections import Counter, defaultdict

try:
    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()
    import cv2
    _HAVE_IMG = True
except Exception:
    _HAVE_IMG = False

DB = '/data/nas-curator.db'
NONE_TSV = '/data/pending_triage_none.tsv'
VERIFY_TSV = '/data/pending_triage_verify.tsv'
OUT_TSV = '/data/pending_triage_dedup2.tsv'
OUT_JSON = '/data/pending_triage_dedup2.json'
LIB_PREFIX = '/vol1/1000/媒体库/'
IMG_EXTS = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp', '.gif',
            '.bmp', '.tif', '.tiff', '.jfif'}
VID_EXTS = {'.mov', '.mp4', '.m4v', '.3gp', '.avi', '.mkv', '.vob',
            '.wmv', '.flv', '.mpg', '.mts'}


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def doc_usable(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(8)
        ext = os.path.splitext(path)[1].lower()
        if ext in ('.docx', '.xlsx', '.pptx', '.zip', '.odoc', '.osheet', '.eddx', '.emmx'):
            import zipfile
            return 'zip-ok' if zipfile.is_zipfile(path) else 'dead'
        if ext == '.pdf':
            return 'pdf-ok' if head.startswith(b'%PDF') else 'dead'
        if ext in ('.doc', '.xls', '.ppt'):
            return 'ole-ok' if head.startswith(b'\xd0\xcf\x11\xe0') else 'dead'
        if ext in ('.txt', '.md', '.csv', '.html', '.eml', '.webloc'):
            with open(path, 'r', encoding='utf-8', errors='strict') as f:
                f.read(4096)
            return 'text-ok'
        if ext == '.rar':
            return 'rar-ok' if head.startswith(b'Rar!') else 'dead'
        return 'exists-ok'
    except Exception as e:
        return 'error:%s' % str(e)[:60]


def decodes(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in IMG_EXTS:
            try:
                with Image.open(path) as im:
                    im.load()
                return 'pil-ok'
            except Exception:
                arr = cv2.imread(path, cv2.IMREAD_UNCHANGED)
                return 'cv2-ok' if arr is not None and arr.size > 0 else 'dead'
        if ext in VID_EXTS:
            cap = cv2.VideoCapture(path)
            ok, frame = cap.read()
            cap.release()
            return 'cv2-ok' if ok and frame is not None and frame.size > 0 else 'dead'
        if ext == '.livp':
            import zipfile
            return 'zip-ok' if zipfile.is_zipfile(path) else 'dead'
    except Exception as e:
        return 'error:%s' % str(e)[:60]
    return doc_usable(path)


OK_VERDICTS = {'pil-ok', 'cv2-ok', 'zip-ok', 'pdf-ok', 'ole-ok', 'text-ok', 'rar-ok', 'exists-ok'}


def build_basename_index():
    out = subprocess.run(['find', LIB_PREFIX, '-type', 'f', '-print0'],
                         capture_output=True, check=True)
    idx = defaultdict(list)
    n = 0
    for raw in out.stdout.split(b'\0'):
        if not raw:
            continue
        p = raw.decode('utf-8', 'surrogateescape')
        idx[os.path.basename(p)].append(p)
        n += 1
    print('basename index: %d files, %d distinct basenames' % (n, len(idx)), flush=True)
    return idx


def main():
    verify = {}
    with open(VERIFY_TSV, encoding='utf-8') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            verify[r['src']] = r
    inbox_by_hash = defaultdict(list)
    for src, r in verify.items():
        if r['sha256_disk']:
            inbox_by_hash[r['sha256_disk']].append(src)

    dup_rows = []
    with open(NONE_TSV, encoding='utf-8') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            if r['bucket'] == '重复(dup)':
                dup_rows.append(r)
    print('dup rows: %d' % len(dup_rows), flush=True)

    idx = build_basename_index()

    # 先算每行的 sha 与库内同名候选的哈希比对
    info = {}
    for r in dup_rows:
        rq_id, src = int(r['rq_id']), r['src']
        sha = verify.get(src, {}).get('sha256_disk', '')
        verdict = verify.get(src, {}).get('verdict', '')
        if not sha:
            if os.path.exists(src):
                try:
                    sha = sha256(src)
                    verdict = 'hashed-doc'
                except Exception as e:
                    info[rq_id] = {'rq_id': rq_id, 'src': src, 'sha256': '',
                                   'action': 'skip', 'reason': 'hash-error:%s' % str(e)[:60],
                                   'peers_inbox': '', 'peers_lib': '', 'retained': ''}
                    continue
            else:
                info[rq_id] = {'rq_id': rq_id, 'src': src, 'sha256': '',
                               'action': 'skip', 'reason': 'src-gone',
                               'peers_inbox': '', 'peers_lib': '', 'retained': ''}
                continue
        if verdict == 'dead':
            info[rq_id] = {'rq_id': rq_id, 'src': src, 'sha256': sha,
                           'action': 'skip', 'reason': 'already-deleted-as-damaged',
                           'peers_inbox': '', 'peers_lib': '', 'retained': ''}
            continue
        peers_inbox = [p for p in inbox_by_hash.get(sha, []) if p != src]
        base = os.path.basename(src)
        cands = [p for p in idx.get(base, []) if p != src]
        lib_match = []
        lib_same_name_diff_hash = []
        for cand in cands:
            try:
                if os.path.getsize(cand) != os.path.getsize(src):
                    lib_same_name_diff_hash.append(cand)
                    continue
                if sha256(cand) == sha:
                    lib_match.append(cand)
                else:
                    lib_same_name_diff_hash.append(cand)
            except Exception:
                continue
        info[rq_id] = {'rq_id': rq_id, 'src': src, 'sha256': sha,
                       'peers_inbox': ';'.join(peers_inbox),
                       'peers_lib': ';'.join(lib_match),
                       'lib_diff_hash': ';'.join(lib_same_name_diff_hash),
                       'verdict': verdict}

    # 组内 keeper 指定：按哈希分组（收件箱内同伴），每组留一份解码通过的
    groups = defaultdict(list)
    for rq_id, inf in info.items():
        if inf.get('sha256'):
            groups[inf['sha256']].append(inf)
    keeper_of = {}
    for sha, members in groups.items():
        # 组内所有收件箱文件（含非 dup 行）：复核 verdict 好的
        all_files = set()
        for m in members:
            all_files.add(m['src'])
            for p in m['peers_inbox'].split(';'):
                if p:
                    all_files.add(p)
        good = [p for p in all_files
                if verify.get(p, {}).get('verdict') in ('pil-ok', 'cv2-ok')]
        if not good:
            # 文档行等：以 members 自身为准
            good = [m['src'] for m in members if m.get('verdict') == 'hashed-doc']
        if good:
            keeper_of[sha] = sorted(good)[0]

    results = []
    for rq_id, inf in info.items():
        if 'action' in inf:
            results.append({k: inf.get(k, '') for k in
                            ('rq_id', 'src', 'sha256', 'action', 'reason',
                             'peers_inbox', 'peers_lib', 'retained')})
            continue
        sha = inf['sha256']
        # 库内保留份
        retained_lib_ok = ''
        retained_lib_bad = ''
        for cand in inf['peers_lib'].split(';'):
            if not cand:
                continue
            d = decodes(cand)
            if d in OK_VERDICTS:
                retained_lib_ok = cand
                break
            retained_lib_bad = cand + ' (decode:%s)' % d
        if retained_lib_ok:
            results.append({'rq_id': rq_id, 'src': inf['src'], 'sha256': sha,
                            'action': 'delete', 'reason': 'retained-in-library-ok',
                            'peers_inbox': inf['peers_inbox'], 'peers_lib': inf['peers_lib'],
                            'retained': retained_lib_ok})
            continue
        if retained_lib_bad:
            results.append({'rq_id': rq_id, 'src': inf['src'], 'sha256': sha,
                            'action': 'skip', 'reason': 'retained-in-library-bad',
                            'peers_inbox': inf['peers_inbox'], 'peers_lib': inf['peers_lib'],
                            'retained': retained_lib_bad})
            continue
        keeper = keeper_of.get(sha)
        if keeper and keeper != inf['src'] and inf['peers_inbox']:
            results.append({'rq_id': rq_id, 'src': inf['src'], 'sha256': sha,
                            'action': 'delete', 'reason': 'retained-in-inbox-ok',
                            'peers_inbox': inf['peers_inbox'], 'peers_lib': inf['peers_lib'],
                            'retained': keeper})
            continue
        if keeper == inf['src'] and inf['peers_inbox']:
            results.append({'rq_id': rq_id, 'src': inf['src'], 'sha256': sha,
                            'action': 'skip', 'reason': 'group-keeper',
                            'peers_inbox': inf['peers_inbox'], 'peers_lib': inf['peers_lib'],
                            'retained': inf['src']})
            continue
        if inf.get('lib_diff_hash'):
            results.append({'rq_id': rq_id, 'src': inf['src'], 'sha256': sha,
                            'action': 'skip', 'reason': 'similar-not-identical',
                            'peers_inbox': inf['peers_inbox'], 'peers_lib': inf['peers_lib'],
                            'retained': ''})
            continue
        results.append({'rq_id': rq_id, 'src': inf['src'], 'sha256': sha,
                        'action': 'skip', 'reason': 'no-peer-found',
                        'peers_inbox': inf['peers_inbox'], 'peers_lib': inf['peers_lib'],
                        'retained': ''})

    cols = ['rq_id', 'src', 'sha256', 'action', 'reason', 'peers_inbox', 'peers_lib', 'retained']
    with open(OUT_TSV, 'w', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter='\t')
        w.writeheader()
        for res in results:
            w.writerow(res)
    summ = {'dup_rows': len(dup_rows),
            'actions': dict(Counter(r['action'] for r in results)),
            'reasons': dict(Counter(r['reason'] for r in results)),
            'groups': len(groups)}
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print('DEDUP2 DONE %s' % json.dumps(summ, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
