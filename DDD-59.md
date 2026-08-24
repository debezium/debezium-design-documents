# DDD-59: Generalizing per-table / per-chunk snapshot retry

Tracking issue: [debezium/dbz#2297](https://github.com/debezium/dbz/issues/2297).

## Motivation

During the initial snapshot Debezium reads every captured table through a fixed-size thread pool, splitting large tables into chunks when configured (`RelationalSnapshotChangeEventSource#createDataEvents`).
When one table or chunk read throws a `SQLException` — a connection reset partway through a large table, for instance — the callable wraps it in a `ConnectException`, it escapes the `ExecutorCompletionService`, and `doExecute` fails the whole snapshot.

The only recovery today is the connector-level `errors.max.retries`, which restarts the snapshot from the beginning.
That makes full snapshot re-consistency the only recovery mode on offer.
A user who would happily accept eventual consistency — re-read just the failed unit and let consumers dedupe on key — has no way to opt into a cheaper recovery.
That flexibility is the real motivation here: the whole-snapshot restart is expensive precisely because it insists on more consistency than many deployments actually need.
The cost is concrete on a large dataset.
The restart re-reads the tables and chunks that already succeeded (topic duplicates), and it unwinds all the way back to where the offset and the consistent-read position were first established.
A chunk that fails for a transient reason should instead be recoverable by retrying *that chunk*.

## Prior art

The Oracle connector already does this connector-locally.
`OracleSnapshotChangeEventSource` overrides `createDataEventsForTableCallable` with a bounded retry loop gated by `snapshot.database.errors.max.retries` (`int`, default `0`), with a narrow retriability check (`isTableSnapshotErrorRetriable` — only ORA-01466), backing off with `Metronome`, firing `notifyCompletedTableWithError` per attempt, hand-rolled rather than via `createPooledResourceCallable`.

Oracle can afford to retry a single table read this cheaply because of *how* it pins the snapshot.
The snapshot phase uses Flashback queries (`SELECT … AS OF SCN`), so every thread reads each table at the same SCN with no shared lock, transaction, or other server-side resource that has to survive across a retry.
A retried read at the same SCN simply returns identical data, so re-running one table is safe and self-consistent without any extra bookkeeping.
Most other connectors cannot sidestep the problem this way — see [Consistency per connector](#consistency-per-connector) — which is exactly what a connector-agnostic version has to account for.

So the question isn't whether snapshot reads should be retriable — one connector already decided yes.
It's whether that belongs in the common snapshot layer for every relational connector, and what a connector-agnostic version has to get right that the Oracle-only one could sidestep.

## Goals

- A common, opt-in per-table/per-chunk retry in `RelationalSnapshotChangeEventSource`, with a connector-overridable retriability predicate.
- Default to current behavior; `0` retries changes nothing.
- Reconcile with Oracle's existing `snapshot.database.errors.max.retries` via a deprecated alias rather than a second overlapping knob (see [Backward compatibility](#backward-compatibility)).
- Retry by **resuming from the last-emitted key** rather than re-emitting a chunk, so retry does not produce duplicate rows or duplicate `FIRST`/`LAST` markers — using the mechanism the incremental snapshot already relies on.

Non-goals:

- Changing `errors.max.retries` — the whole-snapshot retry still sits above this.
- Resuming a snapshot across a connector restart — explicitly out of scope; ad-hoc blocking and incremental snapshots already cover that (see caveats).

## Proposed changes

### Configuration

| Property | Type | Default | Meaning |
|---|---|---|---|
| `snapshot.retry.max` | int | `0` | Times the snapshot may retry a retriable table/chunk failure, as one **whole-snapshot** budget. `0` = current behavior. |
| `snapshot.retry.delay.ms` | long | `10000` | Delay between attempts. |

Both properties share the single `snapshot.retry` stem (`snapshot.retry.max` / `snapshot.retry.delay.ms`) so they read as one group rather than as two unrelated knobs.

This is a single whole-snapshot budget rather than per-unit: a simpler contract, and per-unit granularity can be added later if it's needed, which is easier than walking granularity back once it's been given.
Following house style (`SNAPSHOT_LOCK_TIMEOUT_MS` / `snapshotLockTimeout()` returning `Duration`), the delay getter returns a `Duration` and the max field is `Field::isNonNegativeInteger`-validated.

### Retriability, not "any SQLException"

Retrying every `SQLException` would burn the budget on permanent failures (bad credentials, a broken `snapshot.select.statement.overrides`, a revoked grant).
The loop consults an overridable predicate:

```java
protected boolean isSnapshotErrorRetriable(SQLException e) {
    return false; // conservative default; connectors opt specific error classes in
}
```

Oracle overrides it with its existing ORA-01466 check.
Connectors wanting general transient-error retry (the dbz#2297 connection-reset case) can widen it — e.g. `SQLTransientException` / SQLState class `08` (connection exceptions).
Default `false` keeps it strictly opt-in per connector.

### Where the retry goes

`createDataEventsForTableCallable` and `createDataEventsForChunkedTableCallable` are the two rethrow sites, so they're the boundary.
Each wraps its `doCreateDataEventsFor…` call in a bounded loop that retries only when `isSnapshotErrorRetriable` says so; `InterruptedException` (connector stopping) always propagates immediately.
`getSnapshotSourceTimestamp()` converts its own `SQLException` into a `ConnectException` before the row scan, so the loop unwraps a `ConnectException` whose cause is a retriable `SQLException` as well as the bare `SQLException`.

### Resuming from the last-emitted key (instead of re-emitting)

This is the change that keeps retry from producing duplicates, and it turns out the machinery already exists.
Snapshot rows are dispatched per-row mid-scan (`dispatchSnapshotEvent`), so naively re-running a chunk would re-emit rows already sent and could emit a second `FIRST` marker.
Instead, on retry we resume from the last key we emitted:

- **Chunked path — natural fit.**
  `SnapshotChunkQueryBuilder` already bounds each chunk by `key >= lower AND key < upper` and always appends `ORDER BY <keyCols>`.
  On retry we tighten the lower bound from `>= lower` to `> lastEmittedKey`; the exclusive composite-key form already exists (`CascadingOrBoundaryConditions.buildLowerBound(cols, sql, /*inclusiveFinal=*/false)`).
  This is exactly what the **incremental** snapshot already does — `AbstractChunkQueryBuilder` tracks the last-emitted key and builds `key > lastKey` + `ORDER BY key`.
  So this is porting a proven mechanism into the initial chunked path, not inventing one.
- **Single-threaded path — order added only on opt-in.**
  "Legacy path" here means the single-threaded snapshot (`snapshot.max.threads = 1`), which runs `doCreateDataEventsForTable` as one unbounded `SELECT … FROM <table>` with no ordering.
  That stays exactly as-is when retry is not opted in — no `ORDER BY` is added, so nothing changes for users who don't want this feature.
  When retry *is* opted in, resuming needs a deterministic order, so we sort by the primary key.
  On engines that store the table in primary-key order (InnoDB clustered index, SQL Server clustered index) that is the index's natural sort and effectively free; on a Postgres heap or a large secondary-PK table it is an added sort/index scan, which is a cost the user has explicitly accepted by turning retry on.
- The high-water key is recorded at emit time from the row via `TableSchema.keyFromColumnData(row)`.

**Fallback — keyless and select-override tables.**
Tables with no usable key, and tables under `snapshot.select.overrides`, already run as a single unbounded, unordered chunk.
With no key there's no high-water mark, so these fall back to re-read-and-accept-duplicates on retry (Debezium snapshots are already at-least-once; consumers dedupe on key).
This is the only case where retry can duplicate, and it's documented as such.

**Two honest caveats:**

- *Same-run only, by design.*
  The offset persists a `SnapshotRecord` marker, not the last-emitted key, so in-memory resume covers a retry within the same run cheaply.
  Resuming a snapshot that failed and then needs to continue across a connector restart is **out of scope** for this DDD — that need is already served by ad-hoc blocking snapshots and incremental snapshots, which persist their own resume state.
  v1 retry is same-run only because that's the right boundary, not as a stopgap.
- *Custom nullable keys.*
  The initial chunked builder does not do NULL-aware key comparison, whereas the incremental builder deliberately does.
  Primary keys are non-null so the default is fine, but `message.key.columns` can point at nullable columns; resume on such keys must adopt the incremental builder's NULL-aware bounds, or fall back to the duplicate-accept path.

A resumed read runs in the retry's transaction, so its rows carry that read's `ts_ms` — correct, since they weren't emitted before, though a single chunk can then span two source-time reads; worth stating in the docs.

### Connection recovery

A failed statement can leave the pooled `JdbcConnection` in an aborted-transaction state (on Postgres, every later statement fails until a rollback), which `isValid()` won't detect.
So `recoverConnection` rolls back and reconnects (the `RetriableConnection` pattern from dbz#2244) rather than a liveness check.
It's a connector-overridable hook, because reconnecting has to reapply connector-specific per-connection state — Oracle's PDB context, and (once the fix below lands) Postgres's exported-snapshot pin.

### Consistency per connector

Whether resuming in a new transaction reproduces the same read — and whether a parallel (multi-connection) snapshot is even consistent to begin with — comes down to one question per connector.
`RelationalSnapshotChangeEventSource#initializePooledConnection` copies only autocommit and the isolation *level* to each of the `snapshot.max.threads` pooled connections; it does not share a transaction or a snapshot view.
Two hooks let a connector re-pin the pooled connections — `connectionPoolConnectionCreated` and `getSnapshotConnectionFirstSelect` — so the question is: **does the pin travel in the query text (safe on any connection), or is it bound to one transaction/connection (which drifts unless every pooled connection re-pins the same view)?**

That sorts the connectors into three classes.

**Class A — the pin travels in the query text, so a parallel snapshot is safe for free.**

- **Oracle** (verified) bakes the SCN into each per-table Flashback query (`… AS OF SCN <scn>`, `SelectAllSnapshotQuery.java:52`); the pool hook only re-points the session at the PDB (`OracleSnapshotChangeEventSource.java:78`).
  Any read, original or resumed, on any thread, returns identical data, which is exactly why Oracle's existing per-table retry needs no extra machinery.
- **CockroachDB** and **YashanDB** (inferred, *not in this checkout*) would most naturally land here: CockroachDB's consistent-read primitive is `AS OF SYSTEM TIME <ts>` and YashanDB is deliberately Oracle-compatible (`AS OF SCN`), both timestamps carried in the query text.
  A snapshotter that threads one shared timestamp through all connections would be Oracle-class safe; this is inferred from each database's documented MVCC/time-travel semantics, not from Debezium source (no such snapshot code exists in this repo).

**Class B — a shareable exported snapshot exists but isn't shared yet, so it's drift-prone today and fixable to safe.**

- **Postgres** (verified) uses the replication slot's exported snapshot (`SET TRANSACTION ISOLATION LEVEL REPEATABLE READ; SET TRANSACTION SNAPSHOT '<name>'`, `PostgresSnapshotChangeEventSource.java:281-282`), which *is* designed to be shared across connections — but today it is set only on the main connection (line 107), so the pooled connections inherit only REPEATABLE READ and each starts an independent snapshot.
  Re-issuing `SET TRANSACTION SNAPSHOT '<name>'` on every pooled connection promotes Postgres into Class A — that is the separate fix below.

**Class C — isolation is bound to a transaction/connection with no shareable view, so a naive multi-connection snapshot drifts.**

- **MySQL / MariaDB** (verified; MariaDB extends the same `BinlogSnapshotChangeEventSource`) get their mutual consistency from the **global read lock** freezing writes, not from a shared snapshot: the main connection takes `FLUSH TABLES WITH READ LOCK` + `START TRANSACTION WITH CONSISTENT SNAPSHOT` at REPEATABLE READ (`BinlogSnapshotChangeEventSource.java:167,182`), and each pooled connection pins its *own* REPEATABLE READ view with a `<select> LIMIT 1` first-read (`getSnapshotConnectionFirstSelect`, line 477).
  With the lock held (the default) the N views coincide; under minimal / `*_no_table_locks` / read-only-incremental modes that drop the global lock, parallel connections can drift.
- **SQL Server** (verified) sets `SNAPSHOT` isolation at transaction start (`setTransactionIsolation(TRANSACTION_SNAPSHOT)`, `SqlServerSnapshotChangeEventSource.java:81`), but SNAPSHOT establishes its view at *each* transaction's first statement independently and there's no exported snapshot to share, so the pooled connections each pin a different point in time.
- **Db2 LUW, Db2 for i (IBMi), Informix, Ingres** (inferred, *not in this checkout* — separate community repos) all rely on a transaction isolation level (Db2 RR/RS, IBMi commitment control, Informix `SET ISOLATION TO REPEATABLE READ`, Ingres SERIALIZABLE/REPEATABLE-READ read-only) with no flashback and no exported snapshot, so a multi-connection snapshot would drift the same way SQL Server does.
  These are inferred from each database's documented isolation semantics, not verified against connector source.

What this means for retry: resume-from-key is the common mechanism, but a resumed read only *reproduces* the original point-in-time automatically for Class A.
Class B needs the pooled-connection pin (the Postgres fix).
For Class C the consistent snapshot depends on a held lock (MySQL) or on running single-threaded — so on those connectors retry either resumes inside the same still-consistent context, or, per the Motivation, is where a user consciously trades strict consistency for the cheaper recovery.
The six not-in-repo rows above are clearly marked as inferred; when those connectors move under this common path their real mechanism should be confirmed against their source and the table corrected.

### Postgres exported-snapshot pinning — separate fix

Postgres pins `SET TRANSACTION SNAPSHOT '<exported>'` only on the main connection; the extra pooled connections used when `snapshot.max.threads > 1` copy the isolation level but not the exported snapshot, so a parallel Postgres snapshot already isn't cross-connection consistent, independent of retry.
This is a standalone bug, best fixed in `main` (backportable to 3.6) rather than gated behind this DDD, by overriding `connectionPoolConnectionCreated` in `PostgresSnapshotChangeEventSource` to `rollback()` then issue `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ; SET TRANSACTION SNAPSHOT '<slotCreatedInfo.snapshotName()>'` on each pooled connection, guarded by the same condition the main path uses (`slotCreatedInfo != null && !isOnDemand`, and only when streaming resumes from the snapshot).
The pool is created after the main exporting transaction and it stays open for the whole snapshot, so the name is importable.
This DDD assumes that fix and does not re-solve it.
(Tracked separately as [debezium/dbz#2330](https://github.com/debezium/dbz/issues/2330) / PR [#7740](https://github.com/debezium/debezium/pull/7740).)

### Availability tradeoff (MySQL/MariaDB global lock)

The global lock is held for the whole read phase and the pool is fixed-size, so a chunk parked in `snapshot.retry.max × snapshot.retry.delay.ms` of backoff extends how long writes are blocked server-wide — on exactly the large deployment the Motivation targets.
This is an argument for a modest default delay, and it informs the backoff choice below.

### Backoff

Constant by default.
The deciding factor is the bounded snapshot window some connectors impose: on Oracle (~15 min OOTB), exponential backoff from a 10s base forces a full restart by roughly the 8th attempt, whereas constant 10s allows ~90 retries in the same window.
Exponential can be an opt-in later with a max-delay cap so it can't blow a connector's window.

### Observability

Rather than add a new MXBean metric, a retried snapshot unit is surfaced two ways that already exist.
A WARN log per retry records the unit, the attempt number, and the cause.
A notification is emitted through the existing Notifications API (`NotificationService`), so consumers that already watch notifications see retries without a new metric surface to maintain.
The log plus the notification is enough to start; a counter can be added later if one turns out to be wanted, but it shouldn't be the default surface.

### Backward compatibility

Oracle already ships `snapshot.database.errors.max.retries`, so the common property must not silently break existing Oracle configs.
The common `snapshot.retry.max` becomes canonical; Oracle's `snapshot.database.errors.max.retries` is wired as a deprecated alias via `Field.withDeprecatedAliases(...)` — the same mechanism Oracle already uses for `DEPRECATED_XSTREAM_SERVER_NAME`.
Existing Oracle configs keep working and get the standard deprecation warning, and the alias can be dropped after a release or two.
Oracle keeps only its retriability override (ORA-01466), not the whole retry loop.

### Steps

1. Add `snapshot.retry.max` / `snapshot.retry.delay.ms` to `RelationalDatabaseConnectorConfig` (Duration getter, non-negative validation); wire Oracle's property via `withDeprecatedAliases`.
2. Add `isSnapshotErrorRetriable` (default `false`); move Oracle's ORA-01466 check onto it.
3. Add the whole-snapshot-budgeted retry loop to both callables, unwrapping `ConnectException(SQLException)`.
4. Resume-from-key: on retry, tighten the chunk lower bound to `> lastEmittedKey` (chunked); in the single-threaded path add `ORDER BY <key>` + high-water tracking **only when retry is opted in**; record the key at emit via `keyFromColumnData`. Keyless / select-override tables fall back to duplicate-accept; custom nullable keys adopt the incremental NULL-aware bounds or fall back.
5. `recoverConnection` as a connector-overridable hook (default rollback + reconnect).
6. Surface retries via a WARN log per attempt and a `NotificationService` notification — no new metric.
7. Tests: chunk fails N−1 then succeeds → completes with no duplicate rows and a single `FIRST` marker; fails N+1 → snapshot fails with the attempt count; `0` reproduces current behavior; a non-retriable error fails immediately without consuming the budget; `InterruptedException` aborts regardless; keyless table falls back to re-read.
8. Docs: the properties, the keyless duplicate-accept fallback, the `ts_ms` note, and per-connector consistency.

## Open questions

- The general-transient retriability default for connectors that opt in beyond Oracle — SQLState class `08` only, or the broader `SQLTransientException` family? Leaning class `08` to stay conservative.
- Whether the retry notification reuses an existing notification type or warrants a dedicated one.
