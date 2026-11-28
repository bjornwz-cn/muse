#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剩余文档 72-85 带重跑（remaining-4796，2026-11-25）：只分类、不移动、不删。
范围：24 行（上次 doc-remaining 已定所属方、置信度 0.72<=c<0.85 的 pending 文档行，
rq_id 见 WORKLIST_JSON）。双通道都跑（阿里 qwen4-max＋方舟 agent-plan），
取置信度更高的已定方结论；两边都无法判断则留复核。
结果写 /data/year_siglip_results_remaining4796_rerun.jsonl；
登记由 remaining4796_register_rerun.py 另行执行（只登记 pending 行，
新结论无法判断时保留旧结论）。
Key 只从环境变量 ALIYUN_API_KEY 读，不打印、不落盘。workers=4。"""
import sys, os, json, time, argparse, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

sys.path.insert(0, "/data")
sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

import remaining_docs_classify as base

RESULTS = "/data/year_siglip_results_remaining4796_rerun.jsonl"
WORKLIST_JSON = "/data/remaining4796_rerun_worklist.json"
SUMMARY = "/data/remaining4796_rerun_summary.json"
PROGRESS_DIR = "/data/progress"

try:
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None


def _hb(**kw):
    if _write_hb is None:
        return
    try:
        _write_hb("remaining-4796-docs-rerun", progress_dir=PROGRESS_DIR, **kw)
    except Exception as e:
        print("heartbeat failed: %s" % e, flush=True)


def process_one(item, idx):
    rq_id, path, old_party, old_conf = item
    ext = os.path.splitext(path)[1].lower()
    rec = {'src': path, 'kind': '文档', 'category': '待确认', 'confidence': 0.0,
           'reason': '', 'evidence': '', 'why': '', 'suggested_dst': '',
           'party': '', 'subdir': '', 'rq_id': rq_id,
           'old_party': old_party, 'old_confidence': old_conf}
    try:
        text, extractor, enc = base.extract(path, ext)
    except Exception as e:
        rec.update({'reason': 'doc-extract-failed:%s' % type(e).__name__,
                    'why': 'doc-api_error'})
        return rec
    if enc:
        rec.update({'reason': 'encrypted: %s' % extractor, 'why': 'doc-encrypted'})
        return rec
    ocr_used = False
    if not text.strip():
        if ext == ".pdf":
            otext, oerr = base.ocr_pdf(path)
            if oerr:
                rec['evidence'] = 'extractor=%s text_len=0 ocr_err=%s' % (extractor, oerr)
            elif len(otext.strip()) >= 20:
                text = otext
                ocr_used = True
                extractor = "ocr-service-rapidocr-3p"
        else:
            stext = base.strings_text(path)
            if stext:
                text = stext
                extractor = "strings-fallback"
    rec['evidence'] = 'extractor=%s text_len=%d' % (extractor, len(text))
    if not text.strip():
        rec.update({'reason': 'empty-text: %s' % extractor, 'why': 'doc-empty'})
        return rec
    messages = base.build_messages(text, os.path.basename(path))
    tried = []
    outs = {}
    errs = {}
    for ch in ("aliyun", "ark"):
        tried.append(ch)
        if ch == "aliyun":
            out, err = base.call_aliyun(messages)
        else:
            out, err = base.call_ark(messages)
        if err is not None:
            errs[ch] = err
        else:
            outs[ch] = out
    rec['channels_tried'] = tried
    rec['ocr_used'] = ocr_used
    if not outs:
        rec.update({'reason': 'api_error:%s' % '; '.join(
                        '%s:%s' % (k, v[:120]) for k, v in errs.items())[:200],
                    'why': 'doc-api_error'})
        return rec
    # 取置信度最高的已定方结论；都没有则无法判断（取阿里优先的理由）
    determined = {k: v for k, v in outs.items() if v['party'] != '无法判断'}
    if determined:
        best_ch = max(determined, key=lambda k: determined[k]['confidence'])
        out = determined[best_ch]
        other = [k for k in determined if k != best_ch]
        reason = out['reason']
        if other:
            reason += '（另一通道亦定%s %.2f）' % (
                determined[other[0]]['party'], determined[other[0]]['confidence'])
        rec.update({'party': out['party'], 'subdir': out['subdir'],
                    'doc_model': out.get('model', ''),
                    'doc_models_tried': tried,
                    'reason': ('[%s] %s' % (out.get('channel', '?'), reason))[:300],
                    'evidence': '%s/%s：%s' % (out['party'], out['subdir'] or '-', reason),
                    'why': 'doc-llm-ocr' if ocr_used else 'doc-llm',
                    'category': out['party'],
                    'confidence': round(out['confidence'], 3),
                    'best_channel': best_ch})
    else:
        out = outs.get('aliyun') or outs.get('ark')
        rec.update({'party': '无法判断', 'subdir': '',
                    'doc_model': out.get('model', ''),
                    'doc_models_tried': tried,
                    'reason': ('[%s] %s（两通道一致无法判断）' % (
                        out.get('channel', '?'), out['reason']))[:300],
                    'evidence': out['reason'],
                    'why': 'doc-llm-ocr' if ocr_used else 'doc-llm',
                    'category': '待确认',
                    'confidence': round(out['confidence'], 3)})
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workers', type=int, default=4)
    args = ap.parse_args()
    if not os.environ.get("ALIYUN_API_KEY"):
        print("FATAL: ALIYUN_API_KEY not set in environment", flush=True)
        sys.exit(2)
    llm = base.LLM()
    base.ARK_PRIMARY = base.pick_providers(llm)
    print("ark primary: %s | model: %s | billing: %s" % (
        base.ARK_PRIMARY["name"], base.ARK_PRIMARY["model"],
        base.ARK_PRIMARY.get("billing")), flush=True)
    print("aliyun: model=%s (key from env only)" % base.ALIYUN_MODEL, flush=True)
    with open(WORKLIST_JSON, encoding='utf-8') as f:
        wl = json.load(f)
    items = [(r['rq_id'], r['src'], r['doc_party'], r['confidence']) for r in wl]
    print('worklist items: %d' % len(items), flush=True)
    done = {}
    if os.path.exists(RESULTS):
        with open(RESULTS, encoding='utf-8') as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                    done[d['src']] = d
                except Exception:
                    continue
    todo = [it for it in items if it[1] not in done]
    print('already in results: %d; to process: %d' % (len(done), len(todo)), flush=True)
    if not todo:
        print('nothing to do', flush=True)
        return
    _hb(label='剩余文档72-85带重跑', kind='remaining-4796-rerun', phase='doc',
        status='running', total=len(todo), done=0)
    t0 = time.time()
    n_done = 0
    by_why = Counter()
    upgraded = []
    with open(RESULTS, 'a', encoding='utf-8') as fh:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futs = {ex.submit(process_one, it, i): it for i, it in enumerate(todo)}
            for fut in as_completed(futs):
                try:
                    rec = fut.result()
                except Exception as e:
                    rec = {'src': futs[fut][1], 'kind': '文档', 'category': '待确认',
                           'confidence': 0.0,
                           'reason': 'worker-exception:%s' % type(e).__name__,
                           'evidence': '', 'why': 'doc-api_error',
                           'suggested_dst': '', 'party': '', 'subdir': '',
                           'rq_id': futs[fut][0]}
                fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
                fh.flush()
                n_done += 1
                by_why[rec.get('why')] += 1
                if rec.get('party') and rec['party'] != '无法判断' and rec.get('confidence', 0) >= 0.85:
                    upgraded.append((rec['rq_id'], rec['party'], rec['confidence']))
                if n_done % 10 == 0:
                    print('PROG %d/%d %.0fs why=%s' % (n_done, len(todo), time.time() - t0, dict(by_why)), flush=True)
                    _hb(label='剩余文档72-85带重跑', kind='remaining-4796-rerun',
                        phase='doc', status='running', total=len(todo), done=n_done)
    summary = {'total_worklist': len(items), 'processed': n_done,
               'elapsed_sec': round(time.time() - t0, 1), 'by_why': dict(by_why),
               'stats': dict(base.STATS.c),
               'aliyun_calls': base.STATS.c.get('aliyun_calls', 0),
               'aliyun_prompt_tokens': base.STATS.aliyun_prompt,
               'aliyun_completion_tokens': base.STATS.aliyun_completion,
               'aliyun_total_tokens': base.STATS.aliyun_prompt + base.STATS.aliyun_completion,
               'ark_calls': base.STATS.c.get('ark_calls', 0),
               'upgraded_ge85': upgraded}
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print('DONE %d in %.1fs why=%s' % (n_done, time.time() - t0, dict(by_why)), flush=True)
    print('SUMMARY %s' % json.dumps(summary, ensure_ascii=False), flush=True)
    print('UPGRADED_GE85 %s' % json.dumps(upgraded, ensure_ascii=False), flush=True)
    _hb(label='剩余文档72-85带重跑', kind='remaining-4796-rerun', phase='doc',
        status='done', total=len(todo), done=n_done)


if __name__ == '__main__':
    main()
