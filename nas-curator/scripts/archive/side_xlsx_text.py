#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Re-extract .xlsx sidecars with openpyxl (read-only, values only).
Usage: python3 side_xlsx_text.py --batch <index.tsv> <sidecar_dir>
  processes index rows where extractor=='ooxml' and src endswith .xlsx
"""
import sys, os
import openpyxl


def extract(path, max_rows=40, max_sheets=3):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    parts = []
    try:
        for ws in wb.worksheets[:max_sheets]:
            parts.append("[sheet %s]" % ws.title)
            n = 0
            for row in ws.iter_rows(values_only=True):
                vals = [str(v).strip() for v in row if v is not None and str(v).strip() != ""]
                if vals:
                    parts.append(" | ".join(vals))
                    n += 1
                if n >= max_rows:
                    break
    finally:
        wb.close()
    return "\n".join(parts)


def main():
    idx_path, side_dir = sys.argv[2], sys.argv[3]
    rows = []
    for line in open(idx_path, encoding="utf-8"):
        p = line.rstrip("\n").split("\t")
        if len(p) >= 9:
            rows.append(p)
    targets = [r for r in rows if r[6] == "ooxml" and r[1].lower().endswith(".xlsx")]
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


if __name__ == "__main__":
    main()
