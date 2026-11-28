#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剩余视频任务 步骤 2B-1（容器内）：方舟 agent-plan 看帧重分类。
对 /data/remaining_video_frames/rq_<id>.jpg 逐个调用 agent-plan
（deepseek-v4-1-flash-261017，免费额度），结论写进 review payload
（vlm_category/vlm_confidence/vlm_reason/vlm_model/
 vlm_source='vlm-remaining-frame'，decided_by 同值）。
硬门控：primary 必须为名叫 agent-plan 的 provider 且模型名不含 economy；
无 agent-plan 则直接报错退出；fallback 恒为 None，经济版绝不写进
vlm_category（上次事故教训）。只写结论，不移动、不删除任何文件。
原子条件 vlm_category IS NULL；已有结论的行跳过。
用法：docker exec nas-curator python3 /data/vlm_video_remaining_ark.py \
  --rq-ids <csv> --workers 8 \
  --summary /data/year_vlm_summary_remaining_video_ark.json \
  --heartbeat-id remaining-videos-vlm-ark
"""
import sys, os, json, time, base64, argparse, sqlite3, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, "/app")
sys.path.insert(0, "/data")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

import vlm_reclassify_midconf as V

DB_DEFAULT = "/data/nas-curator.db"
FRAMES_DIR = "/data/remaining_video_frames"

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


_tls = threading.local()


def thread_conn(db_path):
    conn = getattr(_tls, "conn", None)
    if conn is None:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        _tls.conn = conn
    return conn


def pick_agent_plan_only(llm):
    """只取 agent-plan；找不到或模型含 economy 直接报错退出。"""
    ark = [p for p in llm.providers if p.get("kind") == "ark"]
    primary = next((p for p in ark if p.get("name") == "agent-plan"), None)
    if primary is None:
        raise RuntimeError("no agent-plan provider available; refusing to run")
    model = str(primary.get("model") or "")
    if "economy" in model.lower():
        raise RuntimeError("agent-plan resolved to economy model %s; refusing" % model)
    return primary


def write_frame_conclusion(conn, rq_id, cat, conf, reason, model):
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
    d["vlm_source"] = "vlm-remaining-frame"
    cur = conn.execute(
        "UPDATE review_queue SET payload=?, decided_by='vlm-remaining-frame' "
        "WHERE id=? AND status='pending' "
        "AND json_extract(payload,'$.vlm_category') IS NULL",
        (json.dumps(d, ensure_ascii=False), rq_id))
    conn.commit()
    return (cur.rowcount == 1), ("written" if cur.rowcount == 1 else "race_lost")


def process_one(rq_id, src, db_path, primary):
    rec = {"rq_id": rq_id, "src": src}
    t0 = time.time()
    frame = os.path.join(FRAMES_DIR, "rq_%d.jpg" % rq_id)
    if not os.path.exists(frame):
        rec.update({"status": "frame_missing", "error": "no frame file"})
        return rec
    try:
        raw, _o, _r = V.prep_image(frame)
    except Exception as e:
        rec.update({"status": "decode_failed",
                    "error": "%s: %s" % (type(e).__name__, str(e)[:150])})
        return rec
    b64 = base64.b64encode(raw).decode()
    messages = [
        {"role": "system", "content": "只输出 JSON，不要解释。"},
        {"role": "user", "content": [
            {"type": "text", "text": V.build_prompt(os.path.basename(src))},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
        ]},
    ]
    llm = V.thread_llm()
    cat, conf, reason, err = V.try_provider(llm, primary, messages)
    dt = time.time() - t0
    rec["latency_s"] = round(dt, 2)
    if err is not None:
        rec.update({"status": "api_error", "error": err[:200]})
        return rec
    ok, note = write_frame_conclusion(thread_conn(db_path), rq_id, cat, conf, reason, primary["model"])
    rec.update({"status": "ok" if ok else "write_failed", "write_note": note,
                "vlm_cat": cat, "vlm_conf": conf, "vlm_reason": reason,
                "vlm_model": primary["model"], "provider": primary["name"],
                "fallback": False})
    return rec


def main():
    ap = argparse.ArgumentParser(description="剩余视频 Ark agent-plan 看帧 VLM：只写结论")
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--rq-ids", default="", help="逗号分隔的 rq_id")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--summary", default="/data/year_vlm_summary_remaining_video_ark.json")
    ap.add_argument("--heartbeat-id", default="remaining-videos-vlm-ark")
    ap.add_argument("--heartbeat-label", default="剩余视频 agent-plan 看帧")
    args = ap.parse_args()
    rq_ids = [int(x) for x in args.rq_ids.split(",") if x.strip()]
    print("video rq_ids: %d" % len(rq_ids))
    llm = V.LLM()
    primary = pick_agent_plan_only(llm)
    print("primary: %s | model: %s | billing: %s" % (primary["name"], primary["model"], primary.get("billing")))
    print("fallback: none (economy forbidden as final conclusion)")
    conn = sqlite3.connect(args.db, timeout=30)
    to_process = []
    for rid in sorted(rq_ids):
        row = conn.execute("SELECT payload FROM review_queue WHERE id=? AND status='pending'", (rid,)).fetchone()
        if not row:
            print("skip rq=%s row_gone" % rid)
            continue
        d = json.loads(row[0])
        to_process.append((rid, d.get("src", "")))
    conn.close()
    print("to process: %d" % len(to_process))
    progress_dir = os.path.join(os.path.dirname(os.path.abspath(args.db)), "progress")
    _hb(args.heartbeat_id, progress_dir, label=args.heartbeat_label,
        kind="vlm-reclassify", phase="vlm-video", status="running",
        total=len(to_process), done=0)
    t_start = time.time()
    results, done, failed_n, errors = [], 0, 0, []
    workers = max(1, min(16, args.workers))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(process_one, rid, src, args.db, primary): rid for rid, src in to_process}
        for fut in as_completed(futs):
            rec = fut.result()
            results.append(rec)
            done += 1
            if rec.get("status") != "ok":
                failed_n += 1
                errors.append("rq=%s: %s" % (rec.get("rq_id"), (rec.get("error") or rec.get("write_note") or "")[:120]))
            if rec.get("status") == "ok":
                print("OK rq=%s %s(%.2f) %s %.1fs" % (rec["rq_id"], rec["vlm_cat"], rec["vlm_conf"], rec["vlm_model"], rec["latency_s"]))
            else:
                print("%s rq=%s %s" % (rec.get("status"), rec["rq_id"], (rec.get("error", rec.get("write_note", "")))[:120]))
            if done % 50 == 0:
                el = time.time() - t_start
                print("... %d/%d done, %.0fs elapsed, ~%.0fs left" % (done, len(to_process), el, el / done * (len(to_process) - done) if done else 0))
                _hb(args.heartbeat_id, progress_dir, label=args.heartbeat_label,
                    kind="vlm-reclassify", phase="vlm-video", status="running",
                    total=len(to_process), done=done, failed=failed_n, errors=errors)
    total = time.time() - t_start
    ok = [r for r in results if r.get("status") == "ok"]
    summary = {"n": len(results), "ok": len(ok),
               "decode_failed": sum(1 for r in results if r.get("status") == "decode_failed"),
               "api_error": sum(1 for r in results if r.get("status") == "api_error"),
               "frame_missing": sum(1 for r in results if r.get("status") == "frame_missing"),
               "write_failed": sum(1 for r in results if r.get("status") == "write_failed"),
               "fallback_used": 0,
               "ge72": sum(1 for r in ok if r.get("vlm_conf", 0) >= 0.72),
               "ge85": sum(1 for r in ok if r.get("vlm_conf", 0) >= 0.85),
               "avg_latency_s": (round(sum(r.get("latency_s", 0) for r in ok) / len(ok), 2) if ok else None),
               "total_s": round(total, 1),
               "primary": primary["name"], "primary_model": primary["model"],
               "fallback": None,
               "results": results}
    print("SUMMARY " + json.dumps({k: v for k, v in summary.items() if k != "results"}, ensure_ascii=False))
    with open(args.summary, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print("saved " + args.summary)
    _hb(args.heartbeat_id, progress_dir, label=args.heartbeat_label,
        kind="vlm-reclassify", phase="vlm-video", status="done",
        total=len(to_process), done=len(results), failed=failed_n, errors=errors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
