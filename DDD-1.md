# DDD-1: Support for multiple connector tasks for relational Debezium connectors

## Motivation

Currently, it is challenging to use Debezium Connector for SQL Server in a multi-tenanted environment where a single SQL Server instance hosts tens to hundreds of similar tenant-scoped databases due to the fact that a given connector instance can capture changes only from a single database. The challenges are:

1. Kafka Connect resources. Deploying hundreds of connector instances will require a Kafka Connect cluster with significant CPU and memory resources.
2. Cluster stability. If the resources above are insufficient (e.g. memory), it may cause a Connect worker crash which in turn will fail all connector tasks scheduled to the worker. In the best case scenario, those tasks can be just restarted. In a worse case scenario, the restarted connectors will produce duplicates since the source connectors currently don't implement exactly-once processing semantics. In an even worse case, a worker can fail during a long-running snapshot, handling which will require additional cleanup.
3. SQL Server resources. Each server instance will have to maintain an extra set of persistent connections (one per database, tens to hundreds in total) which may require additional server resources.

## Proposed Changes

### Source partition awareness at the task level ([#15](https://github.com/sugarcrm/debezium/pull/15))

Currently, a Debezium connector task can process a single source partition throught its lifecycle. This partition is usually hard-coded in the context initialization code. E.g.:

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

### Support for multiple tasks per connector ([#9](https://github.com/sugarcrm/debezium/issues/9))

Apart from being able to process multiple databases by a given task, it would be great if a connector was able to start multiple tasks and divide the databases between them. This way, it would allow to scale a single connector just by changing its `max.tasks` configuration.

Being able to specify which subset of the databases a given task should process warrants the introduction of a new task-level configuration parameter ([#17](https://github.com/sugarcrm/debezium/issues/17)):

| Name                  | Type     | Default | Importance | Doc                                                          |
| :-------------------- | :------- | :------ | :--------- | :----------------------------------------------------------- |
| `task.database.names` | `STRING` | `None`  | `HIGH`     | The list of comma-separated database names form which the given connector task should capture the changes from. Must be non-empty. |

The connector can monitor the list of CDC-enabled databases and maintain the list of databases to capture the changes from ([#11](https://github.com/sugarcrm/debezium/issues/11)). This will require another configuration parameter:

| Name            | Type      | Default | Importance | Doc                                                          |
| :-------------- | :-------- | :------ | :--------- | :----------------------------------------------------------- |
| `to.be.defined` | `BOOLEAN` | `False` | `Medium`   | If set to true, the connector will monitor the list of CDC-enabled databases and capture changes from all of them. |

### Recover database schema from multiple partitions in one pass ([#14](https://github.com/sugarcrm/debezium/issues/14))

Currently the internal schema representation used by the connector is capable of representing multiple databases (e.g. for the MySQL connector) but isn't capable of initializing from the database history of multiple partitions. It should be possible to initialize it multiple times, once for each partition but it may be suboptimal, especially if the database history topic and the number of partitions are large.

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

### Rework the "real" database name detection logic ([#21](https://github.com/sugarcrm/debezium/issues/21))

Currently, the "real" name of the source database is detected right after connecting to the database at the connection level. In order to make the connection reusable across multiple databases (https://github.com/sugarcrm/debezium/issues/10), we need to:

1. Detect the "real" name of all databases the task is working with.
2. Make the "real" name available to all components that use it right now.

The most natural place for that seems to be the `SqlServerTaskPartition` and its `Provider` being introduced as part of #15.

### Use fully-qualified names for all tables and procedures in Debezium connector for SQL Server ([#10](https://github.com/sugarcrm/debezium/issues/10))

See details in [#10](https://github.com/sugarcrm/debezium/issues/10). This is where the connector-level partitions defined in the beginning come into play. Given a partition, the connector will be able to call `partition.getDatabase()` and perform a query / execute a statement on a connection that isn't scoped to any specific database.

### Introduce `Partitioned*` APIs ([#13](https://github.com/sugarcrm/debezium/pull/13), [#16](https://github.com/sugarcrm/debezium/issues/16), [#18](https://github.com/sugarcrm/debezium/issues/18))

####  `Partitioned*` interfaces

Despite the fact that there are multiple connectors that can benefit from being aware, some connectors are nonpartitioned by their nature. For instance, the MySQL connector consumes all the changes from a nonpartitioned binlog, and it would make sense to have its API and the implementation affected by the changes being proposed as little as possible.

In order to accomodate partition-aware and -unaware components under the same framework, it's proposed to introduce the `Partitioned` analogues of the following interfaces:

1. `SnapshotChangeEventSource` → `PartitionedSnapshotChangeEventSource`
2. `StreamingChangeEventSource` → ``PartitionedStreamingChangeEventSource`
3. `ChangeEventSourceFactory` → `PartitionedChangeEventSourceFactory`

Each of their methods that implements capturing data will have two more arguments added:

1. `<P extends TaskPartition> taskPartition` to identify which task partition the method call should use.
2. `<O extends OffsetContext> offsetContext` the offset context that corresponds to the partition.

This way, each of the connectors could implement the relevant of the two APIs. It will also allow connectors to transition from one API to another and allow the user to chose the mode via configuration.

#### Affect on framework-level classes

Some framework-level classes that are reused by multiple connectors and depend on the interfaces above will have to be reworked to satisfy both of the APIs. It could be either one class per interface or one class implementing both interfaces. The latter looks more reasonable since their implementation will be the same for the most part.

So far, the following classes have been identified:

1. `ChangeEventSourceCoordinator`
2. `RelationalSnapshotChangeEventSource`
3. `AbstractSnapshotChangeEventSource`

#### Example of a change in the `ChangeEventSourceCoordinator` logic

In partitioned mode, the coordinator, instead of just switching from snapshot to the streaming sources, will iterate over a collection of partitions/offsets and reuse each source for each of them:

**Nonpartitioned (current logic)**:

```java
// offset context is part of the source state
snapshotSource  = new SnapshotSource(offsetContext)
streamingSource = new StreamingSource()

// snapshot is executed for its only partition
result = snapshotSource.execute()

// streaming is done from the only partition/offset
streamingSource.execute(result.getOffset())
```

**Partitioned**:

```java
// offset context is part of the interface, not the state
snapshotSource  = new PartitionedSnapshotSource()
streamingSource = new PartitionedStreamingSource()

taskContex.forEach((partition, offset) -> {
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

### Move control loop from streaming sources to the coordinator

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

### Changes in mapping table identifiers to topic names and internal schema representation

In order to accomodate changes from multiple databases, instead of naming topics as `<server>.<schema>.<table>`, the connector will name them as `<server>.<database>.<schema>.<table>`.

The changes implemented in [DBZ-1089](https://issues.redhat.com/browse/DBZ-1089) should be partially reverted since for SQL Server both the database name and schema name should be taken into account when building schema.

### Changes in metrics

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

#### Implementation details:

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

#### Additional code changes

Some test helper methods like `TestHelper#waitForSnapshot` use metrics internally as the source of task status. They need to be reworked to expect the snapshot to be done on a specific partition (e.g. `TestHelper#waitForSnapshot(Partition partition)`).

#### Backward compatibility considerations

The current set of MBeans and their names are part of the connectors' public API. Backward compatibility with the current implementation can be provided in the following way:

1. Introduce a configuration parameter to enable the new metrics format. The default value is "disabled".
2. Pass the value of this parameter to all MBeans via a constructor. Depending on the value, the beans will register with the new or with the old names.
3. Automatically enable the new format if the connector is configured to capture changes from more than one database. This is necessary, because otherwise the connector won't be able to register multiple MBeans with the same name.

#### Grafana dashboards

The [example Grafana dashboard](https://github.com/debezium/debezium-examples/tree/master/monitoring) needs to be updated to support the new metrics. Since dashboard contains not only graphs but also gauges, it seems challenging to display the metrics from all tasks and partitions at once. The easiest and least breaking solution should be to add two more new dropdowns: Task and Partition. The dashboard will display the metrics of the selected partition and its task.

## TODO:

1. Define configuration for multiple databases to be processed by a connector. Only one of `database.dbname` and this one must be configured on a given connector.
2. Provide the upgrade path. The addition of the database name to the records' source partition will make it impossible for the upgraded connector to resume from the offset produced by the previous version. Likely, we'll need to keep the "legacy" version and let the consumers migrate their data and switch over.
