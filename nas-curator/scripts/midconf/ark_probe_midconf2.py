#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Midconf Ark 双通道探针（两阶段门控）：先探 model-3（经济版 payg），
再探 agent-plan（标准版 free），同一调用链（LLM._call_ark 点名），
同一批 3 张照片各调一次。两个通道都通才全量跑；任一不通则停下回执。
只打印 provider/模型/类目/置信度/耗时，不写 DB、不移动文件、不留 Key。
用法（NAS 容器内）：docker exec nas-curator python3 /data/ark_probe_midconf2.py
"""
import sys, os, json, time, base64

sys.path.insert(0, "/app")
sys.path.insert(0, "/data")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

import vlm_reclassify_batch4 as V

SNAPSHOT = "/data/midconf_list.tsv"

def main():
    files = []
    with open(SNAPSHOT, encoding="utf-8") as fh:
        _hdr = fh.readline()
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 4 and p[3] == "照片":
                files.append(p[1])
    files.sort()
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
    ark = [p for p in llm.providers if p.get("kind") == "ark"]
    econ = next((p for p in ark if p.get("name") == "model-3"), None)
    plan = next((p for p in ark if p.get("name") == "agent-plan"), None)
    for prov in (econ, plan):
        if prov is None:
            print("MISSING provider")
        else:
            print("provider: %s | model: %s | billing: %s" % (prov["name"], prov["model"], prov.get("billing")))
    if econ is None or plan is None:
        print("PROBE RESULT FAIL: provider missing")
        return 1
    results = {}
    for prov in (econ, plan):
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
            cat, conf, reason, err = V.try_provider(llm, prov, messages)
            dt = time.time() - t0
            if err is not None:
                print("FAIL api %s via %s: %s (%.1fs)" % (src, prov["name"], err[:150], dt))
            else:
                ok += 1
                print("OK %s -> %s(%.2f) via %s %.1fs" % (src, cat, conf, prov["name"], dt))
        results[prov["name"]] = ok
        print("PROBE %s ok=%d/%d" % (prov["name"], ok, len(picks)))
    gate = all(v == len(picks) for v in results.values()) and len(picks) == 3
    print("PROBE RESULT %s (model-3=%d/3 agent-plan=%d/3)" % ("PASS" if gate else "FAIL", results.get("model-3", 0), results.get("agent-plan", 0)))
    return 0 if gate else 1

if __name__ == "__main__":
    raise SystemExit(main())
