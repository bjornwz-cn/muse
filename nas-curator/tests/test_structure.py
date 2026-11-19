#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 结构断言 12 项（规则层，无模型、无 OCR、无 NAS；全部在 /tmp 内）。

断言口径以 v3.1.1 原版 cmd_plan / cmd_inventory / cmd_apply 的实际行为为准：
 1 程序目录（.git）整目录为一个 unit 项
 2 项目目录（package.json）不打散（一个 unit 项）
 3 邮件按 日期_发件人_主题 命名（rename=True）
 4 扫描件分流：图片型 PDF（无文字层、无 OCR）进扫描件类目
 5 空壳 eml（正文 <40 字）进 shell 类目
 6 回滚零差异：apply 后 rollback，文件回到原位且字节一致
 7 SHA256 精确重复：inventory 发现重复组（keep/dups）
 8 敏感文件（密码）进复核 sensitive
 9 同名目标加 __n，不覆盖
10 压缩包进 inventory 清单（完整性 7z 检测是手动流程，不在引擎断言内）
11 云同步加密壳（__CLOUDSYNC_ENC_）进隔离类目
12 三件套：apply（非 dry-run）返回 operations + report，可据此写出映射表/回滚脚本/报告

规则：curator_core/rules.example.json（通用 10 类模板）。
用法：python3 -m pytest tests/test_structure.py -v 或 python3 tests/test_structure.py
"""
import hashlib
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT_ROOT)

RULES_EXAMPLE = os.path.join(PROJECT_ROOT, "curator_core", "rules.example.json")
RULES_EXAMPLE_ALT = os.path.join(os.path.expanduser("~"), "workspace", "analysis",
                                 "workfiles-curator", "rules.example.json")


def _load_rules():
    for p in (RULES_EXAMPLE, RULES_EXAMPLE_ALT):
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8")), p
    raise FileNotFoundError("rules.example.json not found")


def _write(path, data, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, encoding="utf-8" if "b" not in mode else None) as f:
        f.write(data)


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _by_src(items, name):
    for it in items:
        if os.path.basename(it["src"]) == name or it["src"].endswith(name):
            return it
    return None


def run():
    from curator_core import engine as CE
    rules, _rp = _load_rules()
    shell_cat = rules["shell_category"]
    enc_cat = rules["cloudsync_category"]
    scan_cat = rules["scan_category"]
    passed = []

    with tempfile.TemporaryDirectory(prefix="nas-cur-struct-") as tmp:
        root = os.path.join(tmp, "lib")
        os.makedirs(root)
        # --- fixtures ---
        # 1/2 程序与项目目录
        _write(os.path.join(root, "prog", ".git", "config"), "[core]\n")
        _write(os.path.join(root, "prog", "app.py"), "print('hi')\n")
        _write(os.path.join(root, "proj", "package.json"), '{"name": "x"}\n')
        _write(os.path.join(root, "proj", "index.js"), "console.log(1)\n")
        # 3 邮件（正文 >40 字，带 Date/From/Subject）
        mail = ("Date: Tue, 18 Nov 2026 10:00:00 +0800\n"
                "From: 张三 <zhangsan@example.com>\n"
                "Subject: 关于波科平台合作的回复\n\n"
                "正文内容足够长，超过四十个字符，用于测试邮件命名规则是否生效。\n")
        _write(os.path.join(root, "mail.eml"), mail)
        # 4 扫描件：图片型 PDF（无文字层）——用最小合法 PDF（无文本对象）
        pdf = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
               b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
               b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]>>endobj\n"
               b"trailer<</Root 1 0 R>>\n%%EOF\n")
        _write(os.path.join(root, "合同扫描.pdf"), pdf, mode="wb")
        # 5 空壳 eml（正文极短）。注意：extract_text 返回含邮件头在内的全文，
        # 空壳阈值（<40 字）按抽取后全文计，故夹具只能用极简头，否则原版 v3.1.1
        # 同样不进空壳（已核对原版 curator.py:577 同款条件）。
        _write(os.path.join(root, "empty.eml"), "Subject: x\n\n短\n")
        # 7 SHA256 重复
        dup_bytes = b"same content for dup test 1234567890\n" * 10
        _write(os.path.join(root, "a_dup.txt"), dup_bytes, mode="wb")
        _write(os.path.join(root, "b_dup.txt"), dup_bytes, mode="wb")
        # 8 敏感文件
        _write(os.path.join(root, "账号密码.txt"),
               "ERP 登录账号：admin，密码：P@ssw0rd123，请勿外传。\n")
        # 9 同名：两个不同子目录下的同名合同（用 .txt：.pdf 夹具若只写纯文本、
        # 抽取不到文字层，原版同样进 unreadable，测不到同名逻辑）。
        # 另注意：auto 模式只下钻透明目录（收件箱/待处理/下载…），普通子目录
        # s1/s2 原版同样不扫描，故两文件须直接放在两个透明目录下。
        _write(os.path.join(root, "待处理", "租赁合同.txt"),
               "房屋租赁合同 甲方：甲公司 乙方：乙公司 租金每月 10000 元 租期一年。\n")
        _write(os.path.join(root, "下载", "租赁合同.txt"),
               "房屋租赁合同 甲方：丙公司 乙方：丁公司 租金每月 20000 元 租期两年。\n")
        # 10 压缩包（仅清单，不测完整性）
        _write(os.path.join(root, "pack.zip"), b"PK\x03\x04" + b"\x00" * 100, mode="wb")
        # 11 云同步加密壳（魔数须为 __CLOUDSYNC_ENC__，双下划线结尾，见 textkit）
        _write(os.path.join(root, "enc.docx"), b"__CLOUDSYNC_ENC__" + b"\x00" * 64, mode="wb")
        # 普通文档（对照组）
        _write(os.path.join(root, "2026年10月周报.docx"),
               "本周工作总结：完成波科平台对接，周报内容足够长用于分类测试。\n")

        # --- inventory ---
        inv = CE.inventory(root)
        assert inv.get("dup_group_count", 0) >= 1, "7 应发现 SHA256 重复组"
        passed.append("7 SHA256 精确重复：inventory 发现重复组")
        assert any(a.get("path", "").endswith("pack.zip")
                   for a in inv.get("archives", [])), "10 压缩包应进清单"
        passed.append("10 压缩包进 inventory 清单")

        # --- plan（规则层，无 llm/sem，无 OCR）---
        items, stats = CE.plan_files(root, rules, rename=True, no_ocr=True)
        by_src = {os.path.basename(i["src"]): i for i in items}
        # 1 程序目录
        prog = _by_src(items, "prog")
        assert prog and prog["kind"] == "unit", f"1 程序目录应为 unit 项：{prog}"
        assert prog["category"].startswith("11-程序与工具"), prog["category"]
        passed.append("1 程序目录整目录为一个 unit 项")
        # 2 项目目录
        proj = _by_src(items, "proj")
        assert proj and proj["kind"] == "unit", f"2 项目目录应为 unit 项：{proj}"
        passed.append("2 项目目录不打散")
        # 3 邮件命名
        m = _by_src(items, "mail.eml")
        assert m and re.match(r"^20261118_.+\.eml$", m.get("new_name") or ""), \
            f"3 邮件应按日期_发件人_主题命名：{m.get('new_name') if m else None}"
        passed.append(f"3 邮件命名：{m['new_name']}")
        # 4 扫描件分流
        sc = _by_src(items, "合同扫描.pdf")
        assert sc and sc["category"] == scan_cat, f"4 扫描件应进 {scan_cat}：{sc}"
        passed.append("4 扫描件分流进扫描件类目")
        # 5 空壳
        sh = _by_src(items, "empty.eml")
        assert sh and sh["category"] == shell_cat, f"5 空壳应进 {shell_cat}：{sh}"
        passed.append("5 空壳 eml 进 shell 类目")
        # 8 敏感
        sen = _by_src(items, "账号密码.txt")
        assert sen and sen["action"] == "review" and sen.get("review_kind") == "sensitive", \
            f"8 敏感文件应进复核 sensitive：{sen}"
        assert "P@ssw0rd" not in (sen.get("evidence") or ""), "8 证据不得含敏感内容"
        passed.append("8 敏感文件进复核 sensitive（证据不含内容）")
        # 11 加密壳
        enc = _by_src(items, "enc.docx")
        assert enc and enc["category"] == enc_cat, f"11 加密壳应进 {enc_cat}：{enc}"
        passed.append("11 云同步加密壳进隔离类目")

        # --- apply + rollback（真实移动，在 /tmp 内）---
        todo = [i for i in items if i.get("action") in ("move", "rename")]
        assert todo, "应有可执行项"
        before = {i["src"]: _sha(i["src"]) for i in todo if os.path.isfile(i["src"])}
        operations, report = CE.apply_plan(todo, root, dry_run=False)
        assert report.get("moved", 0) >= 1, "12 应有移动"
        assert operations, "12 应返回 operations（映射表依据）"
        passed.append(f"12 三件套依据：operations {len(operations)} 条，report moved={report.get('moved')}")
        # 9 同名 __n
        dsts = [o["dst_path"] for o in operations]
        lease_dsts = [d for d in dsts if d.endswith("租赁合同.txt") or "__1" in d]
        assert len(lease_dsts) == 2 and len(set(lease_dsts)) == 2, \
            f"9 同名目标应加 __n：{lease_dsts}"
        assert any("__1" in d for d in lease_dsts), "9 第二个同名应带 __1"
        passed.append("9 同名目标加 __n，不覆盖")
        # 6 回滚零差异（rollback_pairs 返回 (moved, missing, failed) 元组）
        from common import rollback as RB
        moved_n, missing, failed = RB.rollback_pairs(
            [(o["src_path"], o["dst_path"]) for o in operations])
        assert moved_n == len(operations), f"6 回滚应全数移回：moved={moved_n} missing={missing} failed={failed}"
        for src, h in before.items():
            assert os.path.exists(src), f"6 回滚后原文件应存在：{src}"
            assert _sha(src) == h, f"6 回滚后字节应一致：{src}"
        passed.append("6 回滚零差异（文件回原位且字节一致）")

    print("\n".join("  ✓ " + p for p in passed))
    print(f"PASS：结构断言 {len(passed)}/12 通过")
    return len(passed)


if __name__ == "__main__":
    n = run()
    sys.exit(0 if n == 12 else 1)
