# DDD-9: A buffer-less implementation of Oracle LogMiner

## Motivation

While most Debezium Oracle users rely on the heap-based implementation where the transaction data is cached in the JVM's heap, this puts a substantially higher burden of resources on the Kafka Connect cluster.
In addition, users that maintain the connector and Kafka Connect environments may not have any knowledge of what the transaction workload is like, which makes scaling the JVM heap difficult.

For users who do use the off-heap implementations of Infinispan and Ehcache, these often come at the cost of performance.
Each of these off-heap caches implement some level of disk access, which will naturally be orders of magnitude slower than reading/writing directly to JVM heap.

Lastly, one of the more technical critiques is everyone believes the Oracle connector's code is significantly complex.
If we eliminate the need to buffer transactions entirely, that significantly reduces the complexity and integration with cache providers, simplifies the configuration, and slims the documentation so that perhaps it's easier to digest with less moving parts. 

## Goals

* Reduce code complexity, eliminating the need for on-heap and off-heap cache integrations.
* Reduce connector memory footprint
* Avoid issues with split transactions that yield _unsupported_ operations.
* Provides an easy way to manage transaction-level sequences for restarts.
* Remove the need for `rac.nodes`.
* Remove the need for a flush table and flush strategies.

## Additional benefits

* Data set would be provided in committed, chronological order.
* Implementation can use simple read, parse, dispatch pattern similar to other connectors.
* Read strategy aligns closely with the current one used when `lob.enabled` is set to `true`.

## Requirements

* LogMiner session must be started to return only committed data.
* Provide a deterministic way to identify the low watermark while consuming data in committed only mode.
* Provide a deterministic way to identify transactions that have already been handled.

## Proposed changes

### Adapter vs Buffer Type

This new implementation could be introduced in one of two ways:

- New `connection.adapter` called `logminer_slim` or similar
- New `log.mining.buffer.type` called `none`

We should evaluate the duplicity involved using an adapter vs an unbuffered mode.

### Offsets

The Oracle LogMiner buffered implementation maintains offsets, shown here: 

```json
{
    "scn": "123456",
    "commit_scn": "123456:1:txId1-txId2,123457:2:txId4"
}
```

The unbuffered implementation will continue to use the same offset fields, `scn` and `commit_scn`.
The `scn` field will continue to represent the connector's low watermark.
The `commit_scn` field will continue to represent the connector's high commit watermarks per redo thread.

There will be two new fields, `txId`, and `txSeq` added to the offsets, shown here:

```json
{
  "scn": "123456",
  "commit_scn": "123456:1:txId1-txId2,123457:2:txId4",
  "txId": "123456", // current transaction being processed
  "txSeq": 25 // last event in current transaction received
}
```

#### Low watermark (scn)

We had originally hoped to use the `SAFE_RESUME_SCN` field in `V$LOGMNR_CONTENTS` to drive the value for the `scn` offset value.
Unfortunately, the `SAFE_RESUME_SCN` field is unreliable for this purpose, so we won't be able to use the field to drive this value.

The advance the offset's `scn` low watermark, the connector will use an interval-based concept very similar to heartbeats.
When the interval has expired, a separate LogMiner session will be opened, and all `START`, `COMMIT`, `ROLLBACK`, and `REPLICATION_MARKER` events will be gathered in chronological order without using `COMMITTED_DATA_ONLY`.
All `START` events will be paired with its corresponding `COMMIT`, `ROLLBACK`, or `REPLICATION_MARKER` event, and discarded.
The remaining events, which should all be `START` events, will be examined and the minimum scn will be computed from this data set, representing the new `scn` low watermark.

Unfortunately due to the reliability of `SAFE_RESUME_SCN`, there are no fields in the `COMMITTED_DATA_ONLY` output can can reliably be used to drive this low watermark.

#### High watermark (commit_scn)

There will be no changes in the `commit_scn` attribute in the offsets. 
It will continue to be driven by the most recently processed `COMMIT` events for a given redo thread.


#### Transaction identifier and sequence (txId and txSeq)

The `txId` represents the current transaction actively being emitted while `txSeq` represents the event's sequence.

These two fields are actively used when the connector is restarted when offsets exist.
The `txId` and `txSeq` attributes are used to force the connector into a filtration mode, where all events read from `scn` are discarded until the connector observes a row where the `XID` value matches `trxId` and the `SEQUENCE#` value matches the `trxSeq`.
When this match occurs, the connector exits the filtration mode and begins processing changes.

This allows the connector to safely be stopped mid-dispatch and resumed without loosing data.

### LogMiner changes

One of the first major changes will involve the low watermark, the `scn` from the offsets.
As described above, the low watermark is not advanced as part of the data iteration in the `COMMITTED_DATA_ONLY` phase, but will use a separate interval-based gathering step.
This is the only reliably way to identify when we've consumed all in-flight transactions, as there is no reliable column in the `COMMITTED_ONLY_DATA` data set to drive this information.

On each log switch, redo and archive logs will be added based on the offset `scn` value.
This mimics the same behavior that users experience when setting the `lob.enabled` configuration option to `true`.
This has an advantage that when using the `redo_log_catalog`, we can examine all the logs in that session and defer the dictionary dump if one already exists in the log range.
This will help reduce the frequency of archive logs and will help avoid the feedback loop when multiple connectors are deployed to the same database using this mining strategy.

The query used to fetch data from the LogMiner performance view must be changed.

In the buffered implementation, the query used `SCN > ? AND SCN <= ?` to control the range of data that would be pulled from the transaction logs.
This required that the connector compute an upper boundary based on some logic that is often hard to explain and difficult for users to compute based on `log.mining.batch.size.*`.

In the unbuffered implementation, the query will use `COMMIT_SCN > ?` instead and the bind value will be computed by examining the `commit_scn` value in the offsets.
If a `commit_scn` value exist, the minimum SCN across all redo threads will be used, otherwise use the offset's `scn` as a fallback.
After each mining iteration, the offsets will be reexamined as they're mutated during data processing.
If no data is fetched in a prior iteration, the same predicate bind value will be used in the next iteration until data is available.

In addition, to facilitate the necessary state tracking, the following columns must be added to the LogMiner query:

* START_SCN
* START_TIMESTAMP
* COMMIT_SCN
* COMMIT_TIMESTAMP

These fields will be used to populate the various source info block attributes of the same semantic names.

### Processor changes

There is some behavior that ultimately will be shared between the unbuffered implementation and the current buffered implementations.
We should consider moving the current LogMiner implementation under a subpackage called `buffered` and adding a new package called `unbuffered`.
This helps identify a clear distinction between the two implementations and which does exactly what.

To aid in sharing behavior between the buffered and unbuffered implementations, several abstract classes should be introduced in `io.debezium.connector.oracle.logminer` package.
For example, one for `StreamingAdapter` and another for `StreamingChangeEventSource`.
We could also think about introducing one to define common handlers and logic for processing changes.

The processing of the initial result set will be mostly identical between the buffered and unbuffered implementations.
Both will rely on the `LogMinerEventRow` to wrap the JDBC row accordingly, as it's constructed from the raw JDBC `ResultSet`.

The buffered solution uses a separate class, `TransactionCommitConsumer`, to provide delayed dispatch of the most recent event to facilitate event consolidation when LOB, XML, and Extended String event types are observed.
We will need to find a way to incorporate this into the unbuffered solution.

#### Start events

With the unbuffered solution dispatching almost instantly, all state can generally be derived from the current event or a prior event in the transaction.
Given that we will be consuming all `START` events, we can use these events to identify whether the transaction is skipped based on:

* Username inclusion/exclusions
* ClientId inclusion/exclusions
* Transaction has previously been handled, comparing with offset's `commit_scn`

When a transaction is marked as _skipped_, the processing should enter a filter mode state, where all future events are discarded until we reach the first `COMMIT`.
This provides an efficient way to quickly iterate and discard data that is of no importance without expensive branching that can cost valuable cycles.

#### Commit events

In the buffered solution, we used the `COMMIT` event as the location where we dispatched all DML events for the transaction.
But given that we are operating with an in-flight dispatch mode with the unbuffered implementation, the commit handler is significantly more lightweight.

In this case, it's important for the handler to record the `COMMIT` event's transaction identifier in the list of transactions for the redo thread's `commit_scn` in the offsets.
This makes sure that when we look at whether we've processed a transaction before, it would be viewed as previously processed.
In addition, this should dispatch the transaction committed event.

#### Rollback events

The unbuffered implementation should never see any `ROLLBACK` events, as they are filtered out automatically by LogMiner.
So this handler should theoretically raise an exception in case its ever called mistakenly. 

#### Schema change events

All schema changes, aka `DDL` events, are wrapped by a corresponding `START` and `COMMIT` event marker.
This handler will work very similar to the buffered implementation, dispatching the DDL in-flight when consumed.

#### Data change events

This handler will work similar to its buffered implementation, in that most of the initial checks around Status 2 and Hybrid should be reused.
The major difference here is what offset information should/will be set per DML event.
We should/need to set the following:

* EventScn
* TransactionId (txId from `XID`)
* Transaction Sequence (txSeq from `SEQUENCE#`)
* SourceTime
* TableId
* RsId
* RowId
* RedoSql (if enabled)

#### Replication markers

Given that we are not buffering transactions in a cache, all `REPLICATION_MARKER` events can be ignored.

#### Unsupported events

Ideally these should no longer occur, but information should be logged if one is observed.

#### LOB/XML/Extended String events

TBD

### Commit consumer

The `TransactionCommitConsumer` is an integral part of the transaction processing logic in the Oracle connector.
There are certain event types for CLOB, BLOB, XML, and Extended String field types that require we merge several events from the result set into one logical change.
This is because Oracle will represent such logical changes as multiple event operations in the redo logs.

In the buffered implementation, the consumer was constructed inline at commit time, fed the list of events for the transaction, and closed.
This provided a single place where we could buffer the last added event to resolve whether the next event should be merged with it.
But in the unbuffered solution, we will need to revisit how the consumer is constructed, as its lifecycle needs to exist across the boundaries of multiple event handlers now.
The most logical idea would be to create this consumer during the `START` (begin transaction) event and then close it at `COMMIT` (end transaction).

However, given that both buffered and unbuffered require this behavior, it may make sense to perform some refactoring here around this class.
For example, we may want to bring the logic into a base processor implementation.

## Concerns / Gaps

* Given that `SAFE_RESUME_SCN` is not reliable and the ALL_ROWS mining pass is used to compute the low watermark, this will introduce periodic latency.
* Given that the low watermark is updated on an interval-basis, there will be higher disk IO. Users should be able to customize how often the low watermark is recomputed.
* DDL changes need to be reviewed to make sure they do not introduce any unexpected/unintended consequences of dispatch/processing order.
* Will this be a problem for any of the three current mining strategies using committed data?
* What about partial rollbacks, do we still need to handle these?

## Risks

* Overall minimal, most critical code paths are reused as-is, i.e. log gathering.
* Processing logic will need to change from relying on caches to reading data in-flight from ResultSet only.
* Offset low/high watermark strategies completely change.

## Future work (out of scope)

* Implement the `txId` and `txSeq` behavior in the buffered adapter. 
* Reuse the reduction of dictionary build steps when enabling the `redo_log_catalog` mining strategy.
  This has the benefit that users who wish to deploy multiple Oracle connectors are not forced into `online_catalog` or `hybrid` strategies.
* Reworking `log.mining.batch.size.*` to work more akin to `max.iteration.transactions` for the buffered implementation.
* Move to an unbounded mining window, only calculating the lower boundary and not the upper boundary (let session logs define that)
