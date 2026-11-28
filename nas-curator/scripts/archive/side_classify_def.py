#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""side-classify D/E/F 分类（阶段 1c）：三处 99-待确认（上海润心/北京供应链/辽宁器械）。
文本已由宿主机抽取为 /data/side_text/<idx>.txt（见 side_text_index.tsv）。
- 有文本/压缩包清单的：文本模型分类（v3 知识块），并按命名 v1 给出新名
  （文档：YYYY-MM-DD_实体_事项.扩展名；压缩包：同式，已带 __vN 的保留版本）。
- 加密（file 判 data / .pag / 读不出）：不调用模型、不改名，进复核清单，
  附文件名关键词推断的建议落位（建议方=来源方，建议子目录按关键词映射）。
- 上海润心 2 个管理文件：按文本分类，但落位时 apply 脚本按原名保留、
  放 2-上海润心/14-工具与其他/（审计记录）。
只读：不移动、不写库。输出 /data/side_classify_def.jsonl + 汇总。
"""
import sys, os, json, time, re, hashlib, importlib.util
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")
from curator_core.llmkit import LLM

spec = importlib.util.spec_from_file_location("scf", "/data/side_classify_full.py")
scf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scf)

PARTIES = scf.PARTIES
SUBDIRS = scf.SUBDIRS
KNOWLEDGE = scf.KNOWLEDGE
PARTY_DESC = scf.PARTY_DESC

SRC_KIND = {"99-待确认-上海润心": "D-上海润心99",
            "99-待确认-北京供应链": "E-北京供应链99",
            "99-待确认-辽宁器械": "F-辽宁器械99"}
PARTY_BY_SRC = {"D-上海润心99": "2-上海润心",
                "E-北京供应链99": "3-北京供应链",
                "F-辽宁器械99": "4-辽宁器械"}

# 文件名关键词 → 建议子目录（用于加密文件的建议落位）
SUGGEST_KW = [
    ("补充协议", "03-合同与协议"), ("变更", "03-合同与协议"),
    ("经销协议", "03-合同与协议"), ("购销合同", "03-合同与协议"),
    ("合同", "03-合同与协议"), ("协议", "03-合同与协议"),
    ("注册证", "02-产品与注册"), ("产品授权", "02-产品与注册"),
    ("授权", "02-产品与注册"), ("检验", "02-产品与注册"),
    ("检测报告", "02-产品与注册"),
    ("营业执照", "01-公司资质与证照"), ("经营许可证", "01-公司资质与证照"),
    ("许可证", "01-公司资质与证照"), ("资质", "01-公司资质与证照"),
    ("对账", "07-财务与经营分析"), ("应收", "07-财务与经营分析"),
    ("应付", "07-财务与经营分析"), ("发票", "07-财务与经营分析"),
    ("负债表", "07-财务与经营分析"), ("预算", "07-财务与经营分析"),
    ("费用", "07-财务与经营分析"), ("销售", "05-销售与业务数据"),
    ("收货", "06-库存与物流"), ("验收", "06-库存与物流"),
    ("库存", "06-库存与物流"), ("物流", "06-库存与物流"),
    ("周报", "10-周报与工作总结"), ("月报", "10-周报与工作总结"),
    ("总结", "10-周报与工作总结"), ("述职", "12-项目资料"),
    ("人事", "11-人事与行政"), ("简历", "11-人事与行政"),
    ("招聘", "11-人事与行政"), ("录用", "11-人事与行政"),
    ("入职", "11-人事与行政"), ("离职", "11-人事与行政"),
    ("会议", "09-会议与汇报"), ("培训", "11-人事与行政"),
    ("招标", "12-招投标"), ("投标", "12-招投标"), ("中标", "12-招投标"),
    ("临床", "15-临床与学术资料"), ("论文", "15-临床与学术资料"),
    ("文献", "15-临床与学术资料"), ("产品资料", "02-产品与注册"),
    ("产品", "02-产品与注册"), ("项目", "08-运营与项目"),
    ("方案", "08-运营与项目"), ("可行性", "08-运营与项目"),
    ("尽调", "08-运营与项目"), ("信用报告", "08-运营与项目"),
    ("价格", "05-销售与业务数据"), ("报价", "05-销售与业务数据"),
    ("挂网", "12-招投标"), ("集采", "12-招投标"),
]


def suggest_subdir(name):
    for kw, sub in SUGGEST_KW:
        if kw in name:
            return sub
    return "14-工具与其他"


def base_name_for_suggest(filename):
    n = filename
    if n.endswith(".pag"):
        n = n[:-4]
    return n


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def doc_classify(llm, primary, fallback, text_blob, filename, extractor):
    prompt = (
        "你是文件归档专家。根据下面这份文档的摘录文本（前 4000 字）与文件名，判断它属于"
        "下面哪一方、进该方哪个子目录，并按命名规则给出新文件名。只按内容所属方判断。\n"
        "判断规则（按顺序）：\n"
        "1) 文本/文件名明确指向某一方业务→定方（见下方人员与组织线索）。\n"
        "2) 业务内容但看不出属于哪一方→无法判断。\n"
        "3) 文本为空/乱码/仅为文件名清单时，按文件名与内含文件名判断；仍无法判断→无法判断。\n"
        f"{KNOWLEDGE}"
        f"可选方：\n{PARTY_DESC}\n"
        "子目录：从该方子目录列表中选最贴近的（周报类→10-周报与工作总结；"
        "人事录用/入职/离职→11-人事与行政；财务付款/对账/发票→07-财务与经营分析；"
        "销售数据→05-销售与业务数据；合同协议→03-合同与协议；会议→09-会议与汇报；"
        "库存物流/收货验收→06-库存与物流；资质证照→01-公司资质与证照；"
        "产品注册/授权→02-产品与注册；招投标→12-招投标；临床学术→15-临床与学术资料"
        "（仅辽宁）；实在没有贴近的→14-工具与其他）。\n"
        "新文件名（命名 v1，文档/压缩包）：YYYY-MM-DD_实体_事项.扩展名。日期取文中日期"
        "（合同/报告取文中日期，收货表取表头起始日期），文中没有的填空；实体取内容主体"
        "公司/品牌简称（如 辽宁器械、世龙经略、华润供应链、史赛克）；事项取标题/首页主题"
        "（不超过 12 字）；扩展名与原文件一致；原名已带 __v2/__v3 等版本号的保留版本号。\n"
        f"文件名：{filename}\n摘录来源：{extractor}\n摘录文本：\n{text_blob}\n"
        '只输出 JSON：{"party":"方名（原样输出，如 3-北京供应链 或 无法判断）",'
        '"subdir":"子目录名（原样输出；无法判断时为空）",'
        '"confidence":0.9,"reason":"一句话理由，说明关键信息",'
        '"doc_date":"YYYY-MM-DD 或空","entity":"实体简称或空",'
        '"subject":"事项（不超过12字）或空"}'
    )
    messages = [
        {"role": "system", "content": "只输出 JSON，不要解释。"},
        {"role": "user", "content": prompt},
    ]
    content, prov, err = scf.call_ark(llm, primary, fallback, messages)
    if err is not None:
        return None, err
    out = scf.parse_json(content)
    if not isinstance(out, dict) or "party" not in out:
        return None, "bad_json:" + (content or "")[:120]
    party = str(out.get("party", "")).strip()
    if party not in PARTIES:
        return None, "invalid_party:" + party[:40]
    try:
        conf = round(max(0.0, min(1.0, float(out.get("confidence", 0)))), 2)
    except Exception:
        conf = 0.0
    subdir = str(out.get("subdir", "")).strip()
    if party != "无法判断" and subdir not in SUBDIRS.get(party, []):
        subdir = ""
    return {"party": party, "subdir": subdir, "confidence": conf,
            "reason": str(out.get("reason", ""))[:150],
            "doc_date": str(out.get("doc_date", "")).strip(),
            "entity": str(out.get("entity", "")).strip(),
            "subject": str(out.get("subject", "")).strip(),
            "model": prov["model"], "provider": prov["name"],
            "fallback": prov is fallback}, None


def sanitize(s):
    return re.sub(r'[\\/:*?"<>|\n\r\t]', "_", s).strip()


def build_new_name(rec, out):
    """v1 新名；失败返回 ''。"""
    ext = rec["ext"]
    date = out.get("doc_date") or ""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        date = ""
    if not date:
        try:
            date = datetime.fromtimestamp(rec["mtime"]).strftime("%Y-%m-%d")
        except Exception:
            date = ""
    entity = sanitize(out.get("entity") or "")
    subject = sanitize(out.get("subject") or "")
    if not entity:
        entity = {"2-上海润心": "上海润心", "3-北京供应链": "北京供应链",
                  "4-辽宁器械": "辽宁器械", "1-王震": "王震"}.get(out["party"], "")
    if not subject:
        stem = os.path.splitext(rec["filename"])[0]
        stem = re.sub(r"__v\d+$", "", stem)
        subject = sanitize(stem)[:12] or "文件"
    ver = ""
    m = re.search(r"(__v\d+)", rec["filename"])
    if m:
        ver = m.group(1)
    return "%s_%s_%s%s%s" % (date, entity, subject, ver, ext)


def main():
    import threading
    llm = LLM()
    primary, fallback = scf.pick_providers(llm)
    print("primary: %s | model: %s | billing: %s" % (
        primary["name"], primary["model"], primary.get("billing")), flush=True)

    rows = []
    with open("/data/side_text_index.tsv", encoding="utf-8") as f:
        hdr = f.readline()
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) < 9:
                continue
            rows.append({"idx": int(p[0]), "src": p[1], "size": int(p[2]),
                         "mtime": int(p[3]), "ext": p[4], "magic": p[5],
                         "extractor": p[6], "text_len": int(p[7]),
                         "encrypted": int(p[8])})
    print("index rows: %d" % len(rows), flush=True)
    for r in rows:
        if "/99-待确认-上海润心/" in r["src"]:
            r["src_kind"] = "D-上海润心99"
        elif "/99-待确认-北京供应链/" in r["src"]:
            r["src_kind"] = "E-北京供应链99"
        else:
            r["src_kind"] = "F-辽宁器械99"
        r["filename"] = os.path.basename(r["src"])

    # 同大小组全量 sha256 找完全重复（仅同大小组才哈希）
    by_size = defaultdict(list)
    for r in rows:
        by_size[r["size"]].append(r["src"])
    to_hash = [p for ps in by_size.values() if len(ps) > 1 for p in ps]
    print("size groups>1 files to hash: %d" % len(to_hash), flush=True)
    hashes = {}
    if to_hash:
        def _h(p):
            try:
                return p, sha256_file(p)
            except Exception:
                return p, None
        with ThreadPoolExecutor(max_workers=2) as ex:
            for p, h in ex.map(_h, to_hash):
                if h:
                    hashes[p] = h
    by_sha = defaultdict(list)
    for p, h in hashes.items():
        by_sha[h].append(p)
    dup_of = {}
    for h, ps in by_sha.items():
        if len(ps) > 1:
            ps.sort()
            # __vN 版本文件不算重复（v1 规则保留版本）
            plain = [p for p in ps if not re.search(r"__v\d+", p)]
            vers = [p for p in ps if re.search(r"__v\d+", p)]
            keep = plain[0] if plain else ps[0]
            for p in ps[1:]:
                if p in vers:
                    continue
                if p == keep:
                    continue
                dup_of[p] = keep
    print("exact dup losers (excl __vN): %d" % len(dup_of), flush=True)

    tls = threading.local()

    def thread_llm():
        o = getattr(tls, "llm", None)
        if o is None:
            o = LLM()
            tls.llm = o
        return o

    def one(r):
        rec = {"src_kind": r["src_kind"], "src": r["src"],
               "filename": r["filename"], "size": r["size"],
               "mtime": r["mtime"], "ext": r["ext"],
               "extractor": r["extractor"]}
        t0 = time.time()
        if r["src"] in dup_of:
            rec.update({"status": "ok", "method": "dedup", "party": "无法判断",
                        "subdir": "", "confidence": 0.0, "reason": "",
                        "decision": "review",
                        "review_reason": "重复：与 %s 内容完全相同（sha256 一致），保留那一份"
                        % dup_of[r["src"]]})
            return rec
        if r["encrypted"]:
            name = base_name_for_suggest(r["filename"])
            if "_state_for_dir" in name:
                rec.update({"status": "ok", "method": "encrypted",
                            "party": "无法判断", "subdir": "",
                            "confidence": 0.0, "reason": "",
                            "decision": "review",
                            "review_reason": "加密/同步状态文件，无业务内容；建议忽略",
                            "suggested_party": PARTY_BY_SRC[r["src_kind"]],
                            "suggested_subdir": ""})
                return rec
            sub = suggest_subdir(name)
            rec.update({"status": "ok", "method": "encrypted",
                        "party": "无法判断", "subdir": "", "confidence": 0.0,
                        "reason": "",
                        "decision": "review",
                        "review_reason": "加密：群晖云同步加密文件，读不出内容；建议落位见 suggested",
                        "suggested_party": PARTY_BY_SRC[r["src_kind"]],
                        "suggested_subdir": sub})
            return rec
        if r["extractor"].startswith("unreadable") or r["extractor"] == "plain-fail":
            rec.update({"status": "ok", "method": "unreadable",
                        "party": "无法判断", "subdir": "", "confidence": 0.0,
                        "reason": "", "decision": "review",
                        "review_reason": "读不出内容（%s）；建议落位见 suggested"
                        % r["extractor"],
                        "suggested_party": PARTY_BY_SRC[r["src_kind"]],
                        "suggested_subdir": suggest_subdir(
                            base_name_for_suggest(r["filename"]))})
            return rec
        try:
            with open("/data/side_text/%d.txt" % r["idx"], encoding="utf-8") as f:
                text = f.read()
        except Exception as e:
            rec.update({"status": "error", "error": str(e)[:150],
                        "decision": "review", "review_reason": "侧车文本读取失败"})
            return rec
        if not text.strip():
            rec.update({"status": "ok", "method": "empty-text",
                        "party": "无法判断", "subdir": "", "confidence": 0.0,
                        "reason": "", "decision": "review",
                        "review_reason": "摘录文本为空，无法判断；建议落位见 suggested",
                        "suggested_party": PARTY_BY_SRC[r["src_kind"]],
                        "suggested_subdir": suggest_subdir(
                            base_name_for_suggest(r["filename"]))})
            return rec
        out, err = doc_classify(thread_llm(), primary, fallback, text,
                                r["filename"], r["extractor"])
        if err is not None:
            rec.update({"status": "api_error", "error": err[:200],
                        "decision": "review", "review_reason": "API失败未分类"})
            return rec
        rec.update({"status": "ok", "method": "doc-text", **out})
        rec["latency_s"] = round(time.time() - t0, 2)
        if rec.get("party") == "无法判断":
            rec["decision"] = "review"
            rec["review_reason"] = "无法判断：" + (rec.get("reason") or "")
            rec["suggested_party"] = PARTY_BY_SRC[r["src_kind"]]
            rec["suggested_subdir"] = suggest_subdir(
                base_name_for_suggest(r["filename"]))
        elif rec.get("confidence", 0) < 0.85:
            rec["decision"] = "review"
            rec["review_reason"] = "低置信（%.2f）：%s" % (rec.get("confidence", 0),
                                                        rec.get("reason") or "")
            rec["suggested_party"] = rec.get("party")
            rec["suggested_subdir"] = rec.get("subdir")
        else:
            rec["decision"] = "move"
            rec["new_name"] = build_new_name(rec, out)
        return rec

    results = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(one, r): r for r in rows}
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            print("%s %s %s %s" % (r.get("status"), r.get("party", ""),
                                   r.get("filename", "")[:40],
                                   r.get("decision", ""))[:180], flush=True)
    results.sort(key=lambda r: r["src"])
    with open("/data/side_classify_def.jsonl", "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("SUMMARY " + json.dumps({
        "n": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "move": sum(1 for r in results if r.get("decision") == "move"),
        "review": sum(1 for r in results if r.get("decision") == "review"),
        "by_party": dict(Counter(r.get("party") for r in results
                                 if r["status"] == "ok")),
        "by_method": dict(Counter(r.get("method") for r in results)),
        "dup_losers": len(dup_of),
        "fallback_used": sum(1 for r in results if r.get("fallback")),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
