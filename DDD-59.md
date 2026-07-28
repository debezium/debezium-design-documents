# DDD-59: Generalizing per-table / per-chunk snapshot retry

Tracking issue: [debezium/dbz#2297](https://github.com/debezium/dbz/issues/2297).

## Motivation

During the initial snapshot Debezium reads every captured table through a fixed-size thread pool,
splitting large tables into chunks when configured
(`RelationalSnapshotChangeEventSource#createDataEvents`). When one table or chunk read throws a
`SQLException` — a connection reset partway through a large table, for instance — the callable wraps
it in a `ConnectException`, it escapes the `ExecutorCompletionService`, and `doExecute` fails the
whole snapshot.

The only recovery today is the connector-level `errors.max.retries`, which restarts the snapshot
from the beginning. On a large dataset that is the wrong unit of recovery:

- A restart re-reads the tables and chunks that already succeeded. There's no chance to clear the
  destination topics in between, so a topic can accumulate a very large number of duplicate records
  on a broker sized for steady-state retention.
- A restart unwinds all the way back to where the offset and the consistent-read position were
  established — a fresh export snapshot id, a new flashback/exported-snapshot position, the schema
  read again — to recover from one chunk that hit a transient error.

If a chunk fails for a reason likely to clear on its own, we should retry *that chunk* rather than
the whole snapshot.

## Prior art

This isn't a greenfield idea in the codebase — the Oracle connector already does it, connector-
locally. `OracleSnapshotChangeEventSource` overrides `createDataEventsForTableCallable` with a
bounded retry loop (`OracleSnapshotChangeEventSource.java:290-355`) gated by
`snapshot.database.errors.max.retries` (`OracleConnectorConfig.java:760`, an `int`, default `0`,
`isNonNegativeInteger`-validated). Three things about that implementation shape this proposal:

- Its retriability check is deliberately narrow — `isTableSnapshotErrorRetriable` only retries
  `ORA-01466` (flashback metadata changed), not "any `SQLException`".
- It backs off with `Metronome`, not `DelayStrategy`.
- It fires `notifyCompletedTableWithError` on every failed attempt, and it's hand-rolled rather than
  going through `createPooledResourceCallable`.

So the real question dbz#2297 raises isn't "should snapshot reads be retriable" — one connector
already decided yes. It's whether that belongs in the common snapshot layer for every relational
connector, and what a connector-agnostic version has to get right that the Oracle-only one could
sidestep. That's what this document is about.

## Goals

- Provide a common, opt-in per-table/per-chunk retry in `RelationalSnapshotChangeEventSource`, with
  a connector-overridable notion of which errors are retriable.
- Default to current behavior; with retries set to `0` nothing changes.
- Reconcile with Oracle's existing `snapshot.database.errors.max.retries` rather than adding a second
  overlapping knob.
- Be honest in the design and the docs about what retry does and does not prevent (duplicates are
  reduced in blast radius, not eliminated — see below).

Non-goals: changing `errors.max.retries` (whole-snapshot retry still sits above this), and resuming
a chunk mid-scan — a retry re-runs the chunk query from its boundary.

## Proposed changes

### Configuration

| Property | Type | Default | Meaning |
|---|---|---|---|
| `snapshot.unit.retries.max` | int | `0` | Attempts to retry a single retriable table/chunk failure before it propagates. `0` = current behavior. |
| `snapshot.unit.retry.delay.ms` | long | `10000` | Delay between attempts. |

Following house style (cf. `SNAPSHOT_LOCK_TIMEOUT_MS` / `snapshotLockTimeout()` in
`RelationalDatabaseConnectorConfig`), the delay getter returns a `Duration`
(`snapshotUnitRetryDelay()`), and the max-retries field is validated with `Field::isNonNegativeInteger`
— the same shape as Oracle's existing property.

One property covers both table and chunk failures; they're the same shape and a second knob only
raises the question of what happens when the two disagree.

**Reconciling with Oracle.** Oracle's `snapshot.database.errors.max.retries` predates this. The
cleanest path is to make the common property the canonical one and treat Oracle's as a deprecated
alias mapping to it, with `OracleSnapshotChangeEventSource` overriding only the *retriability
predicate* (keep ORA-01466) rather than owning the whole loop. Exact deprecation mechanics are an
open question below.

### Retriability, not "any SQLException"

Retrying every `SQLException` would burn the whole budget on permanent failures — bad credentials, a
broken `snapshot.select.statement.overrides`, a revoked grant — and fail anyway, just slower. The
loop consults an overridable predicate:

```java
protected boolean isSnapshotErrorRetriable(SQLException e) {
    return false; // conservative default; connectors opt specific error classes in
}
```

Oracle overrides it with its existing ORA-01466 check. Connectors that want general transient-error
retry (the dbz#2297 use case, driven by connection resets) can widen it — e.g. keying off
`SQLException` subtype / `SQLTransientException` / SQLState class `08` (connection exceptions). The
default staying `false` keeps this strictly opt-in per connector.

### Where the retry goes

`createDataEventsForTableCallable` and `createDataEventsForChunkedTableCallable` are the two rethrow
sites, so they're the boundary. Each wraps its `doCreateDataEventsFor…` call in a bounded loop that
retries only when `isSnapshotErrorRetriable` says so; `InterruptedException` (connector stopping)
always propagates immediately.

One subtlety: `getSnapshotSourceTimestamp()` is called near the top of both `doCreateDataEventsForTable`
and `doCreateDataEventsForChunk` and converts its own `SQLException` into a `ConnectException`
*before* the row scan. To actually cover a transient failure there, the loop catches a
`ConnectException` whose cause is a retriable `SQLException` as well as the `SQLException` itself,
rather than only the latter.

Sketch (chunk callable; the table version is identical in shape):

```java
final DelayStrategy delay = DelayStrategy.constant(connectorConfig.snapshotUnitRetryDelay());
int attempt = 0;
while (true) {
    if (!sourceContext.isRunning()) {
        throw new InterruptedException("Interrupted while snapshotting chunk " + chunk.getChunkId());
    }
    try {
        doCreateDataEventsForChunk(sourceContext, snapshotContext, offset, snapshotReceiver,
                chunk, progressMap, snapshotProgress, connection);
        return;
    }
    catch (SQLException | ConnectException e) {
        final SQLException sql = asRetriableSqlException(e); // unwraps ConnectException cause
        if (sql == null || attempt++ >= connectorConfig.snapshotUnitRetriesMax()) {
            notificationService.initialSnapshotNotificationService().notifyCompletedTableWithError(
                    snapshotContext.partition, snapshotContext.offset, chunk.getTableId().identifier());
            throw new ConnectException("Snapshotting of table " + chunk.getTableId()
                    + " chunk " + chunk.getChunkId() + " failed after " + attempt + " attempt(s)", e);
        }
        recoverConnection(connection);            // see below
        LOGGER.warn("Chunk {} of table {} failed, retrying (attempt {} of {})",
                chunk.getChunkId(), chunk.getTableId(), attempt, connectorConfig.snapshotUnitRetriesMax(), e);
        delay.sleepWhen(true);
    }
}
```

### Connection recovery

A failed statement can leave the pooled `JdbcConnection` in an aborted-transaction state (on
Postgres, every subsequent statement fails with "current transaction is aborted" until a rollback).
`isValid()` — the check already used when borrowing from the pool — reports the socket as healthy and
would let the retry fail instantly, burning the budget. So `recoverConnection` follows the
`RetriableConnection` pattern from dbz#2244: roll back, and reconnect if the connection is no longer
usable, rather than a liveness check alone.

Reconnecting also has to reapply any connector-specific per-connection state that a fresh connection
wouldn't have — Postgres's exported-snapshot pin, Oracle's PDB context (already set up via
`connectionPoolConnectionCreated`). So `recoverConnection` is a connector-overridable hook with a
default of rollback-plus-reconnect, not a fixed sequence.

### Duplicates: reduced, not eliminated

This is the part the Oracle-only version could gloss over and a general one can't. Snapshot rows are
dispatched **per row, mid-scan** (`emitRecordWithCoordination` → `dispatcher.dispatchSnapshotEvent`,
`RelationalSnapshotChangeEventSource.java` ~L681-728), not buffered until the chunk completes. So a
chunk that fails 80% through has already sent those rows downstream; retrying re-scans the chunk from
its boundary and re-dispatches them.

That means chunk retry **shrinks the blast radius** (one chunk's rows, not the whole snapshot) but
does not make retry duplicate-free. Debezium snapshots are already at-least-once and consumers are
expected to dedupe on key, so this is consistent with existing semantics — but the docs must say it
plainly rather than imply idempotency.

There's a sharper case than duplicate rows: the `SnapshotRecord.FIRST` / `LAST` markers. The first
row of a chunk fires `signalFirstRecordEmitted()` mid-scan; the underlying `CountDownLatch` signals
are idempotent, but the *emitted event* is not. If the chunk carrying the snapshot's first row fails
after emitting it and is retried, the retry emits a second `FIRST`-tagged record. The loop therefore
needs to gate marker emission on retried units (only emit `FIRST`/`LAST` on the first attempt), or we
accept and document duplicate markers — flagged in Open questions.

### Consistency is per-connector, not universal

Whether a retry in a new transaction drifts in time depends entirely on how the connector pins the
read, and it varies:

- **Oracle** embeds the SCN in the query text itself (`SELECT … AS OF SCN …`). Any execution,
  original or retried, on any connection, at any later time, returns identical data. No drift.
- **MySQL** under the default global read lock holds it until `createDataEvents` calls
  `releaseDataSnapshotLocks` — which only runs after `awaitCompletion()`, i.e. after every chunk
  including retried ones. No writes land anywhere server-side during the read phase, so no drift; the
  cost here is availability, not consistency (next point).
- **Postgres** is where it's genuinely nuanced, and more so than I first assumed. The exported-
  snapshot pin (`SET TRANSACTION SNAPSHOT '<exported>'`) is applied **once**, on the single
  connection that opens the snapshot transaction, and only when a replication slot was created for
  this run. The extra pooled connections used when `snapshot.max.threads > 1` are *not* pinned to the
  slot's snapshot today — they inherit the isolation level and start their own independent
  `REPEATABLE READ` transactions. So multi-threaded Postgres snapshots already don't guarantee
  cross-connection consistency, retry or not. A retry-aware design can re-issue `SET TRANSACTION
  SNAPSHOT` on the recovered connection — the exported snapshot stays importable as long as Debezium
  keeps the exporting replication connection open, which it does for the whole snapshot phase — but
  that's new per-connection infrastructure that would be *closing* this pre-existing gap, not just
  preserving something the retry breaks.

So the consistency contract is per connector rather than one blanket tradeoff. For Postgres it's also
an opportunity to tighten multi-threaded snapshot consistency generally; scoping that in or out is an
open question below.

### Availability tradeoff (MySQL global lock)

Because the global lock is held for the entire read phase and the pool is fixed-size, a chunk parked
in `retries.max × retry.delay.ms` of backoff occupies a worker slot and extends how long the lock
blocks writes across the whole server — on exactly the large, write-heavy deployment the Motivation
is trying to protect. Enabling snapshot retry under global-lock mode trades write availability for
avoiding a full restart; that has to be documented, and it's an argument for a modest default delay.

### Observability

Snapshot metrics run through `SnapshotProgressListener` → `SnapshotMeter` → the `SnapshotMetricsMXBean`
trait, not direct calls from the change-event source, and the existing per-table metrics expose
`Map<String, Long>` breakdowns. A retry counter should match that: add
`Map<String, Long> getTableSnapshotRetries()` on the trait, a listener callback, and a
`ConcurrentMap` field/mutator on `SnapshotMeter` — three files, plus the WARN log above.

### Steps

1. Add the two properties to `RelationalDatabaseConnectorConfig` (Duration getter, non-negative
   validation); wire Oracle's existing property as a deprecated alias.
2. Add `isSnapshotErrorRetriable` (default `false`) and move Oracle's ORA-01466 check onto it.
3. Add the bounded retry loop to both callables, unwrapping `ConnectException(SQLException)`.
4. Add `recoverConnection` as a connector-overridable hook (default rollback + reconnect); connectors
   with per-connection state reapply it — Postgres re-pins the exported snapshot, Oracle its PDB
   context.
5. Gate `FIRST`/`LAST` marker emission on retried units (or decide to document duplicate markers).
6. Add the `getTableSnapshotRetries` metric across listener/meter/trait, plus WARN logging.
7. Tests: a chunk failing N−1 times then succeeding completes; N+1 fails with the attempt count;
   `0` reproduces current behavior; a non-retriable error fails immediately without consuming the
   budget; `InterruptedException` aborts regardless; retry does not emit a second `FIRST` marker.
8. Docs: the properties, the duplicate/blast-radius note, and the per-connector consistency and
   availability contracts.

## Open questions

- Oracle reconciliation: deprecated alias mapping `snapshot.database.errors.max.retries` onto the new
  property, or keep both for a release with the Oracle one delegating?
- Marker duplication: gate `FIRST`/`LAST` on first attempt, or document that retried units may repeat
  them?
- Retry budget per-unit (proposed) or shared across the whole snapshot?
- Constant vs. exponential backoff — `DelayStrategy` supports both.
- Postgres already doesn't pin the extra pooled connections to the exported snapshot when
  `snapshot.max.threads > 1`, so multi-threaded snapshots have a cross-connection consistency gap
  today, independent of retry. Do we close that as part of this work (re-pin every pooled connection),
  or keep this DDD scoped to retry and track the gap separately?
