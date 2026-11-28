#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""remaining-4796 照片：建清单（只读）。
扫描 pending 照片行，按 remaining_good_summary.py 同口径算 final 结论：
- vlm_category 存在且 vlm_model 不含 economy 且 vlm_confidence>=0.72 -> 用 vlm；
- 否则用 category/confidence（siglip）。
band: ge85 / b72_85 / b35_72 / none(<0.35)。
真实类目 = 12 类目之一；"无法判断" 不算真实类目。
输出 /data/remaining4796_photos.tsv 与 /data/remaining4796_photos_summary.json，
另出 72-85 带清单 /data/remaining4796_72_85_ids.txt（排除 20200614、rq 149995、
rq 150025、CR2 rq 139386、凭据文件），以及加密 25 行与无法定类目候选统计。
用法：docker exec nas-curator python3 /data/remaining4796_build_list.py
"""
import json, os, sqlite3

DB = '/data/nas-curator.db'
OUT_TSV = '/data/remaining4796_photos.tsv'
OUT_JSON = '/data/remaining4796_photos_summary.json'
BAND_IDS = '/data/remaining4796_72_85_ids.txt'

PHOTO_EXTS = {'.jpg', '.jpeg', '.heic', '.heif', '.png', '.gif', '.tiff',
              '.cr2', '.webp', '.nef', '.bmp'}
REAL_CATS = {"人物合影", "自拍", "儿童成长", "旅行风景", "城市与建筑", "美食与咖啡",
             "日常生活", "宠物与动物", "工作与活动", "屏幕截图", "文档与票据", "梗图与表情"}
EXCLUDE_RQ = {149995, 150025, 139386}
CRED_RQ = {138941, 138943, 139432}


def final_of(d):
    vmodel = str(d.get("vlm_model") or "")
    if "vlm_category" in d and "economy" not in vmodel.lower():
        try:
            vconf = float(d.get("vlm_confidence") or 0)
        except Exception:
            vconf = 0.0
        if vconf >= 0.72:
            return d["vlm_category"], vconf, "vlm:" + vmodel
    cat = d.get("category")
    try:
        conf = float(d.get("confidence") or 0)
    except Exception:
        conf = 0.0
    return cat, conf, "siglip"


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
    by_ext = {}
    band = {"ge85": 0, "b72_85": 0, "b35_72": 0, "none": 0}
    enc_rows = []
    for rid, kind, payload in rows:
        try:
            d = json.loads(payload)
        except Exception:
            continue
        src = d.get('src', '') or ''
        ext = os.path.splitext(src)[1].lower()
        if ext not in PHOTO_EXTS:
            continue
        by_ext[ext] = by_ext.get(ext, 0) + 1
        fcat, fconf, fsrc = final_of(d)
        if fconf >= 0.85:
            b = "ge85"
        elif fconf >= 0.72:
            b = "b72_85"
        elif fconf >= 0.35:
            b = "b35_72"
        else:
            b = "none"
        band[b] += 1
        out.append({'rq_id': rid, 'src': src, 'kind': kind,
                    'final_cat': fcat, 'final_conf': round(fconf, 2),
                    'final_src': fsrc, 'band': b,
                    'vlm_cat': d.get('vlm_category'), 'vlm_conf': d.get('vlm_confidence'),
                    'vlm_model': d.get('vlm_model'), 'vlm_source': d.get('vlm_source'),
                    'sig_cat': d.get('category'), 'sig_conf': d.get('confidence')})
        # 加密 25 行核对（上次确认 rq 138768-138792）
        if 138768 <= rid <= 138792:
            enc_rows.append((rid, src, kind, fcat, fconf))
    print('pending photos: %d by_ext=%s band=%s' % (
        len(out), json.dumps(by_ext, ensure_ascii=False), json.dumps(band)), flush=True)
    print('enc range rows in pending photos: %d' % len(enc_rows), flush=True)
    for r in enc_rows:
        print('  enc %s' % (r,), flush=True)
    with open(OUT_TSV, 'w', encoding='utf-8') as f:
        f.write('rq_id\tsrc\tkind\tfinal_cat\tfinal_conf\tfinal_src\tband\tvlm_cat\tvlm_conf\tvlm_model\tvlm_source\tsig_cat\tsig_conf\n')
        for r in out:
            f.write('%d\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' % (
                r['rq_id'], r['src'], r['kind'], r['final_cat'], r['final_conf'],
                r['final_src'], r['band'], r['vlm_cat'], r['vlm_conf'],
                r['vlm_model'], r['vlm_source'], r['sig_cat'], r['sig_conf']))
    # 72-85 带：真实类目，排除口径
    band_ids = [r['rq_id'] for r in out
                if r['band'] == 'b72_85' and r['final_cat'] in REAL_CATS
                and r['rq_id'] not in EXCLUDE_RQ and r['rq_id'] not in CRED_RQ
                and '20200614' not in r['src']]
    excluded_band = [r['rq_id'] for r in out
                     if r['band'] == 'b72_85' and r['final_cat'] not in REAL_CATS]
    print('b72_85 total=%d real-cat=%d excluded-nonreal=%d' % (
        sum(1 for r in out if r['band'] == 'b72_85'), len(band_ids),
        len(excluded_band)), flush=True)
    print('b72_85 non-real-cat sample: %s' % (excluded_band[:20],), flush=True)
    with open(BAND_IDS, 'w', encoding='utf-8') as f:
        f.write('\n'.join(str(x) for x in band_ids) + '\n')
    # 无法定类目候选：final conf<0.35 且 final_cat 为 无法判断/空（加密 25 除外，另行处理）
    undet = [r for r in out if r['band'] == 'none'
             and (r['final_cat'] in (None, '', '无法判断'))
             and not (138768 <= r['rq_id'] <= 138792)]
    # 其中 vlm 与 siglip 双结论情况统计
    undet_both_low = [r for r in undet
                      if (r['vlm_conf'] is None or float(r['vlm_conf'] or 0) < 0.35)
                      and (r['sig_conf'] is None or float(r['sig_conf'] or 0) < 0.35)]
    print('undeterminable candidates (band none + cat 无法判断/空, excl enc25): %d ; both-vlm-siglip<0.35: %d' % (
        len(undet), len(undet_both_low)), flush=True)
    # ge85 明细（供核对：上次 188 张是否仍在）
    ge85_ids = [r['rq_id'] for r in out if r['band'] == 'ge85']
    summ = {'pending_total': len(rows), 'pending_photos': len(out),
            'by_ext': by_ext, 'band': band,
            'b72_85_realcat_ids': len(band_ids),
            'b72_85_nonreal_excluded': len(excluded_band),
            'undet_candidates': len(undet),
            'undet_both_low': len(undet_both_low),
            'enc_range_rows': len(enc_rows),
            'ge85_ids': ge85_ids}
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print('SUMMARY ' + json.dumps({k: v for k, v in summ.items() if k != 'ge85_ids'},
                                   ensure_ascii=False), flush=True)
    print('ge85 count=%d' % len(ge85_ids), flush=True)
    db.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
