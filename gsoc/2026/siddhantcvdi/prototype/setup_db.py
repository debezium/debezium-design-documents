import sqlite3
import os

DB_PATH  = "test.db"
WAL_PATH = "test.db-wal"

# Clean slate
for f in [DB_PATH, WAL_PATH, "test.db-shm", "wal_snapshot.bin"]:
    if os.path.exists(f):
        os.remove(f)
        print(f"Removed old {f}")

conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA page_size=4096")
conn.execute("PRAGMA wal_autocheckpoint=0")  # keep WAL alive
conn.execute("""
    CREATE TABLE orders (
        id     INTEGER PRIMARY KEY,
        item   TEXT    NOT NULL,
        amount REAL    NOT NULL,
        desc   TEXT    NOT NULL 
    )
""")
conn.commit()
print("Created DB in WAL mode, auto-checkpoint disabled")

# HOLD WAL OPEN 
# A reader holding BEGIN prevents SQLite from checkpointing on close
reader = sqlite3.connect(DB_PATH)
reader.execute("PRAGMA journal_mode=WAL")
reader.execute("BEGIN")

# TRANSACTION 1: INSERT 
conn.execute("INSERT INTO orders VALUES (1, 'apple',  50.0, 'dewdew')")
conn.execute("INSERT INTO orders VALUES (2, 'banana', 30.0, 'dwdew')")
conn.execute("INSERT INTO orders VALUES (3, 'cherry', 99.5, 'dewdew')")
conn.commit()
print(f"\nT1 (INSERT 3 rows) done")
print(f"  data_version = {conn.execute('PRAGMA data_version').fetchone()[0]}")

# TRANSACTION 2: UPDATE 
conn.execute("UPDATE orders SET amount = 200.0 WHERE id = 2")
conn.commit()
print(f"\nT2 (UPDATE banana 30->200) done")
print(f"  data_version = {conn.execute('PRAGMA data_version').fetchone()[0]}")

# TRANSACTION 3: DELETE 
conn.execute("DELETE FROM orders WHERE id = 3")
conn.commit()
print(f"\nT3 (DELETE cherry) done")
print(f"  data_version = {conn.execute('PRAGMA data_version').fetchone()[0]}")

# FINAL STATE 
print("\nFinal DB state:")
for row in conn.execute("SELECT rowid, * FROM orders"):
    print(f"  {row}")

#  SAVE WAL SNAPSHOT 
if os.path.exists(WAL_PATH):
    with open(WAL_PATH, "rb") as f:
        data = f.read()
    with open("wal_snapshot.bin", "wb") as f:
        f.write(data)
    print(f"\nWAL snapshot saved ({len(data)} bytes)")
    print(f"File sizes:")
    for f in [DB_PATH, WAL_PATH]:
        print(f"  {f}: {os.path.getsize(f)} bytes")
else:
    print("\nERROR: WAL file not found!")

reader.execute("COMMIT")
reader.close()
conn.close()