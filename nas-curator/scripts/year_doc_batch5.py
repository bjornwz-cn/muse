#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch5 文档分类（只分类、不移动、不写库）。

对快照里 kind=文档 的文件读内容定所属方（仿照 11 处分类 side_classify_full.py
的做法：PARTY_DESC / KNOWLEDGE / SUBDIRS 同款，agent-plan 主调、失败降级
model-3 经济版），kind=其他 的文件直接记 待确认/unsupported_ext，不调模型。
结果追加写进 /data/year_siglip_results_batch5.jsonl（与 SigLIP 结果同格式，
供 year_register_batch5.py 登记）。断点续跑：已有结果的行跳过。

文档提取（容器内，标准库 + pdftotext/catdoc）：
  .pdf -> pdftotext 前 3 页；.docx/.xlsx/.pptx -> zip 内 XML 去标签；
  .doc/.xls/.ppt -> XML 判型或 catdoc；.txt/.csv/.md/.html/.webloc/.eml -> 前 4000 字；
  .zip/.rar -> 内含文件名清单；.eddx/.emmx/.osheet/.odoc -> zip 内 XML 或 strings。
  加密 blob（__CLOUDSYNC_ENC__ 开头 / .pag）-> 无文本，记 doc-encrypted。

类目：category=所属方（1-王震/2-上海润心/3-北京供应链/4-辽宁器械），
无法判断/无文本/失败时 category=待确认。subdir 记在结果行的 subdir 键。
只记 provider 名/模型/计费档，绝不打印 Key。
运行（NAS 容器内）：docker exec nas-curator python3 /data/year_doc_batch5.py --workers 2
"""
import sys, os, json, time, re, argparse, subprocess, zipfile, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

from curator_core.llmkit import LLM

SNAPSHOT = "/data/year_batch5_snapshot.tsv"
RESULTS = "/data/year_siglip_results_batch5.jsonl"
PROGRESS_DIR = "/data/progress"
CAP = 4000

PARTIES = ["1-王震", "2-上海润心", "3-北京供应链", "4-辽宁器械", "无法判断"]
SUBDIRS = {
    "1-王震": ["00-整理说明", "09-会议与汇报", "10-周报与工作总结",
               "12-项目资料", "14-工具与其他"],
    "2-上海润心": ["01-公司资质与证照", "02-产品与注册", "03-合同与协议",
                  "04-供应商与采购", "05-销售与业务数据", "06-库存与物流",
                  "07-财务与经营分析", "08-运营与项目", "09-会议与汇报",
                  "10-周报与工作总结", "11-人事与行政", "12-招投标",
                  "12-项目资料", "13-图片与影像", "14-工具与其他"],
    "3-北京供应链": ["01-公司资质与证照", "02-产品与注册", "03-合同与协议",
                   "04-供应商与采购", "05-销售与业务数据", "06-库存与物流",
                   "07-财务与经营分析", "08-运营与项目", "09-会议与汇报",
                   "10-周报与工作总结", "11-人事与行政", "12-招投标",
                   "13-图片与影像", "14-工具与其他"],
    "4-辽宁器械": ["01-公司资质与证照", "02-产品与注册", "03-合同与协议",
                  "04-供应商与采购", "05-销售与业务数据", "06-库存与物流",
                  "07-财务与经营分析", "08-运营与项目", "09-会议与汇报",
                  "10-周报与工作总结", "11-人事与行政", "12-招投标",
                  "13-图片与影像", "14-工具与其他", "15-临床与学术资料"],
}
PARTY_DESC = (
    "1-王震：王震个人的文件——个人述职/工作总结/工作计划、党建材料、个人证件与人事档案、"
    "个人培训学习、个人周报、家庭/私人文件、非公司业务的个人邮件。\n"
    "2-上海润心：上海华润润心医疗器械有限公司（上海公司）的业务文件——公司资质证照、产品注册、"
    "合同协议、供应商采购、销售业务数据、库存物流、财务经营、运营项目、会议汇报、周报月报、"
    "人事行政、招投标、现场/验收照片视频。注意：浙江事业部、上海业务组属于上海润心。\n"
    "3-北京供应链：北京供应链公司（华润医药商业北京供应链相关）的业务文件——同上编号结构，"
    "内容主体是北京供应链公司、北京区域医院/项目、供应链服务。\n"
    "4-辽宁器械：华润润心（辽宁）医疗器械有限公司（含其上海分公司）的业务文件——同上编号结构，"
    "另有 15-临床与学术资料；内容主体是辽宁公司、沈阳/辽宁区域医院与项目、辽宁电力中心医院SPD、"
    "区域检验中心等。注意：名称含“辽宁器械”“华润润心（辽宁）”的文件属于这一方，"
    "即使有“上海分公司”字样也不属于 2-上海润心。\n"
    "无法判断：正文信息不足以确定属于哪一方，或内容与四方都无关，或为损坏/无关文件。"
)
KNOWLEDGE = (
    "人员与组织线索（内容涉及某一方已知人员、事业部/区域，即为明确指向该方；不要只认公司全名）：\n"
    "- 2-上海润心：王震（总经理，分管营销中心）；金祎、吴洁（供应链总监）、战轲、"
    "王雅琴（人事经理）、于航（浙江事业部负责人）、佟晓锐、郑丹凤（综合管理部）、"
    "张欢（财务部）、黄明辉（财务）、李阳希（上海业务组）、高熙渊（上海业务组）、"
    "王佳乐（上海业务组）、胥艳丽（物流部）、刘芳芳；事业部/区域：浙江事业部、"
    "上海业务组、温州仓、上海仓；业务：波科（BK）、微术、旭化成。\n"
    "- 4-辽宁器械：白天山（辽宁事业部负责人）、王子辰（吉林事业部负责人，辽吉区域）；"
    "辽宁业务组、辽宁分、辽吉区域；沈阳/抚顺/辽源/锦州/营口等辽宁区域医院；"
    "辽宁电力中心医院SPD、区域检验中心；公司名含“辽宁器械”“华润润心（辽宁）”"
    "“华润辽宁器械”（即使有“上海分公司”字样也属辽宁器械）。\n"
    "- 3-北京供应链：北京区域、北京供应链公司、供应链服务方案。\n"
    "- 1-王震：王震个人、党建、述职、个人证件、家庭。\n"
    "品牌名（爱德华、波科等）本身不决定归属，看协议主体/事业部/区域。\n"
    "多方内容以主体为准；确实多方均等、无法定主体时才判无法判断。\n"
)
SUBDIR_HINT = (
    "子目录：必填，从该方子目录列表中选最贴近的（周报类→10-周报与工作总结；"
    "人事录用/入职/离职→11-人事与行政；财务付款/对账/发票→07-财务与经营分析；"
    "销售数据→05-销售与业务数据；合同协议→03-合同与协议；会议→09-会议与汇报；"
    "库存物流→06-库存与物流；资质证照→01-公司资质与证照；产品注册→02-产品与注册；"
    "招投标→12-招投标；实在没有贴近的→14-工具与其他）。"
)

try:
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None


def _hb(**kw):
    if _write_hb is None:
        return
    try:
        _write_hb("year-reclassify-batch5", progress_dir=PROGRESS_DIR, **kw)
    except Exception as e:  # noqa: BLE001
        print("heartbeat failed: %s" % e, flush=True)


def pick_providers(llm):
    ark = [p for p in llm.providers if p.get("kind") == "ark"]
    primary = next((p for p in ark if p.get("name") == "agent-plan"), None)
    if primary is None:
        primary = next((p for p in ark if p.get("billing") == "free"), None)
    fallback = next((p for p in ark if p.get("name") == "model-3"), None)
    if fallback is None:
        fallback = next((p for p in ark
                         if p.get("billing") == "payg" and p is not primary), None)
    if primary is None:
        raise RuntimeError("no ark provider available")
    return primary, fallback


def parse_json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def run(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.stdout
    except Exception:
        return b""


def strip_tags(t):
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def head_bytes(path, n=2 * 1024 * 1024):
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except Exception:
        return b""


def extract(path, ext):
    """返回 (text, extractor, encrypted_flag)。"""
    try:
        with open(path, "rb") as f:
            magic_head = f.read(64)
    except Exception:
        return "", "read-fail", False
    if magic_head.startswith(b"__CLOUDSYNC_ENC__"):
        return "", "cloudsync-encrypted", True
    if ext == ".pdf":
        out = run(["pdftotext", "-f", "1", "-l", "3", "-layout", path, "-"],
                  timeout=180)
        return out.decode(errors="replace")[:CAP], "pdftotext-3p", False
    if ext in (".docx", ".xlsx", ".pptx"):
        try:
            z = zipfile.ZipFile(path)
            parts = []
            for n in z.namelist():
                if n.endswith(".xml") and ("word/" in n or "xl/" in n
                                           or "ppt/slides/" in n
                                           or "sharedStrings" in n):
                    try:
                        parts.append(z.read(n).decode(errors="replace"))
                    except Exception:
                        pass
                    if sum(len(p) for p in parts) > 200000:
                        break
            return strip_tags(" ".join(parts))[:CAP], "ooxml", False
        except Exception:
            return "", "ooxml-fail", False
    if ext in (".doc", ".xls", ".ppt"):
        data = head_bytes(path, 4096)
        s = data[:200].lstrip()
        if s.startswith(b"<?xml") or s.startswith(b"<"):
            raw = head_bytes(path, 2 * 1024 * 1024).decode(errors="replace")
            return strip_tags(raw)[:CAP], "spreadsheetml", False
        out = run(["catdoc", path], timeout=120)
        return out.decode(errors="replace")[:CAP], "catdoc", False
    if ext in (".txt", ".csv", ".md", ".html", ".webloc"):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read(CAP), "plain", False
        except Exception:
            return "", "plain-fail", False
    if ext == ".eml":
        try:
            import email
            from email import policy
            from email.parser import BytesParser
            with open(path, "rb") as fh:
                m = BytesParser(policy=policy.default).parse(fh)
            atts, body = [], ""
            if m.is_multipart():
                for p in m.walk():
                    if p.get_filename():
                        atts.append(p.get_filename())
                    elif p.get_content_type() == "text/plain" and not body:
                        try:
                            body = p.get_content() or ""
                        except Exception:
                            body = ""
            else:
                if m.get_content_type() == "text/plain":
                    try:
                        body = m.get_content() or ""
                    except Exception:
                        body = ""
            body = re.sub(r"\s+", " ", body or "").strip()[:1500]
            blob = ("发件人：%s\n收件人：%s\n抄送：%s\n日期：%s\n主题：%s\n附件：%s\n正文：%s"
                    % (m.get("From"), m.get("To"), m.get("Cc"), m.get("Date"),
                       m.get("Subject"), "；".join(atts) if atts else "无", body))
            return blob[:CAP], "eml-parse", False
        except Exception:
            return "", "eml-fail", False
    if ext == ".zip":
        try:
            z = zipfile.ZipFile(path)
            names, enc = [], False
            for info in z.infolist()[:80]:
                names.append(info.filename)
                if info.flag_bits & 0x1:
                    enc = True
            return "压缩包内含文件：\n" + "\n".join(names), "zip-list", enc
        except Exception:
            return "", "zip-fail", False
    if ext == ".rar":
        out = run(["7z", "l", path], timeout=120)
        if out:
            return out.decode(errors="replace")[:CAP], "7z-list", False
        return "", "archive-nolist", False
    if ext in (".eddx", ".emmx", ".osheet", ".odoc"):
        try:
            z = zipfile.ZipFile(path)
            parts = []
            for n in z.namelist():
                if n.endswith(".xml"):
                    try:
                        parts.append(z.read(n).decode(errors="replace"))
                    except Exception:
                        pass
            if parts:
                return strip_tags(" ".join(parts))[:CAP], "zip-xml", False
        except Exception:
            pass
        out = run(["strings", path], timeout=60)
        return out.decode(errors="replace")[:CAP], "strings", False
    return "", "unreadable", False


def valid_out(out):
    if not isinstance(out, dict) or "party" not in out:
        return None
    party = str(out.get("party", "")).strip()
    if party not in PARTIES:
        return None
    try:
        conf = round(max(0.0, min(1.0, float(out.get("confidence", 0)))), 2)
    except Exception:
        conf = 0.0
    subdir = str(out.get("subdir", "")).strip()
    if party != "无法判断" and subdir not in SUBDIRS.get(party, []):
        subdir = ""
    return {"party": party, "subdir": subdir, "confidence": conf,
            "reason": str(out.get("reason", ""))[:150]}


def text_classify(llm, primary, fallback, text_blob, filename):
    prompt = (
        "你是文件归档专家。根据下面这份文件的摘录文本（开头部分），判断它属于下面哪一方、"
        "进该方哪个子目录。只按内容所属方判断。\n"
        "判断规则（按顺序）：\n"
        "1) 摘录明确指向某一方业务→定方（见下方人员与组织线索）。\n"
        "2) 系统通知/营销广告/个人服务类内容，收件人为王震本人且无公司业务归属信号→1-王震。\n"
        "3) 业务内容但看不出属于哪一方→无法判断。\n"
        f"{KNOWLEDGE}"
        f"可选方：\n{PARTY_DESC}\n"
        f"{SUBDIR_HINT}\n"
        f"文件名：{filename}\n"
        f"摘录：\n{text_blob}\n"
        '只输出 JSON：{"party":"方名（原样输出，如 2-上海润心 或 无法判断）",'
        '"subdir":"子目录名（原样输出，如 03-合同与协议；无法判断时为空）",'
        '"confidence":0.9,"reason":"一句话理由，说明关键信息"}'
    )
    messages = [
        {"role": "system", "content": "只输出 JSON，不要解释。"},
        {"role": "user", "content": prompt},
    ]
    for prov in (primary, fallback):
        if prov is None:
            continue
        content, err = llm._call_ark(prov, messages, True, 60, 300)
        if err or not content:
            continue
        out = valid_out(parse_json(content))
        if out is None:
            return None, "bad_json_or_cat:" + (content or "")[:120], prov
        out.update({"model": prov["model"], "provider": prov["name"],
                    "fallback": prov is fallback})
        return out, None, prov
    return None, "all providers failed", None


_tls = threading.local()


def thread_llm():
    llm = getattr(_tls, "llm", None)
    if llm is None:
        llm = LLM()
        _tls.llm = llm
    return llm


def process_one(item, primary, fallback):
    path, kind = item
    ext = os.path.splitext(path)[1].lower()
    base = {'src': path, 'kind': kind, 'category': '待确认', 'confidence': 0.0,
            'reason': '', 'evidence': '', 'why': '', 'suggested_dst': '',
            'party': '', 'subdir': ''}
    if kind == '其他':
        base.update({'reason': 'unsupported_ext:%s' % ext,
                     'why': 'unsupported_ext'})
        return base
    # 文档：抽文本
    try:
        text, extractor, enc = extract(path, ext)
    except Exception as e:
        base.update({'reason': 'doc-extract-failed:%s' % type(e).__name__,
                     'why': 'doc-decode_failed'})
        return base
    base['evidence'] = 'extractor=%s text_len=%d' % (extractor, len(text))
    if enc:
        base.update({'reason': 'encrypted: %s' % extractor,
                     'why': 'doc-encrypted'})
        return base
    if not text.strip():
        base.update({'reason': 'empty-text: %s' % extractor,
                     'why': 'doc-empty'})
        return base
    llm = thread_llm()
    out, err, prov = text_classify(llm, primary, fallback, text,
                                   os.path.basename(path))
    if err is not None:
        base.update({'reason': 'api_error:%s' % err[:150],
                     'why': 'doc-api_error'})
        return base
    party, conf = out['party'], out['confidence']
    base.update({'party': party, 'subdir': out['subdir'],
                 'doc_model': out.get('model', ''),
                 'reason': out['reason'],
                 'evidence': '%s/%s：%s' % (party, out['subdir'] or '-',
                                            out['reason']) if party != '无法判断'
                             else out['reason'],
                 'why': 'doc-llm'})
    if party == '无法判断':
        base.update({'category': '待确认', 'confidence': round(conf, 3)})
    else:
        base.update({'category': party, 'confidence': round(conf, 3)})
    if prov is not None and out.get('fallback'):
        base['reason'] = '[economy] ' + base['reason']
    return base


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workers', type=int, default=2)
    args = ap.parse_args()
    llm = LLM()
    primary, fallback = pick_providers(llm)
    print("primary: %s | model: %s | billing: %s" % (
        primary["name"], primary["model"], primary.get("billing")), flush=True)
    if fallback is not None:
        print("fallback: %s | model: %s | billing: %s" % (
            fallback["name"], fallback["model"], fallback.get("billing")), flush=True)
    items = []
    with open(SNAPSHOT, encoding='utf-8') as fh:
        for line in fh:
            p = line.rstrip('\n').split('\t')
            if len(p) >= 4 and p[3] in ('文档', '其他'):
                items.append((p[0], p[3]))
    items.sort()
    print('snapshot doc/other files: %d (doc %d, other %d)' % (
        len(items), sum(1 for _, k in items if k == '文档'),
        sum(1 for _, k in items if k == '其他')), flush=True)
    done = {}
    if os.path.exists(RESULTS):
        with open(RESULTS, encoding='utf-8') as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                    done[d['src']] = d
                except Exception:
                    continue
    todo = [it for it in items if it[0] not in done]
    print('already in results: %d; to process: %d' % (len(done), len(todo)), flush=True)
    if not todo:
        print('nothing to do', flush=True)
        return
    _hb(label='第五批分类（收件箱无 review 行）', kind='year-reclassify',
        phase='doc', status='running', total=len(todo), done=0)
    t0 = time.time()
    n_done = 0
    by_why = Counter()
    api_err = 0
    with open(RESULTS, 'a', encoding='utf-8') as fh:
        with ThreadPoolExecutor(max_workers=max(1, min(4, args.workers))) as ex:
            futs = {ex.submit(process_one, it, primary, fallback): it
                    for it in todo}
            for fut in as_completed(futs):
                rec = fut.result()
                fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
                fh.flush()
                n_done += 1
                by_why[rec.get('why')] += 1
                if rec.get('why') == 'doc-api_error':
                    api_err += 1
                if n_done % 50 == 0:
                    print('PROG %d/%d %.0fs why=%s' % (
                        n_done, len(todo), time.time() - t0, dict(by_why)), flush=True)
                    _hb(label='第五批分类（收件箱无 review 行）',
                        kind='year-reclassify', phase='doc', status='running',
                        total=len(todo), done=n_done)
    print('DONE doc/other %d in %.1fs why=%s api_error=%d' % (
        n_done, time.time() - t0, dict(by_why), api_err), flush=True)
    _hb(label='第五批分类（收件箱无 review 行）', kind='year-reclassify',
        phase='doc', status='done', total=len(todo), done=n_done)


if __name__ == '__main__':
    main()
