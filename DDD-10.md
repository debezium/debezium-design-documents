# DBZ-8968: Add support for pluggable ArrayDeque

## Motivation

Debezium (DBZ) connectors use an in-memory queue (ArrayDeque) within the ChangeEventQueue class to buffer changes between the database reader and the Kafka Connect task. When the Connect task (Eg: IcebergConsumer) is slow to process events, the in-memory queue can become full, causing the database reader to slow down due to backpressure.

For example, The Postgres Connector is single-threaded and memory-bound, which limits throughput. 
In snapshot phase, DB reads can be parallelized using the `snapshot.fetch.threads` config. However, the benefits are limited without a fast and efficient writer because of limited internal queue capacity.

This behavior introduces limitations, including:
1. Memory pressure and frequent garbage collection
2. OOM risks under high load (max.queue.size.in.bytes introduces additional overhead)
3. Need for vertical scaling (large heap sizes)
4. Limited throughput for memory bounded connectors (Eg: PostgreSQL)

## Goals

Introduce a pluggable queue abstraction that allows custom implementations of event buffering for
1. Cost efficiency : Small instances can be used to host the connectors.
2. High throughput:  When embedded persistent queue is used, local disc I/O (including serialization and deserialization) is faster than repeated reads from the remote database
3. Increased overall connector resilience.

## Requirements

1. Pluggable Queue Implementation
   * Ability for users to supply their own queue implementation by implementing a defined interface.
   * The connector must dynamically load this implementation via configuration.
2. Queue specific configuration
   * Support additional implementation-specific configuration options (e.g., max size, disk path, thresholds).
   * These options should be passed to the custom queue via configuration.
3. Queue metrics exposure
   * All queue implementations must support emitting basic metrics such as: Queue size, Capacity, Read/write rate etc.
   * Metrics must integrate with the existing Debezium metrics framework for observability.

## Design Options:

Two architectural approaches to support <b>pluggable queue</b> implementations without Queue metrics exposure:
1. Define a Standard ChangeEventQueue Interface:

    Abstract the <i>ChangeEventQueue</i> itself behind a new interface. This allows multiple implementations optimized for different storage or performance characteristics (e.g., in-memory, disk-backed, off-heap). The existing logic would become the DefaultChangeEventQueue.

2. Introduce a Queue Wrapper Layer
   
    Introduce a wrapper around the queue and integrate it within the existing <i>ChangeEventQueue</i>.

## Proposed changes

Option 1 is further broken down into following sub-tasks:
* Define a <b>ChangeEventQueue</b> interface and implement a <b>DefaultChangeEventQueue</b> to replace the existing implementation.
* Add support for custom configuration of <b>ChangeEventQueue</b> implementations.
* Extend the connector configuration to enable selection of a specific <b>ChangeEventQueue</b> implementation at runtime.

### Task 1: DefaultChangeEventQueue implementation:
Refer to https://github.com/debezium/debezium/pull/6468 for the proposed changes.

### Task 2: Custom configuration for Queue implementations:

* Add Has-A relationship between <b>ChangeEventQueueContext</b> and <b>CommonConnectorConfig</b>
* Add getter method to get the queue implementation specific config.
* Queue implementation use custom config from above step.

```java
class ChangeEventQueueContext {
 // … existing fields
  private CommonConnectorConfig config;
 // add builder method for config
}


class CommonConnectorConfig {
  // … existing code
  private Map<String, String> changeEventQueueConfig;

  // set changeEventQueueConfig in constructor
  
  public Map<String, String> GetChangeEventQueueConfig() {
  	return changeEventQueueConfig;
  }
}


class CustomQueue extends AbstractChangeEventQueue {
  // define config
   public CustomQueue(ChangeEventQueueContext context) {
   // get configs by using  context.getConfig().GetChangeEventQueueConfig()
  }
}
```

### Task 3: Connector Configuration to select the Queue implementation:

Introduce the following new configuration parameters:
- `connector.queue`: Specifies the fully qualified class name of the ChangeEventQueue implementation.
- `connector.queue.*`: A namespace for custom configuration properties specific to the selected queue implementation.

```java

public ChangeEventQueue<DataChangeEvent> getChangeEventQueue(Supplier<LoggingContext.PreviousContext> loggingContextSupplier, boolean buffering) {
   String changeEventQueueImplClass = config.getString(CommonConnectorConfig.CHANGE_EVENT_QUEUE);
   ChangeEventQueueContext changeEventQueueContext = ChangeEventQueueContext.builder()
           .pollInterval(config.getPollInterval())
           .maxBatchSize(config.getMaxBatchSize())
           .maxQueueSize(config.getMaxQueueSize())
           .maxQueueSizeInBytes(config.getMaxQueueSizeInBytes())
           .loggingContextSupplier(loggingContextSupplier)
           .buffering(buffering)
           .build();

   return (ChangeEventQueue<DataChangeEvent>) Configuration.getInstanceWithProvidedConstructorType(changeEventQueueImplClass, ChangeEventQueueContext.class, ChangeEventQueueContext);
}

```