#!/usr/bin/env python3
"""从 remaining-docs 结果中删掉 doc-api_error 行，供断点续跑重试。"""
import json

P = "/data/year_siglip_results_remaining_docs.jsonl"
rows = [json.loads(ln) for ln in open(P, encoding="utf-8") if ln.strip()]
kept = [r for r in rows if r.get("why") != "doc-api_error"]
removed = [r for r in rows if r.get("why") == "doc-api_error"]
print("kept %d removed %d" % (len(kept), len(removed)))
for r in removed:
    print("removed rq=%s src=%s reason=%s" % (
        r.get("rq_id"), r.get("src"), (r.get("reason") or "")[:120]))
with open(P, "w", encoding="utf-8") as f:
    for r in kept:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
