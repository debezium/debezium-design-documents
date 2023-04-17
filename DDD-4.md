# DDD-4: Offset Read/Write API

## Motivation

Debezium is a platform to stream changes executed on data by processing transaction logs of a database.
As changes are processed, a position into the transaction log is maintained so that Debezium can safely resume on restarts.

This position, often referred to as offsets, differs by connector.
While the MySQL connector maintains a reference to the binlog filename and a position within the binlog, other connectors maintain state that is specific to that database vendor's implementation.
Therefore, there is no single common structure for these offsets.

Debezium UI is a web-based application that is meant to simplify the deployment and management of Debezium connectors.
We want to improve the user experience with the UI by introducing the ability to perform the following tasks:

* Start, Stop, Pause, and Resume Incremental Snapshots
* Manipulate connector offsets

Both of these tasks require the ability to read connector offsets and the latter requires the ability to write to the offsets.

This document is currently based on **Debezium 2.2.0.CR1**.

## Goals

* Provide support for reading connector offsets
* Provide support for writing connector offsets
* Provide a common schema-based API for working with offset data that is universal for all connectors
* Integrate this behavior into the Debezium UI backend

## Current connector offset behavior

Debezium 2.x supports the notion of partition-based offsets based on the following contracts:

* `io.debezium.pipeline.spi.OffsetContext`
* `io.debezium.pipeline.spi.OffsetContext$Loader`
* `io.debezium.pipeline.spi.Partition`
* `io.debezium.pipeline.spi.Partition$Provider`
* `io.debezium.connector.common.OffsetReader`

These contracts allow the reading of the offset metadata by providing an instance of `org.apache.kafka.connect.storage.OffsetStorageReader` via the `org.apache.kafka.connect.source.SourceTaskContext`, enabling the connector to deserialize the offsets from the appropriate storage implementation.  

## Current connector offset attributes

The following describes the current offset data managed by each connector implementation.

### Cassandra 

The Cassandra 4 and 4 implementations maintain identical offset formats.
The Cassandra connector maintains a key/value pair map that consists of keys based on the keyspace table name and values that refer to a serialized string using the format of `<file-name>:<file-position>`.

So for example, assuming a keyspace `cycling` with a table named `birthday_list`, if a change was detected for this table at position `12345` in the commit log, a entry would exist in this map similar to the following:

```json
{ "birthday_list" : "commitlog-<version>-<msec_timestamp>.log:12345" }
```

For Cassandra, we need to support:

* Adding or Removing keys from this map.
* Modifying the value associated with a given key in this map.

### Db2

The Db2 connector maintains offsets that align with our standard relational connectors.
It maintains a set of Db2-specific key/value pairs as well as some common state available to all relational connector, such as incremental snapshots.
The following describes the Db2-specific fields maintained in the offsets and if there are references in the offsets to common attributes, the comments in the code blocks will refer to those.

This connector maintains a single partition of offsets, based on the `topic.prefix`.

The following refers to the offsets during snapshots:
```json
{
  "snapshot": true, // boolean
  "snapshot_completed": false, // boolean
  "commit_lsn": "<commit_lsn>" // string
}
```

The following refers to the offsets during streaming:
```json
{
  "commit_lsn": "<commit_lsn>", // string
  "change_lsn": "<change_lsn>", // string (may be null)
  "event_serial_no": 0, // long (cannot be null but may be 0)
  
  // See Transaction Monitoring for common transaction-based key/value pairs
  // See Incremental Snapshots for common incremental-snapshot key/value pairs
}
```

### Google Spanner

TBD

### MongoDB

TBD 

### MySQL

The MySQL connector maintains offsets that align with our standard relational connectors.
The offsets consist of a set of MySQL-specific attributes that are managed regardless of the connector's phase as well as attributes managed when in the snapshot or streaming phase.
There is also common state such as incremental snapshots that is also managed in the offsets, see the code block comments where applicable.

This connector maintains a single partition of offsets, based on the `database.dbname`.

#### Fields regardless of the connector's phase
```json
{
  "server_id": 12345, // long, refers to the configured database.server.id
  "gtids": "<restart-gtids-set>", // string, contains the restart gtids, should be omitted if value is empty/null
  "file": "<binlog-filename>", // string, the binlog filename
  "pos": 123, // long, the position in the binlog file where the event starts
  "event": 1, // long, should be omitted if the value is 0
  "row": 1, // long, should be omitted if the value is 0
  "ts_ms": 123456789000 // long, epoch seconds of a timestamp (optional), omit if null
}
```

The following refers to the offsets during snapshots:
```json
{
  "snapshot": true, // boolean
  // See common attributes shown above
}
```

The following refers to the offsets during streaming:
```json
{
  // See common attributes shown above
  // See Transaction Monitoring for common transaction-based key/value pairs
  // See Incremental Snapshots for common incremental-snapshot key/value pairs
}
```

### Oracle

The Oracle connector maintains offsets that align with our relational connectors. 
The offsets consist of a set of state that differs based on whether the connector is in a snapshot or streaming phase.

This connector maintains a single partition of offsets, based on either the configured `pdb.name` or `database.dbname`.

The following refers to the offsets during snapshot:
```json
{
  "scn": "<system-change-number>", // string, may be null
  "snapshot": true, // boolean
  "snapshot_completed": false, // boolean
  "snapshot_pending_tx": "<tx1>:<scn1>,<tx2>:<scn2>,...", // string, comma-separated of "transaction-id:system change number" tuples
  "snapshot_scn": "<system-change-number>" // string, may be null
}
```

The following refers to the offsets during streaming:
```json
{
  "snapshot_scn": "<system-change-number>", // string, may be null
  "snapshot_pending_tx": "<tx1>:<scn1>,<tx2>:<scn2>,...", // string, comma-separated of "transaction-id:system change number" tuples
  
  // The following are applicable only if the connector is using XStream
  "lcr_position": "<position>", // string, can be omitted if null
  
  // The following are applicable only if the connector is using LogMiner
  "scn": "<system-change-number>", // string
  "commit_scn": "<commit-scn-serialized-string>", // string, see below for serialized format specifications

  // See Transaction Monitoring for common transaction-based key/value pairs
  // See Incremental Snapshots for common incremental-snapshot key/value pairs 
}
```

The `commit_scn` field is required by the Oracle connector when using the LogMiner adapter (the default).
This field has undergone a significant transformation over the past several releases and therefore its critical to understand how to serialize/deserialize this as indicated below:

| Version           | Format                                                                     |
|-------------------|----------------------------------------------------------------------------|
| Up to 1.9.4.Final | `<commit-scn>`                                                             |
| 1.9.5.Final       | `<commit-scn>:<rollback-segment-id>:<sql-sequence-number>:<arch-thread-#>` |
| 1.9.6.Final       | `<commit-scn>:<arch-thread-#>:<transaction-id-list>`                       |
| 1.9.7.Final       | `<commit-scn>:<arch-thread-#>:<transaction-id-list>`                       |  
| 2.0.0.Final+      | `<commit-scn>:<arch-thread-#>:<transaction-id-list>`                       |

The `transaction-id-list` portion of the serialized field in 1.9.6.Final+ and later represents a concatenation of transaction ids, in lower-case hex format where each transaction-id is separated by a dash `-`.
An example of a recent `commit_scn` field for an Oracle RAC 2 node environment would be:

```
1234567890:1:5af87ce941a2-978a445f3213,9876543210:2:78f8eaf8741a
```

We can see that Node 1 has a commit scn of `123456789` and two transactions whereas Node 2 has a commit scn of `9876543210` with a single transaction.

### PostgreSQL

The PostgreSQL connector maintains offsets that align with our relational connectors.
The offsets consist of a set of state that differs based on whether the connector is in a snapshot or streaming phase.

This connector maintains a single partition of offsets, based on `database.dbname`. 

The following refers to the offsets regardless of streaming/snapshot:
```json
{
  "ts_usec": 3214567897, // long, epoch microseconds, omit if null
  "txId": 123456, // long, omit if null
  "lsn": 12345, // long, omit if null
  "xmin": 12345, // long, omit if null
  "lsn_proc": 12345, // long, omit if null
  "lsn_commit": 12345, // long, omit if null
  "messageType": "INSERT", // string, omit if null
}
```

The following refers to the offsets during snapshot:
```json
{
  "snapshot": true, // boolean
  "last_snapshot_record": false, // boolean
  // See common attributes shown above
}
```

The following refers to the offsets during streaming:
```json
{
  // See common attributes shown above
  // See Transaction Monitoring for common transaction-based key/value pairs
  // See Incremental Snapshots for common incremental-snapshot key/value pairs 
}
```

### SQL Server

The SQL Server connector maintains offsets that align with our standard relational connectors.
It maintains a set of SQL Server-specific key/value pairs as well as some common state available to all relational connector, such as incremental snapshots.
The following describes the SQL Server-specific fields maintained in the offsets and if there are references in the offsets to common attributes, the comments in the code blocks will refer to those.

This connector maintains multiple partitions of offsets, based on `database.names`.

The following refers to the offsets during snapshots:
```json
{
  "snapshot": true, // boolean
  "snapshot_completed": false, // boolean
  "commit_lsn": "<commit_lsn>" // string
}
```

The following refers to the offsets during streaming:
```json
{
  "commit_lsn": "<commit_lsn>", // string
  "change_lsn": "<change_lsn>", // string (may be null)
  "event_serial_no": 0, // long (cannot be null but may be 0)
  
  // See Transaction Monitoring for common transaction-based key/value pairs
  // See Incremental Snapshots for common incremental-snapshot key/value pairs
}
```

TBD - Describe how multi-partitioning impacts offset stores.

### Vitess

The Vitess connector maintains offsets that align with our standard relational connectors.

This connector maintains a single partition of offsets, based on either the `topic.prefix` or a combination of `topic.prefix` and the *taskId*. 

```json
{
  "vgtid": "<restart-gtids-json-array-string>", // string, omit if null
  // See Transaction Monitoring for common transaction-based key/value pairs
}
```

## Transaction Monitoring

A subset of Debezium connectors manage transaction-based offset attributes.
If a connector supports transaction monitoring features, the following additional offset attributes are managed by the connector:

```json
{
  "transaction_id": "<transaction_id>", // string - the current transaction being processed
  "transaction_data_collection_order_<table-name>": 1 // long
}
```

The `transaction_data_collection_order_<table-name>` entry is a key that is dynamic where the `<table-name>` is replaced by the fully qualified `TableId`. 
This means there can be multiple entries for different tables in the offsets where the value associated with each key represents the number of events observed within the transaction for that specific table.

## Incremental Snapshots

A subset of Debezium connectors manage incremental snapshot offset attributes.
If a connector supports Incremental Snapshots, these attributes will only be visible in the offsets when an Incremental Snapshot is in progress.
These attributes will not appear if the incremental snapshot has concluded or is never started.
The basic format of incremental snapshot attributes is:

```json
{
  "incremental_snapshot_primary_key": "<data>", // string, Object[] serialized to byte array converted to Hex
  "incremental_snapshot_maximum_key": "<data>", // string, Object[] serialized to byte array converted to Hex
  "incremental_snapshot_collections": "<json>" // string
}
```
The incremental snapshot context will serialize the collections that are still to be incrementally snapshot to the connector offsets as JSON and store this serialized value in the `incremental_snapshot_collections` field.
This field is a JSON array of entries that have the following format:

```json
{
  "incremental_snapshot_collections_id": "<database>.<schema>.<table>", // string, the TableId
  "incremental_snapshot_collections_additional_condication": "ID = 25", // string, the additional where condition, may be null
  "incremental_snapshot_collection_surrogate_key": "NAME" // string, may be null.
}
```

Therefore, the expected layout of the `incremental_snapshot_collections` field would be `[{obj1}, {obj2}, ...]`.

## Proposed changes

We need to introduce a minimum of 3 backend endpoints:

* An endpoint to read the connector offsets based on a provided connector name.
* An endpoint to update the connector offsets based on a provided connector name.
* An endpoint to remove connector offsets based on a provided connector name.

The following rules must be observed at all times:

* It is *safe* to read connector offsets, regardless of the connector's current state.
* It is **not safe** to update connector offsets if the connector is in `RUNNING` or `PAUSED` state.
* It is **not safe** to delete connector offsets if the connector is in `RUNNING` or `PAUSED` state.

### Read endpoint

### Update endpoint

TBD 

### Delete endpoint

TBD

## Open questions

None.






