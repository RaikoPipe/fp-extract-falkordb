import sqlite3
c = sqlite3.connect('/app/data/chainlit.db').cursor()
c.execute("SELECT name FROM sqlite_master WHERE type='table'")
print("TABLES:", [r[0] for r in c.fetchall()])
print()
for t in ("steps", "messages", "threads", "elements"):
    try:
        c.execute(f"SELECT * FROM {t} ORDER BY ROWID DESC LIMIT 5")
        cols = [d[0] for d in c.description]
        print(f"=== {t} (cols={cols}) ===")
        for r in c.fetchall():
            print(r)
        print()
    except Exception as e:
        print(f"{t}: {e}")