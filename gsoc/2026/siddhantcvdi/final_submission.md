# GSoC 2026 Final Submission - Siddhant Chaturvedi

**Organization:** Red Hat JBoss (Debezium)
**Project:** Debezium Source Connector for SQLite

---

## Abstract

SQLite is a lightweight, embedded relational database used across desktop, mobile, and edge or IoT applications, where it serves as the primary store for local application state. Despite that reach, no production-ready Change Data Capture connector existed for it. Unlike the databases Debezium already supports, SQLite exposes no logical replication stream: its write-ahead log is physical, storing changed database pages rather than a description of the rows that changed, so there is nothing for an external reader to subscribe to.

This project delivers an incubating Debezium source connector for SQLite that creates the logical change stream SQLite does not provide. For each captured table, the connector installs standard SQL triggers that record every insert, update, and delete into an internal `_debezium_cdc_log` table, in commit order, using only portable SQL and no changes to the host application. This table is the logical change log the connector maintains inside the database, the readable row-level stream that SQLite itself does not expose. It runs the database in WAL mode so capture and the application never block each other, performs a consistent initial snapshot anchored to the log's high-water mark for a lossless handoff to streaming, and then streams changes by polling the log table and emitting standard Debezium change events. The connector also detects schema changes while streaming, reconciles its triggers, and emits schema change events, and it maps SQLite's dynamically typed values onto fixed Kafka Connect schemas. By bridging SQLite with Kafka and Debezium Server, it brings audit trails, observability, and edge-to-cloud pipelines to the large ecosystem of applications already built on SQLite.

---

## Code and Artifacts

**Project Repository:** [debezium/debezium-connector-sqlite](https://github.com/debezium/debezium-connector-sqlite)

### Connector skeleton and configuration

- The standard Debezium source-connector contract adapted to an embedded, file-based database with no server and no logical WAL: `SQLiteSourceConnector`, `SQLiteConnectorTask`, `SQLiteConnectorConfig`, `SQLitePartition`, `SQLiteOffsetContext`, `SQLiteSourceInfo`, `SQLiteSourceInfoStructMaker`, `SQLiteDatabaseSchema`, `SQLiteErrorHandler`, and `SQLiteChangeEventSourceFactory`.
- The connector's position is a single monotonic `change_id` from the internal log, carried in `SQLiteOffsetContext` alongside the snapshot-completion flag that gates the handoff. Because SQLite is reached through a file path rather than a network endpoint, the configuration excludes the relational host, port, user, password, and database-name fields, and adds `database.file.path` plus connector-specific options for the poll batch size, log-compaction threshold, snapshot mode, and affinity-mismatch handling.

### CDC log contract and trigger generation

- `CdcLog` freezes the contract of the `_debezium_cdc_log` table: a monotonically increasing `change_id`, the table name, the operation, the old and new row serialised as JSON, and a commit timestamp. `TriggerGenerator` builds the `AFTER INSERT`, `AFTER UPDATE`, and `AFTER DELETE` statements that write one row per change using SQLite's own `json_object(OLD, NEW)`, so a change and its log row commit together in a single transaction.
- This is the connector's logical-log generator: it turns SQLite's physical, page-level WAL into a readable, row-level, commit-ordered stream using only standard SQL.

### Trigger installation and reconciliation

- `TriggerInstaller` and `TriggerReconciler` install the capture triggers on startup and keep them in step with the schema. Because a hardcoded trigger is blind to DDL, the connector watches SQLite's `PRAGMA schema_version` and reconciles the triggers when the schema moves, rebuilding them against the current columns.

### Initial snapshot and streaming handoff

- `SQLiteSnapshotChangeEventSource` performs a consistent initial snapshot. It opens a WAL read transaction, reads the highest `change_id` in the log within that same view as the streaming start position, and emits every row of each captured table as a read (`op=r`) event. Because the mark and the data come from one consistent view, every change committed after it is left for streaming with no gap and no duplicate.

### Streaming: log polling and change extraction

- `SQLiteStreamingChangeEventSource` streams by treating `_debezium_cdc_log` as an ordinary table: each poll reads a bounded batch of rows with `change_id` greater than the stored offset, emits a Debezium change event for each in commit order, and advances the offset. Reading in small batches keeps an open read transaction from blocking SQLite's WAL checkpointing. `SQLiteChangeRecordEmitter` maps each log row to the standard Debezium envelope, carrying a full before and after image for updates and deletes.

### Schema change detection and schema change events

- The connector detects schema changes while streaming and emits schema change events for created, altered, and dropped tables, during the snapshot and during streaming. Because SQLite exposes no DDL text to an external reader, these events describe the change through a structured table model with a null `ddl` field, and a table rename is reported as a drop of the old name followed by a create of the new one.

### Type affinity and value conversion

- `SQLiteTypeAffinity` and `SQLiteValueConverter` resolve each column to a single Kafka Connect schema from its SQLite type affinity, then convert each value to fit. When a value's storage class does not match the column's affinity, the outcome is governed by the framework's event-conversion failure mode and a connector option that can substitute a typed placeholder for a non-nullable, no-default column. The rules are recorded in two architecture decision records in the repository.

### Restart, log compaction, and transient-error handling

- The connector resumes from its stored `change_id` after a restart, compacts the log by deleting already-committed rows once enough accumulate, and retries transient `SQLITE_BUSY` and `SQLITE_LOCKED` errors so ordinary write contention does not fail the task.

### Metrics and observability

- `SQLiteStreamingChangeEventSourceMetrics` and its MXBean, wired through `SQLiteChangeEventSourceMetricsFactory`, expose the standard Debezium streaming metrics plus SQLite-specific state over JMX: the current and committed `change_id` and the CDC log depth, so an operator can see capture progress and the size of the backlog still to be compacted.

### Documentation

- A full connector [documentation page](https://github.com/debezium/debezium/blob/main/documentation/modules/ROOT/pages/connectors/sqlite.adoc) (overview, how the connector works, data change events, schema change events, data type mappings, setup and deployment, connector properties, monitoring, and limitations) for the Debezium documentation site, plus a repository [README](https://github.com/debezium/debezium-connector-sqlite/blob/main/README.md).

---

## Worklog

**Parent Tracker Issue:** [debezium/dbz#2056](https://github.com/debezium/dbz/issues/2056)

There was no separate testing phase. Tests were written with each phase and had to pass before it was considered done. Unit tests cover logic such as type mapping, configuration validation, and offset math. Integration tests run against a real SQLite file for end-to-end behavior, with no Docker required.

The first three weeks were research and design: studying SQLite's internals (the WAL format, the B-tree page and record layout, and type affinity), evaluating the change-capture approaches, building prototypes, and writing the SQLite connector design document. Implementation then proceeded through an eight-phase plan.

| Phase | Work | Issue | PR |
|---|---|---|---|
| Phase 0: Foundations | Bootstrap the build | [dbz#2043](https://github.com/debezium/dbz/issues/2043) | [#1](https://github.com/debezium/debezium-connector-sqlite/pull/1) |
| Phase 0: Foundations | Connector skeleton classes | [dbz#2052](https://github.com/debezium/dbz/issues/2052) | [#2](https://github.com/debezium/debezium-connector-sqlite/pull/2) |
| Phase 0: Foundations | CDC log contract, test helper, and trigger generator | [dbz#2059](https://github.com/debezium/dbz/issues/2059) | [#3](https://github.com/debezium/debezium-connector-sqlite/pull/3) |
| Phases 1-3: Configuration, schema loading, snapshot | Configure, start, and snapshot | [dbz#2069](https://github.com/debezium/dbz/issues/2069) | [#6](https://github.com/debezium/debezium-connector-sqlite/pull/6) |
| Phases 1-3: Configuration, schema loading, snapshot | Install the CDC capture triggers | [dbz#2161](https://github.com/debezium/dbz/issues/2161) | [#7](https://github.com/debezium/debezium-connector-sqlite/pull/7) |
| Phase 4: Streaming and handoff | Stream ongoing changes and hand off from the snapshot | [dbz#2160](https://github.com/debezium/dbz/issues/2160) | [#8](https://github.com/debezium/debezium-connector-sqlite/pull/8) |
| Phase 5: Schema change detection | Detect schema changes while streaming | [dbz#2377](https://github.com/debezium/dbz/issues/2377) | [#10](https://github.com/debezium/debezium-connector-sqlite/pull/10) |
| Phase 5: Schema change detection | Emit schema change events | [dbz#2486](https://github.com/debezium/dbz/issues/2486) | [#13](https://github.com/debezium/debezium-connector-sqlite/pull/13) |
| Phase 6: Offset management and log compaction | Resume, compact, and retry transient errors | [dbz#2485](https://github.com/debezium/dbz/issues/2485) | [#12](https://github.com/debezium/debezium-connector-sqlite/pull/12) |
| Phase 7: Hardening | Streaming metrics and concurrency hardening | [dbz#2582](https://github.com/debezium/dbz/issues/2582) | [#14](https://github.com/debezium/debezium-connector-sqlite/pull/14) |
| Phase 8: Documentation | Connector documentation page (core `debezium/debezium`) and README | [dbz#2542](https://github.com/debezium/dbz/issues/2542) | [#8038](https://github.com/debezium/debezium/pull/8038) |
| Supporting | Build against Debezium 3.7.0 | [dbz#2583](https://github.com/debezium/dbz/issues/2583) | [#15](https://github.com/debezium/debezium-connector-sqlite/pull/15) |

---

## Articles & Talks

- **Blog Post (pending publication):** _"Change Data Capture for SQLite"_, drafted for the official Debezium blog. It covers why SQLite is a hard target for CDC, how the connector generates and reads a logical change log, and how it fits into a Debezium pipeline.
- **GSoC Presentation:** [SQLite Debezium Source Connector](https://docs.google.com/presentation/d/1WSerO86HAvq3pYF_gEweJae6mENROSkbQ-ZJ6paAdWc/edit?usp=sharing), covering the connector's design, the core problem, and a live demo, prepared for the Debezium community.

---

## Future Work

- **Trigger-free logical log via a C extension.** A loadable extension that registers SQLite's `sqlite3_preupdate_hook` would remove trigger write-amplification and the schema-change reconciliation, at the cost of a per-platform binary and an application-side change. It is the main alternative to the trigger-based generator and a natural next iteration.
- **Support for related databases such as Turso.** Turso builds on SQLite and exposes a compatible change-capture model, so extending the connector or its approach to Turso is a promising direction.
- **WebAssembly.** Explore whether the source connector, running only on the embedded Debezium engine, can be compiled to WebAssembly to run in constrained or browser environments.
