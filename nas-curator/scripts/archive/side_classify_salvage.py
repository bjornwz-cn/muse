#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""side-classify 视频兜底（阶段 1b）：对 cv2 零帧的视频，用宿主机 ffmpeg 抽到的帧做 VLM 分类。
输入：/data/side_salvage_list.txt（视频路径清单）、/data/side_frames/（宿主机 ffmpeg
  已抽好的帧，命名 <idx>_<t>.jpg；idx 与清单行号对应，从 0 开始）。
输出：/data/side_classify_salvage.jsonl（与 full.jsonl 同 shape；仍零帧/仍失败的记
  status=decode_failed、decision=review、review_reason=损坏：ffmpeg 兜底仍无法抽帧）。
只读：不移动、不写库。模型与 prompt 与 full 阶段一致（v2 规则）。
"""
import sys, os, json, time, base64, io, re, glob
sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")
from PIL import Image
from curator_core.llmkit import LLM

sys.path.insert(0, "/data")
import importlib.util
spec = importlib.util.spec_from_file_location("scf", "/data/side_classify_full.py")
# 不执行 main：只借用 vlm_classify/pick_providers/prep_image_pil/PARTIES 等
# 为避免 import 副作用（模块顶层无副作用，只有定义），直接加载
scf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scf)


def main():
    llm = LLM()
    primary, fallback = scf.pick_providers(llm)
    print("primary: %s | model: %s" % (primary["name"], primary["model"]), flush=True)
    with open("/data/side_salvage_list.txt", encoding="utf-8") as f:
        videos = [l.strip() for l in f if l.strip()]
    print("salvage videos: %d" % len(videos), flush=True)
    out_path = "/data/side_classify_salvage.jsonl"
    results = []
    for idx, path in enumerate(videos):
        rec = {"src_kind": "B-待整理-视频", "src": path,
               "filename": os.path.basename(path),
               "size": os.path.getsize(path) if os.path.exists(path) else 0,
               "mtime": int(os.path.getmtime(path)) if os.path.exists(path) else 0,
               "ext": os.path.splitext(path)[1].lower()}
        t0 = time.time()
        frames = sorted(glob.glob("/data/side_frames/%d_*.jpg" % idx))
        raws = []
        for fp in frames:
            try:
                with Image.open(fp) as im:
                    raws.append(scf.prep_image_pil(im))
            except Exception:
                continue
        if not raws:
            rec.update({"status": "decode_failed",
                        "error": "ffmpeg salvage zero frames",
                        "decision": "review",
                        "review_reason": "损坏：ffmpeg 兜底仍无法抽帧"})
            results.append(rec)
            print("FAIL %s" % rec["filename"], flush=True)
            continue
        out, err = scf.vlm_classify(llm, primary, fallback, raws,
                                    rec["filename"], is_video=True)
        if err is not None:
            rec.update({"status": "api_error", "error": err[:200],
                        "decision": "review", "review_reason": "API失败未分类"})
            results.append(rec)
            print("APIERR %s" % rec["filename"], flush=True)
            continue
        rec.update({"status": "ok", "method": "video-ffmpeg-salvage-%d" % len(raws),
                    **out})
        if rec.get("party") == "无法判断":
            rec["decision"] = "review"
            rec["review_reason"] = "无法判断：" + (rec.get("reason") or "")
        elif rec.get("confidence", 0) < 0.85:
            rec["decision"] = "review"
            rec["review_reason"] = "低置信（%.2f）：%s" % (rec.get("confidence", 0),
                                                        rec.get("reason") or "")
        else:
            rec["decision"] = "move"
        rec["latency_s"] = round(time.time() - t0, 2)
        results.append(rec)
        print("OK %s %s %.2f %s" % (rec["party"], rec["filename"][:40],
                                    rec.get("confidence", 0), rec["decision"]), flush=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    from collections import Counter
    print("SUMMARY " + json.dumps({
        "n": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "decode_failed": sum(1 for r in results if r["status"] == "decode_failed"),
        "move": sum(1 for r in results if r.get("decision") == "move"),
        "review": sum(1 for r in results if r.get("decision") == "review"),
        "by_party": dict(Counter(r.get("party") for r in results
                                 if r["status"] == "ok")),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
