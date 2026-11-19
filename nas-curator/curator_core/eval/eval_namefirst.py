#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A/B：文件名优先（name_first）在规则层与模型层同时开启 vs v3.1.0 现状。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
from curator_core import engine as C
from curator_core.llmkit import LLM

HERE = os.path.dirname(os.path.abspath(__file__))
rows = json.load(open(os.path.join(HERE, "cache", "audit_rows.json"), encoding="utf-8"))
rules = json.load(open(os.path.join(HERE, "rules_v8.json"), encoding="utf-8"))
cats = rules["categories"]
low = "99-待确认/04-低置信"
dfl = rules.get("default", "14-工具与其他")
llm = LLM(timeout=90)


def run(nf):
    out = []
    for r in rows:
        cat, sc, reason, ev = C.best_category(r["name"], r["text"], r["ext"], cats, 4, 3,
                                              "", low, name_first=nf)
        out.append({"cat": cat, "conf": "", "src": "rule", "skipped": False, "low_text": False})
    need = [i for i, o in enumerate(out)
            if o["cat"].startswith("99-") or o["cat"] == dfl]
    if need:
        res = llm.classify_batch([(rows[i]["name"], rows[i]["text"]) for i in need],
                                 cats, chunk=8, min_text=20, parallel=2, name_first=nf)
        for i, m in zip(need, res):
            if not m:
                continue
            if m.get("skipped"):
                out[i].update(cat=low, skipped=True)
            elif m.get("low_text"):
                out[i].update(cat=low if m["confidence"] < 0.55 else m["category"],
                              conf=m["confidence"], src="llm", low_text=True)
            elif m["confidence"] >= 0.55:
                out[i].update(cat=m["category"], conf=m["confidence"], src="llm")
    ok = sum(1 for r, o in zip(rows, out) if not o["cat"].startswith("99-")
             and o["cat"].split("/")[0] == r["dir"])
    judged = sum(1 for o in out if not o["cat"].startswith("99-"))
    return ok, judged, out


q = {}
for nf in (False, True):
    ok, j, out = run(nf)
    q[nf] = out
    tag = "v3.1.1 文件名优先" if nf else "v3.1.0 正文优先"
    print(f"{tag}: 判定 {j}/{len(rows)}，一致 {ok}/{j} = {ok/j:.0%}，全量 {ok/len(rows):.0%}")

# 逐条对比：开启后哪些改判、哪些改对
flip = fix = brk = 0
for r, a, b in zip(rows, q[False], q[True]):
    if a["cat"] == b["cat"]:
        continue
    flip += 1
    ta = a["cat"].split("/")[0] == r["dir"]
    tb = b["cat"].split("/")[0] == r["dir"]
    if tb and not ta:
        fix += 1
    if ta and not tb:
        brk += 1
print(f"改判 {flip} 条：改对 {fix}，改错 {brk}")
json.dump({"rows": [{"dir": r["dir"], "name": r["name"],
                     "old": a["cat"], "new": b["cat"], "conf": b["conf"]}
                    for r, a, b in zip(rows, q[False], q[True]) if a["cat"] != b["cat"]]},
          open(os.path.join(HERE, "cache", "ab_namefirst.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print(llm.report())
