#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""side-classify D/E/F 文本抽取（宿主机执行，标准库 only）。
对 3 处新来源（99-待确认-上海润心/北京供应链/辽宁器械）全部文件抽文本 sidecar，
供容器内分类脚本使用。只读：不移动、不修改、不写库。
抽取器（按扩展名 + file magic）：
  .pdf -> pdftotext -f 1 -l 3（仅前 3 页，找标题/日期/实体）
  .docx/.xlsx/.pptx -> zip 内 XML 去标签
  .doc/.xls/.ppt (OLE) -> catdoc；若实为 XML（北京收货表 .xls）-> 取前 2MB 去标签
  .txt/.csv/.webloc/.eml -> 前 4000 字
  .zip -> 内含文件名清单（标加密项）；.rar/.7z -> 7z/bsdtar 若可用，否则仅文件名
  .eddx/.emmx/.osheet/.odoc -> 试 zip 解 XML，否则 strings
  .pag / file 判 data（加密）/ .dwg / 其他不可读 -> 无文本
输出：/vol2/@appshare/nas-curator/data/side_text/<idx>.txt +
  /vol2/@appshare/nas-curator/data/side_text_index.tsv
  （idx, src, size, mtime, ext, magic, extractor, text_len, encrypted）
"""
import os, sys, re, subprocess, zipfile

BASES = ["/vol1/1000/收件箱/年份待确认-王震周报",
         "/vol1/1000/收件箱/99-待确认-刘音",
         "/vol1/1000/收件箱/99-待确认-家庭王震",
         "/vol1/1000/收件箱/待确认-媒体库",
         "/vol1/1000/收件箱/99-待确认-Mac下载"]
OUTDIR = "/vol2/@appshare/nas-curator/data/side_text2"
INDEX = "/vol2/@appshare/nas-curator/data/side_text2_index.tsv"
CAP = 4000


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


def is_xml(data):
    s = data[:200].lstrip()
    return s.startswith(b"<?xml") or s.startswith(b"<")


def extract(path, ext):
    """返回 (text, extractor, encrypted_flag)。"""
    magic = ""
    try:
        magic = subprocess.run(["file", "-b", path], capture_output=True,
                               timeout=30).stdout.decode(errors="replace").strip()
    except Exception:
        pass
    if magic == "data":
        return "", "encrypted-data", True
    size = os.path.getsize(path)
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
        if is_xml(data):
            raw = head_bytes(path, 2 * 1024 * 1024).decode(errors="replace")
            return strip_tags(raw)[:CAP], "spreadsheetml", False
        out = run(["catdoc", path], timeout=120)
        return out.decode(errors="replace")[:CAP], "catdoc", False
    if ext in (".txt", ".csv", ".webloc", ".eml"):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read(CAP), "plain", False
        except Exception:
            return "", "plain-fail", False
    if ext == ".zip":
        try:
            z = zipfile.ZipFile(path)
            names = []
            enc = False
            for info in z.infolist()[:80]:
                names.append(info.filename)
                if info.flag_bits & 0x1:
                    enc = True
            return "压缩包内含文件：\n" + "\n".join(names), "zip-list", enc
        except Exception:
            return "", "zip-fail", False
    if ext in (".rar", ".7z"):
        for cmd in (["7z", "l", path], ["bsdtar", "-tf", path]):
            out = run(cmd, timeout=120)
            if out:
                return out.decode(errors="replace")[:CAP], cmd[0] + "-list", False
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
    # 无扩展名 / 其他：看 magic
    if "text" in magic.lower() or "XML" in magic:
        raw = head_bytes(path, 2 * 1024 * 1024).decode(errors="replace")
        return strip_tags(raw)[:CAP], "magic-text", False
    if "Zip archive" in magic:
        try:
            z = zipfile.ZipFile(path)
            names = [i.filename for i in z.infolist()[:80]]
            return "压缩包内含文件：\n" + "\n".join(names), "zip-list", False
        except Exception:
            return "", "zip-fail", False
    return "", "unreadable:" + magic[:60], False


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    files = []
    for base in BASES:
        for root, _dirs, names in os.walk(base):
            for fn in names:
                files.append(os.path.join(root, fn))
    files.sort()
    print("files: %d" % len(files), flush=True)
    rows = []
    for idx, path in enumerate(files):
        ext = os.path.splitext(path)[1].lower()
        try:
            st = os.stat(path)
            size, mtime = st.st_size, int(st.st_mtime)
        except Exception:
            size, mtime = 0, 0
        try:
            text, extractor, enc = extract(path, ext)
        except Exception as e:
            text, extractor, enc = "", "extract-error:%s" % e, False
        with open(os.path.join(OUTDIR, "%d.txt" % idx), "w",
                  encoding="utf-8") as f:
            f.write(text)
        magic = ""
        try:
            magic = subprocess.run(["file", "-b", path], capture_output=True,
                                   timeout=30).stdout.decode(
                                       errors="replace").strip()
        except Exception:
            pass
        rows.append((idx, path, size, mtime, ext, magic, extractor,
                     len(text), int(enc)))
        if (idx + 1) % 100 == 0:
            print("... %d/%d" % (idx + 1, len(files)), flush=True)
    with open(INDEX, "w", encoding="utf-8") as f:
        f.write("idx\tsrc\tsize\tmtime\text\tmagic\textractor\ttext_len\tencrypted\n")
        for r in rows:
            f.write("\t".join(str(x).replace("\t", " ") for x in r) + "\n")
    from collections import Counter
    print("extractors:", dict(Counter(r[6] for r in rows)), flush=True)
    print("encrypted:", sum(r[8] for r in rows), flush=True)
    print("with text:", sum(1 for r in rows if r[7] > 0), flush=True)
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
