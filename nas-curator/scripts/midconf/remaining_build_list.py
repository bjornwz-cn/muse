#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剩余视频任务 步骤 0：从 review_queue 拉 pending 视频行清单（只读）。
视频扩展名：.mov/.mp4/.avi/.mkv/.m4v/.wmv/.flv/.webm/.vob/.3gp/.mts/.mpg
排除 src 含 __CLOUDSYNC_ENC__ 的加密文件。
输出 /data/remaining_videos.tsv（rq_id, src, kind, size, mtime, sha256_list, has_vlm_old, old_vlm_conf）
与 /data/remaining_videos_summary.json；同时打印 review 计数与 integrity。
用法：docker exec nas-curator python3 /data/remaining_build_list.py
"""
import json, os, sqlite3

DB = '/data/nas-curator.db'
OUT_TSV = '/data/remaining_videos.tsv'
OUT_JSON = '/data/remaining_videos_summary.json'

VID_EXTS = {'.mov', '.mp4', '.avi', '.mkv', '.m4v', '.wmv', '.flv',
            '.webm', '.vob', '.3gp', '.mts', '.mpg'}


def main():
    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=30000')
    print('integrity:', db.execute('PRAGMA integrity_check').fetchone()[0], flush=True)
    counts = dict(db.execute(
        "SELECT status, COUNT(*) FROM review_queue GROUP BY status").fetchall())
    print('review counts:', json.dumps(counts, ensure_ascii=False), flush=True)
    rows = db.execute(
        "SELECT id, kind, payload FROM review_queue WHERE status='pending' ORDER BY id"
    ).fetchall()
    print('pending rows scanned: %d' % len(rows), flush=True)
    out = []
    n_enc = 0
    for rid, kind, payload in rows:
        try:
            d = json.loads(payload)
        except Exception:
            continue
        src = d.get('src', '') or ''
        if '__CLOUDSYNC_ENC__' in src:
            n_enc += 1
            continue
        ext = os.path.splitext(src)[1].lower()
        if ext not in VID_EXTS:
            continue
        out.append({'rq_id': rid, 'src': src, 'kind': kind,
                    'size': d.get('size'), 'mtime': d.get('mtime'),
                    'has_vlm': 'vlm_category' in d,
                    'old_vlm_conf': d.get('vlm_confidence'),
                    'old_vlm_model': d.get('vlm_model'),
                    'old_vlm_source': d.get('vlm_source')})
    with open(OUT_TSV, 'w', encoding='utf-8') as f:
        f.write('rq_id\tsrc\tkind\tsize\tmtime\thas_vlm\told_vlm_conf\told_vlm_model\told_vlm_source\n')
        for r in out:
            f.write('%d\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' % (
                r['rq_id'], r['src'], r['kind'], r['size'], r['mtime'],
                int(r['has_vlm']), r['old_vlm_conf'], r['old_vlm_model'],
                r['old_vlm_source']))
    by_ext = {}
    for r in out:
        e = os.path.splitext(r['src'])[1].lower()
        by_ext[e] = by_ext.get(e, 0) + 1
    summ = {'pending_total': len(rows), 'pending_videos': len(out),
            'excluded_encrypted_pending': n_enc,
            'by_ext': by_ext,
            'with_old_vlm': sum(1 for r in out if r['has_vlm']),
            'rq_133420_in_list': any(r['rq_id'] == 133420 for r in out),
            'rq_149995_in_list': any(r['rq_id'] == 149995 for r in out),
            'rq_133420_src': next((r['src'] for r in out if r['rq_id'] == 133420), None),
            'rq_149995_src': next((r['src'] for r in out if r['rq_id'] == 149995), None)}
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print('SUMMARY ' + json.dumps(summ, ensure_ascii=False), flush=True)
    # 133420 / 149995 行详情（供回执核对）
    for rid in (133420, 149995):
        row = db.execute(
            "SELECT status, decided_by, payload FROM review_queue WHERE id=?", (rid,)).fetchone()
        print('rq %d: %s' % (rid, json.dumps(row, ensure_ascii=False)[:600] if row else 'NOT FOUND'), flush=True)
    db.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
