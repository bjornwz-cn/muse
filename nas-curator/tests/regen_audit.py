#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 NAS 上运行：从已整理库抽样重建 152 条审计样本（只读：只抽正文，不写任何文件）。
输出：JSON 写到 stdout（name/text/ext/dir/size），由调用方保存到 tests/data/。

用法（从本机 VM）：
  ~/workspace/tools/nas-ssh.sh "<marvis-python> -" < tests/regen_audit.py > tests/data/audit_rows_regen.json
其中 <marvis-python> = /vol2/@appcenter/marvis-app/server/assets/bin/agent/runtime/python311/bin/python3

抽样口径与原 realsample.py 一致：seed 7，分层抽样（按类目文件数比例），
正文抽取 TK.extract_text(..., 900, allow_ocr=False)。原 152 条 audit_rows.json
已不在 NAS 上（2026-11-19 全盘查找无果），此为等口径重建集。
"""
import json
import os
import random
import sys

SKILL_SCRIPTS = ("/vol2/@appdata/marvis-app/data/agent/User"
                 "/oAN1i2d0ju6wSi2Svuma2iiBHGEU/skills/custom"
                 "/workfiles-curator/scripts")
sys.path.insert(0, SKILL_SCRIPTS)

import textkit as TK  # noqa: E402

ROOT = "/vol1/1000/工作文件/归档/3-北京供应链-已整理"
RULES = os.path.join(SKILL_SCRIPTS, "eval", "rules_v8.json")
N_TOTAL = 152
SEED = 7

JUNK = {".DS_Store", "Thumbs.db", "desktop.ini"}


def main():
    rules = json.load(open(RULES, encoding="utf-8"))
    valid = {c["path"] for c in rules["categories"]}
    by_cat = {}
    for d in sorted(os.listdir(ROOT)):
        full = os.path.join(ROOT, d)
        if not os.path.isdir(full) or d not in valid:
            continue
        pool = []
        for dp, dns, fns in os.walk(full):
            dns[:] = [x for x in dns if x not in (".git", "__MACOSX")]
            for fn in fns:
                if fn in JUNK or fn.startswith("._"):
                    continue
                pool.append(os.path.join(dp, fn))
        by_cat[d] = sorted(pool)
    total = sum(len(v) for v in by_cat.values())
    random.seed(SEED)
    rows = []
    for d, pool in by_cat.items():
        # 分层：按类目占比取整，至少 1 条；最后补齐到 N_TOTAL
        take = max(1, round(N_TOTAL * len(pool) / total)) if total else 0
        take = min(take, len(pool))
        for fp in random.sample(pool, take):
            rows.append((d, fp))
    random.shuffle(rows)
    rows = rows[:N_TOTAL]
    out = []
    for d, fp in rows:
        ext = os.path.splitext(fp)[1].lower()
        try:
            t, _how = TK.extract_text(fp, ext, 900, allow_ocr=False)
        except Exception:
            t = ""
        try:
            size = os.path.getsize(fp)
        except OSError:
            size = 0
        out.append({"dir": d, "name": os.path.basename(fp), "text": t or "",
                    "ext": ext, "size": size})
    sys.stdout.write(json.dumps({"n": len(out), "seed": SEED, "root": ROOT,
                                 "rows": out}, ensure_ascii=False))


if __name__ == "__main__":
    main()
