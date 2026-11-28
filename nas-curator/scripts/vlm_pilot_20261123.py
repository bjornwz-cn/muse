#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VLM 试点全量 50 项：走管家自己的 llmkit._call_ark（现成客户端 + 自身配置加载的 Key）
对 review_queue 里的 low_conf 图片做视觉重分类。只读：不写库、不动文件、不建计划。
绝不打印任何 Key 值；日志只记 provider 名/模型/计费档。"""
import sys, os, json, time, base64, io, re
sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass
from PIL import Image
from curator_core.llmkit import LLM

CATS = ["人物合影", "自拍", "儿童成长", "旅行风景", "城市与建筑", "美食与咖啡",
        "日常生活", "宠物与动物", "工作与活动", "屏幕截图", "文档与票据", "梗图与表情"]
CAT_DESC = {
    "人物合影": "多个人一起合影", "自拍": "一个人的自拍照", "儿童成长": "婴儿或儿童照片",
    "旅行风景": "旅行风景、山水海滩", "城市与建筑": "城市街道、建筑外观、夜景",
    "美食与咖啡": "饭菜、蛋糕、咖啡、花束", "日常生活": "家里日常、室内环境、物品特写",
    "宠物与动物": "宠物狗猫、动物园或野外动物", "工作与活动": "会议、公司活动、办公室、工地",
    "屏幕截图": "手机/电脑界面截图、聊天记录、商品页截图",
    "文档与票据": "扫描文件、发票收据、手写笔记、带文字的表格合同",
    "梗图与表情": "网络梗图、表情包、搞笑卡通、插画",
}
VALID = set(CATS)

def prep_image(src, max_side=1024):
    img = Image.open(src)
    img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue(), (w, h), img.size

def build_prompt(filename):
    cats = "\n".join(f"- {c}：{CAT_DESC[c]}" for c in CATS)
    return (
        "你是照片归档专家。看这张照片，判断它属于下面哪个类目（只能选其一，原样输出类目名），"
        "并给出置信度（0-1）与一句话理由。\n"
        f"可选类目：\n{cats}\n\n"
        f"文件名：{filename}\n\n"
        '只输出 JSON：{"category":"类目名","confidence":0.9,"reason":"一句话理由"}'
    )

def parse_json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None

def call_ark_usage(llm, p, messages, timeout=60, max_tokens=300):
    """与 _call_ark 同款请求，另回 usage（仅用于抽查 token 量）。"""
    import requests
    body = {"model": p["model"], "messages": messages, "temperature": 0,
            "max_tokens": max_tokens, "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"}}
    url = p["base_url"] + "/chat/completions"
    hdr = {"Authorization": "Bearer " + p["api_key"], "Content-Type": "application/json"}
    for attempt in range(3):
        r = requests.post(url, json=body, headers=hdr, timeout=timeout)
        if r.status_code != 429:
            break
        time.sleep(min(8.0, 2.0 * (attempt + 1)))
    if r.status_code != 200:
        return None, f"http:{r.status_code}|{(r.text or '')[:200]}", None
    data = r.json()
    msg = ((data.get("choices") or [{}])[0].get("message") or {})
    content = msg.get("content") or ""
    if isinstance(content, list):
        content = "".join(str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content)
    return (content or None), None, data.get("usage")

def main():
    sample = json.load(open("/tmp/vlm_sample.json", encoding="utf-8"))
    llm = LLM()
    ark = [p for p in llm.providers if p["kind"] == "ark"]
    plan = [p for p in ark if p.get("billing") == "free"]
    prov = plan[0] if plan else ark[0]
    print("provider: %s | model: %s | billing: %s | base: %s" % (
        prov["name"], prov["model"], prov.get("billing"), prov.get("base_url")))
    print("n_items:", len(sample))
    results = []
    t_start = time.time()
    usage_samples = []
    for idx, item in enumerate(sample):
        rec = {"rq_id": item["rq_id"], "ext": item["ext"], "src": item["src"],
               "sig_top": item["sig_top"], "sig_score": item["sig_score"],
               "sig_2": item["sig_2"], "sig_2score": item["sig_2score"],
               "reason": item["reason"], "provider": prov["name"], "model": prov["model"]}
        try:
            raw, orig, resized = prep_image(item["src"])
        except Exception as e:
            rec.update({"status": "decode_failed", "error": "%s: %s" % (type(e).__name__, str(e)[:150])})
            results.append(rec)
            print("ITEM %d rq=%s DECODE_FAILED %s" % (idx, item["rq_id"], rec["error"]))
            continue
        rec["orig_size"] = list(orig)
        rec["resized"] = list(resized)
        rec["jpeg_bytes"] = len(raw)
        b64 = base64.b64encode(raw).decode()
        messages = [
            {"role": "system", "content": "只输出 JSON，不要解释。"},
            {"role": "user", "content": [
                {"type": "text", "text": build_prompt(os.path.basename(item["src"]))},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ]},
        ]
        t0 = time.time()
        content, err = llm._call_ark(prov, messages, True, 60, 300)
        dt = time.time() - t0
        rec["latency_s"] = round(dt, 2)
        if err or not content:
            rec.update({"status": "api_error", "error": (err or "empty")[:200]})
            results.append(rec)
            print("ITEM %d rq=%s API_ERROR %s" % (idx, item["rq_id"], rec["error"]))
            continue
        out = parse_json(content)
        if not isinstance(out, dict) or "category" not in out:
            rec.update({"status": "bad_json", "raw": (content or "")[:300]})
            results.append(rec)
            print("ITEM %d rq=%s BAD_JSON %s" % (idx, item["rq_id"], (content or "")[:200]))
            continue
        cat = str(out.get("category", "")).strip()
        try:
            conf = float(out.get("confidence", 0))
        except Exception:
            conf = 0.0
        rec.update({"status": "ok", "vlm_cat": cat, "vlm_conf": round(max(0.0, min(1.0, conf)), 2),
                    "vlm_reason": str(out.get("reason", ""))[:120],
                    "vlm_valid": cat in VALID,
                    "agree_siglip": cat == item["sig_top"]})
        results.append(rec)
        print("ITEM %d rq=%s %s sig=%s(%.2f) vlm=%s(%.2f) agree=%s %.1fs %s" % (
            idx, item["rq_id"], item["ext"], item["sig_top"], item["sig_score"] or 0,
            cat, rec["vlm_conf"], rec["agree_siglip"], dt, rec["vlm_reason"][:40]))
        # 每 10 项抽 1 个取 usage（token 抽查）
        if idx % 10 == 0:
            try:
                _, uerr, usage = call_ark_usage(llm, prov, messages)
                if usage:
                    usage_samples.append({"idx": idx, "usage": usage})
                    print("  usage idx=%d %s" % (idx, json.dumps(usage, ensure_ascii=False)))
            except Exception as e:
                print("  usage-call failed:", str(e)[:120])
    total = time.time() - t_start
    ok = [r for r in results if r.get("status") == "ok"]
    agree = sum(1 for r in ok if r.get("agree_siglip"))
    ge85 = sum(1 for r in ok if r.get("vlm_conf", 0) >= 0.85)
    valid = sum(1 for r in ok if r.get("vlm_valid"))
    summary = {"n": len(results), "ok": len(ok), "agree": agree,
               "agree_rate": round(agree / len(ok), 3) if ok else None,
               "ge85": ge85, "ge85_rate": round(ge85 / len(ok), 3) if ok else None,
               "valid_cat": valid, "total_s": round(total, 1),
               "avg_latency_s": round(sum(r.get("latency_s", 0) for r in ok) / len(ok), 2) if ok else None,
               "usage_samples": usage_samples,
               "provider": prov["name"], "model": prov["model"]}
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False))
    with open("/tmp/vlm_pilot_20261123.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "results": results}, f, ensure_ascii=False, indent=1)
    print("saved /tmp/vlm_pilot_20261123.json")

main()
