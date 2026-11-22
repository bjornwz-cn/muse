#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""common.db —— NAS Curator 统一元数据存储（SQLite，WAL 模式）。

只做薄封装：建表 / 连接 / 各表的增查改。无 ORM，无重型依赖。
时间一律存 ISO 本地时间字符串（strftime('%Y-%m-%dT%H:%M:%S', 'now')）。
"""
from __future__ import annotations

import json
import os
import sqlite3

_HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_HERE)
DEFAULT_DB = os.path.join(PROJECT_ROOT, "data", "nas-curator.db")
SCHEMA_PATH = os.path.join(_HERE, "schema.sql")


def init_db(path: str | None = None) -> str:
    """建库（幂等）：建父目录、执行 schema.sql、开 WAL。返回实际 DB 路径。"""
    path = path or DEFAULT_DB
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = connect(path)
    try:
        with open(SCHEMA_PATH, encoding="utf-8") as f:
            conn.executescript(f.read())
        # 迁移：P1 建的旧库补 auto_exempt 列（P2，unit 豁免自动执行阈值用）
        cols = [r[1] for r in conn.execute("PRAGMA table_info(plan_items)")]
        if "auto_exempt" not in cols:
            conn.execute("ALTER TABLE plan_items "
                         "ADD COLUMN auto_exempt INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    finally:
        conn.close()
    return path


def connect(path: str | None = None) -> sqlite3.Connection:
    """打开连接：Row 工厂 + 外键 + WAL + NORMAL 同步 + 写锁等待 30 秒。

    busy_timeout：容器内 Web 服务与收件箱监听（inbox --watch）两个进程同时写
    同一个 SQLite 文件，WAL 下写锁冲突时等 30 秒再报错，而不是立即报 locked。
    """
    path = path or DEFAULT_DB
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _d(row):
    return dict(row) if row is not None else None


# ---------------------------------------------------------------- files

def upsert_file(conn, path, sha256=None, size=None, mtime=None,
                kind="document", ext=None, status="active"):
    """按 path 登记文件；已存在则更新元数据。返回 file_id。"""
    ext = ext if ext is not None else os.path.splitext(path)[1].lower()
    cur = conn.execute(
        """INSERT INTO files (path, sha256, size, mtime, kind, ext, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET
             sha256=COALESCE(excluded.sha256, files.sha256),
             size=COALESCE(excluded.size, files.size),
             mtime=COALESCE(excluded.mtime, files.mtime),
             kind=excluded.kind, ext=excluded.ext, status=excluded.status,
             updated_at=strftime('%Y-%m-%dT%H:%M:%S', 'now')""",
        (path, sha256, size, mtime, kind, ext, status))
    conn.commit()
    return cur.lastrowid or get_file_by_path(conn, path)["id"]


def get_file_by_path(conn, path):
    return _d(conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone())


def get_file_by_sha(conn, sha256):
    return [_d(r) for r in conn.execute(
        "SELECT * FROM files WHERE sha256=? AND status='active'", (sha256,))]


def set_file_status(conn, file_id, status):
    conn.execute("UPDATE files SET status=?, updated_at=strftime('%Y-%m-%dT%H:%M:%S', 'now')"
                 " WHERE id=?", (status, file_id))
    conn.commit()


def save_file_text(conn, file_id, text, source):
    conn.execute(
        """INSERT INTO file_text (file_id, text, source) VALUES (?, ?, ?)
           ON CONFLICT(file_id) DO UPDATE SET text=excluded.text,
             source=excluded.source,
             extracted_at=strftime('%Y-%m-%dT%H:%M:%S', 'now')""",
        (file_id, text, source))
    conn.commit()


def save_file_media(conn, file_id, dhash=None, phash=None, width=None,
                    height=None, duration=None, blur=None, clip_scores=None,
                    captions=None):
    conn.execute(
        """INSERT INTO file_media
           (file_id, dhash, phash, width, height, duration, blur, clip_scores, captions)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(file_id) DO UPDATE SET
             dhash=COALESCE(excluded.dhash, file_media.dhash),
             phash=COALESCE(excluded.phash, file_media.phash),
             width=COALESCE(excluded.width, file_media.width),
             height=COALESCE(excluded.height, file_media.height),
             duration=COALESCE(excluded.duration, file_media.duration),
             blur=COALESCE(excluded.blur, file_media.blur),
             clip_scores=COALESCE(excluded.clip_scores, file_media.clip_scores),
             captions=COALESCE(excluded.captions, file_media.captions)""",
        (file_id, dhash, phash, width, height, duration, blur,
         json.dumps(clip_scores, ensure_ascii=False) if clip_scores is not None else None,
         captions))
    conn.commit()


# ---------------------------------------------------------------- categories / entities / tags

def ensure_category(conn, path, kind=None, keywords=None, desc=None,
                     samples=None, exts=None, parent_id=None):
    conn.execute(
        """INSERT INTO categories (path, kind, keywords, desc, samples, exts, parent_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET
             kind=COALESCE(excluded.kind, categories.kind),
             keywords=COALESCE(excluded.keywords, categories.keywords),
             desc=COALESCE(excluded.desc, categories.desc),
             samples=COALESCE(excluded.samples, categories.samples),
             exts=COALESCE(excluded.exts, categories.exts)""",
        (path, kind,
         json.dumps(keywords, ensure_ascii=False) if keywords is not None else None,
         desc,
         json.dumps(samples, ensure_ascii=False) if samples is not None else None,
         json.dumps(exts, ensure_ascii=False) if exts is not None else None,
         parent_id))
    conn.commit()
    row = conn.execute("SELECT id FROM categories WHERE path=?", (path,)).fetchone()
    return row["id"]


def get_category(conn, path):
    return _d(conn.execute("SELECT * FROM categories WHERE path=?", (path,)).fetchone())


def list_categories(conn, kind=None):
    if kind:
        rows = conn.execute("SELECT * FROM categories WHERE kind=? ORDER BY path", (kind,))
    else:
        rows = conn.execute("SELECT * FROM categories ORDER BY path")
    return [_d(r) for r in rows]


def ensure_tag(conn, name, kind=None, color=None):
    conn.execute(
        "INSERT OR IGNORE INTO tags (name, kind, color) VALUES (?, ?, ?)",
        (name, kind, color))
    conn.commit()
    row = conn.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
    return row["id"]


# ---------------------------------------------------------------- rules

def save_rules(conn, name, payload, activate=True):
    """存一条规则集（版本自增）。payload 为 dict。返回 (id, version)。"""
    row = conn.execute("SELECT MAX(version) AS v FROM rules WHERE name=?", (name,)).fetchone()
    version = (row["v"] or 0) + 1
    if activate:
        conn.execute("UPDATE rules SET is_active=0 WHERE name=?", (name,))
    cur = conn.execute(
        "INSERT INTO rules (name, version, payload, is_active) VALUES (?, ?, ?, ?)",
        (name, version, json.dumps(payload, ensure_ascii=False), 1 if activate else 0))
    conn.commit()
    return cur.lastrowid, version


def get_active_rules(conn, name):
    row = conn.execute(
        "SELECT * FROM rules WHERE name=? AND is_active=1 ORDER BY version DESC LIMIT 1",
        (name,)).fetchone()
    if not row:
        return None
    d = _d(row)
    d["payload"] = json.loads(d["payload"])
    return d


# ---------------------------------------------------------------- plans / plan_items

PLAN_ITEM_KEYS = (
    "file_id", "src_path", "dst_path", "new_name", "category_id", "category",
    "confidence", "reason", "evidence", "action", "kind", "size", "ext",
    "sha256", "tags", "review_kind", "status",
)


def create_plan(conn, library, rules_version=None, summary=None):
    cur = conn.execute(
        "INSERT INTO plans (library, rules_version, summary) VALUES (?, ?, ?)",
        (library, rules_version,
         json.dumps(summary, ensure_ascii=False) if summary is not None else None))
    conn.commit()
    return cur.lastrowid


def add_plan_item(conn, plan_id, item):
    """item 为统一 plan-item dict（见 curator_core.engine / media_core.engine）。
    tags 字段接受 list[str] 或 JSON 字符串。"""
    tags = item.get("tags")
    if isinstance(tags, (list, tuple)):
        tags = json.dumps(list(tags), ensure_ascii=False)
    # 统一 plan-item 用 src/dst；DB 列名是 src_path/dst_path（兼容两种键名）
    src_path = item.get("src_path") or item.get("src")
    dst_path = item.get("dst_path") or item.get("dst")
    cur = conn.execute(
        """INSERT INTO plan_items
           (plan_id, file_id, src_path, dst_path, new_name, category_id, category,
            confidence, reason, evidence, action, kind, size, ext, sha256, tags,
            review_kind, status, auto_exempt)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (plan_id, item.get("file_id"), src_path, dst_path,
         item.get("new_name"), item.get("category_id"), item.get("category"),
         item.get("confidence"), item.get("reason"), item.get("evidence"),
         item.get("action", "move"), item.get("kind"), item.get("size"),
         item.get("ext"), item.get("sha256"), tags, item.get("review_kind"),
         item.get("status", "pending"),
         1 if item.get("auto_exempt") else 0))
    conn.commit()
    return cur.lastrowid


def list_plan_items(conn, plan_id, action=None, status=None):
    sql = "SELECT * FROM plan_items WHERE plan_id=?"
    args = [plan_id]
    if action:
        sql += " AND action=?"
        args.append(action)
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY id"
    return [_d(r) for r in conn.execute(sql, args)]


def set_plan_status(conn, plan_id, status, summary=None):
    if summary is None:
        conn.execute("UPDATE plans SET status=? WHERE id=?", (status, plan_id))
    else:
        conn.execute("UPDATE plans SET status=?, summary=? WHERE id=?",
                     (status, json.dumps(summary, ensure_ascii=False), plan_id))
    conn.commit()


def get_plan(conn, plan_id):
    d = _d(conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone())
    if d and d.get("summary"):
        d["summary"] = json.loads(d["summary"])
    return d


# ---------------------------------------------------------------- operations

def add_operation(conn, plan_id, src_path, dst_path, sha256=None):
    cur = conn.execute(
        "INSERT INTO operations (plan_id, src_path, dst_path, sha256) VALUES (?, ?, ?, ?)",
        (plan_id, src_path, dst_path, sha256))
    conn.commit()
    return cur.lastrowid


def list_operations(conn, plan_id):
    return [_d(r) for r in conn.execute(
        "SELECT * FROM operations WHERE plan_id=? ORDER BY id", (plan_id,))]


def mark_rolled_back(conn, op_id):
    conn.execute("UPDATE operations SET rolled_back_at=strftime('%Y-%m-%dT%H:%M:%S', 'now')"
                 " WHERE id=?", (op_id,))
    conn.commit()


# ---------------------------------------------------------------- review_queue

def has_review_for_src(conn, src):
    """0.1.9 去重：同一 src 是否已有复核行（pending，或已裁决 approved/rejected）。

    复用 #142 重跑脚本的口径（建复核行前查 review_queue 同一 src 的 pending
    行，有则跳过），并按 0.1.9 决议扩展到已裁决行——watcher 重复建计划时，
    同一文件绝不再产生第二条复核行。plan_items 审计行不受影响（照常记）。
    """
    n = conn.execute(
        "SELECT COUNT(*) FROM review_queue WHERE "
        "json_extract(payload, '$.src')=? AND status IN ('pending','approved','rejected')",
        (src,)).fetchone()[0]
    return n > 0


def add_review(conn, file_id=None, plan_id=None, kind=None, payload=None):
    cur = conn.execute(
        "INSERT INTO review_queue (file_id, plan_id, kind, payload) VALUES (?, ?, ?, ?)",
        (file_id, plan_id, kind,
         json.dumps(payload, ensure_ascii=False) if payload is not None else None))
    conn.commit()
    return cur.lastrowid


def list_review(conn, status="pending", kind=None):
    sql = "SELECT * FROM review_queue WHERE status=?"
    args = [status]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    sql += " ORDER BY id"
    out = []
    for r in conn.execute(sql, args):
        d = _d(r)
        if d.get("payload"):
            d["payload"] = json.loads(d["payload"])
        out.append(d)
    return out


def decide_review(conn, review_id, decision, decided_by="user"):
    assert decision in ("approved", "rejected")
    conn.execute(
        "UPDATE review_queue SET status=?, decided_by=?,"
        " decided_at=strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id=?",
        (decision, decided_by, review_id))
    conn.commit()


# ---------------------------------------------------------------- jobs

def create_job(conn, kind):
    cur = conn.execute("INSERT INTO jobs (kind) VALUES (?)", (kind,))
    conn.commit()
    return cur.lastrowid


def finish_job(conn, job_id, status, summary=None):
    assert status in ("done", "failed")
    conn.execute(
        "UPDATE jobs SET status=?, finished_at=strftime('%Y-%m-%dT%H:%M:%S', 'now'),"
        " summary=? WHERE id=?",
        (status, json.dumps(summary, ensure_ascii=False) if summary is not None else None,
         job_id))
    conn.commit()
