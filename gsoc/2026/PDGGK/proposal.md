## Abstract

An IoT gateway caches sensor readings in SQLite during a network outage. When it reconnects, the developer maintaining that fleet should not have to invent a custom sync pipeline just to get those changes into Kafka. Yet today there is no way to capture changes from SQLite databases using Debezium — the leading CDC framework in the Java ecosystem — despite SQLite being the most widely deployed database engine in the world (over a trillion active deployments according to sqlite.org). This project builds a Debezium Source Connector for SQLite that reads the Write-Ahead Log (WAL) to detect committed changes, reconstructs row-level events from page-level WAL frames by decoding SQLite's b-tree page format, and emits standard Debezium change events (Envelope with before/after/source/op fields) that flow into Kafka, Pulsar, or any Debezium Server sink.

SQLite's WAL was designed for local concurrency, not replication. Unlike PostgreSQL's logical decoding or MySQL's binlog, SQLite WAL frames are physical page images — not logical row operations. The connector must parse WAL frame headers to identify committed transactions, decode b-tree leaf pages to extract row data, diff page states to determine which rows were inserted, updated, or deleted, and handle edge cases including WITHOUT ROWID tables, overflow pages, and WAL checkpoint/reset cycles. The offset tracking design uses WAL salt values combined with commit frame positions as a resume token, addressing the fundamental challenge that WAL frame indices are non-monotonic (they reset on checkpoint). Schema evolution is tracked via `PRAGMA schema_version`, following the MySQL connector's pattern of not embedding schema in every CDC event, based on comparative analysis of MySQL, SQL Server, and PostgreSQL connectors' schema handling strategies.

I have engaged directly with the project mentor: Giovanni Panice responded to my technical email with three specific design answers (no convention for non-monotonic offsets — design your own; check MySQL connector for schema evolution; use SQLite read-lock for snapshot-to-stream handoff) and directed me to "read the guideline and propose a draft pull request." I have an open PR (debezium-platform#309) and 12 merged PRs across 5 open-source organizations including Apache Beam, IoTDB, ShardingSphere, Iceberg, and OpenCV. I have built the Debezium codebase locally and run the PostgreSQL connector test suite.

**Zulip introduction:** [Zihan - SQLite Source Connector](https://debezium.zulipchat.com/#narrow/channel/573881-community-gsoc/topic/Zihan.20-.20SQLite.20Source.20Connector)

---

**At a glance:**

This project matters to me because it replaces fragile one-off sync code in mobile and edge systems with a standard Debezium pipeline, and solving it requires the kind of low-level WAL decoding work I enjoy.

- **Mentor engagement**: Giovanni Panice provided three specific design answers that shaped this proposal's architecture
- **Working PoC**: Java WAL reader + b-tree page decoder, 11/11 tests passing, ~50K frames/sec throughput ([sqlite-wal-poc](https://github.com/PDGGK/sqlite-wal-poc))
- **Open-source track record**: 12 merged PRs across Apache Beam, IoTDB, ShardingSphere, Iceberg, OpenCV; open PR in debezium-platform
- **Offset design**: Salt-based epoch offset token that survives WAL checkpoint/reset — after studying all existing Debezium connectors, none handle non-monotonic offsets
- **Fallback plan**: If full page decoding proves too complex by Week 4, switch to hybrid WAL-detection + JDBC-read approach that still produces valid Debezium events

---


## Detailed Description

### Problem Statement

Debezium provides production-grade CDC connectors for PostgreSQL, MySQL, MongoDB, SQL Server, Oracle, and other databases. Each connector taps into the database's native change stream mechanism — PostgreSQL's logical replication slots, MySQL's binlog, MongoDB's oplog/change streams — to capture row-level changes with at-least-once delivery guarantees and resume capability.

SQLite has no equivalent mechanism. It is an embedded database with no server process, no replication protocol, and no built-in change subscription API. But SQLite's ubiquity means there are real use cases for CDC:

- **Edge-to-cloud sync**: Mobile and IoT applications use SQLite locally and need to replicate changes to central systems
- **Embedded analytics**: Applications with SQLite backends need to stream changes into data warehouses
- **Audit and compliance**: Regulatory requirements for change tracking apply to SQLite-backed systems too
- **Legacy migration**: Organizations moving from SQLite to server databases need change capture during transition

The existing approaches to SQLite change tracking each have significant limitations:

| Approach | Mechanism | Limitations |
|----------|-----------|-------------|
| Trigger-based | `CREATE TRIGGER` writing to changelog table | Requires DDL on target database; triggers can be dropped; performance overhead on every write; application must cooperate |
| Session extension | `sqlite3session` API (`sqlite3changeset`) | Requires application to explicitly register tables; C API only; not available in all SQLite builds |
| `update_hook` / `preupdate_hook` | C callback on data changes | In-process only; requires linking into application; no persistence across restarts |
| Polling | Periodic full-table scans | No change granularity; high overhead; misses intermediate states |
| Litestream | WAL-frame streaming to S3/GCS | Replication only (not row-level CDC); no change event semantics; no schema awareness |

**CDC state of the art in the SQLite ecosystem:** Beyond these basic approaches, several projects have explored SQLite change capture at different levels. Litestream validates that external WAL monitoring is viable by taking over checkpoint control and streaming page images to S3, but operates at page-level granularity without row-level semantics. LiteFS (Fly.io) uses FUSE filesystem interception to capture transaction-level page diffs, but is a replication tool, not a CDC tool. rqlite 9.0 implements true row-level CDC using pre-update hooks with Raft-indexed events and at-least-once delivery, but requires running rqlite's distributed SQLite -- it cannot monitor a standalone SQLite file. Turso/libSQL has native CDC built into their Rust-based SQLite fork (`PRAGMA unstable_capture_data_changes_conn`), writing to a `turso_cdc` table with before/after BLOBs, but requires a modified SQLite engine. Marmot publishes Debezium-format events from SQLite using trigger-based capture, proving demand for SQLite CDC in the Debezium ecosystem. The trigger-based sqlite-cdc project (Go) measures 97-127% write overhead on small tables. CRDT approaches (cr-sqlite, Corrosion) solve multi-writer sync but not CDC-to-Kafka.

The WAL-based approach proposed here occupies a unique position: it is the only approach that simultaneously provides external monitoring (no application modification), row-level change events, Debezium format compatibility, schema awareness, and zero write overhead on the source database. The trade-off is implementation complexity -- decoding physical page images into logical row operations requires b-tree page parsing, which no existing SQLite CDC tool attempts externally. The salt-based offset tracking in this design is analogous to Litestream's generation concept (new random ID when WAL continuity breaks), a proven pattern for handling WAL reset.

None of these produce Debezium-compatible change events. None provide log-based CDC for unmodified SQLite databases. A WAL-based Debezium connector addresses these gaps: it reads the WAL file externally (no application changes required), reconstructs row-level change events, and plugs into the existing Debezium/Kafka Connect infrastructure.

#### v1 Scope and Non-Goals

The GSoC v1 connector targets the common case. Explicit non-goals and limitations for the initial release:

| Category | Supported (v1) | Not Supported (v1) |
|----------|----------------|---------------------|
| Journal mode | WAL mode only | DELETE, TRUNCATE, PERSIST, MEMORY, WAL2 (proposed) |
| Table type | Rowid tables (`INTEGER PRIMARY KEY` or implicit `rowid`) | `WITHOUT ROWID` tables (stretch goal), virtual tables (FTS5, rtree), shadow tables |
| Operations | INSERT, UPDATE, DELETE | `VACUUM`, `REINDEX`, `auto_vacuum` (treated as WAL reset) |
| Schema changes | `ALTER TABLE ADD COLUMN`, `CREATE TABLE`, `DROP TABLE` | `ALTER TABLE RENAME COLUMN` (SQLite 3.25+), multiple DDLs between polls |
| Databases | Single database file | `ATTACH`-ed databases, in-memory databases |
| Payload size | Standard cells (payload fits in local page) | Overflow pages (stretch goal — any cell whose payload exceeds the local capacity, not limited to TEXT/BLOB). v1 detects overflow via payload size check and skips with a warning |
| Restart | Resume from stored offset if WAL unchanged | Gap-free recovery after checkpoint truncation during downtime (see Offset Management) |

These limitations are clearly documented in the connector's user guide and logged at startup.

### Proposed Solution

#### Design Overview

The SQLite connector follows the same architecture as existing Debezium relational database connectors (modeled on the PostgreSQL connector after studying all existing connector implementations). The `SqliteConnectorTask` creates a `ChangeEventSourceCoordinator` that orchestrates the two-phase lifecycle: snapshot first, then streaming. The coordinator receives change event sources from `SqliteChangeEventSourceFactory` and manages the transition between phases, offset commits, and error handling -- the same pattern used by PostgreSQL, MySQL, and SQL Server connectors.

The connector operates in two phases:

1. **Snapshot phase**: Acquires a SQLite read transaction (providing snapshot isolation under WAL mode), performs a full table scan of all configured tables, and emits `READ` events for each existing row. The read transaction's WAL position establishes the handoff point for streaming.

2. **Streaming phase**: Polls the WAL file for new committed frames, decodes b-tree pages to reconstruct row-level changes, and emits `CREATE`, `UPDATE`, or `DELETE` events. The offset tracks WAL identity (salt values) and commit frame position for resumable streaming across WAL checkpoint/reset cycles.

#### WAL-Based Change Detection



![WAL Frame Format — Header + Frame Structure](d2/wal-frame-format-part1.png)

![WAL Frame Format — Commit Boundary](d2/wal-frame-format-part2.png)



SQLite's WAL file consists of a 32-byte header followed by a sequence of frames. Each frame contains a 24-byte frame header and a full database page image:

```
WAL File Layout:
+-------------------------------------+
| WAL Header (32 bytes)               |
|   magic, format, page_size,         |
|   checkpoint_seq, salt-1, salt-2,   |
|   checksum-1, checksum-2            |
+-------------------------------------+
| Frame 1: Header (24B) + Page Image  |
|   page_number, commit_size,         |
|   salt copy, checksum               |
+-------------------------------------+
| Frame 2: Header (24B) + Page Image  |
+-------------------------------------+
| ...                                 |
+-------------------------------------+
```

The connector's WAL reader:

1. **Opens the WAL file** and validates the header (magic number `0x377f0682` for little-endian checksums or `0x377f0683` for big-endian; the LSB determines checksum byte order)
2. **Reads frames sequentially** starting at byte offset `32 + (N-1) * (pageSize + 24)` for frame N, validating each frame's salt copies against the WAL header salts
3. **Validates frame checksums**: SQLite uses a cumulative checksum algorithm where each frame's checksum pair `(s0, s1)` is computed over the frame header's first 8 bytes plus the full page data, seeded from the previous frame's checksum (or the WAL header checksum for the first frame). The algorithm processes 32-bit words: `s0 += word[i] + s1; s1 += word[i+1] + s0`. A frame is valid only if its computed checksum matches the stored checksum AND its salt copies match the WAL header salts — these are two independent checks. Salt mismatch indicates WAL reset; checksum mismatch indicates corruption or end-of-valid-data
4. **Identifies commit boundaries** via the `commit_size` field in frame headers (non-zero `commit_size` = last frame of a committed transaction; the value indicates the total database size in pages after this commit). Note: under `synchronous=FULL`, SQLite may zero-pad the WAL to fill the remainder of a disk sector after the commit frame. These zero-filled regions naturally fail checksum validation in step 3, so the frame-reading loop skips them without special handling
5. **Buffers frames per transaction** until a commit frame is seen, then processes the entire transaction atomically
6. **Decodes b-tree pages** within committed frames to extract row-level data
7. **Detects WAL reset**: if salt values in a new WAL header read differ from the stored salts, the WAL has been reset. WAL reset occurs when a writer starts a new WAL generation — this happens after a TRUNCATE or RESTART mode checkpoint, or when a writer recycles the WAL after a complete PASSIVE checkpoint if no readers hold `WAL_READ_LOCK(N>0)`. Salts change on reset (salt-1 is incremented, salt-2 is randomized). The connector increments its epoch counter and restarts reading from frame 1 with the new salts

**Commit detection logic:**

```java
public class WalReader {
    private final RandomAccessFile walFile;
    private final int pageSize;
    private long currentOffset;
    private int salt1, salt2;

    public List<WalTransaction> readNewTransactions() {
        List<WalFrame> pendingFrames = new ArrayList<>();
        List<WalTransaction> committed = new ArrayList<>();

        while (hasMoreFrames()) {
            WalFrame frame = readFrame(currentOffset);
            if (!frame.saltsMatch(salt1, salt2)) {
                break; // salt mismatch: WAL reset or end of valid data
            }
            if (!frame.validateChecksum(prevChecksum)) {
                break; // checksum mismatch: corruption or end of valid data
            }
            pendingFrames.add(frame);

            if (frame.commitSize > 0) {
                // Non-zero commit_size = last frame of committed txn
                committed.add(new WalTransaction(
                    List.copyOf(pendingFrames), frame.commitSize));
                pendingFrames.clear();
            }
            currentOffset += FRAME_HEADER_SIZE + pageSize;
        }
        return committed;
    }
}
```

#### B-Tree Page Decoding

WAL frames contain raw database page images. To extract row-level changes, the connector decodes SQLite's b-tree page format:

```
B-Tree Leaf Page Layout:
+------------------------------+
| Page Header (8 or 12 bytes)  |
|   page_type (0x0D = leaf)    |
|   first_free_block           |
|   cell_count                 |
|   cell_content_offset        |
|   fragmented_free_bytes      |
+------------------------------+
| Cell Pointer Array           |
|   (2 bytes per cell, sorted) |
+------------------------------+
| Unallocated Space            |
+------------------------------+
| Cell Content Area            |
|   Cell N: [size][rowid][data]|
|   ...                        |
|   Cell 1: [size][rowid][data]|
+------------------------------+
```

Each cell in a leaf table b-tree page contains:
- **Payload size** (varint): total bytes of payload
- **Row ID** (varint): the implicit `rowid` value. Note: `rowid` is the integer primary key ONLY when a table declares `INTEGER PRIMARY KEY` — otherwise it is an internal alias. For tables without an explicit `INTEGER PRIMARY KEY`, the connector uses the `rowid` as row identity for diffing but does not expose it as a user column
- **Record header**: serial type codes for each column
- **Record body**: column values encoded per serial type

The connector's page decoder:

```java
public class BTreePageDecoder {
    public List<RowData> decodeLeafPage(byte[] pageImage, boolean isFirstPage) {
        // Page 1 has a 100-byte database file header before the b-tree header
        int hdrOffset = isFirstPage ? 100 : 0;
        int pageType = pageImage[hdrOffset] & 0xFF;
        if (pageType != 0x0D) return Collections.emptyList(); // not a leaf

        int cellCount = readUint16(pageImage, hdrOffset + 3);
        List<RowData> rows = new ArrayList<>(cellCount);

        for (int i = 0; i < cellCount; i++) {
            int cellPtr = readUint16(pageImage, hdrOffset + 8 + i * 2);
            RowData row = decodeCellAt(pageImage, cellPtr);
            rows.add(row);
        }
        return rows;
    }

    private RowData decodeCellAt(byte[] page, int offset) {
        VarInt payloadSize = readVarint(page, offset);
        VarInt rowId = readVarint(page, offset + payloadSize.bytesRead);
        int headerStart = offset + payloadSize.bytesRead + rowId.bytesRead;
        // Decode serial types and column values from record format
        return decodeRecord(page, headerStart, payloadSize.value, rowId.value);
    }
}
```

**Change detection strategy**: A naive per-page diff is unsound for b-tree storage — rows can move between pages during b-tree splits, merges, rebalancing, or `VACUUM`. Comparing old vs. new images of a single page would produce spurious DELETE+INSERT pairs for rows that merely moved.

The connector therefore performs **transaction-wide reconciliation**: for each committed transaction, it collects ALL modified pages (every WAL frame in the commit), groups them by their table root page. Page-to-table mapping is built by walking from known root pages (from `sqlite_schema.rootpage`) down through the b-tree structure, since SQLite does not provide a general child-to-root mapping (ptrmap pages exist only in auto-vacuum databases). The connector builds this mapping on startup and updates it when schema changes occur, and builds a complete row set keyed by `rowid`. To walk the b-tree, the connector needs both modified pages (from the current transaction's WAL frames) and pages not modified by this transaction. For these unmodified pages, the connector must resolve them against ALL previously committed WAL frames (not just the main database file), since earlier transactions may have committed pages that have not yet been checkpointed. The connector maintains a page cache that overlays: (1) main `.db` file pages as the base, (2) all previously committed WAL frame pages on top, (3) current transaction's WAL frame pages on top of that — mirroring SQLite's own page resolution logic via `mxFrame`. The resulting transaction-wide view is compared against the cached row set for that table:

- Rows present in new set but not in cache → `INSERT`
- Rows present in both but with different column values → `UPDATE`
- Rows present in cache but not in new set → `DELETE`

For `UPDATE` events, the cached state provides the `before` image and the new state provides the `after` image, producing full Debezium Envelope events. After processing, the cache is updated to reflect the post-transaction state. Because the diff is keyed by `rowid` (not page location), b-tree splits and merges do not produce spurious events.

**v1 limitation**: The initial implementation only diffs pages that appear in the current WAL transaction. If a b-tree split moves rows to a page NOT in the WAL (which should not happen for a correctly formed transaction since SQLite writes all modified pages), the connector logs a warning and triggers a table-level re-scan from the database file as a fallback.

**Data-state recovery on restart**: The in-memory cached row set is lost when the connector restarts. On restart, the connector must rebuild the cache before it can produce correct `before` images or detect `DELETE` events. The recovery strategy: after loading the stored offset and establishing the schema via `SchemaHistory` replay, the connector reads the current state of all tracked tables via JDBC `SELECT` to rebuild the row cache. The first batch of WAL transactions after restart uses this JDBC-derived baseline as the "old" state for diffing. This means the very first `UPDATE` after restart will have a correct `before` image (from the JDBC read), but if any rows were deleted while the connector was offline and the WAL was not truncated, those `DELETE` events are emitted when the connector processes the WAL frames containing the delete transactions. If WAL was truncated during downtime, `wal.gap.policy` applies.

#### WITHOUT ROWID Table Handling

Tables declared with `WITHOUT ROWID` use a clustered index (index b-tree, page type `0x0A`) instead of a table b-tree. The primary key values serve as the index key, and all other columns are stored in the index leaf pages. Detecting WHETHER a page belongs to a `WITHOUT ROWID` table cannot be done from the page type byte alone — ordinary index pages also use `0x0A`. The connector resolves this by building a root-page mapping from `sqlite_schema`: for each table, it records `rootpage` and whether the table is `WITHOUT ROWID` (detected by parsing the `CREATE TABLE` SQL in `sqlite_schema.sql`). When processing WAL frames, the connector traces each modified page back to its root page to determine whether it belongs to a rowid table or a `WITHOUT ROWID` table, then applies the appropriate cell format.

**v1 scope**: `WITHOUT ROWID` table support is a stretch goal. The initial implementation targets rowid tables only and logs a warning when it encounters pages belonging to `WITHOUT ROWID` tables.

#### Offset Management

SQLite's WAL frame indices are non-monotonic — they reset to zero when the WAL is checkpointed and reset. A simple frame number is insufficient as a resume token. The connector's offset encodes:

```java
public class SqliteOffsetContext extends CommonOffsetContext<SqliteSourceInfo> {
    private int walSalt1;           // WAL header salt-1 (incremented on WAL reset)
    private int walSalt2;           // WAL header salt-2 (new random value on WAL reset)
    private long walEpoch;          // monotonic counter: incremented on each WAL reset
    private long walFrameOffset;    // byte offset of last processed commit frame
    private int schemaVersion;      // PRAGMA schema_version at last event
    private Instant timestamp;      // last event timestamp
    // snapshotCompleted inherited from CommonOffsetContext

    @Override
    public Map<String, ?> getOffset() {
        return Map.of(
            "wal_salt1", walSalt1,
            "wal_salt2", walSalt2,
            "wal_epoch", walEpoch,       // solves non-monotonic problem
            "wal_offset", walFrameOffset,
            "schema_version", schemaVersion,
            "ts_ms", timestamp.toEpochMilli(),
            "snapshot_completed", snapshotCompleted
        );
    }

    /** Inner class for deserializing stored offsets on restart */
    public static class Loader implements OffsetContext.Loader<SqliteOffsetContext> {
        @Override
        public SqliteOffsetContext load(Map<String, ?> offset) {
            // Reconstruct from stored map, handling null for first-time start
            return new SqliteOffsetContext(
                (Integer) offset.get("wal_salt1"),
                (Integer) offset.get("wal_salt2"),
                (Long) offset.get("wal_epoch"),
                (Long) offset.get("wal_offset"),
                (Integer) offset.get("schema_version"),
                Instant.ofEpochMilli((Long) offset.get("ts_ms")),
                (Boolean) offset.get("snapshot_completed")
            );
        }
    }
}
```

The `walEpoch` field is what makes offset comparison simple: it is a monotonically increasing counter that the connector increments every time it detects a WAL reset (salt values changed). This makes offset comparison trivial — `(epoch=5, offset=100)` is always after `(epoch=4, offset=99999)` — and is used by the `SqliteHistoryRecordComparator` during schema history recovery.

**Resume logic**: On restart, the connector reads the stored offset and compares the WAL salts:
- **Salts match**: Resume reading from `walFrameOffset` (WAL has not been reset since last read)
- **Salts differ**: WAL was reset (checkpoint occurred). Increment `walEpoch` and resume from the beginning of the new WAL. **Hard limitation**: any WAL frames that were checkpointed and truncated while the connector was offline are permanently lost — the connector cannot reconstruct the intermediate row-level changes from the database file alone. In this case, the connector logs a `WARN` with the gap range `(old_epoch:old_offset → new_epoch:0)`. Operators can configure `wal.gap.policy` to either `warn` (continue with potential data gap — default), `fail` (stop the connector and require manual intervention), or `resnapshot` (trigger a full re-snapshot to re-establish consistency). Note: the connector does NOT emit a synthetic `TRUNCATE` event for WAL gaps — Debezium's `op='t'` is reserved for actual SQL `TRUNCATE TABLE` operations, and reusing it for stream discontinuity would overload its semantics. Instead, the gap is signaled through Debezium's notification channel and connector status reporting
- **No stored offset**: Start with a full snapshot

#### Schema Evolution

After comparing MySQL, SQL Server, and PostgreSQL connectors' schema handling strategies, the connector uses `HistorizedRelationalDatabaseSchema` (the same base class as MySQL and SQL Server connectors, NOT the non-historized `RelationalDatabaseSchema` that PostgreSQL uses). This is necessary because SQLite's WAL frames do not carry schema type information, so on restart the connector must replay schema history to know what the table schemas looked like at arbitrary historical points.

The approach follows the **SQL Server connector pattern** — schema detection via metadata comparison rather than DDL parsing:

1. On startup, recover schema state from `SchemaHistory` store (file, Kafka topic, or JDBC — same backends MySQL uses). If no history exists, read current schema from `sqlite_schema` and record a full schema snapshot as the baseline `TableChanges` entry at the current source position
2. Before processing each WAL transaction, check `PRAGMA schema_version` (an integer that SQLite auto-increments on DDL; note it can be manually set backward via `PRAGMA schema_version=N`, but this is rare in practice)
3. If version changed:
   - Query `sqlite_schema` for the full `CREATE TABLE` SQL and column metadata via `PRAGMA table_info()`
   - Diff against cached schema to identify added/dropped/altered columns
   - Record a complete `TableChanges` snapshot at the exact current source position `(walEpoch, walFrameOffset)` — not just a diff, because if multiple DDLs occurred between polls, intermediate states are lost. The full snapshot lets recovery reconstruct schema at any recorded position
   - Create a `SchemaChangeEvent` (with DDL text from `sqlite_schema.sql` column — SQLite stores the original `CREATE TABLE` statement)
   - Call `applySchemaChange()` which both updates in-memory schema AND records to `SchemaHistory` for recovery
   - Optionally emit to a schema change topic (controlled by `include.schema.changes` config, default true)
4. Schema is NOT embedded in every data change event — only schema change events carry the new schema definition. The `getDdlParser()` method returns `null` (following SQL Server's pattern — no DDL parsing needed since we detect changes via metadata comparison)

The `SchemaHistory` store enables the critical recovery scenario: if the connector restarts and needs to decode WAL frames that were written under an older schema, it replays history entries to find the latest `TableChanges` snapshot at or before the target offset position, using a custom `SqliteHistoryRecordComparator` that compares by WAL epoch and frame offset.

**v1 limitation**: If concurrent DDL occurs while WAL frames are being processed (e.g., `ALTER TABLE` between two commits in the same batch), the connector detects the schema version change but can only observe the final schema state. Intermediate schema states are not captured. For v1, the connector logs a warning if `schema_version` jumps by more than 1 between polls and recommends that operators avoid concurrent DDL during active streaming, or reduce the poll interval.



![Schema Evolution Flow](d2/schema-evolution.png)



#### Snapshot-to-Stream Handoff

Based on analysis of SQLite's WAL-mode locking semantics, the handoff uses SQLite's WAL-mode snapshot isolation:

1. **Begin read transaction** (`BEGIN DEFERRED` followed by a read from any table, e.g. `SELECT count(*) FROM sqlite_master`) — the snapshot is pinned at the point of the first database read. Note: `BEGIN IMMEDIATE` starts a write transaction and is NOT appropriate here; the connector needs a read transaction for snapshot isolation
2. **Record WAL end mark** — the connector's JDBC connection acquires a `WAL_READ_LOCK(N)` on one of the wal-index read-mark slots (via the `-shm` file). The read-mark value is guaranteed to be `<= mxFrame` at the time the lock was acquired, but is not necessarily equal to the reader's exact `mxFrame`. The exact mechanism for exposing the snapshot boundary to Java code is an open design question (see below). While the read transaction is held, a FULL/RESTART/TRUNCATE checkpoint will block or return BUSY rather than proceeding past the reader's lock — but the lock does not make the checkpoint "become passive"
3. **Scan all configured tables** — emit `READ` events for every existing row
4. **Commit read transaction** — releases the shared lock and read-mark
5. **Begin streaming from handoff marker** — start WAL reading from the recorded commit frame position

Because WAL mode provides snapshot isolation, any writes that occur during step 3 are appended to the WAL beyond the reader's `mxFrame` and are invisible to the snapshot reader. The streaming phase picks them up starting from the handoff position, leaving no gaps.

**Open question for mentor review**: The exact mechanism for determining the reader's WAL end mark (`mxFrame`) is the least proven part of this design. SQLite does not expose a public API for "which WAL frame is my snapshot's last visible commit." `PRAGMA data_version` is insufficient — it is a same-connection staleness indicator that does not encode `mxFrame` or allow frame-level positioning. Alternative approaches under investigation: (a) using `sqlite3_snapshot_get()` (SQLite 3.10+) which returns an opaque snapshot handle tied to the wal-index state, (b) scanning the WAL file while the JDBC read transaction is held and counting frames up to the last commit visible at that snapshot, (c) using a custom native extension via JNI to read the connection's wal-index read-mark directly. This will be validated during the bonding period with mentor input.



![Snapshot-to-Stream Handoff](d2/snapshot-handoff.png)



**Checkpoint interference**: While the connector holds a read transaction, it holds `WAL_READ_LOCK(N)` on a wal-index read-mark slot (via the JDBC connection, not via the raw `RandomAccessFile` WAL reader — this is an important distinction). A PASSIVE checkpoint can transfer pages to the database file up to the reader's read-mark but cannot truncate or reset the WAL. A FULL/RESTART/TRUNCATE checkpoint will block waiting for the reader to release the lock, or return `SQLITE_BUSY` — it does not silently downgrade to passive behavior. **Edge case**: if a reader acquires `WAL_READ_LOCK(0)` (which happens when the WAL is fully backfilled), this lock does NOT prevent WAL reset by a subsequent writer. The connector mitigates this by setting `PRAGMA wal_autocheckpoint=0` on its own connection to prevent self-triggered checkpoints, but this is connection-local — other connections can still checkpoint. The read transaction's `WAL_READ_LOCK(N>0)` is the primary protection against WAL truncation during snapshot.

#### Connector Architecture



![Connector Architecture](d2/connector-architecture.png)



The connector implements the standard Debezium connector class hierarchy:

| Debezium Base Class | SQLite Class | Role |
|:---------------------------------------------|:--------------------------------------|:----------------------|
| RelationalBaseSourceConnector                | SqliteConnector                       | Config, task creation |
| BaseSourceTask                               | SqliteConnectorTask                   | Coordinator, schema   |
| ChangeEventSourceFactory                     | SqliteChangeEventSourceFactory        | Snapshot + streaming  |
| Relational­Snapshot­ChangeEventSource        | Sqlite­Snapshot­ChangeEventSource     | 7-step snapshot       |
| Streaming­ChangeEventSource                  | Sqlite­Streaming­ChangeEventSource    | WAL poll, decode, emit|
| CommonOffsetContext                          | SqliteOffsetContext                   | Salt + epoch + offset |
| Historized­Relational­DatabaseSchema         | SqliteDatabaseSchema                  | Schema history        |
| BaseSourceInfo                               | SqliteSourceInfo                      | Source block fields   |
| Abstract­SourceInfo­StructMaker              | Sqlite­SourceInfo­StructMaker         | Kafka Connect Struct  |
| Relational­ChangeRecord­Emitter             | Sqlite­ChangeRecord­Emitter          | Page-diff to events   |
| Historized­Relational­Database­ConnectorConfig | SqliteConnectorConfig               | Config + SchemaHistory|
| AbstractPartition                            | SqlitePartition                       | `{"server": "name"}`  |



![WAL Processing Pipeline](d2/wal-pipeline.png)



#### Change Event Format

The connector emits standard Debezium change events:

```json
{
  "schema": { ... },
  "payload": {
    "before": null,
    "after": {
      "id": 42,
      "name": "sensor-1",
      "temperature": 23.5,
      "updated_at": "2026-06-15T10:30:00Z"
    },
    "source": {
      "version": "3.5.0",
      "connector": "sqlite",
      "name": "edge-devices",
      "db": "/data/sensors.db",
      "table": "readings",
      "wal_salt1": 1234567890,
      "wal_salt2": 987654321,
      "wal_epoch": 3,
      "wal_offset": 65536
    },
    "op": "c",
    "ts_ms": 1750000000000
  }
}
```

The `source` block includes SQLite-specific fields (`wal_salt1`, `wal_salt2`, `wal_epoch`, `wal_offset`) that enable consumers to reason about the stream position. `wal_epoch` is the authoritative ordering field — it is a monotonically increasing counter that survives WAL resets.

#### SQLite Data Type Mapping

SQLite uses a dynamic type system with 5 storage classes. The connector maps these to Kafka Connect schema types:

| SQLite Storage Class | Kafka Connect Type | Notes |
|---|---|---|
| INTEGER | `INT64` | Includes booleans (0/1) |
| REAL | `FLOAT64` | IEEE 754 double |
| TEXT | `STRING` | UTF-8 encoded |
| BLOB | `BYTES` | Raw binary |
| NULL | nullable field | Any column can be NULL |

SQLite's type affinity rules (column declared as `VARCHAR(100)` has TEXT affinity) are respected: the connector reads the column declaration from `sqlite_schema` to determine the declared type, applies affinity rules per SQLite documentation, and maps to the corresponding Kafka Connect type.

**Dynamic typing challenge**: SQLite allows any value type in any column regardless of declared affinity. A column declared `INTEGER` can store a TEXT value. Kafka Connect schemas are static — the schema is fixed at table registration time. The connector resolves this by using the column's declared affinity to set the Kafka Connect schema type, and coercing actual values at runtime: if a TEXT value appears in an INTEGER-affinity column, the connector attempts numeric parsing; if parsing fails, the value is converted to its string representation. The schema is set at table registration time and does not change based on individual row values — this avoids data-driven schema churn.

**Row identity policy**: Tables with `INTEGER PRIMARY KEY` use that column as the key schema. Tables without an explicit `INTEGER PRIMARY KEY` but with a `UNIQUE` constraint use that constraint's columns as the key. Tables with no primary key and no unique constraint emit events with a `null` key schema, following the same convention as the PostgreSQL connector for tables without a primary key or `REPLICA IDENTITY`. The internal `rowid` is still used for change detection (diffing) but is not exposed in the event key.

#### Key Design Decisions

- **WAL-based over trigger-based**: WAL reading is passive — no DDL changes to the target database, zero write overhead. The CDC landscape analysis confirms trigger-based approaches (e.g., sqlite-cdc) incur 97-127% write overhead.
- **Page-level diff over session extension**: The `sqlite3session` API requires explicit table registration and is C-only. WAL-based change detection works with any unmodified SQLite database.
- **Non-monotonic offset (salt + epoch) over frame number**: After studying all existing Debezium connectors, none handle non-monotonic offsets. The salt-based epoch design is novel, analogous to Litestream's generation concept.
- **Schema version polling over per-event schema**: MySQL and SQL Server connectors both use historized schema without embedding schema in every event. `schema_version` polling follows this proven pattern.

#### Risks and Mitigation

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| B-tree page decoding complexity (overflow pages, free list, pointer maps) | High | Start with common cases (no overflow, rowid tables); explicitly declare overflow and WITHOUT ROWID as stretch goals. Validate against SQLite test databases with known content. If overflow decoding is not correct by week 8, defer to post-GSoC |
| WAL checkpoint during streaming causes gap | Medium | Salt-based offset detects reset. **If connector is running**: the JDBC connection holds `WAL_READ_LOCK(N)` via the wal-index, preventing checkpoint from truncating past the reader's read-mark. (Note: the raw `RandomAccessFile` WAL reader alone does NOT hold WAL locks — the JDBC connection is essential. Also, `WAL_READ_LOCK(0)` does not prevent WAL reset.) **If connector is offline**: changes in truncated WAL frames are permanently lost — connector applies `wal.gap.policy` (warn/fail/resnapshot). Integration tests cover both online and offline checkpoint scenarios |
| Offline downtime + WAL truncation = irreversible data loss | Medium | This is a **hard limitation** of WAL-based CDC without a replication protocol. The connector cannot fabricate missed events. Mitigation: document operational requirement to keep connector running or use `PRAGMA wal_autocheckpoint=0` on the application side. Provide `resnapshot` policy for recovery |
| Performance of page-level diff for high-write workloads | Medium | Benchmark transaction-wide reconciliation cost. Consider caching decoded row sets (not raw pages) to reduce re-decode overhead. Profile with 1000+ writes/sec workload |
| SQLite file locking conflicts with application | Low | Connector opens read-only connection in WAL mode; WAL mode supports concurrent readers. Document requirement that database must be in WAL mode |
| Schema changes between WAL frames cause decode errors | Medium | Check `schema_version` before processing each batch. If version jumps by >1 between polls, log warning about potential missed intermediate schemas. Record full schema snapshots (not diffs) for recovery correctness |
| B-tree row movement across pages within transaction | Medium | Transaction-wide reconciliation keyed by rowid handles splits/merges. If a modified page cannot be traced to a known table root page, fall back to table-level re-scan from the database file |

#### Fallback Plan

If transaction-wide reconciliation is not working correctly by the end of Week 4 (Jun 29), invoke the fallback immediately — this preserves 8 weeks for building a viable connector rather than risking more time on the hard path.

**Minimum viable product (fallback)**: Replace raw WAL page decoding with a **hybrid approach** — use WAL frame monitoring only to detect WHICH tables changed (by mapping page numbers to table root pages via `sqlite_schema`), then re-read the changed tables via JDBC `SELECT` to obtain current state. This sacrifices `before` images and intermediate states but guarantees correctness, produces valid Debezium events, and still delivers value as a SQLite CDC connector. The WAL reader, offset management, schema tracking, and snapshot logic all remain useful. The full page-decoding approach can be added incrementally in a later release.


## Deliverables

### Midterm Evaluation (Week 6)

- `SqliteConnector` and `SqliteConnectorTask`: Configuration, validation, task creation
- `WalReader`: WAL file parsing, frame validation, commit boundary detection
- `BTreePageDecoder`: Leaf page decoding for rowid tables, cell parsing, record format extraction
- `SqliteSnapshotChangeEventSource`: Full-table snapshot via read transaction with WAL position tracking
- `SqliteStreamingChangeEventSource`: WAL polling loop with page-diff change detection
- `SqliteOffsetContext`: Salt-based resume token with WAL epoch tracking
- Unit tests for WAL parsing and page decoding against known SQLite test databases
- Docker-based integration tests with a live SQLite database

### Final Evaluation (Week 12)

- `SqliteDatabaseSchema`: Schema evolution via `schema_version` tracking, schema change events
- Snapshot-to-stream handoff with WAL checkpoint resilience
- Performance benchmarks (change detection latency, throughput for various write rates)
- Debezium Server integration tests (non-Kafka deployment)
- User documentation: configuration properties, supported SQLite versions, WAL mode requirements, limitations
- Blog post on debezium.io summarizing the project
- Upstream PR to debezium/debezium repository

### Stretch Goals

- **WITHOUT ROWID table support**: Decode index b-tree pages (`0x0A`) for `WITHOUT ROWID` tables using root-page mapping from `sqlite_schema`
- **Overflow page handling**: Support cells whose payload exceeds local page capacity via overflow page chain traversal (this is a payload-size property of the cell, not specific to TEXT/BLOB types — any row with enough columns or large values can overflow)
- **Index b-tree change tracking**: Decode index pages to detect changes in indexed columns without reading the full table page
- **FTS5 content table support**: Track changes in full-text search content tables
- **Multi-database monitoring**: Single connector instance monitoring multiple SQLite database files
- **Debezium UI integration**: Configuration panel for the SQLite connector in Debezium's management UI
- **WAL2 support**: Preparation for SQLite's proposed WAL2 mode (concurrent writers)


## Timeline

| Week | Dates | Tasks | Deliverable |
|------|-------|-------|-------------|
| **Bonding** | May 8 – Jun 1 | Dev environment setup; study PostgreSQL connector; connector skeleton + config; WAL reader prototype; discuss b-tree strategy with mentor | Connector loads in Kafka Connect; WAL prototype reviewed |
| **Week 1** | Jun 2 – Jun 8 | WAL reader: header parsing, frame reading, checksum validation, commit detection | WAL reader unit tests passing |
| **Week 2** | Jun 9 – Jun 15 | B-tree page decoder: leaf page parsing, cell extraction, record format, varints, 12 serial types | Page decoder tests passing. **Gate:** all 12 serial types correct? |
| **Week 3** | Jun 16 – Jun 22 | Streaming source: WAL polling, page cache, single-page change detection, Envelope emission | Correct events for single-table INSERT/UPDATE/DELETE |
| **Week 4** | Jun 23 – Jun 29 | Transaction-wide reconciliation: multi-page diff, page overlay, rowid-keyed row sets | Reconciliation passes CT-1/CT-2. **Gate:** if not working, invoke fallback |
| **Week 5** | Jun 30 – Jul 6 | Snapshot source: read-transaction isolation, WAL position handoff; offset context: salt-based resume | End-to-end working. **Gate:** correct events for 3-table DB? |
| **Week 6** | Jul 7 – Jul 13 | Docker integration tests (CT-3 to CT-6); edge case fixes; buffer week; midterm report | **Midterm evaluation** |
| **Week 7** | Jul 14 – Jul 20 | Schema evolution: schema_version tracking, schema change events, type mapping, history integration | Schema evolution complete |
| **Week 8** | Jul 21 – Jul 27 | Core path hardening: reconciliation edge cases, page cache optimization, correctness tests CT-7 to CT-10 | Core streaming production-ready |
| **Week 9** | Jul 28 – Aug 3 | Handoff hardening: checkpoint resilience, salt mismatch recovery; address code review feedback | Handoff passes all edge cases |
| **Week 10** | Aug 4 – Aug 10 | Performance benchmarks (TC-1 to TC-5); Debezium Server integration tests | Benchmark report + Server tests passing |
| **Week 11** | Aug 11 – Aug 17 | User documentation; blog post; remaining code review feedback | Documentation complete |
| **Week 12** | Aug 18 – Aug 25 | Final polish; upstream PR; final project report; submit final evaluation | **Final evaluation** |

Weekly sync with mentor Giovanni Panice via email or Debezium Zulip. Status updates posted to #community-gsoc channel.


## Performance Benchmarking

A structured benchmarking plan to characterize the SQLite connector's performance under realistic workloads:

**Methodology**: All benchmarks run in a Docker environment with a controlled SQLite database, for reproducibility. The database is pre-populated with varying amounts of data, and write workloads are generated by a separate process.

**Test cases:**

- **TC-1 (Change detection latency)**: Measure time from SQLite write commit to Debezium change event emission. Target: <100ms for a single-row insert. Test with 1, 10, 100 concurrent writers at configurable polling intervals (100ms, 500ms, 1s).
- **TC-2 (WAL parsing throughput)**: Generate sustained write workloads (100, 1000, 10000 writes/sec) and measure the connector's ability to keep up with WAL growth. Report frames parsed per second and any lag accumulation.
- **TC-3 (Page decoding overhead)**: Profile the b-tree page decoding cost per page. Measure with varying page sizes (4096, 8192, 16384, 32768, 65536 bytes) and cell counts (10, 100, 1000 rows per page).
- **TC-4 (Snapshot performance)**: Measure snapshot throughput for databases of different sizes (10K, 100K, 1M, 10M rows). Report rows scanned per second and memory usage.
- **TC-5 (Checkpoint resilience)**: Run sustained write+read workloads with periodic checkpoints and verify zero data loss, correct offset recovery, and minimal latency spike during checkpoint.

**Deliverable**: A performance report (`docs/benchmarks/sqlite-connector-report.md`) with raw measurements, analysis, and configuration tuning recommendations.


## Correctness Test Matrix

Beyond performance benchmarks, a structured correctness test plan to verify the connector handles SQLite edge cases:

| Test Case | What It Validates | Expected Behavior |
|-----------|-------------------|-------------------|
| **CT-1**: B-tree page split during multi-row INSERT | Transaction-wide reconciliation handles rows moving between pages | All inserted rows appear as `INSERT` events; no spurious DELETE+INSERT pairs |
| **CT-2**: Multiple updates to same row in single transaction | Net-state diff per rowid | Single `UPDATE` event with final `after` state (intermediate states are not observable at the WAL transaction level) |
| **CT-3**: DDL + DML interleaving (`ALTER TABLE ADD COLUMN` then `INSERT`) | Schema evolution detection and correct decode | Schema change event emitted before data events; new column appears in `after` |
| **CT-4**: Connector restart after WAL checkpoint (no truncation) | Offset resume with matching salts | Streaming resumes from stored offset; no duplicate events |
| **CT-5**: Connector restart after WAL checkpoint + truncation | Gap detection and `wal.gap.policy` behavior | `WARN` log + notification channel alert (default policy); `fail` policy stops connector; `resnapshot` triggers full re-snapshot |
| **CT-6**: Mixed-type column values (TEXT in INTEGER-affinity column) | Dynamic type handling | Value coerced to declared affinity at runtime; schema unchanged; no crash |
| **CT-7**: Large BLOB exceeding page size | Overflow page handling (stretch goal) or graceful skip | If overflow not implemented: row skipped with `WARN` log; if implemented: full value emitted |
| **CT-8**: Table with no primary key | Row identity policy (null key) | Events emitted with `null` key schema; `rowid` used internally for diffing but not exposed in event key |
| **CT-9**: Concurrent writers during snapshot | Snapshot isolation under WAL mode | Snapshot sees consistent state; post-snapshot writes captured by streaming |
| **CT-10**: `VACUUM` during streaming | WAL reset detection | Epoch incremented; `wal.gap.policy` applied |


## Community Collaboration

**Pre-GSoC engagement (already completed):**

- Sent technical email to mentor Giovanni Panice with detailed WAL-based CDC design analysis, receiving three specific technical answers that shaped this proposal's architecture
- Opened draft PR debezium-platform#309 as directed by mentor ("read the guideline and propose a draft pull request")
- Studied Debezium PostgreSQL and MySQL connector architectures as reference implementations

**During GSoC: Debezium community integration:**

- Post design documents and milestone updates to Debezium Zulip #community-gsoc channel for feedback from the broader Debezium team
- Share WAL decoding findings on the Debezium blog — the b-tree page parsing and non-monotonic offset design haven't been well-documented for CDC purposes before
- Monitor Debezium GitHub issues for SQLite-related feature requests and respond with implementation status
- Participate in Debezium community calls to present progress and gather feedback
- Coordinate with Debezium CI/CD team to integrate SQLite connector tests into the build pipeline

**Upstream PR strategy:**

- Follow Debezium coding conventions (checkstyle, formatter, header)
- Submit incremental PRs: (1) connector skeleton + WAL reader, (2) page decoder + streaming, (3) snapshot + offset, (4) schema evolution + documentation
- Respond to every review comment within 24 hours
- Include Testcontainers-based integration tests with every PR

**SQLite community engagement:**

- Report any WAL format edge cases discovered during development to the SQLite forum (sqlite.org/forum)
- If the connector reveals undocumented WAL behaviors, contribute documentation upstream


## Proof of Concept

To validate the WAL-based CDC approach before writing the full Debezium connector, I built a standalone Java proof-of-concept that reads a SQLite WAL file, parses frame headers, and extracts committed page changes. The PoC demonstrates the three core operations the connector depends on:

**1. WAL Header Parsing and Frame Reading**

The PoC opens a SQLite WAL file (`RandomAccessFile` in read-only mode), validates the 32-byte header (magic number `0x377f0682`), reads the format version and page size, and reads frames sequentially. Each 24-byte frame header is parsed to extract the page number, commit flag (`commit_size > 0`), and salt copies. Frame validity is confirmed by two independent checks: (1) salt copies must match the WAL header salts (confirms frame belongs to the current WAL generation), and (2) cumulative checksums must be valid (confirms data integrity, computed using the rolling algorithm seeded from the previous frame's checksum).

**2. Commit Boundary Detection**

Frames are buffered until a commit frame is encountered (non-zero `commit_size` field). This way, the PoC processes complete transactions atomically. Uncommitted frames (from in-progress or rolled-back transactions) are simply not yet committed — they remain in the WAL with valid checksums but no commit boundary. The PoC stops reading at the end of valid data (where checksums or salts no longer match) and ignores any buffered but uncommitted frames.

**3. B-Tree Leaf Page Decoding**

For each committed frame containing a table b-tree leaf page (page type `0x0D`), the PoC decodes the cell pointer array, extracts cells, and parses the SQLite record format: varint-encoded header with serial type codes, followed by column values. The PoC successfully extracts row data (INTEGER, TEXT, REAL, BLOB columns) and matches it against the expected content from `SELECT * FROM table`.

**PoC results on a test database** (10,000 rows across 3 tables, 4096-byte page size):
- WAL header parsing: <1ms
- Frame reading throughput: ~50,000 frames/sec (estimated from profiling; formal benchmarks are a Week 10 deliverable)
- B-tree page decoding: ~2ms per page (average 45 cells/page)
- Decoded row data matches expected content verified via `SELECT * FROM table` queries

During PoC development, we discovered that sqlite3_close() performs a passive checkpoint that can truncate the WAL file before the connector reads it. The test suite addresses this by holding a read transaction open via a background process, and the production connector will similarly maintain a JDBC read-lock to prevent premature truncation.

The PoC validates that WAL parsing, commit detection, and b-tree page decoding work correctly — the three core operations the connector depends on. The PoC does NOT implement change diffing (INSERT/UPDATE/DELETE classification) or transaction-wide reconciliation; these are connector-level concerns. The full Debezium connector builds on the PoC by adding page-diff change detection, transaction-wide reconciliation (diffing across all pages in a commit), offset management, schema tracking, snapshot support, and the Debezium Envelope event format.


## Core Tasks Coverage

The project idea page lists specific core tasks. This section maps each to the proposal's design:

| Core Task | Addressed In | Approach |
|-----------|-------------|----------|
| Read SQLite WAL for change detection | WAL-Based Change Detection (Section 3.2) | Parse WAL header and frames; validate cumulative checksums; detect commit boundaries via `commit_size` field |
| Reconstruct row-level changes | B-Tree Page Decoding (Section 3.3) | Decode leaf pages (type `0x0D`); parse cell pointer arrays and record format; diff page images to classify INSERT/UPDATE/DELETE |
| Handle schema evolution | Schema Evolution (Section 3.6) | `HistorizedRelationalDatabaseSchema` with `PRAGMA schema_version` polling; `SchemaHistory` store for recovery; SQL Server connector pattern (no DDL parsing) |
| Produce Debezium change events | Change Event Format (Section 3.8) | Standard Envelope (`before`/`after`/`source`/`op`/`ts_ms`); SQLite-specific source fields; type mapping for 5 storage classes |
| Manage offsets for resumable streaming | Offset Management (Section 3.5) | Epoch-based resume token: `(wal_epoch, wal_salt1, wal_salt2, wal_offset)`; monotonic epoch counter survives WAL reset |
| Snapshot existing data | Snapshot-to-Stream Handoff (Section 3.7) | WAL-mode read transaction for snapshot isolation; `PRAGMA wal_autocheckpoint=0` during snapshot; handoff at recorded WAL position |
| Handle WAL checkpoint/reset | Offset Management + Streaming | Salt comparison detects reset; epoch increment maintains monotonic ordering. **Hard limitation**: if connector is offline during checkpoint+truncation, intermediate changes are lost — configurable `wal.gap.policy` (warn/fail/resnapshot) |
| Integration testing | Deliverables (Section 4) | Docker-based tests with live SQLite; Testcontainers; checkpoint resilience tests; Debezium Server integration |


## Benefits to Community

For **Debezium**, this connector extends CDC coverage to SQLite — the most widely deployed database engine — enabling edge computing, embedded systems, and mobile applications to participate in Debezium-based data pipelines. The WAL-polling approach, page-level change detection, and non-monotonic offset design may be reusable patterns for other embedded databases that lack built-in replication.

For the **SQLite ecosystem**, this project adds a Debezium-style CDC option for WAL-mode SQLite databases — no triggers, no session extensions, no application changes. IoT and edge deployments are one clear use case — SQLite is often the local data store, and getting those changes into cloud systems today usually means polling, triggers, or custom code rather than log-based CDC.

For the **broader CDC community**, the page-decoding code could be factored into a reusable module over time. Any tool that needs to read SQLite rowid-table leaf pages — forensics tools, backup systems, database introspection utilities — could build on the same decoder.


## What Excites Me About This Project

SQLite's WAL gives you raw B-tree page images, not logical row operations. Reconstructing row-level changes from physical page images — parsing WAL frame headers, decoding b-tree cells via serial type codes and varints, handling checkpoint and WAL reset boundaries — is a low-level systems problem where you have to understand the on-disk format, not just call an API. That's the kind of work I find interesting.

SQLite is the local data store for IoT gateways, mobile apps, and embedded systems where teams today usually fall back to polling, triggers, or custom code rather than log-based CDC. A Debezium SQLite connector lets these deployments use the same CDC infrastructure that enterprises already run for server-side databases.


## About Me

I am a Bachelor of Science student at the University of Melbourne (Computing and Software Systems + Data Science, First Class Honours). Awards: British Physics Olympiad (BPhO) Top Gold; Extended Project Qualification A\*.

I have been contributing to open-source projects since January 2026, with 12 merged PRs across 5 organizations (Apache Beam, Apache ShardingSphere, Apache IoTDB, Apache Iceberg, and OpenCV), plus active open PRs under review. My contributions consistently target resource management, connector reliability, and correct lifecycle handling -- the same concerns that dominate CDC connector development. I have built the Debezium codebase locally and run the PostgreSQL connector's integration test suite.

When I got stuck understanding how Debezium connectors handle non-monotonic offsets (which is core to this proposal), I traced through the SQL Server connector's `TxLogPosition` and the PostgreSQL connector's `Lsn` class to understand how other connectors solve similar ordering problems. After tracing through SQL Server's `TxLogPosition` and PostgreSQL's `Lsn`, I confirmed that neither handles non-monotonic offsets — both rely on monotonically increasing LSNs. The salt-based epoch design was developed to solve this gap.

### Professional Experience

| Period | Role | Organization | Key Work |
|--------|------|-------------|----------|
| Nov 2024 – Feb 2025 | Core Module Developer | Changzhou Data Technology × Alibaba Cloud Bailian | vLLM + GPTQ microservices; ReAct decision pipeline; 98% classification accuracy on enterprise ad-compliance workloads |
| Jan 2025 – Mar 2025 | Frontend Systems Engineer | ByteDance Winter Camp | Built Heimdallr, a distributed frontend monitoring SDK in TypeScript; 5,000 logs/sec ingestion; real-time anomaly alerting |

### Open-Source Contributions

12 merged and 13 open PRs across 8 organizations. All contributions focus on resource management, connector reliability, and correct type handling — the same work this project asks for.

**Debezium** (directly related to this project):

| Contribution | Status |
|-------------|--------|
| [debezium-platform#309](https://github.com/debezium/debezium-platform/pull/309): Add description field to connections API payload | \openpr{} Open |

**Apache Beam** (demonstrates IO connector and streaming expertise):

| Contribution | Status |
|-------------|--------|
| [beam#37681](https://github.com/apache/beam/pull/37681): Fix resource leak in KafkaIO GCS truststore download | \merged{} Merged |
| [beam#37356](https://github.com/apache/beam/pull/37356): Make withBackOffSupplier public for bounded retry | \merged{} Merged |
| [beam#37298](https://github.com/apache/beam/pull/37298): Enhance serialization error messages | \merged{} Merged |
| [beam#37297](https://github.com/apache/beam/pull/37297): Document Ubuntu 24.04 Python version requirements | \merged{} Merged |
| [beam#37458](https://github.com/apache/beam/pull/37458): Add record header support to WriteToKafka | \merged{} Merged |
| [beam#37530](https://github.com/apache/beam/pull/37530): Warn when ValueState contains collection types | \openpr{} Open |
| [beam#37516](https://github.com/apache/beam/pull/37516): Support nested column paths in Iceberg keep/drop config | \openpr{} Open |
| [beam#37342](https://github.com/apache/beam/pull/37342): Fix RequestResponseIO retryable exception types | \openpr{} Open |

**Apache IoTDB** (time-series database expertise):

| Contribution | Status |
|-------------|--------|
| [iotdb#17212](https://github.com/apache/iotdb/pull/17212): Fix Process resource leak in system metrics collection | \merged{} Merged |
| [iotdb#17180](https://github.com/apache/iotdb/pull/17180): Support inclusive end time syntax in GROUP BY clause | \openpr{} Open |
| [iotdb#17400](https://github.com/apache/iotdb/pull/17400): Fix C++ client time column access returning NULL for non-long types | \openpr{} Open |
| [iotdb#17408](https://github.com/apache/iotdb/pull/17408): Fix Java client time column access throwing ArrayIndexOutOfBoundsException | \openpr{} Open |
| [iotdb#17411](https://github.com/apache/iotdb/pull/17411): Fix last_by returning null row after deleting all table data | \openpr{} Open |

**Apache ShardingSphere** (resource management):

| Contribution | Status |
|-------------|--------|
| [shardingsphere#38244](https://github.com/apache/shardingsphere/pull/38244): Fix RegistryCenter resource leak in StatisticsCollectJobWorker | \merged{} Merged |
| [shardingsphere#38152](https://github.com/apache/shardingsphere/pull/38152): Fix ClassCastException reading ZooKeeper config | \merged{} Merged |

**Apache Iceberg, OpenCV:**

| Contribution | Status |
|-------------|--------|
| [iceberg#15463](https://github.com/apache/iceberg/pull/15463): Fix JDBC ResultSet leaks in JdbcCatalog and JdbcUtil | \merged{} Merged |
| [opencv#28502](https://github.com/opencv/opencv/pull/28502): Fix erode/dilate documentation parameter names | \merged{} Merged |
| [opencv#28698](https://github.com/opencv/opencv/pull/28698): Fix resource leaks in Android Utils.java | \merged{} Merged |
| [opencv#28699](https://github.com/opencv/opencv/pull/28699): Replace System.exit with exceptions in HighGui | \merged{} Merged |

**Eclipse SW360** (resource management):

| Contribution | Status |
|-------------|--------|
| [sw360#3969](https://github.com/eclipse-sw360/sw360/pull/3969): Fix CSVReader resource leak in UserDatabaseHandler | \openpr{} Open |
| [sw360#3968](https://github.com/eclipse-sw360/sw360/pull/3968): Fix resource leak in SVMUtils | \openpr{} Open |
| [sw360#3967](https://github.com/eclipse-sw360/sw360/pull/3967): Fix FileInputStream leak in ComponentDatabaseHandler | \openpr{} Open |

**Apache SeaTunnel** (resource management):

| Contribution | Status |
|-------------|--------|
| [seatunnel#10532](https://github.com/apache/seatunnel/pull/10532): Fix JDBC and file resource leaks | \openpr{} Open |
| [seatunnel#10529](https://github.com/apache/seatunnel/pull/10529): Fix premature socket close in SocketSourceReader | \openpr{} Open |

**Technical skills:** Java, Python, C/C++, TypeScript, Spring Framework, Kafka Connect, time-series databases, DAO patterns, microservices, Docker, JUnit, Testcontainers, Git/GitHub.

**Time commitment:** 25+ hours per week. No exam or schedule conflicts (June–August). **Timezone:** UTC+10 (Melbourne).


## Why Debezium / JBoss Community

My open-source work has a recurring theme: finding and fixing resource lifecycle bugs in data infrastructure -- GCS handle leaks in Beam's KafkaIO connector, unclosed JDBC ResultSets in Iceberg's catalog, RegistryCenter leaks in ShardingSphere, Process handle leaks in IoTDB's metrics collection. A CDC connector is, at its core, a resource lifecycle problem: database connections, WAL file handles, offset state, schema history -- all of it must be acquired, tracked, and released correctly across restarts, rebalances, and crashes. The SQLite connector needs exactly this kind of work — managing file handles, tracking state across restarts, cleaning up on failure.

My Beam KafkaIO contributions gave me hands-on experience with the Kafka Connect ecosystem that Debezium runs on. Adding record header support to `WriteToKafka` (beam\#37458), fixing resource leaks in GCS truststore downloads (beam\#37681), and making backoff strategies configurable for bounded retry (beam\#37356) all required understanding how Kafka producers, consumers, and Connect workers manage state across distributed systems. The Iceberg JDBC work (iceberg\#15463) gave me direct experience with the pattern of reading database metadata, tracking schema, and closing cursors correctly -- the same concerns that drive a connector's snapshot and streaming phases.

Giovanni responded to my technical email with three specific design answers: no convention for non-monotonic offset tracking (design my own), study the MySQL connector for schema evolution, and use SQLite's read-lock for snapshot-to-stream handoff. He then directed me to "read the guideline and propose a draft pull request," which I have done (debezium-platform\#309). That pre-proposal technical engagement directly shaped this proposal's architecture.

Adding SQLite support would extend Debezium to embedded, edge, and mobile deployments, and those change events could flow through existing Kafka-based Debezium pipelines and other systems that consume Debezium-formatted events.


## Post Google Summer of Code

I plan to maintain the connector after GSoC. SQLite's WAL format has been stable since version 3.7.0 (2010) — the 32-byte header, 24-byte frame header, and checksum algorithm have not changed in 16 years. This stability means maintenance is primarily about tracking Debezium engine API changes and extending connector features, not chasing format changes. I will run the connector's test suite against each SQLite quarterly release and each Debezium minor release, and fix any regressions.

After the GSoC period, I plan to extend the connector with features that did not fit into the 12-week timeline — full `WITHOUT ROWID` table support, overflow page handling, FTS virtual table content tracking, and multi-database CDC. I also want to contribute to the broader Debezium ecosystem: fixing bugs in existing connectors and helping other contributors on Zulip. My longer-term goal is to pursue Debezium committer status through sustained contribution.


## Communication Plan

- **Weekly sync:** Written status report via email to Giovanni Panice (primary mentor) and Vincenzo Santonastaso (co-mentor). Video call when design decisions need discussion
- **Day-to-day questions:** Debezium Zulip \#community-gsoc channel for public discussion; direct email for sensitive or blocking issues
- **Code reviews:** GitHub PRs with clear descriptions; large changes broken into independently reviewable parts (e.g., WAL parser separate from offset management separate from schema history)
- **Blockers:** Flagged within 24 hours via email and Zulip, with a description of what I have tried and where I am stuck, rather than silently stalling
- **Progress tracking:** Weekly update posted to the Zulip channel so both mentors and the broader Debezium community can follow progress


## References

- [SQLite WAL Format Documentation](https://www.sqlite.org/walformat.html)
- [SQLite File Format (B-Tree Pages)](https://www.sqlite.org/fileformat2.html)
- [Debezium Architecture](https://debezium.io/documentation/reference/stable/architecture.html)
- [Debezium PostgreSQL Connector Source](https://github.com/debezium/debezium/tree/main/debezium-connector-postgres)
- [Debezium MySQL Connector Source](https://github.com/debezium/debezium/tree/main/debezium-connector-mysql)
- [Debezium Connector Development Guide](https://debezium.io/documentation/reference/stable/development/engine.html)
- [JBoss Community GSoC 2026 Ideas](https://spaces.redhat.com/spaces/GSOC/pages/750884772/Google+Summer+of+Code+2026+Ideas)
- [SQLite Session Extension](https://www.sqlite.org/sessionintro.html)
- [Litestream (SQLite Replication)](https://litestream.io/)
