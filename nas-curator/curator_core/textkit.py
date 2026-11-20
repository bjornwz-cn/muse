#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""textkit —— 文件内容理解层（v2.0）

职责：把任意文件变成「可判定的证据」：
  - 抽取：PDF/Word/Excel/PPT/邮件/图片/文本/压缩包目录
  - 结构：标题、日期、表头、页数
  - 命名：把无意义文件名改成人类可读名
  - 目录：聚合成目录画像（类型分布/时间跨度/主题词）
  - 近似：simhash 判近重复

不做任何写操作，纯只读。
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import zipfile
from collections import Counter

TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".log", ".ini", ".yaml", ".yml",
            ".html", ".htm", ".srt", ".ass", ".py", ".sh", ".js", ".sql", ".bat", ".ps1"}
OOXML_EXT = {".docx", ".xlsx", ".pptx", ".docm", ".xlsm"}
OLE_EXT = {".doc", ".xls", ".ppt"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp", ".gif", ".heic", ".heif"}
MAIL_EXT = {".eml", ".msg"}
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"}
AV_EXT = {".mp4", ".mov", ".mkv", ".avi", ".mp3", ".wav", ".flac", ".m4a"}
NEED_TEXT = TEXT_EXT | OOXML_EXT | OLE_EXT | IMG_EXT | {".pdf"} | MAIL_EXT

NOISE_LINE = re.compile(
    r"^(第?\s*\d+\s*页|page\s*\d+|\d+\s*/\s*\d+|机密|内部资料|confidential|"
    r"版权所有|copyright|http[s]?://\S+|\S+@\S+\.\w+|[-—_=·\s]{3,})$", re.I)

# 中文停用词（关键词提取用）
STOP = set("""的 了 和 与 及 或 在 是 为 对 从 到 由 按 将 把 被 让 使 以 之 其 这 那 有 无 不 也 都 就 而
我们 你们 他们 本 该 此 各 每 请 您 我 你 他 她 它 已 未 可 需 应 并 等 中 上 下 内 外 前 后 左右 一个 一些
以及 关于 根据 通过 进行 相关 如下 以上 以下 说明 备注 内容 情况 工作 问题 时间 日期 编号 附件 文件 文档
the and for with from this that are was were will shall have has not you your our we they""".split())

BAD_NAME = re.compile(
    r"^(img|dsc|dscn|pxl|vid|mov|audio|photo|image|scan|scanning|screenshot|screen shot|"
    r"微信图片|微信截图|屏幕截图|截图|未命名|无标题|新建|新建文档|新建文件夹|文档|文件|"
    r"扫描件|扫描|图片|照片|副本|copy|untitled|document|file|download|下载)\s*[-_ ()]*\d*$", re.I)


LOCAL_OCR_URL = "http://127.0.0.1:8866"
GATEWAY_OCR_URL = "http://172.17.0.1:8866"
_OCR_URL_CACHE = {"url": None}


def resolve_ocr_url(timeout=2.0) -> str:
    """OCR 默认地址（0.1.3）：环境变量 OCR_URL 优先；否则探测。

    容器内 127.0.0.1 指向容器自身，ocr-service 在宿主 docker 网桥上，
    所以 127.0.0.1:8866 在容器里永远不通（Connection refused）；
    探测不到就用宿主网关地址 172.17.0.1:8866。GET /ocr 返回 405
    （只接受 POST）也算可达。结果按进程缓存；两边都不通时默认网关地址
    （容器内正确地址；本机开发无 OCR 服务时调用仍会失败并返回空文本，
    不抛错）。"""
    import urllib.error
    import urllib.request
    env = os.environ.get("OCR_URL", "").strip()
    if env:
        return env.rstrip("/")
    if _OCR_URL_CACHE["url"]:
        return _OCR_URL_CACHE["url"]
    for cand in (LOCAL_OCR_URL, GATEWAY_OCR_URL):
        try:
            req = urllib.request.Request(cand + "/ocr", method="GET")
            with urllib.request.urlopen(req, timeout=timeout):
                pass
        except urllib.error.HTTPError:
            _OCR_URL_CACHE["url"] = cand  # 任意 HTTP 状态（含 405）即为可达
            return cand
        except Exception:
            continue
    _OCR_URL_CACHE["url"] = GATEWAY_OCR_URL
    return GATEWAY_OCR_URL


def run_cmd(cmd, timeout=60, stdin_data=None):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, input=stdin_data)
        out = r.stdout or b""
        return out.decode("utf-8", "ignore") or out.decode("gbk", "ignore")
    except Exception:
        return ""


def clean_text(s: str) -> str:
    s = s.replace("\x00", " ").replace("\ufeff", " ")
    s = re.sub(r"[\t\r\f\v]+", " ", s)
    s = re.sub(r" {2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def good_lines(text: str, limit=40):
    """挑出有信息量的行（去页码/页眉/空行）。"""
    out = []
    for ln in text.splitlines():
        ln = ln.strip(" \u3000")
        if len(ln) < 2 or NOISE_LINE.match(ln):
            continue
        out.append(ln)
        if len(out) >= limit:
            break
    return out


# ---------------- 抽取 ----------------

def ooxml_text(path, ext, max_chars=3000):
    """docx/xlsx/pptx：解析真实结构，保留标题/表头。"""
    parts = []
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if ext in (".docx", ".docm"):
                xml = z.read("word/document.xml").decode("utf-8", "ignore")
                # 标题样式段落优先
                for m in re.finditer(r"<w:p\b.*?</w:p>", xml, re.S):
                    p = m.group(0)
                    style = re.search(r'w:pStyle w:val="([^"]+)"', p)
                    txt = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, re.S))
                    txt = clean_text(re.sub(r"<[^>]+>", "", txt))
                    if not txt:
                        continue
                    if style and re.search(r"(?i)heading|标题", style.group(1)):
                        parts.append("【标题】" + txt)
                    else:
                        parts.append(txt)
                    if sum(len(x) for x in parts) > max_chars:
                        break

    except Exception:
        pass

    if ext in (".xlsx", ".xlsm"):
        try:
            import openpyxl  # type: ignore
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            parts.append("【工作表】" + "、".join(wb.sheetnames))
            for ws in wb.worksheets[:3]:
                rows = 0
                for row in ws.iter_rows(values_only=True):
                    vals = [str(c).strip() for c in row if c not in (None, "")]
                    if vals:
                        parts.append(" | ".join(vals[:10]))
                        rows += 1
                    if rows >= 15 or sum(len(x) for x in parts) > max_chars:
                        break
            wb.close()
        except Exception:
            # 回退：sharedStrings + sheet 名
            try:
                with zipfile.ZipFile(path) as z:
                    if "xl/sharedStrings.xml" in z.namelist():
                        ss = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
                        for m in re.findall(r"<t[^>]*>(.*?)</t>", ss, re.S)[:200]:
                            parts.append(clean_text(m))
                    wb = z.read("xl/workbook.xml").decode("utf-8", "ignore")
                    sheets = re.findall(r'<sheet[^>]*name="([^"]+)"', wb)
                    if sheets:
                        parts.insert(0, "【工作表】" + "、".join(sheets))
            except Exception:
                pass

    if ext in (".pptx",):
        try:
            with zipfile.ZipFile(path) as z:
                slides = sorted([n for n in z.namelist()
                                 if re.match(r"ppt/slides/slide\d+\.xml$", n)],
                                key=lambda x: int(re.findall(r"\d+", x)[-1]))
                for i, n in enumerate(slides[:12]):
                    xml = z.read(n).decode("utf-8", "ignore")
                    txts = [clean_text(t) for t in re.findall(r"<a:t>(.*?)</a:t>", xml, re.S)]
                    txts = [t for t in txts if t]
                    if txts:
                        parts.append("【页%d】" % (i + 1) + " / ".join(txts[:6]))
        except Exception:
            pass

    return clean_text("\n".join(parts))[:max_chars]


def eml_text(path, max_chars=3000):
    import email
    from email import policy
    out = []
    try:
        with open(path, "rb") as f:
            msg = email.message_from_binary_file(f, policy=policy.default)
        for h in ("Date", "From", "To", "Subject"):
            v = msg.get(h)
            if v:
                out.append(f"{h}: {v}")
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        body = part.get_content()
                        break
                    except Exception:
                        continue
        else:
            try:
                body = msg.get_content()
            except Exception:
                body = ""
        body = clean_text(body or "")
        # 去掉引用历史
        body = re.split(r"\n(-{2,}\s*原始邮件|-{5,}|On .{0,80}wrote:)", body)[0]
        out.append(body[:max_chars])
    except Exception:
        return ""
    return clean_text("\n".join(out))[:max_chars]


def msg_text(path, max_chars=3000):
    """Outlook .msg：按 UTF-16LE 抽取属性流文本片段。"""
    try:
        import olefile
        out = []
        if not olefile.isOleFile(path):
            return ""
        with olefile.OleFileIO(path) as ole:
            for stream in ("__substg1.0_0037001F", "__substg1.0_0C1A001F",  # 主题/发件人
                           "__substg1.0_1000001F"):                        # 正文
                if ole.exists(stream):
                    try:
                        raw = ole.openstream(stream).read()
                        out.append(raw.decode("utf-16-le", "ignore"))
                    except Exception:
                        pass
        return clean_text("\n".join(out))[:max_chars]
    except Exception:
        return ""


def ocr_file(path, ocr_url=None, timeout=90, max_chars=1500):
    """调 ocr-service /ocr 识别图片或扫描版 PDF。

    ocr_url 为空时自动探测（resolve_ocr_url；容器内用网关地址）。

    已知限制：Docling 中文 OCR 的数字会系统性误读为 0（2026-11-15 归档实测）。
    OCR 文本只用于判类/检索，绝不可信赖其中的数字（如金额、日期、编号）；
    不要试图“修”这个误读——这是上游模型行为，不是本层 bug。
    """
    ocr_url = (ocr_url or resolve_ocr_url()).rstrip("/")
    try:
        import requests
        with open(path, "rb") as f:
            r = requests.post(ocr_url + "/ocr",
                              files={"file": (os.path.basename(path), f)},
                              timeout=timeout)
        if r.status_code == 200:
            j = r.json()
            txt = j.get("text") or j.get("result") or ""
            if isinstance(txt, list):
                txt = "\n".join(str(t) for t in txt)
            return clean_text(str(txt))[:max_chars]
    except Exception:
        pass
    return ""


def pdf_text(path, max_chars=3000):
    txt = run_cmd(["pdftotext", "-f", "1", "-l", "2", "-layout", path, "-"], timeout=60)
    txt = clean_text(txt)
    if len(txt) >= 15:
        pages = run_cmd(["pdfinfo", path], timeout=20)
        m = re.search(r"Pages:\s*(\d+)", pages)
        if m:
            txt = f"【页数】{m.group(1)}\n" + txt
        return txt[:max_chars]
    return ""


def extract_text(path, ext=None, max_chars=3000, ocr_url=None,
                 ocr_timeout=90, allow_ocr=True):
    """返回 (文本, 来源标记)。来源用于诊断：pdf/docx/ocr/cached/empty/cloudsync。

    ocr_url 为空时自动探测（resolve_ocr_url；容器内用网关地址）。
    PDF 先读文字层，无文字层时才调 OCR（来源 pdf-ocr）；图片直接 OCR（img-ocr）。
    """
    ext = (ext or os.path.splitext(path)[1]).lower()
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except Exception as e:
        return "", f"err:{type(e).__name__}"
    if b"__CLOUDSYNC_ENC__" in head:
        return "", "cloudsync"

    if ext in TEXT_EXT:
        try:
            raw = open(path, "rb").read(1 << 20)
            for enc in ("utf-8", "gbk", "utf-16"):
                try:
                    return clean_text(raw.decode(enc))[:max_chars], "text"
                except Exception:
                    continue
        except Exception:
            pass
        return "", "empty"

    if ext == ".pdf":
        t = pdf_text(path, max_chars)
        if t:
            return t, "pdf"
        if allow_ocr:
            t = ocr_file(path, ocr_url, ocr_timeout, max_chars)
            if t:
                return t, "pdf-ocr"
        return "", "scanned"

    if ext in OOXML_EXT:
        t = ooxml_text(path, ext, max_chars)
        return t, ("ooxml" if t else "empty")

    if ext in OLE_EXT:
        tool = {".doc": "catdoc", ".xls": "xls2csv", ".ppt": "catppt"}[ext]
        t = clean_text(run_cmd([tool, path], timeout=60))[:max_chars]
        return t, (tool if t else "empty")

    if ext in MAIL_EXT:
        t = eml_text(path, max_chars) if ext == ".eml" else msg_text(path, max_chars)
        return t, ("eml" if ext == ".eml" else "msg") if t else "empty"

    if ext in IMG_EXT:
        if allow_ocr:
            t = ocr_file(path, ocr_url, ocr_timeout, max_chars)
            if t:
                return t, "img-ocr"
        return "", "image"
    if ext == ".msg":
        t = msg_text(path, max_chars)
        return t, "msg" if t else "empty"
    return "", "skip"


# ---------------- 结构理解 ----------------

DATE_PAT = [
    re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})"),
    re.compile(r"(20\d{2})[-/.年](\d{1,2})月?(?!\d)"),
    re.compile(r"\b(20\d{2})(\d{2})(\d{2})\b"),
]


def find_date(text: str, name: str = "", fallback=None):
    for src in (name or "", text or ""):
        for pat in DATE_PAT:
            m = pat.search(src)
            if not m:
                continue
            g = m.groups()
            y = int(g[0])
            mo = int(g[1]) if len(g) > 1 and g[1] else 1
            d = int(g[2]) if len(g) > 2 and g[2] else 1
            if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{mo:02d}-{d:02d}"
    return fallback


def guess_title(text: str, name: str = "", maxlen=48):
    """从正文里找标题：优先【标题】标记，其次首个有信息量的短行。"""
    if not text:
        return ""
    m = re.search(r"【标题】\s*(.{2,60})", text)
    if m:
        return m.group(1).strip()
    for ln in good_lines(text, 25):
        s = re.sub(r"^[#*\-\d.、,，)）\s]+", "", ln).strip()
        s = re.sub(r"^(主题|标题|事由|关于)\s*[:：]\s*", "", s)
        if not (3 <= len(s) <= 60):
            continue
        if re.search(r"^(日期|时间|编号|发件人|收件人|至|from|to|date|subject)\s*[:：]", s, re.I):
            continue
        if sum(c.isdigit() for c in s) > len(s) * 0.6:
            continue
        if re.fullmatch(r"[\w.\- ]{1,20}\.(docx?|xlsx?|pdf|jpg|png)", s, re.I):
            continue
        return s[:maxlen]
    return ""


def keywords(text: str, topn=8):
    """中英混排关键词（无第三方分词）：中文 2-4gram + 英文词，按频次×长度。"""
    if not text:
        return []
    en = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", text)]
    cnt = Counter(w for w in en if w not in STOP)
    zh = re.findall(r"[\u4e00-\u9fa5]{2,}", text)
    for seg in zh:
        for n in (2, 3, 4):
            for i in range(len(seg) - n + 1):
                w = seg[i:i + n]
                if w in STOP:
                    continue
                cnt[w] += 1
    scored = [(w, c * (1.6 if re.search(r"[\u4e00-\u9fa5]", w) else 1.0) * min(len(w), 4))
              for w, c in cnt.items() if c >= 2]
    scored.sort(key=lambda x: -x[1])
    out, seen = [], set()
    for w, _ in scored:
        if any(w in o for o in seen):
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= topn:
            break
    return out


def is_meaningless(name: str, ext: str = "") -> bool:
    stem = os.path.splitext(name)[0].strip()
    if len(stem) <= 2:
        return True
    # 中文办公场景的高频垃圾名：未命名文档1 / 新建 Microsoft Word 文档 / 文档(3)
    if re.search(r"未命名|无标题", stem):
        return True
    if re.fullmatch(r"(新建文件夹|新建目录|新文件夹|new folder|untitled folder)\s*[（(]?\d*[)）]?", stem, re.I):
        return True
    if re.search(r"^新建\s*(microsoft\s*)?(word|excel|powerpoint|文本文档|文档|文件|演示文稿|工作表)", stem, re.I):
        return True
    if re.fullmatch(r"(文档|文件|工作簿|演示文稿|扫描件|图片|照片)\s*[（(]?\d*[)）]?", stem):
        return True
    if re.fullmatch(r"[\d\W_]+", stem):
        return True
    if re.fullmatch(r"[0-9a-f]{16,}", stem, re.I):
        return True
    if BAD_NAME.match(stem):
        return True
    if re.search(r"(副本|copy|\(\d+\)|（\d+）)$", stem, re.I) and len(stem) < 12:
        return True
    # 纯日期/纯编号
    if re.fullmatch(r"(20\d{2}[-_.]?\d{1,2}[-_.]?\d{0,2}|[A-Z]{0,3}\d{4,12})", stem, re.I):
        return True
    return False


ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


def sanitize(name: str, maxlen=48, keep_ext=None):
    name = ILLEGAL.sub(" ", name)
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = re.sub(r"\s+", " ", name).strip(" ._-")
    if len(name) > maxlen:
        name = name[:maxlen].rstrip(" ._-")
    if keep_ext and not name.lower().endswith(keep_ext.lower()):
        name += keep_ext
    return name


def build_name(orig_name: str, ext: str, text: str, category: str = "",
               date: str = "", template="{date}_{title}", fallback_mtime=None):
    """给无意义文件名生成可读名；返回 (新文件名, 依据)。无法生成返回 ("","")。"""
    title = guess_title(text, orig_name)
    d = date or find_date(text, orig_name, fallback=fallback_mtime)
    if not title:
        kws = keywords(text, 4)
        if kws:
            title = "-".join(kws[:2])
            src = "kw"
        else:
            return "", ""
    else:
        src = "title"
    title = sanitize(title, 40)
    if not title or len(title) < 2:
        return "", ""
    nm = template.format(date=d or "", title=title, cat=category.split("/")[-1])
    nm = re.sub(r"^[_\-]+|[_\-]+$", "", nm)
    nm = sanitize(nm, 60)
    if not nm:
        return "", ""
    newname = nm + ext.lower()
    if newname == orig_name:
        return "", ""
    return newname, src


# ---------------- 近似判重 ----------------

def simhash(text: str, bits=64):
    if not text:
        return 0
    toks = re.findall(r"[A-Za-z]{3,}", text.lower())
    zh = re.findall(r"[\u4e00-\u9fa5]", text)
    toks += ["".join(zh[i:i + 2]) for i in range(len(zh) - 1)]
    if not toks:
        return 0
    v = [0] * bits
    for t, c in Counter(toks).items():
        h = int(hashlib.md5(t.encode()).hexdigest(), 16)
        for i in range(bits):
            v[i] += c if (h >> i) & 1 else -c
    out = 0
    for i in range(bits):
        if v[i] > 0:
            out |= (1 << i)
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# ---------------- 目录画像 ----------------

def dir_digest(path, max_files=400, max_depth=2):
    """只读遍历一个目录，产出画像：类型分布、时间跨度、主题词、子目录规模。"""
    ext_cnt = Counter()
    total = 0
    size = 0
    mtimes = []
    sub = []
    samples = []
    base_depth = path.rstrip("/").count("/")
    for dp, dns, fns in os.walk(path):
        if dp.rstrip("/").count("/") - base_depth >= max_depth:
            dns[:] = []
        dns[:] = [d for d in dns if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
        for fn in fns:
            if fn.startswith("."):
                continue
            p = os.path.join(dp, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            ext_cnt[os.path.splitext(fn)[1].lower() or "(无)"] += 1
            total += 1
            size += st.st_size
            mtimes.append(st.st_mtime)
            if len(samples) < max_files:
                samples.append(p)
            if total >= max_files * 5:
                break
        if total >= max_files * 5:
            break
    texts = []
    for p in samples[:40]:
        ext = os.path.splitext(p)[1].lower()
        if ext in NEED_TEXT - IMG_EXT:
            t, _ = extract_text(p, ext, 800, allow_ocr=False)
            if t:
                texts.append(t)
    kw = keywords("\n".join(texts), 10)
    span = ""
    if mtimes:
        span = (f"{__import__('datetime').datetime.fromtimestamp(min(mtimes)):%Y-%m} ~ "
                f"{__import__('datetime').datetime.fromtimestamp(max(mtimes)):%Y-%m}")
    for d in sorted(os.listdir(path))[:30]:
        fp = os.path.join(path, d)
        if os.path.isdir(fp) and not d.startswith("."):
            n = 0
            for _, _, fns in os.walk(fp):
                n += len(fns)
                if n > 5000:
                    break
            sub.append((d, n))
    return {
        "path": path, "files": total, "bytes": size, "span": span,
        "ext_top": ext_cnt.most_common(12), "keywords": kw,
        "subdirs": sorted(sub, key=lambda x: -x[1])[:20],
        "sample": [os.path.basename(x) for x in samples[:12]],
    }
