# DDD-59: Generalizing per-table / per-chunk snapshot retry

Tracking issue: [debezium/dbz#2297](https://github.com/debezium/dbz/issues/2297).
Incorporates review feedback from @Naros on PR #60 (resume-from-key, whole-snapshot budget,
constant backoff default, deprecated-alias for the Oracle property, Postgres pinning split out).

## Motivation

During the initial snapshot Debezium reads every captured table through a fixed-size thread pool,
splitting large tables into chunks when configured
(`RelationalSnapshotChangeEventSource#createDataEvents`). When one table or chunk read throws a
`SQLException` — a connection reset partway through a large table, for instance — the callable wraps
it in a `ConnectException`, it escapes the `ExecutorCompletionService`, and `doExecute` fails the
whole snapshot.

The only recovery today is the connector-level `errors.max.retries`, which restarts the snapshot
from the beginning. On a large dataset that is the wrong unit of recovery: it re-reads the tables
and chunks that already succeeded (topic duplicates), and unwinds all the way back to where the
offset and the consistent-read position were established. A chunk that fails for a transient reason
should be recoverable by retrying *that chunk*.

## Prior art

The Oracle connector already does this connector-locally: `OracleSnapshotChangeEventSource` overrides
`createDataEventsForTableCallable` with a bounded retry loop gated by
`snapshot.database.errors.max.retries` (`int`, default `0`), with a narrow retriability check
(`isTableSnapshotErrorRetriable` — only ORA-01466), backing off with `Metronome`, firing
`notifyCompletedTableWithError` per attempt, hand-rolled rather than via `createPooledResourceCallable`.

So the question isn't whether snapshot reads should be retriable — one connector already decided yes.
It's whether that belongs in the common snapshot layer for every relational connector, and what a
connector-agnostic version has to get right that the Oracle-only one could sidestep.

## Goals

- A common, opt-in per-table/per-chunk retry in `RelationalSnapshotChangeEventSource`, with a
  connector-overridable retriability predicate.
- Default to current behavior; `0` retries changes nothing.
- Reconcile with Oracle's existing `snapshot.database.errors.max.retries` via a deprecated alias
  rather than a second overlapping knob.
- Retry by **resuming from the last-emitted key** rather than re-emitting a chunk, so retry does not
  produce duplicate rows or duplicate `FIRST`/`LAST` markers — using the mechanism the incremental
  snapshot already relies on.

Non-goals: changing `errors.max.retries` (whole-snapshot retry still sits above this); resuming
across a connector restart (v1 keeps the high-water key in memory for same-run retry — see caveats).

## Proposed changes

### Configuration

| Property | Type | Default | Meaning |
|---|---|---|---|
| `snapshot.retries.max` | int | `0` | Times the snapshot may retry a retriable table/chunk failure, as one **whole-snapshot** budget. `0` = current behavior. |
| `snapshot.retry.delay.ms` | long | `10000` | Delay between attempts. |

Per @Naros, this is a single whole-snapshot budget rather than per-unit — simpler contract, and
per-unit granularity can be added later if needed (easier than walking it back). Following house
style (`SNAPSHOT_LOCK_TIMEOUT_MS` / `snapshotLockTimeout()` returning `Duration`), the delay getter
returns a `Duration` and the max field is `Field::isNonNegativeInteger`-validated.

**Reconciling with Oracle.** The common property becomes canonical; Oracle's
`snapshot.database.errors.max.retries` is wired as a deprecated alias via
`Field.withDeprecatedAliases(...)` (the same mechanism Oracle already uses for
`DEPRECATED_XSTREAM_SERVER_NAME`), so existing Oracle configs keep working and get the deprecation
warning, and the alias can be dropped after a release or two. Oracle keeps only its retriability
override (ORA-01466), not the whole loop.

### Retriability, not "any SQLException"

Retrying every `SQLException` would burn the budget on permanent failures (bad credentials, a broken
`snapshot.select.statement.overrides`, a revoked grant). The loop consults an overridable predicate:

```java
protected boolean isSnapshotErrorRetriable(SQLException e) {
    return false; // conservative default; connectors opt specific error classes in
}
```

Oracle overrides it with its existing ORA-01466 check. Connectors wanting general transient-error
retry (the dbz#2297 connection-reset case) can widen it — e.g. `SQLTransientException` / SQLState
class `08` (connection exceptions). Default `false` keeps it strictly opt-in per connector.

### Where the retry goes

`createDataEventsForTableCallable` and `createDataEventsForChunkedTableCallable` are the two rethrow
sites, so they're the boundary. Each wraps its `doCreateDataEventsFor…` call in a bounded loop that
retries only when `isSnapshotErrorRetriable` says so; `InterruptedException` (connector stopping)
always propagates immediately. `getSnapshotSourceTimestamp()` converts its own `SQLException` into a
`ConnectException` before the row scan, so the loop unwraps a `ConnectException` whose cause is a
retriable `SQLException` as well as the bare `SQLException`.

### Resuming from the last-emitted key (instead of re-emitting)

This is the change that keeps retry from producing duplicates, and it turns out the machinery already
exists. Snapshot rows are dispatched per-row mid-scan (`dispatchSnapshotEvent`), so naively
re-running a chunk would re-emit rows already sent and could emit a second `FIRST` marker. Instead,
on retry we resume from the last key we emitted:

- **Chunked path — natural fit.** `SnapshotChunkQueryBuilder` already bounds each chunk by
  `key >= lower AND key < upper` and always appends `ORDER BY <keyCols>`. On retry we tighten the
  lower bound from `>= lower` to `> lastEmittedKey`; the exclusive composite-key form already exists
  (`CascadingOrBoundaryConditions.buildLowerBound(cols, sql, /*inclusiveFinal=*/false)`). This is
  exactly what the **incremental** snapshot already does — `AbstractChunkQueryBuilder` tracks the
  last-emitted key and builds `key > lastKey` + `ORDER BY key`. So this is porting a proven mechanism
  into the initial chunked path, not inventing one.
- **Legacy single-table path — needs an `ORDER BY`.** `doCreateDataEventsForTable` runs the raw
  `SELECT … FROM <table>` with no ordering. To resume it we add `ORDER BY <key>` (the key is
  available via `getKeyColumnsForChunking(table)`) and track the high-water key. Cost: free on
  clustered-PK engines (InnoDB/MySQL/MariaDB, SQL Server clustered index — already in key order),
  a real but bounded cost on Postgres heap / large secondary-PK tables (an added sort/index scan).
- The high-water key is recorded at emit time from the row via `TableSchema.keyFromColumnData(row)`.

**Fallback — keyless and select-override tables.** Tables with no usable key, and tables under
`snapshot.select.overrides`, already run as a single unbounded, unordered chunk. With no key there's
no high-water mark, so these fall back to re-read-and-accept-duplicates on retry (Debezium snapshots
are already at-least-once; consumers dedupe on key). This is the only case where retry can duplicate,
and it's documented as such.

**Two honest caveats:**

- *Same-run only in v1.* The offset persists a `SnapshotRecord` marker, not the last-emitted key. So
  in-memory resume covers a retry within the same run cheaply; resuming after a connector restart
  would need new offset state (persist the last key per chunk/table) — a larger change, proposed as a
  follow-up rather than part of v1.
- *Custom nullable keys.* The initial chunked builder does not do NULL-aware key comparison, whereas
  the incremental builder deliberately does. Primary keys are non-null so the default is fine, but
  `message.key.columns` can point at nullable columns; resume on such keys must adopt the incremental
  builder's NULL-aware bounds, or fall back to the duplicate-accept path.

A resumed read runs in the retry's transaction, so its rows carry that read's `ts_ms` — correct,
since they weren't emitted before, though a single chunk can then span two source-time reads; worth
stating in the docs.

### Connection recovery

A failed statement can leave the pooled `JdbcConnection` in an aborted-transaction state (on
Postgres, every later statement fails until a rollback), which `isValid()` won't detect. So
`recoverConnection` rolls back and reconnects (the `RetriableConnection` pattern from dbz#2244)
rather than a liveness check. It's a connector-overridable hook, because reconnecting has to reapply
connector-specific per-connection state — Oracle's PDB context, and (once the fix below lands)
Postgres's exported-snapshot pin.

### Consistency per connector

Whether resuming in a new transaction is even needed depends on how each connector pins the read:

- **Oracle** embeds the SCN in the query (`… AS OF SCN …`), so any read, original or resumed, returns
  identical data. No drift; resume-from-key is purely a duplicate/marker optimization here.
- **MySQL / MariaDB** (MariaDB extends the same `BinlogSnapshotChangeEventSource`) hold a global read
  lock and `START TRANSACTION WITH CONSISTENT SNAPSHOT` at REPEATABLE READ for the whole read phase,
  released only after `awaitCompletion()`. No drift; the cost is availability (below).
- **SQL Server** pins via `SNAPSHOT` isolation (`TRANSACTION_SNAPSHOT`) set at transaction start in
  `connectionCreated`. A resumed read must re-establish the same snapshot isolation on a fresh
  transaction.
- **Postgres** uses the replication slot's exported snapshot (`SET TRANSACTION SNAPSHOT`), which today
  is only pinned on the main connection — see the separate fix below.
- **Db2** — snapshot mechanism to be documented from the `debezium-connector-db2` repo (not verified
  here); left as a follow-up rather than asserted.

### Postgres exported-snapshot pinning — separate fix

Postgres pins `SET TRANSACTION SNAPSHOT '<exported>'` only on the main connection; the extra pooled
connections used when `snapshot.max.threads > 1` copy the isolation level but not the exported
snapshot, so a parallel Postgres snapshot already isn't cross-connection consistent, independent of
retry. Per @Naros this is a standalone bug: fix it in `main` (backportable to 3.6) by overriding
`connectionPoolConnectionCreated` in `PostgresSnapshotChangeEventSource` to `rollback()` then issue
`SET TRANSACTION ISOLATION LEVEL REPEATABLE READ; SET TRANSACTION SNAPSHOT '<slotCreatedInfo.snapshotName()>'`
on each pooled connection, guarded by the same condition the main path uses (`slotCreatedInfo != null
&& !isOnDemand`, and only when streaming resumes from the snapshot). The pool is created after the
main exporting transaction and it stays open for the whole snapshot, so the name is importable. This
DDD assumes that fix and does not re-solve it.

### Availability tradeoff (MySQL/MariaDB global lock)

The global lock is held for the whole read phase and the pool is fixed-size, so a chunk parked in
`retries.max × retry.delay.ms` of backoff extends how long writes are blocked server-wide — on
exactly the large deployment the Motivation targets. This is an argument for a modest default delay,
and it informs the backoff choice below.

### Backoff

Constant by default. @Naros's point is decisive: on a connector with a bounded snapshot window
(Oracle ~15 min OOTB), exponential backoff from a 10s base forces a full restart by roughly the 8th
attempt, whereas constant 10s allows ~90 retries in the same window. Exponential can be an opt-in
later with a max-delay cap so it can't blow a connector's window.

### Observability

Snapshot metrics run through `SnapshotProgressListener` → `SnapshotMeter` → the `SnapshotMetricsMXBean`
trait, and existing per-table metrics expose `Map<String, Long>` breakdowns. A retry counter matches
that shape: `Map<String, Long> getTableSnapshotRetries()` on the trait, a listener callback, and a
`ConcurrentMap` field/mutator on `SnapshotMeter`, plus a WARN log per retry.

### Steps

1. Add `snapshot.retries.max` / `snapshot.retry.delay.ms` to `RelationalDatabaseConnectorConfig`
   (Duration getter, non-negative validation); wire Oracle's property via `withDeprecatedAliases`.
2. Add `isSnapshotErrorRetriable` (default `false`); move Oracle's ORA-01466 check onto it.
3. Add the whole-snapshot-budgeted retry loop to both callables, unwrapping `ConnectException(SQLException)`.
4. Resume-from-key: on retry, tighten the chunk lower bound to `> lastEmittedKey` (chunked) / add
   `ORDER BY <key>` + high-water tracking (legacy); record the key at emit via `keyFromColumnData`.
   Keyless / select-override tables fall back to duplicate-accept; custom nullable keys adopt the
   incremental NULL-aware bounds or fall back.
5. `recoverConnection` as a connector-overridable hook (default rollback + reconnect).
6. `getTableSnapshotRetries` metric across listener/meter/trait, plus WARN logging.
7. Tests: chunk fails N−1 then succeeds → completes with no duplicate rows and a single `FIRST`
   marker; fails N+1 → snapshot fails with the attempt count; `0` reproduces current behavior; a
   non-retriable error fails immediately without consuming the budget; `InterruptedException` aborts
   regardless; keyless table falls back to re-read.
8. Docs: the properties, the keyless duplicate-accept fallback, the `ts_ms` note, and per-connector
   consistency.

## Open questions

- Cross-restart resume (persisting the last-emitted key in the offset) — v1 or follow-up? Proposed
  follow-up.
- Db2 snapshot-consistency mechanism — needs the `debezium-connector-db2` repo to document.
- Whether the legacy-path `ORDER BY <key>` should be unconditional or only when retries are enabled
  (to avoid the Postgres sort cost for users who don't opt in).
