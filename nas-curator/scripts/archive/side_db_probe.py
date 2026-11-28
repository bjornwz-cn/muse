import sqlite3, json
c = sqlite3.connect("/data/nas-curator.db")
for pat in ("%待整理%", "%收件箱-邮件归档%"):
    rows = c.execute(
        "SELECT status, COUNT(*) FROM review_queue "
        "WHERE json_extract(payload,'$.src') LIKE ? GROUP BY status", (pat,)).fetchall()
    print(pat, rows)
print("total review rows:", c.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0])
print("max plan id:", c.execute("SELECT MAX(id) FROM plans").fetchone()[0])
print("plans tail:", c.execute("SELECT id, library, status FROM plans ORDER BY id DESC LIMIT 3").fetchall())
# sample payload keys for these sources
r = c.execute(
    "SELECT id, kind, status, payload FROM review_queue "
    "WHERE json_extract(payload,'$.src') LIKE '%待整理%' LIMIT 3").fetchall()
for row in r:
    d = json.loads(row[3])
    print("sample rq:", row[0], row[1], row[2], "keys:", sorted(d.keys()))
    print("  src:", d.get("src"))
print("files table cols:", [x[1] for x in c.execute("PRAGMA table_info(files)")])
print("files rows for sources:",
      c.execute("SELECT COUNT(*) FROM files WHERE path LIKE '%待整理%'").fetchone()[0],
      c.execute("SELECT COUNT(*) FROM files WHERE path LIKE '%收件箱-邮件归档%'").fetchone()[0])
