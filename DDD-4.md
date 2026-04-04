# DDD-4: Manipulate Connector Offset via Debezium Signals

## Motivation

Debezium is a platform to stream changes executed on data by processing transaction logs of a database.
As changes are processed, a position into the transaction log is maintained so that Debezium can safely resume on restarts or failures.

This position, often referred to as offsets, differs by connector. While the MySQL connector maintains a reference to the binlog filename and a position within the binlog, other connectors maintain state that is specific to that database vendor's implementation.
Therefore, there is no single common structure for these offsets.

Sometimes it is necessary to manipulate these offsets, for example, to reset a connector to a specific point in time or to resume from a specific point in time. There has been a long-standing need to provide a common way of managing these offsets, and this document proposes a solution to this problem. Until now the user had to manage the offsets themselves, which is error-prone and cumbersome.

This document proposes a solution to this problem by using Debezium signals to handle the offset management in a common way. This will allow users to manage the offsets in a consistent way across all connectors.

## Goals

* Provide support reading connector offsets
* Provide support writing connector offsets


## Current connector offset behavior

Debezium 3.x supports the notion of partition-based offsets based on the following contracts:

* `io.debezium.pipeline.spi.OffsetContext`
* `io.debezium.pipeline.spi.OffsetContext$Loader`
* `io.debezium.pipeline.spi.Partition`
* `io.debezium.pipeline.spi.Partition$Provider`
* `io.debezium.connector.common.OffsetReader`

These contracts allow the reading of the offset metadata by providing an instance of `org.apache.kafka.connect.storage.OffsetStorageReader` via the `org.apache.kafka.connect.source.SourceTaskContext`, enabling the connector to deserialize the offsets from the appropriate storage implementation.

## Current connector offset attributes

The following describes the current offset data managed by each connector implementation.

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
  "incremental_snapshot_collections_additional_condition": "ID = 25", // string, the additional where condition, may be null
  "incremental_snapshot_collection_surrogate_key": "NAME" // string, may be null.
}
```

Therefore, the expected layout of the `incremental_snapshot_collections` field would be `[{obj1}, {obj2}, ...]`.

## Proposed Solution

Provide a signal action to read and write offsets for each connector. The signal action will be a new action type in the Debezium signal framework.

### Signal Action

The `Change Offset` signal action will be used to change the offset of a connector. The action will have the following attributes:

* `name` - The name of the action. This will be `change-offset`.
* `connector-offsets` - The new offset values to be set. This will be a map of key-value pairs containing the fields required to set the offset. The fields will be specific to the connector implementation.

The signal action will look like this:

| KEY               | VALUE                                                                                      |
|-------------------|--------------------------------------------------------------------------------------------|
| change-offset     | {"data": {"connector-offsets": [{"lsn": 12345, "lsn_proc": 12345, "lsn_commit": 12345 }]}} |

### Offset Fields

There will be a validation mechanism to ensure that the offset fields provided in the signal action are valid for the connector. To achieve this there will be a new interface `io.debezium.pipeline.spi.OffsetValidator` 
and a common class `io.debezium.pipeline.spi.CommonOffsetValidator` that will somewhat look like this:

### Implementation

There will be a new interface `io.debezium.pipeline.spi.ChangeOffsetHandler` that will be used to load and validate the offset fields. The interface will have the following methods:

```java
public interface ChangeOffsetHandler<O extends OffsetContext> {

    O load(Map<String, ?> data);
    
    String validate(Map<String, ?> data);

    List<String> getRequiredFields();
}
```

There will be a new class `io.debezium.pipeline.spi.CommonChangeOffsetHandler` that will be used to load the `ChangeOffsetHandler` for a specific connector. For example, the `PostgresChangeOffsetHandler` will look like this:

```java
public class PostgresChangeOffsetHandler extends CommonChangeOffsetHandler {
    
    @Override
    O load(Map<String, ?> data) {
        // Load the offset fields
    }

    @Override
    String validate(Map<String, ?> data) {
        // Validate the offset fields
    }

    @Override
    List<String> getRequiredFields() {
        // Return the required fields to change the offset
    }
}
```

### Commit Offset

Upon receiving an offset change signal, the connector pauses event streaming, flushes the buffer, updates its offset position, and reloads. Once processed, it awaits offset commit acknowledgment from Kafka Connect before resuming event capture. Details of this to be discussed.
