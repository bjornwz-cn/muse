#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch3 Ark agent-plan 探针：取快照里 3 张照片，用 vlm_reclassify_batch3
同一调用链（LLM._call_ark 点名 agent-plan，失败降级 model-3）各调一次，
只打印 provider/模型/类目/置信度/耗时，不写 DB、不移动文件、不留 Key。
用法（NAS 容器内）：docker exec nas-curator python3 /data/ark_probe_batch3.py
"""
import sys, os, json, time, base64, io

sys.path.insert(0, "/app")
sys.path.insert(0, "/data")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

import vlm_reclassify_batch3 as V

SNAPSHOT = "/data/year_batch3_snapshot.tsv"


def main():
    files = []
    with open(SNAPSHOT, encoding="utf-8") as fh:
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 4 and p[3] == "照片":
                files.append(p[0])
    files.sort()
    # 取第 1 / 中间 / 最后 1 张，分散抽样；解码失败则顺延
    picks = []
    for cand in [files[0], files[len(files) // 2], files[-1]] if files else []:
        try:
            V.prep_image(cand)
            picks.append(cand)
        except Exception as e:
            print("probe skip (decode): %s %s" % (cand, type(e).__name__))
        if len(picks) >= 3:
            break
    print("probe photos: %d" % len(picks))
    llm = V.LLM()
    primary, fallback = V.pick_providers(llm)
    print("primary: %s | model: %s | billing: %s" % (
        primary["name"], primary["model"], primary.get("billing")))
    print("fallback: %s | model: %s | billing: %s" % (
        fallback["name"], fallback["model"], fallback.get("billing")) if fallback else "fallback: none")
    ok = 0
    for src in picks:
        try:
            raw, _o, _r = V.prep_image(src)
        except Exception as e:
            print("FAIL decode %s: %s" % (src, e))
            continue
        b64 = base64.b64encode(raw).decode()
        messages = [
            {"role": "system", "content": "只输出 JSON，不要解释。"},
            {"role": "user", "content": [
                {"type": "text", "text": V.build_prompt(os.path.basename(src))},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ]},
        ]
        t0 = time.time()
        cat, conf, reason, err = V.try_provider(llm, primary, messages)
        used = primary["name"]
        if err is not None and fallback is not None:
            cat, conf, reason, err2 = V.try_provider(llm, fallback, messages)
            if err2 is None:
                used = fallback["name"] + " (fallback)"
                err = None
        dt = time.time() - t0
        if err is not None:
            print("FAIL api %s: %s (%.1fs)" % (src, err[:150], dt))
        else:
            ok += 1
            print("OK %s -> %s(%.2f) via %s %.1fs" % (src, cat, conf, used, dt))
    print("PROBE RESULT ok=%d/%d" % (ok, len(picks)))


if __name__ == "__main__":
    main()
