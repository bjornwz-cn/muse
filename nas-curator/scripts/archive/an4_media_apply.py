#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Media batch apply (2026-11-25 new policy): move pending photos/videos to media library by VLM content category; delete confirmed-damaged/blank media.
- MOVE: pending media with a usable VLM content category -> 媒体库/照片/<big>/<cat> or 媒体库/视频/<cat>; VOB -> 媒体库/视频/DVD视频; livp treated as photo.
- DELETE: 900 pixel-blank photos (mode>=0.8 or gray>=0.95 AND VLM reason has damage keyword) + 2 confirmed black videos (124209,124214). Delete flow: plan + apply, decided_by recorded, DB backed up first, NO operations rows for deletes.
- SKIP (stay pending): 7 missing-source, 1 CR2 (139386), 12 ambiguous low-conf photos, 3 ambiguous low-conf videos, 150025 approved-only (already in place).
- Collision: same name different content -> versioned name (created-time, size, sequence), both kept.
Usage: docker exec nas-curator python3 /data/an4_media_apply.py [--dry-run] [--apply]
"""
import hashlib, json, os, shutil, sqlite3, sys
from datetime import datetime

DB = '/data/nas-curator.db'
LIB_ROOT = '/vol1/1000/媒体库'
OWNER_UID, OWNER_GID = 1000, 1000
DELETE_IDS_FILE = '/data/an4_media_delete_ids.txt'
BLACK_VIDEOS = {124209, 124214}
VOB_IDS = {150144, 150145, 150146, 150147, 150148}
APPROVE_ONLY = {150025}
SKIP_IDS = {139386, 141729, 150196, 150201, 150211, 150213, 150221,
            47109, 106249, 120162, 120339, 125887, 125894, 126076, 126163,
            143963, 144607, 145399, 145906,  # 12 ambiguous low-conf photos
            141865, 142045, 150079}          # 3 ambiguous low-conf videos

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
VID_EXTS = {'.mp4', '.mov', '.mts', '.mpg', '.vob'}
UNUSABLE_CATS = {'待确认', '模糊', '疑似重复', '99-待确认/04-低置信', ''}

def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

def backup_db(db):
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    dst = f'/data/nas-curator.db.bak_an4_media_{ts}'
    src_conn = sqlite3.connect(DB)
    dst_conn = sqlite3.connect(dst)
    src_conn.backup(dst_conn)
    dst_conn.close(); src_conn.close()
    print('DB backed up ->', dst, flush=True)
    return dst

def eff_cat(p):
    for k in ('vlm_category', 'vlm_economy_category'):
        v = (p.get(k) or '').strip()
        if v and v not in UNUSABLE_CATS:
            return v
    return ''

def versioned_name(dst_dir, name):
    """Same name different content: append __1, __2 ... (both kept)."""
    base, ext = os.path.splitext(name)
    cand = os.path.join(dst_dir, name)
    if not os.path.exists(cand):
        return cand
    i = 1
    while True:
        cand = os.path.join(dst_dir, f'{base}__{i}{ext}')
        if not os.path.exists(cand):
            return cand
        i += 1

def main():
    dry = '--dry-run' in sys.argv
    apply = '--apply' in sys.argv
    if not dry and not apply:
        print('usage: an4_media_apply.py [--dry-run|--apply]')
        return 2
    db = sqlite3.connect(DB, timeout=60)
    db.execute('PRAGMA busy_timeout=60000')
    delete_ids = set()
    with open(DELETE_IDS_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                delete_ids.add(int(line))
    delete_ids |= BLACK_VIDEOS
    rows = db.execute("SELECT id, payload FROM review_queue WHERE status='pending'").fetchall()
    moves, deletes, approves, skips = [], [], [], []
    for rid, payload in rows:
        p = json.loads(payload)
        src = p.get('src') or p.get('src_path') or ''
        ext = os.path.splitext(src)[1].lower()
        if ext not in IMG_EXTS and ext not in VID_EXTS:
            continue  # not media
        if rid in delete_ids:
            deletes.append((rid, src, 'blank/damaged-confirmed'))
            continue
        if rid in APPROVE_ONLY:
            approves.append((rid, src, 'already-in-place'))
            continue
        if rid in SKIP_IDS:
            skips.append((rid, src, 'leave-pending'))
            continue
        if rid in VOB_IDS:
            dst = os.path.join(LIB_ROOT, '视频/DVD视频', os.path.basename(src))
            moves.append((rid, src, dst, 'VOB'))
            continue
        cat = eff_cat(p)
        if not cat:
            skips.append((rid, src, 'no-category'))
            continue
        if ext in IMG_EXTS or ext == '.livp':
            sub = PHOTO_DIRS.get(cat)
        else:
            sub = VIDEO_DIRS.get(cat)
        if not sub:
            skips.append((rid, src, 'no-dst-for-cat:' + cat))
            continue
        dst = os.path.join(LIB_ROOT, sub, os.path.basename(src))
        moves.append((rid, src, dst, cat))
    print(f'moves={len(moves)} deletes={len(deletes)} approves={len(approves)} skips={len(skips)}', flush=True)
    from collections import Counter
    print('moves by dst dir:', Counter(os.path.dirname(d) for _,_,d,_ in moves).most_common(), flush=True)
    print('deletes sample:', deletes[:3], flush=True)
    print('skips:', Counter(s[2] for s in skips), flush=True)
    if dry:
        print('DRY RUN - no changes made', flush=True)
        return 0
    # --- apply ---
    backup_db(db)
    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    # Move plan
    cur = db.execute("INSERT INTO plans (library, rules_version, status, summary) VALUES (?,?,?,?)",
                     ('media', 1, 'applying', f'an4 media move {len(moves)} files by VLM content category (2026-11-25 new policy)'))
    move_plan = cur.lastrowid
    moved, failed, renamed = 0, [], 0
    for rid, src, dst, cat in moves:
        if not os.path.exists(src):
            failed.append((rid, src, 'src-missing-at-apply'))
            continue
        dst_dir = os.path.dirname(dst)
        os.makedirs(dst_dir, exist_ok=True)
        final_dst = dst
        action = 'move'
        if os.path.exists(dst):
            # same name: if identical content (sha256) skip as dup, else versioned name
            try:
                if sha256(src) == sha256(dst):
                    db.execute("UPDATE review_queue SET status='approved', decided_by='an4-media-dup', decided_at=? WHERE id=?", (now, rid))
                    continue
            except Exception:
                pass
            final_dst = versioned_name(dst_dir, os.path.basename(dst))
            action = 'rename'
            renamed += 1
        try:
            file_sha = sha256(src)
            size = os.path.getsize(src)
            shutil.move(src, final_dst)
            try:
                os.chown(final_dst, OWNER_UID, OWNER_GID)
            except Exception:
                pass
            db.execute("INSERT INTO plan_items (plan_id, src_path, dst_path, category, action, kind, size, ext, sha256, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (move_plan, src, final_dst, cat, action, 'media', size, os.path.splitext(src)[1].lower(), file_sha, 'done'))
            db.execute("INSERT INTO operations (plan_id, src_path, dst_path, sha256) VALUES (?,?,?,?)",
                       (move_plan, src, final_dst, file_sha))
            db.execute("UPDATE review_queue SET status='approved', decided_by='an4-media-move', decided_at=? WHERE id=?", (now, rid))
            moved += 1
        except Exception as e:
            failed.append((rid, src, 'move-err:' + str(e)[:100]))
        if moved % 200 == 0:
            db.commit()
            print(f'  moved {moved}/{len(moves)}', flush=True)
    db.execute("UPDATE plans SET status='done' WHERE id=?", (move_plan,))
    db.commit()
    print(f'MOVE plan {move_plan} done: moved={moved} renamed={renamed} failed={len(failed)}', flush=True)
    for f_ in failed:
        print('  FAILED:', f_, flush=True)
    # Delete plan (no operations rows)
    cur = db.execute("INSERT INTO plans (library, rules_version, status, summary) VALUES (?,?,?,?)",
                     ('media', 1, 'applying', f'an4 media delete {len(deletes)} confirmed blank/damaged files (王震 2026-11-25 损坏就删)'))
    del_plan = cur.lastrowid
    deleted, del_failed = 0, []
    total_bytes = 0
    for rid, src, why in deletes:
        if not os.path.exists(src):
            # already gone: mark approved with note
            db.execute("UPDATE review_queue SET status='approved', decided_by='an4-media-delete-gone', decided_at=? WHERE id=?", (now, rid))
            continue
        try:
            size = os.path.getsize(src)
            os.remove(src)
            total_bytes += size
            db.execute("INSERT INTO plan_items (plan_id, src_path, dst_path, category, action, kind, size, ext, reason, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (del_plan, src, '', '', 'review', 'media-delete', size, os.path.splitext(src)[1].lower(), why, 'done'))
            db.execute("UPDATE review_queue SET status='approved', decided_by='王震 2026-11-25 裁决删除', decided_at=? WHERE id=?", (now, rid))
            deleted += 1
        except Exception as e:
            del_failed.append((rid, src, 'del-err:' + str(e)[:100]))
        if deleted % 200 == 0:
            db.commit()
    db.execute("UPDATE plans SET status='done' WHERE id=?", (del_plan,))
    db.commit()
    print(f'DELETE plan {del_plan} done: deleted={deleted} bytes={total_bytes} ({total_bytes/1024/1024:.1f} MiB) failed={len(del_failed)}', flush=True)
    for f_ in del_failed:
        print('  DEL FAILED:', f_, flush=True)
    # approves (already in place)
    for rid, src, why in approves:
        db.execute("UPDATE review_queue SET status='approved', decided_by='an4-media-already-in-place', decided_at=? WHERE id=?", (now, rid))
    db.commit()
    print(f'APPROVES: {len(approves)} marked approved (already in place)', flush=True)
    print(f'SKIPS (left pending): {len(skips)}', flush=True)
    db.close()
    return 0

if __name__ == '__main__':
    sys.exit(main())
