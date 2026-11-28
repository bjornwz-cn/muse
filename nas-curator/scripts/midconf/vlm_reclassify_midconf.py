#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VLM 全量批处理 阶段 1：只写结论，不自动执行。

对 review_queue 里 pending + kind='low_conf' 的图片行，用视觉大模型
（DeepSeek-V4.1-Flash 标准版，agent-plan）直接看图重分类，把结论写进
review payload；不生成 plan_items、不移动、不删除任何文件、不做自动执行
（阶段 2 另行安排）。

调用链（试点已验证，零改动）：
- 图片解码与试点同口径，等同 media_core/curate.py 的照片解码：
  PIL + pillow_heif（HEIC/HEIF），Image.open + convert("RGB")，
  长边缩到 1024、JPEG q80、base64。
- 直接调 curator_core.llmkit.LLM._call_ark 点名 agent-plan，
  不走 chat() 默认链（它会先试本地 Ollama 纯文本模型，看不见图）。
- agent-plan 失败（报错/空返回/坏 JSON/非法类目）时降级到 model-3
  （经济版）再试一次，并在 vlm_model 里标记实际出结论的模型。
- 429 退避复用 _call_ark 自带的（3 次重试，指数退避）。

断点续跑：按 rq_id 升序；payload 里已有 vlm_category 的行跳过
（不计入 --limit）。重跑即续跑，无需额外状态文件。

写回：只给 payload 新增 4 个键
  vlm_category / vlm_confidence / vlm_reason / vlm_model，
不改任何现有字段，不动 status / decided_by / decided_at。
UPDATE 带原子条件 json_extract(payload,'$.vlm_category') IS NULL，
保证同一行只写一次。

安全：全程不打印、不记录任何 Key 值；日志只记 provider 名/模型/计费档。

运行位置：NAS 容器内（需要 /app 与 /data 挂载）。
  干跑 5 行：
    docker exec nas-curator python3 /data/vlm_reclassify.py \\
        --rq-ids 2501,2553,2554,2563,2566 --workers 2
  全量（阶段 1）：
    docker exec nas-curator python3 /data/vlm_reclassify.py --workers 2
  （全量约 9,182 张，并行 2 路约 1.9 小时；Plan 免费额度 11-25 到期前 0 元，
   过期后按量约 13 元。先确认百度批 apply 已完成再跑全量。）
"""
import sys, os, json, time, base64, io, re, argparse, sqlite3, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

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
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif"}

DB_DEFAULT = "/data/nas-curator.db"

# 0.2.0 心跳：把进度写进 <DB 同目录>/progress/<id>.json 供任务页显示；
# 写失败绝不影响主流程（best-effort）。
try:
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None


def _hb(task_id, progress_dir, **kw):
    if _write_hb is None:
        return
    try:
        _write_hb(task_id, progress_dir=progress_dir, **kw)
    except Exception as e:  # noqa: BLE001 - 心跳失败只打印，不中断批处理
        print("heartbeat failed: %s" % e, flush=True)


def prep_image(src, max_side=1024):
    """与试点同款：PIL + pillow_heif 解码，长边 1024，JPEG q80。"""
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


def pick_providers(llm):
    """主用 agent-plan（免费额度），降级 model-3（经济版按量）。
    按名字点名；名字对不上时按计费档兜底（首个免费 ark / 首个按量 ark）。"""
    ark = [p for p in llm.providers if p.get("kind") == "ark"]
    primary = next((p for p in ark if p.get("name") == "agent-plan"), None)
    if primary is None:
        primary = next((p for p in ark if p.get("billing") == "free"), None)
    fallback = next((p for p in ark if p.get("name") == "model-3"), None)
    if fallback is None:
        fallback = next((p for p in ark
                         if p.get("billing") == "payg" and p is not primary), None)
    if primary is None:
        raise RuntimeError("no ark provider available")
    return primary, fallback


_tls = threading.local()


def thread_llm():
    llm = getattr(_tls, "llm", None)
    if llm is None:
        llm = LLM()
        _tls.llm = llm
    return llm


def thread_conn(db_path):
    conn = getattr(_tls, "conn", None)
    if conn is None:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        _tls.conn = conn
    return conn


def try_provider(llm, prov, messages):
    """一次 _call_ark 调用 + JSON 解析 + 类目校验。
    返回 (cat, conf, reason, err)。err 为 None 表示成功。"""
    content, err = llm._call_ark(prov, messages, True, 60, 300)
    if err or not content:
        return None, None, None, (err or "empty")[:200]
    out = parse_json(content)
    if not isinstance(out, dict) or "category" not in out:
        return None, None, None, "bad_json:" + (content or "")[:120]
    cat = str(out.get("category", "")).strip()
    if cat not in VALID:
        return None, None, None, "invalid_cat:" + cat[:40]
    try:
        conf = float(out.get("confidence", 0))
    except Exception:
        conf = 0.0
    conf = round(max(0.0, min(1.0, conf)), 2)
    return cat, conf, str(out.get("reason", ""))[:120], None


def write_conclusion(conn, rq_id, cat, conf, reason, model):
    """只新增 4 个键；原子条件保证同一行只写一次。返回 (ok, note)。"""
    row = conn.execute(
        "SELECT payload FROM review_queue WHERE id=? AND status='pending'",
        (rq_id,)).fetchone()
    if not row:
        return False, "row_gone"
    d = json.loads(row[0])
    if "vlm_category" in d:
        return False, "already"
    d["vlm_category"] = cat
    d["vlm_confidence"] = conf
    d["vlm_reason"] = reason
    d["vlm_model"] = model
    d['vlm_source'] = 'vlm-midconf'
    cur = conn.execute(
        "UPDATE review_queue SET payload=?, decided_by='vlm-midconf' "
        "WHERE id=? AND status='pending' "
        "AND json_extract(payload,'$.vlm_category') IS NULL",
        (json.dumps(d, ensure_ascii=False), rq_id))
    conn.commit()
    return (cur.rowcount == 1), ("written" if cur.rowcount == 1 else "race_lost")


def process_one(rq_id, src, db_path, primary, fallback):
    """处理一行：解码 → 主模型 →（失败则降级）→ 写回。返回结果记录。"""
    rec = {"rq_id": rq_id, "src": src}
    t0 = time.time()
    try:
        raw, _orig, _resized = prep_image(src)
    except Exception as e:
        rec.update({"status": "decode_failed",
                    "error": "%s: %s" % (type(e).__name__, str(e)[:150])})
        return rec
    b64 = base64.b64encode(raw).decode()
    messages = [
        {"role": "system", "content": "只输出 JSON，不要解释。"},
        {"role": "user", "content": [
            {"type": "text", "text": build_prompt(os.path.basename(src))},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
        ]},
    ]
    llm = thread_llm()
    cat, conf, reason, err = try_provider(llm, primary, messages)
    used = primary
    if err is not None and fallback is not None:
        cat, conf, reason, err2 = try_provider(llm, fallback, messages)
        if err2 is None:
            used = fallback
            err = None
        else:
            err = "primary:%s | fallback:%s" % (err, err2)
    dt = time.time() - t0
    rec["latency_s"] = round(dt, 2)
    if err is not None:
        rec.update({"status": "api_error", "error": err[:200]})
        return rec
    ok, note = write_conclusion(thread_conn(db_path), rq_id, cat, conf, reason,
                                used["model"])
    rec.update({"status": "ok" if ok else "write_failed", "write_note": note,
                "vlm_cat": cat, "vlm_conf": conf, "vlm_reason": reason,
                "vlm_model": used["model"], "provider": used["name"],
                "fallback": used is fallback})
    return rec


def select_candidates(db_path, rq_ids=None, start_after=None, limit=None, kinds=("low_conf",)):
    """取候选行：pending + low_conf + 图片扩展名，按 rq_id 升序。
    返回 (to_process, n_skipped, n_total_images)。
    limit 只计入未写过结论的新行；已写过的跳过不计数。"""
    conn = sqlite3.connect(db_path, timeout=30)
    q = ("SELECT id, payload FROM review_queue "
         "WHERE status='pending' AND kind IN (%s) ORDER BY id"
         % ",".join("?" * len(kinds)))
    rows = conn.execute(q, list(kinds)).fetchall()
    conn.close()
    want = set(rq_ids) if rq_ids else None
    to_process, skipped = [], 0
    n_img = 0
    for rid, payload in rows:
        try:
            d = json.loads(payload)
        except Exception:
            continue
        ext = os.path.splitext(d.get("src", ""))[1].lower()
        if ext not in IMG_EXTS:
            continue
        n_img += 1
        if want is not None and rid not in want:
            continue
        if start_after is not None and rid <= start_after:
            continue
        if "vlm_category" in d:
            skipped += 1
            continue
        to_process.append((rid, d.get("src", "")))
        if limit is not None and len(to_process) >= limit:
            break
    return to_process, skipped, n_img


def main():
    ap = argparse.ArgumentParser(description="VLM 全量批处理 阶段 1：只写结论")
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--rq-ids", default="", help="逗号分隔的 rq_id，仅处理这些行（干跑用）")
    ap.add_argument("--start-after", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None, help="最多处理多少新行")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--summary", default=None)
    ap.add_argument("--kinds", default="low_conf",
                        help="comma-separated review kinds to select")
    ap.add_argument("--heartbeat-id", default="midconf-reclassify",
                    help="心跳任务标识（0.2.0；年份重分类的 VLM 阶段传 year-reclassify）")
    ap.add_argument("--heartbeat-label", default="中置信 VLM 看图重分类")
    args = ap.parse_args()

    rq_ids = [int(x) for x in args.rq_ids.split(",") if x.strip()] or None
    llm = LLM()
    primary, fallback = pick_providers(llm)
    print("primary: %s | model: %s | billing: %s" % (
        primary["name"], primary["model"], primary.get("billing")))
    if fallback is not None:
        print("fallback: %s | model: %s | billing: %s" % (
            fallback["name"], fallback["model"], fallback.get("billing")))
    else:
        print("fallback: none")

    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip()) or ("low_conf",)
    to_process, skipped, n_img = select_candidates(
        args.db, rq_ids, args.start_after, args.limit, kinds)
    print("pending images (kinds=%s): %d | already written (skipped): %d | "
          "to process: %d" % (",".join(kinds), n_img, skipped, len(to_process)))
    if not to_process:
        print("nothing to do")
        return 0

    progress_dir = os.path.join(os.path.dirname(os.path.abspath(args.db)),
                                "progress")
    _hb(args.heartbeat_id, progress_dir, label=args.heartbeat_label,
        kind="vlm-reclassify", phase="vlm", status="running",
        total=len(to_process), done=0, skipped=skipped)

    t_start = time.time()
    results = []
    done = 0
    failed_n = 0
    errors = []
    with ThreadPoolExecutor(max_workers=max(1, min(4, args.workers))) as ex:
        futs = {ex.submit(process_one, rid, src, args.db, primary, fallback): rid
                for rid, src in to_process}
        for fut in as_completed(futs):
            rec = fut.result()
            results.append(rec)
            done += 1
            if rec.get("status") != "ok":
                failed_n += 1
                errors.append("rq=%s: %s" % (
                    rec.get("rq_id"),
                    (rec.get("error") or rec.get("write_note") or "")[:120]))
            if rec.get("status") == "ok":
                print("OK rq=%s %s(%.2f) %s %.1fs%s" % (
                    rec["rq_id"], rec["vlm_cat"], rec["vlm_conf"],
                    rec["vlm_model"], rec["latency_s"],
                    " [fallback]" if rec.get("fallback") else ""))
            else:
                print("%s rq=%s %s" % (rec.get("status"), rec["rq_id"],
                                       rec.get("error", rec.get("write_note", ""))[:120]))
            if done % 100 == 0:
                el = time.time() - t_start
                print("... %d/%d done, %.0fs elapsed, ~%.0fs left" % (
                    done, len(to_process), el,
                    el / done * (len(to_process) - done) if done else 0))
                _hb(args.heartbeat_id, progress_dir,
                    label=args.heartbeat_label, kind="vlm-reclassify",
                    phase="vlm", status="running", total=len(to_process),
                    done=done, skipped=skipped, failed=failed_n,
                    errors=errors)

    total = time.time() - t_start
    ok = [r for r in results if r.get("status") == "ok"]
    ge85 = sum(1 for r in ok if r.get("vlm_conf", 0) >= 0.85)
    summary = {
        "n": len(results),
        "ok": len(ok),
        "skipped_already_written": skipped,
        "decode_failed": sum(1 for r in results if r.get("status") == "decode_failed"),
        "api_error": sum(1 for r in results if r.get("status") == "api_error"),
        "write_failed": sum(1 for r in results if r.get("status") == "write_failed"),
        "fallback_used": sum(1 for r in ok if r.get("fallback")),
        "ge85": ge85,
        "ge85_rate": round(ge85 / len(ok), 3) if ok else None,
        "avg_latency_s": (round(sum(r.get("latency_s", 0) for r in ok) / len(ok), 2)
                          if ok else None),
        "total_s": round(total, 1),
        "primary": primary["name"], "primary_model": primary["model"],
        "fallback": fallback["name"] if fallback else None,
        "results": results,
    }
    print("SUMMARY " + json.dumps({k: v for k, v in summary.items()
                                    if k != "results"}, ensure_ascii=False))
    out = args.summary or ("/data/vlm_reclassify_%s.json"
                           % time.strftime("%Y%m%d_%H%M"))
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print("saved " + out)
    _hb(args.heartbeat_id, progress_dir, label=args.heartbeat_label,
        kind="vlm-reclassify", phase="vlm", status="done",
        total=len(to_process), done=len(results), skipped=skipped,
        failed=failed_n, errors=errors)
    return 0


if __name__ == "__main__":
    sys.exit(main())
