#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""side-classify 全量分类（阶段 1）：三处来源全部文件，只分类、不移动、不写库。

来源：A 待整理（照片为主）、B 待整理-视频、C 收件箱-邮件归档。
流程：
  1) 盘点全部文件（路径/大小/mtime/扩展名）→ /data/side_classify_inventory.tsv
  2) 按大小分组，仅同大小组做全量 sha256，找完全重复 → 重复者进复核（不删除）
  3) 分类：图片 VLM 看图（v2 prompt，私人无业务信号→1-王震）；
     视频 cv2 抽 0/10/20 秒帧，单次 VLM 调用多帧综合判断；cv2 零帧→记 salvage，
     由宿主机 ffmpeg 兜底抽帧后再判；邮件解析 From/Subject/正文+v3 prompt
     （人员/组织知识块）；.nef/.exe 等不可读→复核。
  4) 决策：party 已定且 confidence>=0.85 且解码正常 → move 候选；
     其余一律 review（附原因：无法判断/低置信/损坏/重复/不可读）。
模型：agent-plan（DeepSeek-V4.1-Flash 标准版）为主，失败降级 model-3（经济版）；
只记 provider 名/模型/计费档，绝不打印 Key。
输出：/data/side_classify_full.jsonl、/data/side_classify_full_summary.json、
/data/side_salvage_list.txt（需 ffmpeg 兜底的视频）；心跳 side-classify-full。
断点续跑：已有结论的行（按 src 匹配）跳过；重跑即续跑。
"""
import sys, os, json, time, base64, io, re, argparse, email, hashlib, threading
from email import policy
from email.parser import BytesParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict, Counter
from datetime import datetime

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

KNOWLEDGE = (
    "人员与组织线索（发件人/收件人/抄送人属于某一方已知人员、或内容涉及该方已知"
    "事业部/区域，即为明确指向该方；不要只认公司全名）：\n"
    "- 2-上海润心：王震（总经理，分管营销中心）；金祎、吴洁（供应链总监）、战轲、"
    "王雅琴（人事经理）、于航（浙江事业部负责人）、佟晓锐、郑丹凤（综合管理部）、"
    "张欢（财务部）、黄明辉（财务）、李阳希（上海业务组）、高熙渊（上海业务组）、"
    "王佳乐（上海业务组）、胥艳丽（物流部）、刘芳芳；事业部/区域：浙江事业部、"
    "上海业务组、温州仓、上海仓；业务：波科（BK）、微术、旭化成。\n"
    "- 4-辽宁器械：白天山（辽宁事业部负责人）、王子辰（吉林事业部负责人，辽吉区域）；"
    "辽宁业务组、辽宁分、辽吉区域；沈阳/抚顺/辽源/锦州/营口等辽宁区域医院；"
    "辽宁电力中心医院SPD、区域检验中心；公司名含“辽宁器械”“华润润心（辽宁）”"
    "“华润辽宁器械”（即使有“上海分公司”字样也属辽宁器械）。\n"
    "- 3-北京供应链：北京区域、北京供应链公司、供应链服务方案。\n"
    "- 1-王震：王震个人、党建、述职、个人证件、家庭。\n"
    "品牌名（爱德华、波科等）本身不决定归属，看协议主体/事业部/区域。\n"
    "多方内容以主体/发件人所属方为准；确实多方均等、无法定主体时才判无法判断。\n"
)

MAIL_SUBDIR_HINT = (
    "子目录：必填，从该方子目录列表中选最贴近的（周报类→10-周报与工作总结；"
    "人事录用/入职/离职→11-人事与行政；财务付款/对账/发票→07-财务与经营分析；"
    "销售数据→05-销售与业务数据；合同协议→03-合同与协议；会议→09-会议与汇报；"
    "库存物流→06-库存与物流；资质证照→01-公司资质与证照；产品注册→02-产品与注册；"
    "招投标→12-招投标；1-王震的邮件→12-项目资料；实在没有贴近的→14-工具与其他）。"
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


def call_ark(llm, primary, fallback, messages):
    """返回 (content, used_provider, err)。主失败则降级。"""
    for prov in (primary, fallback):
        if prov is None:
            continue
        content, err = llm._call_ark(prov, messages, True, 60, 300)
        if err or not content:
            continue
        return content, prov, None
    return None, None, "all providers failed"


def valid_out(out):
    if not isinstance(out, dict) or "party" not in out:
        return None
    party = str(out.get("party", "")).strip()
    if party not in PARTIES:
        return None
    try:
        conf = round(max(0.0, min(1.0, float(out.get("confidence", 0)))), 2)
    except Exception:
        conf = 0.0
    subdir = str(out.get("subdir", "")).strip()
    if party != "无法判断" and subdir not in SUBDIRS.get(party, []):
        subdir = ""
    return {"party": party, "subdir": subdir, "confidence": conf,
            "reason": str(out.get("reason", ""))[:150]}


def vlm_classify(llm, primary, fallback, image_list, filename, is_video=False):
    """image_list: JPEG bytes 列表（视频为多帧）。单次调用综合判断。"""
    n = len(image_list)
    kind_note = ("这是同一视频按时间顺序抽的 %d 帧，综合所有帧判断。" % n) if is_video \
        else "这是一张照片。"
    prompt = (
        "你是文件归档专家。看这张图，判断图中内容属于下面哪一方、进该方哪个子目录。\n"
        "只按内容所属方判断，不要按文件类型（照片/视频/文档）分。\n"
        "判断规则（按顺序）：\n"
        "1) 画面明确含公司业务内容（文件、会议、产品、医院、合同、报表、资质、招投标、"
        "临床资料等）时，按内容所属公司定方；公司名/区域/人员能确定→定方，"
        "否则→无法判断。\n"
        "2) 画面无任何公司业务信号时（家庭/生活/旅游/风景/美食/随手拍等私人手机照片视频），"
        "一律归 1-王震，不要判无法判断。\n"
        "3) 画面完全无内容（全黑/全白/纯色/模糊到无法辨认）→无法判断。\n"
        f"可选方：\n{PARTY_DESC}\n"
        f"文件名：{filename}\n{kind_note}\n"
        "子目录：必填，从该方子目录列表中选一个最贴近的；"
        "1-王震的照片/视频没有贴近的子目录时填空；"
        "2/3/4号的图片影像类填 13-图片与影像；"
        "各方子目录：1-王震[00-整理说明，09-会议与汇报，10-周报与工作总结，"
        "12-项目资料，14-工具与其他]；"
        "2-上海润心/3-北京供应链/4-辽宁器械[01-公司资质与证照，02-产品与注册，"
        "03-合同与协议，04-供应商与采购，05-销售与业务数据，06-库存与物流，"
        "07-财务与经营分析，08-运营与项目，09-会议与汇报，10-周报与工作总结，"
        "11-人事与行政，12-招投标，13-图片与影像，14-工具与其他"
        "（4-辽宁器械另有 15-临床与学术资料；2-上海润心另有 12-项目资料）]。\n"
        '只输出 JSON：{"party":"方名（原样输出，如 2-上海润心 或 无法判断）",'
        '"subdir":"子目录名（原样输出，如 13-图片与影像；无法判断或无贴近子目录时为空）",'
        '"confidence":0.9,"reason":"一句话理由，说明图中看到了什么关键信息"}'
    )
    content_parts = [{"type": "text", "text": prompt}]
    for raw in image_list:
        content_parts.append({"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," + base64.b64encode(raw).decode()}})
    messages = [
        {"role": "system", "content": "只输出 JSON，不要解释。"},
        {"role": "user", "content": content_parts},
    ]
    content, prov, err = call_ark(llm, primary, fallback, messages)
    if err is not None:
        return None, err
    out = valid_out(parse_json(content))
    if out is None:
        return None, "bad_json_or_cat:" + (content or "")[:120]
    out.update({"model": prov["model"], "provider": prov["name"],
                "fallback": prov is fallback})
    return out, None


def text_classify(llm, primary, fallback, text_blob):
    prompt = (
        "你是文件归档专家。根据下面这封邮件的发件人、收件人、主题与正文，判断它属于下面哪一方、"
        "进该方哪个子目录。只按内容所属方判断。\n"
        "判断规则（按顺序）：\n"
        "1) 正文/主题/发件人/收件人/抄送人明确指向某一方业务→定方（见下方人员与组织线索）。\n"
        "2) 系统通知/营销广告/个人服务类邮件（云服务、差旅酒店、银行营销、SaaS通知、"
        "会议系统通知、验证码等），收件人为王震本人且无公司业务归属信号→1-王震。\n"
        "3) 业务内容（招聘、人事、合同、销售、会议、系统）但看不出属于哪一方→无法判断。\n"
        f"{KNOWLEDGE}"
        f"可选方：\n{PARTY_DESC}\n"
        f"{MAIL_SUBDIR_HINT}\n"
        f"邮件：\n{text_blob}\n"
        '只输出 JSON：{"party":"方名（原样输出，如 2-上海润心 或 无法判断）",'
        '"subdir":"子目录名（原样输出，如 10-周报与工作总结；无法判断时为空）",'
        '"confidence":0.9,"reason":"一句话理由，说明关键信息（发件人/主题/正文）"}'
    )
    messages = [
        {"role": "system", "content": "只输出 JSON，不要解释。"},
        {"role": "user", "content": prompt},
    ]
    content, prov, err = call_ark(llm, primary, fallback, messages)
    if err is not None:
        return None, err
    out = valid_out(parse_json(content))
    if out is None:
        return None, "bad_json_or_cat:" + (content or "")[:120]
    out.update({"model": prov["model"], "provider": prov["name"],
                "fallback": prov is fallback})
    return out, None


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
            if p.get_filename():
                atts.append(p.get_filename())
            elif p.get_content_type() == "text/plain" and not body:
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
    month = ""
    try:
        d = m.get("Date")
        if d:
            dt = email.utils.parsedate_to_datetime(str(d))
            month = "%04d-%02d" % (dt.year, dt.month)
    except Exception:
        pass
    if not month:
        mm = re.match(r"(\d{4})(\d{2})", os.path.basename(path))
        if mm:
            month = "%s-%s" % (mm.group(1), mm.group(2))
    return blob, month


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


_tls = threading.local()


def thread_llm():
    llm = getattr(_tls, "llm", None)
    if llm is None:
        llm = LLM()
        _tls.llm = llm
    return llm


def process_one(item, primary, fallback, dup_of):
    src_kind, path, size, mtime, ext = item
    rec = {"src_kind": src_kind, "src": path, "filename": os.path.basename(path),
           "size": size, "mtime": mtime, "ext": ext}
    t0 = time.time()
    if path in dup_of:
        rec.update({"status": "ok", "method": "dedup",
                    "party": "无法判断", "subdir": "", "confidence": 0.0,
                    "reason": "", "decision": "review",
                    "review_reason": "重复：与 %s 内容完全相同（sha256 一致），保留那一份" % dup_of[path]})
        return rec
    llm = thread_llm()
    try:
        if src_kind == "C-邮件归档":
            try:
                blob, month = parse_eml(path)
            except Exception as e:
                rec.update({"status": "decode_failed",
                            "error": "%s: %s" % (type(e).__name__, str(e)[:150]),
                            "decision": "review", "review_reason": "损坏：邮件解析失败"})
                return rec
            rec["mail_month"] = month
            out, err = text_classify(llm, primary, fallback, blob)
            if err is not None:
                rec.update({"status": "api_error", "error": err[:200],
                            "decision": "review", "review_reason": "API失败未分类"})
                return rec
            rec.update({"status": "ok", "method": "eml-text", **out})
        elif ext in VID_EXTS:
            frames = video_frames(path)
            if not frames:
                rec.update({"status": "salvage",
                            "error": "cv2 zero frames",
                            "decision": "review",
                            "review_reason": "待ffmpeg兜底抽帧（cv2 零帧）"})
                return rec
            raws = []
            for fr in frames:
                try:
                    raws.append(prep_image_pil(fr))
                except Exception:
                    continue
            if not raws:
                rec.update({"status": "decode_failed", "error": "frame prep failed",
                            "decision": "review", "review_reason": "损坏：帧转码失败"})
                return rec
            out, err = vlm_classify(llm, primary, fallback, raws,
                                    rec["filename"], is_video=True)
            if err is not None:
                rec.update({"status": "api_error", "error": err[:200],
                            "decision": "review", "review_reason": "API失败未分类"})
                return rec
            rec.update({"status": "ok", "method": "video-frames-%d" % len(raws), **out})
        elif ext in IMG_EXTS:
            try:
                img = Image.open(path)
                raw = prep_image_pil(img)
            except Exception as e:
                rec.update({"status": "decode_failed",
                            "error": "%s: %s" % (type(e).__name__, str(e)[:150]),
                            "decision": "review", "review_reason": "损坏：图片解码失败"})
                return rec
            out, err = vlm_classify(llm, primary, fallback, [raw], rec["filename"])
            if err is not None:
                rec.update({"status": "api_error", "error": err[:200],
                            "decision": "review", "review_reason": "API失败未分类"})
                return rec
            rec.update({"status": "ok", "method": "vlm-image", **out})
        else:
            rec.update({"status": "unreadable",
                        "decision": "review",
                        "review_reason": "不可读格式（%s），无法按内容分类" % ext})
            return rec
    except Exception as e:
        rec.update({"status": "error",
                    "error": "%s: %s" % (type(e).__name__, str(e)[:200]),
                    "decision": "review", "review_reason": "处理异常未分类"})
        return rec
    rec["latency_s"] = round(time.time() - t0, 2)
    if rec.get("status") == "ok":
        if rec.get("party") == "无法判断":
            rec["decision"] = "review"
            rec["review_reason"] = "无法判断：" + (rec.get("reason") or "")
        elif rec.get("confidence", 0) < 0.85:
            rec["decision"] = "review"
            rec["review_reason"] = "低置信（%.2f）：%s" % (rec.get("confidence", 0),
                                                        rec.get("reason") or "")
        else:
            rec["decision"] = "move"
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--out", default="/data/side_classify_full.jsonl")
    ap.add_argument("--summary", default="/data/side_classify_full_summary.json")
    ap.add_argument("--inventory", default="/data/side_classify_inventory.tsv")
    ap.add_argument("--salvage-list", default="/data/side_salvage_list.txt")
    args = ap.parse_args()

    llm = LLM()
    primary, fallback = pick_providers(llm)
    print("primary: %s | model: %s | billing: %s" % (
        primary["name"], primary["model"], primary.get("billing")), flush=True)
    if fallback is not None:
        print("fallback: %s | model: %s | billing: %s" % (
            fallback["name"], fallback["model"], fallback.get("billing")), flush=True)

    # 1) 盘点
    items = []
    for kind, base in (("A-待整理", SRC_A), ("B-待整理-视频", SRC_B),
                       ("C-邮件归档", SRC_C)):
        for root, _dirs, files in os.walk(base):
            for f in files:
                p = os.path.join(root, f)
                try:
                    st = os.stat(p)
                except Exception:
                    continue
                items.append((kind, p, st.st_size, int(st.st_mtime),
                              os.path.splitext(f)[1].lower()))
    items.sort(key=lambda x: x[1])
    print("inventory: %d files" % len(items), flush=True)
    with open(args.inventory, "w", encoding="utf-8") as f:
        f.write("src_kind\tsrc\tsize\tmtime\text\n")
        for kind, p, size, mtime, ext in items:
            f.write("%s\t%s\t%d\t%d\t%s\n" % (kind, p, size, mtime, ext))

    # 2) 同大小组全量 sha256
    by_size = defaultdict(list)
    for it in items:
        by_size[it[2]].append(it[1])
    dup_groups = {s: ps for s, ps in by_size.items() if len(ps) > 1}
    print("size groups>1: %d groups, %d files to hash"
          % (len(dup_groups), sum(len(v) for v in dup_groups.values())), flush=True)
    to_hash = [p for ps in dup_groups.values() for p in ps]
    hashes = {}
    if to_hash:
        def _h(p):
            try:
                return p, sha256_file(p)
            except Exception as e:
                return p, None
        with ThreadPoolExecutor(max_workers=2) as ex:
            for p, h in ex.map(_h, to_hash):
                if h:
                    hashes[p] = h
    by_sha = defaultdict(list)
    for p, h in hashes.items():
        by_sha[h].append(p)
    dup_of = {}
    n_dup_groups = 0
    for h, ps in by_sha.items():
        if len(ps) > 1:
            n_dup_groups += 1
            ps.sort()
            keep = ps[0]
            for p in ps[1:]:
                dup_of[p] = keep
    print("exact dup groups: %d, dup loser files: %d" % (n_dup_groups, len(dup_of)),
          flush=True)

    # 断点续跑：已有结论的行跳过
    done_srcs = set()
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as f:
                for line in f:
                    try:
                        done_srcs.add(json.loads(line)["src"])
                    except Exception:
                        pass
        except Exception:
            pass
    todo = [it for it in items if it[1] not in done_srcs]
    print("already done: %d, to process: %d" % (len(done_srcs), len(todo)), flush=True)

    progress_dir = "/data/progress"
    total = len(items)
    _hb("side-classify-full", progress_dir, label="待整理三处全量分类",
        kind="side-classify-full", phase="classify", status="running",
        total=total, done=len(done_srcs))

    t_start = time.time()
    results = []
    done = len(done_srcs)
    with ThreadPoolExecutor(max_workers=max(1, min(4, args.workers))) as ex:
        futs = {ex.submit(process_one, it, primary, fallback, dup_of): it
                for it in todo}
        for fut in as_completed(futs):
            rec = fut.result()
            results.append(rec)
            done += 1
            if done % 50 == 0:
                el = time.time() - t_start
                print("... %d/%d done, %.0fs elapsed" % (done, total, el), flush=True)
                _hb("side-classify-full", progress_dir, label="待整理三处全量分类",
                    kind="side-classify-full", phase="classify", status="running",
                    total=total, done=done)

    mode = "a" if done_srcs else "w"
    with open(args.out, mode, encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    all_recs = []
    with open(args.out, encoding="utf-8") as f:
        for line in f:
            try:
                all_recs.append(json.loads(line))
            except Exception:
                pass
    salvage = [r["src"] for r in all_recs if r.get("status") == "salvage"]
    with open(args.salvage_list, "w", encoding="utf-8") as f:
        for s in salvage:
            f.write(s + "\n")

    by_party = Counter(r.get("party") for r in all_recs if r.get("status") == "ok")
    by_decision = Counter(r.get("decision") for r in all_recs)
    by_review = Counter()
    for r in all_recs:
        if r.get("decision") == "review":
            rr = (r.get("review_reason") or "")[:4]
            by_review[rr] += 1
    summary = {
        "n_inventory": len(items),
        "n_results": len(all_recs),
        "ok": sum(1 for r in all_recs if r.get("status") == "ok"),
        "decode_failed": sum(1 for r in all_recs if r.get("status") == "decode_failed"),
        "salvage": len(salvage),
        "api_error": sum(1 for r in all_recs if r.get("status") == "api_error"),
        "unreadable": sum(1 for r in all_recs if r.get("status") == "unreadable"),
        "other_status": sum(1 for r in all_recs
                            if r.get("status") not in ("ok", "decode_failed", "salvage",
                                                      "api_error", "unreadable")),
        "dup_groups": n_dup_groups,
        "dup_losers": len(dup_of),
        "by_party": dict(by_party),
        "by_decision": dict(by_decision),
        "by_review_prefix": dict(by_review),
        "move_candidates": sum(1 for r in all_recs if r.get("decision") == "move"),
        "fallback_used": sum(1 for r in all_recs if r.get("fallback")),
        "avg_latency_s": round(sum(r.get("latency_s", 0) for r in all_recs
                                   if r.get("status") == "ok") /
                               max(1, sum(1 for r in all_recs
                                          if r.get("status") == "ok")), 2),
        "total_s": round(time.time() - t_start, 1),
        "primary": primary["name"], "primary_model": primary["model"],
        "fallback": fallback["name"] if fallback else None,
    }
    with open(args.summary, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    _hb("side-classify-full", progress_dir, label="待整理三处全量分类",
        kind="side-classify-full", phase="classify", status="done",
        total=total, done=len(all_recs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
