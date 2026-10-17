import sqlite3
import sys

path = sys.argv[1]
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' order by name")]
print("tables:", len(tables))
for table in tables:
    try:
        count = con.execute(f'select count(*) from "{table}"').fetchone()[0]
    except Exception as exc:  # pragma: no cover - diagnostic only
        print(f"  {table}: ERR {type(exc).__name__}")
        continue
    print(f"  {table}: {count}")
con.close()
