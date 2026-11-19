#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""画像清理：对每类目做「关键词剔除 + 说明收紧 + 样例文件精选」，输出 rules_v8.json 与审计表。

与 v3.1.1 原版 clean_profile.py 的差异（仅为包化所做的最小改动，
判定/清理逻辑不动）：
  - 原版在模块顶层就创建 LLM 并读 rules_v7.json，import 即联网/读盘；
    本版把这些动作移到 main() 里，import 本模块是安全的。
  - `import curator as C` 改为包内相对导入 `from . import engine as C`。
  - clean_one(c, llm)：llm 由调用方传入，不再用模块全局变量。
只在 main() 里跑；不改任何判定逻辑。
"""
from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from . import engine as C
from .llmkit import LLM

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = "/vol1/1000/工作文件/归档/3-北京供应链-已整理"
SRC = os.path.join(HERE, "rules_v7.json")
OUT = os.path.join(HERE, "rules_v8.json")
AUDIT = os.path.join(HERE, "profile_clean_audit.json")


def files_of(cat, root=ROOT):
    p = os.path.join(root, cat)
    out = []
    if not os.path.isdir(p):
        return out
    for dp, dns, fns in os.walk(p):
        dns[:] = [d for d in dns if d not in C.JUNK_NAMES]
        for fn in fns:
            out.append(fn)
    return out


def clean_one(c, llm, root=ROOT):
    cand = C.pick_samples(files_of(c["path"], root), limit=12, pool=12,
                          allow_media=bool(re.search("图片|影像|照片|录音|视频", c["path"])))
    prompt = (
        "你在清理一个文件类目画像，目标是让它能准确判断新文件该不该归到这一类。\n"
        f"类目：{c['path']}\n现有说明：{c.get('desc') or '（无）'}\n"
        f"现有候选关键词：{'、'.join(c.get('keywords') or [])}\n"
        f"该类目下真实文件名候选：{' | '.join(cand) or '（无）'}\n\n"
        "请完成三件事：\n"
        "1) 从候选关键词里保留真正属于本类目核心主题的词（至少3个，最多8个）；"
        "剔除属于其他业务领域的词、纯公司/机构名、碎片词、过泛词（如合同/文件/资料/证明）。禁止新增词。\n"
        "2) 把说明收紧到 60 字内，说清楚「收什么、不收什么」。\n"
        "3) 从候选文件名里挑 3 个最能代表本类目内容的（必须是候选里原样的文件名）；"
        "如果某个文件名明显属于别的类目（例如人事类目里出现产品手册），不要选它。"
        "候选不足 3 个就返回全部。\n"
        '只输出 JSON：{"kept":["词"],"desc":"说明","samples":["文件名"]}'
    )
    out = llm.ask_json(prompt, timeout=90, num_predict=500)
    return c, cand, out


def pick(c, cand, out):
    kws = [k for k in c.get("keywords") or []]
    if isinstance(out, dict):
        kept = [str(x).strip() for x in (out.get("kept") or []) if str(x).strip() in kws]
        kept = list(dict.fromkeys(kept))
        desc = str(out.get("desc") or "").strip()
        smp = [str(x).strip() for x in (out.get("samples") or []) if str(x).strip() in cand]
        smp = list(dict.fromkeys(smp))[:3]
    else:
        kept, desc, smp = [], "", []
    if len(kept) < 3:
        kept = kws
    if not smp:
        smp = cand[:3]
    rec = {
        "path": c["path"],
        "kw_before": kws,
        "kw_after": kept,
        "kw_dropped": [k for k in kws if k not in kept],
        "desc_before": c.get("desc", ""),
        "desc_after": (desc or c.get("desc", ""))[:60],
        "samples_pool": cand,
        "samples": smp,
    }
    return rec


def main(root=ROOT, src=SRC, out_path=OUT, audit_path=AUDIT):
    llm = LLM(timeout=90)
    rules = json.load(open(src, encoding="utf-8"))
    cats = rules["categories"]
    with ThreadPoolExecutor(max_workers=3) as ex:
        res = list(ex.map(lambda c: clean_one(c, llm, root), cats))
    audit = []
    newcats = []
    for c, cand, out in res:
        rec = pick(c, cand, out)
        audit.append(rec)
        nc = dict(c)
        nc["keywords"] = rec["kw_after"]
        nc["desc"] = rec["desc_after"]
        nc["samples"] = rec["samples"]
        newcats.append(nc)
        print(f"{rec['path']}: 词 {len(rec['kw_before'])}->{len(rec['kw_after'])} "
              f"剔除 {'、'.join(rec['kw_dropped']) or '-'} | 样例 {'、'.join(rec['samples'])}")
    rules["categories"] = newcats
    rules["_profile_version"] = "v8-clean"
    rules["_profile_note"] = "关键词经 LLM 语义剔除；每类附 3 个真实样例文件名用于 few-shot 判类"
    json.dump(rules, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(audit, open(audit_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\nsaved:", out_path, audit_path)
    print("provider stats:", {k: v["ok"] for k, v in llm.stats.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
