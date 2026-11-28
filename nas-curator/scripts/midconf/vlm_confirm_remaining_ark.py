#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剩余照片 VLM 重分类：方舟 agent-plan 确认（只取 agent-plan，无 fallback）。
范围：--rq-ids-file 指定的 rq_id 清单（照片，步骤 1 完好）。
只写 vlm_category/vlm_confidence/vlm_reason/vlm_model/vlm_source，
decided_by='vlm-remaining-ark'；不动 status。经济版绝不参与。
用法：docker exec -d nas-curator python3 /data/vlm_confirm_remaining_ark.py \
    --rq-ids-file /data/remaining_photos_ark.txt --workers 8 \
    --log /data/year_vlm_remaining_ark.log --summary /data/year_vlm_summary_remaining_ark.json
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
DB_DEFAULT = "/data/nas-curator.db"
SOURCE = "vlm-remaining-ark"

try:
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None


def _hb(task_id, progress_dir, **kw):
    if _write_hb is None:
        return
    try:
        _write_hb(task_id, progress_dir=progress_dir, **kw)
    except Exception as e:
        print("heartbeat failed: %s" % e, flush=True)


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


def pick_providers(llm):
    """确认专用：只用 agent-plan（标准版 free），不设 fallback。"""
    ark = [p for p in llm.providers if p.get("kind") == "ark"]
    primary = next((p for p in ark if p.get("name") == "agent-plan"), None)
    if primary is None:
        primary = next((p for p in ark if p.get("billing") == "free"), None)
    if primary is None:
        raise RuntimeError("no agent-plan provider available")
    return primary, None


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
        conn = sqlite3.connect(db_path, timeout=60)
        conn.execute("PRAGMA busy_timeout=60000")
        _tls.conn = conn
    return conn


def try_provider(llm, prov, messages):
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
    d["vlm_source"] = SOURCE
    cur = conn.execute(
        "UPDATE review_queue SET payload=?, decided_by=? "
        "WHERE id=? AND status='pending' "
        "AND json_extract(payload,'$.vlm_category') IS NULL",
        (json.dumps(d, ensure_ascii=False), SOURCE, rq_id))
    conn.commit()
    return (cur.rowcount == 1), ("written" if cur.rowcount == 1 else "race_lost")


def process_one(rq_id, src, db_path, primary):
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
    dt = time.time() - t0
    rec["latency_s"] = round(dt, 2)
    if err is not None:
        rec.update({"status": "api_error", "error": err[:200]})
        return rec
    ok, note = write_conclusion(thread_conn(db_path), rq_id, cat, conf, reason,
                                primary["model"])
    rec.update({"status": "ok" if ok else "write_failed", "write_note": note,
                "vlm_cat": cat, "vlm_conf": conf, "vlm_reason": reason,
                "vlm_model": primary["model"], "provider": primary["name"]})
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rq-ids-file", required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--log", default="/data/year_vlm_remaining_ark.log")
    ap.add_argument("--summary", default="/data/year_vlm_summary_remaining_ark.json")
    ap.add_argument("--hb-id", default="remaining-photos-vlm-ark")
    args = ap.parse_args()

    want = []
    with open(args.rq_ids_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                want.append(int(line))
    conn = sqlite3.connect(args.db, timeout=60)
    rows = conn.execute(
        "SELECT id, payload FROM review_queue WHERE status='pending' AND id IN (%s) ORDER BY id"
        % ",".join("?" * len(want)), want).fetchall()
    conn.close()
    items, skipped = [], 0
    for rid, payload in rows:
        d = json.loads(payload)
        if "vlm_category" in d:
            skipped += 1
            continue
        items.append((rid, d.get("src", "")))
    print("ark confirm: want=%d found=%d already=%d to_process=%d" % (
        len(want), len(rows), skipped, len(items)), flush=True)
    llm0 = LLM()
    primary, _fb = pick_providers(llm0)
    print("primary: %s | model: %s | billing: %s" % (
        primary.get("name"), primary.get("model"), primary.get("billing")), flush=True)
    _hb(args.hb_id, progress_dir="/data/progress", status="running",
        total=len(items), done=0, phase="vlm-ark")

    t0 = time.time()
    counts = {"ok": 0, "decode_failed": 0, "api_error": 0, "write_failed": 0}
    ge85 = ge72 = 0
    total_lat = 0.0
    with open(args.log, "w", encoding="utf-8") as logf:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(process_one, rid, src, args.db, primary): rid
                    for rid, src in items}
            done = 0
            for fut in as_completed(futs):
                try:
                    rec = fut.result()
                except Exception as e:
                    rec = {"rq_id": futs[fut], "status": "api_error",
                           "error": "%s: %s" % (type(e).__name__, str(e)[:150])}
                counts[rec["status"]] = counts.get(rec["status"], 0) + 1
                if rec["status"] == "ok":
                    total_lat += rec.get("latency_s", 0)
                    if rec.get("vlm_conf", 0) >= 0.85:
                        ge85 += 1
                    if rec.get("vlm_conf", 0) >= 0.72:
                        ge72 += 1
                logf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                done += 1
                if done % 100 == 0:
                    print("ark %d/%d ok=%d api_error=%d decode_failed=%d ge85=%d elapsed=%.0fs" % (
                        done, len(items), counts["ok"], counts["api_error"],
                        counts["decode_failed"], ge85, time.time() - t0), flush=True)
                    _hb(args.hb_id, progress_dir="/data/progress", status="running",
                        total=len(items), done=done, phase="vlm-ark")
    summ = {"n": len(items), "already": skipped, "ok": counts["ok"],
            "decode_failed": counts["decode_failed"], "api_error": counts["api_error"],
            "write_failed": counts["write_failed"], "ge85": ge85, "ge72": ge72,
            "avg_latency_s": round(total_lat / max(1, counts["ok"]), 2),
            "elapsed_sec": round(time.time() - t0, 1),
            "model": primary.get("model"), "provider": primary.get("name"),
            "billing": primary.get("billing")}
    with open(args.summary, "w", encoding="utf-8") as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print("ARK DONE %s" % json.dumps(summ, ensure_ascii=False), flush=True)
    _hb(args.hb_id, progress_dir="/data/progress", status="done",
        total=len(items), done=len(items), phase="vlm-ark")


if __name__ == "__main__":
    main()
