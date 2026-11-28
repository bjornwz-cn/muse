#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤3 步骤2：去重分析（只读，不删、不写库）。
对 445 个 kind=dup 的待确认行：
 - 取其 sha256（媒体行来自复核清单；文档行现场算哈希）；
 - 找同哈希的同伴：在盘复核清单内（收件箱内）＋ files 表（媒体库内）；
 - 判定保留份：库内同伴优先（须在盘、大小一致、解码通过）；
   仅收件箱内同伴时，留一份解码通过的作保留份；
 - 无同伴 / 保留份不可用 / 本行已删 → 跳过并记原因。
输出 /data/pending_triage_dedup_analysis.tsv 与
/data/pending_triage_dedup_analysis.json。
"""
import csv, hashlib, json, os, sqlite3, sys
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
OUT_TSV = '/data/pending_triage_dedup_analysis.tsv'
OUT_JSON = '/data/pending_triage_dedup_analysis.json'
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
    """文档保留份可用性：存在、能打开、格式头正常。"""
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
    return doc_usable(path) if ext not in IMG_EXTS and ext not in VID_EXTS else 'unhandled-ext:' + ext


def main():
    conn = sqlite3.connect(DB, timeout=30)
    files_rows = conn.execute("select path, sha256, size from files").fetchall()
    conn.close()
    lib_by_hash = defaultdict(list)
    for path, sha, size in files_rows:
        if sha:
            lib_by_hash[sha].append((path, size))

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

    results = []
    for r in dup_rows:
        rq_id, src = int(r['rq_id']), r['src']
        sha = verify.get(src, {}).get('sha256_disk', '')
        verdict = verify.get(src, {}).get('verdict', '')
        if not sha:
            # 文档行等不在复核清单内：现场算哈希
            if os.path.exists(src):
                try:
                    sha = sha256(src)
                    verdict = 'hashed-doc'
                except Exception as e:
                    results.append({'rq_id': rq_id, 'src': src, 'sha256': '',
                                    'action': 'skip', 'reason': 'hash-error:%s' % str(e)[:60],
                                    'peers_inbox': '', 'peers_lib': '', 'retained': ''})
                    continue
            else:
                results.append({'rq_id': rq_id, 'src': src, 'sha256': '',
                                'action': 'skip', 'reason': 'src-gone',
                                'peers_inbox': '', 'peers_lib': '', 'retained': ''})
                continue
        peers_inbox = [p for p in inbox_by_hash.get(sha, []) if p != src]
        peers_lib = lib_by_hash.get(sha, [])
        # 本行已在删除计划中删掉？
        if verdict == 'dead':
            results.append({'rq_id': rq_id, 'src': src, 'sha256': sha,
                            'action': 'skip', 'reason': 'already-deleted-as-damaged',
                            'peers_inbox': ';'.join(peers_inbox),
                            'peers_lib': ';'.join(p for p, _ in peers_lib), 'retained': ''})
            continue
        # 库内保留份：逐个核对在盘＋大小＋解码
        retained = ''
        retained_ok = False
        for lib_path, lib_size in peers_lib:
            if not os.path.exists(lib_path):
                continue
            try:
                if os.path.getsize(lib_path) != lib_size:
                    continue
            except Exception:
                continue
            d = decodes(lib_path)
            if d in ('pil-ok', 'cv2-ok', 'zip-ok', 'pdf-ok', 'ole-ok', 'text-ok', 'rar-ok', 'exists-ok'):
                retained, retained_ok = lib_path, True
                break
            else:
                retained = lib_path + ' (decode:%s)' % d
        if retained_ok:
            results.append({'rq_id': rq_id, 'src': src, 'sha256': sha,
                            'action': 'delete', 'reason': 'retained-in-library-ok',
                            'peers_inbox': ';'.join(peers_inbox),
                            'peers_lib': ';'.join(p for p, _ in peers_lib),
                            'retained': retained})
            continue
        if peers_lib and not retained_ok:
            results.append({'rq_id': rq_id, 'src': src, 'sha256': sha,
                            'action': 'skip', 'reason': 'retained-in-library-bad-or-gone',
                            'peers_inbox': ';'.join(peers_inbox),
                            'peers_lib': ';'.join(p for p, _ in peers_lib),
                            'retained': retained})
            continue
        # 仅收件箱内同伴：留一份解码通过的
        if peers_inbox:
            # 同伴解码结论（复核清单 verdict）
            good = [p for p in peers_inbox
                    if verify.get(p, {}).get('verdict') in ('pil-ok', 'cv2-ok')]
            self_ok = verdict in ('pil-ok', 'cv2-ok', 'hashed-doc')
            if good:
                # 保留份取同伴中第一份好的；本行可删（同伴仍在）
                results.append({'rq_id': rq_id, 'src': src, 'sha256': sha,
                                'action': 'delete', 'reason': 'retained-in-inbox-ok',
                                'peers_inbox': ';'.join(peers_inbox),
                                'peers_lib': '', 'retained': good[0]})
            elif self_ok:
                results.append({'rq_id': rq_id, 'src': src, 'sha256': sha,
                                'action': 'skip', 'reason': 'inbox-peers-bad-self-keeper',
                                'peers_inbox': ';'.join(peers_inbox),
                                'peers_lib': '', 'retained': src})
            else:
                results.append({'rq_id': rq_id, 'src': src, 'sha256': sha,
                                'action': 'skip', 'reason': 'no-good-copy-anywhere',
                                'peers_inbox': ';'.join(peers_inbox),
                                'peers_lib': '', 'retained': ''})
            continue
        results.append({'rq_id': rq_id, 'src': src, 'sha256': sha,
                        'action': 'skip', 'reason': 'no-peer-found',
                        'peers_inbox': '', 'peers_lib': '', 'retained': ''})

    # 组内多删冲突：同一哈希多行都判 delete 时，只留其中一行作保留份
    # （inbox-only 组：第一行判 delete 的同伴可能正是另一行判 delete 的本行）。
    # 重算：按哈希分组，若组内 delete 行数 == 组内 inbox 文件数且无库内保留份，
    # 则每组留 1 行（取第一行）改为 skip-keeper。
    by_hash = defaultdict(list)
    for res in results:
        if res['sha256']:
            by_hash[res['sha256']].append(res)
    fixed = 0
    for sha, grp in by_hash.items():
        dels = [g for g in grp if g['action'] == 'delete' and g['reason'] == 'retained-in-inbox-ok']
        if not dels:
            continue
        # 该组 inbox 文件总数 = 组内行数（都在复核清单内）
        # 若所有 inbox 同伴行都被判删，说明没有留下的 keeper
        inbox_files = set()
        for g in grp:
            inbox_files.add(g['src'])
            for p in g['peers_inbox'].split(';'):
                if p:
                    inbox_files.add(p)
        if len(dels) >= len(inbox_files):
            # 留第一行作保留份
            keep = dels[0]
            keep['action'] = 'skip'
            keep['reason'] = 'group-keeper'
            keep['retained'] = keep['src']
            fixed += 1
    print('group-keeper fixes: %d' % fixed, flush=True)

    cols = ['rq_id', 'src', 'sha256', 'action', 'reason', 'peers_inbox', 'peers_lib', 'retained']
    with open(OUT_TSV, 'w', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter='\t')
        w.writeheader()
        for res in results:
            w.writerow(res)
    summ = {'dup_rows': len(dup_rows),
            'actions': dict(Counter(r['action'] for r in results)),
            'reasons': dict(Counter(r['reason'] for r in results)),
            'groups': len(by_hash),
            'multi_file_groups': sum(1 for v in by_hash.values() if len(v) > 1)}
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print('DEDUP ANALYSIS DONE %s' % json.dumps(summ, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
