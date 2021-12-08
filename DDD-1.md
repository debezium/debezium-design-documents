# DDD-1: Support for multiple source partitions for relational Debezium connectors

## Motivation

Currently, it is challenging to use Debezium Connector for SQL Server in a multi-tenanted environment where a single SQL Server instance hosts tens to hundreds of similar tenant-scoped databases due to the fact that a given connector instance can capture changes only from a single database. The challenges are:

1. Kafka Connect resources. Deploying hundreds of connector instances will require a Kafka Connect cluster with significant CPU and memory resources.
2. Cluster stability. If the resources above are insufficient (e.g. memory), it may cause a Connect worker crash which in turn will fail all connector tasks scheduled to the worker. In the best case scenario, those tasks can be just restarted. In a worse case scenario, the restarted connectors will produce duplicates since the source connectors currently don't implement exactly-once processing semantics. In an even worse case, a worker can fail during a long-running snapshot, handling which will require additional cleanup.
3. SQL Server resources. Each server instance will have to maintain an extra set of persistent connections (one per database, tens to hundreds in total) which may require additional server resources.

## Proposed Changes

### Source partition awareness at the task level

Currently, a Debezium connector task can process a single source partition throughout its lifecycle. This partition is usually hard-coded in the context initialization code. E.g.:

```java
public SqlServerOffsetContext(SqlServerConnectorConfig connectorConfig) {
        partition = Collections.singletonMap(SERVER_PARTITION_KEY, connectorConfig.getLogicalName());
```

In order to achieve partition-awareness, we need to introduce an API and a set of components that do the following:

1. Describe a *set* of source partitions to be processed by the task.
2. Initialize the set above. Normally, based on the task configuration.
3. Represent a source partition in higher-level terms specific to a given connector (e.g. database name).
4. Allow mapping of connector-level partition (e.g. a database name) to the corresponding Kafka Connect source partition.
5. Allow mapping of connector-level partition to the corresponding `OffsetContext`.
6. Instead of a single `OffsetContext` per task, use a map of connector-level partitions to their offset context.

### New configuration parameters

Being able to specify a list of databases to be captured by a connector warrants the introduction of a new configuration parameter:

| Name             | Type     | Default | Importance | Doc                                                          |
| :--------------- | :------- | :------ | :--------- | :----------------------------------------------------------- |
| `database.names` | `STRING` | `None`  | `HIGH`     | The list of comma-separated database names form which the given connector task should capture the changes. If specified, must be non-empty. |

The connector will allow configuring only one of the `database.dbname` and `database.names` parameters.

### New framework-level components

#### `Partition` interface

The `Partition` interface describes the source partition to be processed by the connector in connector-specific terms and provides its representation as a Kafka Connect source partition.
```java
public interface Partition {
    Map<String, String> getSourcePartition();
}
```

For instance, a SQL Server connector partition will contain the source server name and the database name.

In order to be compared by value, not by identity, each `Partition` implementor will have to implement the `equals` and the `hashCode` methods.

#### `Partition.Provider` interface

To reduce the amount of the connector initialization code, it's proposed to introduce a partition `Provider` interface. Once initialized with the connector configuration, the implementation will return a `Set` of partitions that the connector should process.

```java
interface Provider<P extends Partition> {
    Set<P> getPartitions();
}
```

#### `Offsets` class

The `Offsets` class describes a map of source partitions that the connector should process to their corresponding offset contexts.

```java
public final class Offsets<P extends Partition, O extends OffsetContext> {
}
```

### Key implementation changes

#### Extract partition from offset context

Since the source partition details are now represented by a separate interface, there is no longer reason to keep the same information in the offset context. Additionally, it allows for a cleaner separation of concerns. A partition represents part of the connector configuration, while the offset context represent its state.

#### Use fully-qualified names for all tables and procedures in Debezium connector for SQL Server

This is where the connector-level partitions defined in the beginning come into play. Given a partition, the connector will be able to call `partition.getDatabase()` and perform a query / execute a statement on a connection that isn't scoped to any specific database.

#### Rework the "real" database name detection logic

Currently, the "real" name of the source database is detected right after connecting to the database at the connection level. In order to make the connection reusable across multiple databases (https://github.com/sugarcrm/debezium/issues/10), we need to:

1. Detect the "real" name of all databases the task is working with.
2. Make the "real" name available to all components that use it right now.

The most natural place for that seems to be the `SqlServerPartition` and its `Provider` being introduced as part of [#15](https://github.com/sugarcrm/debezium/pull/15).

#### Introduce partition awareness to the core components

In order to enable connector tasks to process multiple source partitions, we need to add the `partition` parameter to the change event source methods. This way, each event source could be reused for processing multiple partitions with a minimal increase in the memory footprint.

Even though some of the existing connectors are single-partitioned by their nature (e.g. the MySQL connector), in order to keep the API simple, all connectors' event sources will accept the source partition.

If a connector support processing multiple partitions, its the event source coordinator, instead of just switching from snapshot to the streaming sources, will iterate over a collection of partitions/offsets and reuse each source for each of them:

**Single-partition (current) control flow**:

```java
// offset context is part of the source state
snapshotSource  = new SnapshotSource(offsetContext)
streamingSource = new StreamingSource()

// snapshot is executed for its only partition
result = snapshotSource.execute()

// streaming is done from the only partition/offset
streamingSource.execute(result.getOffset())
```

**Multi-partition control flow**:

```java
// offset context is part of the interface, not the state
snapshotSource  = new SnapshotSource()
streamingSource = new StreamingSource()

offsets.forEach((partition, offset) -> {
  // snapshot is executed for each given partition
  result = snapshotSource.execute(partition)
  // result are stored per-partition
  results.put(partition, result);
})

results.forEach((partition, offset) -> {
  // streaming is done from one partition at a time
  streamingSource.execute(partition, result.getOffset())
})
```

#### Move the poll interval logic to the change event source coordinator

The above iteration of streaming sources won't work if implemented as is. The reason is that `streamingSource.execute()` will block until the task is stopped.

We need to remove `while (context.isRunning())` from the logic of partitioned streaming sources (e.g. SQL Server) and move this logic to the coordinator:

```java
while (context.isRunning()) {
  results.forEach((partition, offset) -> {
    // streaming is done per-partition
    streamingSource.execute(partition, result.getOffset())
  })
}
```

#### Recover database schema from multiple partitions in one pass ([#2871](https://github.com/debezium/debezium/pull/2871))

Currently, the internal schema representation used by the connector is capable of representing multiple databases (e.g. for the MySQL connector) but isn't capable of initializing from the database history of multiple partitions. It should be possible to initialize it multiple times, once for each partition but it may be suboptimal, especially if the database history topic and the number of partitions are large.

Instead, the schema could recover from multiple offsets in one pass.

**Before**:

```java
for (message : messages) {
  if (message.partition == partition && message.offset < offset) {
    apply(message);
  }
}
```

**After**:

```java
for (message : messages) {
  if (offsets.contains(message.partition)
      && message.offset < offsets.get(message.partition)) {
      apply(message);
    }
  }
}
```

#### Changes in mapping table identifiers to topic names and internal schema representation

In order to accomodate changes from multiple databases, instead of naming topics as `<server>.<schema>.<table>`, the connector will name them as `<server>.<database>.<schema>.<table>`.

The changes implemented in [DBZ-1089](https://issues.redhat.com/browse/DBZ-1089) should be partially reverted since for SQL Server both the database name and schema name should be taken into account when building schema.

#### Changes in metrics

Currently, each connector has one task which processes one source partition. At any moment in time, the connector metrics are represented by a single MBean with the following name:

```
debezium.sql_server:type=connector-metrics,context=<context>,server=<server>
```

The one-to-many deployment topology requires the addition of two more dimensions to the metrics exposed by the connector:

1. Task-level metrics. E.g. “is connected“. Each task run by the connector may be connected to or disconnected from the server independently on others. Task-level metrics will be reported by the following beans:

   ```
   debezium.sql_server:type=connector-metrics,context=<context>,server=<server>,task=<id>
   ```

2. Partition-level metrics. E.g. “snapshot completed“. The snapshot of each individual database may or may be not completed independently on others. Partition-level metrics will be reported by the following beans:

   ```
   debezium.sql_server:type=connector-metrics,context=<context>,server=<server>,database=<database>
   ```

   Note, in order to be able to display task-scoped metrics for a given partition, we may need to add `task=<id>` to the partition-scoped metrics as well. See the "Grafana dashboards" section.

##### Metrics implementation details:

*Note: the following has been implemented in a working prototype but may change before the integration into the upstream repository.*

Internally, metrics for each of the three [contexts](https://debezium.io/documentation/reference/1.5/connectors/sqlserver.html#sqlserver-monitoring) are represented by a `Metrics` class which fulfills two responsibilities (each is declared by an interface):

1. `Listener`: receives status updates updates from the task (e.g. “connected to the database“).
2. `MXBean`: exposes the current status to the JMX server (e.g. “is connected to the database“).

In order to accommodate the new dimensions, the context metrics classes and interfaces above need to be split in smaller pieces with the following behavior:

1. In each context, the declaration of all partition-scoped and task-scoped metrics are extracted into the corresponding separate `MXBean` interfaces and classes. For instance: `ChangeEventSourceMetricsMXBean` → `ChangeEventSourceMetricsMXBean` (common metrics) + `ChangeEventSourcePartitionMetricsMXBean` (partition metrics) + `ChangeEventSourceTaskMetricsMXBean`.

2. Each metrics class still acts as a listener for all status changes regardless of the scope, but for partition-scoped parameters, it accepts an additional argument to identify the partition from which an update is coming. For instance:

   `void snapshotCompleted()` → `void snapshotCompleted(Partition partition)`

3. Each metrics object internally maintains a collection of `MXBean` objects representing one partition each and delegates partition-scoped updates to the corresponding bean.

4. Each metrics object accepts the task identifier in its constructor and uses it for task-scoped metrics and acts as a task-scoped `MXBean`.

5. When registering or unregistering with a JMX server, the metrics class also registers or unregisters its underlying beans.

6. The names of partition-scoped beans must contain the labels that describe the partition. They could be directly derived from `Partition#getSourcePartition()` since it’s also of type `Map<String, String>`. In order to make bean names consistent, the source partition of each connector-level partition must be represented by a `LinkedHashMap`.

##### Additional code changes

Some test helper methods like `TestHelper#waitForSnapshot` use metrics internally as the source of task status. They need to be reworked to expect the snapshot to be done on a specific partition (e.g. `TestHelper#waitForSnapshot(Partition partition)`).

### Backward compatibility considerations

By configuring `database.names`, even if it contains a single value, the user of the connector will opt into the multi-partition mode. The new mode will be incompatible with the existing API in the following aspects:

##### Source partition of the messages produced by the connector

Instead of containing only the server name, the source partition of the records produced by the connector will contain the server name and partition details (e.g. the database name). If a connector using the default configuration is reconfigured to use multiple partitions, it should attempt to read the offsets produced in the multi-partition mode and fall back to reading the single-partition offsets.

If the connector is reconfigured back to the single-partition mode, the stored offsets will have to be prepared manually.

##### Topic and message names

In order to distinct the tables from different databases, instead of naming topics as `<schema>.<table>`, the connector in multi-partition mode will name the topics as `<database>.<schema>.<table>`. The same applies to the message schema names.

##### Snapshot `SELECT` overrides for SQL Server connector

Since the JDBC connection in multi-partition mode doesn't connect to any specific database, the `SELECT` statement that performs the snapshot of a given table should be configured for each database and contain the fully-qualified table name including the database name. For instance,

```
"snapshot.select.statement.overrides": "tenant1.dbo.orders,tenant2.dbo.orders",
"snapshot.select.statement.overrides.tenant1.dbo.orders": "SELECT * FROM tenant1.dbo.orders where delete_flag = 0 ORDER BY id DESC"
"snapshot.select.statement.overrides.tenant2.dbo.orders": "SELECT * FROM tenant2.dbo.orders where delete_flag = 0 ORDER BY id DESC"
```

This could be improved in the future by allowing to use patterns in the snapshot configuration.

##### Metrics

In the multi-partition configuration, the MBean names will be different and will contain the information about the task and the partition which a given set of value describes.

### Support for multiple tasks per connector

Apart from being able to process multiple databases by a given task, it would be great if a connector was able to start multiple tasks and divide the databases between them. This way, it would allow to scale a single connector just by changing its `max.tasks` configuration.

In order to implement this, the `SqlServerConnector` class needs to take the list of databases supplied via `database.names`, divide it into multiple lists and instead of returning one configuration from `taskConfigs`, return one configuration per task with its share of `database.names`.
