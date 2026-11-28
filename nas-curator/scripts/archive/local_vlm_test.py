#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地图片分类测试（纯测试，不动生产链路）。

用 NAS 本地 Ollama 视觉模型对样本照片分类，与线上 VLM（DeepSeek-V4.1-Flash）
已有的结论对照准确率，并记录耗时与资源占用。

安全边界：
- 只读 DB（SELECT），绝不 UPDATE review_queue；结果只写 /data 下的独立文件。
- 不改任何配置、不动 Curator 模型链、不重启容器、不动用户文件（只读样本）。
- 与线上 VLM 同口径：同 12 类目、同 prompt、同解码（PIL+heif，长边 1024，JPEG q80）。

用法（NAS 容器内，/data 已挂载）：
  采样： docker exec nas-curator python3 /data/local_vlm_test.py --sample
  试跑： docker exec nas-curator python3 /data/local_vlm_test.py --pilot 5
  全量： docker exec nas-curator python3 /data/local_vlm_test.py --run --workers 1
  环境变量 LOCAL_VLM_MODEL 指定模型（默认 qwen3-vl:8b）。
"""
import sys, os, json, time, base64, io, re, argparse, sqlite3, random, threading
import urllib.request
import urllib.error

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass
from PIL import Image

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
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif"}
DB = "/data/nas-curator.db"
OLLAMA = "http://172.17.0.1:11434"
MODEL = os.environ.get("LOCAL_VLM_MODEL", "qwen3-vl:8b")
SAMPLE_FILE = "/data/local_vlm_test_sample.json"
RESULTS_FILE = os.environ.get("LOCAL_VLM_RESULTS", "/data/local_vlm_test_results.jsonl")
SUMMARY_FILE = os.environ.get("LOCAL_VLM_SUMMARY", "/data/local_vlm_test_summary.json")
N_PER_CAT = 17
SEED = 20261124


def build_prompt(filename):
    cats = "\n".join("- %s：%s" % (c, CAT_DESC[c]) for c in CATS)
    return (
        "你是照片归档专家。看这张照片，判断它属于下面哪个类目（只能选其一，原样输出类目名），"
        "并给出置信度（0-1）。只输出 JSON，不要解释，不要输出其他任何文字。\n"
        "可选类目：\n%s\n\n"
        "文件名：%s\n\n"
        '只输出 JSON：{"category":"类目名","confidence":0.9}'
        % (cats, filename)
    )


def build_prompt_simple(filename):
    cats = "\n".join("- %s" % c for c in CATS)
    return (
        "你是照片归档专家。看这张照片，判断它属于下面哪个类目，只输出类目名，"
        "不要解释，不要输出其他任何文字。\n"
        "可选类目：\n%s\n\n"
        "文件名：%s" % (cats, filename)
    )


PROMPT_VARIANT = os.environ.get("LOCAL_VLM_PROMPT", "json")


def prep_image(src, max_side=1024):
    """与线上 VLM 同款解码。"""
    img = Image.open(src)
    img = img.convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


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


def call_ollama(b64, filename, timeout=900, num_thread=16):
    """调本地 Ollama /api/chat 看图。返回 (content, latency_s, eval_count, prompt_eval_count)。"""
    prompt = (build_prompt_simple(filename) if PROMPT_VARIANT == "simple"
              else build_prompt(filename))
    payload = {
        "model": MODEL,
        "stream": False,
        "think": False,
        "messages": [{"role": "user", "content": prompt,
                      "images": [b64]}],
        "options": {"temperature": 0, "num_thread": num_thread,
                    "num_predict": 32 if PROMPT_VARIANT == "simple" else 768},
    }
    req = urllib.request.Request(
        OLLAMA + "/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError("http_%s:%s" % (e.code, e.read()[:200].decode("utf-8", "ignore")))
    except Exception as e:
        raise RuntimeError("%s:%s" % (type(e).__name__, str(e)[:200]))
    dt = time.time() - t0
    content = (d.get("message") or {}).get("content", "")
    return content, dt, d.get("eval_count"), d.get("prompt_eval_count")


_tls = threading.local()


def resolve_conn():
    conn = getattr(_tls, "conn", None)
    if conn is None:
        conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=30)
        _tls.conn = conn
    return conn


def resolve_src(src):
    """样本文件可能在测试期间被移回流程搬走：src 不在时沿 operations 表
    追踪最新 dst_path（最多 3 跳）。返回 (实际路径, 是否被搬过)。"""
    if os.path.exists(src):
        return src, False
    cur = src
    for _ in range(3):
        try:
            row = resolve_conn().execute(
                "SELECT dst_path FROM operations WHERE src_path=? "
                "ORDER BY id DESC LIMIT 1", (cur,)).fetchone()
        except Exception:
            break
        if not row or not row[0]:
            break
        cur = row[0]
        if os.path.exists(cur):
            return cur, True
    return src, False


def do_sample():
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=30)
    rows = conn.execute(
        "SELECT id, payload FROM review_queue "
        "WHERE json_extract(payload,'$.vlm_category') IS NOT NULL").fetchall()
    conn.close()
    by_cat = {}
    missing = 0
    for rid, payload in rows:
        try:
            d = json.loads(payload)
        except Exception:
            continue
        src = d.get("src", "")
        if os.path.splitext(src)[1].lower() not in IMG_EXTS:
            continue
        vc = d.get("vlm_category")
        if vc not in VALID:
            continue
        if not os.path.exists(src):
            missing += 1
            continue
        by_cat.setdefault(vc, []).append({
            "rq_id": rid, "src": src, "vlm_cat": vc,
            "vlm_conf": d.get("vlm_confidence"),
            "vlm_model": d.get("vlm_model"),
            "siglip_cat": d.get("category"), "siglip_conf": d.get("confidence"),
        })
    rng = random.Random(SEED)
    sample = []
    per_cat = {}
    for cat in CATS:
        pool = by_cat.get(cat, [])
        rng.shuffle(pool)
        take = pool[:N_PER_CAT]
        per_cat[cat] = {"available_on_disk": len(pool), "taken": len(take)}
        sample.extend(take)
    rng.shuffle(sample)
    with open(SAMPLE_FILE, "w", encoding="utf-8") as f:
        json.dump({"model": MODEL, "seed": SEED, "n_per_cat": N_PER_CAT,
                   "per_cat": per_cat, "missing_on_disk": missing,
                   "n": len(sample), "items": sample},
                  f, ensure_ascii=False, indent=1)
    print("sampled %d items -> %s (missing_on_disk=%d)" % (len(sample), SAMPLE_FILE, missing))
    for cat in CATS:
        print("  %-8s avail=%-5d taken=%d" % (cat, per_cat[cat]["available_on_disk"],
                                              per_cat[cat]["taken"]))
    return sample


def load_done():
    done = {}
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    done[r["rq_id"]] = r
                except Exception:
                    pass
    return done


def process_one(item, num_thread):
    src = item["src"]
    real_src, moved = resolve_src(src)
    rec = {"rq_id": item["rq_id"], "src": src, "vlm_cat": item["vlm_cat"],
           "vlm_conf": item.get("vlm_conf"), "siglip_cat": item.get("siglip_cat"),
           "siglip_conf": item.get("siglip_conf"), "model": MODEL}
    if moved:
        rec["resolved_src"] = real_src
    t0 = time.time()
    try:
        raw = prep_image(real_src)
    except Exception as e:
        rec.update({"status": "decode_failed",
                    "error": "%s:%s" % (type(e).__name__, str(e)[:150])})
        return rec
    b64 = base64.b64encode(raw).decode()
    try:
        content, dt, eval_n, prompt_n = call_ollama(
            b64, os.path.basename(item["src"]), num_thread=num_thread)
    except Exception as e:
        rec.update({"status": "api_error", "error": str(e)[:200],
                    "latency_s": round(time.time() - t0, 2)})
        return rec
    rec["latency_s"] = round(dt, 2)
    rec["eval_count"] = eval_n
    rec["prompt_eval_count"] = prompt_n
    rec["raw"] = (content or "")[:300]
    text = (content or "").strip()
    cat = None
    conf = None
    reason = ""
    # 先试裸类目名（simple prompt），再试 JSON
    cand = re.split(r"[：:]", text.split("\n")[0].strip(), maxsplit=1)[0].strip()
    if cand in VALID:
        cat = cand
    else:
        out = parse_json(content)
        if isinstance(out, dict) and "category" in out:
            # Qwen 系常把类目名连同冒号后的描述一起输出（如“人物合影：多个人一起合影”），
            # 只取冒号前作为类目名；线上 VLM 输出裸名，不受影响。
            c2 = re.split(r"[：:]", str(out.get("category", "")).strip(),
                          maxsplit=1)[0].strip()
            if c2 in VALID:
                cat = c2
                try:
                    conf = float(out.get("confidence", 0))
                except Exception:
                    conf = 0.0
                conf = round(max(0.0, min(1.0, conf)), 2)
                reason = str(out.get("reason", ""))[:120]
    if cat is None or cat not in VALID:
        rec.update({"status": "bad_json", "error": "no_cat:" + (content or "")[:120]})
        return rec
    rec.update({"status": "ok", "local_cat": cat, "local_conf": conf,
                "local_reason": reason,
                "agree_vlm": cat == item["vlm_cat"],
                "agree_siglip": cat == item.get("siglip_cat")})
    return rec


def summarize(results, total_s):
    ok = [r for r in results if r.get("status") == "ok"]
    lat = sorted(r["latency_s"] for r in ok)
    def pct(p):
        if not lat:
            return None
        return lat[min(len(lat) - 1, int(len(lat) * p))]
    agree = sum(1 for r in ok if r.get("agree_vlm"))
    agree_s = sum(1 for r in ok if r.get("agree_siglip"))
    conf = {}
    for cat in CATS:
        sub = [r for r in ok if r["vlm_cat"] == cat]
        if sub:
            conf[cat] = {"n": len(sub),
                         "agree": sum(1 for r in sub if r.get("agree_vlm")),
                         "agree_rate": round(sum(1 for r in sub if r.get("agree_vlm")) / len(sub), 3)}
    pairs = {}
    for r in ok:
        if not r.get("agree_vlm"):
            k = "%s->%s" % (r["vlm_cat"], r["local_cat"])
            pairs[k] = pairs.get(k, 0) + 1
    s = {
        "model": MODEL, "n": len(results), "ok": len(ok),
        "agree_vlm": agree,
        "agree_vlm_rate": round(agree / len(ok), 4) if ok else None,
        "agree_siglip": agree_s,
        "agree_siglip_rate": round(agree_s / len(ok), 4) if ok else None,
        "decode_failed": sum(1 for r in results if r.get("status") == "decode_failed"),
        "api_error": sum(1 for r in results if r.get("status") == "api_error"),
        "bad_json": sum(1 for r in results if r.get("status") == "bad_json"),
        "invalid_cat": sum(1 for r in results if r.get("status") == "invalid_cat"),
        "latency_s": {"mean": round(sum(lat) / len(lat), 2) if lat else None,
                      "median": pct(0.5), "p90": pct(0.9), "p95": pct(0.95),
                      "max": lat[-1] if lat else None},
        "total_s": round(total_s, 1),
        "images_per_min": round(len(ok) / (total_s / 60), 2) if total_s else None,
        "per_category": conf,
        "confusion_pairs": dict(sorted(pairs.items(), key=lambda kv: -kv[1])),
        "loadavg_start": None, "loadavg_end": None,
    }
    return s


def main():
    ap = argparse.ArgumentParser(description="本地图片分类测试（只读，不动生产）")
    ap.add_argument("--sample", action="store_true")
    ap.add_argument("--pilot", type=int, default=0, help="只跑前 N 个样本做测速")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--num-thread", type=int, default=16)
    args = ap.parse_args()

    if args.sample:
        do_sample()
        return 0

    if not os.path.exists(SAMPLE_FILE):
        print("no sample file, run --sample first")
        return 2
    with open(SAMPLE_FILE, encoding="utf-8") as f:
        sample = json.load(f)["items"]
    items = sample[:args.pilot] if args.pilot else sample
    if not args.pilot and not args.run:
        print("pass --pilot N or --run")
        return 2

    done = load_done()
    todo = [it for it in items if it["rq_id"] not in done]
    print("model=%s items=%d already_done=%d to_run=%d workers=%d num_thread=%d" % (
        MODEL, len(items), len(done), len(todo), args.workers, args.num_thread))
    if not todo:
        print("nothing to run")
        return 0

    from concurrent.futures import ThreadPoolExecutor, as_completed
    t_start = time.time()
    load_start = os.getloadavg()
    results = list(done.values())
    n0 = len(results)

    def run_one(it):
        return process_one(it, args.num_thread)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(run_one, it): it for it in todo}
        for fut in as_completed(futs):
            rec = fut.result()
            results.append(rec)
            with open(RESULTS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n = len(results) - n0
            if rec.get("status") == "ok":
                print("[%d/%d] rq=%s local=%s(%.2f) vlm=%s agree=%s %.1fs" % (
                    n, len(todo), rec["rq_id"], rec["local_cat"], rec["local_conf"],
                    rec["vlm_cat"], rec.get("agree_vlm"), rec["latency_s"]), flush=True)
            else:
                print("[%d/%d] rq=%s %s %s" % (
                    n, len(todo), rec["rq_id"], rec.get("status"),
                    rec.get("error", "")[:100]), flush=True)
            if n % 25 == 0:
                el = time.time() - t_start
                print("... %d/%d %.0fs elapsed loadavg=%s" % (
                    n, len(todo), el, os.getloadavg()), flush=True)

    total = time.time() - t_start
    s = summarize(results, total)
    s["loadavg_start"] = load_start
    s["loadavg_end"] = os.getloadavg()
    s["results_file"] = RESULTS_FILE
    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    print("SUMMARY " + json.dumps({k: v for k, v in s.items()
                                    if k not in ("per_category", "confusion_pairs")},
                                   ensure_ascii=False))
    print("per_category: " + json.dumps(s["per_category"], ensure_ascii=False))
    print("confusion: " + json.dumps(s["confusion_pairs"], ensure_ascii=False))
    print("saved " + SUMMARY_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
