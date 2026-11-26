#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自学习反馈回路（v0.2.2）单元测试。

全离线：临时 DB + 临时文件，不碰项目 data/nas-curator.db、不碰 NAS、
不调模型（LLM 用 fake 对象注入）。覆盖任务要求的三条主线：
一条纠正能学会一条规则、该规则下次分类时生效、禁用后失效；
外加格式嗅探、自动降级、媒体提分、决策留痕、SpreadsheetML 抽取。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from common import db as DB
from common import learn as LEARN


@pytest.fixture
def conn(tmp_path):
    db = str(tmp_path / "learn.db")
    DB.init_db(db)
    c = DB.connect(db)
    LEARN.ensure_tables(c)
    yield c
    c.close()


# ---------------------------------------------------------------- 1. 纠正 → 学会规则

def test_correction_learns_rule(conn):
    """一条纠正（预测 06-其他 → 实际 08-运营与项目）能学会文件名规则。"""
    LEARN.record_decision(
        conn, src_path="/tmp/x/陕西纳威税务申报.zip", ext=".zip", kind="document",
        predicted_category="14-工具与其他", predicted_confidence=0.3,
        predicted_reason="default", final_category="08-运营与项目",
        final_dst="/tmp/lib/08-运营与项目/陕西纳威税务申报.zip",
        outcome="user_corrected", corrected=True)
    stats = LEARN.learn_from_decisions(conn)
    assert stats["corrections"] == 1
    assert stats["rules_created"] >= 1
    rules = LEARN.list_rules(conn, status="")
    fn_rules = [r for r in rules
                if r["kind"] == "filename_pattern"
                and r["target"] == "08-运营与项目"]
    assert fn_rules, "应从文件名学会一条指向 08-运营与项目的规则"
    assert any("纳威" in r["pattern"] for r in fn_rules)
    # 决策已标记为已处理，再跑一次不再重复学习。
    stats2 = LEARN.learn_from_decisions(conn)
    assert stats2["decisions"] == 0


def test_rule_boosts_next_classification(conn):
    """学到的规则下次分类时生效：提分改类目，reason 留痕。"""
    rid, is_new = LEARN.upsert_rule(
        conn, kind="filename_pattern", pattern="陕西纳威",
        target="08-运营与项目", confidence=0.8, confirmations=2)
    assert is_new
    ctx = LEARN.LearnContext(conn)
    cat, sc, reason, fired = ctx.boost_doc(
        "/tmp/x/陕西纳威税务申报.zip", "陕西纳威税务申报.zip", "",
        ".zip", "14-工具与其他", 3, "default")
    assert cat == "08-运营与项目"
    assert sc > 3  # 0.8*10=8 分提分
    assert f"learn:rule#{rid}" in reason
    assert fired == [rid]
    # 目标与当前类目相同时只记确认、不改分。
    cat2, sc2, _r2, fired2 = ctx.boost_doc(
        "/tmp/x/陕西纳威税务申报.zip", "陕西纳威税务申报.zip", "",
        ".zip", "08-运营与项目", 11, "name:11/陕西纳威")
    assert cat2 == "08-运营与项目" and sc2 == 11 and fired2 == [rid]


def test_disable_rule_stops_boost(conn):
    """禁用后失效：新 LearnContext 不再给该规则提分。"""
    rid, _ = LEARN.upsert_rule(
        conn, kind="filename_pattern", pattern="陕西纳威",
        target="08-运营与项目", confidence=0.8, confirmations=2)
    assert LEARN.disable_rule(conn, rid, reason="test")
    assert LEARN.get_rule(conn, rid)["status"] == "disabled"
    ctx = LEARN.LearnContext(conn)
    cat, sc, _reason, fired = ctx.boost_doc(
        "/tmp/x/陕西纳威税务申报.zip", "陕西纳威税务申报.zip", "",
        ".zip", "14-工具与其他", 3, "default")
    assert cat == "14-工具与其他" and sc == 3 and fired == []
    # 再启用，恢复生效。
    assert LEARN.enable_rule(conn, rid, reason="test")
    ctx2 = LEARN.LearnContext(conn)
    cat3, _sc3, _r3, fired3 = ctx2.boost_doc(
        "/tmp/x/陕西纳威税务申报.zip", "陕西纳威税务申报.zip", "",
        ".zip", "14-工具与其他", 3, "default")
    assert cat3 == "08-运营与项目" and fired3 == [rid]


def test_unconfirmed_rule_does_not_fire(conn):
    """只被纠正过 1 次的新规则（conf 0.55/确认 1）未过门槛，不提分。"""
    LEARN.record_decision(
        conn, src_path="/tmp/x/陕西纳威税务申报.zip", ext=".zip", kind="document",
        predicted_category="14-工具与其他",
        final_category="08-运营与项目", outcome="user_corrected", corrected=True)
    LEARN.learn_from_decisions(conn)
    ctx = LEARN.LearnContext(conn)
    cat, sc, _reason, fired = ctx.boost_doc(
        "/tmp/x/陕西纳威税务申报.zip", "陕西纳威税务申报.zip", "",
        ".zip", "14-工具与其他", 3, "default")
    assert cat == "14-工具与其他" and fired == []


# ---------------------------------------------------------------- 2. 格式嗅探（本批教训）

def test_sniff_xls(tmp_path):
    ole = tmp_path / "a.xls"
    ole.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)
    assert LEARN.sniff_xls(str(ole)) == "ole"
    ml = tmp_path / "b.xls"
    ml.write_bytes('<?xml version="1.0"?><Workbook><Table></Table></Workbook>'.encode())
    assert LEARN.sniff_xls(str(ml)) == "spreadsheetml"
    other = tmp_path / "c.xls"
    other.write_bytes(b"not a spreadsheet at all")
    assert LEARN.sniff_xls(str(other)) == "unknown"
    assert LEARN.sniff_xls(str(tmp_path / "不存在.xls")) == "unknown"


def test_is_appledouble():
    assert LEARN.is_appledouble("._foo.txt") is True
    assert LEARN.is_appledouble("/vol1/1000/收件箱/._照片.jpg") is True
    assert LEARN.is_appledouble("foo.txt") is False
    assert LEARN.is_appledouble("._") is False
    assert LEARN.is_appledouble("") is False


def test_textkit_spreadsheetml(tmp_path):
    """SpreadsheetML .xls 走 strip-tags 抽文本，不调 xls2csv。"""
    from curator_core.textkit import extract_text
    p = tmp_path / "销售订单.xls"
    p.write_bytes(
        '<?xml version="1.0"?><Workbook><Worksheet><Table>'
        '<Row><Cell><Data>销售订单</Data></Cell><Cell><Data>陕西纳威</Data></Cell></Row>'
        '</Table></Worksheet></Workbook>'.encode("utf-8"))
    text, source = extract_text(str(p), ".xls", allow_ocr=False)
    assert source == "spreadsheetml"
    assert "销售订单" in text and "陕西纳威" in text


# ---------------------------------------------------------------- 3. 自动降级与种子豁免

def test_auto_degrade_on_repeated_correction(conn):
    """规则起了作用但仍被改：conf 降到 0.30 以下自动禁用。"""
    rid, _ = LEARN.upsert_rule(
        conn, kind="filename_pattern", pattern="测试降级",
        target="08-运营与项目", confidence=0.35, confirmations=2)
    LEARN.record_decision(
        conn, src_path="/tmp/x/测试降级文件.txt", ext=".txt", kind="document",
        predicted_category="08-运营与项目", predicted_confidence=0.9,
        predicted_reason=f"name:9/测试降级;learn:rule#{rid}",
        final_category="14-工具与其他", outcome="user_corrected",
        corrected=True, rule_ids_applied=[rid])
    stats = LEARN.learn_from_decisions(conn)
    assert stats["rules_degraded"] == 1
    assert LEARN.get_rule(conn, rid)["status"] == "disabled"


def test_seed_rules_exempt_from_auto_degrade(conn):
    """种子规则即使 conf 跌破 0.30 也不自动禁用（人工定的才豁免）。"""
    added, existed = LEARN.seed_batch_lessons(conn)
    assert added == len(LEARN.SEED_RULES) and existed == 0
    row = conn.execute(
        "SELECT id FROM learned_rules WHERE kind='format_heuristic'"
        " AND pattern='xls-header'").fetchone()
    rid = row[0]
    for _ in range(5):
        LEARN._correct_rule(conn, rid)
    rule = LEARN.get_rule(conn, rid)
    assert float(rule["confidence"]) < 0.30
    assert rule["status"] == "active"


def test_seed_rules_idempotent(conn):
    added, existed = LEARN.seed_batch_lessons(conn)
    assert added == len(LEARN.SEED_RULES)
    added2, existed2 = LEARN.seed_batch_lessons(conn)
    assert added2 == 0 and existed2 == len(LEARN.SEED_RULES)
    assert len(LEARN.list_rules(conn, status="")) == len(LEARN.SEED_RULES)


# ---------------------------------------------------------------- 4. 媒体提分与决策留痕

def test_media_boost(conn):
    """media_filename 规则 conf≥0.8 且确认≥2 才提分 +0.15。"""
    rid, _ = LEARN.upsert_rule(
        conn, kind="media_filename", pattern="截图", target="屏幕截图",
        confidence=0.8, confirmations=2)
    low, _ = LEARN.upsert_rule(
        conn, kind="media_filename", pattern="照片", target="家庭合影",
        confidence=0.7, confirmations=2)
    ctx = LEARN.LearnContext(conn)
    boosts = ctx.media_boosts("2026-01-01_截图.png")
    assert boosts == [(rid, "屏幕截图", 0.15)]
    assert ctx.media_boosts("2026-01-01_照片.jpg") == []
    assert ctx.media_boosts("IMG_0001.jpg") == []


def test_record_review_decision(conn):
    """复核改判留痕：outcome=user_corrected，corrected=1。"""
    did = LEARN.record_review_decision(
        conn, {"kind": "low_conf"},
        {"src": "/tmp/x/a.txt", "category": "14-工具与其他",
         "confidence": 0.4, "reason": "low:3", "suggested_dst": ""},
        "approved", final_category="08-运营与项目")
    row = conn.execute(
        "SELECT outcome, corrected, predicted_category, final_category"
        " FROM decision_log WHERE id=?", (did,)).fetchone()
    assert tuple(row) == ("user_corrected", 1, "14-工具与其他", "08-运营与项目")
    # 不改判：review_approved。
    did2 = LEARN.record_review_decision(
        conn, {"kind": "low_conf"},
        {"src": "/tmp/x/b.txt", "category": "08-运营与项目",
         "confidence": 0.4, "reason": "low:3", "suggested_dst": ""},
        "approved")
    row2 = conn.execute("SELECT outcome, corrected FROM decision_log WHERE id=?",
                        (did2,)).fetchone()
    assert tuple(row2) == ("review_approved", 0)
    # 驳回：review_rejected。
    did3 = LEARN.record_review_decision(
        conn, {"kind": "low_conf"},
        {"src": "/tmp/x/c.txt", "category": "08-运营与项目",
         "confidence": 0.4, "reason": "low:3", "suggested_dst": ""},
        "rejected")
    row3 = conn.execute("SELECT outcome FROM decision_log WHERE id=?",
                        (did3,)).fetchone()
    assert row3[0] == "review_rejected"


def test_record_applied_operations(conn):
    """执行移动后留痕：只记成功移动的项，outcome=auto_applied。"""
    items = [{"src": "/tmp/x/b.txt", "ext": ".txt", "kind": "document",
              "category": "08-运营与项目", "confidence": 0.9,
              "reason": "rule", "evidence": ""},
             {"src": "/tmp/x/c.txt", "ext": ".txt", "kind": "document",
              "category": "08-运营与项目", "confidence": 0.9,
              "reason": "rule", "evidence": ""}]
    ops = [{"src_path": "/tmp/x/b.txt",
            "dst_path": "/tmp/lib/08-运营与项目/b.txt"}]
    assert LEARN.record_applied_operations(conn, items, ops) == 1
    rows = conn.execute(
        "SELECT outcome, final_dst FROM decision_log").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "auto_applied"
    assert rows[0][1] == "/tmp/lib/08-运营与项目/b.txt"


# ---------------------------------------------------------------- 5. LLM 提议（fake 注入，不调模型）

class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    def ask_json(self, prompt, system=None, timeout=120):
        return self.payload


def test_propose_rule_llm_fake():
    """fake LLM 返回合法 JSON → 提议规则；目标不在已知类目内 → None。"""
    fake = _FakeLLM({"pattern": "陕西纳威", "target": "08-运营与项目",
                     "reason": "纳威文件常属运营与项目"})
    out = LEARN.propose_rule_llm(
        {"src": "/tmp/x/纳威税务申报.zip", "name": "纳威税务申报.zip",
         "predicted": "14-工具与其他", "final": "08-运营与项目",
         "reason": "default"},
        llm=fake, known_categories=["08-运营与项目", "14-工具与其他"])
    assert out == {"kind": "filename_pattern", "pattern": "陕西纳威",
                   "target": "08-运营与项目", "reason": "纳威文件常属运营与项目"}
    # 目标不在已知类目内 → 拒绝。
    out2 = LEARN.propose_rule_llm(
        {"src": "/tmp/x/a.zip", "name": "a.zip",
         "predicted": "14-工具与其他", "final": "不存在的类目",
         "reason": "default"},
        llm=_FakeLLM({"pattern": "a", "target": "不存在的类目", "reason": "x"}),
        known_categories=["08-运营与项目"])
    assert out2 is None
    # LLM 返回非法内容 → None，不抛错。
    out3 = LEARN.propose_rule_llm(
        {"src": "/tmp/x/a.zip", "name": "a.zip",
         "predicted": "14-工具与其他", "final": "08-运营与项目",
         "reason": "default"},
        llm=_FakeLLM({"pattern": "", "target": "", "reason": ""}))
    assert out3 is None
    # LLM 调用抛错 → None，不抛错（不走真实 get_selflearn_llm，不联网）。
    class _BoomLLM:
        def ask_json(self, *a, **k):
            raise RuntimeError("boom")

    out4 = LEARN.propose_rule_llm(
        {"src": "/tmp/x/a.zip", "name": "a.zip",
         "predicted": "14-工具与其他", "final": "08-运营与项目",
         "reason": "default"},
        llm=_BoomLLM())
    assert out4 is None
