#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cleanup: delete the 403 inbox source files that are confirmed duplicates
(same sha256 already in the media library at the destination path).
For each review row with decided_by='an4-media-dup':
  - reconstruct expected dst from VLM category (photo/video dirs; VOB -> DVD视频）
  - verify src exists AND dst exists AND sha256(src)==sha256(dst)
  - if verified: delete src (dedup rule: hash identical + kept copy intact)
  - else: report as NOT deleted (leave file, list it)
Records deletions in a plan (action='review', kind='media-dedup', decided_by recorded).
Usage: docker exec nas-curator python3 /data/an4_dup_cleanup.py [--dry-run] [--apply]
"""
import hashlib, json, os, sqlite3, sys
from datetime import datetime

DB = '/data/nas-curator.db'
LIB_ROOT = '/vol1/1000/媒体库'
PHOTO_DIRS = {
    '人物合影': '照片/家庭与人物/人物合影', '儿童成长': '照片/家庭与人物/儿童成长',
    '自拍': '照片/家庭与人物/自拍', '旅行风景': '照片/旅行与城市/旅行风景',
    '城市与建筑': '照片/旅行与城市/城市与建筑', '日常生活': '照片/生活记录/日常生活',
    '美食与咖啡': '照片/生活记录/美食与咖啡', '宠物与动物': '照片/生活记录/宠物与动物',
    '工作与活动': '照片/生活记录/工作与活动', '屏幕截图': '照片/资料与截图/屏幕截图',
    '文档与票据': '照片/资料与截图/文档与票据', '梗图与表情': '照片/资料与截图/梗图与表情',
}
VIDEO_DIRS = {
    '儿童成长': '视频/儿童成长', '旅行风景': '视频/旅行风景',
    '日常生活': '视频/日常生活', '聚会与夜生活': '视频/聚会与夜生活',
    '运动与活动': '视频/运动与活动', '屏幕录制': '视频/屏幕录制',
    '文档与票据': '视频/文档与票据', '自拍': '视频/自拍',
    '城市与建筑': '视频/城市与建筑', '人物合影': '视频/人物合影',
    '工作与活动': '视频/工作与活动', '宠物与动物': '视频/宠物与动物',
    '美食与咖啡': '视频/美食与咖啡', '梗图与表情': '视频/梗图与表情',
    '屏幕截图': '视频/屏幕截图',
}
IMG_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif', '.gif', '.tiff', '.cr2', '.livp'}
UNUSABLE_CATS = {'待确认', '模糊', '疑似重复', '99-待确认/04-低置信', ''}

def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

def eff_cat(p):
    for k in ('vlm_category', 'vlm_economy_category'):
        v = (p.get(k) or '').strip()
        if v and v not in UNUSABLE_CATS:
            return v
    return ''

def main():
    dry = '--dry-run' in sys.argv
    apply = '--apply' in sys.argv
    if not dry and not apply:
        print('usage: an4_dup_cleanup.py [--dry-run|--apply]')
        return 2
    db = sqlite3.connect(DB, timeout=60)
    db.execute('PRAGMA busy_timeout=60000')
    rows = db.execute("SELECT id, payload FROM review_queue WHERE decided_by='an4-media-dup'").fetchall()
    print('dup rows:', len(rows), flush=True)
    verified, problems = [], []
    for rid, payload in rows:
        p = json.loads(payload)
        src = p.get('src') or p.get('src_path') or ''
        ext = os.path.splitext(src)[1].lower()
        if not src or not os.path.exists(src):
            problems.append((rid, src, 'src-gone'))
            continue
        # reconstruct dst (same logic as media apply; VOB handled by ext)
        cat = eff_cat(p)
        if ext == '.vob':
            dst = os.path.join(LIB_ROOT, '视频/DVD视频', os.path.basename(src))
        elif ext in IMG_EXTS:
            sub = PHOTO_DIRS.get(cat)
            dst = os.path.join(LIB_ROOT, sub, os.path.basename(src)) if sub else None
        else:
            sub = VIDEO_DIRS.get(cat)
            dst = os.path.join(LIB_ROOT, sub, os.path.basename(src)) if sub else None
        if not dst:
            problems.append((rid, src, 'no-dst-reconstructed'))
            continue
        # dst may have been versioned (__1) if a same-name different-content file existed;
        # check the exact dst first, then versioned variants
        candidates = [dst]
        base, e = os.path.splitext(dst)
        i = 1
        while os.path.exists(f'{base}__{i}{e}'):
            candidates.append(f'{base}__{i}{e}')
            i += 1
        match = None
        try:
            src_sha = sha256(src)
        except Exception as ex:
            problems.append((rid, src, 'src-hash-err:' + str(ex)[:60]))
            continue
        for cand in candidates:
            try:
                if sha256(cand) == src_sha:
                    match = cand
                    break
            except Exception:
                continue
        if match:
            verified.append((rid, src, match, os.path.getsize(src)))
        else:
            problems.append((rid, src, 'no-matching-dst (dst exists but sha differs or dst missing)'))
    print(f'verified dups (safe to delete src): {len(verified)}', flush=True)
    print(f'problems (NOT deleted): {len(problems)}', flush=True)
    for pr in problems:
        print('  PROBLEM:', pr, flush=True)
    if dry:
        print('DRY RUN - no changes made', flush=True)
        return 0
    # backup DB
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    dst_bak = f'/data/nas-curator.db.bak_an4_dup_{ts}'
    s = sqlite3.connect(DB); d = sqlite3.connect(dst_bak)
    s.backup(d); d.close(); s.close()
    print('DB backed up ->', dst_bak, flush=True)
    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    cur = db.execute("INSERT INTO plans (library, rules_version, status, summary) VALUES (?,?,?,?)",
                     ('media', 1, 'applying', f'an4 dup cleanup: delete {len(verified)} inbox sources already in media library (sha256 identical)'))
    plan_id = cur.lastrowid
    deleted, failed = 0, []
    total_bytes = 0
    for rid, src, dst, size in verified:
        try:
            os.remove(src)
            total_bytes += size
            db.execute("INSERT INTO plan_items (plan_id, src_path, dst_path, category, action, kind, size, ext, reason, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (plan_id, src, dst, 'dup', 'review', 'media-dedup', size, os.path.splitext(src)[1].lower(), 'sha256-identical copy already in media library', 'done'))
            deleted += 1
        except Exception as e:
            failed.append((rid, src, 'del-err:' + str(e)[:100]))
        if deleted % 100 == 0:
            db.commit()
    db.execute("UPDATE plans SET status='done' WHERE id=?", (plan_id,))
    db.commit()
    print(f'DUP CLEANUP plan {plan_id} done: deleted={deleted} bytes={total_bytes} ({total_bytes/1024/1024:.1f} MiB) failed={len(failed)}', flush=True)
    for f_ in failed:
        print('  FAILED:', f_, flush=True)
    db.close()
    return 0

if __name__ == '__main__':
    sys.exit(main())
