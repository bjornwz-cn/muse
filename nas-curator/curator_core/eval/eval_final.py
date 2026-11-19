#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最终配对测试（152 条真实审计样本，规则层同步）：
E 正文优先+样例 ｜ F 文件名优先+样例 ｜ G 文件名优先+无样例(v3.1.1 终版)
"""
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


def run(nf, us):
    out = []
    for r in rows:
        cat, sc, reason, ev = C.best_category(r["name"], r["text"], r["ext"], cats, 4, 3,
                                              "", low, name_first=nf)
        out.append({"cat": cat, "conf": "", "why": reason})
    need = [i for i, o in enumerate(out) if o["cat"].startswith("99-") or o["cat"] == dfl]
    res = llm.classify_batch([(rows[i]["name"], rows[i]["text"]) for i in need], cats,
                             chunk=8, min_text=20, parallel=2, name_first=nf, use_samples=us)
    for i, m in zip(need, res):
        if not m:
            continue
        if m.get("skipped"):
            out[i].update(cat=low, why="no-text")
        elif m.get("low_text") and m["confidence"] < 0.55:
            out[i].update(cat=low, conf=m["confidence"], why="low-text")
        elif m["confidence"] >= 0.55:
            out[i].update(cat=m["category"], conf=m["confidence"], why="llm")
    ok = sum(1 for r, o in zip(rows, out)
             if not o["cat"].startswith("99-") and o["cat"].split("/")[0] == r["dir"])
    judge = sum(1 for o in out if not o["cat"].startswith("99-"))
    return ok, judge, out


store = {}
for tag, nf, us in (("E 正文优先+样例", False, True),
                    ("F 文件名优先+样例", True, True),
                    ("G 文件名优先+无样例", True, False)):
    ok, j, out = run(nf, us)
    store[tag] = out
    print(f"{tag}: 判定 {j}/152，一致 {ok}/{j} = {ok/j:.0%}，全量 {ok/152:.0%}")

a, b = store["E 正文优先+样例"], store["G 文件名优先+无样例"]
flip = fix = brk = 0
for r, x, y in zip(rows, a, b):
    if x["cat"] == y["cat"]:
        continue
    flip += 1
    tx = x["cat"].split("/")[0] == r["dir"]
    ty = y["cat"].split("/")[0] == r["dir"]
    fix += bool(ty and not tx)
    brk += bool(tx and not ty)
print(f"E→G 改判 {flip} 条：改对 {fix}，改错 {brk}")
json.dump({k: [{"dir": r["dir"], "name": r["name"], "cat": o["cat"], "conf": o["conf"], "why": o["why"]}
              for r, o in zip(rows, v)] for k, v in store.items()},
          open(os.path.join(HERE, "cache", "eval_final.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print(llm.report())
