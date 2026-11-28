#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从视频过筛汇总生成视频确认集（容器内，只读不写库）：
确认集 = 过筛 ok 且 vlm_economy_confidence >= 0.72 的行 + 过筛 api_error 的行。
写 /data/midconf_video_confirm_rqids.txt 与 /data/midconf_video_confirm_set.json。
"""
import json

SUMMARY = "/data/year_vlm_summary_midconf_video_screen.json"
OUT_IDS = "/data/midconf_video_confirm_rqids.txt"
OUT_JSON = "/data/midconf_video_confirm_set.json"

def main():
    s = json.load(open(SUMMARY, encoding="utf-8"))
    results = s["results"]
    ge72 = sorted(r["rq_id"] for r in results
                  if r.get("status") == "ok" and (r.get("vlm_conf") or 0) >= 0.72)
    api_err = sorted(r["rq_id"] for r in results if r.get("status") == "api_error")
    confirm = sorted(set(ge72) | set(api_err))
    with open(OUT_IDS, "w", encoding="utf-8") as f:
        f.write(",".join(str(x) for x in confirm))
    info = {"screen_n": s["n"], "screen_ok": s["ok"],
            "screen_ge72": len(ge72), "screen_api_error": len(api_err),
            "confirm_n": len(confirm),
            "screen_decode_failed": s.get("decode_failed")}
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    print("video confirm set: %d (ge72=%d + api_error=%d)" % (len(confirm), len(ge72), len(api_err)))
    print(json.dumps(info, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
