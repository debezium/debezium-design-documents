# DDD-36: Oracle Multi-threaded Streaming

## Overview

This document describes a proposed redesign of the Debezium Oracle connector's LogMiner processing pipeline.
The current implementation is single-threaded, combining database reading, event processing, and event dispatch into a sequential flow, which has traditionally been the approach for all connectors.
But this approach for LogMiner leads to heap pressure from large in-flight transactions and limits throughput by coupling I/O-bound and CPU-bound work together.

The proposed architecture splits the pipeline into three logical threads, connected by two `ChronicleQueue` instances, both using disk-backed memory-mapped files for inter-thread communication.
This eliminates heap exhaustion for large transactions while improving throughput and scalability.

## Current Architecture

The existing single-threaded flow is:

1. Execute LogMiner SQL, iterate `V$LOGMNR_CONTENTS` results over a JDBC `ResultSet`.
2. Map each row to a `LogMinerEventRow` POJO, parse the associated `SQL_REDO`, and cache the POJO's metadata and parsed `SQL_REDO` as a `LogMinerEvent` object in the transaction buffer.
3. On `COMMIT`, pop the transaction off the buffer, apply merge/discard logic, and emit.
4. On `ROLLBACK`, pop the transaction off the buffer, discarding the entire transaction and event list.

The primary limitations of this approach are:

* **Heap Exhaustion**: Large or long-running transactions accumulate unbounded heap usage as events are buffered until a `COMMIT` or `ROLLBACK` is observed. 
While the connector does have off-heap buffer options using Infinispan and Ehcache, the disk-based persistence offered by these solutions is slow, and contributes to throughput issues.
* **Coupled I/O**: The database reader stalls while processing and emitting events, reducing the LogMiner query throughput.
* **No audit trail**: All LogMiner output is discarded after processing, with no ability to replay or inspect what was originally mined.
This becomes even more important as Oracle often only retains logs for a limited period of time (sometimes hours).

## Goal

The goal of this proposal is:

* Provide an alternative architecture that leverages multiple threads to scale the capture, processing, and emission of change events read from LogMiner.
This model mirrors a similar architecture used by Oracle XStream and GoldenGate.
* The implementation must be optional, and opt-in. Due to some of the limitations with `ChronicleQueue` and memory-mapped files, not every environment will be a good fit, and users need a fallback solution.
Additionally, if the single-threaded model is kept, it can act as a fallback when things go wrong during the multi-threaded development/feedback cycle of the feature.

## Pipeline Modes

The connector will support two mutually exclusive pipeline modes at runtime.

### Single-Threaded Pipeline

The existing buffered model, retained as-is. 
Backs the in-flight transaction event buffer: holds full transaction events in memory until commit, equivalent to today's `TransactionCache`.

| Backend                         | Notes                                        |
|---------------------------------|----------------------------------------------|
| Heap (memory)                   | Default, equivalent to today's memory buffer |
| Infinispan (embedded or remote) | For existing Infinispan deployments          |
| Ehcache                         | For existing Ehcache deployments             |

### Multi-Threaded Pipeline

The new parallel model introduced by this work. 
`ChronicleQueue` serves as the durable event transport between threads. 
The heap is used only for the lightweight SkipMap (Queue 1 offsets per transaction), which is trivially small and never needs to spill.

| Backend         | Role                               |
|-----------------|------------------------------------|
| Chronicle Queue | Event transport (Queue 1, Queue 2) |
| Heap            | SkipMap storage only               |

**Deployment constraint:** Chronicle Queue requires a local POSIX filesystem.
Network-attached filesystems (NFS, EFS, Azure Files, SAN) are not supported; see [Chronicle Queue Concerns](#concerns) for details.
The connector may be able to validate the filesystem type at startup and fail fast with a clear error if an unsupported filesystem is detected.

## Proposed Architecture

This section describes the fully realized target architecture.
See [Implementation Phases](#implementation-phases) for the incremental delivery path.

### High-Level Flow

![High-Level Design](DDD-36/oracle_multiple_thread_high_level.svg)

Each thread is fully decoupled from the others.
Threads 1 and 3 are purely I/O-bound.
Thread 2 owns all the processing logic.
The two `ChronicleQueue` queue instances serve as durable, disk-backed inter-thread transports, replacing heap or other off-heap solutions.

### Thread 1 - Reader

Thread 1 is a deliberately simple, fast database reader.
Its only job is to execute the LogMiner SQL, iterate the `ResultSet`, and map each row to a structured entry written to Queue 1.
It contains no business logic.

#### Behavior

* Executes LogMiner SQL against `V$LOGMNR_CONTENTS`
* Maps each JDBC row to an entry in Queue 1 (see below)
* Tracks Queue 1's [last written high watermark](#last-written-high-watermark) to avoid duplicate writes on restarts or re-reads
* Returns to querying the next LogMiner batch as fast as possible

#### High-watermark tracking

In the current architecture, Debezium resumes from the `scn` value in the offsets, which is the eldest in-flight transaction that was previously buffered, and rebuffers changes from this point forward.
If a transaction has previously been seen, by comparing its `XID` or `COMMIT_SCN` values to information in the offsets, the transaction is discarded.
If a transaction has not been seen previously, Debezium iterates all events, and dispatches them.
If a transaction was mid-dispatch when the connector restarted, the algorithm uses the current `txId` and `txSeq` to dispatch only the portion of the transaction not sent previously.

In the multi-threaded model, the approach remains similar.
Thread 1 resumes reading from the `scn` value in the offsets.
It records the current queue offset/index where it starts, and reprimes the queue just as the old approach reprimed the transaction buffer.

Tracking per-event position as XStream does would allow the re-prime to be more efficient, appending only rows not previously seen, even for in-flight transactions.
This would use the [last written high watermark](#last-written-high-watermark) to compare an event's position before it is appended to the queue, e.g.:

```java
if (logMinerEventRow.getPosition() > lastWrittenWatermark) {
  writeRowToQueue(logMinerEventRow);
  updateLastWrittenWatermark(logMinerEventRow);
}
```

If achievable, the `lastWrittenWatermark` would be tracked in the offsets and JMX metrics.

### Queue 1 - LogMiner Raw Data

Queue 1 is an immutable, append-only audit log of everything Oracle LogMiner has produced.
It serves two primary roles:

1. **Inter-thread transport**: Thread 2 will read from it to process transactions.
2. **Audit Log**: This can be retained for debugging purposes or the ability to replay events.

#### Entry Schema

Each entry in the queue contains a set of structured uncompressed metadata key/value pairs, followed by the `SQL_REDO` payload.

The following are the uncompressed metadata fields.
These fields are not compressed as they are read by Thread 2 to maximize Chronicle Queue pre-processing pass speeds.

| Field            | Type     | Notes                                                          |
|------------------|----------|----------------------------------------------------------------|
| `XID`            | `String` | Transaction identifier                                         |
| `SCN`            | `String` | System change number                                           |
| `OPERATION_CODE` | `int`    | Oracle's numeric operation code                                |
| `THREAD#`        | `int`    | Assigned redo thread number                                    |
| `TABLE_NAME`     | `String` | The fully qualified `TableId` value, serialized as a `String`  |
| `ROW_ID`         | `String` | The Oracle unique table row identifier assigned to the change  |
| `CSF`            | `int`    | Continuation flag (1=next row merge with this row, 0=last row) |
| `ROLLBACK`       | `int`    | Savepoint/Partial rollback flag                                |
| `RS_ID`          | `String` | Rollback segment identifier                                    |
| `SSN`            | `int`    | SQL sequence number                                            |

The following are the compressed fields.

| Field      | Type     | Notes                                        |
|------------|----------|----------------------------------------------|
| `SQL_REDO` | `String` | The raw Oracle reconstructed SQL, compressed |

#### Compression

The `SQL_REDO` column is the dominant contributor to Queue 1 disk usage.
Oracle's reconstructed SQL is highly repetitive (table names, column names, and so on).
When this is repeated across multiple rows, this compresses quite well.
Compression is applied at the field level, leaving all metadata fields uncompressed.

Queue compression will be made configurable:

```java
public enum SqlRedoCompression {
    NONE, // fastest write/read, highest disk usage
    LZ4,  // default - low CPU overhead, good ratio
    ZSTD, // best ratio, higher CPU and recommended for large transactions
}
```

The `LZ4` compression is recommended and the default, while `ZSTD` is preferred in Oracle environments where long-running transactions risk disk exhaustion.

#### Retention / Cleanup

The Queue 1 entries are retained until both of the following conditions are met:

1. The `scn >= queueEntry.SCN` (Thread 1 has advanced past this entry)
2. The `commit_scn >= queueEntry.SCN` (Thread 3 has emitted the transaction containing the entry)

Cleanup is driven by `StoreFileListener`, which fires when a queue file reaches maximum capacity and rolls.
When the queue rolls to a new active file, non-active files become eligible for deletion.

A map of "roll cycle to maximum SCN" is maintained by Thread 1, and used to evaluate the deletion condition:

```java
private boolean isSafeToDelete(int cycle) {
    final long maxScnInCycle = cycleScnIndex.getMaxScn(cycle);
    return scnWatermark >= maxScnInCycle && commitScnWatermark >= maxScnInCycle;
}
```

### Thread 2 - Processor

Thread 2 is the streaming change event source's `execute()` loop thread and the thread the runtime controls directly.
Thread 2 owns all transaction processing logic.
It is responsible for reading from Queue 1, reconstructing transactions by `XID` (transaction id), applying event consolidation and partial rollback discard rules, and writing the final reconstructed set of events for each transaction into Queue 2.

#### Transaction Index Lookup

Thread 2 is responsible for maintaining a lightweight heap map of in-flight transactions:
```java
Map<String, Long> transactionIdToBeginIndex; // XID to Queue 1 index of BEGIN events
```

This map holds only a `long` index per transaction (8 bytes), regardless of how many events the transaction contains.
The event payloads themselves remain on disk in Queue 1.
This eliminates the heap exhaustion risk that is present in the current implementation.

#### Processing Flow

When Thread 2 observes a `COMMIT` for a given `XID`, it performs two unique passes over the transaction's entries in Queue 1.

**Pass 1: Build thin index**

Thread 2 seeks to the stored `BEGIN` index for the `XID`, reading the queue forward until the `COMMIT`.
During this pass, only uncompressed metadata key/value pairs are used.
This pass builds:

1. A lightweight ordered index of all events in the transaction, used as an iteration guide in Pass 2.
2. The SkipMap: Queue 1 indices of events to discard, such as partial rollbacks and merged LOB fragments.
3. A LOB merge map grouping LOB fragment sequences for assembly.

**Pass 2: Assemble**

Thread 2 iterates the event index and for each retained event, seeks back to its Queue 1 position, decompresses the `SQL_REDO`, and writes the final event to Queue 2.
Any event in the SkipMap is never decompressed.

For LOB merge events, Pass 2 seeks to each fragment's `queueIndex`, decompresses and merges the fragment values.
This simulates the logic that currently exists inside the `TransactionCommitConsumer` implementation.
Once all groupings are merged, the consolidated event is written to Queue 2.

**Discard Logic - Partial Rollbacks**

LogMiner emits events with `ROLLBACK=1` flag.
This most often is when a savepoint is rolled back within a transaction, but can represent other scenarios like constraint violations, where Oracle records the original insert, and then reverts it due to the constraint.
These events cancel earlier events that often have the same `ROW_ID`.
Both the original and the rollback event are added to the SkipMap, and are excluded from Queue 2, as well as the decompression of their respective `SQL_REDO` column.

**Rollback Handling**

When Thread 2 observes a `ROLLBACK` for a transaction, the entire transaction is discarded.
In this case, the transaction's entry in the transaction index is removed and nothing is written to Queue 2.

### Queue 2 - Processed Events

Queue 2 contains the final, clean set of events to be emitted to the target system.
It is written exclusively by Thread 2 and read exclusively by Thread 3.

Each transaction in Queue 2 begins with a `BEGIN` event, followed by its events and terminated by a `COMMIT`.
The `COMMIT` marker carries the `commit_scn` along with an event count.
Thread 3 waits for this `COMMIT` marker before emitting the transaction.

#### Entry Schema

Queue 2 entries are fully processed, structured events with parsed column values and no raw SQL, unless the user has configured to include the raw SQL.
If the SQL is to be included, it will be compressed.
The exact schema to be used here generally will align to contain all needed fields to populate the `source` information block and the `before` and `after` portions of a Debezium event.

### Thread 3 - Emitter

Thread 3 is purely an I/O-bound producer thread.
It reads all processed events from Queue 2 and emits them as `SourceRecord` instances.
All events are emitted from Queue 2 in transaction commit chronological order.

The `commit_scn` tracks the highest commit SCN emitted for a given Oracle redo thread.
Thread 3 is responsible for maintaining this offset state.

There are several benefits to isolating event dispatch in a dedicated thread:

* In the current model, a large transaction's `COMMIT` causes a burst of events directly into the `ChangeEventQueue` on the same thread doing all processing.
In the new model, Thread 3 drains Queue 2 at its own pace, so large-transaction bursts absorb into Queue 2's disk-backed buffer rather than exhausting the `ChangeEventQueue`'s bounded in-memory capacity.
* Post-processor latency (e.g. a `ReselectPostProcessor` re-selecting LOB columns) no longer stalls Thread 2.
Thread 3 owns all dispatch, so slow post-processors create back-pressure only against Queue 2, leaving Thread 1 and Thread 2 free to continue mining and processing.

> [!NOTE]
> It is possible that Thread 3 could be eliminated by relying on a custom pluggable dequeue, see [DBZ-8968](https://github.com/debezium/debezium/pull/6468).
> This would allow Thread 2 to enqueue directly without concerns of back-pressure if the pluggable dequeue had reasonable size limits for a system's largest transactions.

## Implementation Phases

The multi-threaded pipeline is delivered incrementally. 
Each phase leaves the connector in a fully working state. 
The single-threaded pipeline remains operational throughout: phases apply only to the multi-threaded mode.

### Phase 0: Current Architecture (Baseline)

A single thread owns the entire pipeline: manages the LogMiner JDBC session and SCN window calculation, iterates the result set materializing `LogMinerEventRow` instances, buffers events into a per-transaction in-memory cache, and on `COMMIT` replays the buffer, filters events, and emits to the `EventDispatcher`. On `ROLLBACK`, the buffer is discarded.

### Phase 1: Introduce `LogMinerClient`, Segregate LogMiner Reading

**Goal:** Separate LogMiner result set iteration from event processing without changing any processing logic.

`LogMinerClient` is introduced as the owner of all LogMiner JDBC interaction and started as Thread 1, a long-lived reader thread.
Thread 1 places materialized `LogMinerEventRow` instances onto a bounded `LinkedBlockingQueue`.
The main thread drains the queue and passes each row to the existing event processing logic, which is completely unchanged.
On each poll timeout, the main thread checks for a fatal error cached by Thread 1 and rethrows if present.

**Exit contract:** A working connector where LogMiner reading and event processing run on separate threads, communicating via `LinkedBlockingQueue`.

### Phase 2: Introduce Queue 1 as Transport

**Goal:** Replace the `LinkedBlockingQueue` with Chronicle Queue as the durable inter-thread transport.

Thread 1 writes serialized `LogMinerEventRow` instances to Queue 1 instead of the `LinkedBlockingQueue`.
The main thread tails Queue 1 via an `ExcerptTailer`.
The `LinkedBlockingQueue` is retired.
Back-pressure is now disk-space bounded rather than capacity bounded.

**Exit contract:** A working connector where Queue 1 is established as the durable transport between Thread 1 and the main thread.

### Phase 3: Thread 2 Owns the Tailer, SkipMap Replaces Transaction Buffer

**Goal:** Introduce the new processing model, per-transaction Queue 1 replay with a SkipMap, in a single-threaded context before adding any parallelism.

The main thread becomes Thread 2, the streaming change event source's `execute()` loop thread.
As the thread the runtime controls directly, any fatal error Thread 2 rethrows propagates through `execute()` into the existing `errorHandler`.
Thread 2 owns the Queue 1 coordinator tailer and continues to check Thread 1 for fatal errors on each tailer idle moment.
On `BEGIN`, Thread 2 records the Queue 1 index for the transaction.
On `COMMIT`, Thread 2 performs two passes over Queue 1: first building a `LogMinerTransactionFilter` (SkipMap), a set of Queue 1 offsets representing events to discard, then iterating the transaction a second time, skipping discarded offsets and emitting the remainder directly to the `EventDispatcher`.
On `ROLLBACK`, the transaction's Queue 1 index is discarded with no further action.
The in-memory transaction buffer (`TransactionCache`) is retired.

**Key behavioral difference from today:** The connector no longer buffers entire transaction events in memory. 
Only a lightweight SkipMap is held per transaction. Full event data lives in Queue 1 on disk.

**Exit contract:** A working connector using the new SkipMap-based processing model, single-threaded on the processing side, with no in-memory event buffer.

### Phase 4: Introduce Queue 2 and Thread 3

**Goal:** Decouple event emission from transaction processing by introducing Queue 2 as a buffer between Thread 2 and the `EventDispatcher`.

Thread 2 no longer emits directly to the `EventDispatcher`.
After applying the SkipMap, Thread 2 writes emittable events to Queue 2.
Thread 3 is introduced as a dedicated thread that tails Queue 2 and owns all emission to the `EventDispatcher`.
Events are emitted strictly in the order they appear in Queue 2; no reordering logic is required.
Thread 3 caches fatal errors in its own `AtomicReference`.
Thread 2 now monitors both Thread 1 and Thread 3 for fatal errors on each Queue 1 tailer idle moment or Queue 2 write, rethrowing any non-null error into the existing `errorHandler`.

**Exit contract:** A working connector with the full three-thread pipeline established: Thread 1 → Queue 1 → Thread 2 → Queue 2 → Thread 3 → `EventDispatcher`.

### Phase 5: Thread 2 Fan-Out (Parallel Transaction Preprocessing)

**Goal:** Parallelize transaction preprocessing within Thread 2 using a fork/join model while preserving commit chronological order for Queue 2 writes.

The Thread 2 coordinator continues handling `BEGIN`/`COMMIT`/`ROLLBACK` detection.
On `COMMIT`, the coordinator submits a replay task to a preprocessor thread pool of N workers.
Each worker independently walks Queue 1 from the recorded `BEGIN` index using its own isolated per-transaction reader and builds the SkipMap.
A sequencer sits between the preprocessor pool and the enqueue pool: workers deposit completed SkipMap results, and the sequencer drains in commit order, submitting enqueue tasks only when all prior committed transaction results have completed.
The enqueue pool is a single thread that applies the SkipMap and writes emittable events to Queue 2 in commit order.

**Invariants preserved:**
- Queue 1 is append-only from Thread 1; all other threads only read it.
- Queue 2 is always written in commit order; Thread 3 requires no reordering logic.
- Memory pressure remains bounded; only lightweight SkipMap objects are held in the sequencer's pending map.

**Exit contract:** The full parallel architecture is realized.
Multiple transactions are preprocessed concurrently, but Queue 2 always receives events in commit order.

### Architecture Summary

| Phase     | T1               | Transport             | T2                                                         | Transport | T3                |
|-----------|------------------|-----------------------|------------------------------------------------------------|-----------|-------------------|
| 0 (today) | —                | —                     | Single thread (all)                                        | —         | —                 |
| 1         | `LogMinerClient` | `LinkedBlockingQueue` | Main thread (all processing)                               | —         | —                 |
| 2         | `LogMinerClient` | Queue 1               | Main thread (all processing)                               | —         | —                 |
| 3         | `LogMinerClient` | Queue 1               | Coordinator + SkipMap (single-threaded)                    | direct    | `EventDispatcher` |
| 4         | `LogMinerClient` | Queue 1               | Coordinator + SkipMap                                      | Queue 2   | Emitter           |
| 5         | `LogMinerClient` | Queue 1               | Coordinator + preprocessor pool + sequencer + enqueue pool | Queue 2   | Emitter           |

## Chronicle Queue

> [!IMPORTANT]
> While this document explicitly discusses `ChronicleQueue` as a first-class citizen in the design, it should be treated as an implementation detail.
> The solution should be extendable, supporting other potential similar solutions that could be leveraged where memory-mapped files aren't possible.

### Configuration

Both Queue 1 and 2 are constructed using `SingleChronicleQueueBuilder`:
```java
SingleChronicleQueueBuilder
        .binary(queueDirectory)
        .rollCycle(RollCycles.HOURLY)
        .blockSize(64 << 20)
        .storeFileListener(storeFileListener)
        .build();
```

* **One entry per row**: The `writeDocument` API must be called once per `ResultSet` row, not once per batch. 
Wrapping multiple `ResultSet` rows in a single `writeDocument` will exhaust the mapped chunk boundary, and throw a `DecoratedBufferOverflowException`.
* **Roll Cycle**: hourly rolling with aggressive cleanup keeps disk usage bounded, subject to long-running transactions.
* **Block Size**: A 64MB block size by default. This should be tunable based on observed average entry size.

### Concerns

#### Requires memory-mapped filesystem support

`ChronicleQueue` uses memory-mapped files, and these are not possible or reliable across every filesystem. 
As outlined in [this section](https://github.com/OpenHFT/Chronicle-Queue/?rgh-link-date=2026-04-10T12%3A14%3A21Z#usage), this prevents the use of things like NFS, AFS, SAN-based, or other storages that use network-based file systems.

#### JVM argument requirements

`ChronicleQueue` does come with its own set of concerns, most notably the requirement to set the following JVM arguments due to how the library uses various Sun-specific APIs:

```text
--add-exports=java.base/jdk.internal.ref=ALL-UNNAMED
--add-exports=java.base/sun.nio.ch=ALL-UNNAMED 
--add-exports=jdk.unsupported/sun.misc=ALL-UNNAMED 
--add-exports=jdk.compiler/com.sun.tools.javac.file=ALL-UNNAMED 
--add-opens=jdk.compiler/com.sun.tools.javac=ALL-UNNAMED 
--add-opens=java.base/java.lang=ALL-UNNAMED 
--add-opens=java.base/java.lang.reflect=ALL-UNNAMED 
--add-opens=java.base/java.io=ALL-UNNAMED 
--add-opens=java.base/java.util=ALL-UNNAMED
```

The library authors are working on newer releases that will eliminate these requirements without any loss of performance.

## Scalability

### Multiple Kafka Connect Tasks?

By utilizing multiple threads to handle independent stages during streaming with inter-thread concurrent transport, a given stage could be promoted to a Kafka Connect task instead.
By using multiple tasks, this would allow the connector to take advantage of a Kafka Connect cluster's full processing power, similar to how Google Spanner works.

### Fan-out per Pluggable Database

Given that Oracle transactions cannot span across a pluggable database boundary, there is the possibility to use multiple threads/tasks to fan out the workload per PDB.
In this setup, rather than using a single pipeline instance (Thread 1, Thread 2, Thread 3, Queue 1, Queue 2) to read all changes for all pluggable databases, the connector could fan out to N instances (where N is the number of captured PDBs).

While a fan-out of N readers increases the load on Oracle, it ensures that high activity in one PDB does not cause latency when capturing changes for another.

### Parallelizing LogMiner Reads

With the new log-count/size based mining algorithm, this introduces the potential to scale the reader thread using a fan-out worker strategy during bursts.
There are some concerns with long-running transactions that need to be dealt with, but this architecture lends itself to scaling independent isolated portions of the pipeline.

## Open Items

* **Filesystem validation at startup:** The connector should ideally detect unsupported Chronicle Queue filesystems (NFS, EFS, Azure Files, SAN) at startup and fail fast with a clear error.
It is not yet confirmed whether reliable filesystem-type detection is feasible across all target environments, or whether we must rely on Chronicle Queue failing at first write.

* **Last written high watermark feasibility:** The Thread 1 high-watermark tracking section describes an aspirational per-event position mechanism.
Whether a stable, unique per-event position can be derived from the available LogMiner fields (CSCN, SCN, XID, SSN, RBA; see [Appendix](#last-written-high-watermark)) needs to be confirmed before this mechanism can be relied upon for restart deduplication.

* **Queue 2 entry schema:** The Queue 2 entry schema is not yet fully defined.
It is described in general terms as containing the fields needed to populate the Debezium `source` block and the `before`/`after` event payloads.
The exact field set and serialization format need to be specified.

* **Phase 5 preprocessor pool sizing:** The Phase 5 preprocessor thread pool has N workers, but the default value of N, its configuration mechanism, and how it interacts with long-running transactions that could starve the sequencer are not yet defined.

* **Graceful shutdown sequence:** The document addresses error propagation but not orderly shutdown.
When the connector is stopped or rebalanced, the three-thread pipeline needs a defined drain-and-close sequence: Thread 1 stops mining, Thread 2 finishes in-flight transaction processing, Thread 3 completes pending Queue 2 emission, and offsets are flushed before control is returned to the runtime.

* **Metrics and JMX exposure:** New metrics for the multi-threaded pipeline are not yet defined.
Candidates include: Queue 1 write lag, Queue 2 write lag, preprocessor pool queue depth, SkipMap size per in-flight transaction, and per-phase throughput counters.

## Appendix

### Last Written High Watermark

Oracle's system change number (SCN) is not unique, and can be assigned to two or more changes.
In order to create a high-watermark, one must use a concatenation of multiple values that include things like the commit SCN (CSCN), event SCN (SCN), transaction id (XID), sequence within the transaction (SSN), and the relative byte access (RBA) in the transaction logs.

### SkipMap

The SkipMap is a set of Queue 1 indices representing events within a committed transaction that should be discarded rather than emitted.
It is built during Thread 2's Pass 1 (the metadata-only scan of a transaction in Queue 1) and consumed during Pass 2 (the full decompression and assembly pass).

The SkipMap replaces the discard logic that the current single-threaded model applies against the in-memory transaction buffer at commit time.
In the multi-threaded model, events remain on disk in Queue 1; the SkipMap records only the Queue 1 indices of events to skip, keeping heap usage trivially small regardless of transaction size.

Events are added to the SkipMap when:

* A `ROLLBACK=1` flag is observed (partial rollback or savepoint), which cancels both the rollback event and the earlier event it targets (matched by `ROW_ID`).
* LOB fragment continuation rows are merged into a parent event and should not be emitted independently.

### Commit Chronological Order

Oracle assigns a system change number (SCN) to each committed transaction at the point of commit.
Multiple transactions can share the same commit SCN, particularly in multi-threaded redo environments where transactions on different redo threads commit concurrently.

For this reason, the design does not rely on commit SCN alone as a total ordering key.
Instead, events are ordered by commit chronological order: the sequence in which transactions are committed as observed in the LogMiner output.
This is the ordering the Phase 5 sequencer enforces when draining SkipMap results to the enqueue pool, and the ordering Thread 3 relies on when emitting events from Queue 2.

Maintaining commit chronological order guarantees that events are emitted in the same relative sequence as the original database commits, which is required for correct downstream consumption regardless of how many transactions share a given commit SCN.
