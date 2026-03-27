
# Debezium: Source Connector for SQLite

## About me

1. **Name:** Siddhant Chaturvedi (GitHub: [@siddhantcvdi](https://github.com/siddhantcvdi))
2. **University / Program / Year:** IIIT Gwalior, Integrated BTech+MTech in IT, 3rd Year / Expected Graduation: 2028
3. **Contact:** 
   - Email: siddhantcvdi@gmail.com
   - Phone: +91 6372207434
4. **Time zone:** IST (UTC+5:30)
5. **Experience:** Software Engineering Intern at TeemCRM | JEE Qualified (Top 1.2%)
6. [Link to Zulip Introduction](https://debezium.zulipchat.com/#narrow/channel/573881-community-gsoc/topic/newcomers/near/577126130)

## Code Contributions

### [Define explicit Jandex version in Quarkus modules](https://github.com/debezium/debezium-quarkus/pull/26)

| Field  | Detail |
|--------|--------|
| Repo   | `debezium/debezium-quarkus` |
| Status | Merged |

During the build process, Maven was printing warnings due to a missing explicit Jandex version, causing it to fall back to a default. I explicitly set the `jandex` and `jandex-maven-plugin` versions using the `${version.jandex}` property in the affected `pom.xml` files, aligning them with the centralized dependency management.

### [Connection validator for Redis](https://github.com/debezium/debezium-platform/pull/274)

| Field  | Detail |
|--------|--------|
| Repo   | `debezium/debezium-platform` |
| Status | Merged |

Implemented a Redis Connection Validator in the `debezium-platform-conductor` module, which validates Redis connection configuration before the connector starts. The validator checks host, port, and optional SSL settings, and gives clear error messages for misconfigured fields.

**Key Changes:**
- `RedisConnectionValidator.java` - Core validation logic.
- `RedisConnectionValidatorTest.java` - Unit tests covering valid and invalid configurations
- `RedisConnectionValidatorAuthIT.java` - Integration tests for authenticated Redis connections
- `RedisTestResource` / `RedisTestResourceAuthenticated` - Test containers pulling from **mirror.gcr.io/redis:7-alpine**

### [Fix stale `database.*` namespace references in SQL Server](https://github.com/debezium/debezium/pull/7136)

| Field  | Detail |
|--------|--------|
| Repo   | `debezium/debezium` |
| Status | Merged |

The JDBC driver passthrough configuration namespace was renamed from `database.` to `driver.` in Debezium 2.0, but the SQL Server connector retained several stale references to the old namespace. This PR replaces them across tests, `pom.xml`, and documentation, and expands the SSL configuration docs with clearer examples.

Key changes:
- `pom.xml` - Updated all the SSL properties to use `driver.` prefix matching the camelCase keys expected by the Microsoft JDBC driver.
- `SqlServerConnectorIT.java` - Updated test config to use new namespace and keys.
- `TestHelper.java` - Extended ther helpers to also read `driver.*` system properties alongside `database.*`, so Maven-set SSL properties are correctly picked up during tests.
- `SqlServerConnectorConfig.java` - Replaced the hardcoded `database.applicationIntent` falling back to `database.applicationIntent` for backward compatibility.

### [Google Cloud Pub/Sub connection validator](https://github.com/debezium/debezium-platform/pull/279)

| Field  | Detail |
|--------|--------|
| Repo   | `debezium/debezium-platform` |
| Status | Under Review |

This Pull Request implements **PubSubConnectionValidator** in the **debezium-platform-conductor** module, validating Google Cloud Pub/Sub destinations before the connector starts. 

**It supports three credential modes:**
- Application Default Credentials (ADC) — for GCP-hosted environments and it needs no config. 
- Inline service account JSON via `credentials.json`
- Custom gRPC endpoint via `endpoint` for the Pub/Sub Emulator

**Key changes:**
- `PubSubConnectionValidator.java`: Added a two-phase validation which checks the config first (without network) and then attempts to make a connection. Add support for emulator via `ManagedChannel` with `NoCredentialsProvider`
- `connection-schemas.json`: Added **GOOGLE_PUB_SUB** schema entry with **project.id** as the only required field
- `application.yml`: Added timeout and scope config properties
- `PubSubConnectionValidatorTest.java`: Wrote unit tests covering cases like null config, missing `project.id`, malformed inline credentials, whitespace trimming, and optional field acceptance
- `PubSubConnectionValidatorIT.java`: It includes integration tests against a live PubSubEmulatorContainer, including unreachable endpoint failure
- `PubSubTestResource.java`: This is a quarkus test resource managing the `gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators` container lifecycle
- `pom.xml`: Added **google-cloud-pubsub** and **testcontainers-gcloud** dependencies

## Project Information

### Abstract

SQLite is a lightweight, embedded relational database widely used in desktop, mobile, and edge/IoT applications, where it serves as the primary data store for local application state. Despite its widespread adoption, no production-ready Change Data Capture connector exists for SQLite. This project proposes a Debezium source connector for SQLite that captures row-level changes by directly parsing the SQLite Write Ahead Log (WAL) file. Unlike other Debezium connectors that receive structured change events from a network replication protocol, this connector decodes raw SQLite B-tree leaf pages, reconstructs before and after row states by diffing page versions across WAL frames and produces standard change events operations without requiring any modifications to the main application using the database. The connector supports an initial blocked snapshot, dynamic schema introspection, and a controlled checkpoint lifecycle. By bridging SQLite with Kafka and Debezium Server, this connector enables audit trails, observability, and edge-to-cloud data pipelines for the vast ecosystem of applications already built on SQLite.

### Why this project?

I was introduced to Change Data Capture while exploring Debezium as part of GSoC and I found it to be genuinely interesting. The idea of capturing changes directly from a database without relying on base application logic felt very elegant. As I read more about Debezium and went through parts of the codebase, I was impressed by the connector models for various databases. 

Unlike systems where change events are already exposed in a more structured form, this project requires working with the database internals. Understanding WAL behavior, page-level changes, and binary structures makes it a much deeper systems problem, and that is exactly what makes it exciting to me. I enjoy work that involves digging deep and understanding how things actually function internally.

Apart from the technical challenge, SQLite is used in a huge number of applications like embedded, mobile, and local-first environments, but it does not yet have a strong CDC. Building a connector for it would make real-time change capture possible in these places, which makes the work feel quite practical.

# Technical Description

This section focuses on the technical implemetation of SQLite Source Connector. Databases like PostgreSQL and MySQL expose change events through a network replication protocol. SQLite has no such protocol. It is an embedded, file-based database with no network interface.

![alt text](images/architecture.png)

At a high level, the connector works as follows. On first startup it performs a blocked snapshot reading all existing rows from tracked tables. Once the snapshot completes, the streaming phase begins. A WatchService monitors the .db-wal file for OS-level modification events, gated by PRAGMA data_version to confirm only committed writes trigger a parsing cycle. When a change is detected, the WAL reader parses frame headers and groups them into committed transactions. The page decoder then decodes the raw B-tree leaf pages reading cell pointers, varints, and serial types to reconstruct row values. The WAL differ compares the new page state against the old state, retrieved either from the in-memory page cache or the .db file directly and produces INSERT, UPDATE, or DELETE events with correct before and after values. These events are converted into Debezium SourceRecord objects and placed on the ChangeEventQueue, which Kafka Connect drains via poll() and publishes to Kafka topics. Throughout streaming, the connector holds an open reader transaction and registers a WAL hook to prevent unexpected checkpoints from invalidating the page cache, performing its own controlled checkpoint at configurable intervals.

The Technical Implementation has been divided into 6 main modules:
1. Project Skeleton and Configuration
2. Schema Handling
3. Initial Snapshot
4. WAL Parsing Engine
5. Streaming 
6. Testing and Documentation

## Module 1 - Project Skeleton and Configuration

### Preface and Goal
Every Debezium connector follows the same foundational pattern: 
1. SourceConnector that validates configuration and spawns tasks. 
2. SourceTask that runs the main loop 
3. JDBC connection wrapper that enforces database-specific requirements.

This module establishes the connector foundation following the standard Debezium connector pattern. The SQLite-specific additions are WAL mode enforcement at startup and disabling auto-checkpoint on the connector connection. The goal is a connector that compiles, connects to SQLite, enforces WAL mode, and fails clearly with useful error messages when configuration is wrong. Every subsequent module builds directly on this foundation

### Deliverables
- Connector compiles against the Debezium BOM and Kafka Connect API.
- Connects to any SQLite .db file via JDBC.
- Enforces PRAGMA journal_mode=WAL on startup and fails immediately if WAL mode is unavailable.
- Disables automatic checkpointing on the connector connection.
- Validates all configuration fields.

### Co-located Deployment
Every other Debezium connector connects to a remote database server over a network. SQLite has no server and no network protocol. The connector reads that file directly using Java's FileChannel, which means it must run on the same machine as the .db file. This is enforced at startup and documented as a known limitation.


## Module 2 - Schema Handling

### Preface and Goal

Before the connector can decode a single row from the WAL, it needs to know the structure of every table it is monitoring i.e. the column names, their types, which column is the primary key etc.

The main goal of this module is to dynamically get the schema of any SQLite database without hardcoding and providing column names, types, primary key information, and schema change detection to every other module that needs it.

### Deliverables

- Read and parse all table schemas dynamically from `sqlite_master`
- Provides root page number per table for WAL frame-to-table mapping
- Maps SQLite type affinities to Kafka Connect schema types
- Detects WITHOUT ROWID tables and extracts their composite primary key
- Detects schema changes during streaming via PRAGMA schema_version

### Description

Unlike PostgreSQL or MySQL, SQLite stores the raw CREATE TABLE SQL string in a special table called `sqlite_master`. To know the schema of a table, we need to read this SQL string and parse it on our own.

```sql
SELECT name, sql FROM sqlite_master WHERE type = 'table';
-- Returns rows like:
-- name: "orders"
-- sql:  "CREATE TABLE orders (id INTEGER PRIMARY KEY, item TEXT, amount REAL)"
```
We just need to parse the sql string to get the schema of the table.

We also have to maintain a map that keeps a relation between pages of the database and which table are they associated with. When a WAL frame arrives, the connector knows the page number that changed but not which table it belongs to. By maintaining a rootpage to table name map the connector can immediately identify which table a WAL frame is modifying.

![alt text](images/module2.png)

`SqliteValueConverter` handles the affinity-to-Kafka-Connect type mapping.SQLite uses five type affinities rather than strict types. The connector maps these to Kafka Connect schema types:

| SQLite Affinity  | Kafka Connect Type |
|--------|--------|
| INTEGER   | INT64 |
| REAL | FLOAT64 |
| TEXT | STRING |
| BLOB | BYTES |
| NUMERIC | FLOAT64/ INT64 |

### Schema Change Detection
SQLite increments PRAGMA schema_version on every DDL operation. The connector can poll this value each streaming cycle. When it changes, SqliteDatabaseSchema re-reads sqlite_master, rebuilds the column map, and emits a schema change event.

## Module 3 - Initial Snapshot

### Preface and Goal

When the connector starts for the first time, it has no knowledge of the current state of the database. Before streaming WAL changes, it needs to establish a baseline. This is the snapshot. Without it consumers would only see changes that happen after the connector started, without context of what existed before.

The goal is a full read of all tracked tables that completes atomically after which the connector transitions seamlessly into streaming mode.

### Deliverables

- Full read of all rows from every tracked table via BEGIN IMMEDIATE
- Each row emitted as a READ event to the ChangeEventQueue
- Offset context initialised with data_version, wal_frame_index, and per-table last_rowid
- Clean transition into streaming mode after snapshot completes

![alt text](images/module3-snapshot-flow.png)

### BEGIN IMMEDIATE
SQLite provides a transaction type called BEGIN IMMEDIATE that acquires a reserved lock on the database at the start of the transaction. This prevents any other connection from writing while the snapshot transaction is open, while still allowing other readers.

```sql
BEGIN IMMEDIATE;
  -- connector reads all rows here
  -- no other writer can commit during this window
  SELECT rowid, * FROM orders;
  SELECT rowid, * FROM products;
  ...
COMMIT;
```

This gives the connector a consistent point-in-time view of the entire database for the duration of the snapshot. All reads within this transaction see the same version of the data regardless of how long the snapshot takes.

### Offset Initialisation
After the snapshot completes we need to populate the offset context so streaming can resume correctly on restart.
```json
{
  "snapshot_completed": true,
  "data_version": 42,
  "wal_frame_index": 17,
  "tables": {
    "orders":   { "last_rowid": 150 },
    "products": { "last_rowid": 32  }
  }
}
```

On restart, if snapshot_completed is true we can skip the snapshot  entirely and streaming resumes from wal_frame_index.

### Why rowid Must Be Explicit
The snapshot query uses `SELECT rowid, *` rather than `SELECT *`. The rowid is SQLite's internal unique row identifier and is not included in `SELECT *` by default. It must be explicitly selected as it is used in WAL Parsing Engine.

## Module 4 - WAL Parsing Engine 
### Preface and Goal
This module contains the core technical contribution of the connector. Every Debezium connector receives change events from the database through a structured network protocol. In SQLite the only source of change information is the WAL file (a binary file of raw database page images with no row-level structure).

The goal of this module is to extract row-level INSERT, UPDATE, and DELETE events with correct before and after values directly from the raw WAL binary without issuing any additional queries to the database.

### Deliverables

- Parse WAL file header and frame headers from raw binary.
- Group WAL frames into committed transactions using the commit marker.
- Decode SQLite B-tree leaf pages: cell pointers, varints, serial types, column values.
- Retrieve old page state from page cache or .db file fallback.
- Diff old and new decoded rows by rowid to produce INSERT/UPDATE/DELETE events with correct before/after values.
- Handle overflow pages for large row payloads.

### WAL File Format
The WAL file is a sequence of frames. Each frame is a fixed-size unit consisting of a 24-byte header followed by one full database page of data (typically 4096 bytes). The file begins with a 32-byte WAL header that establishes the page size and two salt values used to validate frames.

![alt text](images/module4-wal-file-format.png)

The frame header contains the page number (which database page this frame is a new version of) and a commit size field. A commit size of zero means the frame is part of an in-progress transaction. A non-zero value means this is the last frame of a committed transaction. Everything from the previous commit marker up to and including this frame forms one atomic unit. The connector only processes complete committed transactions, never partial ones.

### Getting Old and New Pages
To produce before/after row values, the connector needs the state before the transaction and the state after.The new state is always the current WAL frame. The old state comes from one of two places:
1. Page modified for first time: Get the old state from from .db file at offset (pageNumber - 1) × pageSize.
2. Page modified again (already in WAL): Get old state from previous WAL frame for that page (from page cache)

The connector maintains a page cache `Map<pageNumber, lastFrameData`  which is its in-memory record of what each page looked like after the last transaction it processed. It is updated after every transaction and provides an O(1) lookup for the old state of any subsequent write to the same page. Without this cache, the connector would need to scan the entire WAL file from the beginning on every change to find the previous version of a page.

### Decoding a Page
A WAL frame's page data is a raw SQLite B-tree page. For CDC purposes, only table leaf pages are relevant which can be identified by the first byte of the page being `0x0D`. Interior pages `(0x05)` are structural index nodes with no row data and are skipped.

![alt text](images/module4-page-structure.png)

A leaf page contains a cell pointer array near the top which is a list of 2-byte offsets pointing to where each cell (row) starts within the page. Each cell in a page encodes one row using SQLite's record format.
The following image gives a detailed structure of a cell.

![alt text](images/module4-cell-record-format.png)

The record header in a cell contains serial types. Serial types in the record header tell the decoder what type each column value is and how many bytes it occupies in the body. The decoder reads the header first to collect all serial types, then reads the body values in order.the header says the first column is Type 1 (1 byte) and the second is Type 13 (0 bytes, empty string), the decoder reads 1 byte for the first value and moves the pointer 0 bytes for the second.

Now that we know, the detailed structure of a page and a cell, it becomes easy to decode and get the data in any row of a page.

### Overflow Pages
When a row's payload exceeds roughly 1/4 of the page size, SQLite stores the overflow in overflow pages. The cell on the leaf page contains only the first portion of the record and a 4-byte pointer to the first overflow page. Each overflow page begins with a 4-byte pointer to the next, followed by continuation data. The decoder follows this chain, reading each overflow page from the WAL frame map or .db file, until the full record is reassembled.

### Diffing Old and New Pages
![alt text](images/module4-old-and-new-diff.png)
Once both old and new pages are decoded into maps of `rowid → column values`, the diff is straightforward:
1. rowid in new only: INSERT  (before = null,  after = new row)
2. rowid in old only: DELETE  (before = old row, after = null)
3. rowid in both, values differ: UPDATE (before = old row, after = new row)
4. rowid in both, values same: no event

This produces the complete before/after change events that the Debezium framework expects.

## Module 5 - Streaming

### Preface & Goal

The WAL parsing engine decoded WAL pages and produced change events. This module is about the layer that drives that engine continuously, that is detecting when changes happen, triggering the WAL parsing cycle, managing the checkpoint lifecycle to keep the page cache consistent, and wiring everything into the Debezium framework so events can reach Kafka.

The goal is a continuous streaming loop that processes every committed SQLite transaction as a stream of Debezium change events and controls checkpointing safely.

### Deliverables

- WatchService monitoring `.db-wal` for file modification events
- PRAGMA data_version gate confirming committed writes before parsing
- Full streaming loop driving the WAL parsing engine
- Checkpoint lifecycle
- Correct restart recovery from stored offset (wal_frame_index, data_version)
- An Emitter building Debezium SourceRecord objects with correct before/after Struct values and Avro-compatible naming

### How changes are detected

![alt text](images/module5-streaming-loop.png)

The connector uses a two-layer detection mechanism to know when a committed write has occurred:

1. **Layer 1 - Watch Service:** Java's WatchService API registers the directory containing the .db-wal file for OS-level file modification events. When any process writes to the WAL, the OS notifies the connector immediately.

2. **Layer 2 - PRAGMA data_version:** WatchService fires on every write to the WAL file, including mid-transaction writes that are not yet committed. PRAGMA data_version is a SQLite counter that only increments when a transaction fully commits. The connector reads this value after each WatchService event and compares it to the stored value. If it has not changed, the WAL write was not a commit and the connector ignores it and waits for the next event.


### The Checkpoint Lifecycle

The WAL file cannot grow indefinitely and SQLite needs to periodically checkpoint to reclaim space and maintain read performance. But if a checkpoint happens unexpectedly while the connector is reading, the page cache becomes stale. It holds old page states that no longer match what is in the WAL which leads to incorrect before values in change events.

![alt text](images/module5-checkpoint-lifecycle.png)

We can solve this using this sequence:

- **Hold BEGIN:** The connector maintains an open read transaction (BEGIN) on a dedicated reader connection. According to the SQLite WAL documentation, a writer will only reset the WAL if no readers are currently using it. Holding BEGIN registers the connector as an active reader, preventing the WAL from being reset while the connector is mid-cycle.

- **WAL hook:** The connector registers a WAL hook via sqlite3_wal_hook() (accessible through the native API of org.xerial:sqlite-jdbc) that fires after every commit. By returning zero from this hook, the connector suppresses all automatic checkpoints on the database entirely regardless of which connection triggers them.

- **Periodic controlled checkpoint:** To prevent the WAL from growing without bound, the connector should perform its own checkpoint at configurable intervals. The connector needs to release BEGIN, run PRAGMA wal_checkpoint(FULL), clear the page cache and re-acquire BEGIN. Because the connector controls the timing, it knows the cache is intentionally stale during this window and rebuilds it cleanly from the updated .db file on the next reads.

### Restart Recovery
Every time a transaction is processed, the connector should update its offset:
```json
{
  "snapshot_completed": true,
  "data_version": 42,
  "wal_frame_index": 17,
  "tables": {
    "orders": { "last_rowid": 150 }
  }
}
```

On restart, the connector reads this offset from Kafka Connect.
1. If data_version matches the current database value, that means nothing changed while the connector was down, resume from wal_frame_index
2. If data_version is higher then changes were missed and we need to re-read WAL from wal_frame_index to catch up
3. If WAL has been reset (salt mismatch at stored frame index) then perform a new snapshot

## Module 6 — Testing & Documentation

### Preface & Goal

The connector is only good if it works in every scenario. The WAL parsing engine involves low-level binary decoding with several subtle edge cases like overflow pages, checkpoint boundaries, salt validation. This module builds the confidence through targeted unit tests on the parsing layer and end-to-end integration tests across every scenario the connector is expected to handle.

### Deliverables
- Unit tests for each WAL parsing class with known binary inputs and expected outputs
- Integration tests covering all major CDC scenarios end-to-end
- README with setup instructions, config reference, and deployment guide
- Documentation of known limitations


# Roadmap

## Phase 1

### Community Bonding

Before coding starts I plan to:
- Set up the full Debezium development environment and confirm the build works end to end
- Study the Postgres connector source code in depth — specifically the connector task, streaming source, change record emitter, and offset context — as these are the direct reference implementations for the SQLite connector
- Read the official SQLite WAL documentation and file format specification in detail.
- Discuss and finalise the detailed design with mentors, particularly the WAL hook API access via `sqlite-jdbc`, the `WITHOUT ROWID` handling approach, and overflow page scope for Phase 1.

---

### Week 1: Project Skeleton

- Set up Maven project with correct Debezium BOM, sqlite-jdbc, and Kafka Connect dependencies
- Implement the connector entry point.
- Implement a JDBC connection wrapper that enforces WAL mode on startup and disables auto-checkpoint.
- Implement error handling.
- Connector JAR builds, connects to a SQLite file, and enforces WAL mode.

---

### Week 2 — Schema Handling

- Implement a schema registry that reads all table definitions dynamically from sqlite_master.
- Build and maintain a root page number to table name map so the WAL parsing engine can immediately identify which table a given WAL frame belongs to
- Implement type affinity mapping from SQLite's five affinities to Kafka Connect schema types

---

### Week 3 — Initial Snapshot

- Implement the snapshot phase using `BEGIN IMMEDIATE` to acquire an exclusive writer lock and get the initial state.
- Implement an offset context that stores `snapshot_completed`, `data_version`, `wal_frame_index`, and per-table `last_rowid`
- Wire the connector task's `start()`, `poll()`, and `stop()` lifecycle together end to end
- Integration test: snapshot produces correct `READ` events for all rows

---

### Week 4 — Varint Decoder & Page Decoder

- Implement a varint decoder for SQLite's variable-length integer encoding.
- Implement a page decoder that checks the page type byte (`0x0D` for table leaf), reads the cell pointer array, and for each cell parses required details. 
- Handle all serial type categories: NULL, integers of varying widths, float64, integer constants, TEXT, and BLOB
- Unit tests using real WAL binary snapshots captured from the Python prototype

---

### Week 5 — WAL Reader & Differ

- Implement a WAL reader that opens `.db-wal` as a `FileChannel`, parses the 32-byte WAL header to extract page size and salt values, reads frame headers sequentially, validates salts, and groups frames into committed transactions using the commit marker.
- Implement a WAL differ that retrieves old page state from the page cache or the database file as a fallback, calls the page decoder on both old and new pages, diffs decoded rows by rowid, and produces INSERT/UPDATE/DELETE change events with correct before/after values.
- Unit tests: INSERT/UPDATE/DELETE detection from known old/new page pairs

---

## Phase 2 — Midterm

At this point we have snapshot working end to end, WAL parsing engine fully unit tested, basic streaming working for simple tables. Remaining work is streaming orchestration.

---

### Week 6 — Streaming Loop

- Implement the main streaming loop: `WatchService` on the `.db-wal` directory for OS-level file modification events.
- Implement a change record emitter that builds Debezium `SourceRecord` objects with before/after `Struct` values and Avro-compatible field and topic naming.
- Update the offset after every transaction with the latest `data_version` and `wal_frame_index`
- Integration test: INSERT/UPDATE/DELETE each produce correct `SourceRecord` with before/after values

---

### Week 7 — Checkpoint Lifecycle

- Hold `BEGIN` on a dedicated reader connection to prevent WAL reset mid-cycle
- Register a WAL hook via `sqlite3_wal_hook()` through the `sqlite-jdbc` native API to suppress all automatic checkpoints regardless of which connection triggers them
- Implement periodic controlled checkpoint
- Expose a config field to control checkpoint frequency
- Integration test: connector produces correct before/after values across a controlled checkpoint

---

### Week 8 — Restart Recovery & Schema Changes

- Implement restart recovery.
- Implement schema change handling.
- Integration tests: restart with no missed events. Handle `ALTER TABLE without connector restart

---

### Week 9 — Overflow Pages & WITHOUT ROWID

- Implement overflow page chain following in the page decoder and detect when a cell's payload exceeds the inline threshold.
- Implement `WITHOUT ROWID` table handling.
- Integration tests: large row payloads decoded correctly, `WITHOUT ROWID` table changes captured

---

### Week 10 — Integration Testing & Bug Fixes

- Full integration test suite: snapshot, streaming, restart, schema change, checkpoint, overflow pages, multi-table, `WITHOUT ROWID`
- Fix any bugs surfaced by integration tests
- Stress test: high write throughput, WAL growing large, checkpoint under load.
- Verify offset correctness under all restart scenarios.

---

### Week 11 — Polish & Code Review Preparation

- Dedicated week for code quality, addressing mentor review feedback, and preparing the final writeup. No new features, the main focus is on making the existing code production-ready and ensuring the project is in a state that can be submitted and maintained by the community.

---

### Final Week — Documentation & Submission

- Complete README: prerequisites, quick start, full configuration reference, deployment guide for both Kafka Connect and Embedded Engine
- Document known limitations
- Final code submission to the Debezium organisation
- Final report written and submitted via the GSoC website

## Other commitments

I am a full-time student at the Indian Institute of Information Technology. I currently have a part-time internship which will conclude before the GSoC coding period begins and will not overlap with any development work.

I also have a Bachelor's Thesis Project (BTP) running through the summer, expected to wrap up by end of June. However, this period coincides with my college vacations from May to mid-July, so I will have full availability for GSoC without any conflict.

From May to mid-July I am on college vacation, which means no classes and maximum flexibility outside of the BTP. From mid-July onwards, classes resume from 10am to 4pm, after which I am completely free. The bulk of the complex work — streaming orchestration, checkpoint lifecycle, edge cases, and integration testing (Weeks 6-10) — falls in this period, and the post-4pm availability is more than sufficient to maintain full GSoC pace for those weeks.

I am committed to communicating proactively with my mentors if anything changes, and to maintaining consistent weekly updates regardless of workload elsewhere.

## Post GSOC Commitments

