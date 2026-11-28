#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""side-classify 试点：三处来源分层取样，只分类、不移动、不写库。

来源：
  A /vol1/1000/收件箱/待整理（照片为主）
  B /vol1/1000/收件箱/待整理-视频（视频）
  C /vol1/1000/收件箱/收件箱-邮件归档（.eml）
取样：A 60、B 20、C 40，共 120，分层等距抽样（年份/扩展名/月份）。
图片：PIL+heif 解码 → VLM 看图定所属方+子目录；
视频：cv2 抽 0/10/20 秒 3 帧，逐帧 VLM，多数表决；
邮件：解析 From/Subject/正文前 1500 字+附件名，文本模型定所属方+子目录。
模型：agent-plan（DeepSeek-V4.1-Flash 标准版）为主，失败降级 model-3（经济版），
与年份批次同一链路；只记 provider 名/模型/计费档，绝不打印 Key。
输出：/data/side_classify_pilot.jsonl + /data/side_classify_pilot_summary.json；
心跳 side-classify-pilot。只读：不移动文件、不写 DB、不删任何东西。
"""
import sys, os, json, time, base64, io, re, argparse, email, threading
from email import policy
from email.parser import BytesParser
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

try:
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None

SRC_A = "/vol1/1000/收件箱/待整理"
SRC_B = "/vol1/1000/收件箱/待整理-视频"
SRC_C = "/vol1/1000/收件箱/收件箱-邮件归档"

PARTIES = ["1-王震", "2-上海润心", "3-北京供应链", "4-辽宁器械", "无法判断"]
SUBDIRS = {
    "1-王震": ["00-整理说明", "09-会议与汇报", "10-周报与工作总结",
               "12-项目资料", "14-工具与其他"],
    "2-上海润心": ["01-公司资质与证照", "02-产品与注册", "03-合同与协议",
                  "04-供应商与采购", "05-销售与业务数据", "06-库存与物流",
                  "07-财务与经营分析", "08-运营与项目", "09-会议与汇报",
                  "10-周报与工作总结", "11-人事与行政", "12-招投标",
                  "12-项目资料", "13-图片与影像", "14-工具与其他"],
    "3-北京供应链": ["01-公司资质与证照", "02-产品与注册", "03-合同与协议",
                   "04-供应商与采购", "05-销售与业务数据", "06-库存与物流",
                   "07-财务与经营分析", "08-运营与项目", "09-会议与汇报",
                   "10-周报与工作总结", "11-人事与行政", "12-招投标",
                   "13-图片与影像", "14-工具与其他"],
    "4-辽宁器械": ["01-公司资质与证照", "02-产品与注册", "03-合同与协议",
                  "04-供应商与采购", "05-销售与业务数据", "06-库存与物流",
                  "07-财务与经营分析", "08-运营与项目", "09-会议与汇报",
                  "10-周报与工作总结", "11-人事与行政", "12-招投标",
                  "13-图片与影像", "14-工具与其他", "15-临床与学术资料"],
}
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".jfif"}
VID_EXTS = {".mov", ".mp4"}

PARTY_DESC = (
    "1-王震：王震个人的文件——个人述职/工作总结/工作计划、党建材料、个人证件与人事档案、"
    "个人培训学习、个人周报、家庭/私人照片视频、非公司业务的个人邮件。\n"
    "2-上海润心：上海华润润心医疗器械有限公司（上海公司）的业务文件——公司资质证照、产品注册、"
    "合同协议、供应商采购、销售业务数据、库存物流、财务经营、运营项目、会议汇报、周报月报、"
    "人事行政、招投标、现场/验收照片视频。注意：浙江事业部、上海业务组属于上海润心。\n"
    "3-北京供应链：北京供应链公司（华润医药商业北京供应链相关）的业务文件——同上编号结构，"
    "内容主体是北京供应链公司、北京区域医院/项目、供应链服务。\n"
    "4-辽宁器械：华润润心（辽宁）医疗器械有限公司（含其上海分公司）的业务文件——同上编号结构，"
    "另有 15-临床与学术资料；内容主体是辽宁公司、沈阳/辽宁区域医院与项目、辽宁电力中心医院SPD、"
    "区域检验中心等。注意：名称含“辽宁器械”“华润润心（辽宁）”的文件属于这一方，"
    "即使有“上海分公司”字样也不属于 2-上海润心。\n"
    "无法判断：画面/正文信息不足以确定属于哪一方，或内容与四方都无关，或为损坏/无关文件。"
)


def _hb(task_id, progress_dir, **kw):
    if _write_hb is None:
        return
    try:
        _write_hb(task_id, progress_dir=progress_dir, **kw)
    except Exception as e:
        print("heartbeat failed: %s" % e, flush=True)


def pick_providers(llm):
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


def prep_image_pil(img, max_side=1024):
    img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def vlm_classify(llm, primary, fallback, image_bytes, filename, extra_hint=""):
    prompt = (
        "你是文件归档专家。看这张图，判断图中内容属于下面哪一方、进该方哪个子目录。\n"
        "只按内容所属方判断，不要按文件类型（照片/视频/文档）分。\n"
        f"可选方：\n{PARTY_DESC}\n"
        f"文件名：{filename}\n{extra_hint}\n"
        '只输出 JSON：{"party":"方名（原样输出，如 2-上海润心 或 无法判断）",'
        '"subdir":"子目录名（原样输出，如 13-图片与影像；无法判断时为空）",'
        '"confidence":0.9,"reason":"一句话理由，说明图中看到了什么关键信息"}'
    )
    messages = [
        {"role": "system", "content": "只输出 JSON，不要解释。"},
        {"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode()}},
        ]},
    ]
    for prov in (primary, fallback):
        if prov is None:
            continue
        content, err = llm._call_ark(prov, messages, True, 60, 300)
        if err or not content:
            continue
        out = parse_json(content)
        if not isinstance(out, dict) or "party" not in out:
            continue
        party = str(out.get("party", "")).strip()
        if party not in PARTIES:
            continue
        try:
            conf = round(max(0.0, min(1.0, float(out.get("confidence", 0)))), 2)
        except Exception:
            conf = 0.0
        subdir = str(out.get("subdir", "")).strip()
        if party != "无法判断" and subdir not in SUBDIRS.get(party, []):
            subdir = ""
        return {"party": party, "subdir": subdir, "confidence": conf,
                "reason": str(out.get("reason", ""))[:150],
                "model": prov["model"], "provider": prov["name"],
                "fallback": prov is fallback}
    return None


def text_classify(llm, primary, fallback, text_blob, filename):
    prompt = (
        "你是文件归档专家。根据下面这封邮件的发件人、收件人、主题与正文，判断它属于下面哪一方、"
        "进该方哪个子目录。只按内容所属方判断。\n"
        f"可选方：\n{PARTY_DESC}\n"
        f"邮件：\n{text_blob}\n"
        '只输出 JSON：{"party":"方名（原样输出，如 2-上海润心 或 无法判断）",'
        '"subdir":"子目录名（原样输出，如 10-周报与工作总结；无法判断时为空）",'
        '"confidence":0.9,"reason":"一句话理由，说明关键信息（发件人/主题/正文）"}'
    )
    messages = [
        {"role": "system", "content": "只输出 JSON，不要解释。"},
        {"role": "user", "content": prompt},
    ]
    for prov in (primary, fallback):
        if prov is None:
            continue
        content, err = llm._call_ark(prov, messages, True, 60, 300)
        if err or not content:
            continue
        out = parse_json(content)
        if not isinstance(out, dict) or "party" not in out:
            continue
        party = str(out.get("party", "")).strip()
        if party not in PARTIES:
            continue
        try:
            conf = round(max(0.0, min(1.0, float(out.get("confidence", 0)))), 2)
        except Exception:
            conf = 0.0
        subdir = str(out.get("subdir", "")).strip()
        if party != "无法判断" and subdir not in SUBDIRS.get(party, []):
            subdir = ""
        return {"party": party, "subdir": subdir, "confidence": conf,
                "reason": str(out.get("reason", ""))[:150],
                "model": prov["model"], "provider": prov["name"],
                "fallback": prov is fallback}
    return None


def video_frames(path, times=(0, 10, 20)):
    try:
        import cv2
    except Exception:
        return []
    try:
        cap = cv2.VideoCapture(path)
    except Exception:
        return []
    if not cap.isOpened():
        return []
    frames = []
    try:
        for ss in times:
            try:
                cap.set(cv2.CAP_PROP_POS_MSEC, float(ss) * 1000.0)
                ok, frame = cap.read()
            except Exception:
                continue
            if not ok or frame is None or frame.size == 0:
                continue
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frames.append(Image.fromarray(rgb))
    finally:
        cap.release()
    return frames


def parse_eml(path):
    with open(path, "rb") as fh:
        m = BytesParser(policy=policy.default).parse(fh)
    atts = []
    body = ""
    if m.is_multipart():
        for p in m.walk():
            ct = p.get_content_type()
            if p.get_filename():
                atts.append(p.get_filename())
            elif ct == "text/plain" and not body:
                try:
                    body = p.get_content() or ""
                except Exception:
                    body = ""
    else:
        if m.get_content_type() == "text/plain":
            try:
                body = m.get_content() or ""
            except Exception:
                body = ""
    body = re.sub(r"\s+", " ", body or "").strip()[:1500]
    blob = ("发件人：%s\n收件人：%s\n抄送：%s\n日期：%s\n主题：%s\n附件：%s\n正文：%s"
            % (m.get("From"), m.get("To"), m.get("Cc"), m.get("Date"),
               m.get("Subject"), "；".join(atts) if atts else "无", body))
    return blob


def stratified(paths, n):
    """等距抽样：已排序的 paths 里均匀取 n 个。"""
    if len(paths) <= n:
        return list(paths)
    step = len(paths) / n
    return [paths[int(i * step)] for i in range(n)]


def sample():
    import glob
    a_all, b_all, c_all = [], [], []
    for root, _dirs, files in os.walk(SRC_A):
        for f in files:
            if os.path.splitext(f)[1].lower() in IMG_EXTS | VID_EXTS:
                a_all.append(os.path.join(root, f))
    for root, _dirs, files in os.walk(SRC_B):
        for f in files:
            if os.path.splitext(f)[1].lower() in VID_EXTS | IMG_EXTS:
                b_all.append(os.path.join(root, f))
    for root, _dirs, files in os.walk(SRC_C):
        for f in files:
            if f.lower().endswith(".eml"):
                c_all.append(os.path.join(root, f))
    a_all.sort()
    b_all.sort()
    c_all.sort()
    picks = []
    for p in stratified(a_all, 60):
        picks.append(("A-待整理", p))
    for p in stratified(b_all, 20):
        picks.append(("B-待整理-视频", p))
    for p in stratified(c_all, 40):
        picks.append(("C-邮件归档", p))
    return picks, len(a_all), len(b_all), len(c_all)


_tls = threading.local()


def thread_llm():
    llm = getattr(_tls, "llm", None)
    if llm is None:
        llm = LLM()
        _tls.llm = llm
    return llm


def process_one(item, primary, fallback):
    src_kind, path = item
    rec = {"src_kind": src_kind, "src": path, "filename": os.path.basename(path)}
    t0 = time.time()
    try:
        size = os.path.getsize(path)
    except Exception as e:
        rec.update({"status": "gone", "error": str(e)[:150]})
        return rec
    rec["size"] = size
    ext = os.path.splitext(path)[1].lower()
    llm = thread_llm()
    try:
        if src_kind == "C-邮件归档":
            try:
                blob = parse_eml(path)
            except Exception as e:
                rec.update({"status": "decode_failed",
                            "error": "%s: %s" % (type(e).__name__, str(e)[:150])})
                return rec
            out = text_classify(llm, primary, fallback, blob, rec["filename"])
            if out is None:
                rec.update({"status": "api_error", "error": "all providers failed"})
                return rec
            rec.update({"status": "ok", "method": "eml-text", **out})
        elif ext in VID_EXTS:
            frames = video_frames(path)
            if not frames:
                rec.update({"status": "decode_failed", "error": "no frames extracted"})
                return rec
            votes = []
            for i, fr in enumerate(frames):
                try:
                    raw = prep_image_pil(fr)
                except Exception as e:
                    continue
                out = vlm_classify(llm, primary, fallback, raw, rec["filename"],
                                   extra_hint="这是视频第 %d 帧（共 %d 帧）。"
                                   % (i + 1, len(frames)))
                if out:
                    votes.append(out)
            if not votes:
                rec.update({"status": "api_error", "error": "all frames failed"})
                return rec
            best = max(votes, key=lambda v: v["confidence"])
            parties = [v["party"] for v in votes]
            agree = parties.count(best["party"])
            rec.update({"status": "ok", "method": "video-frames-%d" % len(frames),
                        "party": best["party"], "subdir": best["subdir"],
                        "confidence": best["confidence"],
                        "reason": best["reason"], "model": best["model"],
                        "provider": best["provider"], "fallback": best["fallback"],
                        "frame_votes": parties, "frame_agree": agree})
        else:
            try:
                img = Image.open(path)
                raw = prep_image_pil(img)
            except Exception as e:
                rec.update({"status": "decode_failed",
                            "error": "%s: %s" % (type(e).__name__, str(e)[:150])})
                return rec
            out = vlm_classify(llm, primary, fallback, raw, rec["filename"])
            if out is None:
                rec.update({"status": "api_error", "error": "all providers failed"})
                return rec
            rec.update({"status": "ok", "method": "vlm-image", **out})
    except Exception as e:
        rec.update({"status": "error", "error": "%s: %s" % (type(e).__name__, str(e)[:200])})
    rec["latency_s"] = round(time.time() - t0, 2)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--out", default="/data/side_classify_pilot.jsonl")
    ap.add_argument("--summary", default="/data/side_classify_pilot_summary.json")
    args = ap.parse_args()

    llm = LLM()
    primary, fallback = pick_providers(llm)
    print("primary: %s | model: %s | billing: %s" % (
        primary["name"], primary["model"], primary.get("billing")), flush=True)
    if fallback is not None:
        print("fallback: %s | model: %s | billing: %s" % (
            fallback["name"], fallback["model"], fallback.get("billing")), flush=True)

    picks, na, nb, nc = sample()
    print("source counts: A=%d B=%d C=%d | pilot picks: %d" % (na, nb, nc, len(picks)),
          flush=True)
    progress_dir = "/data/progress"
    _hb("side-classify-pilot", progress_dir, label="待整理三处分类试点",
        kind="side-classify-pilot", phase="pilot", status="running",
        total=len(picks), done=0)

    t_start = time.time()
    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, min(4, args.workers))) as ex:
        futs = {ex.submit(process_one, it, primary, fallback): it for it in picks}
        for fut in as_completed(futs):
            rec = fut.result()
            results.append(rec)
            done += 1
            print("%s %s %s %s" % (rec.get("status"), rec.get("src_kind"),
                                   rec.get("party", rec.get("error", ""))[:40],
                                   rec.get("filename", ""))[:200], flush=True)
            if done % 20 == 0:
                _hb("side-classify-pilot", progress_dir, label="待整理三处分类试点",
                    kind="side-classify-pilot", phase="pilot", status="running",
                    total=len(picks), done=done)

    with open(args.out, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    ok = [r for r in results if r.get("status") == "ok"]
    by_party = {}
    by_kind_party = {}
    for r in ok:
        by_party[r["party"]] = by_party.get(r["party"], 0) + 1
        k = (r["src_kind"], r["party"])
        by_kind_party[k] = by_kind_party.get(k, 0) + 1
    ge85 = sum(1 for r in ok if r.get("confidence", 0) >= 0.85)
    summary = {
        "n": len(results),
        "ok": len(ok),
        "decode_failed": sum(1 for r in results if r.get("status") == "decode_failed"),
        "api_error": sum(1 for r in results if r.get("status") == "api_error"),
        "other": sum(1 for r in results if r.get("status") not in ("ok", "decode_failed", "api_error")),
        "ge85": ge85,
        "ge85_rate": round(ge85 / len(ok), 3) if ok else None,
        "by_party": by_party,
        "by_kind_party": {("%s|%s" % k): v for k, v in sorted(by_kind_party.items())},
        "fallback_used": sum(1 for r in ok if r.get("fallback")),
        "avg_latency_s": round(sum(r.get("latency_s", 0) for r in ok) / len(ok), 2) if ok else None,
        "total_s": round(time.time() - t_start, 1),
        "primary": primary["name"], "primary_model": primary["model"],
        "fallback": fallback["name"] if fallback else None,
        "source_counts": {"A": na, "B": nb, "C": nc},
    }
    with open(args.summary, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    _hb("side-classify-pilot", progress_dir, label="待整理三处分类试点",
        kind="side-classify-pilot", phase="pilot", status="done",
        total=len(picks), done=len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
