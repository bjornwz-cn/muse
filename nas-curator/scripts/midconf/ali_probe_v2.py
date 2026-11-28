#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阿里通道探针 v2（容器内，只读不写库）：
Key 只从 stdin 第一行读（内存使用，绝不打印、不落盘）。
1. GET <base>/models 列出可用模型。
2. 按顺序试视觉模型（qwen3.7-max、qwen4-max、qwen3.8-max、deepseek-v4.1-flash、
   glm-5.3、deepseek-v4.1-flash-1017），各对 1 张照片调一次 chat/completions，
   第一个返回有效类目的即为可用视觉模型。
只打印模型名/类目/置信度/耗时/HTTP 状态，不打印 Key。
用法：docker exec -i nas-curator python3 /data/ali_probe_v2.py < key.txt
"""
import sys, os, json, time, base64, urllib.request, urllib.error

sys.path.insert(0, "/app")
sys.path.insert(0, "/data")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

BASE = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
CANDIDATES = ["qwen3.7-max", "qwen4-max", "qwen3.8-max", "deepseek-v4.1-flash",
              "glm-5.3", "deepseek-v4.1-flash-1017", "qwen3.7-plus", "qwen3.6-flash"]

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
    # 找一张可解码的测试照片
    files = []
    try:
        with open("/data/midconf_list.tsv", encoding="utf-8") as fh:
            _hdr = fh.readline()
            for line in fh:
                p = line.rstrip("\n").split("\t")
                if len(p) >= 4 and p[3] == "照片":
                    files.append(p[1])
    except Exception as e:
        print("cannot read midconf_list.tsv: %s" % e)
    files.sort()
    pick = None
    for cand in files[:20]:
        try:
            V.prep_image(cand)
            pick = cand
            break
        except Exception:
            continue
    if not pick:
        print("PROBE RESULT FAIL: no decodable photo found")
        return 1
    print("probe photo: %s" % pick)
    try:
        raw, _o, _r = V.prep_image(pick)
    except Exception as e:
        print("PROBE RESULT FAIL: decode %s" % e)
        return 1
    b64 = base64.b64encode(raw).decode()
    for model in CANDIDATES:
        if model not in names:
            print("skip %s (not in list)" % model)
            continue
        t0 = time.time()
        cat, conf, reason, err = chat_once(key, model, b64, os.path.basename(pick))
        dt = time.time() - t0
        if err is not None:
            print("FAIL %s: %s (%.1fs)" % (model, err[:200], dt))
        else:
            print("OK %s -> %s(%.2f) %.1fs reason=%s" % (model, cat, conf, dt, reason))
            print("PROBE RESULT PASS vision_model=%s" % model)
            return 0
    print("PROBE RESULT FAIL: no vision model worked")
    return 1

if __name__ == "__main__":
    raise SystemExit(main())
