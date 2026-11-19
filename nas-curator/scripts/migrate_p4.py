#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P4 迁移：Marvis 侧两个旧技能的工作区数据 → NAS Curator 统一 DB。

迁移内容：
1. media-curator 的 hash_index.tsv（59,453 行）→ files（kind=media）+
   file_media（dhash）。路径存在与否决定 status=active/gone；去重只按 path；
   同一 sha256 多条 path 是真实重复文件，全部入库（引擎按 sha 查重复）。
   TSV 有两种行格式：5 列（sha/size/path/dhash/ts）与 3 列（sha/照片|视频/path）。
2. workfiles-curator 规则 → categories + rules：
   - work：rules.baijing-supply-v8.json（14 类目 + 5 实体画像）
   - family：rules.family.json（5 类目）
   - media：media_core/config.json（photo_dirs + video_dirs → 媒体类目）
   - example：rules.example.json 只存 rules 表（name=example，不激活），
     其 10 个通用类目不进 categories（与 live 的 v8 体系重叠，会污染分类）。
3. workfiles-curator 的映射表/正文缓存：NAS 上不存在（history/ 与 index/
   均为空目录，全盘无 map_*.tsv / cache.json），无物可迁，记 0。

只读源文件；只写目标 DB（调用方须先备份 DB、停容器）。支持 --dry-run
（只统计、不写库）。幂等：重复跑按 path/sha 去重、规则版本自增。
只用 tmp/临时 DB 测试，不碰 NAS——除非在 NAS 上显式执行。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import db as DB  # noqa: E402

SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def migrate_hash_index(conn, tsv_path, dry_run, stats):
    """hash_index.tsv → files + file_media。返回统计 dict。

    两种行格式（旧索引混写）：
    - 5 列：sha256 \\t size \\t path \\t dhash \\t timestamp
    - 3 列：sha256 \\t kind（照片/视频）\\t path（无 size/dhash，见 53271 行起）
    去重只按 path（files.path UNIQUE）；同一 sha256 出现在多条 path 是真实
    重复文件，两条都入库（引擎日后按 sha 查重复），只计数。
    """
    n_lines = n_ok = n_dup_sha = n_dup_path = n_bad = n_gone = 0
    n_kind_only = 0
    seen_sha: dict[str, str] = {}
    seen_path: dict[str, str] = {}
    rows = []  # (path, sha256, size, dhash, status)
    with open(tsv_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            n_lines += 1
            p = line.rstrip("\n").split("\t")
            if len(p) < 3:
                n_bad += 1
                continue
            sha, col2, path = p[0].strip(), p[1].strip(), p[2].strip()
            if not SHA_RE.match(sha) or not path:
                n_bad += 1
                continue
            try:
                size = int(col2)
                dh_raw = p[3].strip() if len(p) > 3 and p[3].strip() else ""
            except ValueError:
                # 3 列 kind 格式：无 size/dhash
                size = None
                dh_raw = ""
                n_kind_only += 1
            dhash = int(dh_raw) if dh_raw.isdigit() else None
            if sha in seen_sha:
                n_dup_sha += 1
            seen_sha[sha] = path
            if path in seen_path:
                n_dup_path += 1
            seen_path[path] = sha
            status = "active" if os.path.exists(path) else "gone"
            if status == "gone":
                n_gone += 1
            rows.append((path, sha, size, dhash, status))
            n_ok += 1
    stats["hash_index"] = {"lines": n_lines, "valid": n_ok,
                           "kind_only_lines": n_kind_only,
                           "dup_sha_paths": n_dup_sha,
                           "dup_path_overwritten": n_dup_path,
                           "bad_lines": n_bad, "gone_paths": n_gone}
    if dry_run:
        return
    # 单事务批量写入（与 common.db.upsert_file / save_file_media 同语义）
    conn.execute("BEGIN")
    try:
        conn.executemany(
            """INSERT INTO files (path, sha256, size, kind, ext, status)
               VALUES (?, ?, ?, 'media', ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                 sha256=COALESCE(excluded.sha256, files.sha256),
                 size=COALESCE(excluded.size, files.size),
                 kind='media', ext=excluded.ext, status=excluded.status,
                 updated_at=strftime('%Y-%m-%dT%H:%M:%S', 'now')""",
            [(path, sha, size, os.path.splitext(path)[1].lower(), status)
             for path, sha, size, _dh, status in rows])
        conn.executemany(
            """INSERT INTO file_media (file_id, dhash) VALUES (
                 (SELECT id FROM files WHERE path=?), ?)
               ON CONFLICT(file_id) DO UPDATE SET
                 dhash=COALESCE(excluded.dhash, file_media.dhash)""",
            [(path, dh) for path, _sha, _size, dh, _st in rows])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    stats["hash_index"]["inserted_files"] = len(seen_path)


def _import_categories(conn, payload, kind, dry_run, stats, name):
    """规则 JSON 的 categories → categories 表；返回 (类目数, 实体数)。"""
    cats = payload.get("categories") or []
    n_cat = 0
    cat_ids = {}
    for c in cats:
        path = c.get("path")
        if not path:
            continue
        samples = (c.get("examples") or [])[:3]
        samples = [s[:200] for s in samples if isinstance(s, str)]
        if not dry_run:
            cid = DB.ensure_category(
                conn, path, kind=kind,
                keywords=c.get("keywords"), desc=c.get("desc"),
                samples=samples or None, exts=c.get("exts"))
            cat_ids[path] = cid
        n_cat += 1
    n_ent = 0
    for ent_name, prof in (payload.get("entities") or {}).items():
        if not isinstance(prof, dict):
            continue
        # 主导类目 = 除 __ 开头键外计数最大的类目
        cands = {k: v for k, v in prof.items()
                 if not k.startswith("__") and isinstance(v, (int, float))}
        if not cands:
            continue
        dom = max(cands, key=cands.get)
        total = prof.get("__total") or sum(cands.values())
        ratio = prof.get("__ratio") or 0.0
        if not dry_run:
            cid = cat_ids.get(dom)
            if cid is None:
                row = conn.execute("SELECT id FROM categories WHERE path=?",
                                   (dom,)).fetchone()
                cid = row[0] if row else None
            if cid is not None:
                conn.execute(
                    """INSERT INTO entities (name, category_id, count, ratio)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(name) DO UPDATE SET
                         category_id=excluded.category_id,
                         count=excluded.count, ratio=excluded.ratio,
                         updated_at=strftime('%Y-%m-%dT%H:%M:%S', 'now')""",
                    (ent_name, cid, int(total), float(ratio)))
        n_ent += 1
    if not dry_run:
        conn.commit()
    stats[f"rules_{name}"] = {"categories": n_cat, "entities": n_ent}
    return n_cat, n_ent


def migrate_rules(conn, work_json, family_json, example_json, media_json,
                  dry_run, stats):
    if work_json:
        payload = _load_json(work_json)
        _import_categories(conn, payload, "document", dry_run, stats, "work")
        if not dry_run:
            DB.save_rules(conn, "work", payload, activate=True)
    if family_json:
        payload = _load_json(family_json)
        _import_categories(conn, payload, "document", dry_run, stats, "family")
        if not dry_run:
            DB.save_rules(conn, "family", payload, activate=True)
    if media_json:
        cfg = _load_json(media_json)
        n_cat = 0
        for dirs in (cfg.get("photo_dirs") or {}, cfg.get("video_dirs") or {}):
            for _label, rel in dirs.items():
                if not rel:
                    continue
                if not dry_run:
                    DB.ensure_category(conn, rel, kind="media",
                                       desc=f"媒体类目（旧 media-curator）")
                n_cat += 1
        if not dry_run:
            conn.commit()
            DB.save_rules(conn, "media", cfg, activate=True)
        stats["rules_media"] = {"categories": n_cat, "entities": 0}
    if example_json:
        # 只存档，不激活、不进 categories（通用模板，与 live 的 v8 体系重叠）
        payload = _load_json(example_json)
        if not dry_run:
            DB.save_rules(conn, "example", payload, activate=False)
        stats["rules_example"] = {"categories": len(payload.get("categories") or []),
                                  "entities": 0, "archived_only": True}


def main():
    ap = argparse.ArgumentParser(description="P4 迁移：旧技能数据 → 统一 DB")
    ap.add_argument("--db", required=True)
    ap.add_argument("--hash-index", default="")
    ap.add_argument("--rules-work", default="")
    ap.add_argument("--rules-family", default="")
    ap.add_argument("--rules-example", default="")
    ap.add_argument("--media-config", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = DB.connect(args.db)
    try:
        DB.init_db(args.db)
        before = {
            "files": conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
            "file_media": conn.execute("SELECT COUNT(*) FROM file_media").fetchone()[0],
            "categories": conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0],
            "entities": conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
            "rules": conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0],
        }
        stats: dict = {"dry_run": args.dry_run, "before": before}
        if args.hash_index:
            migrate_hash_index(conn, args.hash_index, args.dry_run, stats)
        migrate_rules(conn, args.rules_work, args.rules_family,
                      args.rules_example, args.media_config, args.dry_run, stats)
        after = {
            "files": conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
            "file_media": conn.execute("SELECT COUNT(*) FROM file_media").fetchone()[0],
            "categories": conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0],
            "entities": conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
            "rules": conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0],
        }
        stats["after"] = after
        stats["delta"] = {k: after[k] - before[k] for k in before}
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
