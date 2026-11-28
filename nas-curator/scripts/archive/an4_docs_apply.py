#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Docs batch apply (2026-11-25 new policy): move pending docs by content-determined party.
- Nawei set (54): 36 monthly tax zips + 4 tax PDFs + 3 balance sheets + 4 auth letters + 1 情况说明 + 4 JDE xls + 2 rar
  -> 工作文件/3-北京供应链/08-运营与项目/02-投资与尽调/<year>/, renamed YYYY-MM-DD_陕西纳威_<matter>.<ext>
- Henan Xiuwang (138000) + Hebei Jiaxuan (138001) zips (same M&A due diligence, Dec 2022)
  -> 工作文件/3-北京供应链/08-运营与项目/02-投资与尽调/2022/, renamed 2022-12-30_<company>_<matter>.zip
- 34 determined-party docs -> 工作文件/<party>/<doc_subdir>/, original names (.eml never renamed).
- Credentials (139399, 138941, 138943) -> 家庭文件/王震/, original names, content NOT read.
- Collision: same name different content -> versioned __1/__2; identical content -> skip as dup (mark approved).
- Everything else stays pending.
Usage: docker exec nas-curator python3 /data/an4_docs_apply.py [--dry-run] [--apply]
"""
import hashlib, json, os, re, shutil, sqlite3, sys
from datetime import datetime
from collections import Counter

DB = '/data/nas-curator.db'
WORK_ROOT = '/vol1/1000/工作文件'
FAM_ROOT = '/vol1/1000/家庭文件'
OWNER_UID, OWNER_GID = 1000, 1000
NAWEI_DST = os.path.join(WORK_ROOT, '3-北京供应链/08-运营与项目/02-投资与尽调')

# Nawei monthly tax zips: rq 137964-137999, filename like 2019.1(1).zip
NAWEI_ZIP_IDS = set(range(137964, 138000))
# Nawei related docs: rq -> (year, matter, date_str)
NAWEI_DOCS = {
    137907: ('2019', '税务申报', '2019-01-01'),   # 2019年.pdf
    137908: ('2020', '税务申报', '2020-01-01'),   # 2020年.pdf
    137909: ('2021', '税务申报', '2021-01-01'),   # 2021年.pdf
    137910: ('2022', '税务申报', '2022-01-01'),   # 2022年1-3月.pdf
    137960: ('2019', '资产负债表', '2019-01-01'), # 负债表2019.pdf
    137961: ('2020', '资产负债表', '2020-01-01'), # 负债表2020.pdf
    137962: ('2021', '资产负债表', '2021-01-01'), # 负债表2021.pdf
    137951: ('2022', '授权书-吉泰中航', '2022-01-01'),  # 吉泰-中航-2022.pdf
    137955: ('2022', '授权书-正天红会', '2022-01-01'),  # 正天-红会-2022.pdf
    137956: ('2021', '授权书-正天西京', '2021-01-01'),  # 正天-西京-2021.pdf
    137957: ('2022', '授权书-正天西京', '2022-01-01'),  # 正天-西京-2022.pdf
    137953: ('2022', '情况说明', '2022-01-01'),   # 情况说明.pdf (year inferred: due-diligence year)
    137905: ('2021', 'JDE销售订单', '2021-11-11'), # 11.11-11.20.xls (year inferred 2021)
    137906: ('2021', 'JDE销售订单', '2021-12-11'), # 12.11-12.20.xls
    137915: ('2021', 'JDE销售订单', '2021-08-11'), # 8.11-8.20.xls
    137916: ('2021', 'JDE销售订单', '2021-09-01'), # 9.1-9.10.xls
    138002: ('2022', '调档材料', '2022-11-21'),   # 3-陕西纳威调档材料.rar (mtime)
    138005: ('2022', '法务', '2022-01-01'),       # 法务(1).rar (year inferred)
}
# Henan / Hebei due-diligence zips
OTHER_DD = {
    138000: ('2022', '2022-12-30_河南修旺_股权穿透与信用报告.zip'),
    138001: ('2022', '2022-12-30_河北佳轩_关联方与信用报告.zip'),
}
CRED_IDS = {139399, 138941, 138943}

def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

def backup_db():
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    dst = f'/data/nas-curator.db.bak_an4_docs_{ts}'
    s = sqlite3.connect(DB); d = sqlite3.connect(dst)
    s.backup(d); d.close(); s.close()
    print('DB backed up ->', dst, flush=True)
    return dst

def versioned_name(dst_dir, name):
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

def nawei_zip_newname(src):
    """2019.1(1).zip -> 2019-01-01_陕西纳威_税务申报.zip"""
    name = os.path.basename(src)
    m = re.match(r'(\d{4})\.(\d{1,2})', name)
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    return f'{y:04d}-{mo:02d}-01_陕西纳威_税务申报.zip', f'{y:04d}'

def main():
    dry = '--dry-run' in sys.argv
    apply = '--apply' in sys.argv
    if not dry and not apply:
        print('usage: an4_docs_apply.py [--dry-run|--apply]')
        return 2
    db = sqlite3.connect(DB, timeout=60)
    db.execute('PRAGMA busy_timeout=60000')
    rows = db.execute("SELECT id, payload FROM review_queue WHERE status='pending'").fetchall()
    moves = []  # (rid, src, dst, note)
    for rid, payload in rows:
        p = json.loads(payload)
        src = p.get('src') or p.get('src_path') or ''
        ext = os.path.splitext(src)[1].lower()
        base = os.path.basename(src)
        if rid in NAWEI_ZIP_IDS:
            nn = nawei_zip_newname(src)
            if not nn:
                print(f'WARN {rid} zip name unparsed: {base}', flush=True); continue
            newname, year = nn
            dst = os.path.join(NAWEI_DST, year, newname)
            moves.append((rid, src, dst, 'nawei-tax-zip'))
            continue
        if rid in NAWEI_DOCS:
            year, matter, datestr = NAWEI_DOCS[rid]
            newname = f'{datestr}_陕西纳威_{matter}{ext}'
            dst = os.path.join(NAWEI_DST, year, newname)
            moves.append((rid, src, dst, 'nawei-doc'))
            continue
        if rid in OTHER_DD:
            year, newname = OTHER_DD[rid]
            dst = os.path.join(NAWEI_DST, year, newname)
            moves.append((rid, src, dst, 'other-dd'))
            continue
        if rid in CRED_IDS:
            dst = os.path.join(FAM_ROOT, '王震', base)
            moves.append((rid, src, dst, 'credential'))
            continue
        party = (p.get('doc_party') or '').strip()
        if party in ('1-王震', '2-上海润心', '3-北京供应链', '4-辽宁器械'):
            subdir = (p.get('doc_subdir') or '14-工具与其他').strip()
            dst = os.path.join(WORK_ROOT, party, subdir, base)
            moves.append((rid, src, dst, 'determined-doc'))
            continue
    print(f'doc moves={len(moves)}', flush=True)
    print('by note:', Counter(m[3] for m in moves), flush=True)
    print('by dst top:', Counter(os.path.dirname(m[2]) for m in moves).most_common(12), flush=True)
    if dry:
        print('DRY RUN - no changes made', flush=True)
        return 0
    backup_db()
    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    cur = db.execute("INSERT INTO plans (library, rules_version, status, summary) VALUES (?,?,?,?)",
                     ('docs', 1, 'applying', f'an4 docs move {len(moves)} files by content party (2026-11-25 new policy; Nawei->3-北京供应链尽调)'))
    plan_id = cur.lastrowid
    moved, renamed, dup_skip, failed = 0, 0, 0, []
    for rid, src, dst, note in moves:
        if not os.path.exists(src):
            failed.append((rid, src, 'src-missing-at-apply'))
            continue
        dst_dir = os.path.dirname(dst)
        os.makedirs(dst_dir, exist_ok=True)
        final_dst = dst
        action = 'move'
        if os.path.exists(dst):
            try:
                if sha256(src) == sha256(dst):
                    db.execute("UPDATE review_queue SET status='approved', decided_by='an4-docs-dup', decided_at=? WHERE id=?", (now, rid))
                    dup_skip += 1
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
            db.execute("INSERT INTO plan_items (plan_id, src_path, dst_path, category, action, kind, size, ext, sha256, reason, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (plan_id, src, final_dst, note, action, 'doc', size, os.path.splitext(src)[1].lower(), file_sha, note, 'done'))
            db.execute("INSERT INTO operations (plan_id, src_path, dst_path, sha256) VALUES (?,?,?,?)",
                       (plan_id, src, final_dst, file_sha))
            db.execute("UPDATE review_queue SET status='approved', decided_by='an4-docs-move', decided_at=? WHERE id=?", (now, rid))
            moved += 1
        except Exception as e:
            failed.append((rid, src, 'move-err:' + str(e)[:100]))
        if moved % 20 == 0:
            db.commit()
    db.execute("UPDATE plans SET status='done' WHERE id=?", (plan_id,))
    db.commit()
    print(f'DOC plan {plan_id} done: moved={moved} renamed={renamed} dup_skip={dup_skip} failed={len(failed)}', flush=True)
    for f_ in failed:
        print('  FAILED:', f_, flush=True)
    db.close()
    return 0

if __name__ == '__main__':
    sys.exit(main())
