#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""New-5 sources classify (stage 1e): 11 places, 367 files.
G 年份待确认-王震周报 (10 docs: text model; 7 .doc are __CLOUDSYNC_ENC__ -> review)
H 99-待确认-刘音 (4: hash dedup vs 家庭文件/刘音/; VLM family placement)
I 99-待确认-家庭王震 (7: hash dedup vs 家庭文件/王震/; m4a -> 通话录音/; pdf -> text model)
J 待确认-媒体库 (60: hash dedup internal; encrypted 27 -> review; VLM 15-category)
K 99-待确认-Mac下载 (286: hash dedup internal; kept copies VLM 3-way work/family/media)
Output: /data/side_classify_new5.jsonl (same shape as other classify outputs).
Read-only: no moves, no DB writes.
"""
import sys, os, json, time, base64, io, re, hashlib
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")
from PIL import Image
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass
import cv2
from curator_core.llmkit import LLM

sys.path.insert(0, "/data")
import importlib.util
spec = importlib.util.spec_from_file_location("scf", "/data/side_classify_full.py")
scf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scf)

ROOT = "/vol1/1000/收件箱"
FAMILY = "/vol1/1000/家庭文件"
SRC = {
    "G": os.path.join(ROOT, "年份待确认-王震周报"),
    "H": os.path.join(ROOT, "99-待确认-刘音"),
    "I": os.path.join(ROOT, "99-待确认-家庭王震"),
    "J": os.path.join(ROOT, "待确认-媒体库"),
    "K": os.path.join(ROOT, "99-待确认-Mac下载"),
}
IMG_EXTS = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".webp", ".tiff", ".tif"}
VID_EXTS = {".mov", ".mp4", ".mpg", ".mpeg", ".m4v"}

MEDIA_PHOTO_CATS = ["人物合影", "儿童成长", "自拍", "城市与建筑", "旅行风景",
                    "宠物与动物", "工作与活动", "日常生活", "美食与咖啡",
                    "屏幕截图", "文档与票据", "梗图与表情", "运动与活动", "聚会与夜生活"]
MEDIA_VIDEO_CATS = ["儿童成长", "屏幕录制", "旅行风景", "日常生活", "聚会与夜生活", "运动与活动"]

MEDIA_PROMPT = """你是媒体库归档专家。看这张图，判断它属于媒体库 15 类内容标签中的哪一类。
只输出 JSON：{"category":"类目名（原样输出）","confidence":0.9,"reason":"一句话理由"}。
照片类目（14 个）：人物合影、儿童成长、自拍、城市与建筑、旅行风景、宠物与动物、工作与活动、日常生活、美食与咖啡、屏幕截图、文档与票据、梗图与表情、运动与活动、聚会与夜生活。
视频类目（6 个）：儿童成长、屏幕录制、旅行风景、日常生活、聚会与夜生活、运动与活动。
规则：应用图标/ App 图标 / 界面截图一律归 屏幕截图；证件/发票/单据类归 文档与票据；无法判断时 category 填"无法判断"。
这是一张%s。文件名：%s
"""

FAMILY_LIUYIN_PROMPT = """你是家庭文件归档专家。看这张图/视频帧，判断它属于家庭文件/刘音/下哪个位置。
刘音/现有子目录：会议、刘音学习、刘音考试、刘音证件照、北京德尔康尼骨科医院有限公司、北票地契、北部战区总医院（下有 CRRT学习、会议、刘音-设备报废相关文件、发票、文档-办公、科室资产、细化2015、细化2016、细化2017）、医疗检查、发票、张苏身份证号、科室资产、考试、证件照片、资产清查、身体检查报告。
只输出 JSON：{"subdir":"相对刘音/的路径，如 北部战区总医院/会议","confidence":0.9,"reason":"一句话理由"}。
无法判断时 subdir 填"无法判断"。文件名：%s
"""

MAC_PROMPT = """你是文件归档专家。看这张图，判断它按内容属于工作、家庭还是媒体。
工作类：进工作文件 4 个文件夹之一（1-王震、2-上海润心、3-北京供应链、4-辽宁器械），再选子目录（01-公司资质与证照、02-产品与注册、03-合同与协议、04-供应商与采购、05-销售与业务数据、06-库存与物流、07-财务与经营分析、08-运营与项目、09-会议与汇报、10-周报与工作总结、11-人事与行政、12-招投标、13-图片与影像、14-工具与其他；1-王震只有 00-整理说明、09-会议与汇报、10-周报与工作总结、12-项目资料、14-工具与其他；4-辽宁器械另有 15-临床与学术资料）。
家庭类：进家庭文件对应位置（刘音/或王震/下子目录）。
媒体类：进媒体库对应类目（照片：人物合影、儿童成长、自拍、城市与建筑、旅行风景、宠物与动物、工作与活动、日常生活、美食与咖啡、屏幕截图、文档与票据、梗图与表情、运动与活动、聚会与夜生活；视频：儿童成长、屏幕录制、旅行风景、日常生活、聚会与夜生活、运动与活动）。
规则：应用图标/ App 图标 / 界面截图一律归媒体 屏幕截图；工作文档/合同/报表截图归工作；家庭/生活照片归家庭；无法判断时 dest 填"无法判断"。
只输出 JSON：{"dest":"work 或 family 或 media 或 无法判断","party":"方名（work 时为 1-王震/2-上海润心/3-北京供应链/4-辽宁器械；family 时为 家庭文件；media 时为 媒体库）","subdir":"子目录（work 时为编号子目录名；family 时为相对家庭文件/的路径如 刘音/会议；media 时为 照片/类目 或 视频/类目）","confidence":0.9,"reason":"一句话理由"}。
文件名：%s
"""

ZHOU_PROMPT = """你是文件归档专家。下面是一份王震的周报正文摘录（可能含 OCR/提取噪声）。请读正文确定它的年份与日期。
只输出 JSON：{"year":2016,"date":"2016-07-10","confidence":0.9,"reason":"一句话理由，说明依据文中哪个日期"}。
年份取正文里明确的年份（如"2016年"、"16年7月"、"2024-Q3"）；日期取该周周报对应的周一或文中首个日期；正文无明确年份时 year 填 0、date 填空。
正文：
%s
文件名：%s
"""

PDF_WANGZHEN_PROMPT = """你是家庭文件归档专家。下面是一份家庭文件/王震/待归档 PDF 的正文摘录。请判断它属于王震/下哪个子目录。
王震/现有子目录：JOPLIN、NAS文档、上海落户文件、会议录音、学习证书、摩托车、新兴花园装修图片、证件、证件照、通话录音。
只输出 JSON：{"subdir":"子目录名（原样输出）","confidence":0.9,"reason":"一句话理由"}。
无法判断时 subdir 填"无法判断"。正文：
%s
文件名：%s
"""


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def list_files(base):
    out = []
    for root, _d, names in os.walk(base):
        for fn in names:
            out.append(os.path.join(root, fn))
    out.sort()
    return out


def size_map(base):
    m = {}
    for root, _d, names in os.walk(base):
        for fn in names:
            p = os.path.join(root, fn)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            m.setdefault(sz, []).append(p)
    return m


def base_rec(path, src_kind):
    st = os.stat(path)
    return {"src_kind": src_kind, "src": path, "filename": os.path.basename(path),
            "size": st.st_size, "mtime": int(st.st_mtime),
            "ext": os.path.splitext(path)[1].lower()}


def is_cloudsync_enc(path):
    try:
        with open(path, "rb") as f:
            return f.read(20).startswith(b"__CLOUDSYNC_ENC__")
    except OSError:
        return False


def pil_decode_ok(path):
    try:
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:
        return False


def video_frames(path, times=(0, 10, 20)):
    cap = cv2.VideoCapture(path)
    raws = []
    try:
        for t in times:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, fr = cap.read()
            if ok and fr is not None:
                try:
                    im = Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
                    raws.append(scf.prep_image_pil(im))
                except Exception:
                    continue
    finally:
        cap.release()
    return raws


def vlm_media(llm, primary, fallback, raws, filename, is_video):
    kind = "视频" if is_video else "照片"
    prompt = MEDIA_PROMPT % (kind, filename)
    parts = [{"type": "text", "text": prompt}]
    for raw in raws:
        parts.append({"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," + base64.b64encode(raw).decode()}})
    messages = [{"role": "system", "content": "只输出 JSON，不要解释。"},
                {"role": "user", "content": parts}]
    content, prov, err = scf.call_ark(llm, primary, fallback, messages)
    if err is not None:
        return None, err
    out = scf.parse_json(content)
    if not out or "category" not in out:
        return None, "bad_json:" + (content or "")[:100]
    cats = MEDIA_VIDEO_CATS if is_video else MEDIA_PHOTO_CATS
    cat = str(out.get("category", "")).strip()
    if cat != "无法判断" and cat not in cats:
        return None, "bad_cat:" + cat
    try:
        conf = round(max(0.0, min(1.0, float(out.get("confidence", 0)))), 2)
    except Exception:
        conf = 0.0
    return {"category": cat, "confidence": conf,
            "reason": str(out.get("reason", ""))[:150],
            "model": prov["model"], "provider": prov["name"],
            "fallback": prov is fallback}, None


def vlm_generic(llm, primary, fallback, raws, prompt):
    parts = [{"type": "text", "text": prompt}]
    for raw in raws:
        parts.append({"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," + base64.b64encode(raw).decode()}})
    messages = [{"role": "system", "content": "只输出 JSON，不要解释。"},
                {"role": "user", "content": parts}]
    content, prov, err = scf.call_ark(llm, primary, fallback, messages)
    if err is not None:
        return None, None, err
    out = scf.parse_json(content)
    if not out:
        return None, None, "bad_json:" + (content or "")[:100]
    return out, prov, None


def text_call(llm, primary, fallback, prompt):
    messages = [{"role": "system", "content": "只输出 JSON，不要解释。"},
                {"role": "user", "content": prompt}]
    content, prov, err = scf.call_ark(llm, primary, fallback, messages)
    if err is not None:
        return None, None, err
    out = scf.parse_json(content)
    if not out:
        return None, None, "bad_json:" + (content or "")[:100]
    return out, prov, None


def main():
    llm = LLM()
    primary, fallback = scf.pick_providers(llm)
    print("primary: %s | model: %s" % (primary["name"], primary["model"]), flush=True)
    results = []

    def add(r):
        results.append(r)

    # ---------- text2 index ----------
    idx2 = {}
    try:
        for line in open("/data/side_text2_index.tsv", encoding="utf-8"):
            p = line.rstrip("\n").split("\t")
            if len(p) >= 9 and p[0].isdigit():
                idx2[p[1]] = int(p[0])
    except OSError:
        pass

    def sidecar2(path):
        i = idx2.get(path)
        if i is None:
            return ""
        try:
            return open("/data/side_text2/%d.txt" % i, encoding="utf-8").read()
        except OSError:
            return ""

    # ---------- G 周报 ----------
    print("== G 周报 ==", flush=True)
    for path in list_files(SRC["G"]):
        rec = base_rec(path, "G-王震周报")
        if is_cloudsync_enc(path):
            rec.update({"status": "encrypted", "decision": "review",
                        "review_reason": "加密：群晖云同步加密文件（__CLOUDSYNC_ENC__），读不出内容；建议落位 1-王震/12-项目资料/04-周报/（年份待确认）",
                        "method": "encrypted"})
            add(rec)
            print("ENC %s" % rec["filename"], flush=True)
            continue
        text = sidecar2(path)
        if not text.strip():
            # try ole fallback for .doc
            try:
                sys.path.insert(0, "/data")
                from side_ole_text import extract as ole_extract
                text = ole_extract(path)
            except Exception:
                text = ""
        if not text.strip():
            rec.update({"status": "empty-text", "decision": "review",
                        "review_reason": "读不出内容（正文提取为空）；建议落位 1-王震/12-项目资料/04-周报/（年份待确认）",
                        "method": "empty-text"})
            add(rec)
            print("EMPTY %s" % rec["filename"], flush=True)
            continue
        out, prov, err = text_call(llm, primary, fallback,
                                   ZHOU_PROMPT % (text[:3500], rec["filename"]))
        if err is not None:
            rec.update({"status": "api_error", "error": err[:200],
                        "decision": "review", "review_reason": "API失败未分类"})
            add(rec)
            continue
        year = out.get("year") or 0
        date = str(out.get("date") or "").strip()
        try:
            conf = round(float(out.get("confidence", 0)), 2)
        except Exception:
            conf = 0.0
        ext = rec["ext"]
        if year and date and re.match(r"^\d{4}-\d{2}-\d{2}$", date) and conf >= 0.85:
            new_name = "%s_王震_周报%s" % (date, ext)
            rec.update({"status": "ok", "method": "doc-text",
                        "party": "1-王震", "subdir": "12-项目资料",
                        "year": int(year), "date": date, "new_name": new_name,
                        "confidence": conf, "reason": str(out.get("reason", ""))[:150],
                        "model": prov["model"], "provider": prov["name"],
                        "fallback": prov is fallback, "decision": "move"})
        else:
            rec.update({"status": "ok", "method": "doc-text",
                        "party": "无法判断", "confidence": conf,
                        "reason": str(out.get("reason", ""))[:150],
                        "decision": "review",
                        "review_reason": "无法判断：年份/日期不确定（%s）；建议落位 1-王震/12-项目资料/04-周报/" % (out.get("reason", "")[:80])})
        add(rec)
        print("G %s -> %s %s" % (rec["filename"][:30], rec.get("party"), rec["decision"]), flush=True)

    # ---------- H 刘音 ----------
    print("== H 刘音 ==", flush=True)
    sm_liuyin = size_map(os.path.join(FAMILY, "刘音"))
    for path in list_files(SRC["H"]):
        rec = base_rec(path, "H-刘音")
        dup_of = None
        for cand in sm_liuyin.get(rec["size"], []):
            try:
                if sha256(cand) == sha256(path):
                    dup_of = cand
                    break
            except OSError:
                continue
        if dup_of:
            rec.update({"status": "ok", "method": "dedup", "decision": "review",
                        "review_reason": "重复：与 %s 内容完全相同（sha256 一致），保留那一份" % dup_of,
                        "dup_of": dup_of})
            add(rec)
            print("DUP %s" % rec["filename"], flush=True)
            continue
        ext = rec["ext"]
        if ext in IMG_EXTS:
            if not pil_decode_ok(path):
                rec.update({"status": "decode_failed", "decision": "review",
                            "review_reason": "损坏：图片解码失败；建议落位 家庭文件/刘音/（按文件名 2015-11-28 会议照片疑似北部战区总医院/会议/）"})
                add(rec)
                continue
            try:
                with Image.open(path) as im:
                    raws = [scf.prep_image_pil(im)]
            except Exception as e:
                rec.update({"status": "decode_failed", "decision": "review",
                            "review_reason": "损坏：图片解码失败 %s" % type(e).__name__})
                add(rec)
                continue
            out, prov, err = vlm_generic(llm, primary, fallback, raws,
                                         FAMILY_LIUYIN_PROMPT % rec["filename"])
            if err is not None:
                rec.update({"status": "api_error", "decision": "review", "review_reason": "API失败未分类"})
                add(rec)
                continue
            sub = str(out.get("subdir", "")).strip()
            try:
                conf = round(float(out.get("confidence", 0)), 2)
            except Exception:
                conf = 0.0
            if sub and sub != "无法判断" and conf >= 0.85:
                rec.update({"status": "ok", "method": "vlm-family",
                            "party": "家庭文件", "subdir": "刘音/" + sub,
                            "confidence": conf, "reason": str(out.get("reason", ""))[:150],
                            "model": prov["model"], "provider": prov["name"],
                            "fallback": prov is fallback, "decision": "move"})
            else:
                rec.update({"status": "ok", "method": "vlm-family",
                            "party": "无法判断", "confidence": conf,
                            "reason": str(out.get("reason", ""))[:150],
                            "decision": "review",
                            "review_reason": "无法判断：%s；建议落位 家庭文件/刘音/北部战区总医院/会议/（同场 2015-11-28 照片）" % (out.get("reason", "")[:80])})
            add(rec)
        elif ext in VID_EXTS:
            raws = video_frames(path)
            if not raws:
                rec.update({"status": "decode_failed", "decision": "review",
                            "review_reason": "损坏：视频抽帧失败；建议落位 家庭文件/刘音/（视频内容无法确认）"})
                add(rec)
                continue
            out, prov, err = vlm_generic(llm, primary, fallback, raws,
                                         FAMILY_LIUYIN_PROMPT % rec["filename"])
            if err is not None:
                rec.update({"status": "api_error", "decision": "review", "review_reason": "API失败未分类"})
                add(rec)
                continue
            sub = str(out.get("subdir", "")).strip()
            try:
                conf = round(float(out.get("confidence", 0)), 2)
            except Exception:
                conf = 0.0
            if sub and sub != "无法判断" and conf >= 0.85:
                rec.update({"status": "ok", "method": "vlm-family-video",
                            "party": "家庭文件", "subdir": "刘音/" + sub,
                            "confidence": conf, "reason": str(out.get("reason", ""))[:150],
                            "model": prov["model"], "provider": prov["name"],
                            "fallback": prov is fallback, "decision": "move"})
            else:
                rec.update({"status": "ok", "method": "vlm-family-video",
                            "party": "无法判断", "confidence": conf,
                            "reason": str(out.get("reason", ""))[:150],
                            "decision": "review",
                            "review_reason": "无法判断：%s；建议落位 家庭文件/刘音/" % (out.get("reason", "")[:80])})
            add(rec)
        else:
            rec.update({"status": "unreadable", "decision": "review",
                        "review_reason": "不可读格式（%s），无法按内容分类" % ext})
            add(rec)
        print("H %s -> %s %s" % (rec["filename"][:30], rec.get("party"), rec["decision"]), flush=True)

    # ---------- I 家庭王震 ----------
    print("== I 家庭王震 ==", flush=True)
    sm_wangzhen = size_map(os.path.join(FAMILY, "王震"))
    for path in list_files(SRC["I"]):
        rec = base_rec(path, "I-家庭王震")
        dup_of = None
        for cand in sm_wangzhen.get(rec["size"], []):
            try:
                if sha256(cand) == sha256(path):
                    dup_of = cand
                    break
            except OSError:
                continue
        if dup_of:
            rec.update({"status": "ok", "method": "dedup", "decision": "review",
                        "review_reason": "重复：与 %s 内容完全相同（sha256 一致），保留那一份" % dup_of,
                        "dup_of": dup_of})
            add(rec)
            print("DUP %s" % rec["filename"], flush=True)
            continue
        ext = rec["ext"]
        if ext == ".m4a":
            # valid MP4 container check (ftyp + moov)
            ok_mp4 = False
            try:
                with open(path, "rb") as f:
                    head = f.read(1 << 20)
                ok_mp4 = b"ftyp" in head[:32] and b"moov" in head
            except OSError:
                pass
            if not ok_mp4:
                rec.update({"status": "decode_failed", "decision": "review",
                            "review_reason": "损坏：音频容器无法解析；建议落位 家庭文件/王震/通话录音/"})
                add(rec)
                continue
            rec.update({"status": "ok", "method": "name-pattern",
                        "party": "家庭文件", "subdir": "王震/通话录音",
                        "confidence": 0.9, "reason": "文件名 手机号_时间戳.m4a，通话录音命名格式；音频容器解析正常",
                        "decision": "move"})
            add(rec)
        elif ext == ".pdf":
            text = sidecar2(path)
            if not text.strip():
                rec.update({"status": "empty-text", "decision": "review",
                            "review_reason": "读不出内容（正文提取为空）；建议落位 家庭文件/王震/证件/"})
                add(rec)
                continue
            out, prov, err = text_call(llm, primary, fallback,
                                       PDF_WANGZHEN_PROMPT % (text[:3500], rec["filename"]))
            if err is not None:
                rec.update({"status": "api_error", "decision": "review", "review_reason": "API失败未分类"})
                add(rec)
                continue
            sub = str(out.get("subdir", "")).strip()
            try:
                conf = round(float(out.get("confidence", 0)), 2)
            except Exception:
                conf = 0.0
            if sub and sub != "无法判断" and conf >= 0.85:
                rec.update({"status": "ok", "method": "doc-text",
                            "party": "家庭文件", "subdir": "王震/" + sub,
                            "confidence": conf, "reason": str(out.get("reason", ""))[:150],
                            "model": prov["model"], "provider": prov["name"],
                            "fallback": prov is fallback, "decision": "move"})
            else:
                rec.update({"status": "ok", "method": "doc-text",
                            "party": "无法判断", "confidence": conf,
                            "reason": str(out.get("reason", ""))[:150],
                            "decision": "review",
                            "review_reason": "无法判断：%s；建议落位 家庭文件/王震/证件/" % (out.get("reason", "")[:80])})
            add(rec)
        else:
            rec.update({"status": "unreadable", "decision": "review",
                        "review_reason": "不可读格式（%s），无法按内容分类" % ext})
            add(rec)
        print("I %s -> %s %s" % (rec["filename"][:30], rec.get("party"), rec["decision"]), flush=True)

    # ---------- J 媒体库 ----------
    print("== J 媒体库 ==", flush=True)
    j_files = list_files(SRC["J"])
    # internal hash groups
    by_hash = {}
    for path in j_files:
        try:
            by_hash.setdefault(sha256(path), []).append(path)
        except OSError:
            continue
    dup_loser = {}
    for h, ps in by_hash.items():
        if len(ps) > 1:
            ps.sort()
            for loser in ps[1:]:
                dup_loser[loser] = ps[0]
    for path in j_files:
        rec = base_rec(path, "J-媒体库")
        if path in dup_loser:
            rec.update({"status": "ok", "method": "dedup", "decision": "review",
                        "review_reason": "重复：与 %s 内容完全相同（sha256 一致），保留那一份" % dup_loser[path],
                        "dup_of": dup_loser[path]})
            add(rec)
            print("DUP %s" % rec["filename"], flush=True)
            continue
        if is_cloudsync_enc(path):
            rec.update({"status": "encrypted", "decision": "review",
                        "review_reason": "加密：群晖云同步加密文件（__CLOUDSYNC_ENC__），读不出内容；建议落位 媒体库/（按文件名：%s，无法看图确认类目）" % rec["filename"][:40],
                        "method": "encrypted"})
            add(rec)
            print("ENC %s" % rec["filename"], flush=True)
            continue
        ext = rec["ext"]
        is_video = ext in VID_EXTS
        if is_video:
            raws = video_frames(path)
        else:
            if not pil_decode_ok(path):
                rec.update({"status": "decode_failed", "decision": "review",
                            "review_reason": "损坏：图片解码失败；建议落位 媒体库/照片/（无法看图确认类目）"})
                add(rec)
                continue
            try:
                with Image.open(path) as im:
                    raws = [scf.prep_image_pil(im)]
            except Exception as e:
                rec.update({"status": "decode_failed", "decision": "review",
                            "review_reason": "损坏：图片解码失败 %s" % type(e).__name__})
                add(rec)
                continue
        if is_video and not raws:
            rec.update({"status": "decode_failed", "decision": "review",
                        "review_reason": "损坏：视频抽帧失败；建议落位 媒体库/视频/（无法看图确认类目）"})
            add(rec)
            continue
        out, err = vlm_media(llm, primary, fallback, raws, rec["filename"], is_video)
        if err is not None:
            rec.update({"status": "api_error", "error": err[:200],
                        "decision": "review", "review_reason": "API失败未分类"})
            add(rec)
            continue
        cat = out["category"]
        conf = out["confidence"]
        if cat == "无法判断" or conf < 0.85:
            rec.update({"status": "ok", "method": "vlm-media",
                        "party": "无法判断", "confidence": conf,
                        "reason": out["reason"], "decision": "review",
                        "review_reason": "无法判断：%s；建议落位 媒体库/%s/" % (out["reason"][:80], "视频" if is_video else "照片")})
        else:
            kind = "视频" if is_video else "照片"
            rec.update({"status": "ok", "method": "vlm-media",
                        "party": "媒体库", "subdir": "%s/%s" % (kind, cat),
                        "confidence": conf, "reason": out["reason"],
                        "model": out["model"], "provider": out["provider"],
                        "fallback": out["fallback"], "decision": "move"})
        add(rec)
        print("J %s -> %s %s" % (rec["filename"][:30], rec.get("party"), rec["decision"]), flush=True)

    # ---------- K Mac下载 ----------
    print("== K Mac下载 ==", flush=True)
    k_files = list_files(SRC["K"])
    by_hash = {}
    for path in k_files:
        try:
            by_hash.setdefault(sha256(path), []).append(path)
        except OSError:
            continue

    def keep_key(p):
        fn = os.path.basename(p)
        return (1 if ("(2)" in fn or " (2)" in fn or "副本" in fn) else 0, fn)

    dup_loser_k = {}
    kept = []
    for h, ps in by_hash.items():
        if len(ps) > 1:
            ps_sorted = sorted(ps, key=keep_key)
            kept.append(ps_sorted[0])
            for loser in ps_sorted[1:]:
                dup_loser_k[loser] = ps_sorted[0]
        else:
            kept.append(ps[0])
    kept.sort()
    for path in k_files:
        rec = base_rec(path, "K-Mac下载")
        if path in dup_loser_k:
            rec.update({"status": "ok", "method": "dedup", "decision": "review",
                        "review_reason": "重复：与 %s 内容完全相同（sha256 一致），保留那一份" % dup_loser_k[path],
                        "dup_of": dup_loser_k[path]})
            add(rec)
            print("DUP %s" % rec["filename"], flush=True)
            continue
        ext = rec["ext"]
        is_video = ext in VID_EXTS
        if is_video:
            raws = video_frames(path)
            if not raws:
                rec.update({"status": "decode_failed", "decision": "review",
                            "review_reason": "损坏：视频抽帧失败；建议落位 待人工确认"})
                add(rec)
                continue
        else:
            if not pil_decode_ok(path):
                rec.update({"status": "decode_failed", "decision": "review",
                            "review_reason": "损坏：图片解码失败；建议落位 待人工确认"})
                add(rec)
                continue
            try:
                with Image.open(path) as im:
                    raws = [scf.prep_image_pil(im)]
            except Exception as e:
                rec.update({"status": "decode_failed", "decision": "review",
                            "review_reason": "损坏：图片解码失败 %s" % type(e).__name__})
                add(rec)
                continue
        out, prov, err = vlm_generic(llm, primary, fallback, raws,
                                     MAC_PROMPT % rec["filename"])
        if err is not None:
            rec.update({"status": "api_error", "decision": "review", "review_reason": "API失败未分类"})
            add(rec)
            continue
        dest = str(out.get("dest", "")).strip()
        party = str(out.get("party", "")).strip()
        sub = str(out.get("subdir", "")).strip()
        try:
            conf = round(float(out.get("confidence", 0)), 2)
        except Exception:
            conf = 0.0
        if dest == "work" and party in ("1-王震", "2-上海润心", "3-北京供应链", "4-辽宁器械") \
                and sub and conf >= 0.85:
            rec.update({"status": "ok", "method": "vlm-3way",
                        "party": party, "subdir": sub, "confidence": conf,
                        "reason": str(out.get("reason", ""))[:150],
                        "model": prov["model"], "provider": prov["name"],
                        "fallback": prov is fallback, "decision": "move"})
        elif dest == "family" and party == "家庭文件" and sub and conf >= 0.85:
            rec.update({"status": "ok", "method": "vlm-3way",
                        "party": "家庭文件", "subdir": sub, "confidence": conf,
                        "reason": str(out.get("reason", ""))[:150],
                        "model": prov["model"], "provider": prov["name"],
                        "fallback": prov is fallback, "decision": "move"})
        elif dest == "media" and party == "媒体库" and sub and conf >= 0.85:
            rec.update({"status": "ok", "method": "vlm-3way",
                        "party": "媒体库", "subdir": sub, "confidence": conf,
                        "reason": str(out.get("reason", ""))[:150],
                        "model": prov["model"], "provider": prov["name"],
                        "fallback": prov is fallback, "decision": "move"})
        else:
            rec.update({"status": "ok", "method": "vlm-3way",
                        "party": "无法判断", "confidence": conf,
                        "reason": str(out.get("reason", ""))[:150],
                        "decision": "review",
                        "review_reason": "无法判断：%s；建议落位 待人工确认" % (out.get("reason", "")[:80])})
        add(rec)
        print("K %s -> %s %s" % (rec["filename"][:30], rec.get("party"), rec["decision"]), flush=True)

    with open("/data/side_classify_new5.jsonl", "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    from collections import Counter
    print("SUMMARY " + json.dumps({
        "n": len(results),
        "by_kind": dict(Counter(r["src_kind"] for r in results)),
        "by_decision": dict(Counter(r.get("decision") for r in results)),
        "by_party": dict(Counter(r.get("party") for r in results if r.get("decision") == "move")),
        "by_method": dict(Counter(r.get("method") for r in results)),
        "encrypted": sum(1 for r in results if r.get("status") == "encrypted"),
        "decode_failed": sum(1 for r in results if r.get("status") == "decode_failed"),
        "dup": sum(1 for r in results if r.get("method") == "dedup"),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
