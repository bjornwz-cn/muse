#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""curator_core.engine —— 文档整理引擎（workfiles-curator v3.1.1 包化）。

来源：analysis/workfiles-curator/scripts/curator.py（1,378 行）的命令函数
重构成可 import 的函数。判定逻辑、阈值、内容抽取一律不动，只做：
  - 模块化：cmd_inventory/cmd_learn/cmd_plan/cmd_analyze/cmd_explain/
    cmd_apply/cmd_rollback 变成 inventory/learn_rules/plan_files/
    analyze/explain/apply_plan/rollback_map；
  - 输出统一 plan-item（见 _to_plan_item）；
  - 新增 prescreen 层（plan_files 的 prescreener 参数）；
  - 补齐两项架构要求（见 _sensitive_hit / SHA256 去重，均集中在
    plan_files 内，不动 best_category）。

判定链（与 v3.1.1 一致，prescreen/sensitive/dup 为新增前置）：
  空壳/加密/扫描件/不可读 -> 敏感隔离 -> SHA256 重复 -> 预筛
  -> 实体画像（semkit.classify 内优先） -> 语义向量（sem）
  -> 文件名业务类型优先（best_category name_first=True）
  -> LLM 兜底 -> 无正文不猜 -> 低置信。
阈值：min_score 默认 4、sem_min_sim 0.42、sem_margin 0.02、
llm_min_conf 0.55（均以传入实例/参数为准，不改默认值）。

本模块 import 时不要求任何模型服务在线：LLM/sem/prescreener
只在调用方传入实例时才用。
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import textkit as TK
from . import orgkit as _orgkit
from .llmkit import LLM
from .semkit import SemanticClassifier

# common.tags：包外兄弟层。nas-curator 在 sys.path 上时用绝对导入；
# 否则试相对导入；都没有则 tags 为空列表（引擎本身不依赖 DB）。
try:
    from common.tags import gen_document_tags as _gen_document_tags
except Exception:  # noqa: BLE001
    try:
        from ..common.tags import gen_document_tags as _gen_document_tags
    except Exception:  # noqa: BLE001
        _gen_document_tags = None

__all__ = [
    "best_category", "score_rule", "scan_units", "unit_kind", "mail_name",
    "pick_samples", "plan_files", "apply_plan", "inventory", "learn_rules",
    "explain", "analyze", "rollback_map", "unique_target", "human", "sha256",
    "TEXT_EXT", "OOXML_EXT", "OLE_EXT", "IMG_EXT", "ARCHIVE_EXT", "NEED_TEXT",
    "JUNK_NAMES", "ATOMIC_FILES", "ATOMIC_DIRS",
    "DEFAULT_FROZEN", "DEFAULT_TRANSPARENT",
]

TEXT_EXT = TK.TEXT_EXT
OOXML_EXT = TK.OOXML_EXT
OLE_EXT = TK.OLE_EXT
# 0.1.5：以下三项保留为缺省快照（兼容旧导入）；判定逻辑一律调 TK 的函数
# （TK.get_image_exts() / TK.image_exts() / TK.need_text_exts()），
# 否则设置页改了图片扩展名后，运行中进程仍用旧值。
IMG_EXT = TK.IMG_EXT
ARCHIVE_EXT = TK.ARCHIVE_EXT | {".bz2", ".xz", ".iso"}
IMAGE_EXT = TK.IMG_EXT | {".pdf"}
NEED_TEXT = TK.NEED_TEXT
JUNK_NAMES = {"__MACOSX", ".DS_Store", "Thumbs.db", "desktop.ini"}

# 程序/工程/仓库标志：命中即整目录作为一个单元，绝不拆分
ATOMIC_FILES = {"package.json", "cmakelists.txt", "dockerfile", "docker-compose.yml",
                "docker-compose.yaml", "requirements.txt", "setup.py", "pyproject.toml",
                "go.mod", "cargo.toml", "pom.xml", "build.gradle", "makefile",
                "configure", "manifest.json", "composer.json", "gemfile"}
ATOMIC_DIRS = {".git", ".svn", "node_modules", "bin", "lib", "obj", "dist", "build",
               "target", "venv", ".venv", "__pycache__", "vendor", "src", "include"}

DEFAULT_FROZEN = ("归档", "已整理", "_归档", "系统", "备份", "回收站",
                  "__归档", "archive", "Archive", ".trash", ".recycle")
DEFAULT_TRANSPARENT = ("收件箱", "待处理", "待整理", "Inbox", "inbox",
                       "Downloads", "下载", "临时", "temp", "未整理")


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024.0


def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except (OSError, PermissionError):
        return None


def walk_files(root, skip_dirs=()):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and d not in JUNK_NAMES]
        for name in filenames:
            if name in JUNK_NAMES or name.startswith("._"):
                continue
            p = os.path.join(dirpath, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            yield p, st


# ---------------------------------------------------------------- inventory

def inventory(root, max_dup_show=200):
    """只读盘点（与原 cmd_inventory 同口径，不写文件）。

    返回 dict：文件数/目录数/总体积/扩展名 Top20/SHA256 重复组/
    空壳/压缩包/扫描件数（image_or_pdf_count）。
    """
    root = os.path.abspath(root)
    files, ext_counter, ext_bytes = [], Counter(), Counter()
    total = 0
    for p, st in walk_files(root, skip_dirs=(".git", ".svn")):
        ext = os.path.splitext(p)[1].lower()
        files.append((p, st.st_size, ext, st.st_mtime))
        total += st.st_size
        ext_counter[ext or "(无扩展名)"] += 1
        ext_bytes[ext or "(无扩展名)"] += st.st_size

    by_size = defaultdict(list)
    for p, size, ext, _ in files:
        if size > 0:
            by_size[size].append(p)
    dup_groups, hashed = [], 0
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        groups = defaultdict(list)
        for p in paths:
            h = sha256(p)
            hashed += 1
            if h:
                groups[h].append(p)
        for h, plist in groups.items():
            if len(plist) > 1:
                plist_sorted = sorted(plist, key=lambda x: (len(x), x))
                dup_groups.append({"sha256": h, "size": size, "count": len(plist),
                                   "keep": plist_sorted[0], "dups": plist_sorted[1:],
                                   "wasted": size * (len(plist) - 1)})
    dup_groups.sort(key=lambda g: g["wasted"], reverse=True)

    shells = [p for p, size, ext, _ in files
              if (ext in (".eml", ".msg") and size < 2048) or size == 0]
    archives = [{"path": p, "size": size} for p, size, ext, _ in files if ext in ARCHIVE_EXT]
    images = sum(1 for _, _, ext, _ in files if ext in TK.image_exts())
    dirs = sum(len(dn) for _, dn, _ in os.walk(root))

    return {
        "root": root, "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "file_count": len(files), "dir_count": dirs,
        "total_bytes": total, "total_human": human(total),
        "ext_top": [{"ext": e, "count": c, "bytes": ext_bytes[e]}
                    for e, c in ext_counter.most_common(20)],
        "dup_group_count": len(dup_groups),
        "dup_file_count": sum(g["count"] - 1 for g in dup_groups),
        "reclaimable_bytes": sum(g["wasted"] for g in dup_groups),
        "reclaimable_human": human(sum(g["wasted"] for g in dup_groups)),
        "hashed_files": hashed, "dup_groups": dup_groups[:max_dup_show],
        "shell_count": len(shells), "shell_samples": shells[:20],
        "archive_count": len(archives), "archives": archives[:50],
        "image_or_pdf_count": images,
    }


# ---------------------------------------------------------------- 单元识别

def unit_kind(directory, depth_files=400):
    """判断一个目录是「原子单元（程序/代码/工程）」还是「项目单元」。"""
    try:
        entries = os.listdir(directory)
    except OSError:
        return None, ""
    lower = [e.lower() for e in entries]
    if any(x in lower for x in (".git", ".svn")):
        return "repo", "含版本库"
    if any(x in lower for x in ("node_modules",)):
        return "repo", "含 node_modules"
    hit = [e for e in lower if e in ATOMIC_FILES]
    if hit:
        return "code", "含工程文件:" + ",".join(hit[:3])
    try:
        subdirs = [e for e in entries if os.path.isdir(os.path.join(directory, e))]
    except OSError:
        subdirs = []
    exe = 0
    doc = 0
    n = 0
    for dp, dns, fns in os.walk(directory):
        dns[:] = [d for d in dns if d not in JUNK_NAMES]
        for fn in fns:
            n += 1
            e = os.path.splitext(fn)[1].lower()
            if e in (".exe", ".dll", ".so", ".dylib", ".msi", ".bat", ".cmd"):
                exe += 1
            elif e in (".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf"):
                doc += 1
            if n > depth_files:
                break
        if n > depth_files:
            break
    if exe >= 1 and (exe >= doc):
        return "program", f"含可执行/库 {exe} 个"
    if len(subdirs) >= 2 and doc >= 3:
        return "project", f"多子目录 {len(subdirs)} + 文档 {doc} 个"
    if n >= 15 and len(subdirs) >= 3:
        return "project", f"文件 {n} + 子目录 {len(subdirs)}"
    return None, ""


def scan_units(root, mode="auto", skip_tops=(), protect=(), frozen=(), transparent=(),
               _depth=0, max_depth=2, deep=False):
    """识别「不该拆散的单元」并收集散文件。

    返回 (units, loose, covered)。规则与 v3.1.1 原版一致：
      · 类目目录 / 冻结目录（归档、已整理、系统…）一律不动；
      · 透明目录（收件箱、待处理、下载…）下钻一层，把里面真正要整理的挑出来；
      · 其余非单元目录保持原样，避免误搬用户自己的结构。
    """
    units, loose, covered = [], [], set()

    def skipped(name):
        return (not name or name.startswith(".") or name in skip_tops
                or name in frozen or name in JUNK_NAMES)

    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return units, loose, covered

    for name in entries:
        p = os.path.join(root, name)
        if not os.path.isdir(p) or skipped(name):
            continue
        if name in protect or p in protect:
            files, b = dir_size(p)
            units.append({"path": p, "rel": name, "kind": "protected",
                          "evidence": "手动保护", "files": files, "bytes": b,
                          "files_list": []})
            covered.add(os.path.abspath(p))
            continue
        kind, ev = unit_kind(p)
        if kind and mode != "off":
            files, b = dir_size(p)
            units.append({"path": p, "rel": name, "kind": kind, "evidence": ev,
                          "files": files, "bytes": b, "files_list": None})
            covered.add(os.path.abspath(p))
            continue
        if (name in transparent or mode == "project" or deep) \
                and _depth < max_depth + (3 if deep else 0):
            u2, l2, c2 = scan_units(p, mode, skip_tops, protect, frozen,
                                    transparent, _depth + 1,
                                    max_depth + (3 if deep else 0), deep)
            units.extend(u2)
            loose.extend(l2)
            covered |= c2
            covered.add(os.path.abspath(p))

    for name in entries:
        p = os.path.join(root, name)
        if not os.path.isfile(p):
            continue
        try:
            loose.append((p, os.stat(p)))
        except OSError:
            pass
    return units, loose, covered


def dir_size(path):
    files, b = 0, 0
    for dp, dns, fns in os.walk(path):
        dns[:] = [d for d in dns if d not in JUNK_NAMES]
        for fn in fns:
            try:
                b += os.stat(os.path.join(dp, fn)).st_size
                files += 1
            except OSError:
                pass
    return files, b


# ---------------------------------------------------------------- 打分

def score_rule(name_lower, body_lower, ext, keywords, exts, w_name=3, w_body=2):
    name_hits = [k for k in keywords if k and k.lower() in name_lower]
    body_hits = [k for k in keywords if k and k.lower() in body_lower]
    s = sum(w_name * len(k) for k in name_hits) + sum(w_body * len(k) for k in body_hits)
    if exts and ext in [e.lower() for e in exts]:
        s += 1
    return s, name_hits, body_hits


def _score_name_only(name, ext, cats):
    """只按文件名打分（正文不参与），用于文件名优先判定。"""
    best, best_score, best_kw, best_ev = "", 0, [], ""
    nl = name.lower()
    for c in cats:
        kws = c.get("keywords", [])
        hits = [k for k in kws if k and k.lower() in nl]
        if not hits:
            continue
        sc = sum(3 * len(k) for k in hits)
        if c.get("exts") and ext in [e.lower() for e in c["exts"]]:
            sc += 1
        if sc > best_score:
            best, best_score, best_kw = c["path"], sc, hits
            best_ev = f"名:{','.join(hits)}"
    return best, best_score, best_kw, best_ev


def best_category(name, body, ext, cats, min_score, w_body, default_cat, low_cat,
                  name_first=False, name_first_min=8):
    """先强后弱打分（v3.1.1 原样，不动）。

    name_first=True 时：文件名命中足够强（≥name_first_min）就不再让正文改写结论。
    依据：真实库审计中「仅文件名」命中样本一致率 88%，而「文件+正文」只有 58%——
    长正文会把文件名给出的业务类型带偏（配送协议被判成库存物流、供应商合同被判成资质）。
    """
    if name_first:
        bn, sn, kn, evn = _score_name_only(name, ext, cats)
        if sn >= name_first_min and len(kn) >= 1:
            return bn, sn, f"name:{sn}/{'|'.join(kn[:2])}", evn
    best, best_score, best_kw, best_ev = default_cat, 0, [], ""
    for c in cats:
        sc, nh, bh = score_rule(name.lower(), body.lower(), ext,
                                c.get("keywords", []), c.get("exts", []),
                                w_name=3, w_body=w_body)
        if sc > best_score:
            best, best_score, best_kw = c["path"], sc, (nh + bh)
            ev = ""
            for k in bh:
                i = body.lower().find(k.lower())
                if i >= 0:
                    ev = body[max(0, i - 15):i + 25].strip()
                    break
            best_ev = (f"名:{','.join(nh)} " if nh else "") + (f"文:{ev}" if ev else "")
    if best_score == 0:
        return default_cat, 0, "default", ""
    hits = len(best_kw)
    if best_score < 8 and hits <= 1 and best_kw and max(len(k) for k in best_kw) <= 2:
        return low_cat, best_score, f"low:weak/{best_kw[0]}", best_ev
    if best_score < min_score:
        return low_cat, best_score, f"low:{best_score}/{'|'.join(best_kw[:2])}", best_ev
    return best, best_score, f"sc:{best_score}", best_ev


# ---------------------------------------------------------------- 通用小件

def load_cache(path):
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def cache_key(p, st):
    return hashlib.sha1(f"{p}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()


def mail_name(path, ext, max_chars=1500):
    """邮件按「日期_发件人_主题」命名。"""
    t, _ = TK.extract_text(path, ext, max_chars, allow_ocr=False)
    m_date = re.search(r"Date:\s*(.+?)(?:\n|$)", t)
    d_std = ""
    if m_date:
        try:
            from email.utils import parsedate_to_datetime
            d_std = parsedate_to_datetime(m_date.group(1).strip()).strftime("%Y-%m-%d")
        except Exception:
            d_std = ""
    if d_std:
        t = t.replace(m_date.group(1), d_std)
    m_from = re.search(r"From:\s*(.+?)(?:\n|$)", t)
    m_subj = re.search(r"Subject:\s*(.+?)(?:\n|$)", t)
    if not m_subj:
        return "", ""
    d = TK.find_date(m_date.group(1) if m_date else "", t)
    who = ""
    if m_from:
        raw = m_from.group(1).strip()
        mm = re.match(r'\s*"?([^"<@]{1,30})"?\s*<', raw)
        if mm:
            who = mm.group(1).strip()
        else:
            who = raw.split("@")[0].strip(' "<>')[:16]
    subj = re.sub(r"^(Re|RE|Fwd|FW|回复|转发)\s*[:：]\s*", "", m_subj.group(1)).strip()
    subj = TK.sanitize(subj, 32)
    if not subj:
        return "", ""
    date_str = d.replace("-", "") if d else ""
    parts = [x for x in (date_str, TK.sanitize(who, 12), subj) if x]
    return "_".join(parts) + ext, "mail"


def unique_target(directory, name):
    target = os.path.join(directory, name)
    if not os.path.exists(target):
        return target
    stem, ext = os.path.splitext(name)
    i = 1
    while True:
        cand = os.path.join(directory, f"{stem}__{i}{ext}")
        if not os.path.exists(cand):
            return cand
        i += 1


def _load_rules(rules):
    """rules 接受 dict 或 JSON 文件路径；返回 dict（不改动调用方传入的对象）。"""
    if rules is None:
        return {}
    if isinstance(rules, dict):
        return rules
    with open(rules, encoding="utf-8") as f:
        return json.load(f)


def write_digest(path, root, units, rows, texts):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    L = [f"# 目录画像：`{root}`", "",
         f"生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}", "",
         f"- 整目录单元：{len(units)}", f"- 计划条目：{len(rows)}", ""]
    L.append("| 单元 | 类型 | 文件数 | 判定类目 | 依据 | 主题词 |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for u in units:
        row = next((r for r in rows if r["src"] == u["path"]), None)
        kw = "、".join(TK.keywords(texts.get(u["path"], ""), 5)) or "-"
        L.append(f"| {u['rel']} | {u['kind']} | {u['files']} | "
                 f"{row['category'] if row else '-'} | {row['reason'] if row else '-'} | {kw} |")
    L.append("")
    by_dir = defaultdict(list)
    for r in rows:
        if r["ext"] != "(目录)":
            by_dir[os.path.dirname(r["src"])].append(r)
    L.append("## 散文件目录分布")
    L.append("| 目录 | 文件数 | 主要类目 | 主题词 |")
    L.append("| --- | --- | --- | --- |")
    for d, grp in sorted(by_dir.items(), key=lambda x: -len(x[1]))[:30]:
        c = Counter(g["category"] for g in grp).most_common(2)
        body = "\n".join(texts.get(g["src"], "") for g in grp[:6])
        L.append(f"| {os.path.relpath(d, root)} | {len(grp)} | "
                 f"{'、'.join(f'{k}({v})' for k, v in c)} | "
                 f"{'、'.join(TK.keywords(body, 4)) or '-'} |")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return path


# ---------------------------------------------------------------- 补齐项 1：敏感文件隔离
# 架构 §3.5 要求、v3.1.1 代码里实际没有实现，在此补齐。
# 位置：plan_files 散文件判定、best_category 之前。best_category 本身不动。

_SENSITIVE_NAME_WORDS = ("密码", "password", "账号", "登录", "身份证", "护照", "银行卡", "token")
_SENSITIVE_BODY_WORDS = ("密码", "身份证", "护照", "银行卡")
# key 只匹配独立词，避免误伤普通英文单词（如 keyboard、monkey）。
_KEY_RE = re.compile(r"(?i)(?<![a-z])key(?![a-z])")


def _sensitive_hit(filename, body):
    """返回命中的敏感词列表（按文件名命中在前、去重保序）；未命中返回 []。

    文件名命中任一词即隔离；正文只看「密码/身份证/护照/银行卡」。
    """
    hits = []
    low = (filename or "").lower()
    for w in _SENSITIVE_NAME_WORDS:
        if w in low and w not in hits:
            hits.append(w)
    if _KEY_RE.search(filename or "") and "key" not in hits:
        hits.append("key")
    for w in _SENSITIVE_BODY_WORDS:
        if w in (body or "") and w not in hits:
            hits.append(w)
    return hits


# ---------------------------------------------------------------- 补齐项 2：SHA256 精确重复进复核
# plan_files 内维护本轮登记表（sha256 -> 首次出现路径）；传入 db_conn
# （common.db 的连接）时再查 files 表 status='active' 的跨轮登记。
# 第一个文件正常走流程，后续重复文件进 review/dup。

def _db_dup_path(db_conn, sha, src):
    """在 DB 的 active 文件里找同一 sha256 的另一路径；没有返回 None。"""
    if db_conn is None or not sha:
        return None
    get_by_sha = None
    try:
        from common.db import get_file_by_sha as get_by_sha
    except Exception:  # noqa: BLE001
        try:
            from ..common.db import get_file_by_sha as get_by_sha
        except Exception:  # noqa: BLE001
            return None
    try:
        rows = get_by_sha(db_conn, sha) or []
    except Exception:  # noqa: BLE001
        return None
    src_abs = os.path.abspath(src)
    for r in rows:
        p = r.get("path") if isinstance(r, dict) else r["path"]
        if p and os.path.abspath(p) != src_abs:
            return p
    return None


# ---------------------------------------------------------------- 统一 plan-item 映射（集中处理，不散在各处）

def _decide_action(row, rename):
    """按行 (category/reason) 定 action 与 review_kind。"""
    cat = row.get("category") or ""
    reason = row.get("reason") or ""
    is_unit = row.get("ext") == "(目录)"
    if cat.startswith("99-"):
        if reason == "sensitive":
            rk = "sensitive"
        elif reason.startswith("dup:"):
            rk = "dup"
        elif reason in ("shell", "cloudsync-enc"):
            rk = "shell"
        elif reason == "scanned-no-text":
            rk = "ambiguous"
        elif reason.startswith(("unreadable:", "low:", "no-text:", "low-text:")):
            rk = "low_conf"
        else:
            rk = "low_conf"
        return "review", rk
    if reason == "default" or reason.startswith("low:"):
        return "review", "low_conf"
    if not is_unit and rename and row.get("target_name") \
            and row["target_name"] != os.path.basename(row["src"]):
        return "rename", None
    return "move", None


def _map_confidence(row, min_score, is_review):
    """按行定 confidence；review 行算出后 ≥0.6 压到 0.45。"""
    raw = row.get("confidence")
    conf = None
    if raw not in ("", None):
        try:
            conf = float(raw)
        except (TypeError, ValueError):
            conf = None
    if conf is None:
        reason = row.get("reason") or ""
        m = None
        if reason.startswith("rule+sem:"):
            m = re.search(r"(\d+\.\d+)", reason)
        elif reason.startswith(("prescreen:", "sem:", "llm:")):
            m = re.search(r"(\d+\.\d+)", reason)
        if m:
            conf = float(m.group(1))
        else:
            sc = row.get("score") or 0
            if sc >= 12:
                conf = 0.9
            elif sc >= 8:
                conf = 0.85
            elif sc >= min_score:
                conf = 0.7
            else:
                conf = 0.4
    conf = max(0.0, min(1.0, conf))
    if is_review and conf >= 0.6:
        conf = 0.45
    return conf


def _to_plan_item(row, root, min_score, rename, texts, mtimes, dst_root=None):
    """内部行 -> 统一 plan-item dict（media_core 与 CLI 都按此格式对接）。

    dst_root：目标根目录。默认 None 即 root（库内整理）；输入源（如统一收件箱、
    笔记本备份）传目标库根目录，归位 dst 落到对应库而非输入源内部。
    """
    src = row["src"]
    cat = row.get("category") or ""
    ext = row.get("ext") or ""
    is_unit = ext == "(目录)"
    action, review_kind = _decide_action(row, rename)
    is_review = action == "review"
    conf = _map_confidence(row, min_score, is_review)
    target_name = row.get("target_name") or os.path.basename(src)
    dst_base = dst_root or root
    if is_review:
        dst = ""
    elif is_unit:
        dst = os.path.join(dst_base, cat)
    else:
        dst = os.path.join(dst_base, cat, target_name)
    body = texts.get(src, "") if not is_unit else texts.get(src, "")
    tags = []
    if _gen_document_tags is not None:
        try:
            ents = _orgkit.orgs(body, 6) if body else []
            date_str = TK.find_date(body, os.path.basename(src)) or None
            tagged = _gen_document_tags(category=cat, entities=ents, date_str=date_str,
                                        name=os.path.basename(src), text=body,
                                        mtime=mtimes.get(src))
            tags = [t[0] for t in tagged]
        except Exception:  # noqa: BLE001
            tags = []
    if is_unit:
        kind = "unit"
    elif ext in (TK.get_image_exts() | TK.AV_EXT):
        kind = "media"
    else:
        kind = "document"
    return {
        "src": src, "dst": dst, "new_name": target_name, "category": cat,
        "confidence": conf, "reason": row.get("reason") or "",
        "evidence": (row.get("evidence") or "")[:80],
        "action": action, "kind": kind,
        "size": int(row.get("size") or 0), "ext": ext,
        "sha256": row.get("sha256") or "", "tags": tags,
        "review_kind": review_kind, "file_id": None,
        # P2(P1.1)：score 补进 plan-item（plan CSV 的 score 列用）；
        # auto_exempt：整目录 unit 行豁免自动执行阈值（结构性判定，不按单文件置信度拦）。
        "score": row.get("score") or 0,
        "auto_exempt": bool(is_unit),
    }


# ---------------------------------------------------------------- plan

def plan_files(root, rules, *, llm=None, sem=None, prescreener=None, rename=False,
               rename_llm=False, unit_mode="auto", protect=(), workers=8, cache=None,
               min_score=4, content_first=False, ocr_url=None,
               no_ocr=False, max_chars=2000, ocr_timeout=90, llm_min_conf=0.55,
               llm_max=2000, llm_all=False, llm_chunk=8, llm_timeout=240,
               strong=12, name_template="{date}_{title}", deep=False,
               digest=None, db_conn=None, dst_root=None):
    """生成统一 plan-item 列表。返回 (items, stats)。

    rules 接受 dict 或 JSON 文件路径；llm/sem/prescreener 接受已建好的
    实例（不传即不用，不会为此新建连接或探活模型服务）。
    cache 接受缓存目录路径（str）或已加载的 dict。
    db_conn 传入 common.db 的连接时做跨轮 SHA256 去重；不传只做本轮内去重。
    dst_root 传入目标库根目录时，move/rename 项的 dst 落到该库（输入源归位用）；
    不传则 dst 落在扫描根目录内（库内整理）。
    """
    root = os.path.abspath(root)
    rules = _load_rules(rules)
    default_cat = rules.get("default", "06-其他")
    shell_cat = rules.get("shell_category", "99-待确认/空壳")
    low_cat = rules.get("low_conf_category", "99-待确认/04-低置信")
    enc_cat = rules.get("cloudsync_category", "99-待确认/01-云同步加密")
    sensitive_cat = rules.get("sensitive_category", "99-待确认/05-敏感隔离")
    dup_cat = rules.get("dup_category", "99-待确认/06-重复待确认")
    cats = rules.get("categories", [])
    cat_tops = {c["path"].split("/")[0] for c in cats} | {
        default_cat.split("/")[0], shell_cat.split("/")[0],
        low_cat.split("/")[0], enc_cat.split("/")[0]}
    whitelist = rules.get("categories_whitelist") or []

    llm_quota = [max(0, llm_max)]
    llm_calls = [0]
    if llm is not None:
        try:
            if not llm.alive():
                llm = None
        except Exception:  # noqa: BLE001
            llm = None
    if sem is not None:
        try:
            if not sem.alive():
                sem = None
            elif not sem.cents:
                sem.fit(rules)
        except Exception:  # noqa: BLE001
            sem = None
    if prescreener is not None:
        try:
            if not prescreener.sem.cents:
                prescreener.fit(rules)
        except Exception:  # noqa: BLE001
            pass
    prescreen_calls = [0]
    prescreen_hits = [0]

    if isinstance(cache, str):
        cache_path = os.path.join(cache, "cache.json")
        cache_dict = load_cache(cache_path)
    elif isinstance(cache, dict):
        cache_path = ""
        cache_dict = cache
    else:
        cache_path = ""
        cache_dict = {}
    cache_on = bool(cache_path) or isinstance(cache, dict)

    protect_t = tuple(protect or ())
    frozen = tuple(rules.get("frozen_dirs", DEFAULT_FROZEN))
    transparent = tuple(rules.get("transparent_dirs", DEFAULT_TRANSPARENT))
    units, loose, _ = scan_units(root, unit_mode, skip_tops=cat_tops,
                                 protect=protect_t, frozen=frozen, transparent=transparent,
                                 deep=deep)

    # 1) 抽取文本（散文件 + 单元内抽样）
    items = []
    for p, st in loose:
        rel = os.path.relpath(p, root)
        if "/" in rel and rel.split("/")[0] in cat_tops:
            continue
        items.append((p, st, rel, None))
    for u in units:
        picks = []
        for dp, dns, fns in os.walk(u["path"]):
            dns[:] = [d for d in dns if d not in JUNK_NAMES]
            for fn in fns:
                ext = os.path.splitext(fn)[1].lower()
                if ext in TK.need_text_exts() - TK.get_image_exts():
                    picks.append(os.path.join(dp, fn))
                if len(picks) >= 12:
                    break
            if len(picks) >= 12:
                break
        u["files_list"] = picks

    def grab(item):
        p, st, rel, _ = item
        ext = os.path.splitext(p)[1].lower()
        key = cache_key(p, st)
        if key in cache_dict:
            c = cache_dict[key]
            return p, c.get("text", ""), c.get("source", "cached")
        t, s = TK.extract_text(p, ext, max_chars, ocr_url, ocr_timeout,
                               allow_ocr=not no_ocr)
        return p, t, s

    texts, srcs, mtimes = {}, {}, {}
    for p, st, _, _ in items:
        mtimes[p] = st.st_mtime
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for p, t, s in ex.map(grab, items):
            texts[p] = t
            srcs[p] = s

    unit_text = {}
    for u in units:
        buf = []
        for p in u["files_list"][:10]:
            if p in texts:
                buf.append(texts[p])
            else:
                t, s = TK.extract_text(p, os.path.splitext(p)[1].lower(), 800,
                                       ocr_url, ocr_timeout, allow_ocr=not no_ocr)
                buf.append(t)
        unit_text[u["path"]] = "\n".join(buf)
        mtimes[u["path"]] = None

    rows = []
    pending_llm = []
    titles = {}   # src -> 模型给出的准确标题（用于命名阶段）
    seen_sha = {}  # sha256 -> 首次出现路径（本轮登记表）

    # 2) 单元判定：整个目录一个类目（或项目名）
    prog_cat = rules.get("program_category", "11-程序与工具")
    scan_cat = rules.get("scan_category", "99-待确认/03-扫描件待OCR")
    for u in units:
        body = unit_text.get(u["path"], "")
        if u["kind"] == "project":
            proj_root = rules.get("project_root", "12-项目资料")
            rows.append({
                "src": u["path"], "size": u["bytes"], "ext": "(目录)",
                "category": os.path.join(proj_root, os.path.basename(u["path"])),
                "score": 0, "reason": "unit/project", "text_len": len(body),
                "evidence": u["evidence"][:80], "unit": u["rel"],
                "target_name": os.path.basename(u["path"]), "name_reason": "",
                "confidence": "", "files": u["files"], "sha256": "",
            })
            continue
        if u["kind"] == "protected":
            rows.append({
                "src": u["path"], "size": u["bytes"], "ext": "(目录)",
                "category": os.path.join(rules.get("protect_category", "12-项目资料"),
                                         os.path.basename(u["path"])),
                "score": 0, "reason": "unit/protected", "text_len": len(body),
                "evidence": "手动保护", "unit": u["rel"],
                "target_name": os.path.basename(u["path"]), "name_reason": "",
                "confidence": "", "files": u["files"], "sha256": "",
            })
            continue
        if u["kind"] in ("program", "code", "repo"):
            rows.append({
                "src": u["path"], "size": u["bytes"], "ext": "(目录)",
                "category": os.path.join(prog_cat, os.path.basename(u["path"])),
                "score": 0, "reason": f"unit/{u['kind']}",
                "text_len": len(body), "evidence": u["evidence"][:80], "unit": u["rel"],
                "target_name": os.path.basename(u["path"]), "name_reason": "",
                "confidence": "", "files": u["files"], "sha256": "",
            })
            continue
        uname = os.path.basename(u["path"])
        utarget = uname
        if rename and TK.is_meaningless(uname + ".d") and len(body) >= 40:
            ut = titles.get(u["path"]) or (TK.guess_title(body, uname) if llm else "")
            if ut and len(ut) >= 4:
                utarget = TK.sanitize(ut, 40)
        cat, sc, reason, ev = best_category(os.path.basename(u["path"]), body, "",
                                            cats, min_score, 3, "", "")
        if not cat:
            cat = "06-其他/" + ("程序与工具" if u["kind"] in ("program", "code", "repo") else "项目归档")
            reason = f"unit:{u['kind']}"
        else:
            reason = f"unit/{u['kind']}:{reason}"
        if llm and llm_quota[0] > 0 and (not cat or reason.endswith("default")) and len(body) >= 40:
            llm_quota[0] -= 1
            llm_calls[0] += 1
            r = llm.classify(os.path.basename(u["path"]), body, cats)
            if r and r["confidence"] >= llm_min_conf:
                cat, reason = r["category"], f"llm-unit:{r['confidence']:.2f}"
                if r["title"]:
                    ev = "LLM:" + r["title"]
                    titles[u["path"]] = r["title"]
        rows.append({
            "src": u["path"], "size": u["bytes"], "ext": "(目录)", "category": cat,
            "score": sc, "reason": reason, "text_len": len(body),
            "evidence": (ev or u["evidence"])[:80], "unit": u["rel"],
            "target_name": utarget,
            "name_reason": "" if utarget == uname else "llm-title",
            "confidence": "", "files": u["files"], "sha256": "",
        })

    # 3) 散文件判定
    for p, st, rel, _ in items:
        ext = os.path.splitext(p)[1].lower()
        name = os.path.splitext(os.path.basename(p))[0]
        body = texts.get(p, "")
        file_sha = sha256(p) if st.st_size > 0 else ""
        base_row = {"src": p, "size": st.st_size, "ext": ext, "unit": "",
                    "target_name": os.path.basename(p), "name_reason": "",
                    "files": 1, "sha256": file_sha or ""}
        if st.st_size == 0:
            rows.append({**base_row, "category": shell_cat,
                         "score": 0, "reason": "shell", "text_len": 0, "evidence": "",
                         "confidence": ""})
            continue
        if ext in (".eml", ".msg") and (len(body) < 40 or len(set(body)) < 8):
            rows.append({**base_row, "category": shell_cat,
                         "score": 0, "reason": "shell", "text_len": len(body), "evidence": "",
                         "confidence": ""})
            continue
        if srcs.get(p) == "cloudsync":
            rows.append({**base_row, "category": enc_cat,
                         "score": 0, "reason": "cloudsync-enc", "text_len": 0, "evidence": "",
                         "confidence": ""})
            continue
        if ext == ".pdf" and srcs.get(p) == "scanned":
            rows.append({**base_row, "category": scan_cat,
                         "score": 0, "reason": "scanned-no-text", "text_len": 0,
                         "evidence": "", "confidence": ""})
            continue
        if ext in TK.need_text_exts() and len(body) < 10 and ext not in TK.get_image_exts():
            rows.append({**base_row, "category": low_cat,
                         "score": 0, "reason": f"unreadable:{srcs.get(p, 'empty')}",
                         "text_len": len(body), "evidence": "", "confidence": ""})
            continue
        # 补齐 1：敏感隔离（best_category 之前；evidence 只写命中词，不写内容）
        sens = _sensitive_hit(os.path.basename(p), body)
        if sens:
            rows.append({**base_row, "category": sensitive_cat,
                         "score": 0, "reason": "sensitive", "text_len": len(body),
                         "evidence": "敏感词:" + "、".join(sens), "confidence": "0.00"})
            continue
        # 补齐 2：SHA256 精确重复（第一个正常走流程，后续进复核）
        if file_sha:
            first = seen_sha.get(file_sha)
            if first is None:
                first = _db_dup_path(db_conn, file_sha, p)
            if first is not None:
                rows.append({**base_row, "category": dup_cat,
                             "score": 0, "reason": "dup:sha256", "text_len": len(body),
                             "evidence": first, "confidence": "0.90"})
                continue
            seen_sha[file_sha] = p
        # 预筛：命中则直接定类，不再走规则/语义/LLM
        pre_cat = None
        pre_sim = 0.0
        if prescreener is not None:
            prescreen_calls[0] += 1
            pre_cat, pre_sim, _pre_margin = prescreener.decide(os.path.basename(p), body)
            if pre_cat:
                prescreen_hits[0] += 1
        if pre_cat:
            rows.append({**base_row, "category": pre_cat,
                         "score": 0, "reason": f"prescreen:{pre_sim:.2f}",
                         "text_len": len(body), "evidence": "",
                         "confidence": f"{pre_sim:.2f}"})
            if cache_on and p in texts:
                cache_dict[cache_key(p, st)] = {"text": texts[p][:600], "source": srcs.get(p, "")}
            continue
        w_body = 4 if (content_first or len(body) >= 120) else (3 if len(body) >= 30 else 2)
        cat, sc, reason, ev = best_category(name, body, ext, cats, min_score,
                                            w_body, default_cat, low_cat, name_first=True)
        conf = ""
        if whitelist and cat.split("/")[0] not in whitelist:
            cat, reason = default_cat, "whitelist-miss"
        need_llm = bool(llm) and llm_quota[0] > 0 and (
            llm_all or reason in ("default",) or reason.startswith("low:"))
        if need_llm:
            llm_quota[0] -= 1
        rows.append({**base_row, "category": cat,
                     "score": sc, "reason": reason, "text_len": len(body),
                     "evidence": (ev or "")[:80], "confidence": conf})
        if need_llm:
            pending_llm.append((len(rows) - 1, os.path.basename(p), body, ev or ""))
        if cache_on and p in texts:
            cache_dict[cache_key(p, st)] = {"text": texts[p][:600], "source": srcs.get(p, "")}

    # 3.4) 向量语义归类：语义优先于字面规则（规则强命中除外）
    # 敏感/重复/预筛行不参与：它们已经定案，不可被向量改写。
    sem_hits = 0
    if sem and sem.cents:
        targets = [(i, r) for i, r in enumerate(rows)
                   if r["ext"] != "(目录)"
                   and not str(r["reason"]).startswith(("prescreen:", "dup:"))
                   and str(r["reason"]) != "sensitive"]
        sem_items = [(os.path.basename(r["src"]), texts.get(r["src"], "")) for _, r in targets]
        res = sem.classify_batch(sem_items)
        hit = 0
        for (i, r), x in zip(targets, res):
            if not x or not x["ok"]:
                continue
            strong_rule = r["reason"] and not r["reason"].startswith(("low", "default")) \
                and r["score"] >= strong
            if strong_rule and r["category"] == x["category"]:
                r["reason"] = f"rule+sem:{x['sim']:.2f}"
                hit += 1
                continue
            r["category"] = x["category"]
            r["reason"] = f"sem:{x['sim']:.2f}/{x['margin']:.2f}"
            r["confidence"] = f"{x['sim']:.2f}"
            hit += 1
        sem_hits = hit
        # 向量已定的不再送 LLM
        pending_llm = [t for t in pending_llm
                       if not str(rows[t[0]]["reason"]).startswith(("sem:", "rule+sem"))]

    # 3.5) 规则判不准的，批量交本地小模型「看内容」重判（语义优先）
    if pending_llm and llm:
        llm_calls[0] += len(pending_llm)
        res = llm.classify_batch([(n, b) for _, n, b, _ in pending_llm], cats,
                                 chunk=llm_chunk, timeout=llm_timeout)
        for (idx, _n, _b, _ev), r in zip(pending_llm, res):
            if r and r.get("skipped"):
                # 无正文 + 机器名：不猜，直接转待确认（v3.1.0）
                rows[idx]["category"] = low_cat
                rows[idx]["reason"] = "no-text:待确认"
                rows[idx]["confidence"] = ""
                continue
            if r and r.get("low_text") and r["confidence"] < llm_min_conf:
                # 正文过短、只有文件名可依：给出参考但不采纳
                rows[idx]["category"] = low_cat
                rows[idx]["reason"] = f"low-text:{r['category'].split('/')[0]}/{r['confidence']:.2f}"
                rows[idx]["confidence"] = f"{r['confidence']:.2f}"
                continue
            if r and r["confidence"] >= llm_min_conf:
                rows[idx]["category"] = r["category"]
                rows[idx]["reason"] = f"llm:{r['confidence']:.2f}"
                rows[idx]["confidence"] = f"{r['confidence']:.2f}"
                if r["title"]:
                    rows[idx]["evidence"] = ("LLM:" + r["title"])[:80]
                    titles[rows[idx]["src"]] = r["title"]

    # 4) 一致性校正（避免同类文件分家）
    # 4.1 同名族：同目录同 stem 的不同扩展名归同一类目（多数票）
    fam = defaultdict(list)
    for r in rows:
        if r["ext"] not in ("(目录)",):
            fam[(os.path.dirname(r["src"]), os.path.splitext(os.path.basename(r["src"]))[0])].append(r)
    fanout = 0
    for (_, _), grp in fam.items():
        if len(grp) < 2:
            continue
        top = Counter(g["category"].split("/")[0] for g in grp if not g["category"].startswith("99-")).most_common(1)
        if not top:
            continue
        major = top[0][0]
        for g in grp:
            if g["category"].split("/")[0] != major and not g["category"].startswith("99-"):
                g["category"] = [x["category"] for x in grp if x["category"].split("/")[0] == major][0]
                g["reason"] = "family-merge"
                fanout += 1

    # 4.2 目录投票：同一父目录多数已定类目，带走 default/低置信
    by_dir = defaultdict(list)
    for r in rows:
        if r["ext"] != "(目录)":
            by_dir[os.path.dirname(r["src"])].append(r)
    votes = 0
    for d, grp in by_dir.items():
        if len(grp) < 3:
            continue
        solid = [g for g in grp if not g["reason"].startswith(("default", "low:", "unreadable"))]
        if len(solid) < 2:
            continue
        c, n = Counter(g["category"] for g in solid).most_common(1)[0]
        if n / max(1, len(grp)) >= 0.6:
            for g in grp:
                if g["reason"].startswith(("default", "low:")):
                    g["category"] = c
                    g["reason"] = "dir-vote"
                    votes += 1

    # 4.3 近重复：内容相同/极近的文件同归类
    buckets = []
    near = 0
    for r in rows:
        if r["ext"] == "(目录)" or r["text_len"] < 40:
            continue
        h = TK.simhash(texts.get(r["src"], ""))
        if not h:
            continue
        placed = False
        for b in buckets:
            if TK.hamming(b["h"], h) <= 3 and not r["category"].startswith("99-"):
                if b["cat"] != r["category"] and not b["cat"].startswith("99-"):
                    r["category"] = b["cat"]
                    r["reason"] = "near-dup"
                    near += 1
                placed = True
                break
        if not placed:
            buckets.append({"h": h, "cat": r["category"]})

    # 5) 命名
    renamed = 0
    if rename:
        for r in rows:
            if r["ext"] == "(目录)":
                continue
            base = os.path.basename(r["src"])
            ext = r["ext"]
            if ext not in (".eml", ".msg") and not TK.is_meaningless(base, ext) \
                    and not rename_llm:
                continue
            newname, why = "", ""
            lt = titles.get(r["src"])
            if lt and len(lt) >= 4 and ext not in (".eml", ".msg"):
                lt = re.sub(r"\s+", " ", lt).strip(" ._-")
                m = (re.search(r"(20\d{2})[-_.年]?(\d{1,2})[-_.月]?(\d{1,2})", base)
                     or re.search(r"(20\d{2})[-_.年]?(\d{1,2})[-_.月]?(\d{1,2})", lt))
                if m and m.group(3):
                    try:
                        d = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                    except ValueError:
                        d = ""
                    if d and d[:4] not in lt:
                        lt = f"{d}_{lt}"
                newname, why = TK.sanitize(lt, 52) + ext, "llm-title"
            if ext in (".eml", ".msg"):
                newname, why = mail_name(r["src"], ext)
            if not newname:
                body = texts.get(r["src"], "")
                if not body:
                    continue
                if llm and (not TK.guess_title(body) or len(TK.guess_title(body)) < 3):
                    t = llm.name_file(base, body, r["category"])
                    if t:
                        newname, why = TK.sanitize(t, 46) + ext, "llm"
                if not newname:
                    fallback = datetime.fromtimestamp(
                        os.stat(r["src"]).st_mtime).strftime("%Y-%m-%d")
                    newname, why = TK.build_name(base, ext, body, r["category"],
                                                 template=name_template,
                                                 fallback_mtime=fallback)
            if newname:
                r["target_name"] = newname
                r["name_reason"] = why
                renamed += 1

    # 6) 输出：内部行 -> 统一 plan-item
    unit_texts = {u["path"]: unit_text.get(u["path"], "") for u in units}
    all_texts = dict(texts)
    all_texts.update(unit_texts)
    plan_items = [_to_plan_item(r, root, min_score, rename, all_texts, mtimes,
                                dst_root=dst_root)
                  for r in rows]
    if cache_path and cache_dict:
        os.makedirs(cache, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache_dict, f, ensure_ascii=False)
    if digest:
        write_digest(digest, root, units, rows, texts)

    by_action = Counter(it["action"] for it in plan_items)
    by_review = Counter(it["review_kind"] for it in plan_items if it["action"] == "review")
    dup_items = [it for it in plan_items if it["review_kind"] == "dup"]
    stats = {
        "total": len(plan_items),
        "counts": dict(Counter(it["category"] for it in plan_items)),
        "by_action": dict(by_action),
        "by_review_kind": dict(by_review),
        "review": int(by_action.get("review", 0)),
        "low_conf": int(by_review.get("low_conf", 0)),
        "dup_count": len(dup_items),
        "dup_reclaimable_bytes": sum(it["size"] for it in dup_items),
        "llm_calls": llm_calls[0],
        "prescreen_calls": prescreen_calls[0],
        "prescreen_hits": prescreen_hits[0],
        "sem_hits": sem_hits,
        "units": len(units),
        "loose": len(items),
        "with_text": sum(1 for r in rows if r["text_len"] >= 20),
        "sources": dict(Counter(srcs.values())),
        "corrections": {"family": fanout, "dir_vote": votes, "near_dup": near},
        "renamed": renamed,
    }
    return plan_items, stats


# ---------------------------------------------------------------- apply / rollback

def _resolve_target(dst, seen):
    """目标已存在（磁盘上或本轮已规划）时加 __n 后缀，绝不覆盖。"""
    if dst not in seen and not os.path.exists(dst):
        seen.add(dst)
        return dst
    d, name = os.path.dirname(dst), os.path.basename(dst)
    stem, ext = os.path.splitext(name)
    i = 1
    while True:
        cand = os.path.join(d, f"{stem}__{i}{ext}")
        if cand not in seen and not os.path.exists(cand):
            seen.add(cand)
            return cand
        i += 1


def apply_plan(items, root, *, dry_run=False, uid=1000, gid=1000, keep_review=False):
    """按统一 plan-item 执行移动。返回 (operations, report)。

    operations: [{src_path, dst_path, sha256}]（与原 --map 映射表同格式，
    第三列 sha256；调用方持久化后可用 common.rollback.rollback_pairs 回滚）。
    执行逻辑与原 cmd_apply 一致：源必须在 root 内、同名加 __n、
    shutil.move、chown（失败忽略）。与 CSV 时代不同的是：
    action='review'/'skip'/'tag' 的项一律不移动（等人工复核决定），
    keep_review=True 时连 99- 类目的 move 项也保持原位。
    dry_run 不落盘，但仍返回按 __n 规则解析好的 operations。
    """
    root = os.path.abspath(root)
    moved, failed, per_cat, operations = 0, [], Counter(), []
    skipped, already = 0, 0
    seen_targets = set()
    for it in items:
        src = it.get("src") or ""
        cat = it.get("category") or "06-其他"
        action = it.get("action") or "move"
        if action not in ("move", "rename"):
            skipped += 1
            continue
        if cat.startswith("99-") and keep_review:
            skipped += 1
            continue
        if not src or not os.path.exists(src):
            failed.append({"src": src, "reason": "源不存在"})
            continue
        # 安全栅栏：源必须位于根目录内，防止把别的目录搬进来
        if os.path.abspath(src) != root and not os.path.abspath(src).startswith(root + os.sep):
            failed.append({"src": src, "reason": f"源不在根目录 {root} 内，拒绝执行"})
            continue
        dst = it.get("dst") or ""
        if not dst:
            failed.append({"src": src, "reason": "无目标路径"})
            continue
        if os.path.abspath(src) == os.path.abspath(dst):
            per_cat[cat] += 1
            already += 1
            continue
        target = _resolve_target(dst, seen_targets)
        if dry_run:
            operations.append({"src_path": src, "dst_path": target,
                               "sha256": it.get("sha256") or ""})
            per_cat[cat] += 1
            moved += 1
            continue
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(src, target)
            try:
                os.chown(target, uid, gid)
                if os.path.isdir(target):
                    for dp, _, fns in os.walk(target):
                        for fn in fns:
                            try:
                                os.chown(os.path.join(dp, fn), uid, gid)
                            except OSError:
                                pass
            except (PermissionError, AttributeError):
                pass
            operations.append({"src_path": src, "dst_path": target,
                               "sha256": it.get("sha256") or ""})
            per_cat[cat] += 1
            moved += 1
        except Exception as e:  # noqa: BLE001
            failed.append({"src": src, "reason": str(e)})
    report = {"moved": moved, "failed": failed, "per_cat": dict(per_cat),
              "skipped": skipped, "already": already, "total": len(items)}
    return operations, report


def rollback_map(map_path, dry_run=False):
    """按 TSV 映射表回滚（兼容原版 rollback --map）。

    映射表格式：每行 `原路径 \\t 新路径 \\t sha256 \\t 时间`。
    返回 {"moved": n, "missing": [...], "failed": [...]}。
    """
    try:
        from common.rollback import rollback_from_map as _rb
    except Exception:  # noqa: BLE001
        from ..common.rollback import rollback_from_map as _rb
    return _rb(map_path, dry_run=dry_run)


# ---------------------------------------------------------------- analyze / explain

def analyze(root, unit_mode="auto"):
    """目录画像（与原 cmd_analyze 同口径，不写文件）。返回 dict。"""
    root = os.path.abspath(root)
    d = TK.dir_digest(root)
    units, loose, _ = scan_units(root, unit_mode,
                                 frozen=DEFAULT_FROZEN, transparent=DEFAULT_TRANSPARENT)
    return {"root": root, "digest": d,
            "unit_count": len(units), "loose_count": len(loose),
            "units": [{"rel": u["rel"], "kind": u["kind"], "evidence": u["evidence"],
                       "files": u["files"], "bytes": u["bytes"]} for u in units]}


def explain(path, rules=None, *, llm=None, ocr_url=None,
            no_ocr=False, max_chars=3000):
    """解释单个文件/目录的判定依据（与原 cmd_explain 同口径，返回 dict 不打印）。

    文件：标题猜测、日期、关键词、文件名是否有意义、simhash、正文前 600 字、
    抽取方式与正文长度；传入 llm 与 rules 时附 LLM 判定。
    目录：dir_digest + unit_kind。
    """
    p = os.path.abspath(path)
    if os.path.isdir(p):
        d = TK.dir_digest(p)
        k, ev = unit_kind(p)
        return {"path": p, "is_dir": True, "digest": d,
                "unit_kind": k, "unit_evidence": ev}
    ext = os.path.splitext(p)[1].lower()
    st = os.stat(p)
    t, src = TK.extract_text(p, ext, max_chars, ocr_url, allow_ocr=not no_ocr)
    out = {
        "path": p, "is_dir": False,
        "size": st.st_size, "size_human": human(st.st_size),
        "source": src, "text_len": len(t),
        "title": TK.guess_title(t, os.path.basename(p)) or "",
        "date": TK.find_date(t, os.path.basename(p)) or "",
        "keywords": TK.keywords(t, 8),
        "meaningless": TK.is_meaningless(os.path.basename(p), ext),
        "simhash": f"{TK.simhash(t):016x}",
        "text_preview": t[:600],
    }
    if llm is not None and rules is not None:
        rules_d = _load_rules(rules)
        try:
            alive = bool(llm.alive())
        except Exception:  # noqa: BLE001
            alive = False
        out["llm"] = llm.classify(os.path.basename(p), t,
                                  rules_d.get("categories", [])) if alive else None
    return out


# ---------------------------------------------------------------- learn

MACHINE_FN = re.compile(r"(?i)^(img|dsc|pxl|mvimg|screenshot|scan|doc|file|新建)[ _\-]?\d{3,}$")

DOC_EXT = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".md", ".txt", ".csv"}
MEDIA_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".m4a", ".mp3", ".mp4", ".mov", ".wav"}


def pick_samples(paths, limit=5, allow_media=False, pool=12):
    """从类目里挑「人类看得懂」的样例文件名，供 LLM 判类做 few-shot。

    allow_media=False 时文档类优先、图片音视频垫底（它们文件名信息量低）。
    """
    out, seen = [], set()

    def rank(p):
        ext = os.path.splitext(p)[1].lower()
        tier = 0 if ext in DOC_EXT else (1 if ext in MEDIA_EXT else 2)
        if not allow_media and tier == 1:
            tier = 3
        return (tier, len(os.path.basename(p)), os.path.basename(p))
    cands = sorted(paths, key=rank)
    for p in cands:
        fn = os.path.basename(p)
        stem = os.path.splitext(fn)[0]
        if not stem or fn in seen:
            continue
        if re.fullmatch(r"[0-9a-fA-F\-]{16,36}", stem) or re.fullmatch(r"[\d\s_\-().]+", stem):
            continue
        if MACHINE_FN.match(stem) or "副本" in fn:
            continue
        if not (re.search(r"[\u4e00-\u9fa5]", stem) or re.search(r"[A-Za-z]{3,}", stem)):
            continue
        if not 4 <= len(stem) <= 40:
            continue
        seen.add(fn)
        out.append(fn)
        if len(out) >= max(limit, pool):
            break
    return out[:max(limit, pool)]


def learn_rules(root, *, per_cat=6, topk=10, llm=None, default="", skip=(),
                ocr_url=None, no_ocr=False, llm_timeout=120):
    """从一个「已经整理好」的目录反推类目体系，返回 rules dict（不写文件）。

    与原 cmd_learn 同口径：每类目抽样读正文 -> 关键词（LLM 有则用 LLM，
    否则 TK.keywords）-> 跨类去噪 -> 实体画像（entitykit.build）。
    llm 传入实例时才用；不传则纯规则学习。
    """
    from . import entitykit
    from . import orgkit

    root = os.path.abspath(root)
    if isinstance(skip, str):
        skip_t = tuple(x for x in skip.split(",") if x)
    else:
        skip_t = tuple(skip or ())
    frozen = tuple(DEFAULT_FROZEN) + skip_t
    candidates = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if not os.path.isdir(p) or name.startswith(".") or name in frozen:
            continue
        if name.startswith("99-") or name in ("_Review", "待确认"):
            continue
        candidates.append((name, p))
    if not candidates:
        raise ValueError("没有可学习的子目录：请把 root 指向「已经按类目分好」的目录")

    cats, low = [], []
    sample_texts = {}
    for name, p in candidates:
        files, _ = dir_size(p)
        exts = Counter()
        samples = []
        pool, files_all = {}, []
        for dp, dns, fns in os.walk(p):
            dns[:] = [d for d in dns if d not in JUNK_NAMES]
            for fn in sorted(fns):
                ep = os.path.join(dp, fn)
                ext = os.path.splitext(fn)[1].lower()
                exts[ext] += 1
                if len(files_all) < 200:
                    files_all.append(ep)
                if ext in TK.need_text_exts():
                    pool.setdefault(ext, []).append(ep)
        # 按扩展名轮转取样，避免整个类目只读到同一种文档
        while len(samples) < per_cat and any(pool.values()):
            for ext in list(pool):
                if pool[ext] and len(samples) < per_cat:
                    samples.append(pool[ext].pop(0))
        texts = []
        for sp in samples:
            ext = os.path.splitext(sp)[1].lower()
            t, _ = TK.extract_text(sp, ext, 900, ocr_url=ocr_url,
                                   allow_ocr=not no_ocr)
            if t:
                texts.append(t)
        desc = ""
        kws = []
        if llm is not None:
            fnames = [os.path.basename(x) for x in samples] + \
                     [os.path.basename(x) for x in files_all[:30]]
            lres = llm.learn_keywords(name, texts, fnames, timeout=llm_timeout)
            if lres and lres.get("keywords"):
                kws = lres["keywords"]
                desc = lres.get("desc", "")
                if lres.get("name"):
                    nm = re.sub(r"^\d+[-_. ]*", "", str(lres["name"])).strip()
                    if 2 <= len(nm) <= 16 and nm not in name:
                        pass  # 原版只打印 LLM 建议类目名，不采纳
        if not kws:
            kws = TK.keywords("\n".join(texts), topk * 2)
        core = re.sub(r"^\d+[-_. ]*", "", name)
        core = re.sub(r"(已整理|整理|归档|文件|资料|[_\- ]*\d+)$", "", core).strip()
        if len(core) >= 2:
            kws = [core] + [k for k in kws if core not in k]
        examples = []
        for t in texts:
            t1 = re.sub(r"\s+", " ", t).strip()
            if len(t1) >= 60:
                examples.append(t1[:240])
            if len(examples) >= 6:
                break
        sample_texts[name] = list(texts)
        cats.append({"path": name, "_kws_raw": kws, "keywords": kws[:topk],
                     "desc": desc, "examples": examples,
                     "samples": pick_samples(files_all, limit=3, pool=12,
                                            allow_media=bool(re.search(r"图片|影像|照片|录音|视频", name))),
                     "exts": [e for e, _ in exts.most_common(4) if e]})
        if len(texts) < 2 or not kws:
            low.append(name)

    # 跨类去噪：在很多类目里都出现的高频词（如「有限公司」「医疗器械」）不具备区分度
    selfs = orgkit.learn_self_names([t for c in cats for t in c.get("examples", [])], top=2)
    df = Counter()
    for c in cats:
        for k in set(c["_kws_raw"]):
            df[k] += 1
    limit = max(2, min(4, int(len(cats) * 0.25)))
    scraps = ("有限公", "限公司", "器械有", "疗器械", "技有限", "海加晴", "及运营")
    JUNK_KW = {"公司", "产品", "文件", "资料", "证明", "材料", "相关", "其他", "附件"}

    def noisy(k):
        k = (k or "").strip()
        if not k or k.endswith(scraps):
            return True
        if k in JUNK_KW:
            return True
        if len(k) < 2 or len(k) > 12:            # 单字/长句都不适合当区分词
            return True
        if re.fullmatch(r"[\d\s.\-_/年月日]+", k):  # 纯数字/日期
            return True
        if any(x and x in k for x in selfs):      # 本方机构名，无区分度
            return True
        return False
    for c in cats:
        core = c["keywords"][0] if c["keywords"] else ""
        cand = sorted([k for k in c["_kws_raw"] if df[k] <= limit and not noisy(k)],
                      key=lambda x: -len(x))
        kept = []
        for k in cand:
            if any(k != o and (k in o or o in k) for o in kept):
                continue
            kept.append(k)
        kept = (([core] if core else []) + [k for k in kept if k != core])[:topk]
        if core and core not in kept:
            kept.insert(0, core)
        c["keywords"] = kept
        c.pop("_kws_raw", None)

    names = [c["path"] for c in cats]
    default_cat = default
    if not default_cat:
        for guess in [n for n in names if "其他" in n] + ["10-其他", "99-其他", "其他"]:
            if guess in names:
                default_cat = guess
                break
        default_cat = default_cat or names[-1]
    rules = {"default": default_cat, "categories": cats, "_learned_from": root,
             "self_names": selfs, "_low_keyword_categories": low}
    try:
        ents = entitykit.build(sample_texts, selfs)
        rules["entities"] = ents
    except Exception:  # noqa: BLE001
        rules["entities"] = {}
    return rules
