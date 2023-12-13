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
* Add support for running multiple connectors one Debezium engine (see also [this discussion](https://github.com/debezium/debezium-design-documents/pull/8#issuecomment-1836752998)).
* Add support for running sink connectors.

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
There will be two thread pools, the one for running tasks in parallel and one for processing CDC event pipelines.
In context of this document, CDC event pipelines means the chain of record transformations and eventually also record serialization and processing by user-provided `Consumer`.
Number of the worker thread in the task thread pool will be the same as the number of tasks.
Number of worker threads for CDC event pipelines will be specified either by the user via configuration or the default value will be used.
The default value will be the number of the cores of the underlying machine as running the tasks is not CPU intensive.

### Running source tasks in parallel

Kafka Connect `SourceTask` life cycle will be separated into a self-contained task.
These tasks will be executed in dedicated threads created by [`Executors.newSingleThreadExecutor()`](https://docs.oracle.com/en/java/javase/11/docs/api/java.base/java/util/concurrent/Executors.html#newSingleThreadExecutor()).
In case of `RetriableException` the task will be restarted.
In other cases, all other tasks will be stopped gracefully and the engine will fail with the task exception.

#### Storing offsets

Running tasks in parallel doesn't seem to be an issue for storing offsets.
Tasks are created with 1:1 mapping to connector's/DB's partitions (e.g. in case of SQL server each database is a separate partition and for each database will be created one task), so each task should read or write its own key in offset hash map.
Reading the offsets from multiple tasks therefore shouldn't introduce any concurrency issues and for writing [OffsetStorageWriter](https://github.com/apache/kafka/blob/3.6.0/connect/runtime/src/main/java/org/apache/kafka/connect/storage/OffsetStorageWriter.java#L65) is explicitly marked as thread-safe.

### Processing CDC events concurrently

Current `DebeziumEngine` API partially delegates event processing to the [`ChangeConsumer`](https://github.com/debezium/debezium/blob/v2.4.0.Final/debezium-api/src/main/java/io/debezium/engine/DebeziumEngine.java#L159) implementation, which is provided by the user.
More specifically, events are passed to the [`ChangeConsumer.handleBatch()`](https://github.com/debezium/debezium/blob/v2.4.0.Final/debezium-api/src/main/java/io/debezium/engine/DebeziumEngine.java#L167) in a batch manner, as a `List`.
This makes sense as in many cases events are submitted to another system and this is usually more efficient to do in a batch.
However, this prevents us from making a complete event processing pipeline, which would be run in a dedicated thread.

Before passing a batch of events to the `ChangeConsumer`, user-defined single message transformations are applied to the records.
In the case of [`ConvertingEngineBuilder`](https://github.com/debezium/debezium/blob/v2.4.0.Final/debezium-embedded/src/main/java/io/debezium/embedded/ConvertingEngineBuilder.java#L53), events are moreover serialized to `SourceRecords` before passing to the `ChangeConsumer`.
Both these tasks will be run in parallel in `ThreadPoolExecutor` mentioned above.
Before passing the batch to the `ChangeConsumer`, tasks for all records in the batch will be awaited to finish.

The implementation should also try to optimize record serialization/deserialization in case of the `ConvertingEngineBuilder`.
Currently, the records are serialized to `SourceRecords` before passing to the `ChangeConsumer` and then deserialized back when `ChangeConsumer` calls `RecordCommitter`.
In case when the record processing is delegated to the `ChangeConsumer` provided by the user, it seems there's no easy way to avoid this serialization/deserialization process without breaking the existing API.
One possible solution, though a little bit more complex, is outlined below.

On the other hand, in case of when processing of the records are done by a [`Consumer`](https://docs.oracle.com/en/java/javase/11/docs/api/java.base/java/util/function/Consumer.html) provided by the user, we can easily avoid deserialization step by storing original record.
The records will be passed to the chain of transformations, serialized, passed to provided consumer and if it succeeded, original (stored) record will be passed to the `RecordCommitter` without the need to deserialize it again.

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

#### Custom record processor

When the user wants to do more complex processing of the records, providing only a simple event `Consumer` is not sufficient and the user has to implement `ChangeConsumer`.
However, as mentioned above, this approach has a drawback that every record has to be deserialized back after processing to be able to commit it.
One possible solution is to give the user complete control over the record processing, including applying the chain of transformations and serializing the records.
This would of course mean exposing some `DebeziumEngine` internal object to the user implementation - namely the chain of transformations, serializer and executor service for eventual parallel processing.
Such generalization of `ChangeConsumer` can look like this:

```java
    /**
     * Generalization of {@link DebeziumEngine.ChangeConsumer}, giving complete control over the records processing.
     * Processor is initialized with all the required engine internals, like chain of transformations, to be able to implement whole record processing chain.
     * Implementations can provide e.g. serial or parallel processing of the change records.
     */
    @Incubating
    public interface RecordProcessor<R> {

        /**
         * Initialize the processor with object created and managed by {@link DebeziumEngine}, which are needed for records processing.
         *
         * @param recordService {@link ExecutorService} which allows to run processing of individual records in parallel
         * @param transformations chain of transformations to be applied on every individual record
         * @param serializer converter converting {@link SourceRecord} into desired format
         * @param committer implementation of {@link DebeziumEngine.RecordCommitter} responsible for committing individual records as well as batches
         */
        void initialize(final ExecutorService recordService, final Transformations transformations, final Function<SourceRecord, R> serializer,
                        final RecordCommitter committer);

        /**
         * Processes a batch of records provided by the source connector.
         * Implementations are assumed to use {@link DebeziumEngine.RecordCommitter} to appropriately commit individual records and the batch itself.
         *
         * @param records List of {@link SourceRecord} provided by the source connector to be processed.
         * @throws InterruptedException
         */
        void processRecords(final List<SourceRecord> records) throws InterruptedException;
    }
```

In current implementation this interface will be used only internally, but if there is a demand in the community for complete control over the record processing in the future, it can be later on exposed for implementation via SPI.

#### Options to process records in parallel

To sum up, following options for parallel processing will be provided to the user:

* Run the chain of transformations in parallel for every record, wait until the whole batch is transformed and pass the batch to the user-provided `ChangeConsumer`.
  This option will be selected if the user provides `ChangeConsumer` into the `Builder` and no converter is provided to the engine.
* Run the chain of transformations and serialize every record in parallel, wait until the whole batch is transformed and pass the batch to the user-provided `ChangeConsumer`.
  This option will be selected if the user provides `ChangeConsumer` into the `Builder` and  a converter is provided to the engine.
* Run the chain of transformations of the records in parallel.
  Await the results and apply user-provided `Consumer` on the transformed batch one by one in the same order as in the original batch.
  This option will be selected if the user provides `Consumer` into the `Builder` and no converter is provided to the engine.
* Run the chain of transformations and serialization of the records in parallel.
  Await the results and apply user-provided `Consumer` on the transformed batch one by one in the same order as in the original batch.
  This option will be selected if the user provides `Consumer` into the `Builder` and a converter is provided to the engine.
* Run the chain of transformations and consuming of the record by user-provided `Consumer` in parallel.
  This option will be selected if the user provides `Consumer` into the `Builder`, no converter is provided to the engine and the option `CONSUME_RECORDS_ASYNC` is set to `true`.
* Run the chain of transformations, serialization and consuming of the record by user-provided `Consumer` in parallel.
  This option will be selected if the user provides `Consumer` into the `Builder`, a converter is provided to the engine and the option `CONSUME_RECORDS_ASYNC` is set to `true`.
 
#### Storing offsets

Unlike parallel execution of tasks, in case of parallel processing of the records committing the right offset is crucial not to miss any event.
Assume we have a chain of events which are materialized by the source connector as records `R1->R2->R3` and we process them in parallel.
If the scheduler picks threads processing `R2` and `R3` first and thread processing  `R1` needs to wait, it may happen that e.g. processing of `R3` finished as the first and offset for `R3` is committed.
If the engine is shut down at this point, on the next start engine would start from `R3` and `R1`, eventually also `R2`, will be missed by the engine.
This would break at-least-once guarantee provided by Debezium.
Therefore we need always commit only the offset of the record whose all preceding records were already processed and committed.

Possible record processing flows are listed in the previous paragraph.
The first two options are trivial - processing of events is delegated to the user-provided `ChangeConsumer` and therefore also record committing is handled by it and correct order of commits is not engine responsibility.
Next two options when user-provided `Consumer` is run in serial manner are trivial as well - the `Consumer` is run in a sequence manner on the transformed batch and records are committed one by one as they are consumed by the `Consumer`.

Remaining two options run the whole chain in parallel.
In these cases record commit cannot be part of the chain, otherwise we can lose a record as described above.
Engine has to wait until the processing pipeline for the first event is executed and then commit the record.
Then it has to wait for the second record to be processed and so on, until the whole batch of events is processed.
This will ensure at-least-once delivery.
On the other hand, this can increase the number of duplicated records after engine restart, however, this is a drawback which the user has to accept if asynchronous record processing is required.

### Engine state and life cycle

The state of the engine will be described by the `AtomicReference<State> state` variable.
`State` enumeration will contain following elements:

* `STARTING` - the engine is being started, which mostly means engine object is being created or was already created, but `run()` method wasn't called yet,
* `INITIALIZING` - switch to this state at the very beginning of the `run()` method, engine is in this state during initializing of the connector, starting the connector itself and calling `DebeziumEngine.ConnectorCallback.connectorStarted()` callback,
* `CREATING_TASKS` - switch to this state after successful start of the connector, configurations of the tasks are being created and initialized,
* `STARING_TASKS` - tasks are being started, each in separate thread; stays in this stage until tasks are started, start of the tasks have failed or time specified by `TASK_MANAGEMENT_TIMEOUT_MS` option elapsed,
* `POLLING_TASKS` - tasks polling has started; this is the main phase when the data are produced and engine stays in this stage until it starts to shut down or exception was thrown,
* `STOPPING` - the engine is being stopped, either because engine's `close()` method was called or an exception was thrown; offsets are stored, `ExecutorService` for processing records shut down,  tasks and connector are stopped in this stage,
* `STOPPED` - engine has been stopped; final state, cannot move any further from this state and any call on engine object in this state should fail

Possible state transitions:

* `STARTING` -> `INITIALIZING`
* `INITIALIZING` -> `CREATING_TASKS`
* `CREATING_TASKS` -> `STARING_TASKS`
* `STARING_TASKS` -> `POLLING_TASKS`
* (`STARTING` | `INITIALIZING` | `CREATING_TASKS` | `STARING_TASKS` | `POLLING_TASKS`) -> `STOPPING` 
* `STOPPING`  -> `STOPPED` 

#### Preventing resource leakage

An engine stage that requires special attention is the one during which tasks are started.
At this stage connections the databases are being created and if something bad happens or the  engine is shut down while tasks are being started, it may result in various leaked resources, e.g. unclosed replication slots.
For more detail about possible issues, please see [DBZ-2534](https://issues.redhat.com/browse/DBZ-2534).
To prevent this situation, transition from `STARING_TASKS` into  `STOPPING`  won't be possible by calling engine's `close()` method.
Also, `STARING_TASKS` must finish completely.
Even if one of the threads fails to start the task it was running, the main (engine) thread has to wait for all other tasks until they finish (no matter if successfully or not) before moving into the `STOPPING` state.
In general, transition `STARING_TASKS` ->  `STOPPING` is possible, but it should happen only in the case when an exception was thrown from method starting tasks and only until all threads starting tasks have finished.

#### Exception handling

Retriable exceptions are handled at the place where they happen and relevant action is retried until `ERRORS_MAX_RETRIES` attempts are exhausted.
Contrary to the existing `EmebeddedEngine` implementation, task is not restarted at this point (**TODO**: *re-think, why is the task restarted in the current implementation?*).
After that the exception is propagated up to the stack.
Any exception which is not caught for retrying is propagated further.
All exceptions should be handled at the one place - in the `catch` block of the engine `run()` method.
Once any exception is hit, the engine should enter `STOPPING` state and start with engine shut down.

#### Exiting from task polling loop

Task exists polling loop in following cases:

* by changing engine state to any other state than `POLLING_TASKS` (the only possibility is to change to `STOPPING` state)
* throwing an exception either from task's `poll()` method or during processing of the batch of the records
* indirectly by shutting down the `ExecutorService` running the processing of the records when engine is about to shut down, which would result in throwing exception if another record is submitted for processing (however, this shouldn't happen as the thread should noticed that the engine state has changed beforehand - before processing next batch)

Exiting from the task polling loop should happen in reasonable quick time once the current batch is processed.
When the engine shut down is called, `ExecutorService` running the processing of the records is gracefully shut down.
This means that records which are currently being processed are awaited to be processed, but no other new records are accepted for processing, even if they are already scheduled.
Main thread waits for `ExecutorService` to shut down (i.e. to process submitted records) maximum `POLLING_SHUTDOWN_TIMEOUT_MS` ms, then the `ExecutorService` is shut down abruptly.
Therefore  immediate shutdown without waiting for the records to be processed can be achieved by setting `POLLING_SHUTDOWN_TIMEOUT_MS` to zero.
Adding a dedicated method for immediate shutdown would require adding a new public method which is not part of `DebeziumEngine` API and there doesn't seem to be a need for it as `POLLING_SHUTDOWN_TIMEOUT_MS` can be set to reasonable small value.
It can be added in the future if there is user demand for it.

#### Engine shut down

During the engine shut down all tasks should be stopped if the engine reached at least `STARING_TASKS` state.
Before calling task shut down, shutdown of `ExecutorService` used for processing CDC records is called and awaited.
Each task should also commit an offset before its shutdown is called and the engine waits for tasks to stop.
Users can set the `TASK_MANAGEMENT_TIMEOUT_MS` option (which is used also for start of the tasks) to adjust waiting time for the shutdown of the tasks.
Once shutdown of all tasks finishes, stop of the connector follows.
Connector should always be stopped, no matter what the previous engine state was.
Engine reaches `STOPPED` state and no other action should be possible.
If the user wants to start the engine again, the engine object has to be re-created.

### Auxiliary interfaces and objects

To reduce the number of parameters which need to be passed to various engine methods and objects, it's convenient to create several auxiliary objects, namely connector and task contexts.
Contexts should hold references to long lived objects, typically created during creation of engine, connector or task, like e.g. `OffsetStorageReader` or `OffsetStorageWriter`.
Auxiliary `DebeziumSourceConnector` and `DebeziumSourceTask` would hold these contexts.
As in long-term we would like to decouple the Debezium engine from Kafka Connect API, this may serve as the first step to this direction and these objects can serve as a replacements for Kafka Connect objects.
As mentioned at the beginning, we cannot directly replace these objects by our implementation.
Therefore these objects would also contain references to Kafka Connect connector and tasks objects, respectively, and provide Connect objects when needed.

These interfaces are highly experimental and are assumed to be subject of future changes or removed completely.
The main purpose is to explore if this direction of how to gradually remove from Kafka Connect objects is a viable way or not.

Proposed auxiliary interfaces should be as follows:

```java
@Incubating
public interface DebeziumSourceConnector {

    /**
     * Returns the {@link DebeziumSourceConnectorContext} for this DebeziumSourceConnector.
     * @return the DebeziumSourceConnectorContext for this connector
     */
    DebeziumSourceConnectorContext context();

    /**
     * Initialize the connector with its {@link DebeziumSourceConnectorContext} context.
     * @param context {@link DebeziumSourceConnectorContext} containing references to auxiliary objects.
     */
    void initialize(DebeziumSourceConnectorContext context);
}
```

```java
@Incubating
public interface DebeziumSourceConnectorContext {

    /**
     * Returns the {@link OffsetStorageReader} for this DebeziumConnectorContext.
     * @return the OffsetStorageReader for this connector.
     */
    OffsetStorageReader offsetStorageReader();

    /**
     * Returns the {@link OffsetStorageWriter} for this DebeziumConnectorContext.
     * @return the OffsetStorageWriter for this connector.
     */
    OffsetStorageWriter offsetStorageWriter();
}
```

```java
@Incubating
public interface DebeziumSourceTask {
    /**
     * Returns the {@link DebeziumSourceTaskContext} for this DebeziumSourceTask.
     * @return the DebeziumSourceTaskContext for this task
     */
    DebeziumSourceTaskContext context();
}
```

```java
@Incubating
public interface DebeziumSourceTaskContext {
    /**
     * Gets the configuration with which the task has been started.
     */
    Map<String, String> config();

    /**
     * Gets the {@link OffsetStorageReader} for this SourceTask.
     */
    OffsetStorageReader offsetStorageReader();

    /**
     * Gets the {@link OffsetStorageWriter} for this SourceTask.
     */
    OffsetStorageWriter offsetStorageWriter();

    /**
     * Gets the {@link OffsetCommitPolicy} for this task.
     */
    OffsetCommitPolicy offsetCommitPolicy();

    /**
     * Gets the {@link Clock} which should be used with {@link OffsetCommitPolicy} for this task.
     */
    Clock clock();

    /**
     * Gets the transformations which the task should apply to source events before passing them to the consumer.
     */
    Transformations transformations();
}
```

### Other future-proof changes

Switching to virtual threads should be straightforward, just by switching to appropriate `ExecutorService`, e.g. by using `Executors.newVirtualThreadPerTaskExecutor()` instead of `Executors.newFixedThreadPool()`.
Switching to structured concurrency should be almost as easy as switching to virtual threads.

Separating `SourceTask`s into self-contained tasks and running them in parallel in different threads should form a firm ground for executing them on a different machine via `gRPC`.
The main question is how to exchange objects need to run the tasks on the remote machines and the required objects can be complicated and even not known in advance (e.g. user-provided `ChangeConsumer`), so it may be hard or even impossible to provide protobuf representation for it.
Protobuf in version 3 provides [support for maps](https://protobuf.dev/programming-guides/proto3/#maps), which can be used easily for passing all the configuration options to the remote machine.
So as of now, the most simple method seems to provide several modes in this engine that can run, one of them being "task-executor", which will defer the initialization and start and will do it via gRPC once it obtains task configuration from the engine.
In this case the engine would act only as an orchestrator of other nodes running the tasks.
This would require smaller refactoring, mainly of the engine's `run()` method implementation, but given that implementing this would possibly require separate DDD, this seems to be acceptable for now.
Proof of concept should be done as part of the implementation or as a follow-up task.
PoC may reveal some weak points and possible future changes.

So far, there are no particular requests for the Debezium operator or the UI.
Proper separation of the functionality into fine-grained functions should however make exposing any engine functionality to external service smooth and easy.

As per [Quarkus extension guide](https://quarkus.io/guides/writing-extensions), the `EmbeddedEngine` should be usable for Quarkus extension even with current implementation.
New implementation should allow seamless integration with Quarkus as well.
Similar to gRPC, a PoC should be done as a follow-up task.

### Testing

Testsuite will be changed to use only the `DebeziumEngine` API.
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

As one of the main drivers behind this effort is performance, an important part of this effort will be development of performance tests for `DebeziumEngine`.
There should be two kinds of tests - [JMH](https://github.com/openjdk/jmh) benchmarks and more robust end-to-end performance tests.
JMH benchmarks can mimic [debezium-microbenchmark-oracle](https://github.com/debezium/debezium/blob/v2.4.0.Final/debezium-microbenchmark-oracle/src/main/java/io/debezium/performance/connector/oracle/EndToEndPerf.java) JMH tests, possibly with using [SimpleSourceConnector](https://github.com/debezium/debezium/blob/v2.4.0.Final/debezium-embedded/src/main/java/io/debezium/connector/simple/SimpleSourceConnector.java) or some modification of it.
End-to-end performance tests should include streaming the data from at least one database, possibly PostgreSQL, and streaming the data to "`/dev/null`" consumer to minimize the impact of the sink consumer.
Data can be possibly generated by the tools developed under [Debezium-performance](https://github.com/Debezium-performance/).

