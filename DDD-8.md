# DDD-7: Read-only incremental snapshots for other relational connectors

## Motivation
[Incremental snapshot](https://debezium.io/blog/2021/10/07/incremental-snapshots/) was a major improvements introduced in Debezium 1.6. 
The incremental snapshot was introduced to solve three key issues with Debezium's handling of captured tables. 
First, there was the near-impossibility of adding additional tables to the captured list if existing data needed to be streamed. 
Second, the long-running snapshot process could not be terminated or resumed, requiring a complete restart if interrupted. 
Third, change data streaming was blocked until the snapshot was completed. 
The incremental snapshot addresses these problems by enabling more flexible and efficient data capture.

The incremental snapshot was implemented based on the paper [DBLog: A Watermark Based Change-Data-Capture Framework](https://arxiv.org/pdf/2010.12597v1) by Andreas Andreakis and Ioannis Papapanagiotou.
The main idea behind this approach is that change data streaming is executed continuously together with snapshotting. 
The framework inserts low and high watermarks into the transaction log (by writing to the source database) and between those two points, a part of the snapshotted table is read. 
The framework keeps a record of database changes in between the watermarks and reconciles them with the snapshotted values, if the same records are snapshotted and modified during the window.

This means that the Debezium must have write permission on a specific table used during snapshot to create the low/high watermarks. 

This can be a limitation for some user that cannot give write permission to Debezium, the motivation behind that can be different:

* Capture changes from read-replicas
* Policy restriction

To remove this friction, there was a community contribution that permitted to have [read-only incremental snapshots for MySQL](https://debezium.io/blog/2022/04/07/read-only-incremental-snapshots/) leveraging on the `GTIDs`.

## Goals

The goal is to investigate the possibility to have read-only incremental snapshot for others databases too. 

### PostgreSQL

There are two possible way to avoid writing the low/high watermark on the table identified by the `signal.data.collection` property:

* Use `pg_logical_emit_message` that permits to write directly into the WAL. It is available from PostgreSQL 9.6.
* Use `pg_current_snapshot` that permits to get a current snapshot - a data structure showing which transaction IDs are now in-progress. It is available from PostgreSQL 13.

The use of `pg_logical_emit_message` is restricted to superusers and users having REPLICATION privilege, 
so it is fine to be used with major Debezium configurations except when logical decoder plugin is `pgoutput` and `publication.autocreate.mode` is `disabled`. 
With this setup, Debezium does not require replication privileges since the creation of publications are not automatically managed by Debezium.

Since, in general, it is best to manually create publications for the tables that you want to capture, before you set up the connector, the use of `pg_logical_emit_message` has been discarded.

The `pg_current_snapshot` functions instead does not require any specific privileges, so it is a good fit for meet our goal. 

#### High level algorithm
The `pg_current_snapshot` return a [`pg_snapshot`](https://github.com/postgres/postgres/blob/06c418e163e913966e17cb2d3fb1c5f8a8d58308/src/include/utils/snapshot.h#L142) that is composed by:

* xmin: defines the oldest active transaction in the system. All transactions with a txid lower than this value have already been committed.
* xmax: contains the most recent transaction ID known by the snapshot. All tuples with a txid > xmax are invisible by the current snapshot.
* xip: the set of in-progress transaction IDs contained in a snapshot.

```shell
SELECT pg_current_snapshot();
 pg_current_snapshot 
---------------------
 795:799:795,797
(1 row)
```

```text
(1) pause log event processing
(2) Set lwInProgressTxSet := xip from pg_current_snapshot()
(3) chunk := select next chunk from table
(4) Set hwInProgressTxSet := xip from pg_current_snapshot() subtracted by lwInProgressTxSet
(5) resume log event processing
  inwindow := false
  // other steps of event processing loop
  while true do
       e := next event from changelog
       append e to outputbuffer
       if not inwindow then
           if lwInProgressTxSet.contains(e.txId) //reached the low watermark
               inwindow := true
       
       if hwInProgressTxSet.contains(e.txId) //haven't reached the high watermark yet
           if chunk contains e.key then
               remove e.key from chunk
       else //reached the high watermark
           for each row in chunk do
               append row to outputbuffer
   // other steps of event processing loop
```
#### Proposed changes

