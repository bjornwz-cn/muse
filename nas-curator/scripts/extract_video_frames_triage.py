#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤3 视频抽帧（NAS HOST 侧，容器内无 ffmpeg）。
对 /data/triage_video_list.tsv 的 354 个视频：ffprobe 取时长，ffmpeg 取中间帧
为 JPEG 存 <DATA>/triage_frames/rq_<id>.jpg；失败时改取首帧再试一次。
只读 DB（不读库，只读清单），只写帧/清单/汇总，不动用户文件。
"""
import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

DATA = "/vol2/@appshare/nas-curator/data"
LIST = os.path.join(DATA, "triage_video_list.tsv")
FRAMES = os.path.join(DATA, "triage_frames")
OK_TXT = os.path.join(DATA, "triage_video_frame_ok.txt")
FAIL_TSV = os.path.join(DATA, "triage_video_frame_failed.tsv")
SUMMARY = os.path.join(DATA, "triage_video_frames_summary.json")


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


def grab(src, out, ss=None):
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if ss is not None:
        cmd += ["-ss", "%.3f" % ss]
    cmd += ["-i", src, "-frames:v", "1", "-q:v", "2", "-y", out]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return (r.stderr or "ffmpeg failed").strip()[:200]
    if not os.path.exists(out) or os.path.getsize(out) < 500:
        return "frame too small/missing (%s bytes)" % (os.path.getsize(out) if os.path.exists(out) else -1)
    return None


def one(row):
    rid, src = row
    out = os.path.join(FRAMES, "rq_%d.jpg" % rid)
    t0 = time.time()
    if not src or not os.path.exists(src):
        return {"rq_id": rid, "src": src, "status": "src_missing",
                "error": "src not on disk", "latency_s": round(time.time() - t0, 2)}
    dur, err = ffprobe_duration(src)
    if err is not None:
        # ffprobe 失败也试首帧（容器侧 cv2 已证明能读出画面）
        err2 = grab(src, out, ss=None)
        if err2 is None:
            return {"rq_id": rid, "src": src, "status": "ok", "frame": out,
                    "duration_s": None, "latency_s": round(time.time() - t0, 2),
                    "note": "probe-failed-but-first-frame-ok"}
        return {"rq_id": rid, "src": src, "status": "probe_failed",
                "error": err, "latency_s": round(time.time() - t0, 2)}
    err = grab(src, out, ss=dur / 2.0)
    if err is not None:
        err2 = grab(src, out, ss=None)
        if err2 is None:
            return {"rq_id": rid, "src": src, "status": "ok", "frame": out,
                    "duration_s": round(dur, 2), "latency_s": round(time.time() - t0, 2),
                    "note": "mid-frame-failed-first-frame-ok"}
        return {"rq_id": rid, "src": src, "status": "frame_failed",
                "error": err, "latency_s": round(time.time() - t0, 2)}
    return {"rq_id": rid, "src": src, "status": "ok", "frame": out,
            "duration_s": round(dur, 2), "latency_s": round(time.time() - t0, 2)}


def main():
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 4
    os.makedirs(FRAMES, exist_ok=True)
    rows = []
    with open(LIST, encoding="utf-8") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        idx = {c: i for i, c in enumerate(header)}
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 2:
                rows.append((int(p[idx["rq_id"]]), p[idx["src"]]))
    print("videos: %d" % len(rows), flush=True)
    t0 = time.time()
    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, row): row[0] for row in rows}
        for fut in as_completed(futs):
            rec = fut.result()
            results.append(rec)
            done += 1
            if done % 50 == 0:
                print("... %d/%d done %.0fs" % (done, len(rows), time.time() - t0), flush=True)
    results.sort(key=lambda r: r["rq_id"])
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] != "ok"]
    with open(OK_TXT, "w", encoding="utf-8") as f:
        f.write(",".join(str(r["rq_id"]) for r in ok))
    with open(FAIL_TSV, "w", encoding="utf-8") as f:
        f.write("rq_id\tsrc\tstatus\terror\n")
        for r in failed:
            f.write("%d\t%s\t%s\t%s\n" % (r["rq_id"], r["src"], r["status"], (r.get("error") or "").replace("\t", " ").replace("\n", " ")))
    summ = {"n": len(rows), "ok": len(ok), "failed": len(failed),
            "by_status": {s: sum(1 for r in results if r["status"] == s) for s in set(r["status"] for r in results)},
            "elapsed_s": round(time.time() - t0, 1)}
    with open(SUMMARY, "w", encoding="utf-8") as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print("FRAMES DONE %s" % json.dumps(summ, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
