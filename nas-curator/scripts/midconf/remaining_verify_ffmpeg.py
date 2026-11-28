#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剩余视频任务 步骤 1A（NAS 宿主机侧，容器内无 ffmpeg）：
对 /data/remaining_videos.tsv 全部视频逐个全量验证：
- ffprobe -count_frames 取首个视频流总帧数（nb_read_frames；无则 nb_frames；再无则 duration*avg_fps 估算，记 total_source）
- ffmpeg -nostdin 全量解码到 null，取最终 frame= N 为解出帧数
- sha256 + size + mtime
结论 ff_verdict：good（解出==总数且 rc==0）/ damaged（解出<总数或 rc!=0 或解出 0）
  / missing（源不在盘）/ novideo（无视频流）/ probe_failed
只读源文件、不写库；输出 /data/remaining_verify_ffmpeg.tsv 与
/data/remaining_verify_ffmpeg_summary.json；写心跳 /data/progress/remaining-videos-verify.json。
用法（宿主机）：python3 /data/remaining_verify_ffmpeg.py [--workers 6]
"""
import argparse, hashlib, json, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

DATA = "/vol2/@appshare/nas-curator/data"
LIST = os.path.join(DATA, "remaining_videos.tsv")
OUT_TSV = os.path.join(DATA, "remaining_verify_ffmpeg.tsv")
OUT_JSON = os.path.join(DATA, "remaining_verify_ffmpeg_summary.json")
HB = os.path.join(DATA, "progress", "remaining-videos-verify.json")
FRAME_RE = re.compile(r"frame=\s*(\d+)")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe_total(src):
    """返回 (total_frames:int|None, source:str, err:str|None)。
    先不用 -count_frames 直接读 nb_frames（瞬间完成）；只有 nb_frames 为 N/A
    时才用 -count_frames 全量计数（慢，仅作兜底）。"""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames,nb_frames,avg_frame_rate,duration",
         "-of", "json", src],
        capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return None, "probe_failed", (r.stderr or "ffprobe failed").strip()[:150]
    try:
        streams = json.loads(r.stdout).get("streams", [])
    except Exception as e:
        return None, "probe_failed", "bad json: %s" % e
    if not streams:
        return None, "novideo", "no video stream"
    s = streams[0]
    v = s.get("nb_frames")
    if v not in (None, "N/A", ""):
        try:
            n = int(v)
            if n > 0:
                return n, "nb_frames", None
        except Exception:
            pass
    # 兜底：全量计数
    r2 = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames,avg_frame_rate,duration",
         "-of", "json", src],
        capture_output=True, text=True, timeout=900)
    if r2.returncode != 0:
        return None, "probe_failed", (r2.stderr or "ffprobe count failed").strip()[:150]
    try:
        streams2 = json.loads(r2.stdout).get("streams", [])
    except Exception as e:
        return None, "probe_failed", "bad json: %s" % e
    if not streams2:
        return None, "novideo", "no video stream"
    s2 = streams2[0]
    v = s2.get("nb_read_frames")
    if v not in (None, "N/A", ""):
        try:
            n = int(v)
            if n > 0:
                return n, "nb_read_frames", None
        except Exception:
            pass
    try:
        dur = float(s2.get("duration") or 0)
        num, den = s2.get("avg_frame_rate", "0/1").split("/")
        fps = float(num) / float(den) if float(den) else 0
        if dur > 0 and fps > 0:
            return int(round(dur * fps)), "duration*fps", None
    except Exception:
        pass
    return None, "probe_failed", "no frame count available: %s" % json.dumps(s2)[:150]


def decode_all(src):
    """ffmpeg 全量解码到 null。返回 (decoded:int|None, rc:int, err_tail:str)。"""
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-threads", "2", "-v", "info", "-i", src,
         "-map", "0:v:0", "-f", "null", "-"],
        capture_output=True, text=True, timeout=1800)
    frames = FRAME_RE.findall(r.stderr or "")
    decoded = int(frames[-1]) if frames else None
    tail = ""
    if r.returncode != 0:
        lines = [l for l in (r.stderr or "").splitlines() if l.strip()]
        tail = " | ".join(lines[-3:])[:300]
    return decoded, r.returncode, tail


def one(row):
    rid, src = row
    t0 = time.time()
    rec = {"rq_id": rid, "src": src, "latency_s": 0}
    try:
        st = os.stat(src)
    except FileNotFoundError:
        rec.update({"ff_verdict": "missing", "error": "src not on disk"})
        rec["latency_s"] = round(time.time() - t0, 2)
        return rec
    except Exception as e:
        rec.update({"ff_verdict": "probe_failed", "error": "stat: %s" % str(e)[:120]})
        rec["latency_s"] = round(time.time() - t0, 2)
        return rec
    rec["size_disk"] = st.st_size
    rec["mtime_disk"] = int(st.st_mtime)
    try:
        rec["sha256"] = sha256(src)
    except Exception as e:
        rec.update({"ff_verdict": "probe_failed", "error": "hash: %s" % str(e)[:120]})
        rec["latency_s"] = round(time.time() - t0, 2)
        return rec
    total, total_src, perr = probe_total(src)
    rec["total_frames"] = total
    rec["total_source"] = total_src
    if total is None:
        rec.update({"ff_verdict": total_src,
                    "error": perr or total_src})
        rec["latency_s"] = round(time.time() - t0, 2)
        return rec
    try:
        decoded, rc, tail = decode_all(src)
    except subprocess.TimeoutExpired:
        rec.update({"ff_verdict": "damaged", "decoded_frames": None,
                    "error": "ffmpeg timeout 1800s"})
        rec["latency_s"] = round(time.time() - t0, 2)
        return rec
    except Exception as e:
        rec.update({"ff_verdict": "damaged", "decoded_frames": None,
                    "error": "ffmpeg run: %s: %s" % (type(e).__name__, str(e)[:120])})
        rec["latency_s"] = round(time.time() - t0, 2)
        return rec
    rec["decoded_frames"] = decoded
    rec["ffmpeg_rc"] = rc
    rec["ffmpeg_err"] = tail
    if rc == 0 and decoded is not None and decoded >= total:
        rec["ff_verdict"] = "good"
    else:
        rec["ff_verdict"] = "damaged"
        if decoded is None:
            rec["error"] = "no frame count from ffmpeg; rc=%d; %s" % (rc, tail)
        elif rc != 0:
            rec["error"] = "ffmpeg rc=%d decoded=%s/%s; %s" % (rc, decoded, total, tail)
        else:
            rec["error"] = "partial decode %s/%s" % (decoded, total)
    rec["latency_s"] = round(time.time() - t0, 2)
    return rec


def hb(status, total, done, extra=None):
    try:
        os.makedirs(os.path.dirname(HB), exist_ok=True)
        d = {"task": "remaining-videos-verify-ffmpeg", "status": status,
             "total": total, "done": done, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if extra:
            d.update(extra)
        with open(HB, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    rows = []
    with open(LIST, encoding="utf-8") as f:
        hdr = f.readline()
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 2 and p[0].isdigit():
                rows.append((int(p[0]), p[1]))
    print("verify scope: %d videos, workers=%d" % (len(rows), args.workers), flush=True)
    hb("running", len(rows), 0)
    t0 = time.time()
    results, done = [], 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(one, row): row[0] for row in rows}
        for fut in as_completed(futs):
            rec = fut.result()
            results.append(rec)
            done += 1
            if done % 50 == 0:
                print("... %d/%d %.0fs" % (done, len(rows), time.time() - t0), flush=True)
                hb("running", len(rows), done)
    results.sort(key=lambda r: r["rq_id"])
    cols = ["rq_id", "src", "ff_verdict", "total_frames", "total_source",
            "decoded_frames", "ffmpeg_rc", "size_disk", "mtime_disk",
            "sha256", "error", "ffmpeg_err", "latency_s"]
    with open(OUT_TSV, "w", encoding="utf-8") as f:
        f.write("\t".join(cols) + "\n")
        for r in results:
            vals = [str(r.get(c, "")).replace("\t", " ").replace("\n", " ").replace("\r", " ") for c in cols]
            f.write("\t".join(vals) + "\n")
    counts = {}
    for r in results:
        counts[r["ff_verdict"]] = counts.get(r["ff_verdict"], 0) + 1
    summ = {"n": len(results), "counts": counts,
            "total_s": round(time.time() - t0, 1),
            "avg_latency_s": round(sum(r.get("latency_s", 0) for r in results) / max(1, len(results)), 2)}
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print("VERIFY-FFMPEG DONE " + json.dumps(summ, ensure_ascii=False), flush=True)
    hb("done", len(rows), len(rows), summ)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
