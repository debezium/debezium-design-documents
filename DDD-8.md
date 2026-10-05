# DDD-8: Read-only incremental snapshots for other relational connectors

## Motivation
[Incremental snapshot](https://debezium.io/blog/2021/10/07/incremental-snapshots/) was a major improvement introduced in Debezium 1.6 to solve three key issues with Debezium's handling of captured tables.

First, there was the near-impossibility of adding additional tables to the captured list if existing data needed to be streamed. 
Second, the long-running snapshot process could not be terminated or resumed, requiring a complete restart if interrupted. 
Third, change data streaming was blocked until the snapshot was completed. 
The incremental snapshot addresses these problems by enabling more flexible and efficient data capture.

The incremental snapshot was implemented based on the paper [DBLog: A Watermark Based Change-Data-Capture Framework](https://arxiv.org/pdf/2010.12597v1) by Andreas Andreakis and Ioannis Papapanagiotou.
The main idea behind this approach is that change data streaming is executed continuously together with snapshotting. 
The framework inserts low and high watermarks into the transaction log (by writing to the source database), and between those two points, a part of the snapshotted table is read. 
The framework keeps a record of database changes between the watermarks and reconciles them with the snapshotted values if the same records are snapshotted and modified during the window.

This means that Debezium must have write permission on a specific table used during the snapshot to create the low/high watermarks.

This can be a limitation for some users with different motivations:

* Capturing changes from read-replicas
* Policy restrictions - hard to get write permissions

To remove this friction, there was a community contribution that permitted to have [read-only incremental snapshots for MySQL](https://debezium.io/blog/2022/04/07/read-only-incremental-snapshots/) leveraging on the `GTIDs`.

## Goals

The goal is to investigate the possibility to have read-only incremental snapshot for others databases too. 

### PostgreSQL

There are two possible way to avoid writing the low/high watermark on the table identified by the `signal.data.collection` property:

* Use [pg_logical_emit_message](https://pgpedia.info/p/pg_logical_emit_message.html****) that permits to write directly into the WAL. It is available from PostgreSQL 9.6.
* Use [pg_current_snapshot](https://pgpedia.info/p/pg_current_snapshot.html) that permits to get a current snapshot - a data structure showing which transaction IDs are now in-progress. It is available from PostgreSQL 13.

The use of `pg_logical_emit_message` is restricted to superusers and users having `REPLICATION` privilege, 
so it is fine to be used with major Debezium configurations except when logical decoder plugin is `pgoutput` and `publication.autocreate.mode` is `disabled`. 
With this setup, Debezium does not require replication privileges since the creation of publications are not automatically managed by Debezium.

Since, in general, it is best to manually create publications for the tables that you want to capture, before you set up the connector, the use of `pg_logical_emit_message` has been discarded.

The `pg_current_snapshot` functions instead does not require any specific privileges, so it is a good fit for meet our goal. 

#### High level algorithm
The `pg_current_snapshot` returns a [pg_snapshot](https://github.com/postgres/postgres/blob/06c418e163e913966e17cb2d3fb1c5f8a8d58308/src/include/utils/snapshot.h#L142) that is composed by:

* xmin: defines the oldest active transaction in the system. All transactions with a txid lower than this value have already been committed.
* xmax: contains the most recent transaction ID known by the snapshot. All tuples with a txid > xmax are invisible by the current snapshot.
* xip: the set of in-progress transaction IDs contained in a snapshot.

> **_NOTE:_** the id returned is `xid8` which never wraps around, so we don't need to take care about it either.

```shell
SELECT pg_current_snapshot();
 pg_current_snapshot 
---------------------
 795:799:795,797
(1 row)
```
The idea is to use the `pg_current_snapshot` to get the `xmin` before starting reading the chunk and the `xmax` after finishing the read of the chunk. 
In this way we can clearly identify the de-duplication window. When an event with `txId` that falls inside the window arrives, it needs to be de-duplicated. 

It is also worth to note that the connection used during incremental snapshot queries doesn't explicitly set the transaction isolation level, 
by default the [READ COMMITTED](https://www.postgresql.org/docs/current/transaction-iso.html) is used.
This will avoid a possible *dirty read* - a transaction reads data written by a concurrent uncommitted transaction.

In pseudocode, the algorithm for deduplicating events read from log and events retrieved via snapshot chunks looks like this: 

```text
(1) pg_snapshot lwSnapshot := pg_current_snapshot()
(2) chunk := select next chunk from table
(3) pg_snapshot hwSnapshot := pg_current_snapshot()
  inwindow := false
  // other steps of event processing loop
  while true do
       e := next event from changelog
       append e to outputbuffer
       if not inwindow then
           if e.txId >= pg_snaphot_min(lwSnapshot)  //reached the low watermark
               inwindow := true
       
       if e.txId < max(pg_snaphot_max(lwSnapshot), pg_snaphot_max(hwSnapshot)) //haven't reached the high watermark yet
           if chunk contains e.key then
               remove e.key from chunk
       else //reached the high watermark
           for each row in chunk do
               append row to outputbuffer
   // other steps of event processing loop
```

The figure below shows the process of deduplication. We have:

* Transaction log: is the resulting database logs, each square represent and event with the indication of: its key, the type of operation (**I**nsert, **U**pdate, **D**elete), and the associated transaction.
* Back-fill chunks: record read from the database during chunk snapshot. Striped one means that the record has been removed by the deduplication.
* Output stream: the final emitted events. 

The thing to note here is what happens to the record with key `k2`. 
This record is read by the snapshot and, since it associated to a transaction that completed during the snapshot - the high watermark reveal only the `Tx13` is still in progress, 
we are sure that this event must be deduplicated. This will effectively remove from the windows the event that was read.

![High-level diagram of incremental snapshotting](DDD-8/postgres-read-only-window.jpg)

#### Proposed changes

Provide a `PostgresReadOnlyIncrementalSnapshotChangeEventSource` that will implement the following methods from the `IncrementalSnapshotChangeEventSource` interface:

```java
void processMessage(P partition, DataCollectionId dataCollectionId, Object key, OffsetContext offsetContext); // (1)
void emitWindowOpen(); // (2)
void emitWindowClose(P partition, OffsetContext offsetContext); // (3)
```
1. Contains the logic to check whenever the low/high watermark reached and deduplication.
2. Call the `pg_current_snapshot()` to get the current in-progress transaction IDs
3. Call the `pg_current_snapshot()` to get the current in-progress transaction IDs, and define the completed transaction IDs.

Extend the `AbstractIncrementalSnapshotContext` with `PostgresReadOnlyIncrementalSnapshotContext` to add information about the low/high watermark sets.

Note that before calling the `pg_current_snapshot()` a call to `pg_current_xact_id ()` must be done to force the database to assign a new transaction ID because we will not any database updates. This enables the `pg_current_snapshot()` to see the others transactions. 

Support for `read.only` property will be added to enable the read only incremental snapshot. 

#### Limitations
The `pg_current_snapshot()` will show only top-level transaction IDs; sub-transaction IDs are not shown;
So if the snapshot is reading a chunk from a table that is modified through a sub-transaction a duplicate may not be recognized. 

Furthermore, the de-duplication algorithm is based on the assumption that the transaction isolation level is set to [READ COMMITTED](https://www.postgresql.org/docs/current/transaction-iso.html).

Since the support for `pg_current_snapshot()` was added on PostgreSQL 13, the read-only incremental snapshot will be available only for PostgreSQl versions greater than or equal to 13. 

### SQL Server

#### Motivation

Read-only incremental snapshots are especially useful when a Debezium SQL Server connector reads from an Always On readable secondary, where avoiding writes is necessary and CDC progress can lag behind the data visible on the replica. The existing CDC watermark, `sys.fn_cdc_get_max_lsn()`, reflects CDC capture progress. Because the capture job runs on the primary and its metadata changes must also be redone on the secondary, this value can lag behind changes already visible in a snapshot read there.

Using this lagging value as the upper boundary of a snapshot window creates a trade-off: waiting for CDC to catch up delays chunk reconciliation, while closing the window too early can allow streamed events to overwrite newer values already read into the chunk. Initial experiments suggest that `sys.dm_hadr_database_replica_states.last_commit_lsn`, read on the replica serving the chunk, can provide a watermark that tracks the data visible there more closely.

#### Goals

* Enable read-only incremental snapshots for SQL Server deployments with a readable Always On secondary.
* Use a replica-local watermark to bound the changes visible to a chunk read, independently of the asynchronous CDC capture job.
* Preserve the existing streamed-event buffering and snapshot-row deduplication behavior.
* Make the delay while CDC catches up, and the AG-specific nature of the approach, explicit.

#### Proposed changes

Use `last_commit_lsn` from `sys.dm_hadr_database_replica_states` as the source of low and high watermarks for a chunk read on an Always On secondary:

1. Read the DMV watermark from the same secondary that will serve the chunk, immediately before starting the chunk's SNAPSHOT-isolation transaction. Convert the DMV's numeric LSN representation to the binary LSN representation used by CDC.
2. Read the chunk in the SNAPSHOT-isolation transaction and commit it.
3. Read the DMV watermark again after the commit to establish the high watermark.
4. Buffer and reconcile streamed CDC events for the chunk's key range using these boundaries, following the existing incremental snapshot window logic.
5. Before closing the window, wait until `sys.fn_cdc_get_max_lsn()` has reached the high watermark, so CDC events through that position are available for reconciliation.

This separates the snapshot's visibility boundary from CDC capture progress: the DMV can describe changes already visible on the secondary even when CDC has not yet captured them. CDC remains the source of change events, and its catch-up time affects when a chunk can be finalized, not the watermark used to describe the chunk read.

Initial tests on SQL Server 2022 Linux containers with a two-node, synchronous-commit AG found that the DMV value bounded the tested snapshot reads from both sides, including during replica suspension/resumption and CDC capture-job disruption. In the same setup, `fn_cdc_get_max_lsn()` was a safe lower bound but lagged the visible snapshot data, so it was not a useful upper bound. These results are preliminary and do not establish a supported contract between the DMV LSN and CDC LSNs.

#### Concerns and open questions

* The DMV-based watermark is specific to Always On deployments and requires appropriate DMV access. Single-instance CDC and deployments without a readable AG secondary need a separate approach.
* The design depends on reading the watermark from the same replica that serves the chunk; routing a chunk and its watermark read to different replicas could invalidate the boundary.
* A DMV position may correspond to a commit that does not produce a CDC event. If CDC never advances to that position, waiting for CDC to reach the high watermark could prevent the window from closing. This behavior needs to be resolved before implementation.
* The numeric-to-binary LSN conversion and watermark behavior have only been tested in a limited SQL Server configuration. Compatibility across supported SQL Server versions, Windows, asynchronous commit, and other AG topologies remains to be established.
* The end-to-end chunk reconciliation flow has not yet been tested. It also remains to be determined whether SQL Server can use shared incremental snapshot window logic or needs connector-specific handling.

#### Implementation work items

1. Verify whether CDC can catch up to a DMV watermark when the last commit is to a table that is not CDC-enabled; define a safe behavior if it cannot.
2. Validate the DMV watermark and LSN conversion across supported SQL Server versions and relevant AG configurations.
3. Integrate the low/high watermark source with incremental snapshot event buffering and chunk reconciliation, ensuring watermark and chunk reads use the same replica.
4. Exercise the complete reconciliation flow and document supported configurations and any required permissions.

### MongoDB

Also for MongoDB the motivation behind the research a possible way to provide read-only incremental snapshot are the same
with a specific addition: try to overcome the challenges related to the different kind of MongoDB topologies. 

#### Topologies
MongoDB has three topology types:

* Standalone server
* Replica set
* Sharded cluster

Since Standalone server do not have the _oplog_ and so Debezium is not able to streaming events from it, we do not consider it. Although MongoDB does not recommend running a standalone server in production.

##### Replica set

A [replica set (RS)](https://www.mongodb.com/docs/manual/core/replica-set-members/) in MongoDB is a group of _mongod_ processes that provide redundancy and high availability. The members of a replica set are:

* Primary:
  receives all write operations.
* Secondaries:
  replicate operations from the primary to maintain an identical data set. To replicate data, a secondary applies operations from the primary's oplog to its own data set in an asynchronous process. A replica set can have one or more secondaries.

> Note: Technically there are also hidden members (secondaries which are purposefully hidden from client advertising) and arbiter nodes (nodes which don't hold data but vote in primary election). 
> Neither of these are likely relevant for our case (although technically a hidden node could be used for CDC with direct connection)

Every node in a RS has its own [_oplog_ collection](https://www.mongodb.com/docs/manual/core/replica-set-oplog/) located into `local.oplog.rs`.

The standard way to connect to RS is using the following connection string

```text
mongodb://user:password@mongodb0.example.com:27017,mongodb1.example.com:27017,mongodb2.example.com:27017/?authSource=admin&replicaSet=myRepl
```

providing the complete or partial list of nodes that compose the RS. 
In that case the client attempts to discover all servers in the RS, and sends operations to the primary member.

By default, an application directs its [read operations](https://www.mongodb.com/docs/manual/core/read-preference/) to the primary member in a replica set (i.e. read preference mode "primary"). But, clients can specify a read preference to send read operations to secondaries.

This type of configuration is common for CDC scenario where users wants to reduce the load on the primary that is already under the write load.

So you can directly connect to a secondary specifying its address and setting the read preference mode accordingly. 

This configuration can lead to a possible weird issues in particular type of deployments. 
For example, if the nodes of the RS has two network address, internal and external, and you configure 
the connection string to point to a secondary using its external address, when the client connects to the RS and retrieve the cluster description, even the intern addresses of the nodes, it will always use the internal address causing connection failures.
For this reason with RS topology it is generally not a good idea to configure the RS with addresses which are not reachable form the outside. 
The correct approach in this case is to use hostnames and make sure that the hostname is resolved to internal address for in-cluster communication and resolve to a public address for external access.

Another approach, but it sacrifices high availability capabilities, is to use the [directConnection](https://www.mongodb.com/docs/manual/reference/connection-string/#mongodb-urioption-urioption.directConnection) property, in that case it will connect only to the specified host in the connection string.

##### Sharded cluster

A [MongoDB sharded cluster](https://www.mongodb.com/docs/manual/core/sharded-cluster-components/) (SC) consists of the following components:

* shard: Each shard contains a subset of the sharded data. Each shard must be deployed as a replica set.

* mongos: The mongos acts as a query router, providing an interface between client applications and the sharded cluster. mongos can support hedged reads to minimize latencies.

* config servers: Config servers store metadata and configuration settings for the cluster. Config servers must be deployed as a replica set (CSRS).

The standard way to connect to SC is using the following connection string

```text
mongodb://user:password@mongos0.example.com:27017,mongos1.example.com:27017,mongos2.example.com:27017/?authSource=admin
```

#### Oplog and Change stream

The [oplog](https://www.mongodb.com/docs/manual/core/replica-set-oplog/) (operations log) is a special capped collection that keeps a rolling record of all operations that modify the data stored in your databases.
If write operations do not modify any data or fail, they do not create oplog entries. 

MongoDB applies database operations on the primary and then records the operations on the primary's oplog. The secondary members then copy and apply these operations in an asynchronous process. All replica set members contain a copy of the oplog, in the local.oplog.rs collection, which allows them to maintain the current state of the database.
To facilitate replication, all replica set members send heartbeats (pings) to all other members. Any secondary member can import oplog entries from any other member.

Change streams allow applications to access real-time data changes without the prior complexity and risk of manually tailing the oplog. Applications can use change streams to subscribe to all data changes on a single collection, a database, or an entire deployment, and immediately react to them.
Change streams is used by MongoDB Debezium connector. 

#### Is read-only incremental snapshot possible?

Reminding us that the incremental snapshot is based on the watermarking approach that permits to de-duplicate events read with events coming from the log, the challenge with the read-only incremental snapshot 
is to provide a way to detect the snapshot window boundaries without writing on the database to mark the log. 

Having a look to the [change stream](https://www.mongodb.com/docs/v5.2/reference/change-events/) event we can see an interesting attribute: _clusterTime_ -
the timestamp from the oplog entry associated with the event.

So if we are able to get the max timestamp in the _oplog_ we can use this value as a log/high watermark, so that the resulting high level algorithm will be:

```text
(1) Pause the streaming
(2) Timestamp lw := getLastTimestamp()
(3) chunk := select next chunk from table
(4) Timestamp hw := getLastTimestamp()
(5) Resume the streaming
  inwindow := false
  // other steps of event processing loop
  while true do
       e := next event from changelog
       append e to outputbuffer
       if not inwindow then
           if e.clusterTime >= lw  //reached the low watermark
               inwindow := true
       
       if e.clusterTime < hw //haven't reached the high watermark yet
           if chunk contains e.key then
               remove e.key from chunk
       else //reached the high watermark
           for each row in chunk do
               append row to outputbuffer
   // other steps of event processing loop
```

where _getLastTimestamp()_ is a function to get the last timestamp available in the _oplog_.
We have found that in the _mongosh_ there is exactly this type of function [getReplicationInfo().tLast](https://www.mongodb.com/docs/manual/reference/method/db.getReplicationInfo/#mongodb-data-db.getReplicationInfo--.tLast).
Unfortunately this is not available through the mongo driver but it easy to replicate looking at the [source code](https://github.com/mongodb/mongo/blob/274f2fddd6a0f975a678e8a953e704f7be6dda14/src/mongo/shell/db.js#L1124).

It just accesses the _local_ db, every node in the RS has its own, and then query the _oplog.rs_ collection to get the max available timestamp. 

First of all, this poses us a problem: the user must have permission to access the _local_ db. This, based on the different [capture.scope](https://debezium.io/documentation/reference/2.7/connectors/mongodb.html#mongodb-property-capture-scope) we support in Debezium, can be a limitation. 
Furthermore, based on the different type of MongoDB topologies there can be problems to have a consistent information reading directly the _oplog_. 
For example, in a scenario where the Debezium user want to connect directly to a replica, we can just read the _oplog_ of that specific node that can have inconsistent information due to the asynchronous nature of the data replication between primary and secondary nodes. 

This in a RS set deployment, and it will be completely not possible in a sharded cluster deployment, since the connection goes through the _mongos_ component.

To solve this limitation we can think to directly use the change stream API instead going directly on the _oplog_. 
So we will have a change stream for the normal event streaming and a temporary change stream opened specifically to get the low and high watermarks.

```java
MongoCursor<ChangeStreamDocument<Document>> cursor = db.watch().cursor(); 
ChangeStreamDocument<Document> lastDocument = cursor.tryNext();


BsonDocument resumeToken = lastDocument != null? lastDocument.getResumeToken() : cursor.getResumeToken();

BsonTimestamp lastTimestamp = ResumeTokens.getTimestamp(resumeToken);
```
the last line will just reuse a Debezium utility class that extract the timestamp from the `_data` field of the resume token.

So the _getLastTimestamp_ function can be implemented using the above code snippets.

##### Open questions

The [MongoDB docs](https://www.mongodb.com/docs/manual/changeStreams/#open-a-change-stream) says:

> For a replica set, you can issue the open change stream operation from any of the data-bearing members. 

They don't tell anything about the possible out of sync of the _oplog_. 