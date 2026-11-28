#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抢救分类登记：读 /data/year_siglip_results_remaining4796_rescue.jsonl，
把 138738/138747 的新结论写进 pending review 行。已定方才更新；
无法判断或 api_error 保留旧结论。decided_by='doc-remaining-rescue'。"""
import sys, os, json, sqlite3

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

DB_PATH = "/data/nas-curator.db"
RESULTS = "/data/year_siglip_results_remaining4796_rescue.jsonl"


def main():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    n_total = n_updated = n_skipped = 0
    with open(RESULTS, encoding="utf-8", errors="replace") as fh:
        lines = [ln for ln in fh if ln.strip()]
    for line in lines:
        try:
            res = json.loads(line)
        except Exception:
            continue
        n_total += 1
        if res.get('why') in ('doc-api_error', 'doc-empty'):
            print('skip %s why=%s' % (res.get('rq_id'), res.get('why')), flush=True)
            n_skipped += 1
            continue
        if res.get('party') in ('', '无法判断'):
            print('skip %s undetermined, old kept' % res.get('rq_id'), flush=True)
            n_skipped += 1
            continue
        row = conn.execute(
            "SELECT id, status, payload FROM review_queue WHERE id=?",
            (res['rq_id'],)).fetchone()
        if row is None or row[1] != 'pending':
            print('skip %s not pending' % res.get('rq_id'), flush=True)
            n_skipped += 1
            continue
        payload = json.loads(row[2]) if row[2] else {}
        cat = res['category']
        payload['category'] = cat
        payload['confidence'] = float(res.get('confidence') or 0)
        payload['reason'] = res.get('reason', '')
        payload['evidence'] = res.get('evidence', '')
        payload['tags'] = [cat, '文档']
        payload['doc_party'] = res.get('party', '')
        payload['doc_subdir'] = res.get('subdir', '')
        payload['doc_model'] = res.get('doc_model', '')
        payload['source'] = 'doc-remaining-rescue'
        payload['batch'] = 'remaining4796-rescue'
        conn.execute(
            "UPDATE review_queue SET payload=?, decided_by=? "
            "WHERE id=? AND status='pending'",
            (json.dumps(payload, ensure_ascii=False), 'doc-remaining-rescue', res['rq_id']))
        n_updated += 1
    conn.commit()
    conn.close()
    print('REGISTER-RESCUE DONE total=%d updated=%d skipped=%d' % (n_total, n_updated, n_skipped), flush=True)


if __name__ == '__main__':
    main()
