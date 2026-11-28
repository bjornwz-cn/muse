#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抢救分类：138738 .osheet 与 138747 .odoc（原误判加密，实为可读群晖 Office 文档）。
纯 Python 抽文本：文件内嵌 JSON，从 "v"/"text"/"title" 值与中文 runs 拼文本；
双通道（阿里 qwen4-max＋方舟 agent-plan）取高置信度已定方结论。
结果写 /data/year_siglip_results_remaining4796_rescue.jsonl，
登记由 remaining4796_register_rescue.py 执行（decided_by='doc-remaining-rescue'）。
Key 只从环境变量 ALIYUN_API_KEY 读。"""
import sys, os, json, time, re

sys.path.insert(0, "/data")
sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

import remaining_docs_classify as base

RESULTS = "/data/year_siglip_results_remaining4796_rescue.jsonl"
SUMMARY = "/data/remaining4796_rescue_summary.json"
TARGETS = {
    138738: "/vol1/1000/收件箱/99-待确认-辽宁器械/03-无法识别/无标题(2).osheet",
    138747: "/vol1/1000/收件箱/99-待确认-辽宁器械/03-无法识别/防疫物资储备方案.odoc",
}
CAP = 4000


def extract_synology(path):
    """从群晖 Office 容器内嵌 JSON 抽文本值。"""
    with open(path, 'rb') as f:
        data = f.read()
    txt = data.decode('utf-8', errors='ignore')
    vals = []
    for m in re.finditer(r'"(?:v|text|title)"\s*:\s*"((?:[^"\\]|\\.)*)"', txt):
        try:
            v = json.loads('"%s"' % m.group(1))
        except Exception:
            v = m.group(1)
        v = v.strip()
        if v:
            vals.append(v)
    # 兜底：中文 runs
    cjk_runs = re.findall(r'[\u4e00-\u9fff][\u4e00-\u9fff0-9a-zA-Z，。、；：（）《》%·\-/]{3,}', txt)
    blob = '\n'.join(vals)
    if len(blob) < 100:
        blob = (blob + '\n' + '\n'.join(cjk_runs))[:CAP]
    return blob[:CAP], len(vals)


def main():
    if not os.environ.get("ALIYUN_API_KEY"):
        print("FATAL: ALIYUN_API_KEY not set", flush=True)
        sys.exit(2)
    llm = base.LLM()
    base.ARK_PRIMARY = base.pick_providers(llm)
    print("ark primary: %s | model: %s" % (base.ARK_PRIMARY["name"], base.ARK_PRIMARY["model"]), flush=True)
    t0 = time.time()
    out_recs = []
    for rq_id, path in TARGETS.items():
        text, nvals = extract_synology(path)
        rec = {'src': path, 'kind': '文档', 'category': '待确认', 'confidence': 0.0,
               'reason': '', 'evidence': 'extractor=synology-json text_len=%d vals=%d' % (len(text), nvals),
               'why': '', 'suggested_dst': '', 'party': '', 'subdir': '',
               'rq_id': rq_id}
        print('rq %d text_len=%d vals=%d head=%s' % (rq_id, len(text), nvals, text[:80].replace('\n', ' ')), flush=True)
        if len(text.strip()) < 20:
            rec.update({'reason': 'empty-text: synology-json', 'why': 'doc-empty'})
            out_recs.append(rec)
            continue
        messages = base.build_messages(text, os.path.basename(path))
        outs, errs, tried = {}, {}, []
        for ch in ("aliyun", "ark"):
            tried.append(ch)
            out, err = base.call_aliyun(messages) if ch == "aliyun" else base.call_ark(messages)
            if err is not None:
                errs[ch] = err
            else:
                outs[ch] = out
        rec['channels_tried'] = tried
        if not outs:
            rec.update({'reason': 'api_error:%s' % '; '.join('%s:%s' % (k, v[:100]) for k, v in errs.items())[:200],
                        'why': 'doc-api_error'})
            out_recs.append(rec)
            continue
        det = {k: v for k, v in outs.items() if v['party'] != '无法判断'}
        if det:
            best = max(det, key=lambda k: det[k]['confidence'])
            out = det[best]
            rec.update({'party': out['party'], 'subdir': out['subdir'],
                        'doc_model': out.get('model', ''), 'doc_models_tried': tried,
                        'reason': ('[%s] %s' % (out.get('channel', '?'), out['reason']))[:300],
                        'evidence': '%s/%s：%s' % (out['party'], out['subdir'] or '-', out['reason']),
                        'why': 'doc-llm-synology', 'category': out['party'],
                        'confidence': round(out['confidence'], 3), 'best_channel': best})
        else:
            out = outs.get('aliyun') or outs.get('ark')
            rec.update({'party': '无法判断', 'subdir': '', 'doc_model': out.get('model', ''),
                        'doc_models_tried': tried,
                        'reason': ('[%s] %s（两通道一致无法判断）' % (out.get('channel', '?'), out['reason']))[:300],
                        'evidence': out['reason'], 'why': 'doc-llm-synology',
                        'category': '待确认', 'confidence': round(out['confidence'], 3)})
        out_recs.append(rec)
    with open(RESULTS, 'w', encoding='utf-8') as f:
        for r in out_recs:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    summary = {'n': len(out_recs), 'elapsed_sec': round(time.time() - t0, 1),
               'aliyun_calls': base.STATS.c.get('aliyun_calls', 0),
               'aliyun_prompt_tokens': base.STATS.aliyun_prompt,
               'aliyun_completion_tokens': base.STATS.aliyun_completion,
               'ark_calls': base.STATS.c.get('ark_calls', 0),
               'results': [(r['rq_id'], r.get('party'), r.get('confidence')) for r in out_recs]}
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print('RESCUE DONE %s' % json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
