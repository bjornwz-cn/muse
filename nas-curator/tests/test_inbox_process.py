#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 测试：inbox / process 命令（本地 /tmp 夹具，无模型、无 NAS）。

 1 inbox 缺失目录 → 报空、不建目录、返回 0
 2 inbox 扫一次 → 只处理稳定文件（settle 内改动的跳过），归位 dst 落工作库
 3 process 目录 → 盘点+分类+归位预览，dst 落工作库
 4 process 单文件 → 1 项，文档规则层分类
 5 process 单个媒体文件（无模型）→ 1 项 media，不崩
规则：curator_core/rules.example.json（通用 10 类模板）。
用法：python3 -m pytest tests/test_inbox_process.py -v 或 python3 tests/test_inbox_process.py
"""
import csv
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT_ROOT)

RULES_EXAMPLE = os.path.join(PROJECT_ROOT, "curator_core", "rules.example.json")

import curator as CUR  # noqa: E402


def _write(path, data, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, encoding="utf-8" if "b" not in mode else None) as f:
        f.write(data)


def _rows(csv_path):
    with open(csv_path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def run():
    passed = []
    with tempfile.TemporaryDirectory(prefix="nas-cur-inbox-") as tmp:
        db = os.path.join(tmp, "t.db")
        # --- 1 inbox 缺失目录 ---
        rc = CUR.main(["--db", db, "inbox", "--inbox",
                       os.path.join(tmp, "no-such-inbox"),
                       "--rules", RULES_EXAMPLE, "--no-ocr",
                       "--out", os.path.join(tmp, "p0.csv")])
        assert rc == 0, f"1 缺失收件箱应返回 0：{rc}"
        assert not os.path.exists(os.path.join(tmp, "no-such-inbox")), "1 不得新建收件箱目录"
        passed.append("1 inbox 缺失目录报空、不建目录、返回 0")

        # --- 2 inbox 扫一次（settle） ---
        inbox = os.path.join(tmp, "inbox")
        os.makedirs(inbox)
        old = os.path.join(inbox, "租赁合同.txt")
        _write(old, "房屋租赁合同 甲方：甲公司 乙方：乙公司 租金每月 10000 元。\n")
        fresh = os.path.join(inbox, "刚到的文件.txt")
        _write(fresh, "房屋租赁合同 甲方：丙公司 乙方：丁公司 租金每月 20000 元。\n")
        os.utime(old, (time.time() - 3600, time.time() - 3600))  # 1 小时前 → 稳定
        csv2 = os.path.join(tmp, "p2.csv")
        rc = CUR.main(["--db", db, "inbox", "--inbox", inbox,
                       "--settle-minutes", "3",
                       "--rules", RULES_EXAMPLE, "--no-ocr", "--out", csv2])
        assert rc == 0
        rows = _rows(csv2)
        srcs = [r["src"] for r in rows]
        assert old in srcs, f"2 稳定文件应进计划：{srcs}"
        assert fresh not in srcs, f"2 未稳定文件应跳过：{srcs}"
        move = [r for r in rows if r["action"] in ("move", "rename")]
        assert move and move[0]["dst"].startswith("/vol1/1000/工作文件/"), \
            f"2 文档归位 dst 应落工作库：{move}"
        passed.append(f"2 inbox 扫一次：稳定文件进计划、未稳定跳过、dst 落工作库（{len(rows)} 项）")

        # --- 3 process 目录 ---
        pdir = os.path.join(tmp, "pick")
        _write(os.path.join(pdir, "报价单.txt"),
               "报价单：设备报价明细，总价 120000 元，含税。\n")
        _write(os.path.join(pdir, "随手记.txt"), "今天天气不错。\n")
        csv3 = os.path.join(tmp, "p3.csv")
        rc = CUR.main(["--db", db, "process", pdir,
                       "--rules", RULES_EXAMPLE, "--no-ocr", "--out", csv3])
        assert rc == 0
        rows = _rows(csv3)
        assert len(rows) >= 2, f"3 目录应至少 2 项：{rows}"
        mv = [r for r in rows if r["action"] in ("move", "rename")]
        assert mv and all(r["dst"].startswith("/vol1/1000/工作文件/") for r in mv), \
            f"3 归位 dst 应落工作库：{mv}"
        passed.append(f"3 process 目录：{len(rows)} 项，归位预览 dst 落工作库")

        # --- 4 process 单文件 ---
        single = os.path.join(tmp, "single", "会议纪要.txt")
        _write(single, "会议纪要：评审通过，下周验收，参会人员张三李四。\n")
        csv4 = os.path.join(tmp, "p4.csv")
        rc = CUR.main(["--db", db, "process", single,
                       "--rules", RULES_EXAMPLE, "--no-ocr", "--out", csv4])
        assert rc == 0
        rows = _rows(csv4)
        assert len(rows) == 1, f"4 单文件应恰好 1 项：{rows}"
        assert rows[0]["category"].startswith("03-"), f"4 会议纪要应进 03-类：{rows[0]}"
        passed.append("4 process 单文件：1 项，进 03-会议与纪要")

        # --- 5 process 单个媒体文件（无模型，不崩） ---
        img = os.path.join(tmp, "single", "shot.jpg")
        _write(img, b"\xff\xd8\xff\xe0" + b"\x00" * 200, mode="wb")
        csv5 = os.path.join(tmp, "p5.csv")
        rc = CUR.main(["--db", db, "process", img, "--no-ocr", "--out", csv5])
        assert rc == 0
        rows = _rows(csv5)
        assert len(rows) == 1 and rows[0]["src"] == img, f"5 媒体单文件应 1 项：{rows}"
        passed.append("5 process 单个媒体文件（无模型）：1 项，不崩")

    print("\n".join("  ✓ " + p for p in passed))
    print(f"PASS：inbox/process 断言 {len(passed)}/5 通过")
    return len(passed)


if __name__ == "__main__":
    run()
