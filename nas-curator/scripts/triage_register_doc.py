#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤3 文档登记（只登记、不移动、不建 plan）：
读 /data/year_siglip_results_triage.jsonl，把每条文档 LLM 结论写进已有的
pending review 行（按 src 匹配）：更新 category/confidence/reason/evidence/
suggested_dst/tags/doc_party/doc_subdir/doc_model/source/batch，
decided_by='doc-triage'。只动 pending 行；非 pending 的跳过。
幂等：重复跑覆盖写同一批结论。"""
import sys, os, json, sqlite3

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

DB_PATH = "/data/nas-curator.db"
RESULTS = "/data/year_siglip_results_triage.jsonl"
PROGRESS_DIR = "/data/progress"

try:
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None


def _hb(**kw):
    if _write_hb is None:
        return
    try:
        _write_hb("triage-reclassify", progress_dir=PROGRESS_DIR, **kw)
    except Exception as e:
        print("heartbeat failed: %s" % e, flush=True)


def main():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    existing = {}
    for r in conn.execute(
            "SELECT id, status, payload FROM review_queue "
            "WHERE json_extract(payload,'$.src') LIKE '/vol1/1000/收件箱/%'"):
        try:
            d = json.loads(r[2]) if r[2] else {}
        except Exception:
            d = {}
        if d.get('src'):
            existing.setdefault(d['src'], (r[0], r[1], d))
    n_total = n_updated = n_skipped = 0
    with open(RESULTS, encoding="utf-8", errors="replace") as fh:
        lines = [ln for ln in fh if ln.strip()]
    _hb(label="待确认文档重分类", kind="triage-reclassify", phase="register-doc",
        status="running", total=len(lines), done=0)
    for line in lines:
        try:
            res = json.loads(line)
        except Exception:
            continue
        n_total += 1
        src = res['src']
        ent = existing.get(src)
        if ent is None:
            print('no review row for src: %s' % src, flush=True)
            n_skipped += 1
            continue
        rid, status, payload = ent
        if status != 'pending':
            n_skipped += 1
            continue
        cat = res['category']
        conf = float(res.get('confidence') or 0)
        payload['category'] = cat
        payload['confidence'] = conf
        payload['reason'] = res.get('reason', '')
        payload['evidence'] = res.get('evidence', '')
        payload['suggested_dst'] = res.get('suggested_dst', '')
        payload['tags'] = [cat, '文档'] if cat and cat not in ('待确认', '模糊', '疑似重复') else ['文档']
        payload['doc_party'] = res.get('party', '')
        payload['doc_subdir'] = res.get('subdir', '')
        payload['doc_model'] = res.get('doc_model', '')
        payload['source'] = 'doc-triage'
        payload['batch'] = 'triage'
        conn.execute(
            "UPDATE review_queue SET payload=?, decided_by=? "
            "WHERE id=? AND status='pending'",
            (json.dumps(payload, ensure_ascii=False), 'doc-triage', rid))
        n_updated += 1
        if n_total % 50 == 0:
            conn.commit()
            _hb(label="待确认文档重分类", kind="triage-reclassify", phase="register-doc",
                status="running", total=len(lines), done=n_total)
    conn.commit()
    conn.close()
    print('total results: %d updated: %d skipped: %d' % (n_total, n_updated, n_skipped), flush=True)
    _hb(label="待确认文档重分类", kind="triage-reclassify", phase="register-doc",
        status="done", total=len(lines), done=n_total,
        extra={"updated": n_updated, "skipped": n_skipped})
    print('REGISTER-DOC DONE updated=%d skipped=%d' % (n_updated, n_skipped), flush=True)


if __name__ == '__main__':
    main()
