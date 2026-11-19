#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.1.0 判类策略对比：清画像 / few-shot 样例 / 无正文不猜。

A 基线：rules_v7，无样例，无低正文策略（= v3.1 旧行为）
B 清画像：rules_v8，无样例
C 画像+样例：rules_v8 + 每类 3 个样例文件名
D 全量：C + 无正文/机器名直接跳过
"""
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
from curator_core.llmkit import LLM

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache", "samples_41.json")
samples = json.load(open(CACHE, encoding="utf-8"))
v7 = json.load(open(os.path.join(HERE, "rules_v7.json"), encoding="utf-8"))
v8 = json.load(open(os.path.join(HERE, "rules_v8.json"), encoding="utf-8"))


def cats_with(rules, use_samples):
    cats = copy.deepcopy(rules["categories"])
    if not use_samples:
        for c in cats:
            c.pop("samples", None)
    return cats


def strip_self(cats, name):
    """评测防泄漏：待判文件若恰好是某类样例，判它时把该名从样例里去掉。"""
    out = copy.deepcopy(cats)
    for c in out:
        c["samples"] = [s for s in (c.get("samples") or []) if s != name]
    return out


def run(tag, rules, use_samples, min_text):
    llm = LLM(timeout=90)
    base = cats_with(rules, use_samples)
    items = [(s["name"], s["text"]) for s in samples]
    t0 = time.time()
    res = []
    # 41 条样本最多落在若干桶里，桶内共享同一份 cats。
    all_smp = {s for c in base for s in (c.get("samples") or [])}
    buckets = {}
    for i, (n, _t) in enumerate(items):
        key = n if (use_samples and n in all_smp) else ""
        buckets.setdefault(key, []).append(i)
    leak = len([k for k in buckets if k])
    print(f"[{tag}] 分桶 {len(buckets)} 个（受样例泄漏需单独判 {leak} 条）")
    for n, idxs in buckets.items():
        cs = strip_self(base, n) if n else base
        sub = [(items[i][0], items[i][1]) for i in idxs]
        out = llm.classify_batch(sub, cs, chunk=8, min_text=min_text)
        for i, r in zip(idxs, out):
            res.append((i, r))
    res.sort(key=lambda x: x[0])
    res = [r for _, r in res]
    dt = time.time() - t0
    ok = skip = low = 0
    missed = []
    for s, r in zip(samples, res):
        if r and r.get("skipped"):
            skip += 1
            continue
        if r and r.get("low_text"):
            low += 1
        if r and r["category"] == s["truth"]:
            ok += 1
        else:
            missed.append((s["truth"], (r or {}).get("category"), s["name"]))
    judgeable = len(samples) - skip
    acc_all = ok / len(samples)
    acc_j = ok / judgeable if judgeable else 0
    print(f"\n[{tag}] 全量 {len(samples)} | 判对 {ok} | 跳过 {skip} | 低正文 {low}")
    print(f"  准确率(全部) {acc_all:.0%} | 准确率(可判) {acc_j:.0%} | 耗时 {dt:.0f}s "
          f"({dt/len(samples):.1f}s/条) | {llm.report()}")
    if missed:
        print("  误判：")
        for t, p, n in missed:
            print(f"    {t[:12]:14s} -> {(p or '-'):14s} {n[:40]}")
    return {"tag": tag, "acc_all": acc_all, "acc_judgeable": acc_j, "ok": ok,
            "skip": skip, "low": low, "sec": dt, "n": len(samples), "missed": missed,
            "detail": [{"truth": s["truth"], "name": s["name"], "pred": r}
                       for s, r in zip(samples, res)]}


def main():
    plan = [
        ("A 基线 v7/无样例", v7, False, 0),
        ("B 清画像 v8/无样例", v8, False, 0),
        ("C v8+样例", v8, True, 0),
        ("D v8+样例+无正文跳过", v8, True, 20),
    ]
    out = []
    for tag, rules, us, mt in plan:
        out.append(run(tag, rules, us, mt))
    json.dump(out, open(os.path.join(HERE, "cache", "eval_v31_ablation.json"), "w",
                        encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n=== 汇总 ===")
    print(f"{'配置':28s}{'全量':>7s}{'可判':>7s}{'跳过':>6s}{'耗时':>8s}")
    for o in out:
        print(f"{o['tag']:28s}{o['acc_all']:>7.0%}{o['acc_judgeable']:>7.0%}"
              f"{o['skip']:>6d}{o['sec']:>7.0f}s")


if __name__ == "__main__":
    main()
