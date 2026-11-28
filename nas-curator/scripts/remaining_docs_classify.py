#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剩余文档重分类（remaining-docs，2026-11-25）：只分类、不移动、不写库外状态。

范围：pending 行中 src 扩展名为文档的全部行，排除加密文件（__CLOUDSYNC_ENC__
magic 头 / .pag / 旧 reason 含“加密”）与 5 个凭据文件（不动）。
实测：文档扩展名 pending 共 1016 行；加密 691 行；非加密 325 行；
凭据 5 行；本次可执行 320 行。

流程：逐个抽文本（沿用 year_doc_triage.py 的 extract），有文本跑 doc LLM
定所属方（1-王震/2-上海润心/3-北京供应链/4-辽宁器械）与类目；
无文本的 PDF 用 ocr-service（RapidOCR）OCR 前 3 页再分类，
非 PDF 无文本试 strings 兜底；仍无文本记 doc-empty。

通道（并行分流，worker 12）：
  - 阿里云百炼（新 Key，仅内存环境变量 ALIYUN_API_KEY，绝不落盘/打印）：
    qwen4-max，compatible-mode/v1。旧 Ark 已判“无法判断”的 219 行首选阿里
    （换模型家族给第二意见）；其余新行阿里/方舟轮流首选。
  - 方舟 agent-plan：deepseek-v4.1-flash-261017（免费额度）。
  - 任一通道判“无法判断”时，另一通道给第二意见；两边都无法判断才留复核。
  - model-3 经济版本次不用（经济版结论只进复核、不作最终结论）。
结果追加写 /data/year_siglip_results_remaining_docs.jsonl（断点续跑），
登记由 remaining_docs_register.py 另行执行（只登记 pending 行）。

运行（NAS 容器内，detached + 心跳）：
  ALIYUN_API_KEY=<key经环境变量传入> nohup docker exec ... python3 \
    /data/remaining_docs_classify.py --workers 12 > /data/remaining_docs_run.log 2>&1 &
"""
import sys, os, json, time, re, argparse, subprocess, zipfile, threading, sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

from curator_core.llmkit import LLM

RESULTS = "/data/year_siglip_results_remaining_docs.jsonl"
WORKLIST = "/data/remaining_docs_worklist.tsv"
SUMMARY = "/data/remaining_docs_summary.json"
PROGRESS_DIR = "/data/progress"
CAP = 4000
OCR_URL = "http://172.17.0.1:8866/ocr"
ALIYUN_BASE = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
ALIYUN_MODEL = "qwen4-max"

DOC_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt",
            ".eml", ".md", ".csv", ".rtf", ".html", ".odoc", ".osheet",
            ".eddx", ".pag"}
CRED_IDS = {138941, 138943, 139426, 139428, 139432}
CRED_NAMES = {"AccessKey.csv", "SecretKey.csv", "ApiKey.txt",
              "OpenClaw-CF令牌-取用后请删除.txt", "火山AccessKey.txt"}

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
PARTY_DESC = (
    "1-王震：王震个人的文件——个人述职/工作总结/工作计划、党建材料、个人证件与人事档案、"
    "个人培训学习、个人周报、家庭/私人文件、非公司业务的个人邮件。\n"
    "2-上海润心：上海华润润心医疗器械有限公司（上海公司）的业务文件——公司资质证照、产品注册、"
    "合同协议、供应商采购、销售业务数据、库存物流、财务经营、运营项目、会议汇报、周报月报、"
    "人事行政、招投标、现场/验收照片视频。注意：浙江事业部、上海业务组属于上海润心。\n"
    "3-北京供应链：北京供应链公司（华润医药商业北京供应链相关）的业务文件——同上编号结构，"
    "内容主体是北京供应链公司、北京区域医院/项目、供应链服务。\n"
    "4-辽宁器械：华润润心（辽宁）医疗器械有限公司（含其上海分公司）的业务文件——同上编号结构，"
    "另有 15-临床与学术资料；内容主体是辽宁公司、沈阳/辽宁区域医院与项目、辽宁电力中心医院SPD、"
    "区域检验中心等。注意：名称含“辽宁器械”“华润润心（辽宁）”的文件属于这一方，"
    "即使有“上海分公司”字样也不属于 2-上海润心。\n"
    "无法判断：正文信息不足以确定属于哪一方，或内容与四方都无关，或为损坏/无关文件。"
)
KNOWLEDGE = (
    "人员与组织线索（内容涉及某一方已知人员、事业部/区域，即为明确指向该方；不要只认公司全名）：\n"
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
    "多方内容以主体为准；确实多方均等、无法定主体时才判无法判断。\n"
)
SUBDIR_HINT = (
    "子目录：必填，从该方子目录列表中选最贴近的（周报类→10-周报与工作总结；"
    "人事录用/入职/离职→11-人事与行政；财务付款/对账/发票→07-财务与经营分析；"
    "销售数据→05-销售与业务数据；合同协议→03-合同与协议；会议→09-会议与汇报；"
    "库存物流→06-库存与物流；资质证照→01-公司资质与证照；产品注册→02-产品与注册；"
    "招投标→12-招投标；实在没有贴近的→14-工具与其他）。"
)

try:
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None


def _hb(**kw):
    if _write_hb is None:
        return
    try:
        _write_hb("remaining-docs-reclassify", progress_dir=PROGRESS_DIR, **kw)
    except Exception as e:  # noqa: BLE001
        print("heartbeat failed: %s" % e, flush=True)


# ---------------- 计数（线程安全） ----------------
class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.c = Counter()
        self.aliyun_prompt = 0
        self.aliyun_completion = 0

    def inc(self, k, n=1):
        with self.lock:
            self.c[k] += n

    def add_tokens(self, p, co):
        with self.lock:
            self.aliyun_prompt += p
            self.aliyun_completion += co


STATS = Stats()


# ---------------- 文本抽取（沿用 year_doc_triage.py） ----------------
def run(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.stdout
    except Exception:
        return b""


def strip_tags(t):
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def head_bytes(path, n=2 * 1024 * 1024):
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except Exception:
        return b""


def extract(path, ext):
    """返回 (text, extractor, encrypted_flag)。"""
    try:
        with open(path, "rb") as f:
            magic_head = f.read(64)
    except Exception:
        return "", "read-fail", False
    if magic_head.startswith(b"__CLOUDSYNC_ENC__"):
        return "", "cloudsync-encrypted", True
    if ext == ".pdf":
        out = run(["pdftotext", "-f", "1", "-l", "3", "-layout", path, "-"],
                  timeout=180)
        return out.decode(errors="replace")[:CAP], "pdftotext-3p", False
    if ext in (".docx", ".xlsx", ".pptx"):
        try:
            z = zipfile.ZipFile(path)
            parts = []
            for n in z.namelist():
                if n.endswith(".xml") and ("word/" in n or "xl/" in n
                                           or "ppt/slides/" in n
                                           or "sharedStrings" in n):
                    try:
                        parts.append(z.read(n).decode(errors="replace"))
                    except Exception:
                        pass
                    if sum(len(p) for p in parts) > 200000:
                        break
            return strip_tags(" ".join(parts))[:CAP], "ooxml", False
        except Exception:
            return "", "ooxml-fail", False
    if ext in (".doc", ".xls", ".ppt"):
        data = head_bytes(path, 4096)
        s = data[:200].lstrip()
        if s.startswith(b"<?xml") or s.startswith(b"<"):
            raw = head_bytes(path, 2 * 1024 * 1024).decode(errors="replace")
            return strip_tags(raw)[:CAP], "spreadsheetml", False
        out = run(["catdoc", path], timeout=120)
        return out.decode(errors="replace")[:CAP], "catdoc", False
    if ext in (".txt", ".csv", ".md", ".html", ".webloc"):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read(CAP), "plain", False
        except Exception:
            return "", "plain-fail", False
    if ext == ".eml":
        try:
            import email
            from email import policy
            from email.parser import BytesParser
            with open(path, "rb") as fh:
                m = BytesParser(policy=policy.default).parse(fh)
            atts, body = [], ""
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
            return blob[:CAP], "eml-parse", False
        except Exception:
            return "", "eml-fail", False
    if ext == ".zip":
        try:
            z = zipfile.ZipFile(path)
            names, enc = [], False
            for info in z.infolist()[:80]:
                names.append(info.filename)
                if info.flag_bits & 0x1:
                    enc = True
            return "压缩包内含文件：\n" + "\n".join(names), "zip-list", enc
        except Exception:
            return "", "zip-fail", False
    if ext == ".rar":
        out = run(["7z", "l", path], timeout=120)
        if out:
            return out.decode(errors="replace")[:CAP], "7z-list", False
        return "", "archive-nolist", False
    if ext in (".eddx", ".emmx", ".osheet", ".odoc"):
        try:
            z = zipfile.ZipFile(path)
            parts = []
            for n in z.namelist():
                if n.endswith(".xml"):
                    try:
                        parts.append(z.read(n).decode(errors="replace"))
                    except Exception:
                        pass
            if parts:
                return strip_tags(" ".join(parts))[:CAP], "zip-xml", False
        except Exception:
            pass
        out = run(["strings", path], timeout=60)
        return out.decode(errors="replace")[:CAP], "strings", False
    return "", "unreadable", False


def strings_text(path):
    """strings 兜底：抽可读行，要求含中文或足够长度。"""
    out = run(["strings", path], timeout=60).decode(errors="replace")
    lines = [ln.strip() for ln in out.splitlines()
             if len(ln.strip()) >= 4]
    cjk = sum(1 for ch in out if "\u4e00" <= ch <= "\u9fff")
    blob = "\n".join(lines)
    if cjk >= 10 or len(blob) >= 200:
        return blob[:CAP]
    return ""


def ocr_pdf(path):
    """ocr-service RapidOCR 前 3 页。返回 (text, err)。"""
    import requests
    try:
        with open(path, "rb") as f:
            r = requests.post(OCR_URL,
                              files={"file": (os.path.basename(path), f,
                                              "application/pdf")},
                              data={"max_pages": "3"}, timeout=600)
        if r.status_code != 200:
            return "", "ocr-service:%d" % r.status_code
        d = r.json()
        return (d.get("text") or "")[:CAP], None
    except Exception as e:  # noqa: BLE001
        return "", "ocr-error:%s" % type(e).__name__


# ---------------- LLM ----------------
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


def build_messages(text_blob, filename):
    prompt = (
        "你是文件归档专家。根据下面这份文件的摘录文本（开头部分），判断它属于下面哪一方、"
        "进该方哪个子目录。只按内容所属方判断。\n"
        "判断规则（按顺序）：\n"
        "1) 摘录明确指向某一方业务→定方（见下方人员与组织线索）。\n"
        "2) 系统通知/营销广告/个人服务类内容，收件人为王震本人且无公司业务归属信号→1-王震。\n"
        "3) 业务内容但看不出属于哪一方→无法判断。\n"
        f"{KNOWLEDGE}"
        f"可选方：\n{PARTY_DESC}\n"
        f"{SUBDIR_HINT}\n"
        f"文件名：{filename}\n"
        f"摘录：\n{text_blob}\n"
        '只输出 JSON：{"party":"方名（原样输出，如 2-上海润心 或 无法判断）",'
        '"subdir":"子目录名（原样输出，如 03-合同与协议；无法判断时为空）",'
        '"confidence":0.9,"reason":"一句话理由，说明关键信息"}'
    )
    return [
        {"role": "system", "content": "只输出 JSON，不要解释。"},
        {"role": "user", "content": prompt},
    ]


def call_aliyun(messages):
    """阿里云百炼 qwen4-max（Key 只从环境变量读，不打印）。返回 (out, err)。"""
    import requests
    key = os.environ.get("ALIYUN_API_KEY", "")
    if not key:
        return None, "aliyun-no-key"
    body = {"model": ALIYUN_MODEL, "messages": messages, "temperature": 0,
            "max_tokens": 300, "response_format": {"type": "json_object"}}
    hdr = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    last_err = "aliyun-unknown"
    for attempt in range(3):
        try:
            r = requests.post(ALIYUN_BASE + "/chat/completions", json=body,
                              headers=hdr, timeout=90)
        except Exception as e:  # noqa: BLE001
            last_err = ("timeout" if "timeout" in str(e).lower()
                        else "server") + ":" + type(e).__name__
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429:
            STATS.inc("aliyun_429")
            time.sleep(2 * (attempt + 1))
            last_err = "ratelimit:429"
            continue
        if r.status_code != 200:
            code = r.status_code
            last_err = ("auth" if code in (401, 403) else
                        "quota" if code == 402 else
                        "model" if code == 404 else "server") + ":%d" % code
            if code in (401, 403, 404):
                break
            time.sleep(2 * (attempt + 1))
            continue
        try:
            data = r.json()
        except Exception:
            return None, "aliyun-bad-json"
        usage = data.get("usage") or {}
        STATS.add_tokens(int(usage.get("prompt_tokens") or 0),
                         int(usage.get("completion_tokens") or 0))
        STATS.inc("aliyun_calls")
        msg = ((data.get("choices") or [{}])[0].get("message") or {})
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = "".join(str(c.get("text", "")) if isinstance(c, dict)
                              else str(c) for c in content)
        if not content:
            return None, "aliyun-empty"
        out = valid_out(parse_json(content))
        if out is None:
            return None, "aliyun-bad_json:" + content[:120]
        out.update({"model": ALIYUN_MODEL, "provider": "aliyun",
                    "channel": "aliyun"})
        return out, None
    return None, last_err


_tls = threading.local()


def thread_llm():
    llm = getattr(_tls, "llm", None)
    if llm is None:
        llm = LLM()
        _tls.llm = llm
    return llm


ARK_PRIMARY = None


def call_ark(messages):
    """方舟 agent-plan。返回 (out, err)。"""
    llm = thread_llm()
    content, err = llm._call_ark(ARK_PRIMARY, messages, True, 60, 300)
    STATS.inc("ark_calls")
    if err or not content:
        return None, "ark:" + (err or "empty")
    out = valid_out(parse_json(content))
    if out is None:
        return None, "ark-bad_json:" + (content or "")[:120]
    out.update({"model": ARK_PRIMARY["model"], "provider": "agent-plan",
                "channel": "ark"})
    return out, None


def classify_text(text, filename, first_channel):
    """双通道：首选通道判无法判断时，另一通道给第二意见。
    返回 (out, err, tried[list of channel])。out 为 None 表示两通道都失败。"""
    messages = build_messages(text, filename)
    tried = []
    order = ["aliyun", "ark"] if first_channel == "aliyun" else ["ark", "aliyun"]
    first_out, first_err = None, None
    for ch in order:
        tried.append(ch)
        if ch == "aliyun":
            out, err = call_aliyun(messages)
        else:
            out, err = call_ark(messages)
        if err is not None:
            if ch == order[0]:
                first_err = err
                continue  # 首选失败，试另一通道
            return None, "%s; %s" % (first_err or "", err), tried
        if out["party"] != "无法判断":
            out["second_opinion"] = (ch != order[0])
            return out, None, tried
        # 无法判断：留作 first_out，试另一通道
        first_out = out
        first_err = None
    if first_out is not None:
        first_out["second_opinion"] = True
        return first_out, None, tried
    return None, first_err or "all channels failed", tried


# ---------------- 单行处理 ----------------
def process_one(item, idx):
    rq_id, path, old_party, old_source = item
    ext = os.path.splitext(path)[1].lower()
    base = {'src': path, 'kind': '文档', 'category': '待确认', 'confidence': 0.0,
            'reason': '', 'evidence': '', 'why': '', 'suggested_dst': '',
            'party': '', 'subdir': '', 'rq_id': rq_id}
    # 1) 抽文本
    try:
        text, extractor, enc = extract(path, ext)
    except Exception as e:  # noqa: BLE001
        base.update({'reason': 'doc-extract-failed:%s' % type(e).__name__,
                     'why': 'doc-decode_failed'})
        return base
    if enc:
        base.update({'reason': 'encrypted: %s' % extractor,
                     'why': 'doc-encrypted'})
        return base
    ocr_used = False
    ocr_chars = 0
    ocr_err = ""
    if not text.strip():
        if ext == ".pdf":
            STATS.inc("ocr_attempted")
            otext, oerr = ocr_pdf(path)
            if oerr:
                ocr_err = oerr
                STATS.inc("ocr_failed")
            elif len(otext.strip()) >= 20:
                text = otext
                ocr_used = True
                ocr_chars = len(otext.strip())
                STATS.inc("ocr_recovered")
                extractor = "ocr-service-rapidocr-3p"
            else:
                ocr_chars = len(otext.strip())
                STATS.inc("ocr_empty")
        else:
            stext = strings_text(path)
            if stext:
                text = stext
                extractor = "strings-fallback"
                STATS.inc("strings_recovered")
    base['evidence'] = 'extractor=%s text_len=%d' % (extractor, len(text))
    if ocr_used:
        base['evidence'] += ' ocr_chars=%d' % ocr_chars
    if ocr_err:
        base['evidence'] += ' ocr_err=%s' % ocr_err
    if not text.strip():
        base.update({'reason': 'empty-text: %s%s' % (
                        extractor, ('; ' + ocr_err) if ocr_err else ''),
                     'why': 'doc-empty'})
        STATS.inc("doc_empty")
        return base
    STATS.inc("text_extracted")
    # 2) 定通道：旧 Ark 已判无法判断 → 阿里首选；其余按序号轮流
    if old_party == "无法判断":
        first = "aliyun"
    else:
        first = "aliyun" if (idx % 2 == 0) else "ark"
    out, err, tried = classify_text(text, os.path.basename(path), first)
    base['channels_tried'] = tried
    base['ocr_used'] = ocr_used
    if err is not None:
        base.update({'reason': 'api_error:%s' % err[:200],
                     'why': 'doc-api_error'})
        STATS.inc("api_error")
        return base
    party, conf = out['party'], out['confidence']
    tag = "[%s]" % out.get("channel", "?")
    reason = out['reason']
    if out.get("second_opinion"):
        reason = reason + "（另一通道复核一致）" if party == "无法判断" \
            else reason + "（第二意见采纳）"
    base.update({'party': party, 'subdir': out['subdir'],
                 'doc_model': out.get('model', ''),
                 'doc_models_tried': [t for t in tried],
                 'reason': ("%s %s" % (tag, reason))[:300],
                 'evidence': '%s/%s：%s' % (party, out['subdir'] or '-',
                                            reason) if party != '无法判断'
                             else reason,
                 'why': 'doc-llm-ocr' if ocr_used else 'doc-llm'})
    if party == '无法判断':
        base.update({'category': '待确认', 'confidence': round(conf, 3)})
        STATS.inc("undetermined")
    else:
        base.update({'category': party, 'confidence': round(conf, 3)})
        STATS.inc("determined")
        STATS.inc("party:" + party)
    return base


def pick_providers(llm):
    ark = [p for p in llm.providers if p.get("kind") == "ark"]
    primary = next((p for p in ark if p.get("name") == "agent-plan"), None)
    if primary is None:
        primary = next((p for p in ark if p.get("billing") == "free"), None)
    if primary is None:
        raise RuntimeError("no ark provider available")
    return primary


def load_worklist():
    """从 DB 取 pending 文档行，排除加密与凭据。返回 (items, counts)。"""
    conn = sqlite3.connect("/data/nas-curator.db", timeout=30)
    counts = Counter()
    items = []
    for rid, payload in conn.execute(
            "SELECT id, payload FROM review_queue WHERE status='pending'"):
        try:
            d = json.loads(payload) if payload else {}
        except Exception:
            d = {}
        src = d.get('src', '')
        ext = os.path.splitext(src)[1].lower()
        if ext not in DOC_EXTS:
            continue
        counts['doc_ext_pending'] += 1
        if rid in CRED_IDS or os.path.basename(src) in CRED_NAMES:
            counts['excluded_credential'] += 1
            continue
        if ext == '.pag' or '加密' in (d.get('reason') or ''):
            counts['excluded_encrypted_reason_or_pag'] += 1
            continue
        try:
            with open(src, 'rb') as f:
                head = f.read(20)
        except Exception:
            head = b''
        if head.startswith(b'__CLOUDSYNC_ENC__'):
            counts['excluded_encrypted_magic'] += 1
            continue
        items.append((rid, src, d.get('doc_party', ''),
                      d.get('source', '')))
        counts['worklist'] += 1
    conn.close()
    items.sort(key=lambda t: t[0])
    with open(WORKLIST, 'w', encoding='utf-8') as f:
        f.write("rq_id\tsrc\text\told_party\told_source\n")
        for rid, src, op, osrc in items:
            f.write("%s\t%s\t%s\t%s\t%s\n" % (
                rid, src, os.path.splitext(src)[1].lower(), op, osrc))
    return items, counts


def main():
    global ARK_PRIMARY
    ap = argparse.ArgumentParser()
    ap.add_argument('--workers', type=int, default=12)
    args = ap.parse_args()
    if not os.environ.get("ALIYUN_API_KEY"):
        print("FATAL: ALIYUN_API_KEY not set in environment", flush=True)
        sys.exit(2)
    llm = LLM()
    ARK_PRIMARY = pick_providers(llm)
    print("ark primary: %s | model: %s | billing: %s" % (
        ARK_PRIMARY["name"], ARK_PRIMARY["model"],
        ARK_PRIMARY.get("billing")), flush=True)
    print("aliyun: model=%s (key from env only)" % ALIYUN_MODEL, flush=True)
    items, counts = load_worklist()
    print('worklist counts: %s' % dict(counts), flush=True)
    print('worklist items: %d (old-无法判断 %d, fresh %d)' % (
        len(items), sum(1 for it in items if it[2] == '无法判断'),
        sum(1 for it in items if it[2] != '无法判断')), flush=True)
    done = {}
    if os.path.exists(RESULTS):
        with open(RESULTS, encoding='utf-8') as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                    done[d['src']] = d
                except Exception:
                    continue
    todo = [it for it in items if it[1] not in done]
    print('already in results: %d; to process: %d' % (len(done), len(todo)),
          flush=True)
    if not todo:
        print('nothing to do', flush=True)
        return
    _hb(label='剩余文档重分类', kind='remaining-docs', phase='doc',
        status='running', total=len(todo), done=0)
    t0 = time.time()
    n_done = 0
    by_why = Counter()
    with open(RESULTS, 'a', encoding='utf-8') as fh:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futs = {ex.submit(process_one, it, i): it
                    for i, it in enumerate(todo)}
            for fut in as_completed(futs):
                try:
                    rec = fut.result()
                except Exception as e:  # noqa: BLE001
                    rec = {'src': futs[fut][1], 'kind': '文档',
                           'category': '待确认', 'confidence': 0.0,
                           'reason': 'worker-exception:%s' % type(e).__name__,
                           'evidence': '', 'why': 'doc-api_error',
                           'suggested_dst': '', 'party': '', 'subdir': '',
                           'rq_id': futs[fut][0]}
                fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
                fh.flush()
                n_done += 1
                by_why[rec.get('why')] += 1
                if n_done % 25 == 0:
                    print('PROG %d/%d %.0fs why=%s stats=%s' % (
                        n_done, len(todo), time.time() - t0, dict(by_why),
                        dict(STATS.c)), flush=True)
                    _hb(label='剩余文档重分类', kind='remaining-docs',
                        phase='doc', status='running', total=len(todo),
                        done=n_done)
    summary = {
        'total_worklist': len(items), 'processed': n_done,
        'elapsed_sec': round(time.time() - t0, 1),
        'by_why': dict(by_why),
        'stats': dict(STATS.c),
        'aliyun_calls': STATS.c.get('aliyun_calls', 0),
        'aliyun_prompt_tokens': STATS.aliyun_prompt,
        'aliyun_completion_tokens': STATS.aliyun_completion,
        'aliyun_total_tokens': STATS.aliyun_prompt + STATS.aliyun_completion,
        'ark_calls': STATS.c.get('ark_calls', 0),
        'exclusion_counts': dict(counts),
    }
    with open(SUMMARY, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print('DONE %d in %.1fs why=%s' % (n_done, time.time() - t0, dict(by_why)),
          flush=True)
    print('SUMMARY %s' % json.dumps(summary, ensure_ascii=False), flush=True)
    _hb(label='剩余文档重分类', kind='remaining-docs', phase='doc',
        status='done', total=len(todo), done=n_done)


if __name__ == '__main__':
    main()
