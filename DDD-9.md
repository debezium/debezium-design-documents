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

### Offsets

Currently, the Oracle connector maintains two critical values in the offsets, `scn` and `commit_scn`, shown here:

```json
{
    "scn": "123456",
    "commit_scn": "123456:1:txId1-txId2,123457:2:txId4"
}
```

Moving forward, the connector needs to maintain the resume SCN and each committed transaction id since the resume SCN and its commit SCN.
In addition, need to maintain the last processed transaction id and event sequence.
I would also prefer to move away from _encoded_ values to the more natural nested `Struct`s style.

I'm not sure how hard this makes editing the offsets for users, but this what I have in mind:

```json
{
  "resume_scn": "123456",
  "txId": "123456", // current transaction being processed
  "txSeq": 25, // last event in current transaction received
  "committed_transactions": [{
    "id": "a748391bc3123",
    "thread": 1,
    "scn": "123478"
  }]
}
```

### LogMiner query

The LogMiner query must be adjusted to include a new predicate condition to exclude transactions in `committed_transactions`.
The query change would include something like the following:

```sql
AND UPPER(RAWTOHEX(XID)) NOT IN ( <committed-transaction.id(s) in upper-case> )
```

The construction of the in-clause should use the same logic for the `table.include.list` to wrap the predicate in a disjunction is the list of values exceeds 1000 values, since Oracle does not permit creating in-clauses with more than 1000 elements.

In addition, the `RESUME_SCN` column should be included, if it isn't already.

### Processor changes

The processor logic needs to be changed to move away from using "caches", and instead will simply parse the `LogMinerEventRow` details, and dispatch.
During the dispatch, the processor will likely need to be merged with the `TransactionCommitConsumer` to handle merging of CLOB, BLOB, XML, and Extended String operations on-the-fly.

In addition, the returned `startScn` will no longer be based on the last read SCN, but instead will be based on the last non-null `getResumeScn()` value.
As a value is present in the `getResumeScn()` event field, the offset's `resume_scn` should be updated.

The `ResultSet` itself will always contain changes to transactions we've not yet seen and processed or a transaction that was mid-dispatch when the connector was stopped.
If the transaction id matches the offset's `txId`, then the processor will ignore any event with a sequence less-than or equal-to the offset's `txSeq`, to avoid redispatching events already sent to Kafka.
When a transaction commits, it is added to the offset's `committed_transactions` and the `txId` and `txSeq` are cleared.

### Schema changes

TBD

## Concerns / Gaps

* LogMiner will reorder all changes in chronological commit order, meaning SCN values will increment/decrement from row-to-row, while the commits will always be in chronological order, always equal-to or greater.
* DDL changes need to be reviewed to make sure they do not introduce any unexpected/unintended consequences of dispatch/processing order.
* Large transactions will be re-read across mining steps; will this create any disk IO performance concerns on the database?
* Will this be a problem for any of the three current mining strategies using committed data?
* What about partial rollbacks, do we still need to handle these?
* Do we implement this as a new adapter, i.e. `logminer_slim`, or change `logminer` proper?
* Is the change in offsets being non-backward compatible a blocker or concern?

## Risks

* Overall minimal, most critical code paths are reused as-is, i.e. log gathering.
* Processing logic will need to change from relying on caches to reading data in-flight from ResultSet only.
* Offset low/high watermark strategies completely change.