#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""curator_core 引擎级冒烟测试（只动 /tmp，不动 NAS、不联网、不依赖模型服务）。

覆盖：规则层 plan_files（20 个样本：合同/发票/简历/周报/扫描件/
空壳 eml/程序目录 .git/敏感文件/重复文件/机器名/无关键词文件）、
action/confidence/review_kind 映射、apply_plan(dry_run) 不落盘、
apply_plan 实跑 + common.rollback.rollback_pairs 原样移回、
db_conn 跨轮 SHA256 去重、prescreen/sem 假实例注入、inventory、
explain、learn_rules、llmkit 密钥来源。

运行：python3 tests/test_curator_core_smoke.py（无 pytest 也可跑；
有 pytest 时也可 pytest tests/test_curator_core_smoke.py）。
"""
import hashlib
import json
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import db as common_db  # noqa: E402
from common.rollback import rollback_pairs  # noqa: E402
from curator_core import engine  # noqa: E402
from curator_core.engine import (  # noqa: E402
    apply_plan,
    explain,
    inventory,
    learn_rules,
    plan_files,
)
from curator_core.prescreen import Prescreener  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
RULES_PATH = os.path.join(PROJECT_ROOT, "curator_core", "rules.example.json")


def load_rules():
    with open(RULES_PATH, encoding="utf-8") as f:
        return json.load(f)


def w(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(path, mode, encoding=None if isinstance(content, bytes) else "utf-8") as f:
        f.write(content)
    return path


CONTRACT = ("供货合同\n甲方华润供应链与乙方强生公司签订本供货合同，双方就产品供货、"
            "价格结算、交货验收达成协议。合同条款包括：第一条供货范围，第二条价格与付款，"
            "第三条违约责任。本协议自双方签章之日起生效，合同一式两份。")
INVOICE = ("增值税专用发票 发票代码 1100192130，发票号码 98765432。购买方华润供应链，"
           "销售方北京力达康。货物名称医疗器械，税额合计人民币壹万贰仟元整。"
           "付款方式银行转账，结算日期 2024-03-15，请财务及时报销入账。")
RESUME = ("个人简历 姓名陈松沪，男，25 岁，意向岗位器械销售。教育经历：2014 年毕业于"
          "连云港师范高等专科学校。工作经历：2020 年至今就职于青海医疗器械公司，"
          "担任器械销售，负责区域销售与客户维护，面试通过后可入职。")
WEEKLY = ("2024 年第 10 周周报 本周工作总结：完成强生库存盘点与对账工作，推进沈阳分仓"
          "建设进度，协调供应商送货事宜。下周工作计划：跟进集采报价，整理本周工作动态，"
          "汇报重点工作完成情况。")
MINUTES = ("会议纪要 项目例会 2024-03-12 参会人员：王震、战轲、黄明辉。会议议程："
           "评审一季度经营指标，讨论验收标准，明确决议事项。会议记录：各事业部汇报"
           "进展，会议决定下周复核库存清单。")
LICENCE = ("营业执照 统一社会信用代码 91110119MA02AE87XE，企业名称华润（北京）医疗器械"
           "供应链管理有限公司，类型有限责任公司。经营范围：销售医疗器械。许可证编号："
           "京食药监械经营许 20170056 号，备案有效。")
QUOTATION = ("报价清单 力达康产品报价单 序号产品名称单价数量总价：骨科螺钉单价 120 元，"
             "锁定钢板单价 880 元。清单明细如下，询价比价后确认成本核算，总价合计 52800 元。")
NOTICE = ("通知 关于 2024 年清明节放假安排的通知：根据国家放假安排，4 月 4 日至 4 月 6 日"
          "放假调休。请各部门提前做好工作安排，提醒值班人员按时到岗，节后正常上班。")
SCHEME = ("技术方案 椎间融合器植入技术方案 本方案介绍产品技术参数、配置要求与施工步骤，"
           "包括实施方案、可行性分析与图纸说明。说明书详见附件，参数以注册证为准。")
PAYMENT = ("付款对账单 2024 年 2 月对账：应付账款 128400 元，已付款 90000 元，结算余额"
           "38400 元。请财务核对银行流水与报销凭证，确认回款后结算。")
SENSITIVE_BODY = ("2024 年第一季度工作安排表，请各部门按时提交。请携带身份证办理入职手续。")
MINIMAL_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
               b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
               b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
               b"trailer<</Root 1 0 R>>\n%%EOF\n")


def build_fixture(root):
    """建 19 个散文件 + 1 个程序目录（myapp，含 .git）。返回 (loose_paths, unit_path)。"""
    files = {
        "供货合同-强生.txt": CONTRACT,
        "增值税发票-2024.txt": INVOICE,
        "陈松沪简历.txt": RESUME,
        "2024年第10周周报.txt": WEEKLY,
        "会议纪要-项目例会.txt": MINUTES,
        "营业执照-华润.txt": LICENCE,
        "报价清单-力达康.txt": QUOTATION,
        "通知-放假安排.txt": NOTICE,
        "技术方案-实施方案.txt": SCHEME,
        "付款对账单-2024.txt": PAYMENT,
        "扫描件-20240101.pdf": MINIMAL_PDF,
        "空壳邮件.eml": "",
        "空文件.txt": "",
        "密码表-账号登录.txt": SENSITIVE_BODY,
        "供货合同-强生-副本.txt": CONTRACT,          # 与 #1 同字节 -> SHA256 重复
        "增值税发票-2024-副本.txt": INVOICE,         # 与 #2 同字节 -> SHA256 重复
        "IMG_20240101_120000.txt": "照片",
        # .xyz 不在任何类目 exts 里、无关键词 -> 0 分落默认类目（reason == 'default'）
        "随手记.xyz": "今天天气不错，去公园散步了一小时，晚上回家做饭。",
        "keyboard-layout.txt": ("This document describes the keyboard layout "
                                "for the new office laptops. Press the keys to test."),
    }
    paths = {}
    for name, content in files.items():
        paths[name] = w(os.path.join(root, name), content)
    unit = os.path.join(root, "myapp")
    w(os.path.join(unit, ".git", "HEAD"), "ref: refs/heads/main\n")
    w(os.path.join(unit, "package.json"), '{"name": "myapp", "version": "1.0.0"}\n')
    w(os.path.join(unit, "src", "app.py"), "print('hello')\n")
    return paths, unit


def by_src(items, name):
    for it in items:
        if os.path.basename(it["src"]) == name:
            return it
    raise AssertionError(f"找不到 plan-item: {name}")


def test_plan_rules_only():
    with tempfile.TemporaryDirectory() as root:
        paths, unit = build_fixture(root)
        items, stats = plan_files(root, load_rules(), no_ocr=True)
        assert len(items) == 20, f"应为 20 项，实得 {len(items)}"
        assert stats["total"] == 20
        assert stats["llm_calls"] == 0
        assert stats["prescreen_calls"] == 0
        # 规则命中（合同/发票各有一对同字节文件：其中一个正常归类，另一个进 dup；
        # 排序上“-副本”先于原文件，故不按文件名断言哪一个是 dup）
        pair = [by_src(items, n) for n in
                ("供货合同-强生.txt", "供货合同-强生-副本.txt")]
        assert sorted(x["category"] for x in pair) == \
            ["01-合同与协议", "99-待确认/06-重复待确认"], pair
        pair = [by_src(items, n) for n in
                ("增值税发票-2024.txt", "增值税发票-2024-副本.txt")]
        assert sorted(x["category"] for x in pair) == \
            ["08-财务与票据", "99-待确认/06-重复待确认"], pair
        assert by_src(items, "陈松沪简历.txt")["category"] == "09-人事与招聘"
        assert by_src(items, "2024年第10周周报.txt")["category"] == "06-周报与工作报告"
        assert by_src(items, "会议纪要-项目例会.txt")["category"] == "03-会议与纪要"
        assert by_src(items, "营业执照-华润.txt")["category"] == "05-资质与证照"
        assert by_src(items, "报价清单-力达康.txt")["category"] == "02-报价与清单"
        assert by_src(items, "通知-放假安排.txt")["category"] == "07-通知与公告"
        assert by_src(items, "技术方案-实施方案.txt")["category"] == "04-技术与方案"
        assert by_src(items, "付款对账单-2024.txt")["category"] == "08-财务与票据"
        # 规则强命中置信度 0.85/0.9，且为 move（取每对中正常归类的那个）
        for name in ("供货合同-强生-副本.txt", "营业执照-华润.txt"):
            it = by_src(items, name)
            assert it["action"] == "move", (name, it)
            assert it["confidence"] >= 0.85, (name, it)
            assert it["dst"] == os.path.join(root, it["category"], it["new_name"])
        # 程序目录整目录为一个 unit 项
        unit_item = by_src(items, "myapp")
        assert unit_item["kind"] == "unit", unit_item
        assert unit_item["action"] == "move", unit_item
        assert unit_item["category"] == "11-程序与工具/myapp", unit_item
        assert unit_item["dst"] == os.path.join(root, "11-程序与工具/myapp")
        assert unit_item["size"] > 0
        # 2026-11-24 计划 #160 修复：unit 项不再豁免（auto_exempt 恒 False），
        # 是否自动执行只由 is_auto_apply_eligible 判定（unit 永不自动执行）
        assert unit_item["auto_exempt"] is False, unit_item
        # 空壳进 shell 类目
        for name in ("空壳邮件.eml", "空文件.txt"):
            it = by_src(items, name)
            assert it["category"] == "99-待确认/空壳", (name, it)
            assert it["action"] == "review" and it["review_kind"] == "shell", (name, it)
            # 0.2.5 §3.2：review 项保留 dst 作建议目标（批量批准靠它搬文件）。
            assert it["dst"] == os.path.join(root, it["category"], it["new_name"]), (name, it)
            assert it["confidence"] < 0.60, (name, it)
        # 扫描件（无文本层）进 review/ambiguous
        it = by_src(items, "扫描件-20240101.pdf")
        assert it["action"] == "review" and it["review_kind"] == "ambiguous", it
        assert it["reason"] == "scanned-no-text", it
        # 机器名无正文 -> unreadable -> review/low_conf
        it = by_src(items, "IMG_20240101_120000.txt")
        assert it["action"] == "review" and it["review_kind"] == "low_conf", it
        assert it["reason"].startswith("unreadable:"), it
        # 无关键词 0 分落默认 -> review/low_conf（宁可空着，不可猜错）
        it = by_src(items, "随手记.xyz")
        assert it["category"] == "10-其他" and it["action"] == "review", it
        assert it["review_kind"] == "low_conf" and it["reason"] == "default", it
        # key 只在 keyboard/keys 里出现 -> 不触发敏感隔离（.txt 扩展名匹配得 1 分，走 low:）
        it = by_src(items, "keyboard-layout.txt")
        assert it["review_kind"] == "low_conf" and it["reason"].startswith("low:"), it
        assert it["category"] == "99-待确认/04-低置信", it
        # 含 review 项，且 plan-item 字段齐全
        assert any(it["action"] == "review" for it in items)
        for it in items:
            assert set(it) == {"src", "dst", "new_name", "category", "confidence",
                               "reason", "evidence", "action", "kind", "size",
                               "ext", "sha256", "tags", "review_kind", "file_id",
                               # P2(P1.1)：score（plan CSV 用）与 auto_exempt（unit 豁免）
                               "score", "auto_exempt",
                               # 0.2.5 §3.2：confidence_original（压之前的原始置信度）
                               "confidence_original"}, it
            assert isinstance(it["tags"], list)
            assert it["confidence"] <= 1.0
        assert stats["review"] >= 8, stats
        print("test_plan_rules_only: PASS (20 项，review %d 项)" % stats["review"])


def test_sensitive_isolation():
    with tempfile.TemporaryDirectory() as root:
        build_fixture(root)
        items, _ = plan_files(root, load_rules(), no_ocr=True)
        it = by_src(items, "密码表-账号登录.txt")
        assert it["category"] == "99-待确认/05-敏感隔离", it
        assert it["action"] == "review" and it["review_kind"] == "sensitive", it
        assert it["reason"] == "sensitive" and it["confidence"] == 0.0, it
        # evidence 只写命中词，绝不写文件内容片段
        assert "密码" in it["evidence"] and "身份证" in it["evidence"], it
        assert "工作安排表" not in it["evidence"], it
        assert "入职手续" not in it["evidence"], it
        print("test_sensitive_isolation: PASS")


def test_sha256_dup_within_run():
    with tempfile.TemporaryDirectory() as root:
        build_fixture(root)
        items, stats = plan_files(root, load_rules(), no_ocr=True)
        # 每对同字节文件恰好一个进 dup 复核，另一个正常走流程
        for pair in (("供货合同-强生.txt", "供货合同-强生-副本.txt"),
                     ("增值税发票-2024.txt", "增值税发票-2024-副本.txt")):
            a, b = (by_src(items, n) for n in pair)
            dups = [x for x in (a, b) if x["review_kind"] == "dup"]
            normals = [x for x in (a, b) if x["review_kind"] is None]
            assert len(dups) == 1 and len(normals) == 1, (pair, a, b)
            dup = dups[0]
            assert dup["action"] == "review" and dup["reason"] == "dup:sha256", dup
            assert dup["category"] == "99-待确认/06-重复待确认", dup
            assert dup["evidence"] == normals[0]["src"], dup  # 首次出现的文件路径
            assert dup["sha256"] == normals[0]["sha256"] and dup["sha256"], dup
        assert stats["dup_count"] == 2, stats
        assert stats["dup_reclaimable_bytes"] > 0, stats
        print("test_sha256_dup_within_run: PASS")


def test_sha256_dup_across_runs_via_db():
    with tempfile.TemporaryDirectory() as root:
        db_path = os.path.join(root, "test.db")
        common_db.init_db(db_path)
        conn = common_db.connect(db_path)
        try:
            a = w(os.path.join(root, "a.txt"),
                  "供货合同 甲方与乙方签订本供货合同，合同条款、协议内容详见正文，双方签章生效。")
            b = w(os.path.join(root, "b.txt"),
                  "周报 本周工作总结：完成库存盘点，推进分仓建设，汇报重点工作完成情况。")
            sha_a = hashlib.sha256(open(a, "rb").read()).hexdigest()
            # DB 里已有一条 active 记录（另一路径）与 a.txt 同 sha256
            common_db.upsert_file(conn, "/elsewhere/old-a.txt", sha256=sha_a,
                                  size=os.path.getsize(a), status="active")
            items, _ = plan_files(root, load_rules(), no_ocr=True, db_conn=conn)
            it_a = by_src(items, "a.txt")
            assert it_a["review_kind"] == "dup" and it_a["reason"] == "dup:sha256", it_a
            assert it_a["evidence"] == "/elsewhere/old-a.txt", it_a
            it_b = by_src(items, "b.txt")
            assert it_b["review_kind"] is None and it_b["action"] == "move", it_b
        finally:
            conn.close()
        print("test_sha256_dup_across_runs_via_db: PASS")


def test_apply_dry_run_and_real_then_rollback():
    with tempfile.TemporaryDirectory() as root:
        paths, unit = build_fixture(root)
        items, _ = plan_files(root, load_rules(), no_ocr=True)
        # dry_run 不落盘
        ops, report = apply_plan(items, root, dry_run=True)
        assert report["moved"] > 0 and report["skipped"] >= 8, report
        assert os.path.exists(paths["供货合同-强生.txt"])
        assert not os.path.exists(os.path.join(root, "01-合同与协议"))
        assert all(op["dst_path"] for op in ops)
        # 实跑：move 项搬走，review 项原地不动
        ops, report = apply_plan(items, root)
        assert report["moved"] == len(ops), report
        assert report["failed"] == [], report
        moved_srcs = {op["src_path"] for op in ops}
        assert paths["供货合同-强生.txt"] not in moved_srcs or \
            not os.path.exists(paths["供货合同-强生.txt"])
        for op in ops:
            assert os.path.exists(op["dst_path"]), op
            assert not os.path.exists(op["src_path"]), op
        assert os.path.exists(paths["密码表-账号登录.txt"])  # review 项不动
        assert os.path.exists(paths["空文件.txt"])
        assert os.path.isdir(os.path.join(root, "11-程序与工具", "myapp"))
        # 回滚：原样移回
        moved_n, missing, failed = rollback_pairs(
            [(op["src_path"], op["dst_path"]) for op in ops])
        assert moved_n == len(ops) and missing == [] and failed == [], \
            (moved_n, missing, failed)
        for op in ops:
            assert os.path.exists(op["src_path"]), op
            assert not os.path.exists(op["dst_path"]), op
        assert os.path.isdir(unit) and os.path.exists(os.path.join(unit, ".git", "HEAD"))
        print("test_apply_dry_run_and_real_then_rollback: PASS "
              f"(moved {len(ops)} 项，全部移回)")


def test_prescreen_fake():
    class FakePrescreener:
        sem = SimpleNamespace(cents={"01-合同与协议": True})

        def fit(self, rules):
            return 1

        def decide(self, name, text):
            if "合同" in name:
                return "01-合同与协议", 0.88, 0.15
            return None, 0.30, 0.01

    with tempfile.TemporaryDirectory() as root:
        w(os.path.join(root, "供货合同-强生.txt"), CONTRACT)
        w(os.path.join(root, "随手记.xyz"), "今天天气不错，去公园散步了一小时。")
        items, stats = plan_files(root, load_rules(), no_ocr=True,
                                  prescreener=FakePrescreener())
        it = by_src(items, "供货合同-强生.txt")
        assert it["reason"].startswith("prescreen:"), it
        assert it["confidence"] == 0.88 and it["category"] == "01-合同与协议", it
        assert stats["prescreen_calls"] == 2 and stats["prescreen_hits"] == 1, stats
        assert by_src(items, "随手记.xyz")["reason"] == "default"
        print("test_prescreen_fake: PASS")


def test_prescreener_class_with_fake_sem():
    class FakeSem:
        def __init__(self, sim, margin):
            self._sim, self._margin = sim, margin

        def classify(self, name, text):
            return {"category": "01-合同与协议", "sim": self._sim,
                    "margin": self._margin, "ok": True}

    ps = Prescreener(sem=FakeSem(0.80, 0.20))
    assert ps.decide("供货合同.txt", "合同条款") == ("01-合同与协议", 0.80, 0.20)
    ps2 = Prescreener(sem=FakeSem(0.50, 0.20))
    assert ps2.decide("供货合同.txt", "合同条款") == (None, 0.50, 0.20)
    # 构造本身不要求服务在线
    Prescreener()
    print("test_prescreener_class_with_fake_sem: PASS")


def test_sem_fake():
    class FakeSem:
        cents = {"01-合同与协议": [1.0]}

        def alive(self):
            return True

        def fit(self, rules):
            return 1

        def classify_batch(self, items):
            out = []
            for name, text in items:
                if not text:
                    out.append({"category": "01-合同与协议", "sim": 0.0,
                                "margin": 0.0, "ok": False})
                else:
                    out.append({"category": "01-合同与协议", "sim": 0.5,
                                "margin": 0.1, "ok": True})
            return out

    with tempfile.TemporaryDirectory() as root:
        w(os.path.join(root, "供货合同-强生.txt"), CONTRACT)
        w(os.path.join(root, "陈松沪简历.txt"), RESUME)
        items, stats = plan_files(root, load_rules(), no_ocr=True, sem=FakeSem())
        assert stats["sem_hits"] == 2, stats
        reasons = {os.path.basename(it["src"]): it["reason"] for it in items}
        assert any(r.startswith(("sem:", "rule+sem:")) for r in reasons.values()), reasons
        print("test_sem_fake: PASS")


def test_inventory_explain_learn():
    with tempfile.TemporaryDirectory() as root:
        paths, _ = build_fixture(root)
        inv = inventory(root)
        assert inv["file_count"] >= 19, inv
        assert inv["dup_group_count"] == 2, inv  # 两对同字节文件
        assert inv["reclaimable_bytes"] > 0
        assert inv["shell_count"] >= 2, inv
        ex = explain(paths["供货合同-强生.txt"])
        assert ex["title"] and ex["text_len"] > 100, ex
        assert "合同" in "、".join(ex["keywords"]) or ex["keywords"], ex
        assert ex["meaningless"] is False
        ex_dir = explain(root)
        assert ex_dir["is_dir"] is True and ex_dir["digest"]["files"] >= 19
        # learn：已整理好的两类目录
        org = os.path.join(root, "organized")
        w(os.path.join(org, "01-合同", "供货合同-甲.txt"), CONTRACT)
        w(os.path.join(org, "01-合同", "租赁协议-乙.txt"), CONTRACT)
        w(os.path.join(org, "02-周报", "第一周周报.txt"), WEEKLY)
        w(os.path.join(org, "02-周报", "第二周周报.txt"), WEEKLY)
        rules = learn_rules(org)
        assert len(rules["categories"]) == 2, rules
        assert rules["default"] in ("01-合同", "02-周报")
        assert all(c["keywords"] for c in rules["categories"]), rules
        print("test_inventory_explain_learn: PASS")


def test_llmkit_key_sources():
    from curator_core import llmkit
    assert llmkit.CONFIG_PATH == "" or os.environ.get("CURATOR_LLM_CONFIG"), \
        "未指定 CURATOR_LLM_CONFIG 时不得默认读任何密钥文件"
    assert not os.path.exists(os.path.join(PROJECT_ROOT, "curator_core", "llm.json"))
    old_plan = os.environ.pop("ARK_PLAN_KEY", None)
    old_payg = os.environ.pop("ARK_PAYG_KEY", None)
    old_cfg = os.environ.pop("CURATOR_LLM_CONFIG", None)
    try:
        os.environ["ARK_PLAN_KEY"] = "dummy-plan-key"
        from curator_core.llmkit import LLM
        llm = LLM()
        names = [p["name"] for p in llm.providers]
        assert "ark-plan" in names, names
        assert "ark-payg" not in names, names  # 未设 PAYG key 就不出现
        assert all(p["name"] != "local" or True for p in llm.providers)
        # CURATOR_LLM_CONFIG 指定的配置文件可提供 key
        cfg_path = os.path.join(tempfile.gettempdir(), "curator-test-llm-config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"order": ["ark-payg", "local"],
                       "providers": {"ark-payg": {"api_key": "dummy-payg-key"}}}, f)
        os.environ["CURATOR_LLM_CONFIG"] = cfg_path
        llmkit.CONFIG_PATH = cfg_path
        llm2 = LLM()
        assert "ark-payg" in [p["name"] for p in llm2.providers]
        os.unlink(cfg_path)
    finally:
        if old_plan is not None:
            os.environ["ARK_PLAN_KEY"] = old_plan
        else:
            os.environ.pop("ARK_PLAN_KEY", None)
        if old_payg is not None:
            os.environ["ARK_PAYG_KEY"] = old_payg
        if old_cfg is not None:
            os.environ["CURATOR_LLM_CONFIG"] = old_cfg
        else:
            os.environ.pop("CURATOR_LLM_CONFIG", None)
        llmkit.CONFIG_PATH = os.environ.get("CURATOR_LLM_CONFIG") or ""
    print("test_llmkit_key_sources: PASS")


TESTS = [
    test_plan_rules_only,
    test_sensitive_isolation,
    test_sha256_dup_within_run,
    test_sha256_dup_across_runs_via_db,
    test_apply_dry_run_and_real_then_rollback,
    test_prescreen_fake,
    test_prescreener_class_with_fake_sem,
    test_sem_fake,
    test_inventory_explain_learn,
    test_llmkit_key_sources,
]

if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"{t.__name__}: FAIL: {e!r}")
            import traceback
            traceback.print_exc()
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} 通过")
    sys.exit(1 if failed else 0)
