# DDD-7: Asynchronous Debezium Embedded Engine

## Motivation

Debezium comes in two basic flavors.
One flavor is  [Kafka Connect](https://docs.confluent.io/platform/current/connect/index.html) source connectors.
The other is an independent engine, which can be embedded into a user application or is wrapped in the [Debezium server](https://debezium.io/documentation/reference/2.4/operations/debezium-server.html), which is a standalone application.
While in the first case the connector life cycle and execution of connector tasks are managed by Kafka Connect, in the later case the task life cycle is managed purely by the Debezium engine itself and the Debezium project has full control how the tasks within the engine are executed.
Current implementation of the [`DebeziumEngine` interface](https://github.com/debezium/debezium/blob/v2.4.0.Final/debezium-api/src/main/java/io/debezium/engine/DebeziumEngine.java), [`EmebeddedEngine`](https://github.com/debezium/debezium/blob/v2.4.0.Final/debezium-embedded/src/main/java/io/debezium/embedded/EmbeddedEngine.java#L91), executes all the steps in a serial manner.
This includes executing event transformations as well as event serialization.
Event serialization is not provided directly by `EmebddedEngine`.
It's provided by its extension, [`ConvertingEngine`](https://github.com/debezium/debezium/blob/v2.4.0.Final/debezium-embedded/src/main/java/io/debezium/embedded/ConvertingEngineBuilder.java#L169), which implements it as part of [event processing](https://github.com/debezium/debezium/blob/v2.4.0.Final/debezium-embedded/src/main/java/io/debezium/embedded/ConvertingEngineBuilder.java#L102).

Moreover, current `EmebddedEngine` implementation doesn't support executing multiple source tasks, even in case the source connector supports it, such e.g. [SQL server connector](https://debezium.io/documentation/reference/2.4/connectors/sqlserver.html#sqlserver-property-tasks-max).
Only the [first task is executed](https://github.com/debezium/debezium/blob/v2.4.0.Final/debezium-embedded/src/main/java/io/debezium/embedded/EmbeddedEngine.java#L918), no matter how many tasks connector configuration provides.

In the era of large data sets and multicore servers, using a single thread for processing all the CDC events from the database is a clear limiting factor.
Providing a new implementation of `DebeziumEngine` interface which would run some of the tasks in parallel can give significant performance boost.
New implementation and related changes should also aim for a good test coverage and ease of testing of the new implementation as well as any other future implementations.

## Goals

Provide new implementation of [`DebeziumEngine` interface](https://github.com/debezium/debezium/blob/v2.4.0.Final/debezium-api/src/main/java/io/debezium/engine/DebeziumEngine.java) which would

* allow to run multiple [source tasks](https://github.com/apache/kafka/blob/3.6.0/connect/api/src/main/java/org/apache/kafka/connect/source/SourceTask.java) for given connector if the connector provides multiple tasks
* run potentially time-consuming code (e.g. event transformation or serialization) in the dedicated threads
* allow possible further speedup by optionally disabling total ordering of the messages
* be well-prepared for future changes and new features, namely
  * switching to [virtual threads](https://docs.oracle.com/en/java/javase/21/core/virtual-threads.html)
  * eventually delegate source tasks to external workers via [gRPC](https://grpc.io/) calls (especially to be able to horizontally scale connector load to multiple pods in Kubernetes cluster)
  * better integration with Debezium k8s operator and UI
  * providing Debezium engine as a [Quarkus](https://quarkus.io/) extension

Another high-level goal is to adjust the current Debezium testsuite to use the `DebeziumEngine` interface instead of hard-coded `EmebddedEngine` implementation.
This should allow for easy switching to any other implementation of `DebeziumEngine` and thus making new implementation to be easily tested with the current testsuite.

## Non-goals

* Change [`DebeziumEngine`](https://github.com/debezium/debezium/blob/v2.4.0.Final/debezium-api/src/main/java/io/debezium/engine/DebeziumEngine.java) interface.
* Do any other changes in the Debezium [API package](https://github.com/debezium/debezium/tree/v2.4.0.Final/debezium-api).
* Implement any parallelization inside connectors (e.g. tracking database CDC changes in multiple threads).
* Remove dependency on Kafka Connect API.

### Preserving Kafka Connect model

As the main goal of the Debezium engine is to be able to execute Debezium outside of Kafka, it may be strange why not to take this opportunity to get rid of Kafka dependencies.
The reason is simple: it would be a too complex change which would not impact only the Debezium engine.
E.g. removing [`WorkerConfig`](https://github.com/apache/kafka/blob/3.6.0/connect/runtime/src/main/java/org/apache/kafka/connect/runtime/WorkerConfig.java) would require removing [`OffsetBackingStore`](https://github.com/apache/kafka/blob/3.6.0/connect/runtime/src/main/java/org/apache/kafka/connect/storage/OffsetBackingStore.java),
which would require removing [`OffsetStorageReader`](https://github.com/apache/kafka/blob/3.6.0/connect/api/src/main/java/org/apache/kafka/connect/storage/OffsetStorageReader.java) etc., etc., resulting in substantial changes in the Debezium core and connectors.
Therefore, this should be done in a separate task which would deserve a dedicated DDD describing all the changes and proposing replacements for Kafka Connect interfaces and classes.

## Proposed changes

### Thread pools

Implementation of concurrent processing will be based on the Java [Executors](https://docs.oracle.com/en/java/javase/11/docs/api/java.base/java/util/concurrent/Executors.html) framework.
As the Debezium is currently based on Java 11, new concurrency features introduced by the project Loom, namely virtual threads and structured concurrency, cannot be used now.
However, it's expected to switch to virtual threads in the future, once Debezium will be based on Java 21 or higher.
[`ThreadPoolExecutor`](https://docs.oracle.com/en/java/javase/11/docs/api/java.base/java/util/concurrent/ThreadPoolExecutor.html) will be used for the creating and pooling of the threads.
The `ThreadPoolExecutor` will be created by factory method [`Executors.newFixedThreadPool​(int nThreads)`](https://docs.oracle.com/en/java/javase/11/docs/api/java.base/java/util/concurrent/Executors.html#newFixedThreadPool(int)).
Number of worker threads will be specified either by the user via configuration or the default value will be used.
The default value will be the number of the cores of the underlying machine.
There will be only one thread pool, the one for processing CDC event pipelines.
`SourceTasks`s, which will be also run in separate threads, start and stop with the engine and therefore don't need any thread pooling.

### Running source tasks in parallel

Kafka Connect `SourceTask` life cycle will be separated into a self-contained task.
These tasks will be executed in dedicated threads created by [`Executors.newSingleThreadExecutor()`](https://docs.oracle.com/en/java/javase/11/docs/api/java.base/java/util/concurrent/Executors.html#newSingleThreadExecutor()).
In case of `RetriableException` the task will be restarted.
In other cases, all other tasks will be stopped gracefully and the engine will fail with the task exception.

### Processing CDC events concurrently

Current `DebeziumEngine` API partially delegates event processing to the [`ChangeConsumer`](https://github.com/debezium/debezium/blob/v2.4.0.Final/debezium-api/src/main/java/io/debezium/engine/DebeziumEngine.java#L159) implementation, which is provided by the user.
More specifically, events are passed to the [`ChangeConsumer#handleBatch()`](https://github.com/debezium/debezium/blob/v2.4.0.Final/debezium-api/src/main/java/io/debezium/engine/DebeziumEngine.java#L167) in a batch manner, as a `List`.
This makes sense as in many cases events are submitted to another system and this is usually more efficient to do in a batch.
However, this prevents us from making a complete event processing pipeline, which would be run in a dedicated thread.

Before passing a batch of events to the `ChangeConsumer`, user defined single message transformations are applied to the records.
In the case of [`ConvertingEngineBuilder`](https://github.com/debezium/debezium/blob/v2.4.0.Final/debezium-embedded/src/main/java/io/debezium/embedded/ConvertingEngineBuilder.java#L53), events are moreover serialized to `SourceRecords` before passing to the `ChangeConsumer`.
Both these tasks will be run in parallel in `ThreadPoolExecutor` mentioned above.
Before passing the batch to the `ChangeConsumer`, tasks for all records in the batch will be awaited to finish.

The implementation should also try to optimize record serialization/deserialization in case of the `ConvertingEngineBuilder`.
Currently, the records are serialized to `SourceRecords` before passing to the `ChangeConsumer` and then deserialized back when `ChangeConsumer` calls `RecordCommitter`.
TBD task is to find a way how to avoid record deserialization during commit without breaking existing API.

#### Disabling total ordering of the CDC events

Performance-wise, further speedup can be achieved by skipping message ordering and delivering messages in order they are ready to go.
This makes sense in scenarios when the message order is not important (e.g. scenarios when the underlying database receives only inserts) or the ordering is done on the receiver side by application consuming CDC events.

While not very often used, `DebeziumEngine` also provides other means of how the user can handle change records.
Instead of implementing `ChangeConsumer`, the user can provide only a [`Consumer`](https://docs.oracle.com/en/java/javase/11/docs/api/java.base/java/util/function/Consumer.html) function, which would handle the records.
In such a case we can create a complete pipeline for handling CDC records and moreover we don't have to pass the records to the user implementation in batches.
This allows us to process each message individually and if processing one record takes a longer time, e.g. because of the record size, other records don't need to be blocked.
This would result in breaking the total order of messages.
However, as mentioned above, in some specific scenarios this may make sense and would be desirable.

New implementation should provide an option to disable total ordering of records.
This would be allowed only in case when the user doesn't provide record handling via `ChangeConsumer`.
When the user doesn't provide record handling via `ChangeConsumer` and total ordering or records is enabled (it would be the default), pipelines for processing CDC records will run in separate threads, but the implementation has to ensure that the total ordering of the records will be preserved.  

### Other future-proof changes

Switching to virtual threads should be straightforward, just by switching to appropriate `ExecutorService`, e.g. by using `Executors.newVirtualThreadPerTaskExecutor()` instead of `Executors.newFixedThreadPool()`.
Switching to structured concurrency should be almost as easy as switching to virtual threads.

Separating `SourceTask`s into self-contained tasks and running them in parallel in different threads should form a firm ground for executing them on a different machine via `gRPC`.
It's assumed that no other changes will be needed in this regard.
Proof of concept will be done as part of the implementation or as a follow-up task.
PoC may reveal some weak points and possible future changes.

So far, there are no particular requests for the Debezium operator or the UI.
Proper separation of the functionality into fine-grained functions should however make exposing any engine functionality to external service smooth and easy.

As per [Quarkus extension guide](https://quarkus.io/guides/writing-extensions), the `EmbeddedEngine` should be usable for Quarkus extension even with current implementation.
New implementation should allow seamless integration with Quarkus as well.
Similar to gRPC, a PoC should be done as a follow-up task.

### Testing

Testsuite will be changed to use only `DebeziumEngine` API.
Most of the tests which use `DebeziumEngine` inherit from `AbstractConnectorTest`, where a `DebeziumEngine` instance is created.
`AbstractConnectorTest` will contain a protected method which will be responsible for creating `DebeziumEngine`.
When switching to the new `DebeziumEngine` implementation, the switching will be done by adjusting only this single method.
This would also allow changing the engine implementation in the specific tests if needed.

In the future, if there is any such need, the testsuite can be parameterized with `DebeziumEngine` implementation as a parameter.
This would allow us to run the testsuite against multiple engine implementations.
However, in the near future we don't expect there will be any such need, so for now a dedicated method within `AbstractConnectorTest`  should be sufficient.

As the existing testsuite is based on `EmbeddedEngine` implementation, which provides richer API than the `DebeziumEngine` interface, it's not possible to use  `DebeziumEngine` API exclusively in the whole testsuite.
It's possible to use `DebeziumEngine` API in most of the cases or introduce some helper methods instead, except one case - `EmbeddedEngine#runWithTask()`.
It exposes the engine Kafka Connect `SourceTask` for the testing.
If we get rid of this method, we would lose some important tests.
To preserve the ability to test the engine source task and at the same time not break encapsulation of `DebeziumEngine`, a new interface `TestingDebeziumEngine` will be introduced.
It will belong to the test package of the `debezium-embedded` module.
The interface should contain only one method, `runWithTask(Consumer<SourceTask> consumer)`:

```java
public interface TestingDebeziumEngine<T> extends DebeziumEngine<T> {
    /**
     * Run consumer function with engine task, e.g. in case of Kafka with {@link SourceTask}.
     * Effectively expose engine internal task for testing.
     */
    void runWithTask(Consumer<SourceTask> consumer);
}
```

In the future, we may add more methods to this interface if it would be beneficial from the testing point of view to expose any other `DebeziumEngine` internals or add convenient methods for testing.
Number of such methods should be, however, kept as minimal as possible and methods should be added only for a very good reason, as the interface will force `DebeziumEngine` implementations to implement all these methods as well if the implementation should be tested with the Debezium testsuite.
