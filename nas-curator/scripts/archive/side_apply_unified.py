#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unified apply (stage 3): move all 11 sources per /data/side_naming.jsonl,
mark moved files' pending review rows approved (decided_by side-classify*),
INSERT review rows for remaining reviews that lack one, write manifest/
rename map/review list. NO deletions here (separate side_delete_dups.py).
Preconditions: guard 11 sources True + controls False; batch3 heartbeat done.
Run: python3 /data/side_apply_unified.py [--dry-run]
"""
import sys, os, json, time, re, sqlite3, argparse
sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

DB = "/data/nas-curator.db"
NAMING = "/data/side_naming.jsonl"
REMAINING = "/data/side_review_remaining.jsonl"
SOURCES = [
    "/vol1/1000/收件箱/待整理",
    "/vol1/1000/收件箱/待整理-视频",
    "/vol1/1000/收件箱/收件箱-邮件归档",
    "/vol1/1000/收件箱/99-待确认-上海润心",
    "/vol1/1000/收件箱/99-待确认-北京供应链",
    "/vol1/1000/收件箱/99-待确认-辽宁器械",
    "/vol1/1000/收件箱/年份待确认-王震周报",
    "/vol1/1000/收件箱/99-待确认-刘音",
    "/vol1/1000/收件箱/99-待确认-家庭王震",
    "/vol1/1000/收件箱/待确认-媒体库",
    "/vol1/1000/收件箱/99-待确认-Mac下载",
]
CONTROLS = ["/vol1/1000/收件箱/", "/vol1/1000/媒体库/"]


def kind_for(reason):
    r = reason or ""
    if r.startswith("重复"):
        return "dup"
    if r.startswith("低置信") or r.startswith("API失败"):
        return "low_conf"
    if "模糊" in r:
        return "blur"
    return "ambiguous"


def versioned_name(dst_dir, new_name, mtime, size):
    stem, ext = os.path.splitext(new_name)
    tstr = time.strftime("%Y%m%d-%H%M%S", time.localtime(mtime))
    n = 1
    while True:
        cand = "%s_%s_%d_%d%s" % (stem, tstr, size, n, ext)
        if not os.path.exists(os.path.join(dst_dir, cand)):
            return cand
        n += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # 1. guard (is_excluded returns (bool, reason) tuple; unpack [0])
    from common.guard import is_excluded
    bad = [s for s in SOURCES if not is_excluded(s)[0]]
    if bad:
        print("GUARD FAIL (not excluded): %s" % bad, flush=True)
        return 2
    badc = [c for c in CONTROLS if is_excluded(c)[0]]
    if badc:
        print("GUARD FAIL (control excluded): %s" % badc, flush=True)
        return 2
    print("guard ok: 11 sources True, controls False", flush=True)

    # 2. batch3 SigLIP phase done? (VLM phase is API-bound; moves are metadata-only,
    # so proceeding during VLM is the sanctioned "错开". Abort only while SigLIP runs.)
    try:
        hb = json.load(open("/data/progress/year-reclassify-batch3.json"))
        phase = hb.get("phase", "")
        if phase == "siglip":
            print("batch3 SigLIP still running (%s/%s); abort" % (
                hb.get("done"), hb.get("total")), flush=True)
            return 3
        print("batch3 phase=%s (%s/%s, status=%s); SigLIP done, proceeding" % (
            phase, hb.get("done"), hb.get("total"), hb.get("status")), flush=True)
    except OSError as e:
        print("batch3 heartbeat unreadable: %s; abort" % e, flush=True)
        return 3

    moves = [json.loads(l) for l in open(NAMING, encoding="utf-8")]
    moves.sort(key=lambda r: r["src"])
    print("moves: %d" % len(moves), flush=True)
    moved_srcs = set(r["src"] for r in moves)
    # remaining reviews: resolve-1 + resolve-2, dedup by src (resolve-2 wins:
    # it carries the salvage outcome for the 70 ffmpeg videos, whose full-run
    # rows say only "待ffmpeg兜底"); drop any src that is being moved.
    reviews_by_src = {}
    for path in (REMAINING, "/data/side_review_remaining2.jsonl"):
        if os.path.exists(path):
            for l in open(path, encoding="utf-8"):
                r = json.loads(l)
                reviews_by_src[r["src"]] = r
    reviews = [r for r in reviews_by_src.values() if r["src"] not in moved_srcs]
    print("remaining reviews: %d (deduped, moved srcs dropped)" % len(reviews), flush=True)

    if args.dry_run:
        from collections import Counter
        print("DRY RUN " + json.dumps({
            "moves": len(moves),
            "by_party": dict(Counter(r["party"] for r in moves)),
            "dst_dirs": len(set(r["dst_dir"] for r in moves)),
            "reviews": len(reviews),
            "review_kinds": dict(Counter(kind_for(r.get("review_reason", "")) for r in reviews)),
        }, ensure_ascii=False), flush=True)
        return 0

    # 3. DB backup (sqlite backup API, same file as host path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = "/data/nas-curator.db.bak-%s-pre-side-classify" % stamp
    db = sqlite3.connect(DB)
    db.execute("PRAGMA busy_timeout=60000")

    _orig_exec = db.execute

    def dexec(sql, params=()):
        # batch3 VLM/summary phase also writes to this DB; retry on lock
        for attempt in range(8):
            try:
                return _orig_exec(sql, params)
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and attempt < 7:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
    with sqlite3.connect(bak) as dst:
        db.backup(dst)
    print("db backup: %s" % bak, flush=True)

    # 4. plan
    summary = json.dumps({"kind": "side-classify-unified", "backup": bak,
                          "naming": NAMING, "moves": len(moves),
                          "reviews": len(reviews)}, ensure_ascii=False)
    cur = dexec("INSERT INTO plans (library, summary) VALUES ('inbox', ?)", (summary,))
    plan_id = cur.lastrowid
    dexec("UPDATE plans SET status='applying' WHERE id=?", (plan_id,))
    db.commit()
    print("plan #%d created" % plan_id, flush=True)

    # pending review rows by src
    pending = {}
    for rq_id, payload in dexec(
            "SELECT id, payload FROM review_queue WHERE status='pending'"):
        try:
            src = (json.loads(payload or "{}") or {}).get("src")
        except Exception:
            src = None
        if src:
            pending[src] = rq_id
    print("pending review rows loaded: %d" % len(pending), flush=True)

    counts = {"moved": 0, "renamed": 0, "failed": 0, "src_gone": 0,
              "marked_approved": 0, "review_inserted": 0,
              "review_already_pending": 0, "review_items": 0}

    def mark_approved(src, decided_by):
        rq_id = pending.get(src)
        if not rq_id:
            return False
        cur = dexec(
            "UPDATE review_queue SET status='approved', decided_by=?, "
            "decided_at=strftime('%Y-%m-%dT%H:%M:%S','now') "
            "WHERE id=? AND status='pending'", (decided_by, rq_id))
        if cur.rowcount:
            counts["marked_approved"] += 1
            return True
        return False

    t0 = time.time()
    manifest_rows = []
    rename_rows = []
    for i, r in enumerate(moves):
        src = r["src"]
        dst_dir = r["dst_dir"]
        new_name = r["new_name"]
        renamed = (new_name != r["filename"])
        if not os.path.exists(src):
            counts["src_gone"] += 1
            dexec(
                """INSERT INTO plan_items
                   (plan_id, src_path, dst_path, new_name, category, confidence,
                    reason, evidence, action, kind, size, ext, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (plan_id, src, None, new_name,
                 "%s/%s" % (r["party"], r.get("subdir", "")),
                 r.get("confidence"), "side-classify:src-gone",
                 r.get("src_kind", ""), "move", "inbox",
                 r.get("size"), r.get("ext"), "failed"))
            continue
        os.makedirs(dst_dir, exist_ok=True)
        dst_full = os.path.join(dst_dir, new_name)
        if os.path.exists(dst_full):
            new_name = versioned_name(dst_dir, new_name, r.get("mtime", 0), r.get("size", 0))
            dst_full = os.path.join(dst_dir, new_name)
            renamed = True
        try:
            os.rename(src, dst_full)
        except OSError as e:
            counts["failed"] += 1
            dexec(
                """INSERT INTO plan_items
                   (plan_id, src_path, dst_path, new_name, category, confidence,
                    reason, evidence, action, kind, size, ext, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (plan_id, src, dst_full, new_name,
                 "%s/%s" % (r["party"], r.get("subdir", "")),
                 r.get("confidence"), "side-classify:move-failed:%s" % type(e).__name__,
                 r.get("src_kind", ""), "move", "inbox",
                 r.get("size"), r.get("ext"), "failed"))
            print("FAILED %s %s" % (src, e), flush=True)
            continue
        action = "rename" if renamed else "move"
        if renamed:
            counts["renamed"] += 1
        counts["moved"] += 1
        dexec(
            """INSERT INTO plan_items
               (plan_id, src_path, dst_path, new_name, category, confidence,
                reason, evidence, action, kind, size, ext, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (plan_id, src, dst_full, new_name,
             "%s/%s" % (r["party"], r.get("subdir", "")),
             r.get("confidence"), "side-classify:%s" % r.get("method", ""),
             r.get("src_kind", ""), action, "inbox",
             r.get("size"), r.get("ext"), "done"))
        dexec(
            "INSERT INTO operations (plan_id, src_path, dst_path) VALUES (?,?,?)",
            (plan_id, src, dst_full))
        mark_approved(src, "side-classify-rename" if renamed else "side-classify")
        manifest_rows.append((src, dst_full, r["party"], r.get("subdir", ""),
                              r.get("confidence"), r.get("method", "")))
        if renamed:
            rename_rows.append((src, dst_full))
        if (i + 1) % 500 == 0:
            db.commit()
            print("... %d/%d moved (%.0fs)" % (i + 1, len(moves), time.time() - t0), flush=True)
    db.commit()

    # 5. remaining reviews -> plan_items + INSERT missing review rows
    review_list_rows = []
    for r in reviews:
        src = r["src"]
        reason = r.get("review_reason", "")
        k = kind_for(reason)
        suggested = ""
        m = re.search(r"建议落位\s*(.*)$", reason)
        if m:
            suggested = m.group(1).strip()
        dexec(
            """INSERT INTO plan_items
               (plan_id, src_path, dst_path, category, confidence,
                reason, evidence, action, kind, size, ext, review_kind, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (plan_id, src, None, r.get("party") or "无法判断",
             r.get("confidence"), "side-classify-review:%s" % reason[:200],
             r.get("method", ""), "review", "inbox",
             r.get("size"), r.get("ext"), k, "pending"))
        counts["review_items"] += 1
        if src in pending:
            counts["review_already_pending"] += 1
        else:
            payload = json.dumps({
                "src": src,
                "category": r.get("party") or "无法判断",
                "confidence": r.get("confidence") or 0.0,
                "reason": reason[:300],
                "evidence": r.get("method", ""),
                "suggested_dst": suggested,
                "tags": [r.get("src_kind", "")],
            }, ensure_ascii=False)
            dexec(
                "INSERT INTO review_queue (plan_id, kind, payload) VALUES (?,?,?)",
                (plan_id, k, payload))
            counts["review_inserted"] += 1
        review_list_rows.append((src, reason, suggested))
    db.commit()

    dexec("UPDATE plans SET status='done' WHERE id=?", (plan_id,))
    db.commit()

    # 6. manifest files
    with open("/data/side_apply_manifest.tsv", "w", encoding="utf-8") as f:
        f.write("src\tdst\tparty\tsubdir\tconfidence\tmethod\n")
        for row in manifest_rows:
            f.write("\t".join(str(x) for x in row) + "\n")
    with open("/data/side_rename_map.tsv", "w", encoding="utf-8") as f:
        f.write("src\tdst\n")
        for row in rename_rows:
            f.write("\t".join(row) + "\n")
    with open("/data/side_review_list.tsv", "w", encoding="utf-8") as f:
        f.write("src\treason\tsuggested_dst\n")
        for row in review_list_rows:
            f.write("\t".join(str(x).replace("\t", " ") for x in row) + "\n")

    # 7. verify
    src_left = sum(1 for r in moves if os.path.exists(r["src"]))
    dst_ok = 0
    for src, dst, *_ in manifest_rows:
        if os.path.exists(dst):
            dst_ok += 1
    integ = dexec("PRAGMA integrity_check").fetchone()[0]
    items = dexec(
        "SELECT status, COUNT(*) FROM plan_items WHERE plan_id=? GROUP BY status",
        (plan_id,)).fetchall()
    print("SUMMARY " + json.dumps({
        "plan_id": plan_id, "backup": bak, "counts": counts,
        "src_left": src_left, "dst_ok": dst_ok, "manifest": len(manifest_rows),
        "integrity": integ, "plan_items": items,
        "elapsed_s": round(time.time() - t0, 1),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
