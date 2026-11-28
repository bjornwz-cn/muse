#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阿里通道探针（容器内，只读不写库）：
Key 只从 stdin 第一行读（内存使用，绝不打印、不落盘）。
1. GET <base>/models 列出可用模型。
2. 用 qwen-vl-plus（若不存在则从列表里选一个视觉模型）对 2 张照片
   各调一次 chat/completions（OpenAI 兼容格式，base64 data URL）。
只打印模型名/类目/置信度/耗时/HTTP 状态，不打印 Key。
用法：docker exec -i nas-curator python3 /data/ali_probe_midconf.py < key.txt
"""
import sys, os, json, time, base64, io, urllib.request, urllib.error

sys.path.insert(0, "/app")
sys.path.insert(0, "/data")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

BASE = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"

import vlm_reclassify_batch4 as V

def req_json(key, method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", "Bearer " + key)
    r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"_http_error": e.read().decode()[:300]}
    except Exception as e:
        return 0, {"_error": "%s: %s" % (type(e).__name__, str(e)[:200])}

def chat_once(key, model, b64, filename):
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "只输出 JSON，不要解释。"},
            {"role": "user", "content": [
                {"type": "text", "text": V.build_prompt(filename)},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ]},
        ],
    }
    st, out = req_json(key, "POST", "/chat/completions", body)
    if st != 200:
        return None, None, None, "http_%s: %s" % (st, json.dumps(out, ensure_ascii=False)[:200])
    try:
        content = out["choices"][0]["message"]["content"]
    except Exception:
        return None, None, None, "bad_response: " + json.dumps(out, ensure_ascii=False)[:200]
    parsed = V.parse_json(content)
    if not isinstance(parsed, dict) or parsed.get("category") not in V.VALID:
        return None, None, None, "bad_json_or_cat: " + (content or "")[:150]
    return parsed["category"], round(max(0.0, min(1.0, float(parsed.get("confidence", 0)))), 2), str(parsed.get("reason", ""))[:120], None

def main():
    key = sys.stdin.readline().strip()
    if not key:
        print("PROBE RESULT FAIL: no key on stdin")
        return 1
    st, models = req_json(key, "GET", "/models")
    print("GET /models http=%s" % st)
    names = []
    if st == 200:
        names = [m.get("id") for m in models.get("data", []) if m.get("id")]
        print("models: %s" % json.dumps(names, ensure_ascii=False))
    else:
        print("models error: %s" % json.dumps(models, ensure_ascii=False)[:300])
        print("PROBE RESULT FAIL: cannot list models")
        return 1
    model = "qwen-vl-plus"
    if model not in names:
        cands = [n for n in names if "vl" in n.lower() or "vision" in n.lower()]
        print("qwen-vl-plus NOT in list; vision candidates: %s" % cands)
        if not cands:
            print("PROBE RESULT FAIL: no vision model available")
            return 1
        model = cands[0]
    print("probe model: %s" % model)
    files = []
    with open("/data/midconf_list.tsv", encoding="utf-8") as fh:
        _hdr = fh.readline()
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 4 and p[3] == "照片":
                files.append(p[1])
    files.sort()
    picks = []
    for cand in [files[0], files[len(files) // 2]] if files else []:
        try:
            V.prep_image(cand)
            picks.append(cand)
        except Exception as e:
            print("probe skip (decode): %s %s" % (cand, type(e).__name__))
        if len(picks) >= 2:
            break
    print("probe photos: %d" % len(picks))
    ok = 0
    for src in picks:
        try:
            raw, _o, _r = V.prep_image(src)
        except Exception as e:
            print("FAIL decode %s: %s" % (src, e))
            continue
        b64 = base64.b64encode(raw).decode()
        t0 = time.time()
        cat, conf, reason, err = chat_once(key, model, b64, os.path.basename(src))
        dt = time.time() - t0
        if err is not None:
            print("FAIL api %s: %s (%.1fs)" % (src, err[:200], dt))
        else:
            ok += 1
            print("OK %s -> %s(%.2f) via alibaba/%s %.1fs" % (src, cat, conf, model, dt))
    print("PROBE RESULT %s (alibaba/%s ok=%d/%d)" % ("PASS" if ok == len(picks) and picks else "FAIL", model, ok, len(picks)))
    return 0 if (ok == len(picks) and picks) else 1

if __name__ == "__main__":
    raise SystemExit(main())
