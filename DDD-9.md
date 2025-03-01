# DDD-9: A buffer-less implementation of Oracle LogMiner

## Motivation

While most Debezium Oracle users rely on the heap-based implementation where the transaction data is cached in the JVM's heap, this puts a substantially higher burden of resources on the Kafka Connect cluster.
In addition, users that maintain the connector and Kafka Connect environments may not have any knowledge of what the transaction workload is like, which makes scaling the JVM heap difficult.

For users who do use the off-heap implementations of Infinispan and Ehcache, these often come at the cost of performance.
Each of these off-heap caches implement some level of disk access, which will naturally be orders of magnitude slower than reading/writing directly to JVM heap.

Lastly, one of the more technical critiques is everyone believes the Oracle connector's code is significantly complex.
If we eliminate the need to buffer transactions entirely, that significantly reduces the complexity and integration with cache providers, simplifies the configuration, and slims the documentation so that perhaps it's easier to digest with less moving parts. 

## Goals

* Reduce code complexity, eliminating the need on-heap and off-heap cache integrations.
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

## Concerns / Gaps

* LogMiner will reorder all changes in chronological commit order, meaning SCN values will increment/decrement from row-to-row, while the commits will always be in chronological, always equal-to or greater than order.
* DDL changes need to be reviewed to make sure they do not introduce any unexpected/unintended consequences of dispatch/processing order.
* Large transactions will be re-read across mining steps; will this create any performance concerns?
* Will this be a problem for any of the three current mining strategies using committed data?
* What about partial rollbacks, do we still need to handle these?

## Risks

* Overall minimal, most critical code paths are reused as-is, i.e. log gathering.
* Processing logic will need to change from relying on caches to reading data in-flight from ResultSet only.
* Offset low/high watermark strategies completely change.
