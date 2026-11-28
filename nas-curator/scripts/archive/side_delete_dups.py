#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Strict-rule dedup deletion (stage 4, AFTER apply verified).
Deletes a review dup loser ONLY if: sha256(loser src) == sha256(kept dst)
AND the kept copy is confirmed present at its final dst (from naming map).
Creates its own plan (library inbox, kind side-classify-dedup-delete),
records plan_items + operations, marks the loser's review row approved
(decided_by side-classify-dedup). Writes /data/side_delete_list.tsv.
Run: python3 /data/side_delete_dups.py [--dry-run]
"""
import sys, os, json, time, sqlite3, hashlib, argparse
sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

DB = "/data/nas-curator.db"
NAMING = "/data/side_naming.jsonl"
REMAINING = "/data/side_review_remaining.jsonl"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    naming = {}
    for line in open(NAMING, encoding="utf-8"):
        r = json.loads(line)
        naming[r["src"]] = r["dst_full"]
    dups = []
    seen = set()
    for path in (REMAINING, "/data/side_review_remaining2.jsonl"):
        if not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8"):
            r = json.loads(line)
            if r.get("method") == "dedup" and r.get("dup_of") and r["src"] not in seen:
                seen.add(r["src"])
                dups.append(r)
    print("dup losers in review: %d" % len(dups), flush=True)
    # kept copy dst: dup_of is a SOURCE path; if the kept source itself was moved,
    # its dst is in naming; if the kept copy was already in place (family target),
    # dup_of itself is the final location.
    plan = []
    for r in dups:
        loser = r["src"]
        kept_src = r["dup_of"]
        kept_dst = naming.get(kept_src, kept_src)
        plan.append({"loser": loser, "kept_src": kept_src, "kept_dst": kept_dst,
                     "reason": r.get("review_reason", "")})
    if args.dry_run:
        for p in plan:
            print("WOULD-CHECK loser=%s kept_dst=%s exists=%s" % (
                p["loser"], p["kept_dst"], os.path.exists(p["kept_dst"])))
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = "/data/nas-curator.db.bak-%s-pre-side-classify-delete" % stamp
    db = sqlite3.connect(DB)
    db.execute("PRAGMA busy_timeout=30000")
    with sqlite3.connect(bak) as dst:
        db.backup(dst)
    print("db backup: %s" % bak, flush=True)
    summary = json.dumps({"kind": "side-classify-dedup-delete", "backup": bak,
                          "candidates": len(plan)}, ensure_ascii=False)
    cur = db.execute("INSERT INTO plans (library, summary) VALUES ('inbox', ?)", (summary,))
    plan_id = cur.lastrowid
    db.execute("UPDATE plans SET status='applying' WHERE id=?", (plan_id,))
    db.commit()
    print("plan #%d created" % plan_id, flush=True)

    pending = {}
    for rq_id, payload in db.execute(
            "SELECT id, payload FROM review_queue WHERE status='pending'"):
        try:
            src = (json.loads(payload or "{}") or {}).get("src")
        except Exception:
            src = None
        if src:
            pending[src] = rq_id

    FINAL_ROOTS = ("/vol1/1000/工作文件/", "/vol1/1000/家庭文件/", "/vol1/1000/媒体库/")
    IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".tiff", ".tif", ".bmp"}

    def kept_decodes(path):
        ext = os.path.splitext(path)[1].lower()
        if ext not in IMG_EXTS:
            return True  # non-image: hash equality + existence is the check
        try:
            from PIL import Image
            try:
                import pillow_heif  # noqa
                pillow_heif.register_heif_opener()
            except Exception:
                pass
            with Image.open(path) as im:
                im.verify()
            return True
        except Exception:
            return False

    deleted, skipped = [], []
    for p in plan:
        loser, kept_dst = p["loser"], p["kept_dst"]
        why = None
        if not kept_dst.startswith(FINAL_ROOTS):
            # kept copy still in the inbox (a review file, not yet placed):
            # strict rule -- kept copy must be confirmed at its FINAL location
            why = "kept-not-final-location"
        elif not os.path.exists(loser):
            why = "loser-gone"
        elif not os.path.exists(kept_dst):
            why = "kept-dst-missing"
        else:
            try:
                if sha256(loser) != sha256(kept_dst):
                    why = "sha-mismatch"
                elif not kept_decodes(kept_dst):
                    why = "kept-decode-failed"
            except OSError as e:
                why = "hash-error:%s" % type(e).__name__
        if why:
            skipped.append((loser, kept_dst, why))
            db.execute(
                """INSERT INTO plan_items
                   (plan_id, src_path, dst_path, reason, action, kind, status)
                   VALUES (?,?,?,?,?,?,?)""",
                (plan_id, loser, kept_dst,
                 "side-classify-dedup:skip:%s" % why, "move", "inbox", "skipped"))
            print("SKIP %s (%s)" % (loser, why), flush=True)
            continue
        try:
            os.remove(loser)
        except OSError as e:
            skipped.append((loser, kept_dst, "remove-failed:%s" % type(e).__name__))
            db.execute(
                """INSERT INTO plan_items
                   (plan_id, src_path, dst_path, reason, action, kind, status)
                   VALUES (?,?,?,?,?,?,?)""",
                (plan_id, loser, kept_dst,
                 "side-classify-dedup:remove-failed", "move", "inbox", "failed"))
            print("FAILED-REMOVE %s %s" % (loser, e), flush=True)
            continue
        deleted.append((loser, kept_dst))
        db.execute(
            """INSERT INTO plan_items
               (plan_id, src_path, dst_path, reason, action, kind, status)
               VALUES (?,?,?,?,?,?,?)""",
            (plan_id, loser, kept_dst,
             "side-classify-dedup:deleted, kept copy at dst verified sha-identical",
             "move", "inbox", "done"))
        db.execute(
            "INSERT INTO operations (plan_id, src_path, dst_path) VALUES (?,?,?)",
            (plan_id, loser, kept_dst))
        rq_id = pending.get(loser)
        if rq_id:
            db.execute(
                "UPDATE review_queue SET status='approved', decided_by=?, "
                "decided_at=strftime('%Y-%m-%dT%H:%M:%S','now') "
                "WHERE id=? AND status='pending'",
                ("side-classify-dedup", rq_id))
        print("DELETED %s (kept %s)" % (loser, kept_dst), flush=True)
    db.commit()
    db.execute("UPDATE plans SET status='done' WHERE id=?", (plan_id,))
    db.commit()
    with open("/data/side_delete_list.tsv", "w", encoding="utf-8") as f:
        f.write("deleted_src\tkept_dst\n")
        for loser, kept in deleted:
            f.write("%s\t%s\n" % (loser, kept))
    with open("/data/side_delete_skipped.tsv", "w", encoding="utf-8") as f:
        f.write("src\tkept_dst\treason\n")
        for loser, kept, why in skipped:
            f.write("%s\t%s\t%s\n" % (loser, kept, why))
    print("SUMMARY " + json.dumps({
        "plan_id": plan_id, "backup": bak,
        "deleted": len(deleted), "skipped": len(skipped),
        "integrity": db.execute("PRAGMA integrity_check").fetchone()[0],
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
