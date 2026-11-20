#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NAS Curator（文件管家）统一 CLI —— P1 引擎合并。

子命令（参数与原 workfiles-curator 兼容）：
  inventory / learn / plan / apply / rollback / review / analyze / explain
  inbox [--watch] / process <文件或目录> [--apply]

入口（2026-11-19 王震定：一个统一收件箱 + 任意选择处理，不设三个收件区）：
  统一收件箱 /vol1/1000/收件箱（唯一投递入口）；任意文件/目录可选中处理；
  输入源 下载/Wangzhen-Mac/（笔记本备份，只上传）可整目录处理或定时扫描。
  原库内收件箱（工作文件/收件箱、媒体库/_收件箱）不再作为入口，保留目录不动。

plan 同时写：plans + plan_items 表（SQLite）、plan CSV（兼容原版）、
review_queue 表（action=review 的项，不再只落 99-待确认文件夹；文件夹落盘保留做兼容）。

硬规则：muse/ 与 AI模型/ 在排除列表内（见 common.guard），其余 --root 通用；
plan/apply 逐条过滤排除路径下的 src。
凭据：火山 key 只走环境变量（ARK_PLAN_KEY / ARK_PAYG_KEY）或
CURATOR_LLM_CONFIG 指定的配置文件；本文件绝不读、不写任何凭据。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from common import db as DB  # noqa: E402
from common import guard  # noqa: E402

CONFIG_DIR = os.path.join(_HERE, "config")
LIBRARIES_JSON = os.path.join(CONFIG_DIR, "libraries.json")
DEFAULT_RULES = os.path.join(_HERE, "curator_core", "rules.baijing-supply-v8.json")
DEFAULT_FAMILY_RULES = os.path.join(_HERE, "curator_core", "rules.family.json")

# libraries.json 里 rules 字段 → 规则文件（P4：此前 _load_plan_rules 一律用
# 工作库规则，--library family 的计划也会按 v8 工作类目分类，是错的）
RULES_BY_NAME = {
    "baijing-supply-v8": DEFAULT_RULES,
    "family-default": DEFAULT_FAMILY_RULES,
}
DEFAULT_MEDIA_CFG = os.path.join(_HERE, "media_core", "config.json")

# 原版 plan CSV 列（兼容 apply 读取：src / category / target_name）。
PLAN_CSV_FIELDS = ["src", "size", "ext", "category", "score", "reason",
                   "text_len", "evidence", "unit", "target_name",
                   "name_reason", "confidence", "files", "action",
                   "review_kind", "sha256", "tags", "dst"]


def _load_libraries():
    try:
        with open(LIBRARIES_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"libraries": [], "thresholds": {}}


def _library_root(cfg, lib_id, default=""):
    for lib in cfg.get("libraries", []):
        if lib.get("id") == lib_id:
            return lib.get("root", default)
    return default


def _resolve_library(args):
    """--library 工作/家庭/媒体/笔记本备份，或按 --root 匹配；返回 (entry|None, kind)。

    kind：document（文档库）、media（媒体库）、mixed（混合输入源：
    统一收件箱 / 笔记本备份，文档走文档引擎、照片视频走媒体引擎，
    归位 dst 分别落工作库 / 媒体库）。
    """
    cfg = _load_libraries()
    libs = cfg.get("libraries", [])
    if getattr(args, "library", None):
        for lib in libs:
            if args.library in (lib.get("id"), lib.get("name")):
                return lib, lib.get("kind", "document")
        for src in cfg.get("input_sources", []):
            if args.library in (src.get("id"), src.get("name")):
                return src, src.get("kind", "mixed")
        raise SystemExit(f"未知库：{args.library}（可选：{', '.join(l['id'] for l in libs)}）")
    root = os.path.abspath(getattr(args, "root", "") or "")
    for lib in libs:
        if root == lib["root"] or root.startswith(lib["root"] + os.sep):
            return lib, lib.get("kind", "document")
    for src in cfg.get("input_sources", []):
        if root == src["root"] or root.startswith(src["root"] + os.sep):
            return src, src.get("kind", "mixed")
    inbox = (cfg.get("inbox") or {}).get("path", "")
    if inbox and (root == inbox or root.startswith(inbox + os.sep)):
        return {"id": "inbox", "name": "统一收件箱", "kind": "mixed",
                "root": inbox}, "mixed"
    return None, getattr(args, "kind", "document")


def _setup_llm(args):
    """只建 LLM（轻量：读模型列表文件 + 探活）。

    收件箱监听每轮重建一次，模型列表/Key 改动无需重启容器即可生效（0.1.2）。
    """
    if not getattr(args, "llm", False):
        return None
    from curator_core import LLM
    llm = LLM(args.llm_host, args.llm_model)
    if not llm.alive():
        print("! LLM 不可达，自动降级为规则判定")
        return None
    return llm


def _setup_sem(args, rules):
    """建语义分类器 + 预筛（fit 较重，进程内只建一次）。"""
    if not getattr(args, "sem", False):
        return None, None
    from curator_core import Prescreener, SemanticClassifier
    sem = SemanticClassifier(
        os.environ.get("SEM_HOST", "http://172.17.0.1:11500"),
        getattr(args, "sem_model", "bge-m3:latest"),
        min_sim=getattr(args, "sem_min_sim", 0.42),
        min_margin=getattr(args, "sem_margin", 0.02),
        self_names=(rules.get("self_names") or ()) if rules else ())
    if not sem.alive():
        print("！语义模型不可用（bge-m3），跳过向量分类")
        return None, None
    n = sem.fit(rules)
    print(f"语义空间就绪：{n} 个类目向量")
    prescreener = None
    if rules is not None:
        try:
            prescreener = Prescreener(sem=sem)
        except TypeError:
            prescreener = Prescreener()
        prescreener.fit(rules)
        print("预筛层就绪（bge-m3，sim≥0.75 且 margin≥0.10 直接定类）")
    return sem, prescreener


def _setup_models(args, rules):
    """按 --llm/--sem 装配 LLM / 语义 / 预筛；不可达时降级，不抛错。"""
    if not (getattr(args, "llm", False) or getattr(args, "sem", False)):
        return None, None, None
    llm = _setup_llm(args)
    sem, prescreener = _setup_sem(args, rules)
    return llm, sem, prescreener


def _apply_path_guard(items, cfg):
    """0.1.2 路径强信号：低置信（< auto 阈值）建议不得跨顶层移动。

    在 _dispatch_plan 的三个返回点统一跑（文档/媒体/混合输入源全覆盖）。
    只改建议，不动文件；被拦下的项数打印一行。
    """
    from common import routing as RT
    roots = RT.roots_from_config(cfg)
    threshold = ((cfg or {}).get("thresholds") or {}).get("auto", 0.85)
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = 0.85
    items, info = RT.apply_path_prior_guard(items, roots, threshold)
    if info.get("guarded"):
        print(f"  路径先验：{info['guarded']} 项低置信跨顶层建议已转复核")
    return items


def _dispatch_plan(root, kind, rules, llm, sem, prescreener, args, cfg):
    """按 kind 把 root 交给对应引擎，返回 (items, stats)。

    document：文档引擎，dst 落在 root 内（库内整理）。
    media：媒体引擎，dst 落在 root 内。
    mixed（统一收件箱 / 笔记本备份等输入源）：文档走文档引擎，dst_root=工作库；
    照片视频走媒体引擎，dst=媒体库；文档引擎产出的 kind=media 项丢弃（媒体引擎覆盖）。
    已知局限（P1）：文档默认归工作库；家庭 vs 工作的分流需家庭规则，P2 再做。
    """
    if kind == "media":
        from media_core import engine as ME
        classifier, mcfg = _build_media_classifier(args)
        mcfg.setdefault("library_root", root)
        paths = _collect_media_paths(root)
        items, stats = ME.plan_media_files(paths, mcfg, classifier=classifier,
                                           library_root=root)
        return _apply_path_guard(items, cfg), stats
    from curator_core import engine as CE
    if rules is None:
        raise SystemExit("文档 plan 需要 --rules（规则文件）")
    dst_root = None
    if kind == "mixed":
        dst_root = _library_root(cfg, "work", "")
    items, stats = CE.plan_files(
        root, rules, llm=llm, sem=sem, prescreener=prescreener,
        rename=args.rename, rename_llm=args.rename_llm,
        unit_mode=args.unit_mode, protect=tuple(x for x in (args.protect or "").split(",") if x),
        workers=args.workers, cache=args.cache or None, min_score=args.min_score,
        content_first=args.content_first, ocr_url=args.ocr_url,
        no_ocr=args.no_ocr, max_chars=args.max_chars,
        ocr_timeout=args.ocr_timeout, llm_min_conf=args.llm_min_conf,
        llm_max=args.llm_max, dst_root=dst_root or None,
        deep=getattr(args, "deep", False))
    if kind == "mixed":
        from media_core import engine as ME
        # P2(P1.1)：家庭 vs 工作分流（文档项按启发式改写 dst 到家庭库/人名）。
        items = _route_family_items(items, cfg)
        media_root = _library_root(cfg, "media", "")
        kept = [i for i in items if i.get("kind") != "media"]
        dropped = len(items) - len(kept)
        classifier, mcfg = _build_media_classifier(args)
        mpaths = _collect_media_paths(root)
        mitems, mstats = ME.plan_media_files(mpaths, mcfg, classifier=classifier,
                                             library_root=media_root)
        stats = dict(stats)
        stats["media"] = mstats
        if dropped:
            stats["media_doc_dropped"] = dropped
        return _apply_path_guard(kept + mitems, cfg), stats
    return _apply_path_guard(items, cfg), stats


def _route_family_items(items, cfg):
    """P2(P1.1)：mixed 输入源的文档项做家庭分流。

    命中家庭（人名/家庭关键词）的 move/rename 项，把 dst 从工作库改写到
    家庭库；review 项 dst 为空，不改；unit 整目录项按目录名同样路由。
    返回改写后的 items（原地改 dst，并打 library/tags 标记）。

    0.1.2（人优先 + 路径强信号）：
    - 分流判断除文件名/正文/标签外，把源文件路径也放进 haystack
      （如 下载/Wangzhen-Mac/刘音/会议/x.jpg 里的「刘音」就是强信号）；
    - 家庭成员的工作内容（工作库类目 01-14）进
      家庭/<成员>/工作/<内容子目录>/（会议/票据等）；家庭个人内容
      （15-19 类目）仍走 家庭/<成员>/<类目>/，不加「工作」层。
    """
    import os
    from common import routing as RT
    family_root = _library_root(cfg, "family", "")
    if not family_root:
        return items
    n_family = 0
    for it in items:
        if it.get("kind") == "media":
            continue
        if it.get("action") not in ("move", "rename"):
            continue
        name = os.path.basename(it.get("src") or "")
        lib, person = RT.route_document(
            name=name,
            text=((it.get("src") or "") + " " + (it.get("evidence") or "")),
            tags=it.get("tags") or [])
        if lib != "family":
            it["library"] = "work"
            continue
        it["library"] = "family"
        it["family_person"] = person
        tags = list(it.get("tags") or [])
        if "家庭" not in tags:
            tags.append("家庭")
        it["tags"] = tags
        dst = it.get("dst") or ""
        if dst:
            target = it.get("new_name") or name
            if RT.is_work_category(it.get("category") or ""):
                it["dst"] = RT.rewrite_family_work_dst(
                    family_root, person, target, it.get("category") or "")
            else:
                it["dst"] = RT.rewrite_family_dst(
                    dst, family_root, person, target_name=target)
        n_family += 1
    if n_family:
        print(f"  家庭分流：{n_family} 项归家庭库")
    return items


def _build_media_classifier(args):
    """P2(P1.1)：给媒体分支建 Classifier；模型不可用时返回 (None, mcfg) 并告警，
    plan 仍可跑（只做去重/解码/模糊检测，分类进 review），不阻塞流程。"""
    from media_core import engine as ME
    mcfg = {}
    try:
        if getattr(args, "media_config", ""):
            mcfg = _load_rules(args.media_config)
        elif os.path.exists(DEFAULT_MEDIA_CFG):
            mcfg = _load_rules(DEFAULT_MEDIA_CFG)
    except (OSError, ValueError):
        mcfg = {}
    try:
        return ME.build_classifier(mcfg), mcfg
    except Exception as e:  # noqa: BLE001 - 模型缺失/损坏时降级，不阻塞 plan
        print(f"! 媒体模型不可用（{e}），本次只做去重与解码检测，分类进复核")
        return None, mcfg


def _load_rules(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_plan_rules(args):
    """--rules 未给时按库选规则：work→baijing-supply-v8，family→family-default；
    库外/未匹配时默认工作库规则；都没有则返回 None。"""
    if getattr(args, "rules", ""):
        return _load_rules(args.rules)
    name = None
    try:
        lib, _kind = _resolve_library(args)
        name = (lib or {}).get("rules")
    except SystemExit:
        name = None
    path = RULES_BY_NAME.get(name) if name else None
    if path and os.path.exists(path):
        return _load_rules(path)
    if os.path.exists(DEFAULT_RULES):
        return _load_rules(DEFAULT_RULES)
    return None


# ---------------------------------------------------------------- plan

def cmd_plan(args):
    guard.assert_allowed(args.root, "整理根目录")
    conn = DB.connect(args.db)
    DB.init_db(args.db)
    cfg = _load_libraries()
    lib, kind = _resolve_library(args)
    library_id = lib["id"] if lib else "custom"
    rules = _load_plan_rules(args)
    llm, sem, prescreener = _setup_models(args, rules)
    items, stats = _dispatch_plan(args.root, kind, rules, llm, sem, prescreener,
                                  args, cfg)
    # 排除过滤：逐条 src 再过一次 guard（纵深防御）
    kept = []
    for it in items:
        bad, _ = guard.is_excluded(it.get("src") or "")
        if bad:
            print(f"! 跳过排除路径：{it.get('src')}")
            continue
        kept.append(it)
    items = kept
    rules_version = rules.get("version") if isinstance(rules, dict) else None
    plan_id = _store_plan(conn, library_id, rules_version, items)
    DB.set_plan_status(conn, plan_id, "pending_review")
    csv_path = args.out or os.path.join(_HERE, "data", f"plan_{plan_id}.csv")
    _write_plan_csv(items, csv_path)
    n_review = sum(1 for i in items if i.get("action") == "review")
    n_move = sum(1 for i in items if i.get("action") in ("move", "rename"))
    print(f"计划 #{plan_id}（库：{library_id}）：{len(items)} 项，"
          f"可执行 {n_move}，进复核 {n_review}，跳过 {len(items) - n_move - n_review}")
    print(f"  CSV：{csv_path}")
    if stats.get("llm_calls") is not None:
        print(f"  LLM 调用：{stats['llm_calls']} 次"
              + (f"（预筛命中 {stats.get('prescreen_hits', 0)} 次）" if "prescreen_hits" in stats else ""))
    return 0


def _write_plan_csv(items, out_path):
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PLAN_CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for it in items:
            row = {k: it.get(k, "") for k in PLAN_CSV_FIELDS}
            # 统一 plan-item 用 new_name；CSV 兼容列 target_name 取 new_name
            row["target_name"] = it.get("target_name") or it.get("new_name") or ""
            row["tags"] = ",".join(it.get("tags") or [])
            w.writerow(row)
    return out_path


def _store_plan(conn, library_id, rules_version, items):
    """写 plans + plan_items + review_queue。返回 plan_id。"""
    summary = {"n": len(items),
               "move": sum(1 for i in items if i.get("action") == "move"),
               "rename": sum(1 for i in items if i.get("action") == "rename"),
               "review": sum(1 for i in items if i.get("action") == "review"),
               "skip": sum(1 for i in items if i.get("action") == "skip")}
    plan_id = DB.create_plan(conn, library_id, rules_version, summary)
    for it in items:
        DB.add_plan_item(conn, plan_id, it)
        if it.get("action") == "review":
            DB.add_review(conn, file_id=it.get("file_id"), plan_id=plan_id,
                          kind=it.get("review_kind") or "low_conf",
                          payload={"src": it.get("src"), "category": it.get("category"),
                                   "confidence": it.get("confidence"),
                                   "reason": it.get("reason"),
                                   "evidence": it.get("evidence"),
                                   "suggested_dst": it.get("dst"),
                                   "tags": it.get("tags") or []})
    return plan_id


# ---------------------------------------------------------------- inventory

def cmd_inventory(args):
    guard.assert_allowed(args.root, "盘点根目录")
    from curator_core import engine as CE
    res = CE.inventory(args.root)
    out = {"root": args.root, **res}
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"文件 {res.get('files')} / 目录 {res.get('dirs')} / "
          f"总体积 {res.get('total_gb', 0):.2f} GB")
    print(f"重复组 {len(res.get('dup_groups', []))}，"
          f"可回收约 {res.get('dup_gb', 0):.2f} GB")
    return 0


# ---------------------------------------------------------------- learn

def cmd_learn(args):
    guard.assert_allowed(args.root, "学习根目录")
    from curator_core import engine as CE
    rules = CE.learn_rules(args.root, per_cat=args.per_cat, topk=args.topk)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=1)
    print(f"类目 {len(rules.get('categories', []))} 个 → {args.out}")
    return 0


def _collect_media_paths(root):
    """收集 root 下的照片/视频文件，跳过 _Review（已复核区）与隐藏目录。

    P2(P1.1)：跳过 node_modules 目录与 TypeScript 声明文件 .d.mts
    （.mts 扩展名与 AVCHD 视频碰撞，146 个 node_modules 文件曾被误当视频抽帧）。
    """
    from media_core import MEDIA_EXTS
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns
                  if d not in ("_Review", "node_modules") and not d.startswith(".")]
        for fn in fns:
            if fn.lower().endswith(".d.mts"):
                continue
            if os.path.splitext(fn)[1].lower() in MEDIA_EXTS:
                out.append(os.path.join(dp, fn))
    return sorted(out)


# ---------------------------------------------------------------- apply

def cmd_apply(args):
    conn = DB.connect(args.db)
    DB.init_db(args.db)
    if args.plan_id:
        plan = DB.get_plan(conn, args.plan_id)
        if not plan:
            raise SystemExit(f"计划 #{args.plan_id} 不存在")
        items = DB.list_plan_items(conn, args.plan_id)
        root = args.root or _common_root([i["src_path"] for i in items])
        # DB 行转统一 plan-item dict
        items = [{**i, "src": i["src_path"], "dst": i["dst_path"],
                  "tags": json.loads(i["tags"]) if i.get("tags") else []}
                 for i in items]
        items = [i for i in items if i.get("action") in ("move", "rename")]
    else:
        items = []
        with open(args.plan, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                r.setdefault("dst", "")
                r.setdefault("tags", [])
                items.append(r)
        root = args.root or _common_root([r.get("src", "") for r in items if r.get("src")])
        plan = None
    guard.assert_allowed(root, "执行根目录")
    items = [i for i in items if not guard.is_excluded(i.get("src") or "")[0]]
    # 移动 mechanics 文档/媒体相同，统一走文档引擎的 apply_plan（shutil.move + 同名 __n + chown）。
    from curator_core import engine as CE
    operations, report = CE.apply_plan(items, root, dry_run=args.dry_run,
                                       uid=args.uid, gid=args.gid)
    if not args.dry_run:
        plan_id = plan["id"] if plan else None
        if plan_id is None:
            plan_id = DB.create_plan(conn, "custom", None, {"csv": args.plan})
        for op in operations:
            DB.add_operation(conn, plan_id, op["src_path"], op["dst_path"],
                             op.get("sha256"))
        if args.map:
            _write_map_tsv(operations, args.map)
            from common import rollback as RB
            rb = os.path.splitext(args.map)[0] + "_rollback.sh"
            RB.build_rollback_script(operations, rb)
            print(f"  映射表：{args.map}\n  回滚脚本：{rb}")
        DB.set_plan_status(conn, plan_id, "done", report)
        if args.report:
            _write_apply_report(args.report, root, report, args.map)
    print(f"{'[预演] ' if args.dry_run else ''}移动 {report.get('moved', 0)} 项，"
          f"跳过/失败 {len(report.get('failed', []))} 项")
    return 0


def _common_root(paths):
    paths = [p for p in paths if p]
    if not paths:
        return "."
    try:
        base = os.path.commonpath(paths)
    except ValueError:
        return "."
    return base if os.path.isdir(base) else os.path.dirname(base)


def _is_media_items(items):
    kinds = {i.get("kind") for i in items}
    return kinds == {"media"}


def _write_map_tsv(operations, map_path):
    os.makedirs(os.path.dirname(os.path.abspath(map_path)), exist_ok=True)
    import datetime
    with open(map_path, "w", encoding="utf-8") as f:
        for op in operations:
            f.write("\t".join([op["src_path"], op["dst_path"],
                               op.get("sha256") or "",
                               datetime.datetime.now().isoformat(timespec="seconds")]) + "\n")


def _write_apply_report(report_path, root, report, map_path):
    os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
    import datetime
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("# 整理执行报告\n\n")
        f.write(f"- 时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write(f"- 根目录：`{root}`\n- 成功移动：{report.get('moved', 0)}\n"
                f"- 失败/跳过：{len(report.get('failed', []))}\n\n")
        for k, v in (report.get("per_cat") or {}).items():
            f.write(f"- {k}：{v}\n")
        if report.get("failed"):
            f.write("\n## 跳过/失败\n\n")
            for p, why in report["failed"][:200]:
                f.write(f"- `{p}` → {why}\n")
        if map_path:
            f.write(f"\n## 回溯\n\n- 映射表：`{os.path.abspath(map_path)}`\n")


# ---------------------------------------------------------------- rollback

def cmd_rollback(args):
    from common import rollback as RB
    if getattr(args, "plan_id", None):
        conn = DB.connect(args.db)
        DB.init_db(args.db)
        res = RB.rollback_from_operations(conn, args.plan_id, dry_run=args.dry_run)
        DB.set_plan_status(conn, args.plan_id, "rolled_back")
        print(f"{'[预演] ' if args.dry_run else ''}回滚 {res['moved']} 项，"
              f"缺失 {len(res['missing'])} 项，失败 {len(res['failed'])} 项")
    else:
        res = RB.rollback_from_map(args.map, dry_run=args.dry_run)
        print(f"{'[预演] ' if args.dry_run else ''}回滚 {res['moved']} 项，"
              f"缺失 {len(res['missing'])} 项，失败 {len(res['failed'])} 项")
    return 0


# ---------------------------------------------------------------- review

def cmd_review(args):
    conn = DB.connect(args.db)
    DB.init_db(args.db)
    if args.action == "list":
        rows = DB.list_review(conn, status=args.status or "pending", kind=args.kind)
        print(f"复核队列（{args.status or 'pending'}）：{len(rows)} 条")
        for r in rows[: args.limit]:
            p = r.get("payload") or {}
            print(f"  #{r['id']} [{r['kind']}] {p.get('src', '')} "
                  f"→ 建议 {p.get('category', '')}（conf {p.get('confidence', '')}，{p.get('reason', '')}）")
        return 0
    if args.action == "decide":
        if not args.review_id or not args.decision:
            raise SystemExit("decide 需要 --review-id 与 --decision approved|rejected")
        DB.decide_review(conn, args.review_id, args.decision,
                         decided_by=args.by or "user")
        print(f"复核 #{args.review_id} → {args.decision}")
        return 0
    raise SystemExit("review 子命令：list | decide")


# ---------------------------------------------------------------- analyze / explain

def cmd_analyze(args):
    guard.assert_allowed(args.root, "分析根目录")
    from curator_core import engine as CE
    res = CE.inventory(args.root)
    print(json.dumps(res, ensure_ascii=False, indent=1)[:4000])
    return 0


def cmd_explain(args):
    from curator_core import engine as CE
    rules = _load_rules(args.rules) if args.rules else None
    res = CE.explain(args.path, rules)
    print(json.dumps(res, ensure_ascii=False, indent=1)[:4000])
    return 0


# ---------------------------------------------------------------- inbox / process

def _settle_filter(items, settle_minutes):
    """只留「稳定」文件：mtime 早于 now - settle 分钟；其余计入 skipped。"""
    if not settle_minutes:
        return items, 0
    cutoff = time.time() - settle_minutes * 60
    kept, skipped = [], 0
    for it in items:
        try:
            mtime = os.path.getmtime(it.get("src") or "")
        except OSError:
            skipped += 1
            continue
        if mtime > cutoff:
            skipped += 1
            continue
        kept.append(it)
    return kept, skipped


def _plan_single_file(path, args, cfg, db_conn=None):
    """单个文件做全量规划（process 用）。返回 ([plan-item], stats)。

    文档：正文抽取 → 敏感隔离 → SHA256 跨轮去重（db_conn 传入时）→ 规则 best_category；
    媒体：走媒体引擎。P1 单文件只做规则层（不走 sem/LLM/预筛），改名逻辑亦不做
    （target_name 即原名）；批量 plan 才走完整判定链。
    """
    from media_core import MEDIA_EXTS
    ext = os.path.splitext(path)[1].lower()
    if ext in MEDIA_EXTS:
        from media_core import engine as ME
        media_root = _library_root(cfg, "media", "")
        classifier, mcfg = _build_media_classifier(args)
        return ME.plan_media_files([path], mcfg, classifier=classifier,
                                   library_root=media_root)
    from curator_core import engine as CE
    from curator_core import extract_text as _extract_text
    rules = _load_plan_rules(args)
    if rules is None:
        raise SystemExit("文档处理需要 --rules（规则文件）")
    work_root = _library_root(cfg, "work", "")
    cats = rules.get("categories", [])
    low_cat = rules.get("low_conf_category", "99-待确认/04-低置信")
    default_cat = rules.get("default", "06-其他")
    shell_cat = rules.get("shell_category", "99-待确认/空壳")
    sensitive_cat = rules.get("sensitive_category", "99-待确认/05-敏感隔离")
    dup_cat = rules.get("dup_category", "99-待确认/06-重复待确认")
    st = os.stat(path)
    name = os.path.basename(path)
    body = ""
    if st.st_size > 0:
        body, _src = _extract_text(path, ext, args.max_chars, args.ocr_url,
                                   allow_ocr=not args.no_ocr)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    sha = h.hexdigest() if st.st_size > 0 else ""
    row = {"src": path, "size": st.st_size, "ext": ext, "unit": "",
           "target_name": name, "name_reason": "", "files": 1, "sha256": sha,
           "text_len": len(body), "evidence": "", "confidence": ""}
    if st.st_size == 0:
        row.update(category=shell_cat, score=0, reason="shell")
    else:
        sens = None
        hit = getattr(CE, "_sensitive_hit", None)
        if callable(hit):
            sens = hit(name, body)
        if sens:
            row.update(category=sensitive_cat, score=0, reason="sensitive",
                       evidence="敏感词:" + "、".join(sens), confidence="0.00")
        elif sha and db_conn is not None:
            firsts = [r for r in DB.get_file_by_sha(db_conn, sha)
                      if r.get("path") != path]
            if firsts:
                row.update(category=dup_cat, score=0, reason="dup:sha256",
                           evidence=firsts[0].get("path") or "", confidence="0.90")
            else:
                cat, sc, reason, ev = CE.best_category(
                    name, body, ext, cats, args.min_score, 3,
                    default_cat, low_cat, name_first=True)
                row.update(category=cat, score=sc, reason=reason, evidence=ev or "")
        else:
            cat, sc, reason, ev = CE.best_category(
                name, body, ext, cats, args.min_score, 3,
                default_cat, low_cat, name_first=True)
            row.update(category=cat, score=sc, reason=reason, evidence=ev or "")
    item = CE._to_plan_item(row, os.path.dirname(path), args.min_score, False,
                            {path: body}, {path: st.st_mtime}, dst_root=work_root)
    # P2(P1.1)：单文件也做家庭分流
    if item.get("action") in ("move", "rename"):
        from common import routing as RT
        lib, person = RT.route_document(name=name, text=body,
                                        tags=item.get("tags") or [])
        if lib == "family":
            family_root = _library_root(cfg, "family", "")
            if family_root and item.get("dst"):
                item["dst"] = RT.rewrite_family_dst(
                    item["dst"], family_root, person,
                    target_name=item.get("new_name") or name)
                item["library"] = "family"
                item["family_person"] = person
    return [item], {"llm_calls": 0}


def _apply_items(items, root, args, conn, plan_id):
    """执行 move/rename 项（process/inbox --apply 用）：写 operations、映射表、
    回滚脚本、报告三件套。默认不删除、不覆盖（同名 __n，见 apply_plan）。

    自动执行门控（P3，与 Web API 的 auto_apply 子集口径一致）：只放行
    confidence >= auto 阈值（默认 0.85，见 libraries.json thresholds.auto）
    或 unit 行 auto_exempt 的项；被拦下的 move/rename 项不移动，改为写入
    review_queue（kind=low_conf），出现在 Web 复核队列里等人工点。
    （背景：媒体引擎 score>=0.35 即出 move 项，低于统一自动执行阈值，
    不门控会把 0.40 置信的文件自动归位。）
    """
    cfg = _load_libraries()
    threshold = float((cfg.get("thresholds") or {}).get("auto", 0.85))
    todo = [i for i in items if i.get("action") in ("move", "rename")]
    todo = [i for i in todo if not guard.is_excluded(i.get("src") or "")[0]]
    gated, held = [], []
    for i in todo:
        conf = i.get("confidence")
        conf = float(conf) if conf is not None else 0.0
        if conf >= threshold or i.get("auto_exempt"):
            gated.append(i)
        else:
            held.append(i)
    if held:
        print(f"  自动执行门控：{len(held)} 项置信度 < {threshold}，不自动移动，进复核队列")
        for i in held:
            DB.add_review(conn, file_id=i.get("file_id"), plan_id=plan_id,
                          kind="low_conf",
                          payload={"src": i.get("src"), "category": i.get("category"),
                                   "confidence": i.get("confidence"),
                                   "reason": i.get("reason"),
                                   "evidence": i.get("evidence"),
                                   "suggested_dst": i.get("dst"),
                                   "tags": i.get("tags") or []})
    todo = gated
    from curator_core import engine as CE
    operations, report = CE.apply_plan(todo, root, dry_run=args.dry_run,
                                       uid=args.uid, gid=args.gid)
    if not args.dry_run:
        for op in operations:
            DB.add_operation(conn, plan_id, op["src_path"], op["dst_path"],
                             op.get("sha256"))
        if args.map:
            _write_map_tsv(operations, args.map)
            from common import rollback as RB
            rb = os.path.splitext(args.map)[0] + "_rollback.sh"
            RB.build_rollback_script(operations, rb)
            print(f"  映射表：{args.map}\n  回滚脚本：{rb}")
        DB.set_plan_status(conn, plan_id, "done", report)
        if args.report:
            _write_apply_report(args.report, root, report, args.map)
    print(f"{'[预演] ' if args.dry_run else ''}移动 {report.get('moved', 0)} 项，"
          f"跳过/失败 {len(report.get('failed', []))} 项")
    return report


def _print_plan_summary(plan_id, library_id, items, csv_path, stats):
    n_review = sum(1 for i in items if i.get("action") == "review")
    n_move = sum(1 for i in items if i.get("action") in ("move", "rename"))
    print(f"计划 #{plan_id}（库：{library_id}）：{len(items)} 项，"
          f"可执行 {n_move}，进复核 {n_review}，跳过 {len(items) - n_move - n_review}")
    print(f"  CSV：{csv_path}")
    if stats.get("llm_calls") is not None:
        print(f"  LLM 调用：{stats['llm_calls']} 次"
              + (f"（预筛命中 {stats.get('prescreen_hits', 0)} 次）" if "prescreen_hits" in stats else ""))


def cmd_inbox(args):
    """统一收件箱：扫一次（默认）或 --watch 循环扫。只处理稳定文件（settle）。"""
    cfg = _load_libraries()
    inbox_cfg = cfg.get("inbox") or {}
    inbox_path = args.inbox or inbox_cfg.get("path", "/vol1/1000/收件箱")
    settle = args.settle_minutes or inbox_cfg.get("settle_minutes", 3)
    interval = args.interval_minutes or inbox_cfg.get("scan_interval_minutes", 30)
    conn = DB.connect(args.db)
    DB.init_db(args.db)
    rules = _load_plan_rules(args)
    # 语义/预筛建一次（fit 较重）；LLM 每轮重建，模型列表/Key 改动无需重启
    # 容器即可生效（0.1.2）。
    sem, prescreener = _setup_sem(args, rules)

    def once():
        if not os.path.isdir(inbox_path):
            print(f"收件箱尚不存在：{inbox_path}（P3 部署时创建；本次无文件可处理）")
            return None
        guard.assert_allowed(inbox_path, "收件箱")
        a2 = argparse.Namespace(**vars(args))
        a2.root = inbox_path
        # 收件箱按定义就是混合输入源（文档→工作库、媒体→媒体库），不按路径解析
        llm = _setup_llm(args)
        items, stats = _dispatch_plan(inbox_path, "mixed", rules, llm, sem,
                                      prescreener, a2, cfg)
        kept = []
        for it in items:
            bad, _ = guard.is_excluded(it.get("src") or "")
            if bad:
                print(f"! 跳过排除路径：{it.get('src')}")
                continue
            kept.append(it)
        kept, n_unstable = _settle_filter(kept, settle)
        if n_unstable:
            print(f"  未稳定（{settle} 分钟内有改动）跳过 {n_unstable} 项，下轮再扫")
        rules_version = rules.get("version") if isinstance(rules, dict) else None
        plan_id = _store_plan(conn, "inbox", rules_version, kept)
        DB.set_plan_status(conn, plan_id, "pending_review")
        csv_path = args.out or os.path.join(_HERE, "data", f"plan_{plan_id}.csv")
        _write_plan_csv(kept, csv_path)
        _print_plan_summary(plan_id, "inbox", kept, csv_path, stats)
        if args.apply:
            _apply_items(kept, inbox_path, args, conn, plan_id)
        return plan_id

    if not args.watch:
        once()
        return 0
    runs = 0
    try:
        while True:
            once()
            runs += 1
            if args.max_runs and runs >= args.max_runs:
                break
            time.sleep(interval * 60)
    except KeyboardInterrupt:
        print("收件箱监听已停止")
    return 0


def cmd_process(args):
    """任意选择处理：文件或目录（可多个）→ 盘点 → 分类 → 改名 → 打标签 → 归位。

    默认只做计划（写 DB + CSV，不移动）；--apply 才真正归位（移动 + 三件套）。
    目录按 --root 通用规则解析：三库 / 统一收件箱 / 笔记本备份 / 任意目录均可。
    """
    cfg = _load_libraries()
    conn = DB.connect(args.db)
    DB.init_db(args.db)
    rules = _load_plan_rules(args)
    llm, sem, prescreener = _setup_models(args, rules)
    all_items, stats_total = [], {"llm_calls": 0, "prescreen_hits": 0}
    roots = []
    for raw in args.path:
        p = os.path.abspath(raw)
        guard.assert_allowed(p, "处理目标")
        if os.path.isdir(p):
            a2 = argparse.Namespace(**vars(args))
            a2.root = p
            lib, kind = _resolve_library(a2)
            if lib is None:
                # 库外任意目录：按混合输入源处理，归位 dst 落对应库（工作/媒体）
                kind = "mixed"
            items, stats = _dispatch_plan(p, kind, rules, llm, sem,
                                          prescreener, a2, cfg)
            roots.append(p)
        elif os.path.isfile(p):
            items, stats = _plan_single_file(p, args, cfg, db_conn=conn)
            roots.append(os.path.dirname(p))
        else:
            print(f"! 跳过不存在的路径：{p}")
            continue
        stats_total["llm_calls"] += stats.get("llm_calls") or 0
        stats_total["prescreen_hits"] += stats.get("prescreen_hits") or 0
        all_items.extend(items)
    kept = []
    for it in all_items:
        bad, _ = guard.is_excluded(it.get("src") or "")
        if bad:
            print(f"! 跳过排除路径：{it.get('src')}")
            continue
        kept.append(it)
    # 0.1.2：单文件路径（_plan_single_file）不走 _dispatch_plan，在此补一道路径先验。
    kept = _apply_path_guard(kept, cfg)
    rules_version = rules.get("version") if isinstance(rules, dict) else None
    plan_id = _store_plan(conn, "process", rules_version, kept)
    DB.set_plan_status(conn, plan_id, "pending_review")
    csv_path = args.out or os.path.join(_HERE, "data", f"plan_{plan_id}.csv")
    _write_plan_csv(kept, csv_path)
    _print_plan_summary(plan_id, "process", kept, csv_path, stats_total)
    for it in kept[:20]:
        dst = it.get("dst") or f"复核（{it.get('review_kind') or it.get('reason')}）"
        print(f"  {it.get('src')} → {dst}")
    if len(kept) > 20:
        print(f"  …另 {len(kept) - 20} 项见 CSV")
    if args.apply:
        root = _common_root([i.get("src") or "" for i in kept]) or "."
        _apply_items(kept, root, args, conn, plan_id)
    return 0


# ---------------------------------------------------------------- main

def _add_plan_args(p):
    """plan / inbox / process 共用的判定参数（与原 workfiles-curator 兼容）。"""
    p.add_argument("--rules", default="")
    p.add_argument("--media-config", default="")
    p.add_argument("--ocr-url", default=None,
                   help="OCR 服务地址；不填则自动探测（容器内用宿主网关地址）")
    p.add_argument("--no-ocr", action="store_true")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--max-chars", type=int, default=2000)
    p.add_argument("--ocr-timeout", type=int, default=90)
    p.add_argument("--min-score", type=int, default=4)
    p.add_argument("--content-first", action="store_true")
    p.add_argument("--cache", default="")
    p.add_argument("--unit-mode", choices=["auto", "project", "off"], default="auto")
    p.add_argument("--protect", default="")
    p.add_argument("--rename", action="store_true")
    p.add_argument("--rename-llm", action="store_true")
    p.add_argument("--llm", action="store_true")
    p.add_argument("--llm-host", default="http://172.17.0.1:11434")
    p.add_argument("--llm-model", default="qwen3:4b")
    p.add_argument("--llm-min-conf", type=float, default=0.55)
    p.add_argument("--llm-max", type=int, default=2000)
    p.add_argument("--sem", action="store_true")
    p.add_argument("--sem-model", default="bge-m3:latest")
    p.add_argument("--sem-min-sim", type=float, default=0.42)
    p.add_argument("--sem-margin", type=float, default=0.02)
    return p


def _add_apply_args(p):
    """--apply 执行类参数（process / inbox 用）。"""
    p.add_argument("--apply", action="store_true", help="真正归位（移动）；不给则只做计划")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--map", default="")
    p.add_argument("--report", default="")
    p.add_argument("--uid", type=int, default=1000)
    p.add_argument("--gid", type=int, default=1000)
    return p


def build_parser():
    ap = argparse.ArgumentParser(
        description="NAS Curator（文件管家）统一 CLI：文档 + 媒体整理，可回滚")
    ap.add_argument("--db", default=os.path.join(_HERE, "data", "nas-curator.db"),
                    help="SQLite 元数据路径")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("inventory", help="只读盘点")
    p1.add_argument("--root", required=True)
    p1.add_argument("--out", default="")
    p1.set_defaults(func=cmd_inventory)

    pl = sub.add_parser("learn", help="从已整理目录反推类目体系")
    pl.add_argument("--root", required=True)
    pl.add_argument("--out", default="rules_learned.json")
    pl.add_argument("--per-cat", type=int, default=6)
    pl.add_argument("--topk", type=int, default=10)
    pl.set_defaults(func=cmd_learn)

    p2 = sub.add_parser("plan", help="生成分类/命名计划（只读，写 DB + CSV）")
    p2.add_argument("--root", required=True)
    p2.add_argument("--library", default="", help="work | family | media | mac-backup（可省，按 root 自动匹配）")
    p2.add_argument("--kind", default="document", choices=["document", "media"])
    p2.add_argument("--out", default="")
    # P4：深挖模式——scan_units 下钻全部子目录（max_depth 2→5），修库全量
    # 覆盖用。默认 auto 模式只下钻透明目录（收件箱/待处理等），对已整理的库
    # （工作文件 14,427 文件只产出 3 项）覆盖不足。
    p2.add_argument("--deep", action="store_true",
                    help="深挖全部子目录做全量计划（修库用），默认只下钻透明目录")
    _add_plan_args(p2)
    p2.set_defaults(func=cmd_plan)

    pi = sub.add_parser("inbox", help="扫统一收件箱（默认扫一次；--watch 循环扫）")
    pi.add_argument("--inbox", default="", help="收件箱路径，默认 config 的 /vol1/1000/收件箱")
    pi.add_argument("--watch", action="store_true", help="循环扫描")
    pi.add_argument("--interval-minutes", type=int, default=0, help="0=用 config（30）")
    pi.add_argument("--settle-minutes", type=int, default=0, help="0=用 config（3）")
    pi.add_argument("--max-runs", type=int, default=0, help="--watch 最多扫几轮，0=无限")
    pi.add_argument("--out", default="")
    _add_plan_args(pi)
    _add_apply_args(pi)
    pi.set_defaults(func=cmd_inbox)

    pp = sub.add_parser("process", help="任意选择处理：文件或目录（可多个），默认只做计划")
    pp.add_argument("path", nargs="+", help="文件或目录")
    pp.add_argument("--out", default="")
    _add_plan_args(pp)
    _add_apply_args(pp)
    pp.set_defaults(func=cmd_process)

    p3 = sub.add_parser("apply", help="按计划执行（可回滚）")
    p3.add_argument("--plan", default="")
    p3.add_argument("--plan-id", type=int, default=0)
    p3.add_argument("--root", default="")
    p3.add_argument("--map", default="")
    p3.add_argument("--report", default="")
    p3.add_argument("--uid", type=int, default=1000)
    p3.add_argument("--gid", type=int, default=1000)
    p3.add_argument("--dry-run", action="store_true")
    p3.set_defaults(func=cmd_apply)

    p4 = sub.add_parser("rollback", help="按映射表或计划回滚")
    p4.add_argument("--map", default="")
    p4.add_argument("--plan-id", type=int, default=0)
    p4.add_argument("--dry-run", action="store_true")
    p4.set_defaults(func=cmd_rollback)

    p5 = sub.add_parser("review", help="复核队列")
    p5.add_argument("action", choices=["list", "decide"])
    p5.add_argument("--status", default="pending")
    p5.add_argument("--kind", default="")
    p5.add_argument("--limit", type=int, default=50)
    p5.add_argument("--review-id", type=int, default=0)
    p5.add_argument("--decision", default="", choices=["", "approved", "rejected"])
    p5.add_argument("--by", default="user")
    p5.set_defaults(func=cmd_review)

    p6 = sub.add_parser("analyze", help="目录画像")
    p6.add_argument("--root", required=True)
    p6.add_argument("--unit-mode", choices=["auto", "project", "off"], default="auto")
    p6.set_defaults(func=cmd_analyze)

    p7 = sub.add_parser("explain", help="解释单个文件的判定依据")
    p7.add_argument("--path", required=True)
    p7.add_argument("--rules", default="")
    p7.set_defaults(func=cmd_explain)

    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
