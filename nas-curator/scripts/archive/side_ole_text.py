#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OLE text heuristic extraction (container, olefile) with readability filter.
Usage: python3 side_ole_text.py <file...>  (prints first 1500 chars each)
Batch: python3 side_ole_text.py --batch <index.tsv> <sidecar_dir>
  processes index rows where extractor=='catdoc' and text_len==0
"""
import sys, os, re
import olefile

MAIN = {"doc": ["WordDocument"], "xls": ["Workbook", "Book"],
        "ppt": ["PowerPoint Document"]}
PAT = re.compile(r"[\u4e00-\u9fffA-Za-z0-9 _\-,.;:()\[\]\"'，。、；：！？·—/\\]{2,}")
COMMON = set("的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部度家电力里如心学只没经还方实之信战车区号北京南天市海产品价格销售客户订单合同公司医院系统管理资料文件项目会议周报月度年度")
ASCII_WORD = re.compile(r"[A-Za-z]{3,}")


def readable(run):
    if len(run) > 80:
        return False
    n = len(run)
    good = sum(1 for ch in run if ch in COMMON or ch.isascii())
    if good / max(n, 1) < 0.45:
        return False
    return True


def runs_u16(data):
    try:
        s = data.decode("utf-16-le", errors="ignore")
    except Exception:
        return []
    return [m.group(0).strip() for m in PAT.finditer(s)]


def runs_gbk(data):
    try:
        s = data.decode("gbk", errors="ignore")
    except Exception:
        return []
    return [m.group(0).strip() for m in PAT.finditer(s)]


def extract(path):
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    ole = olefile.OleFileIO(path)
    streams = MAIN.get(ext)
    if not streams:
        streams = ["WordDocument", "Workbook", "Book", "PowerPoint Document"]
    texts = []
    for name in streams:
        if not ole.exists(name):
            continue
        try:
            data = ole.openstream(name).read()
        except Exception:
            continue
        texts += runs_u16(data)
        texts += runs_gbk(data)
    seen = set()
    uniq = []
    for t in texts:
        if len(t) < 2 or t in seen:
            continue
        seen.add(t)
        if readable(t):
            uniq.append(t)
    return " ".join(uniq)


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--batch":
        idx_path, side_dir = sys.argv[2], sys.argv[3]
        rows = []
        for line in open(idx_path, encoding="utf-8"):
            p = line.rstrip("\n").split("\t")
            if len(p) >= 9:
                rows.append(p)
        targets = [r for r in rows if r[6] == "catdoc" and int(r[7]) == 0]
        print("targets: %d" % len(targets))
        for r in targets:
            idx, src = int(r[0]), r[1]
            try:
                t = extract(src)[:4000]
            except Exception as e:
                print("ERR %d %s %s" % (idx, src, e))
                continue
            with open(os.path.join(side_dir, "%d.txt" % idx), "w", encoding="utf-8") as f:
                f.write(t)
            print("ok %d len=%d %s" % (idx, len(t), src.split("/")[-1]))
        return
    for p in sys.argv[1:]:
        try:
            t = extract(p)
            print("== %s (%d chars)" % (p, len(t)))
            print(t[:1500])
        except Exception as e:
            print("== %s ERR %s" % (p, e))


if __name__ == "__main__":
    main()
