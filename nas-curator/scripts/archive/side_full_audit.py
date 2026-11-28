import json, collections
rows=[json.loads(l) for l in open("/data/side_classify_full.jsonl")]
mv=[r for r in rows if r.get("decision")=="move"]
rv=[r for r in rows if r.get("decision")=="review"]
s304=[r for r in mv if r["party"]=="1-王震" and r.get("subdir")=="14-工具与其他"]
print("304 by src_kind:", collections.Counter(r["src_kind"] for r in s304))
print("304 by ext:", collections.Counter(r["ext"] for r in s304))
for r in s304[:6]:
    print("  304:", r["src"].split("/")[-1], "|", (r.get("reason") or "")[:70])
print("1-王震/12-项目资料:", [(r["src"].split("/")[-1], r["src_kind"]) for r in mv if r["party"]=="1-王震" and r.get("subdir")=="12-项目资料"])
print("mail moves by (party,subdir):", collections.Counter((r["party"], r.get("subdir")) for r in mv if r["ext"]==".eml").most_common(12))
print("mail review count:", sum(1 for r in rv if r["ext"]==".eml"))
print("1-王震 moves by src_kind:", collections.Counter(r["src_kind"] for r in mv if r["party"]=="1-王震"))
print("review 无法判断 count:", sum(1 for r in rv if (r.get("review_reason") or "").startswith("无法判断")))
print("review 低置信 count:", sum(1 for r in rv if (r.get("review_reason") or "").startswith("低置信")))
print("review blank/solid hints:", sum(1 for r in rv if any(k in (r.get("review_reason") or "") for k in ["纯白","全白","全黑","纯色","纯黑","空白","纯灰","纯蓝","纯红","纯粉","纯橙","纯黄","纯绿"])))
