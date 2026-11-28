#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剩余视频任务 步骤 2A：抽帧（NAS 宿主机侧，容器内无 ffmpeg）。
对 /data/remaining_good_videos.tsv 的完好视频，ffprobe 取时长、ffmpeg 取中间帧
为 JPEG 存 /data/remaining_video_frames/rq_<id>.jpg。
只读源文件、不写库；输出帧目录 + /data/remaining_video_frames_summary.json。
用法（宿主机）：python3 /data/remaining_extract_frames.py [--workers 4]
"""
import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

DATA = "/vol2/@appshare/nas-curator/data"
LIST = os.path.join(DATA, "remaining_good_videos.tsv")
FRAMES = os.path.join(DATA, "remaining_video_frames")
SUMMARY = os.path.join(DATA, "remaining_video_frames_summary.json")
LOG = os.path.join(DATA, "remaining_video_frames.log")


def ffprobe_duration(src):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "json", src], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return None, (r.stderr or "ffprobe failed").strip()[:150]
    try:
        d = float(json.loads(r.stdout)["format"]["duration"])
    except Exception as e:
        return None, "bad duration json: %s" % e
    if d <= 0:
        return None, "duration<=0"
    return d, None


def one(row):
    rid, src = row
    out = os.path.join(FRAMES, "rq_%d.jpg" % rid)
    t0 = time.time()
    if not src or not os.path.exists(src):
        return {"rq_id": rid, "src": src, "status": "src_missing",
                "error": "src not on disk", "latency_s": round(time.time() - t0, 2)}
    dur, err = ffprobe_duration(src)
    if err is not None:
        return {"rq_id": rid, "src": src, "status": "probe_failed",
                "error": err, "latency_s": round(time.time() - t0, 2)}
    ss = dur / 2.0
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", "%.3f" % ss, "-i", src,
                        "-frames:v", "1", "-q:v", "2", "-y", out],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return {"rq_id": rid, "src": src, "status": "frame_failed",
                "error": (r.stderr or "ffmpeg failed").strip()[:200],
                "latency_s": round(time.time() - t0, 2)}
    if not os.path.exists(out) or os.path.getsize(out) < 500:
        return {"rq_id": rid, "src": src, "status": "frame_failed",
                "error": "frame too small/missing (%s bytes)" % (os.path.getsize(out) if os.path.exists(out) else -1),
                "latency_s": round(time.time() - t0, 2)}
    return {"rq_id": rid, "src": src, "status": "ok", "frame": out,
            "duration_s": round(dur, 2), "latency_s": round(time.time() - t0, 2)}


def main():
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 4
    os.makedirs(FRAMES, exist_ok=True)
    rows = []
    with open(LIST, encoding="utf-8") as fh:
        hdr = fh.readline().rstrip("\n").split("\t")
        idx = {c: i for i, c in enumerate(hdr)}
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 2 and p[idx["rq_id"]].isdigit():
                rows.append((int(p[idx["rq_id"]]), p[idx["src"]]))
    print("videos to frame: %d" % len(rows), flush=True)
    t0 = time.time()
    results, done = [], 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, row): row[0] for row in rows}
        for fut in as_completed(futs):
            rec = fut.result()
            results.append(rec)
            done += 1
            if done % 100 == 0:
                print("... %d/%d done %.0fs" % (done, len(rows), time.time() - t0), flush=True)
    results.sort(key=lambda r: r["rq_id"])
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] != "ok"]
    summary = {"n": len(results), "ok": len(ok),
               "src_missing": sum(1 for r in results if r["status"] == "src_missing"),
               "probe_failed": sum(1 for r in results if r["status"] == "probe_failed"),
               "frame_failed": sum(1 for r in results if r["status"] == "frame_failed"),
               "total_s": round(time.time() - t0, 1), "results": results}
    with open(SUMMARY, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    with open(LOG, "w", encoding="utf-8") as f:
        for r in results:
            f.write("%s rq=%d %s\n" % (r["status"], r["rq_id"], (r.get("error") or r.get("frame") or "")))
    print("SUMMARY n=%d ok=%d src_missing=%d probe_failed=%d frame_failed=%d total_s=%.1f" % (
        len(results), len(ok), summary["src_missing"], summary["probe_failed"],
        summary["frame_failed"], summary["total_s"]))
    print("saved %s" % SUMMARY)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
