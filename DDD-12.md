# Quarkus Debezium Extension

Quarkus Debezium Extension should provide a simple way to receive data source events inside a Quarkus Native Application and apply some logic to them (see image) thanks to the engine and an appropriate connector.

![](./DDD-12/dbz-emb.png)

In order to be able to receive those events from the data-source, the actual way to achieve it is to import the `debezium-engine` and a connector like in this way:

```xml
<dependency>
    <groupId>io.debezium</groupId>
    <artifactId>debezium-embedded</artifactId>
    <version>${version.debezium}</version>
</dependency>
<dependency>
    <groupId>io.debezium</groupId>
    <artifactId>debezium-connector-mysql</artifactId>
    <version>${version.debezium}</version>
</dependency>
```

This approach doesn't work out-of-the-box in situations in which [we want to build a native image of the application](https://debezium.io/blog/2025/03/12/superfast-debezium/).

## 2. Module Organization (Debezium quarkus engine with Quarkus connector)

The module proposed contains the `engine` and the `connector` like in this way:

```xml
<dependency>
    <groupId>io.debezium</groupId>
    <artifactId>debezium-quarkus-mysql</artifactId>
    <version>${version.debezium}</version>
</dependency>
```

With this solution, the configuration property delegated to define the connector class should be unavailable and already defined inside the extension:

```txt
connector.class=io.debezium.connector.mysql.MySqlConnector
```

![](./DDD-12/s2-dbz-lib.png)

## Quarkus Debezium Extension configuration

The extensions must be configurable using the properties and yaml like any Quarkus application. The configuration properties available for the debezium engine must be available using a prefix `quarkus.debezium.xxx` like:

```properties
quarkus.debezium.configuration.offset.storage=org.apache.kafka.connect.storage.MemoryOffsetBackingStore
quarkus.debezium.configuration.database.hostname=localhost
quarkus.debezium.configuration.database.port=5432
quarkus.debezium.configuration.database.user=postgresuser
quarkus.debezium.configuration.database.password=postgrespw
quarkus.debezium.configuration.database.dbname=postgresuser
quarkus.debezium.configuration.snapshot.mode=never
```

Apart from the usual configuration properties like data source addresses, Debezium traditionally follows a _configuration over code_ approach, defining certain behavioral aspects using external configuration files interpreted at runtime. However, this approach changes in the Quarkus extension, which favors _code over configuration_—or more specifically, _annotation over configuration_. In this model, some features of the Debezium Engine are exposed through annotations, making Debezium instrumentation more expressive and developer-friendly.

## Quarkus Debezium Extension DI

Debezium internally use the `ServiceRegistry` to inject and manage object lifecycle thanks to `ServiceLoader` mechanism. Quarkus includes a lightweight CDI implementation called `ArC` which can be used to manage the classes loaded through the `ServiceLoader`.

## Quarkus Debezium Extension additional feature

The extension permits to address some use-cases already present in Debezium but in a _Quarkus_ way:

- [1.Debezium Engine Lifecycle events](#quarkus-debezium-lifecycle-events)
- [2.Debezium Heartbeat events](#quarkus-debezium-heartbeats-events)
- [3.Debezium Listener](#quarkus-debezium-listener)
- [4.Debezium Custom Data Converter](#custom-debezium-data-converter)
- [5.Debezium SchemaChange Listener](#quarkus-debezium-schemachange-listener)
- [6.Debezium Notification Handler](#quarkus-debezium-notification-handler)
- [7.Debezium PostProcess Consumer](#quarkus-debezium-postprocessor)
- [8.Debezium Custom Converter](#quarkus-debezium-custom-converter)

### Quarkus Debezium Lifecycle Events

We can summarize the lifecycle of a Debezium Embedded Engine in the follows steps:

| Phase            | description                                                           | code                                  |
|------------------|-----------------------------------------------------------------------|---------------------------------------|
| *initialization* | configuration is built and the engine is created, but not yet running | after the configuration is validated  |
| *startup*        | connectors are initialized, DB connection established                 | right after `engine.run()` is invoked |
| *shutdown*       | engine terminates (graceful or with error)                            | observable via `CompletitionCallback` |

The Quarkus Debezium extension allows you to be notified of the engine's state using the following annotation:

```java
import io.debezium.engine.source;
import jakarta.enterprise.context.ApplicationScoped;


@ApplicationScoped
class DebeziumEngineLifeCycle {
    
    @DebeziumEngineInit()
    public void init() {
        /// some logic to apply
    }

    @DebeziumEngineStartup()
    public void startup(DebeziumSourceConnectorContext context) {
        /// some logic to apply 
    }

    @DebeziumEngineShutdown()
    public void shutdown(Status status) {
        /// some logic to apply
    }
}
```

### Quarkus Debezium Heartbeats events

In Debezium, heartbeat events are lightweight, periodic messages emitted when no database changes occur. They confirm that the connector is alive and still connected to the source, helping to detect liveness. In the Quarkus Debezium Extension can detect heartbeats events in this way:

```java
import io.debezium.engine.ChangeEvent;
import jakarta.enterprise.context.ApplicationScoped;  


@ApplicationScoped  
class HeartbeatListener {
  
    @DebeziumHeartbeat()  
    public void heartbeat(ChangeEvent<String, String> event) {  
        /// some logic to apply 
    }  
}
```

it's possible to define the `heartbeat.action.query` and `heartbeat.interval.ms` interval in case of relational databases:

```java
import io.debezium.engine.ChangeEvent;
import jakarta.enterprise.context.ApplicationScoped;  


@ApplicationScoped  
class HeartbeatListener {
  
    @DebeziumHeartbeat(query="SELECT now()", interval=10000)  
    public void heartbeat(ChangeEvent<String, String> event) {  
        /// some logic to apply 
    }  
}
```

### Quarkus Debezium Listener

a Quarkus Developer using a `Debezium Listener`  can intercept events (`INSERT, UPDATE, DELETE...`) from a table like `order`, with a simple annotation like:

```java
import io.debezium.engine.ChangeEvent;
import jakarta.enterprise.context.ApplicationScoped;  


@ApplicationScoped  
class OrderListener {
  
    @DebeziumListener("order")  
    public void listener(ChangeEvent<String, String> event) {  
        /// some logic to apply 
    }  
}
```

or in batch:

```java
import io.debezium.engine.ChangeEvent;
import jakarta.enterprise.context.ApplicationScoped;  


@ApplicationScoped  
class OrderListener {
  
    @DebeziumBatchListener("order")  
    public void listener(List<ChangeEvent<String, String>> events) {  
        /// some logic to apply
    }  
}
```

even listen only a certain type of event (using for example the configuration `skipped.operations`)

```java
import io.debezium.engine.InsertEvent;
import jakarta.enterprise.context.ApplicationScoped;  
import io.debezium.engine.quarkus.Operation.INSERT;

@ApplicationScoped  
class OrderListener {
  
    @DebeziumBatchListener("order", INSERT)  
    public void listener(List<InsertEvent<String, String>> events) {  
        /// some logic to apply
    }  
}
```

### Custom Debezium Data Converter

It should be possible to receive events mapped as data classes like:

```java
public record Order(long id, String name, int price) {}
```

```java
import io.debezium.engine.InsertEvent;
import jakarta.enterprise.context.ApplicationScoped;

@ApplicationScoped  
class OrderListener {  
  
    @DebeziumListener("order")  
    public void listener(InsertEvent<String, Order> event) {  
        /// some logic to apply
    }  
}
```

using something similar for the [quarkus kafka library](https://quarkus.io/guides/kafka#jackson-serialization):

```java
package com.acme.order.jackson;

import io.quarkus.debezium.client.serialization.ObjectMapperDeserializer;

public class OrderDeserializer extends ObjectMapperDeserializer<Order> {
    public OrderDeserializer() {
        super(Order.class);
    }
}
```

```properties
quarkus.debezium.deserializer=com.acme.order.jackson.OrderDeserializer
```

### Quarkus Debezium SchemaChange Listener
Debezium automatically detects and captures schema changes in the source database, such as adding or removing columns, modifying data types, or altering primary keys. These changes are parsed from the database's DDL statements and used to update Debezium's internal schema history, ensuring that change events reflect the current table structure. The Quarkus extension can expose a listener to such kind of event:

```java
import io.debezium.engine.InsertEvent;
import jakarta.enterprise.context.ApplicationScoped;

@ApplicationScoped  
class SchemaChangeListener {  
  
    @DebeziumSchemaChangeListener()  
    public void listener(SchemaChangeEvent event) {  
        /// some logic to apply
    }  
}
```

### Quarkus Debezium Notification Handler
Debezium [notifications](https://debezium.io/documentation/reference/3.1/configuration/notification.html) provide a mechanism to obtain status information about the connector. It should be possible to receive notifications in the quarkus extension like:

```java
import io.debezium.engine.InsertEvent;
import jakarta.enterprise.context.ApplicationScoped;

@ApplicationScoped  
class SnapshotListener {  
  
    @DebeziumNotificationHandler()  
    public void handler(Notification notification) {
        /// some logic to apply
    }
    
}
```

### Quarkus Debezium PostProcessor

In Debezium Embedded Engine, a PostProcessor is an internal hook used to modify or transform events right before they are emitted to the application’s `.notifying()` callback. In the quarkus extension can be accessible by:

```java
import io.debezium.engine.InsertEvent;
import jakarta.enterprise.context.ApplicationScoped;

@ApplicationScoped  
class CustomPostProcessor {  
  
    @DebeziumPostProcessor()  
    public void process(Object key, Struct value) {
        /// some logic to apply
    }
    
}
```

### Quarkus Debezium Custom Converter
A custom converter in Debezium is a way to transform specific column values as they're read from the source database before they're emitted in the change event.


#### Quarkus Debezium Relational Converter

```java
import io.debezium.engine.InsertEvent;
import jakarta.enterprise.context.ApplicationScoped;

@ApplicationScoped
class RawToStringConverter {

    @DebeziumRelationalConverter(typeName="RAW")
    public ConverterDefinition<SchemaBuilder> bind(RelationalColumn raw) { 
        return new ConverterDefinition<>(SchemaBuilder.string(), Object::toString);
    }
    
}
```

*example based on a simplified [RawToStringConverter](https://github.com/debezium/debezium/blob/42c3ea529d8e955b1e1fad0b0e2b1fb325e5ec87/debezium-connector-oracle/src/main/java/io/debezium/connector/oracle/converters/RawToStringConverter.java#L35).

## Considerations

This approach that is inspired by Kafka can be useful for the development of Debezium Server.
