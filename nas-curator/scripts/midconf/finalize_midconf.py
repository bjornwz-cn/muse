#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Midconf收尾（容器内）：把精确的通道调用数/费用估算写进汇总文件，
并把 midconf-reclassify 心跳置为 done。只写汇总 JSON 与心跳文件，不写 DB。
"""
import sys, os, json, sqlite3

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")
try:
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None

SUMMARY = "/data/midconf_reclassify_summary.json"

def main():
    d = json.load(open(SUMMARY, encoding="utf-8"))
    # 精确调用数（顶层尝试次数，不含 _call_ark 内置 429 重试）：
    # agent-plan: 探针1 3 + 探针2 3 + 单阶段误跑 238 + 确认run1 697 + 确认run2 657
    #             + NEF 1 + 视频确认 379 + 重试7 7 = 1985
    # model-3: 探针2 3 + 照片过筛 2253 + 视频过筛 519 + NEF 1 = 2776
    # 阿里: 探针 1 + curl 交叉验证 2 = 3，全部 401，0 成功
    d["channels"] = {
        "ark_agent_plan_calls": 1985,
        "ark_agent_plan_model": "deepseek-v4-1-flash-261017",
        "ark_agent_plan_billing": "free",
        "ark_model3_calls": 2776,
        "ark_model3_model": "deepseek-v4-1-flash-economy-261017",
        "ark_model3_billing": "payg",
        "alibaba_calls": 3,
        "alibaba_successful_calls": 0,
        "alibaba_note": "channel blocked: provided key returned 401 InvalidApiKey on the token-plan endpoint (GET /models) and 401 invalid_api_key on DashScope; 0 classifications from Alibaba",
    }
    d["fees"] = {
        "agent_plan_yuan": 0,
        "agent_plan_note": "billing=free, subscription quota",
        "model3_yuan_estimate": "4-8",
        "model3_estimate_basis": "2776 calls x ~1500 input tokens + ~60 output tokens per call; economy peak 1.60/6.40 yuan per M tokens, off-peak 0.80/3.20 (Ark price page 2026-11-19); actual amount per Ark console billing",
        "alibaba_yuan": 0,
        "total_yuan_estimate": "4-8 (model-3 payg only; agent-plan free; Alibaba 0)",
    }
    d["photo_confirm"] = {
        "run1": {"n": 697, "ok": 696, "api_error": 1, "note": "stopped 06:45 for Alibaba wiring; log overwritten by relaunch, DB authoritative"},
        "run2": {"n": 657, "ok": 650, "api_error": 7, "ge85": 156, "total_s": 332.9},
        "retry7": {"n": 7, "ok": 7, "api_error": 0, "ge85": 2, "total_s": 10.7, "note": "workers 1; all 7 were 429 AccountRateLimitExceeded in run2"},
        "total_ok_unique": 1346 + 7,
        "total_api_error_unique": 0,
    }
    d["keepbest"] = {
        "first_run": {"kept_new": 1047, "restored": 1176, "restored_missing_new": 906, "restored_lower_new": 270, "no_old_with_new": 29, "no_vlm": [124411, 126027]},
        "after_retry7": {"kept_new": 2221, "restored": 2, "restored_lower_new": 2, "no_old_with_new": 30, "no_vlm": [126027]},
        "note": "second keep-best run is an idempotency re-check (restored rows count as kept_new on re-run); first run is the authoritative accounting",
    }
    with open(SUMMARY, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    print("summary channels/fees patched")
    if _write_hb is not None:
        try:
            _write_hb("midconf-reclassify", progress_dir="/data/progress",
                      label="中置信重分类", kind="midconf-reclassify", phase="done",
                      status="done", total=2944, done=2944)
            print("heartbeat midconf-reclassify -> done")
        except Exception as e:
            print("heartbeat failed: %s" % e)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
