#!/usr/bin/env python3
"""登记后核查：doc-remaining 行数、所属方分布、凭据/加密行未动、api_error 行保留旧结论。"""
import sqlite3, json
from collections import Counter

c = sqlite3.connect("/data/nas-curator.db")
print("integrity:", c.execute("PRAGMA integrity_check").fetchall())
print("status:", [r for r in c.execute(
    "SELECT status,count(*) FROM review_queue GROUP BY status")])
print("source doc-remaining:",
      c.execute("SELECT count(*) FROM review_queue "
                "WHERE json_extract(payload,'$.source')='doc-remaining'").fetchone()[0])
print("decided_by doc-remaining:",
      c.execute("SELECT count(*) FROM review_queue "
                "WHERE decided_by='doc-remaining'").fetchone()[0])
parties = Counter()
for (p,) in c.execute(
        "SELECT payload FROM review_queue "
        "WHERE json_extract(payload,'$.source')='doc-remaining'"):
    parties[json.loads(p).get("doc_party", "")] += 1
print("doc_party dist among doc-remaining:", dict(parties))
print("doc_model dist:",
      Counter(json.loads(p).get("doc_model", "") for (p,) in c.execute(
          "SELECT payload FROM review_queue "
          "WHERE json_extract(payload,'$.source')='doc-remaining'")))
print("cred rows (id, decided_by, source):",
      [r for r in c.execute(
          "SELECT id, decided_by, json_extract(payload,'$.source') "
          "FROM review_queue WHERE id IN (138941,138943,139426,139428,139432)")])
print("encrypted sample 138008:",
      c.execute("SELECT decided_by, json_extract(payload,'$.source') "
                "FROM review_queue WHERE id=138008").fetchone())
print("api_error row 137963:",
      c.execute("SELECT decided_by, json_extract(payload,'$.source'), "
                "json_extract(payload,'$.doc_party'), json_extract(payload,'$.reason') "
                "FROM review_queue WHERE id=137963").fetchone())
print("sample determined row:",
      c.execute("SELECT id, json_extract(payload,'$.doc_party'), "
                "json_extract(payload,'$.doc_subdir'), json_extract(payload,'$.doc_model') "
                "FROM review_queue WHERE json_extract(payload,'$.source')='doc-remaining' "
                "AND json_extract(payload,'$.doc_party')='2-上海润心' LIMIT 3").fetchall())
