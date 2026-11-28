#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Year-folder register: write SigLIP results into review_queue (no move).

读 /data/year_siglip_results.jsonl，把每条 SigLIP 结论写进 review_queue：
- 无行的：用 DB.add_review 建行（kind='low_conf'），payload 与管家正常
  流程一致（src/category/confidence/reason/evidence/suggested_dst/tags）。
- 有行的：更新 payload 的 category/confidence/reason/evidence/
  suggested_dst（保留 kind、status、vlm_* 与原有其他键）。
- 只写 DB，不移动、不建 plan、不执行。
- 幂等：重复跑只会覆盖写同一批结论，不会重复建行（建行前查
  has_review_for_src）。

运行（NAS 容器内）：
  docker exec nas-curator python3 /data/year_register.py
输出：建行数、更新行数、跳过数，并把低置信图片的 row id 写到
  /data/year_vlm_rqids.txt（供 vlm_reclassify.py --rq-ids 用）。
"""
import sys, os, json, sqlite3

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

DB_PATH = "/data/nas-curator.db"
RESULTS = "/data/year_siglip_results_stepc.jsonl"
RQIDS_OUT = "/data/stepc_vlm_rqids.txt"
PROGRESS_DIR = "/data/progress"
CONF_HIGH = 0.85
IMG_EXTS = {'.jpg', '.jpeg', '.heic', '.png', '.heif', '.webp', '.gif', '.nef'}

# 0.2.0 心跳：同一任务标识 year-reclassify，阶段 register；写失败不影响主流程。
try:
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None


def _hb(**kw):
    if _write_hb is None:
        return
    try:
        _write_hb("stepc-recheck", progress_dir=PROGRESS_DIR, **kw)
    except Exception as e:  # noqa: BLE001 - 心跳失败只打印，不中断登记
        print("heartbeat failed: %s" % e, flush=True)


def main():
    import common.db as DB
    conn = DB.connect(DB_PATH)
    # 现有行：src -> (id, status, kind, payload)
    existing = {}
    for r in conn.execute(
            "SELECT id, status, kind, payload FROM review_queue "
            "WHERE json_extract(payload,'$.src') LIKE '/vol1/1000/收件箱/%'"):
        try:
            d = json.loads(r[3]) if r[3] else {}
        except Exception:
            d = {}
        src = d.get('src')
        if src:
            existing.setdefault(src, (r[0], r[1], r[2], d))

    # 步骤 C 只更新已有行，绝不建新行（n_created 恒为 0，仅作计数占位）
    n_created = n_updated = n_skipped = n_no_row = n_damaged = n_gone = 0
    low_conf_img_ids = []
    n_total = 0
    import datetime as _dt
    _recheck_at = _dt.datetime.now(
        _dt.timezone(_dt.timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    # 先数总行数，供心跳 total 用（文件约 2.5MB，扫一遍很快）。
    with open(RESULTS, encoding="utf-8", errors="replace") as _fh:
        n_total_lines = sum(1 for _ in _fh if _.strip())
    _hb(label="步骤 C 复检（收件箱已有 review 行）", kind="year-reclassify", phase="register",
        status="running", total=n_total_lines, done=0)
    with open(RESULTS) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                res = json.loads(line)
            except Exception:
                continue
            n_total += 1
            src = res['src']
            why = str(res.get('why') or '')
            is_doc = why.startswith('doc') or why.startswith('unsupported')
            SRC_TAG = 'doc-stepc' if is_doc else 'siglip-stepc'
            DECIDED_BY = 'doc-stepc' if is_doc else 'siglip-stepc'
            kind_cn = res['kind']
            cat = res['category']
            conf = float(res.get('confidence') or 0)
            # tags：真实类目时 [类目, 照片/视频]，否则只 [照片/视频]
            if cat and cat not in ('待确认', '模糊', '疑似重复'):
                tags = [cat, kind_cn]
            else:
                tags = [kind_cn]
            ent = existing.get(src)
            if ent is None:
                # 步骤 C 只更新已有行：结果里无对应行说明快照外文件混入，
                # 跳过不建行（绝不调用 DB.add_review）。
                n_no_row += 1
                continue
            else:
                rid, status, rkind, payload = ent
                # 只在 pending 行上更新结论；已 approved/rejected 的不动
                if status != 'pending':
                    n_skipped += 1
                    continue
                # 保留原结论（可审计、可回退），再写新结论
                payload['recheck_prev_category'] = payload.get('category')
                payload['recheck_prev_confidence'] = payload.get('confidence')
                payload['recheck_prev_reason'] = payload.get('reason')
                payload['recheck_at'] = _recheck_at
                payload['recheck_batch'] = 'stepc'
                if why in ('decode_failed', 'gone', 'classify_failed'):
                    # 解码失败/文件消失：标记损坏（只标记，不删除）
                    payload['recheck_status'] = ('gone' if why == 'gone'
                                                 else 'damaged')
                    payload['recheck_note'] = (
                        '文件已不在盘，复检时源不存在' if why == 'gone' else
                        '解码失败，确认损坏（按 08:02 口径待删，须复核验证＋删除清单，本步不删）')
                    if why == 'gone':
                        n_gone += 1
                    else:
                        n_damaged += 1
                else:
                    payload['recheck_status'] = 'ok'
                    payload['recheck_note'] = ''
                payload['category'] = cat
                payload['confidence'] = conf
                payload['reason'] = res.get('reason', '')
                payload['evidence'] = res.get('evidence', '')
                payload['suggested_dst'] = res.get('suggested_dst', '')
                payload['tags'] = tags
                payload['doc_party'] = res.get('party', '')
                payload['doc_subdir'] = res.get('subdir', '')
                payload['source'] = SRC_TAG
                conn.execute(
                    "UPDATE review_queue SET payload=?, decided_by=? "
                    "WHERE id=? AND status='pending'",
                    (json.dumps(payload, ensure_ascii=False), DECIDED_BY, rid))
                n_updated += 1
                if kind_cn == '照片' and conf < CONF_HIGH and \
                        os.path.splitext(src)[1].lower() in IMG_EXTS and \
                        'vlm_category' not in payload:
                    low_conf_img_ids.append(rid)
    conn.commit()
    # 已有行里低置信且无 vlm 结论的图片也进 VLM 候选（复用的行不在 results 里，
    # 但其中低置信的也需要 VLM；这里只补“本次 SigLIP 新判定为低置信”的行，
    # 复用行若低置信则其 confidence<0.85，已在 payload 里，另行收集）
    conn.close()
    low_conf_img_ids = sorted(set(low_conf_img_ids))
    with open(RQIDS_OUT, 'w') as fh:
        fh.write(','.join(str(i) for i in low_conf_img_ids))
    print('total results: %d' % n_total)
    print('created rows: %d (stepc never creates)' % n_created)
    print('updated rows: %d' % n_updated)
    print('skipped (non-pending): %d' % n_skipped)
    print('no existing row (skipped, not created): %d' % n_no_row)
    print('marked damaged (decode_failed): %d' % n_damaged)
    print('marked gone (src missing at classify time): %d' % n_gone)
    print('low-conf image row ids for VLM: %d -> %s' % (len(low_conf_img_ids), RQIDS_OUT))
    _hb(label="步骤 C 复检（收件箱已有 review 行）", kind="year-reclassify", phase="register",
        status="done", total=n_total_lines, done=n_total,
        extra={"created": n_created, "updated": n_updated,
               "skipped": n_skipped, "no_row": n_no_row,
               "damaged": n_damaged, "gone": n_gone,
               "low_conf_vlm_candidates": len(low_conf_img_ids)})


if __name__ == '__main__':
    main()
