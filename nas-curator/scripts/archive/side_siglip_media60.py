#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SigLIP advisory pass for the 60 media files (container, media_core).
Reuses year-batch Classifier/decide/target_dir; NO DB writes.
Output: /data/side_siglip_media60.jsonl {src, kind, category, confidence, top1, top1_score, reason, suggested_dst}.
Run AFTER batch3 SigLIP finishes (host/container CPU contention).
"""
import sys, os, json, time
sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")
os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("MKL_NUM_THREADS", "4")

SRC = "/vol1/1000/收件箱/待确认-媒体库"
IMG = {".jpg", ".jpeg", ".heic", ".png", ".heif", ".webp", ".gif"}
VID = {".mov", ".mp4"}


def main():
    from media_core.curate import Classifier, decode_item_frames, decide, target_dir
    from media_core.engine import _classify_video_frames
    cfg = json.load(open("/app/media_core/config.json"))
    cls = Classifier(cfg)
    try:
        import torch as _torch
        _torch.set_num_threads(4)
    except Exception:
        pass
    files = []
    for root, _d, names in os.walk(SRC):
        for fn in names:
            p = os.path.join(root, fn)
            ext = os.path.splitext(fn)[1].lower()
            kind = "照片" if ext in IMG else ("视频" if ext in VID else None)
            if kind:
                files.append((p, kind))
    files.sort()
    print("files: %d" % len(files), flush=True)
    out = []
    for path, kind in files:
        st = os.stat(path)
        row = {"path": path, "name": os.path.basename(path), "size": st.st_size,
               "mtime": st.st_mtime, "kind": kind,
               "ext": os.path.splitext(path)[1].lower()}
        rec = {"src": path, "kind": kind}
        try:
            frames, blur, white, colors, ar, note = decode_item_frames(row)
        except Exception as e:
            rec.update({"category": "待确认", "confidence": 0.0,
                        "reason": "decode_failed:%s" % type(e).__name__})
            out.append(rec)
            print("FAIL %s" % row["name"], flush=True)
            continue
        if note or not frames:
            rec.update({"category": "待确认", "confidence": 0.0,
                        "reason": "decode_failed:%s" % (note or "noframe")})
            out.append(rec)
            print("FAIL %s" % row["name"], flush=True)
            continue
        try:
            if kind == "视频":
                cat, cat2, s1, s2, why = _classify_video_frames(cls, frames, row, cfg)
            else:
                sc = cls.scores(frames[0], "照片")
                cat, cat2, s1, s2, why = decide(row, sc, blur, white, colors, ar, "", cfg)
        except Exception as e:
            rec.update({"category": "待确认", "confidence": 0.0,
                        "reason": "classify_failed:%s" % type(e).__name__})
            out.append(rec)
            print("FAIL %s" % row["name"], flush=True)
            continue
        if why == "accepted":
            dst = target_dir(cfg, kind, cat)
            rec.update({"category": cat, "confidence": round(float(s1), 3),
                        "reason": "siglip:%.2f" % s1,
                        "evidence": "%s %.2f / %s %.2f" % (cat, s1, cat2, s2),
                        "suggested_dst": dst})
        else:
            rec.update({"category": "待确认",
                        "confidence": round(min(float(s1), 0.45), 3),
                        "reason": "low:%s:%.2f" % (why, s1),
                        "top1": cat2, "top1_score": round(float(s1), 3)})
        out.append(rec)
        print("OK %s %s %.2f" % (rec["category"], row["name"][:40], rec["confidence"]), flush=True)
    with open("/data/side_siglip_media60.jsonl", "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    from collections import Counter
    print("SUMMARY " + json.dumps({
        "n": len(out),
        "accepted": sum(1 for r in out if r.get("suggested_dst")),
        "by_category": dict(Counter(r["category"] for r in out)),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
