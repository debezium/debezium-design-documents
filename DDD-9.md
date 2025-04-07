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
* The LogMiner resume SCN is used to control the low watermark in the offsets.
* Transactions committed after the resume SCN must have its id and SCN maintained in the offsets.
* LogMiner query should exclude any transaction already committed since the resume SCN.

## Proposed changes

### Adapter vs Buffer Type

This new implementation could be introduced in one of two ways:

- New `connection.adapter` called `logminer_slim` or similar
- New `log.mining.buffer.type` called `none`

We should evaluate the duplicity involved using an adapter vs an unbuffered mode.

### Offsets

Currently, the Oracle connector maintains two critical values in the offsets, `scn` and `commit_scn`, shown here:

```json
{
    "scn": "123456",
    "commit_scn": "123456:1:txId1-txId2,123457:2:txId4"
}
```

Moving forward, the connector will store the LogMiner resume SCN in the `scn` field, while continuing to maintain the same commit detail in `commit_scn`.
In addition, the offsets will need to maintain two additional pieces of information, the last processed transaction identifier and event sequence.

The new updated offsets structure would look like this:

```json
{
  "resume_scn": "123456",
  "commit_scn": "123456:1:txId1-txId2,123457:2:txId4",
  "txId": "123456", // current transaction being processed
  "txSeq": 25 // last event in current transaction received
}
```

By maintaining a similar structure while adding the two fields, LogMiner users can switch between the buffered and unbuffered modes once we add support to the buffered mode for tracking the `trxId` and `txSeq` attributes.

### LogMiner query

The LogMiner query must be adjusted to include a new predicate condition to exclude transactions in `committed_transactions`.
The query change would include something like the following:

```sql
AND UPPER(RAWTOHEX(XID)) NOT IN ( <committed-transaction.id(s) in upper-case> )
```

The construction of the in-clause should use the same logic for the `table.include.list` to wrap the predicate in a disjunction if the list of values exceeds 1000 values, since Oracle does not permit creating in-clauses with more than 1000 elements.

In addition, the following columns need to be added to the query:

* SAFE_RESUME_SCN
* START_SCN
* START_TIMESTAMP
* COMMIT_SCN
* COMMIT_TIMESTAMP

### Processor changes

There is a lot of behavior that the unbuffered processor will require that is identical to the buffered processor implementations.
Architecturally, we should consider introducing a separation of packages under `processor` where we have two subpackages: `buffered` and `unbuffered`.
This makes a clear distinction between what uses caches/buffers and what does not.
It also helps provide a package structure to define abstract classes and interfaces for shared behavior between them.

For the unbuffered processor, events are to be read from the JDBC result set into a `LogMinerEventRow`, which is identical to the `buffered` style.
During event handler function calls, the row object is converted into a `LogMinerEvent`.
However, since the unbuffered solution is not working with caches, the handler functions will focus on dispatching the `LogMinerEvent` to the `TransactionCommitConsumer` in-flight.

When the `LogMinerProcessor#processResults` returns, it should return an `Scn` value based on the last safe resume scn read from the JDBC result set.
This field should always be populated with a value for each row in the result set, and is value is managed by Oracle LogMiner directly.
As mentioned above, this value will be stored in the offsets `scn` field and will drive the next mining iteration's read position.

In practice, the JDBC result set should only contain committed transactions that we have not yet seen and processed, or transactions that were mid-dispatch when the connector was previously stopped.
When the connector is stopped mid-dispatch of a transaction and is resumed, the offset `scn` value will continue to refer to the start of that transaction until the transaction's commit is observed.
During the restart, all events for the mid-dispatched transaction will be reprocessed.
In this case, we need a mechanism that will advance the connector forward without redispatching events until we reach the event sequence that matches `txSeq`, the last event to be sent to Kafka.
Once this position is found, then it's safe to continue processing events like normal.

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

### Schema changes

TBD

## Concerns / Gaps

* LogMiner will reorder all changes in chronological commit order, meaning SCN values will increment/decrement from row-to-row, while the commits will always be in chronological order, always equal-to or greater.
* DDL changes need to be reviewed to make sure they do not introduce any unexpected/unintended consequences of dispatch/processing order.
* Large transactions will be re-read across mining steps; will this create any disk IO performance concerns on the database?
* Will this be a problem for any of the three current mining strategies using committed data?
* What about partial rollbacks, do we still need to handle these?

## Risks

* Overall minimal, most critical code paths are reused as-is, i.e. log gathering.
* Processing logic will need to change from relying on caches to reading data in-flight from ResultSet only.
* Offset low/high watermark strategies completely change.