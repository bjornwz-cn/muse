#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实库抽样：无 OCR 时分流分布（跳过/低置信/正常），以及在可判样本上与目录的一致率。"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
from curator_core import engine as C
from curator_core import textkit as TK
from curator_core.llmkit import LLM

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = "/vol1/1000/工作文件/归档/3-北京供应链-已整理"
rules = json.load(open(os.path.join(HERE, "rules_v8.json"), encoding="utf-8"))
cats = rules["categories"]
valid = {c["path"] for c in cats}

random.seed(7)
pool = []
for d in sorted(os.listdir(ROOT)):
    if not os.path.isdir(os.path.join(ROOT, d)) or d not in valid:
        continue
    for dp, dns, fns in os.walk(os.path.join(ROOT, d)):
        dns[:] = [x for x in dns if x not in C.JUNK_NAMES]
        for fn in fns:
            pool.append((d, os.path.join(dp, fn)))
random.shuffle(pool)
pick = pool[:60]

samples = []
for d, fp in pick:
    ext = os.path.splitext(fp)[1].lower()
    try:
        t, how = TK.extract_text(fp, ext, 900, allow_ocr=False)
    except Exception:
        t, how = "", "err"
    samples.append({"truth": d, "name": os.path.basename(fp), "text": t or "", "how": how,
                    "ext": ext, "size": os.path.getsize(fp)})

llm = LLM(timeout=90)
res = llm.classify_batch([(s["name"], s["text"]) for s in samples], cats, chunk=8, min_text=20)
skip = low = ok = 0
for s, r in zip(samples, res):
    if not r:
        s["out"] = "fail"
        continue
    if r.get("skipped"):
        s["out"] = "skip"
        skip += 1
    elif r.get("low_text"):
        s["out"] = "low"
        low += 1
        ok += r["category"] == s["truth"]
    else:
        s["out"] = "normal"
        ok += r["category"] == s["truth"]
n = len(samples)
judge = n - skip
print(f"抽样 {n}（未开 OCR）| 跳过 {skip} ({skip/n:.0%}) | 低置信 {low} | 正常 {n-skip-low}")
print(f"可判 {judge} 条中与目录一致 {ok} = {ok/judge:.0%}")
by_ext = {}
for s in samples:
    by_ext.setdefault(s["ext"], [0, 0])
    by_ext[s["ext"]][1] += 1
    by_ext[s["ext"]][0] += s["out"] == "skip"
print("跳过率按扩展名：", {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_ext.items(), key=lambda x: -x[1][1])[:10]})
json.dump({"n": n, "skip": skip, "low": low, "acc_judgeable": ok / judge if judge else 0,
           "samples": samples,
           "detail": [{"truth": s["truth"], "name": s["name"], "out": s["out"], "pred": r}
                      for s, r in zip(samples, res)]},
          open(os.path.join(HERE, "cache", "realsample_v31.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print(llm.report())
