#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 回归测试：规则层准确率（新引擎 vs v3.1.1 原版，同数据同口径）。

数据：tests/data/audit_rows_regen.json（152 条，2026-11-19 从
/vol1/1000/工作文件/归档/3-北京供应链-已整理 等口径重建；原 audit_rows.json
已不在 NAS 上，全盘查找无果，此为等口径重建集，非原始 152 条）。
口径：目录名当真值（dir 字段），与 eval_final.py 一致；只比规则层
（best_category，name_first=True），不含 LLM——LLM 部分见 test_full_pipeline_nas.py。
断言：新引擎规则层准确率 ≥ 原版规则层准确率（逻辑未改，理论上相等；
允许 ±1 条浮动，超出即回归失败）。
用法：python3 -m pytest tests/test_regression.py -v
  或：python3 tests/test_regression.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "curator_core"))

DATA = os.path.join(HERE, "data", "audit_rows_regen.json")
RULES_V8 = os.path.join(PROJECT_ROOT, "curator_core", "rules_v8.json")
# 回退：分析副本里的 rules_v8.json
RULES_V8_ALT = os.path.join(os.path.expanduser("~"), "workspace", "analysis",
                            "workfiles-curator", "scripts", "eval", "rules_v8.json")
ORIGINAL_CURATOR = os.path.join(os.path.expanduser("~"), "workspace", "analysis",
                                "workfiles-curator", "scripts")


def _load_rules():
    for p in (RULES_V8, RULES_V8_ALT):
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))
    raise FileNotFoundError("rules_v8.json not found")


def _load_rows():
    d = json.load(open(DATA, encoding="utf-8"))
    return d["rows"]


def _acc(best_category, rows, cats, low):
    ok = judged = 0
    for r in rows:
        cat, _sc, _reason, _ev = best_category(
            r["name"], r["text"], r["ext"], cats, 4, 3, "", low, name_first=True)
        if cat.startswith("99-"):
            continue
        judged += 1
        if cat.split("/")[0] == r["dir"]:
            ok += 1
    return ok, judged


def run():
    rows = _load_rows()
    rules = _load_rules()
    cats = rules["categories"]
    low = rules.get("low_conf_category", "99-待确认/04-低置信")
    # 原版 v3.1.1
    sys.path.insert(0, ORIGINAL_CURATOR)
    import curator as orig
    base_ok, base_judged = _acc(orig.best_category, rows, cats, low)
    # 新引擎
    from curator_core import best_category as new_best
    new_ok, new_judged = _acc(new_best, rows, cats, low)
    print(f"样本 {len(rows)} 条（目录真值）")
    print(f"原版 v3.1.1 规则层：判定 {base_judged}，一致 {base_ok} = {base_ok / base_judged:.1%}" if base_judged else "原版：无判定")
    print(f"新引擎规则层：判定 {new_judged}，一致 {new_ok} = {new_ok / new_judged:.1%}" if new_judged else "新引擎：无判定")
    assert new_judged == base_judged, f"判定条数不一致：新 {new_judged} vs 原 {base_judged}"
    assert new_ok >= base_ok - 1, f"回归失败：新 {new_ok} < 原 {base_ok} - 1"
    print("PASS：规则层准确率不低于 v3.1.1")
    return {"n": len(rows), "baseline_ok": base_ok, "new_ok": new_ok,
            "judged": base_judged}


if __name__ == "__main__":
    run()
