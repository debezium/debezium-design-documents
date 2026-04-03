import struct
import os
import re
import sqlite3

WAL_PATH = "wal_snapshot.bin"
DB_PATH  = "test.db"



def parse_create_table(sql):
    """
    Parse a CREATE TABLE sql string and return list of:
      (position, column_name, declared_type, is_pk, is_rowid_alias)

    is_rowid_alias = True when column is INTEGER PRIMARY KEY
                     meaning its value is the rowid itself
                     and it is omitted from the record body
    """
    columns = []

    # Strip CREATE TABLE name ( ... ) WITHOUT ROWID
    # Extract just the column definitions block
    inner = re.search(r'\((.+)\)', sql, re.DOTALL)
    if not inner:
        return columns

    body = inner.group(1)

    # Split by comma but not commas inside parentheses
    depth = 0
    current = []
    parts = []
    for ch in body:
        if ch == '(':
            depth += 1
            current.append(ch)
        elif ch == ')':
            depth -= 1
            current.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append(''.join(current).strip())

    pos = 0
    for part in parts:
        part = part.strip()

        # Skip table constraints (PRIMARY KEY (...), UNIQUE (...), etc.)
        upper = part.upper().lstrip()
        if (upper.startswith('PRIMARY KEY') or
            upper.startswith('UNIQUE') or
            upper.startswith('CHECK') or
            upper.startswith('FOREIGN KEY')):
            continue

        # Parse column: name type [constraints...]
        tokens = part.split()
        if len(tokens) < 1:
            continue

        col_name     = tokens[0].strip('"\'`[]')
        declared_type = tokens[1].upper() if len(tokens) > 1 else "TEXT"

        # Normalize to SQLite affinity groups
        if any(x in declared_type for x in ['INT']):
            affinity = 'INTEGER'
        elif any(x in declared_type for x in ['REAL','FLOA','DOUB']):
            affinity = 'REAL'
        elif any(x in declared_type for x in ['TEXT','CHAR','CLOB']):
            affinity = 'TEXT'
        elif 'BLOB' in declared_type or declared_type == '':
            affinity = 'BLOB'
        else:
            affinity = 'NUMERIC'

        # Check if this is INTEGER PRIMARY KEY (rowid alias)
        upper_part  = part.upper()
        is_pk       = 'PRIMARY KEY' in upper_part
        is_rowid_alias = (affinity == 'INTEGER' and is_pk)

        columns.append((pos, col_name, affinity, is_pk, is_rowid_alias))
        pos += 1

    return columns


def load_schema(db_path):
    """
    Read all table schemas from sqlite_master.
    Returns dict: { table_name -> [(pos, col_name, type, is_pk, is_rowid_alias)] }
    """
    conn   = sqlite3.connect(db_path)
    schema = {}
    rows   = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
    ).fetchall()
    conn.close()

    for name, sql in rows:
        cols = parse_create_table(sql)
        schema[name] = cols
        print(f"  Loaded schema for '{name}':")
        for pos, col, typ, pk, rowid_alias in cols:
            flags = []
            if pk:           flags.append("PK")
            if rowid_alias:  flags.append("rowid-alias")
            flag_str = f"  [{', '.join(flags)}]" if flags else ""
            print(f"    col[{pos}] {col:15s} {typ:10s}{flag_str}")

    return schema


# WAL HELPERS 

def read_varint(data, pos):
    result = 0
    for i in range(9):
        b = data[pos + i]
        if i == 8:
            result = (result << 8) | b
            return result, pos + 9
        result = (result << 7) | (b & 0x7F)
        if not (b & 0x80):
            return result, pos + i + 1
    return result, pos + 9


def decode_value(data, pos, serial_type, declared_type=None):
    if serial_type == 0:
        return None, pos
    elif serial_type == 1:
        v = struct.unpack_from(">b", data, pos)[0]
        return float(v) if declared_type == "REAL" else v, pos+1
    elif serial_type == 2:
        v = struct.unpack_from(">h", data, pos)[0]
        return float(v) if declared_type == "REAL" else v, pos+2
    elif serial_type == 3:
        raw = (data[pos]<<16)|(data[pos+1]<<8)|data[pos+2]
        if raw & 0x800000: raw -= 0x1000000
        return float(raw) if declared_type == "REAL" else raw, pos+3
    elif serial_type == 4:
        v = struct.unpack_from(">i", data, pos)[0]
        return float(v) if declared_type == "REAL" else v, pos+4
    elif serial_type == 5:
        raw = int.from_bytes(data[pos:pos+6], "big", signed=True)
        return float(raw) if declared_type == "REAL" else raw, pos+6
    elif serial_type == 6:
        v = struct.unpack_from(">q", data, pos)[0]
        return float(v) if declared_type == "REAL" else v, pos+8
    elif serial_type == 7:
        return struct.unpack_from(">d", data, pos)[0], pos+8
    elif serial_type == 8:
        return (0.0 if declared_type == "REAL" else 0), pos
    elif serial_type == 9:
        return (1.0 if declared_type == "REAL" else 1), pos
    elif serial_type >= 12 and serial_type % 2 == 0:
        length = (serial_type - 12) // 2
        return bytes(data[pos:pos+length]), pos+length
    elif serial_type >= 13 and serial_type % 2 == 1:
        length = (serial_type - 13) // 2
        return data[pos:pos+length].decode("utf-8", "replace"), pos+length
    return None, pos


def page_to_table(page_num, page_cache, db_path, page_size, schema):
    """
    Figure out which table a page belongs to by reading sqlite_master.
    SQLite stores table root page numbers in sqlite_master.
    """
    conn  = sqlite3.connect(db_path)
    rows  = conn.execute(
        "SELECT name, rootpage FROM sqlite_master WHERE type='table'"
    ).fetchall()
    conn.close()

    # Build map: rootpage -> table_name
    root_map = {rp: name for name, rp in rows}
    return root_map.get(page_num)


def decode_leaf_page(page_data, schema_cols):
    """
    Decode all rows from a table b-tree leaf page.
    Returns dict: { rowid -> { col_name -> value } }
    schema_cols = [(pos, col_name, type, is_pk, is_rowid_alias)]
    """
    if page_data[0] != 0x0D:
        return {}

    cell_count = struct.unpack_from(">H", page_data, 3)[0]
    rows       = {}

    for i in range(cell_count):
        cell_ptr         = struct.unpack_from(">H", page_data, 8 + i * 2)[0]
        pos              = cell_ptr
        payload_size, pos = read_varint(page_data, pos)
        rowid,        pos = read_varint(page_data, pos)

        hdr_start      = pos
        hdr_size,  pos = read_varint(page_data, pos)
        hdr_end        = hdr_start + hdr_size

        serial_types = []
        while pos < hdr_end:
            st, pos = read_varint(page_data, pos)
            serial_types.append(st)

        body_pos = hdr_end
        values   = {}

        for st_idx, st in enumerate(serial_types):
            if st_idx < len(schema_cols):
                _, col_name, declared_type, _, is_rowid_alias = schema_cols[st_idx]
            else:
                col_name      = f"col_{st_idx}"
                declared_type = None
                is_rowid_alias = False

            # INTEGER PRIMARY KEY is omitted from record body
            # its value IS the rowid
            if is_rowid_alias and st == 0:
                values[col_name] = rowid
                continue

            val, body_pos = decode_value(page_data, body_pos, st, declared_type)
            values[col_name] = val

        rows[rowid] = values

    return rows


def format_row(row_dict):
    return "{" + ", ".join(f"{k}: {repr(v)}" for k, v in row_dict.items()) + "}"


# ── MAIN ─────────────────────────────────────────────────────────

SCHEMA = load_schema(DB_PATH)


with open(WAL_PATH, "rb") as f:
    wal = f.read()

magic, version, page_size, seq = struct.unpack_from(">IIII", wal, 0)
salt1, salt2 = struct.unpack_from(">II", wal, 16)

print(f"  Magic:     0x{magic:08X}  {'OK' if magic == 0x377F0682 else 'INVALID!'}")
print(f"  Page size: {page_size} bytes")
print(f"  Salt-1:    0x{salt1:08X}")
print(f"  Salt-2:    0x{salt2:08X}")

FRAME_SIZE   = 24 + page_size
TOTAL_FRAMES = (len(wal) - 32) // FRAME_SIZE
print(f"  WAL size:  {len(wal)} bytes → {TOTAL_FRAMES} frames")

frames = []
offset = 32
for i in range(TOTAL_FRAMES):
    pg_num, commit, s1, s2 = struct.unpack_from(">IIII", wal, offset)[:4]
    page_data  = wal[offset+24 : offset+24+page_size]
    page_type  = page_data[0]
    valid      = (s1 == salt1 and s2 == salt2)
    frames.append({
        "index": i, "page_num": pg_num, "commit": commit,
        "valid": valid, "page_data": page_data, "page_type": page_type
    })
    type_str   = {0x0D:"TABLE LEAF", 0x05:"TABLE INTERIOR",
                  0x0A:"INDEX LEAF", 0x02:"INDEX INTERIOR"
                 }.get(page_type, f"0x{page_type:02X}")
    commit_str = f"COMMIT (size={commit})" if commit else "pending"
    print(f"  Frame {i}: page={pg_num:2d}  {commit_str:22s}  "
          f"type={type_str}  salts={'OK' if valid else 'MISMATCH'}")
    offset += FRAME_SIZE


transactions = []
current      = []
for f in frames:
    if not f["valid"]:
        print(f"  Frame {f['index']}: salt mismatch → checkpoint boundary")
        break
    current.append(f)
    if f["commit"] > 0:
        transactions.append(current)
        pages = [x["page_num"] for x in current]
        print(f"  Transaction {len(transactions):2d}: "
              f"frames {[x['index'] for x in current]}  pages={pages}")
        current = []

# Build rootpage -> table_name map from sqlite_master
conn      = sqlite3.connect(DB_PATH)
root_map  = {
    rp: name
    for name, rp in conn.execute(
        "SELECT name, rootpage FROM sqlite_master WHERE type='table'"
    ).fetchall()
}
conn.close()
print(f"\n  Root page map: {root_map}")

page_cache = {}

def get_old_page(page_num):
    if page_num in page_cache:
        return page_cache[page_num], "WAL cache"
    with open(DB_PATH, "rb") as f:
        f.seek((page_num - 1) * page_size)
        return f.read(page_size), ".db file"

for txn_idx, txn in enumerate(transactions):
    print(f"\n  ── Transaction {txn_idx + 1} ──────────────────────────")
    for frame in txn:
        pg         = frame["page_num"]
        table_name = root_map.get(pg)

        if frame["page_type"] != 0x0D:
            type_str = {0x05:"TABLE INTERIOR", 0x0A:"INDEX LEAF"
                       }.get(frame["page_type"], f"0x{frame['page_type']:02X}")
            print(f"  Page {pg}: type={type_str} → SKIP")
            page_cache[pg] = frame["page_data"]
            continue

        if table_name is None:
            print(f"  Page {pg}: unknown table → SKIP")
            page_cache[pg] = frame["page_data"]
            continue

        schema_cols         = SCHEMA.get(table_name, [])
        new_page            = frame["page_data"]
        old_page, old_src   = get_old_page(pg)

        old_rows = decode_leaf_page(old_page, schema_cols)
        new_rows = decode_leaf_page(new_page, schema_cols)

        print(f"\n  Page {pg} → table='{table_name}'  (old from: {old_src})")

        if old_rows:
            print("  OLD rows:")
            for rid, vals in sorted(old_rows.items()):
                print(f"    rowid={rid}  {format_row(vals)}")
        else:
            print("  OLD rows: (empty)")

        if new_rows:
            print("  NEW rows:")
            for rid, vals in sorted(new_rows.items()):
                print(f"    rowid={rid}  {format_row(vals)}")
        else:
            print("  NEW rows: (empty)")

        all_rowids = sorted(set(old_rows) | set(new_rows))
        events     = []
        for rid in all_rowids:
            if rid not in old_rows:
                events.append(("INSERT", rid, None,          new_rows[rid]))
            elif rid not in new_rows:
                events.append(("DELETE", rid, old_rows[rid], None))
            elif old_rows[rid] != new_rows[rid]:
                events.append(("UPDATE", rid, old_rows[rid], new_rows[rid]))

        if events:
            print("\n  EVENTS:")
            for op, rid, before, after in events:
                if op == "INSERT":
                    print(f"    INSERT  rowid={rid}  after={format_row(after)}")
                elif op == "DELETE":
                    print(f"    DELETE  rowid={rid}  before={format_row(before)}")
                elif op == "UPDATE":
                    print(f"    UPDATE  rowid={rid}")
                    print(f"      before: {format_row(before)}")
                    print(f"      after:  {format_row(after)}")
        else:
            print("\n  EVENTS: none (structural change only)")

        page_cache[pg] = new_page
