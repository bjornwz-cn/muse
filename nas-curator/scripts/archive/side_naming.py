#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Naming pass (stage 2): assign v1 names + final dst to every move candidate.
Inputs (container /data): side_classify_full.jsonl, side_classify_def.jsonl,
  side_classify_salvage.jsonl, side_review_resolved.jsonl, side_classify_new5.jsonl
  (whichever exist; decision==move rows only).
Outputs: /data/side_naming.jsonl + summary.
Rules (v1, 2026-11-24):
 - .eml: keep original name. 1-王震 -> 12-项目资料/06-邮件归档/01-主归档-按年月/<YYYY-MM>/ ;
   2/3/4 -> <party>/<subdir>/.
 - images/videos (work): date = EXIF DateTimeOriginal else mtime; label = text model on
   VLM reason (2-4 chars); new name YYYY-MM-DD_<label>_<seq>.ext; seq per (dst,date,label).
   1-王震 -> root; 2/3/4 -> <party>/<subdir>/.
 - docs (D/E/F): new_name from classify record (validate v1 regex); dst <party>/<subdir>/.
 - media / family records (party startswith 媒体库 / 家庭文件）: keep original name,
   dst from record's dst_dir field.
 - 上海润心 2 记录文件 (05-与库内已有文件重复清单.csv / _删除记录_20261120.txt):
   keep original name, dst 2-上海润心/14-工具与其他/.
 - 周报10 (subdir 12-项目资料 + year field): dst 1-王震/12-项目资料/04-周报/<year>/.
 - collision with existing dst file -> versioned name
   {stem}_{YYYYMMDD-HHMMSS}_{size}_{n}{ext} (time = EXIF else mtime).
Read-only: no moves, no DB writes.
"""
import sys, os, json, re, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")
from PIL import Image
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass
from curator_core.llmkit import LLM

sys.path.insert(0, "/data")
import importlib.util
spec = importlib.util.spec_from_file_location("scf", "/data/side_classify_full.py")
scf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scf)

WORK_ROOT = "/vol1/1000/工作文件"
# media photo categories live under group dirs; video categories are direct under 视频/
MEDIA_PHOTO_GROUP = {
    "人物合影": "家庭与人物", "儿童成长": "家庭与人物", "自拍": "家庭与人物",
    "城市与建筑": "旅行与城市", "旅行风景": "旅行与城市",
    "宠物与动物": "生活记录", "工作与活动": "生活记录", "日常生活": "生活记录",
    "美食与咖啡": "生活记录",
    "屏幕截图": "资料与截图", "文档与票据": "资料与截图", "梗图与表情": "资料与截图",
    "运动与活动": "生活记录", "聚会与夜生活": "生活记录",
}


def media_dst_dir(subdir):
    # subdir like "照片/屏幕截图" or "视频/日常生活" -> real path under 媒体库
    parts = subdir.split("/")
    if len(parts) == 2 and parts[0] == "照片":
        grp = MEDIA_PHOTO_GROUP.get(parts[1])
        if grp:
            return os.path.join(MEDIA_ROOT, "照片", grp, parts[1])
    return os.path.join(MEDIA_ROOT, subdir)

MEDIA_ROOT = "/vol1/1000/媒体库"
FAMILY_ROOT = "/vol1/1000/家庭文件"
IMG_EXTS = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".webp", ".tiff", ".tif", ".nef", ".jfif"}
VID_EXTS = {".mov", ".mp4", ".mpg", ".mpeg", ".m4v", ".avi", ".mkv"}
V1_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_.+\.(pdf|docx?|xlsx?|pptx?|zip|rar|7z|txt|csv|pag|webloc|eml|eddx|osheet|odoc|emmx|dwg)$", re.I)
RECORD_FILES = ["side_classify_full.jsonl", "side_classify_def.jsonl",
                "side_classify_salvage.jsonl", "side_review_resolved.jsonl",
                "side_review_resolved2.jsonl", "side_classify_new5.jsonl"]
LABEL_KW = [("会议", "会议"), ("合影", "合影"), ("产品", "产品"), ("医院", "医院"),
            ("截图", "截图"), ("单据", "单据"), ("培训", "培训"), ("展会", "展会"),
            ("证件", "证件"), ("发票", "发票"), ("名片", "名片"), ("仓库", "仓库"),
            ("手术", "手术"), ("儿童", "儿童"), ("孩子", "儿童"), ("美食", "美食"),
            ("夜景", "夜景"), ("建筑", "建筑"), ("宠物", "宠物"), ("风景", "风景"),
            ("合同", "合同"), ("报表", "报表"), ("资质", "资质"), ("招标", "招投标"),
            ("白板", "白板"), ("投影", "会议"), ("讲座", "讲座"), ("授课", "培训")]

LABEL_PROMPT = """下面是一张图片/视频的内容描述（一句话）。请用2-4个汉字概括图片内容，作为归档文件名中的"内容"部分。
只输出 JSON：{"label":"会议合影"}。不要解释。
标签示例：会议合影、产品展示、家庭聚会、风景、截图、单据、培训现场、医院走廊、合影、美食、证件、发票、名片、展会、仓库、手术、儿童、宠物、建筑、夜景、证件照、随手拍。
描述：%s
文件名：%s
"""


def exif_date(path, mtime):
    try:
        with Image.open(path) as im:
            ex = im.getexif() if hasattr(im, "getexif") else None
            if ex:
                for tag in (36867, 306):
                    v = ex.get(tag)
                    if v:
                        m = re.match(r"(\d{4}):(\d{2}):(\d{2})", str(v))
                        if m:
                            return "%s-%s-%s" % m.groups()
    except Exception:
        pass
    return time.strftime("%Y-%m-%d", time.localtime(mtime))


def fallback_label(reason):
    for kw, lab in LABEL_KW:
        if kw in (reason or ""):
            return lab
    return "随手拍"


def label_one(llm, primary, fallback, reason, filename):
    msg = [{"role": "system", "content": "只输出 JSON，不要解释。"},
           {"role": "user", "content": LABEL_PROMPT % (reason or "", filename)}]
    content, prov, err = scf.call_ark(llm, primary, fallback, msg)
    if err is None and content:
        out = scf.parse_json(content)
        if out and isinstance(out.get("label"), str):
            lab = out["label"].strip()
            if 2 <= len(lab) <= 6 and "/" not in lab:
                return lab
    return fallback_label(reason)


def load_moves():
    moves = []
    for fn in RECORD_FILES:
        p = os.path.join("/data", fn)
        if not os.path.exists(p):
            print("skip missing %s" % fn, flush=True)
            continue
        n = 0
        for line in open(p, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("decision") != "move":
                continue
            r["_source_file"] = fn
            moves.append(r)
            n += 1
        print("loaded %s: %d moves" % (fn, n), flush=True)
    # dedup by src (same file may appear in full + resolved? no; but salvage overlaps full reviews)
    seen = {}
    for r in moves:
        seen[r["src"]] = r
    print("unique moves: %d (raw %d)" % (len(seen), len(moves)), flush=True)
    return list(seen.values())


import threading
_TLS = threading.local()


def thread_llm():
    if getattr(_TLS, "llm", None) is None:
        _TLS.llm = LLM()
        _TLS.providers = scf.pick_providers(_TLS.llm)
    return _TLS.llm, _TLS.providers


def main():
    llm = LLM()
    primary, fallback = scf.pick_providers(llm)
    print("primary: %s | model: %s" % (primary["name"], primary["model"]), flush=True)
    moves = load_moves()
    # label pass for work images/videos (needs model; thread-local LLM like full/def)
    need_label = [r for r in moves
                  if r.get("ext", "").lower() in IMG_EXTS | VID_EXTS
                  and not str(r.get("party", "")).startswith(("媒体库", "家庭文件"))]
    print("need label: %d" % len(need_label), flush=True)

    def do_label(r):
        tllm, (tprimary, tfallback) = thread_llm()
        r["_label"] = label_one(tllm, tprimary, tfallback, r.get("reason", ""), r.get("filename", ""))
        return r

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(do_label, need_label))
    print("labels done", flush=True)

    # assign dst + names
    out = []
    seq_counter = {}
    for r in moves:
        src = r["src"]
        ext = r.get("ext", "").lower()
        party = r.get("party", "")
        subdir = r.get("subdir", "")
        fname = r.get("filename", os.path.basename(src))
        mtime = r.get("mtime", 0) or int(os.path.getmtime(src)) if os.path.exists(src) else 0
        size = r.get("size", 0)
        rec = {"src": src, "party": party, "subdir": subdir, "filename": fname,
               "ext": ext, "size": size, "mtime": mtime,
               "method": r.get("method", ""), "confidence": r.get("confidence"),
               "source_file": r.get("_source_file", ""),
               "src_kind": r.get("src_kind", "")}
        # media / family: keep name, dst from record
        if party.startswith("媒体库"):
            dst_dir = media_dst_dir(subdir) if subdir else MEDIA_ROOT
            rec.update({"dst_dir": dst_dir, "new_name": fname, "keep_name": True})
        elif party.startswith("家庭文件"):
            dst_dir = os.path.join(FAMILY_ROOT, subdir) if subdir else FAMILY_ROOT
            rec.update({"dst_dir": dst_dir, "new_name": fname, "keep_name": True})
        elif fname in ("05-与库内已有文件重复清单.csv", "_删除记录_20261120.txt"):
            rec.update({"dst_dir": os.path.join(WORK_ROOT, "2-上海润心", "14-工具与其他"),
                        "new_name": fname, "keep_name": True})
        elif ext == ".eml":
            if party == "1-王震":
                m = re.match(r"(\d{4})(\d{2})\d{2}_", fname)
                ym = "%s-%s" % (m.group(1), m.group(2)) if m else time.strftime("%Y-%m", time.localtime(mtime))
                dst_dir = os.path.join(WORK_ROOT, "1-王震", "12-项目资料", "06-邮件归档",
                                       "01-主归档-按年月", ym)
            else:
                dst_dir = os.path.join(WORK_ROOT, party, subdir) if subdir else os.path.join(WORK_ROOT, party)
            rec.update({"dst_dir": dst_dir, "new_name": fname, "keep_name": True})
        elif ext in IMG_EXTS | VID_EXTS:
            date = exif_date(src, mtime) if ext in IMG_EXTS else time.strftime("%Y-%m-%d", time.localtime(mtime))
            label = r.get("_label") or fallback_label(r.get("reason", ""))
            if party == "1-王震":
                dst_dir = os.path.join(WORK_ROOT, "1-王震")
            else:
                dst_dir = os.path.join(WORK_ROOT, party, subdir) if subdir else os.path.join(WORK_ROOT, party)
            key = (dst_dir, date, label)
            seq_counter[key] = seq_counter.get(key, 0) + 1
            seq = seq_counter[key]
            new_name = "%s_%s_%02d%s" % (date, label, seq, ext)
            rec.update({"dst_dir": dst_dir, "new_name": new_name, "keep_name": False,
                        "date": date, "label": label, "seq": seq})
        else:
            # docs: use model new_name if valid v1; else generate one
            # (resolved records carry no new_name) from mtime/doc_date + party + stem
            nn = r.get("new_name") or ""
            if party == "1-王震" and subdir == "12-项目资料" and r.get("year"):
                dst_dir = os.path.join(WORK_ROOT, "1-王震", "12-项目资料", "04-周报", str(r["year"]))
            elif subdir:
                dst_dir = os.path.join(WORK_ROOT, party, subdir)
            else:
                dst_dir = os.path.join(WORK_ROOT, party)
            if nn and V1_RE.match(nn):
                rec.update({"dst_dir": dst_dir, "new_name": nn, "keep_name": False})
            else:
                date = r.get("doc_date") or ""
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", date or ""):
                    date = time.strftime("%Y-%m-%d", time.localtime(mtime)) if mtime else ""
                entity = {"1-王震": "王震", "2-上海润心": "上海润心",
                          "3-北京供应链": "北京供应链", "4-辽宁器械": "辽宁器械"}.get(party, "文件")
                stem = os.path.splitext(fname)[0]
                ver = ""
                mver = re.search(r"(__v\d+)$", stem)
                if mver:
                    ver = mver.group(1)
                    stem = stem[: -len(ver)]
                subject = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", stem).strip()[:12] or "文件"
                gen = "%s_%s_%s%s%s" % (date, entity, subject, ver, ext)
                if V1_RE.match(gen):
                    rec.update({"dst_dir": dst_dir, "new_name": gen, "keep_name": False,
                                "name_flag": "generated_v1"})
                else:
                    rec.update({"dst_dir": dst_dir, "new_name": fname, "keep_name": True,
                                "name_flag": "invalid_v1_kept_original"})
        # collision with existing dst file -> versioned name
        dst_full = os.path.join(rec["dst_dir"], rec["new_name"])
        if os.path.exists(dst_full):
            stem, e = os.path.splitext(rec["new_name"])
            tstr = time.strftime("%Y%m%d-%H%M%S", time.localtime(mtime))
            n = 1
            while True:
                cand = "%s_%s_%d_%d%s" % (stem, tstr, size, n, e)
                if not os.path.exists(os.path.join(rec["dst_dir"], cand)):
                    break
                n += 1
            rec["new_name"] = cand
            rec["name_flag"] = "collision_versioned"
            dst_full = os.path.join(rec["dst_dir"], cand)
        rec["dst_full"] = dst_full
        out.append(rec)
    with open("/data/side_naming.jsonl", "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    from collections import Counter
    print("SUMMARY " + json.dumps({
        "n": len(out),
        "by_party": dict(Counter(r["party"] for r in out)),
        "keep_name": sum(1 for r in out if r.get("keep_name")),
        "renamed": sum(1 for r in out if not r.get("keep_name")),
        "collision_versioned": sum(1 for r in out if r.get("name_flag") == "collision_versioned"),
        "invalid_v1": sum(1 for r in out if r.get("name_flag") == "invalid_v1_kept_original"),
        "dst_dirs": len(set(r["dst_dir"] for r in out)),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
