#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""common.learn —— NAS Curator 自学习反馈回路（v0.2.2，见 docs/SELF-LEARNING-DESIGN.md）。

闭环：decision_log 留痕（预测 vs 实际）→ learn_from_decisions 从纠正里提炼规则
→ learned_rules 参与下一次分类（提分）→ 纠正率上升的规则自动降级。

原则：
  - 只加不改：现有分类逻辑（best_category / decide / 阈值 / 路径先验）一律不动；
    本模块只提供“查规则、提分、记决策、学规则”四个纯函数入口，由调用方在集成点调用。
  - 硬规则永远优先：学到的规则目标必须在已知类目集合内，否则丢弃；提分有上限。
  - 本模块只依赖标准库 + sqlite3（不 import curator_core / media_core，避免循环引用）；
    LLM 提议走 curator_core.llmkit（懒加载、可注入 fake，便于离线测试）。
  - 绝不读凭据文件内容：学习只用文件名、类目、正文关键词统计，不读、不记任何密钥值。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3

# ---------------------------------------------------------------- 常量

# 提分门槛：规则要起作用，须同时满足 confidence >= 0.7 且 confirmations >= 2。
# （policy「文件名不可信」的落实：单次纠正学到的规则不能立刻左右分类。）
RULE_FIRE_MIN_CONF = 0.7
RULE_FIRE_MIN_CONFIRM = 2
# 文档提分：+confidence*10，上限 +10；每文件最多 1 条规则起作用。
DOC_BOOST_CAP = 10.0
# 媒体提分：给匹配类目的 SigLIP/ChineseCLIP 分数加固定值。
MEDIA_BOOST = 0.15
MEDIA_FIRE_MIN_CONF = 0.8
# 学习步长：确认 +0.05（上限 0.95）；反纠正 -0.15（下限 0.10）；低于 0.30 自动禁用。
CONFIRM_STEP = 0.05
CORRECT_STEP = 0.15
CONF_MAX = 0.95
CONF_MIN = 0.10
DEGRADE_BELOW = 0.30
NEW_RULE_CONF = 0.55

# 分词/关键词过滤：单字、纯数字/日期、通用词不具备区分度（与 learn_rules 的 noisy() 同口径）。
JUNK_TOKENS = {
    "公司", "产品", "文件", "资料", "证明", "材料", "相关", "其他", "附件",
    "新建", "副本", "复制", "扫描", "图片", "照片", "视频", "文档", "下载",
    "最终", "修改", "更新", "备份", "整理", "归档", "待确认", "未命名",
}
_TOKEN_RE = re.compile(r"[0-9a-zA-Z\u4e00-\u9fff]+")
_DATE_NUM_RE = re.compile(r"^[\d\s.\-_/年月日]+$")

# ---------------------------------------------------------------- 建表

SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_path TEXT NOT NULL, ext TEXT, kind TEXT,
    predicted_category TEXT, predicted_confidence REAL, predicted_reason TEXT,
    final_category TEXT, final_dst TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('auto_applied','review_approved','review_rejected','user_corrected','manual_move')),
    corrected INTEGER NOT NULL DEFAULT 0,
    rule_ids_applied TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    processed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_decision_processed ON decision_log (processed);
CREATE TABLE IF NOT EXISTS learned_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('filename_pattern','content_keyword','source_path','format_heuristic','policy','media_filename')),
    pattern TEXT NOT NULL, target TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.55,
    confirmations INTEGER NOT NULL DEFAULT 0,
    corrections INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('active','disabled')) DEFAULT 'active',
    source TEXT, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE (kind, pattern, target)
);
CREATE INDEX IF NOT EXISTS idx_learned_status ON learned_rules (status);
CREATE TABLE IF NOT EXISTS learn_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER, event TEXT NOT NULL,
    detail TEXT, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);
"""


def ensure_tables(conn) -> None:
    """建三张自学习表（IF NOT EXISTS，可反复执行）。"""
    conn.executescript(SCHEMA)
    conn.commit()


def _d(row):
    return dict(row) if row is not None else None


# ---------------------------------------------------------------- 决策留痕

def record_decision(conn, *, src_path, ext="", kind="",
                    predicted_category="", predicted_confidence=None,
                    predicted_reason="", final_category="", final_dst="",
                    outcome="auto_applied", corrected=False,
                    rule_ids_applied=()):
    """记一条分类决策。返回 decision_log id。调用方包 try/except，学习失败不影响主流程。"""
    ensure_tables(conn)
    cur = conn.execute(
        "INSERT INTO decision_log (src_path, ext, kind, predicted_category,"
        " predicted_confidence, predicted_reason, final_category, final_dst,"
        " outcome, corrected, rule_ids_applied)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (src_path, ext, kind, predicted_category, predicted_confidence,
         predicted_reason, final_category, final_dst, outcome,
         1 if corrected else 0,
         json.dumps(list(rule_ids_applied or []), ensure_ascii=False)))
    conn.commit()
    return cur.lastrowid


def record_review_decision(conn, review_row, payload, decision,
                           final_category="", final_dst=""):
    """复核裁定的决策留痕（供 api/review.py::decide_one 与 cmd_review 调用）。

    review_row：review_queue 行 dict；payload：其 payload dict（预测值）；
    decision：'approved' | 'rejected'（调用方刚写入的状态）。
    final_category/final_dst：用户最终定的类目/目的地（未改即 payload 原值）。
    纠正判定：用户最终类目或目的地与预测不同 → user_corrected。
    """
    pred_cat = (payload or {}).get("category") or ""
    pred_conf = (payload or {}).get("confidence")
    try:
        pred_conf = float(pred_conf) if pred_conf not in ("", None) else None
    except (TypeError, ValueError):
        pred_conf = None
    pred_dst = (payload or {}).get("suggested_dst") or ""
    final_category = final_category or pred_cat
    final_dst = final_dst or pred_dst
    if decision == "rejected":
        outcome = "review_rejected"
        corrected = False
    elif final_category != pred_cat or (final_dst and pred_dst and final_dst != pred_dst):
        outcome = "user_corrected"
        corrected = True
    else:
        outcome = "review_approved"
        corrected = False
    return record_decision(
        conn, src_path=(payload or {}).get("src") or "",
        kind=(review_row or {}).get("kind") or "",
        predicted_category=pred_cat, predicted_confidence=pred_conf,
        predicted_reason=(payload or {}).get("reason") or "",
        final_category=final_category, final_dst=final_dst,
        outcome=outcome, corrected=corrected)


def record_applied_operations(conn, items, operations):
    """执行移动后的决策留痕（供 _apply_items / cmd_apply 调用）。

    items：plan-item dict 列表（预测值）；operations：apply_plan 返回的操作列表
    （含 src_path/dst_path）。按 src 匹配；只记成功移动的项。返回记了几条。
    """
    by_src = {}
    for it in items or []:
        s = it.get("src") or it.get("src_path") or ""
        if s:
            by_src[s] = it
    n = 0
    for op in operations or []:
        src = op.get("src_path") or ""
        dst = op.get("dst_path") or ""
        it = by_src.get(src)
        if it is None or not dst:
            continue
        try:
            record_decision(
                conn, src_path=src, ext=it.get("ext") or "",
                kind=it.get("kind") or "",
                predicted_category=it.get("category") or "",
                predicted_confidence=it.get("confidence"),
                predicted_reason=it.get("reason") or "",
                final_category=it.get("category") or "",
                final_dst=dst, outcome="auto_applied", corrected=False,
                rule_ids_applied=_parse_rule_ids(
                    (it.get("reason") or "") + " " + (it.get("evidence") or "")))
            n += 1
        except Exception:
            continue
    return n


_RULE_ID_RE = re.compile(r"learn:rule#(\d+)")


def _parse_rule_ids(evidence):
    """从 plan-item evidence 里抠出起作用的规则 id（learn:rule#N 标记）。"""
    try:
        return [int(x) for x in _RULE_ID_RE.findall(evidence or "")]
    except (TypeError, ValueError):
        return []


# ---------------------------------------------------------------- 规则存取

def list_rules(conn, status="active"):
    """列出学到的规则（默认只列 active）。返回 dict 列表。"""
    ensure_tables(conn)
    sql = "SELECT * FROM learned_rules"
    args = []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY confidence DESC, confirmations DESC, id"
    return [_d(r) for r in conn.execute(sql, args).fetchall()]


def get_rule(conn, rule_id):
    ensure_tables(conn)
    return _d(conn.execute(
        "SELECT * FROM learned_rules WHERE id=?", (rule_id,)).fetchone())


def _log_event(conn, rule_id, event, detail=""):
    conn.execute("INSERT INTO learn_events (rule_id, event, detail) VALUES (?,?,?)",
                 (rule_id, event, detail or ""))


def upsert_rule(conn, *, kind, pattern, target, confidence=NEW_RULE_CONF,
                source="", confirmations=1):
    """新建或强化一条规则（同 kind/pattern/target 去重）。

    已存在：confirmations += 传入值、confidence += 0.05*传入值（上限 0.95）。
    不存在：新建，confidence 为传入值。返回 (rule_id, is_new)。
    """
    ensure_tables(conn)
    pattern = (pattern or "").strip()
    target = (target or "").strip()
    if not pattern or not target:
        return None, False
    row = conn.execute(
        "SELECT * FROM learned_rules WHERE kind=? AND pattern=? AND target=?",
        (kind, pattern, target)).fetchone()
    if row is not None:
        d = _d(row)
        new_conf = min(CONF_MAX, float(d["confidence"]) + CONFIRM_STEP * confirmations)
        conn.execute(
            "UPDATE learned_rules SET confirmations=confirmations+?,"
            " confidence=?, updated_at=strftime('%Y-%m-%dT%H:%M:%S','now')"
            " WHERE id=?", (confirmations, new_conf, d["id"]))
        _log_event(conn, d["id"], "confirmed",
                   f"confirmations+{confirmations} conf->{new_conf:.2f} src={source}")
        conn.commit()
        return d["id"], False
    cur = conn.execute(
        "INSERT INTO learned_rules (kind, pattern, target, confidence,"
        " confirmations, source) VALUES (?,?,?,?,?,?)",
        (kind, pattern, target, confidence, confirmations, source))
    _log_event(conn, cur.lastrowid, "created",
               f"conf={confidence:.2f} src={source}")
    conn.commit()
    return cur.lastrowid, True


def disable_rule(conn, rule_id, reason="manual"):
    ensure_tables(conn)
    conn.execute("UPDATE learned_rules SET status='disabled',"
                 " updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?",
                 (rule_id,))
    _log_event(conn, rule_id, "disabled", reason)
    conn.commit()
    return True


def enable_rule(conn, rule_id, reason="manual"):
    ensure_tables(conn)
    conn.execute("UPDATE learned_rules SET status='active',"
                 " updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?",
                 (rule_id,))
    _log_event(conn, rule_id, "enabled", reason)
    conn.commit()
    return True


# ---------------------------------------------------------------- 分词 / 关键词

def _tokens(text):
    """分词：保留 2 字以上、非纯数字/日期、非通用词的 token。"""
    out = []
    for tok in _TOKEN_RE.findall(text or ""):
        if len(tok) < 2:
            continue
        if _DATE_NUM_RE.match(tok):
            continue
        if tok in JUNK_TOKENS:
            continue
        if tok.isdigit():
            continue
        out.append(tok)
    return out


def _top_keywords(text, topn=5):
    """正文关键词：按词频取 topn（轻量实现，不依赖 textkit，避免循环引用）。"""
    from collections import Counter
    c = Counter(_tokens(text))
    return [t for t, _ in c.most_common(topn)]


# ---------------------------------------------------------------- 学习

def _confirm_rule(conn, rule_id):
    row = conn.execute("SELECT * FROM learned_rules WHERE id=?", (rule_id,)).fetchone()
    if row is None:
        return
    d = _d(row)
    new_conf = min(CONF_MAX, float(d["confidence"]) + CONFIRM_STEP)
    conn.execute("UPDATE learned_rules SET confirmations=confirmations+1,"
                 " confidence=?, updated_at=strftime('%Y-%m-%dT%H:%M:%S','now')"
                 " WHERE id=?", (new_conf, rule_id))
    _log_event(conn, rule_id, "confirmed", f"conf->{new_conf:.2f}")


def _correct_rule(conn, rule_id):
    """规则起了作用但最终仍被改：降 confidence；低于阈值自动禁用（种子规则除外）。"""
    row = conn.execute("SELECT * FROM learned_rules WHERE id=?", (rule_id,)).fetchone()
    if row is None:
        return
    d = _d(row)
    new_conf = max(CONF_MIN, float(d["confidence"]) - CORRECT_STEP)
    conn.execute("UPDATE learned_rules SET corrections=corrections+1,"
                 " confidence=?, updated_at=strftime('%Y-%m-%dT%H:%M:%S','now')"
                 " WHERE id=?", (new_conf, rule_id))
    _log_event(conn, rule_id, "corrected", f"conf->{new_conf:.2f}")
    src = (d.get("source") or "")
    if new_conf < DEGRADE_BELOW and not src.startswith("seed:"):
        conn.execute("UPDATE learned_rules SET status='disabled' WHERE id=?", (rule_id,))
        _log_event(conn, rule_id, "degraded",
                   f"conf {new_conf:.2f} < {DEGRADE_BELOW}，自动禁用")


def _file_text_by_path(conn, src_path):
    """按路径取 file_text（有则返回正文，否则 ''）。"""
    try:
        row = conn.execute(
            "SELECT ft.text FROM files f JOIN file_text ft ON ft.file_id=f.id"
            " WHERE f.path=?", (src_path,)).fetchone()
        return (row[0] or "") if row else ""
    except Exception:
        return ""


def learn_from_decisions(conn, known_categories=None, use_llm=False, llm=None):
    """消费未处理的 decision_log，提炼/强化/降级规则。返回统计 dict。

    known_categories：合法目标类目集合（文档 rules JSON 的 categories 路径；
    媒体为 config.json 的类目键）。传入时目标不在集合内的候选直接丢弃；
    不传时不校验目标（只建议测试时不传，生产务必传）。
    use_llm：为真时对“确定性提炼无强信号”的纠正调用 LLM 提议（见 propose_rule_llm）。
    """
    ensure_tables(conn)
    rows = [_d(r) for r in conn.execute(
        "SELECT * FROM decision_log WHERE processed=0 ORDER BY id").fetchall()]
    stats = {"decisions": len(rows), "corrections": 0, "confirmations": 0,
             "rules_created": 0, "rules_strengthened": 0, "rules_degraded": 0,
             "llm_proposed": 0, "skipped": 0}
    if not rows:
        return stats
    known = set(known_categories or []) if known_categories is not None else None

    def _target_ok(t):
        return True if known is None else (t in known)

    # 来源路径聚合：同一父目录多次纠正到同一目标才成规则（≥2 次）。
    src_groups = {}
    for r in rows:
        if r.get("corrected") and r.get("final_category"):
            parent = os.path.basename(os.path.dirname(r.get("src_path") or ""))
            if parent:
                src_groups.setdefault((parent, r["final_category"]), []).append(r["id"])
    for (parent, target), ids in src_groups.items():
        if len(ids) >= 2 and _target_ok(target):
            rid, is_new = upsert_rule(conn, kind="source_path", pattern=parent,
                                      target=target, source=f"decisions:{','.join(map(str, ids))}",
                                      confirmations=len(ids))
            stats["rules_created" if is_new else "rules_strengthened"] += 1

    for r in rows:
        rid = r["id"]
        applied = []
        try:
            applied = json.loads(r.get("rule_ids_applied") or "[]")
        except ValueError:
            applied = []
        if r.get("corrected"):
            stats["corrections"] += 1
            # 反纠正：起作用的规则降级。
            for rule_id in applied:
                before = get_rule(conn, rule_id)
                _correct_rule(conn, rule_id)
                after = get_rule(conn, rule_id)
                if before and after and before["status"] == "active" \
                        and after["status"] == "disabled":
                    stats["rules_degraded"] += 1
            # 提炼新规则：文件名 token + 内容关键词。
            final_cat = (r.get("final_category") or "").strip()
            name = os.path.basename(r.get("src_path") or "")
            learned_any = False
            if final_cat and _target_ok(final_cat):
                for tok in _tokens(name):
                    rid2, is_new = upsert_rule(
                        conn, kind="filename_pattern", pattern=tok, target=final_cat,
                        source=f"decision#{rid}")
                    stats["rules_created" if is_new else "rules_strengthened"] += 1
                    learned_any = True
                body = _file_text_by_path(conn, r.get("src_path") or "")
                for kw in _top_keywords(body):
                    rid2, is_new = upsert_rule(
                        conn, kind="content_keyword", pattern=kw, target=final_cat,
                        source=f"decision#{rid}")
                    stats["rules_created" if is_new else "rules_strengthened"] += 1
                    learned_any = True
            # 确定性提炼无强信号时，可选 LLM 提议。
            if use_llm and not learned_any and final_cat:
                proposed = propose_rule_llm(
                    {"src": r.get("src_path") or "", "name": name,
                     "predicted": r.get("predicted_category") or "",
                     "final": final_cat,
                     "reason": r.get("predicted_reason") or ""},
                    llm=llm, known_categories=known)
                if proposed:
                    rid2, is_new = upsert_rule(
                        conn, kind=proposed.get("kind") or "filename_pattern",
                        pattern=proposed["pattern"], target=proposed["target"],
                        confidence=NEW_RULE_CONF,
                        source=f"llm:decision#{rid}")
                    stats["llm_proposed"] += 1
                    stats["rules_created" if is_new else "rules_strengthened"] += 1
        else:
            # 确认：起作用的规则 +1。
            for rule_id in applied:
                _confirm_rule(conn, rule_id)
                stats["confirmations"] += 1
        conn.execute("UPDATE decision_log SET processed=1 WHERE id=?", (rid,))
    conn.commit()
    return stats


# ---------------------------------------------------------------- 分类时查规则

class LearnContext:
    """一次 plan 运行内复用的规则快照（建一次，多文件共用）。"""

    def __init__(self, conn):
        ensure_tables(conn)
        self.rules = list_rules(conn, status="active")

    def _fireable(self, rule):
        """规则起作用的条件：confidence ≥0.7 且 confirmations ≥2。"""
        try:
            return (float(rule.get("confidence") or 0) >= RULE_FIRE_MIN_CONF
                    and int(rule.get("confirmations") or 0) >= RULE_FIRE_MIN_CONFIRM)
        except (TypeError, ValueError):
            return False

    def boost_doc(self, src, name, body, ext, category, score, reason):
        """文档提分：命中规则给对应类目加分。

        返回 (category, score, reason, rule_ids)。最多 1 条规则起作用（取
        confidence 最高者）；目标与当前类目相同时只记确认、不改分。
        """
        name_l = (name or "").lower()
        body_l = (body or "").lower()
        parent = os.path.basename(os.path.dirname(src or ""))
        best = None
        for r in self.rules:
            kind = r.get("kind")
            pat = (r.get("pattern") or "")
            if not pat or not self._fireable(r):
                continue
            hit = False
            if kind == "filename_pattern" and pat.lower() in name_l:
                hit = True
            elif kind == "content_keyword" and pat.lower() in body_l:
                hit = True
            elif kind == "source_path" and pat == parent:
                hit = True
            if not hit:
                continue
            if best is None or float(r["confidence"]) > float(best["confidence"]):
                best = r
        if best is None:
            return category, score, reason, []
        target = best["target"]
        rid = best["id"]
        if target == category:
            return category, score, reason, [rid]
        try:
            boost = min(DOC_BOOST_CAP, float(best["confidence"]) * 10.0)
        except (TypeError, ValueError):
            boost = 0.0
        new_score = (score or 0) + boost
        new_reason = (reason or "") + f";learn:rule#{rid}({best['pattern']})+{boost:.0f}"
        return target, new_score, new_reason, [rid]

    def media_boosts(self, filename):
        """媒体提分：返回 [(rule_id, target_category, boost)]。

        只认 kind=media_filename、confidence ≥0.8、confirmations ≥2 的规则；
        pattern 在文件名中出现即命中。
        """
        name_l = (filename or "").lower()
        out = []
        for r in self.rules:
            if r.get("kind") != "media_filename":
                continue
            try:
                if float(r.get("confidence") or 0) < MEDIA_FIRE_MIN_CONF:
                    continue
                if int(r.get("confirmations") or 0) < RULE_FIRE_MIN_CONFIRM:
                    continue
            except (TypeError, ValueError):
                continue
            pat = (r.get("pattern") or "")
            if pat and pat.lower() in name_l:
                out.append((r["id"], r["target"], MEDIA_BOOST))
        return out


# ---------------------------------------------------------------- 格式启发式（种子规则的执行体）

def sniff_xls(path):
    """嗅探 .xls 文件头：返回 'ole' | 'spreadsheetml' | 'unknown'。

    OLE：D0 CF 11 E0；SpreadsheetML：XML（<?xml 或 < 开头）。
    2026-11-25 批教训：500MB/335MB 的销售订单大表是 SpreadsheetML，
    按 OLE 调 xls2csv 会失败、被误判损坏。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return "unknown"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "ole"
    s = head[:200].lstrip()
    if s.startswith(b"<?xml") or s.startswith(b"<"):
        return "spreadsheetml"
    return "unknown"


def is_appledouble(name):
    """macOS AppleDouble 元数据文件（._*）：本身有效，不是损坏。"""
    base = os.path.basename(name or "")
    return base.startswith("._") and len(base) > 2


def verify_video_cv2(path, max_frames=6):
    """容器内验视频：只用 cv2（容器内无 ffprobe；cv2 内部即 ffmpeg 库）。

    返回 (ok, n_frames, mean_brightness)：ok 为真表示能解出帧；
    亮度接近 0 且多帧全黑时调用方按黑视频处理。
    """
    try:
        import cv2
    except Exception:
        return False, 0, 0.0
    cap = cv2.VideoCapture(path)
    n, bright = 0, 0.0
    try:
        import numpy as np
        while n < max_frames:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            try:
                bright += float(np.asarray(frame).mean())
            except Exception:
                pass
            n += 1
    finally:
        cap.release()
    if n == 0:
        return False, 0, 0.0
    return True, n, bright / n


# ---------------------------------------------------------------- LLM 提议（可配置入口）

DEFAULT_SELFLEARN_MODEL = "qwen3:4b"


def get_selflearn_llm():
    """自学习 LLM 入口：走 curator_core.llmkit 的 provider 链（本地优先→云端 fallback）。

    模型名不写死：环境变量 CURATOR_SELFLEARN_MODEL 覆盖（缺省 qwen3:4b，
    与 llmkit 默认一致）。host 沿用 llmkit 默认（NAS 11434）。
    返回 LLM 实例；llmkit 不可用时返回 None（调用方走确定性提炼）。
    """
    try:
        from curator_core.llmkit import LLM
    except Exception:
        return None
    want = (os.environ.get("CURATOR_SELFLEARN_MODEL") or "").strip()
    llm = LLM(model=want or DEFAULT_SELFLEARN_MODEL)
    if want:
        for p in (llm.providers or []):
            if p.get("kind") == "ollama":
                p["model"] = want
    return llm


def propose_rule_llm(correction, llm=None, known_categories=None):
    """LLM 从一条纠正里提议规则。返回 dict 或 None。

    correction：{src, name, predicted, final, reason}。
    llm 为 None 时用 get_selflearn_llm()；仍拿不到时返回 None。
    输出必校验：pattern/target/reason 非空；target 在 known_categories 内（传入时）。
    LLM 只是提议者：返回的规则仍要过确认门槛才起作用（见 §3.2）。
    """
    if llm is None:
        llm = get_selflearn_llm()
    if llm is None:
        return None
    name = correction.get("name") or os.path.basename(correction.get("src") or "")
    prompt = (
        "你是文件整理系统的规则分析员。下面是一条纠正记录："
        f"文件“{name}”，系统预测归类为“{correction.get('predicted') or ''}”，"
        f"用户实际把它归到“{correction.get('final') or ''}”。"
        "请提炼一条可复用的分类规则，只输出 JSON，不要解释，格式："
        "{\"pattern\": \"触发关键词\", \"target\": \"目标类目\", \"reason\": \"一句话理由\"}"
    )
    try:
        ask = getattr(llm, "ask_json", None)
        if callable(ask):
            out = ask(prompt, system="只输出 JSON，不要解释。", timeout=120)
        else:
            text = llm.chat(prompt, system="只输出 JSON，不要解释。",
                            fmt="json", timeout=120)
            out = None
            if text:
                try:
                    out = json.loads(text)
                except ValueError:
                    out = None
    except Exception:
        return None
    if not isinstance(out, dict):
        return None
    pattern = (out.get("pattern") or "").strip()
    target = (out.get("target") or "").strip()
    reason = (out.get("reason") or "").strip()
    if not pattern or not target or not reason:
        return None
    if known_categories is not None and target not in set(known_categories):
        return None
    kind = "filename_pattern"
    if "|" in pattern:
        pattern = pattern.split("|")[0].strip()
    return {"kind": kind, "pattern": pattern, "target": target, "reason": reason}


# ---------------------------------------------------------------- 种子规则

SEED_RULES = [
    # (kind, pattern, target, confidence, confirmations, source)
    ("filename_pattern", "纳威", "08-运营与项目", 0.80, 3,
     "seed:2026-11-25-batch(54 files)"),
    ("content_keyword", "税务申报", "08-运营与项目", 0.75, 2,
     "seed:2026-11-25-batch"),
    ("media_filename", "截图", "屏幕截图", 0.70, 2,
     "seed:heuristic(needs-live-confirm)"),
    ("format_heuristic", "xls-header", "sniff-first:OLE-vs-SpreadsheetML", 0.90, 1,
     "seed:2026-11-25-batch"),
    ("format_heuristic", "appledouble-valid", "._* is valid metadata, not damaged", 0.90, 1,
     "seed:2026-11-25-batch"),
    ("format_heuristic", "cv2-only-video", "verify video with cv2 only (no ffprobe in container)", 0.90, 1,
     "seed:2026-11-25-batch"),
    ("policy", "filename-untrusted", "classify by content, not filename alone", 0.85, 1,
     "seed:2026-11-25-batch"),
    ("policy", "photo-by-content", "photos/videos to media library by content, not owner", 0.90, 1,
     "seed:2026-11-25-batch"),
    ("policy", "doc-by-content", "documents: read content to judge ownership", 0.90, 1,
     "seed:2026-11-25-batch"),
]


def seed_batch_lessons(conn):
    """录入首批种子规则（幂等：同 kind/pattern/target 已存在则跳过）。返回 (新增, 已有)。"""
    ensure_tables(conn)
    added, existed = 0, 0
    for kind, pattern, target, conf, confs, src in SEED_RULES:
        row = conn.execute(
            "SELECT id FROM learned_rules WHERE kind=? AND pattern=? AND target=?",
            (kind, pattern, target)).fetchone()
        if row is not None:
            existed += 1
            continue
        cur = conn.execute(
            "INSERT INTO learned_rules (kind, pattern, target, confidence,"
            " confirmations, source) VALUES (?,?,?,?,?,?)",
            (kind, pattern, target, conf, confs, src))
        _log_event(conn, cur.lastrowid, "seeded", f"conf={conf:.2f} src={src}")
        added += 1
    conn.commit()
    return added, existed
