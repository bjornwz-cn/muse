#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Midconf step 2A (NAS HOST-side, not container: container has no ffmpeg):
For each of the 690 midconf videos, ffprobe duration, ffmpeg -ss duration/2
extract ONE middle frame as JPEG into <DATA>/midconf_frames/rq_<id>.jpg.
Read-only vs DB (reads only /data/midconf_list.tsv for rq_id/src).
Writes only frame JPEGs + frame lists/summary under /data. No DB writes,
no file moves/deletes.
Usage (host): python3 extract_video_frames_midconf.py [--workers 4]
"""
import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

DATA = "/vol2/@appshare/nas-curator/data"
LIST = os.path.join(DATA, "midconf_list.tsv")
FRAMES = os.path.join(DATA, "midconf_frames")
OK_TXT = os.path.join(DATA, "midconf_video_frame_ok.txt")
FAIL_TSV = os.path.join(DATA, "midconf_video_frame_failed.tsv")
SUMMARY = os.path.join(DATA, "midconf_video_frames_summary.json")
LOG = os.path.join(DATA, "midconf_video_frames.log")

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
                "error": "src not on disk", "latency_s": round(time.time()-t0, 2)}
    dur, err = ffprobe_duration(src)
    if err is not None:
        return {"rq_id": rid, "src": src, "status": "probe_failed",
                "error": err, "latency_s": round(time.time()-t0, 2)}
    ss = dur / 2.0
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", "%.3f" % ss, "-i", src,
                        "-frames:v", "1", "-q:v", "2", "-y", out],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return {"rq_id": rid, "src": src, "status": "frame_failed",
                "error": (r.stderr or "ffmpeg failed").strip()[:200],
                "latency_s": round(time.time()-t0, 2)}
    if not os.path.exists(out) or os.path.getsize(out) < 500:
        return {"rq_id": rid, "src": src, "status": "frame_failed",
                "error": "frame too small/missing (%s bytes)" % (os.path.getsize(out) if os.path.exists(out) else -1),
                "latency_s": round(time.time()-t0, 2)}
    return {"rq_id": rid, "src": src, "status": "ok", "frame": out,
            "duration_s": round(dur, 2), "latency_s": round(time.time()-t0, 2)}

def main():
    workers = int(sys.argv[sys.argv.index("--workers")+1]) if "--workers" in sys.argv else 4
    os.makedirs(FRAMES, exist_ok=True)
    rows = []
    with open(LIST, encoding="utf-8") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        idx = {c: i for i, c in enumerate(header)}
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 4 and p[idx["file_kind"]] == "视频":
                rows.append((int(p[idx["rq_id"]]), p[idx["src"]]))
    print("videos: %d" % len(rows), flush=True)
    assert len(rows) == 690, "expected 690 videos, got %d" % len(rows)
    t0 = time.time()
    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, row): row[0] for row in rows}
        for fut in as_completed(futs):
            rec = fut.result()
            results.append(rec)
            done += 1
            if done % 100 == 0:
                print("... %d/%d done %.0fs" % (done, len(rows), time.time()-t0), flush=True)
    results.sort(key=lambda r: r["rq_id"])
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] != "ok"]
    with open(OK_TXT, "w", encoding="utf-8") as f:
        f.write(",".join(str(r["rq_id"]) for r in ok))
    with open(FAIL_TSV, "w", encoding="utf-8") as f:
        f.write("rq_id\tsrc\tstatus\terror\n")
        for r in failed:
            f.write("%d\t%s\t%s\t%s\n" % (r["rq_id"], r["src"], r["status"], (r.get("error") or "").replace("\t", " ").replace("\n", " ")))
    summary = {"n": len(results), "ok": len(ok),
               "src_missing": sum(1 for r in results if r["status"] == "src_missing"),
               "probe_failed": sum(1 for r in results if r["status"] == "probe_failed"),
               "frame_failed": sum(1 for r in results if r["status"] == "frame_failed"),
               "total_s": round(time.time()-t0, 1), "results": results}
    with open(SUMMARY, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    with open(LOG, "w", encoding="utf-8") as f:
        for r in results:
            f.write("%s rq=%d %s\n" % (r["status"], r["rq_id"], (r.get("error") or r.get("frame") or "")))
    print("SUMMARY n=%d ok=%d src_missing=%d probe_failed=%d frame_failed=%d total_s=%.1f" % (
        len(results), len(ok), summary["src_missing"], summary["probe_failed"],
        summary["frame_failed"], summary["total_s"]))
    print("saved %s ; %s ; %s" % (SUMMARY, OK_TXT, FAIL_TSV))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
