#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fix 4 def records whose sidecars were UTF-8-broken (head -c 4000 cut a char),
plus 2 hallucinated doc_dates. Repairs sidecars, re-classifies the 4 files,
fixes 2 new_names, updates /data/side_classify_def.jsonl in place.
"""
import sys, os, json, re
sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")
from curator_core.llmkit import LLM

sys.path.insert(0, "/data")
import importlib.util
spec = importlib.util.spec_from_file_location("scd", "/data/side_classify_def.py")
scd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scd)

TARGETS = {
    "20220420141053741.pdf": 18,
    "金属锁定接骨板系统20173460054.pdf": 89,
    "英全康适儿20130221.pdf": 928,
    "赛福乐临床二折页0121.pdf": 929,
}
FIX_NAMES = {
    "8.1-8.10.xls": "2020-08-01_世龙经略_销售定单明细.xls",
    "BJ-IN PL-SJP200201修改.xls": "2020-02-01_辽宁器械_沙特装箱单发票.xls",
}


def main():
    llm = LLM()
    primary, fallback = scd.scf.pick_providers(llm)
    print("primary: %s | model: %s" % (primary["name"], primary["model"]), flush=True)
    rows = [json.loads(l) for l in open("/data/side_classify_def.jsonl", encoding="utf-8")]
    by_name = {}
    for r in rows:
        by_name[r["filename"]] = r
    # 1. repair sidecars
    for name, idx in TARGETS.items():
        p = "/data/side_text/%d.txt" % idx
        raw = open(p, "rb").read()
        text = raw.decode("utf-8", errors="replace")
        open(p, "w", encoding="utf-8").write(text)
        print("repaired sidecar %d (%s) len=%d" % (idx, name, len(text)), flush=True)
    # 2. re-classify the 4
    for name, idx in TARGETS.items():
        r = by_name.get(name)
        if r is None:
            print("MISS %s" % name, flush=True)
            continue
        text = open("/data/side_text/%d.txt" % idx, encoding="utf-8").read()
        # find extractor from index
        extractor = "pdftotext-3p"
        out, err = scd.doc_classify(llm, primary, fallback, text, name, extractor)
        if err is not None:
            print("APIERR %s %s" % (name, err[:100]), flush=True)
            continue
        r.update({"status": "ok", "method": "doc-text", **out})
        if r.get("party") == "无法判断":
            r["decision"] = "review"
            r["review_reason"] = "无法判断：" + (r.get("reason") or "")
        elif r.get("confidence", 0) < 0.85:
            r["decision"] = "review"
            r["review_reason"] = "低置信（%.2f）：%s" % (r.get("confidence", 0), r.get("reason") or "")
        else:
            r["decision"] = "move"
            nn = scd.build_new_name(r, out)
            if nn:
                r["new_name"] = nn
        r.pop("error", None)
        print("RECLASS %s -> %s/%s conf=%s %s" % (
            name, r.get("party"), r.get("subdir", ""), r.get("confidence"), r["decision"]), flush=True)
    # 3. fix 2 hallucinated dates
    for name, nn in FIX_NAMES.items():
        r = by_name.get(name)
        if r is not None and r.get("new_name"):
            print("FIXNAME %s: %s -> %s" % (name, r["new_name"], nn), flush=True)
            r["new_name"] = nn
    with open("/data/side_classify_def.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    from collections import Counter
    print("SUMMARY " + json.dumps({
        "n": len(rows),
        "move": sum(1 for r in rows if r.get("decision") == "move"),
        "review": sum(1 for r in rows if r.get("decision") == "review"),
        "by_party": dict(Counter(r.get("party") for r in rows if r.get("decision") == "move")),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
